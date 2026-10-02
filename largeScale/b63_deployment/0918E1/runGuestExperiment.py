#!/usr/bin/env python3
"""Run one compiled star topology and collect saturation metrics inside a VM.

Inputs: one compiled case directory, topology.json, and traffic settings from the host runner.
Outputs: raw iperf/ping files, vm_samples.csv, repeat summaries, and summary.json.
Side effects: builds/starts a Docker Compose emulation and normally removes its containers afterward.
Context: copied to each experiment VM and launched concurrently by executeVmMatrix.py.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import ipaddress
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import threading
import time
from pathlib import Path
from typing import Any


CPU_FIELDS = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")
PING_TIME_RE = re.compile(r"time[=<]([0-9.]+)\s*ms")
PING_LOSS_RE = re.compile(r"([0-9.]+)%\s+packet loss")


def parseArgs() -> argparse.Namespace:
    """Parse the case, output, traffic, and lifecycle settings supplied by the host."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-dir", type=Path, required=True)
    parser.add_argument("--result-dir", type=Path, required=True)
    parser.add_argument("--as-count", type=int, required=True)
    parser.add_argument("--duration", type=float, required=True)
    parser.add_argument("--preflight-duration", type=float, default=5)
    parser.add_argument("--preflight-retries", type=int, default=2)
    parser.add_argument("--command-grace-seconds", type=float, default=300)
    parser.add_argument("--repeats", type=int, required=True)
    parser.add_argument("--baseline-seconds", type=float, default=5)
    parser.add_argument("--cooldown-seconds", type=float, default=5)
    parser.add_argument("--ping-interval", type=float, default=0.25)
    parser.add_argument("--sample-interval", type=float, default=1)
    parser.add_argument("--startup-wait", type=float, default=30)
    parser.add_argument("--project-name", required=True)
    parser.add_argument("--build-parallelism", type=int, default=4)
    parser.add_argument("--traffic-mode", choices=("local-pair", "vm-to-vm-sink"), default="local-pair")
    parser.add_argument("--sink-address")
    parser.add_argument("--sink-port-base", type=int, default=6000)
    parser.add_argument("--source-index", type=int, default=0)
    parser.add_argument("--external-ix-interface", default="ens4")
    parser.add_argument("--external-ix-mac")
    parser.add_argument("--external-network-name", default="e1-0918e1-external-ix")
    parser.add_argument("--external-network-cidr", default="10.250.0.0/16")
    parser.add_argument("--external-network-gateway", default="10.250.0.1")
    parser.add_argument("--external-network-ip-range-prefix", type=int, default=24)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--keep-running", action="store_true")
    return parser.parse_args()


def composeCommand() -> list[str]:
    """Return the installed Docker Compose command in plugin or v1 form."""
    if shutil.which("docker"):
        result = subprocess.run(["docker", "compose", "version"], capture_output=True, check=False)
        if result.returncode == 0:
            return ["docker", "compose"]
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    raise RuntimeError("neither 'docker compose' nor 'docker-compose' is installed")


def runLogged(
    command: list[str], logPath: Path, *, cwd: Path | None = None, env: dict[str, str] | None = None
) -> None:
    """Run one lifecycle command and preserve merged output in the result directory."""
    logPath.parent.mkdir(parents=True, exist_ok=True)
    with logPath.open("w", encoding="utf-8") as log:
        log.write("+ " + " ".join(command) + "\n")
        log.flush()
        result = subprocess.run(command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"command failed with exit {result.returncode}; see {logPath}")


def readCpuCounters() -> dict[str, int]:
    """Read aggregate VM CPU jiffies from /proc/stat."""
    fields = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1:9]
    return {name: int(value) for name, value in zip(CPU_FIELDS, fields)}


def readMemory() -> dict[str, float]:
    """Return memory and swap counters from /proc/meminfo."""
    values: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        name, raw = line.split(":", 1)
        values[name] = int(raw.strip().split()[0]) * 1024
    total = values["MemTotal"]
    used = total - values.get("MemAvailable", values.get("MemFree", 0))
    swapTotal = values.get("SwapTotal", 0)
    swapFree = values.get("SwapFree", 0)
    swapUsed = max(0, swapTotal - swapFree)
    return {
        "memoryTotalBytes": float(total),
        "memoryUsedBytes": float(used),
        "memoryAvailableBytes": float(values.get("MemAvailable", values.get("MemFree", 0))),
        "memoryPercent": used * 100.0 / total,
        "swapTotalBytes": float(swapTotal),
        "swapUsedBytes": float(swapUsed),
        "swapPercent": 0.0 if swapTotal <= 0 else swapUsed * 100.0 / swapTotal,
    }


def readPressureAvg10(kind: str) -> float | None:
    """Read Linux PSI avg10 for cpu or memory pressure when supported."""
    path = Path("/proc/pressure") / kind
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("some "):
            for field in line.split()[1:]:
                if field.startswith("avg10="):
                    return float(field.split("=", 1)[1])
    return None


def readNetworkBytes() -> tuple[int, int]:
    """Sum non-loopback guest interface counters as a diagnostic VM-wide rate."""
    rxTotal = 0
    txTotal = 0
    for line in Path("/proc/net/dev").read_text(encoding="utf-8").splitlines()[2:]:
        interface, counters = line.split(":", 1)
        if interface.strip() == "lo":
            continue
        fields = counters.split()
        rxTotal += int(fields[0])
        txTotal += int(fields[8])
    return rxTotal, txTotal


class VmMonitor:
    """Sample VM resource counters while the main thread changes phase/repeat labels."""

    def __init__(self, interval: float):
        self.interval = interval
        self.samples: list[dict[str, Any]] = []
        self.phase = "startup"
        self.repeat = -1
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._collect, name="vm-monitor", daemon=True)

    def start(self) -> None:
        """Start sampling in a daemon thread."""
        self._thread.start()

    def setPhase(self, phase: str, repeat: int) -> None:
        """Label subsequent samples with the current experiment phase and repetition."""
        with self._lock:
            self.phase = phase
            self.repeat = repeat

    def stop(self) -> None:
        """Stop sampling and wait for the final sample loop to exit."""
        self._stop.set()
        self._thread.join(timeout=self.interval + 2)

    def _collect(self) -> None:
        previousCpu = readCpuCounters()
        previousRx, previousTx = readNetworkBytes()
        previousTime = time.monotonic()
        started = previousTime
        while not self._stop.wait(self.interval):
            now = time.monotonic()
            cpu = readCpuCounters()
            totalDelta = sum(cpu.values()) - sum(previousCpu.values())
            idleDelta = (cpu["idle"] + cpu["iowait"]) - (previousCpu["idle"] + previousCpu["iowait"])
            stealDelta = cpu["steal"] - previousCpu["steal"]
            cpuPercent = 0.0 if totalDelta <= 0 else max(0.0, 100.0 * (totalDelta - idleDelta) / totalDelta)
            stealPercent = 0.0 if totalDelta <= 0 else max(0.0, 100.0 * stealDelta / totalDelta)
            memory = readMemory()
            rxBytes, txBytes = readNetworkBytes()
            elapsed = max(now - previousTime, 1e-9)
            with self._lock:
                phase = self.phase
                repeat = self.repeat
            self.samples.append(
                {
                    "elapsedSeconds": now - started,
                    "phase": phase,
                    "repeat": repeat,
                    "cpuPercent": cpuPercent,
                    "cpuStealPercent": stealPercent,
                    "cpuPressureAvg10": readPressureAvg10("cpu"),
                    "memoryUsedBytes": memory["memoryUsedBytes"],
                    "memoryAvailableBytes": memory["memoryAvailableBytes"],
                    "memoryTotalBytes": memory["memoryTotalBytes"],
                    "memoryPercent": memory["memoryPercent"],
                    "memoryPressureAvg10": readPressureAvg10("memory"),
                    "swapUsedBytes": memory["swapUsedBytes"],
                    "swapTotalBytes": memory["swapTotalBytes"],
                    "swapPercent": memory["swapPercent"],
                    "load1": os.getloadavg()[0],
                    "guestRxMbps": (rxBytes - previousRx) * 8.0 / elapsed / 1_000_000,
                    "guestTxMbps": (txBytes - previousTx) * 8.0 / elapsed / 1_000_000,
                }
            )
            previousCpu = cpu
            previousRx, previousTx = rxBytes, txBytes
            previousTime = now


def percentile(values: list[float], percentage: float) -> float | None:
    """Compute a linearly interpolated percentile without third-party packages."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentage / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def runningServices(project: str) -> set[str]:
    """Return Compose service labels for running containers in this project."""
    result = subprocess.run(
        [
            "docker",
            "ps",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--format",
            '{{.Label "com.docker.compose.service"}}',
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def waitForServices(project: str, required: set[str], timeout: float) -> None:
    """Wait until all generated pair router services are running."""
    deadline = time.monotonic() + timeout
    missing = required
    while time.monotonic() < deadline:
        missing = required - runningServices(project)
        if not missing:
            return
        time.sleep(2)
    raise TimeoutError(f"services did not become ready: {sorted(missing)[:10]}")


def execCommand(composeBase: list[str], service: str, command: list[str]) -> list[str]:
    """Build a non-interactive Compose exec command for one logical router."""
    return [*composeBase, "exec", "-T", service, *command]


def buildTrafficPairs(
    topologyPairs: list[dict[str, Any]], args: argparse.Namespace
) -> list[dict[str, Any]]:
    """Return local or remote-sink pair definitions used by iperf and ping."""
    if args.traffic_mode == "local-pair":
        return list(topologyPairs)
    if not args.sink_address:
        raise ValueError("--sink-address is required for vm-to-vm-sink traffic")
    pairs: list[dict[str, Any]] = []
    for pair in topologyPairs:
        remotePair = dict(pair)
        remotePair["serverAddress"] = str(args.sink_address)
        remotePair["port"] = int(args.sink_port_base) + int(args.source_index) * 512 + int(pair["index"])
        remotePair["serverService"] = "external-sink"
        pairs.append(remotePair)
    return pairs


def createExternalIxNetwork(args: argparse.Namespace) -> dict[str, Any]:
    """Create the per-VM macvlan network on the shared external IX interface."""
    externalInterface = str(args.external_ix_interface)
    if not Path(f"/sys/class/net/{externalInterface}").exists():
        if not args.external_ix_mac:
            raise RuntimeError(f"external IX interface does not exist: {externalInterface}")
        matches = subprocess.run(
            ["bash", "-lc", f"ip -o link | awk '$0 ~ /{args.external_ix_mac.lower()}/ {{print $2}}' | tr -d ':'"],
            text=True,
            capture_output=True,
            check=False,
        ).stdout.strip().splitlines()
        if not matches:
            raise RuntimeError(f"cannot resolve external IX interface for MAC {args.external_ix_mac}")
        externalInterface = matches[0]
    subprocess.run(["sudo", "ip", "link", "set", "dev", externalInterface, "up"], check=True)
    subnet = ipaddress.ip_network(str(args.external_network_cidr), strict=True)
    gateway = ipaddress.ip_address(str(args.external_network_gateway))
    prefix = int(args.external_network_ip_range_prefix)
    if prefix <= subnet.prefixlen or prefix > 30:
        raise ValueError("external network IP range prefix must be longer than subnet prefix")
    rangeSize = 1 << (32 - prefix)
    rangeAddress = ipaddress.ip_address(int(subnet.network_address) + int(args.source_index) * rangeSize)
    ipRange = ipaddress.ip_network((int(rangeAddress), prefix), strict=True)
    if not ipRange.subnet_of(subnet) or gateway not in subnet:
        raise ValueError("external network IP range is outside the configured shared subnet")
    inspect = subprocess.run(
        ["docker", "network", "inspect", str(args.external_network_name)],
        text=True,
        capture_output=True,
        check=False,
    )
    if inspect.returncode == 0:
        try:
            documents = json.loads(inspect.stdout)
            containers = documents[0].get("Containers") or {}
        except (json.JSONDecodeError, IndexError, TypeError) as exc:
            raise RuntimeError(f"cannot inspect existing external network: {exc}") from exc
        if containers:
            raise RuntimeError(
                f"external network {args.external_network_name} still has containers; refusing to replace it"
            )
        subprocess.run(["docker", "network", "rm", str(args.external_network_name)], check=True)
    command = [
        "docker",
        "network",
        "create",
        "--driver",
        "macvlan",
        "--opt",
        f"parent={externalInterface}",
        "--subnet",
        str(subnet),
        "--gateway",
        str(gateway),
        "--ip-range",
        str(ipRange),
        str(args.external_network_name),
    ]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"external IX Docker network creation failed: {result.stderr.strip()}")
    return {
        "name": str(args.external_network_name),
        "driver": "macvlan",
        "parentInterface": externalInterface,
        "subnet": str(subnet),
        "ipRange": str(ipRange),
        "gateway": str(gateway),
        "sourceIndex": int(args.source_index),
    }


def inspectBgp(composeBase: list[str], pairs: list[dict[str, Any]]) -> dict[str, Any]:
    """Record non-gating RS session status so direct-pair traffic remains independently usable."""
    def inspectPair(pair: dict[str, Any]) -> tuple[int, bool]:
        """Inspect one border router and return its ASN plus RS establishment state."""
        result = subprocess.run(
            execCommand(composeBase, pair["clientService"], ["birdc", "show", "protocols"]),
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
        expectedName = "p_rs100"
        isEstablished = result.returncode == 0 and any(
            expectedName in line and "Established" in line for line in result.stdout.splitlines()
        )
        return int(pair["asn"]), isEstablished

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(pairs))) as executor:
        states = list(executor.map(inspectPair, pairs))
    failures = [asn for asn, isEstablished in states if not isEstablished]
    return {"checked": len(states), "established": len(states) - len(failures), "failedAsns": failures}


def parseIperf(raw: str, returnCode: int) -> dict[str, Any]:
    """Extract receiver throughput from one iperf3 JSON document."""
    result: dict[str, Any] = {"returnCode": returnCode, "success": False, "bitsPerSecond": None}
    try:
        document = json.loads(raw)
        result["error"] = document.get("error")
        end = document.get("end") or {}
        summary = end.get("sum_received") or end.get("sum") or {}
        bitsPerSecond = summary.get("bits_per_second")
        if returnCode == 0 and bitsPerSecond is not None:
            result["success"] = True
            result["bitsPerSecond"] = float(bitsPerSecond)
    except json.JSONDecodeError as exc:
        result["error"] = f"invalid JSON: {exc}"
    return result


def parsePing(raw: str, returnCode: int) -> dict[str, Any]:
    """Extract individual RTTs and loss from standard iputils ping output."""
    rtts = [float(value) for value in PING_TIME_RE.findall(raw)]
    lossMatch = PING_LOSS_RE.search(raw)
    return {
        "returnCode": returnCode,
        "success": returnCode == 0 and bool(rtts),
        "packetLossPercent": float(lossMatch.group(1)) if lossMatch else None,
        "rttSamplesMs": rtts,
        "averageRttMs": statistics.fmean(rtts) if rtts else None,
        "p95RttMs": percentile(rtts, 95),
    }


def runIperfPreflight(
    composeBase: list[str],
    pairs: list[dict[str, Any]],
    resultDir: Path,
    duration: float,
    commandGraceSeconds: float,
    retries: int,
) -> dict[str, Any]:
    """Verify every pair, retrying transient startup/L2 failures with raw evidence."""
    preflightDir = resultDir / "preflight" / "iperf"
    preflightDir.mkdir(parents=True, exist_ok=True)
    maxAttempts = max(1, int(retries) + 1)
    pending = list(pairs)
    finalResults: dict[int, dict[str, Any]] = {}
    recoveredAsns: list[int] = []
    attemptSummaries: list[dict[str, Any]] = []

    for attempt in range(1, maxAttempts + 1):
        if not pending:
            break
        attemptDir = preflightDir / f"attempt-{attempt:02d}"
        attemptDir.mkdir(parents=True, exist_ok=True)
        processes: list[tuple[dict[str, Any], subprocess.Popen[str]]] = []
        for pair in pending:
            command = execCommand(
                composeBase,
                pair["clientService"],
                [
                    "iperf3",
                    "-c",
                    str(pair["serverAddress"]),
                    "-p",
                    str(pair["port"]),
                    "-t",
                    str(duration),
                    "-J",
                ],
            )
            processes.append(
                (
                    pair,
                    subprocess.Popen(
                        command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
                    ),
                )
            )

        nextPending: list[dict[str, Any]] = []
        attemptResults: list[dict[str, Any]] = []
        for pair, process in processes:
            output, _ = process.communicate(timeout=duration + commandGraceSeconds)
            asn = int(pair["asn"])
            (attemptDir / f"as-{asn}.json").write_text(output, encoding="utf-8")
            parsed = parseIperf(output, process.returncode)
            parsed["asn"] = asn
            parsed["attempt"] = attempt
            attemptResults.append(parsed)
            finalResults[asn] = parsed
            if not parsed["success"]:
                nextPending.append(pair)
            elif asn in {int(item["asn"]) for item in finalResults.values() if item.get("attempt", 1) > 1}:
                recoveredAsns.append(asn)
        pending = nextPending
        attemptSummaries.append(
            {
                "attempt": attempt,
                "configuredPairCount": len(attemptResults),
                "successCount": sum(1 for item in attemptResults if item["success"]),
                "failureCount": sum(1 for item in attemptResults if not item["success"]),
                "failedAsns": [int(item["asn"]) for item in attemptResults if not item["success"]],
            }
        )
        if pending and attempt < maxAttempts:
            time.sleep(1.0)

    results = [finalResults[int(pair["asn"])] for pair in pairs if int(pair["asn"]) in finalResults]
    successful = [item for item in results if item["success"]]
    failedAsns = [int(item["asn"]) for item in results if not item["success"]]
    recoveredAsns = sorted(set(recoveredAsns))
    (preflightDir / "summary.json").write_text(
        json.dumps(
            {
                "durationSeconds": duration,
                "configuredPairCount": len(pairs),
                "maxAttempts": maxAttempts,
                "attempts": attemptSummaries,
                "recoveredAsns": recoveredAsns,
                "results": results,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    summary = {
        "durationSeconds": duration,
        "configuredPairCount": len(pairs),
        "successCount": len(successful),
        "failureCount": len(failedAsns),
        "failedAsns": failedAsns,
        "maxAttempts": maxAttempts,
        "attempts": attemptSummaries,
        "recoveredAsns": recoveredAsns,
        "status": "PASS" if not failedAsns else "FAIL",
        "results": results,
    }
    (resultDir / "preflight" / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary


def summarizeVmSamples(samples: list[dict[str, Any]], repeat: int) -> dict[str, Any]:
    """Summarize traffic-phase VM samples for one repetition."""
    selected = [sample for sample in samples if sample["repeat"] == repeat and sample["phase"] == "traffic"]
    cpu = [float(sample["cpuPercent"]) for sample in selected]
    cpuSteal = [float(sample["cpuStealPercent"]) for sample in selected]
    cpuPressure = [
        float(sample["cpuPressureAvg10"]) for sample in selected if sample.get("cpuPressureAvg10") is not None
    ]
    memory = [float(sample["memoryPercent"]) for sample in selected]
    usedBytes = [float(sample["memoryUsedBytes"]) for sample in selected]
    availableBytes = [float(sample["memoryAvailableBytes"]) for sample in selected]
    memoryPressure = [
        float(sample["memoryPressureAvg10"])
        for sample in selected
        if sample.get("memoryPressureAvg10") is not None
    ]
    swapBytes = [float(sample["swapUsedBytes"]) for sample in selected]
    swapPercent = [float(sample["swapPercent"]) for sample in selected]
    guestRates = [float(sample["guestRxMbps"]) + float(sample["guestTxMbps"]) for sample in selected]
    return {
        "sampleCount": len(selected),
        "cpuMeanPercent": statistics.fmean(cpu) if cpu else None,
        "cpuP95Percent": percentile(cpu, 95),
        "cpuPeakPercent": max(cpu) if cpu else None,
        "cpuStealMeanPercent": statistics.fmean(cpuSteal) if cpuSteal else None,
        "cpuPressureAvg10Mean": statistics.fmean(cpuPressure) if cpuPressure else None,
        "memoryMeanPercent": statistics.fmean(memory) if memory else None,
        "memoryPeakPercent": max(memory) if memory else None,
        "memoryPeakBytes": max(usedBytes) if usedBytes else None,
        "memoryAvailableMeanBytes": statistics.fmean(availableBytes) if availableBytes else None,
        "memoryPressureAvg10Mean": statistics.fmean(memoryPressure) if memoryPressure else None,
        "swapMeanBytes": statistics.fmean(swapBytes) if swapBytes else None,
        "swapPeakBytes": max(swapBytes) if swapBytes else None,
        "swapMeanPercent": statistics.fmean(swapPercent) if swapPercent else None,
        "guestInterfaceAggregateMeanMbps": statistics.fmean(guestRates) if guestRates else None,
    }


def runTrafficRepeat(
    composeBase: list[str],
    pairs: list[dict[str, Any]],
    repeatDir: Path,
    duration: float,
    commandGraceSeconds: float,
    pingInterval: float,
) -> dict[str, Any]:
    """Launch unlimited TCP iperf clients and paired pings, then preserve raw results."""
    iperfDir = repeatDir / "iperf"
    pingDir = repeatDir / "ping"
    iperfDir.mkdir(parents=True, exist_ok=True)
    pingDir.mkdir(parents=True, exist_ok=True)
    iperfProcesses: list[tuple[dict[str, Any], subprocess.Popen[str]]] = []
    pingProcesses: list[tuple[dict[str, Any], subprocess.Popen[str]]] = []
    pingCount = max(1, int(duration / pingInterval))

    for pair in pairs:
        command = execCommand(
            composeBase,
            pair["clientService"],
            [
                "iperf3",
                "-c",
                str(pair["serverAddress"]),
                "-p",
                str(pair["port"]),
                "-t",
                str(duration),
                "-J",
            ],
        )
        iperfProcesses.append(
            (pair, subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT))
        )

    time.sleep(0.5)
    for pair in pairs:
        command = execCommand(
            composeBase,
            pair["clientService"],
            ["ping", "-n", "-i", str(pingInterval), "-c", str(pingCount), str(pair["serverAddress"])],
        )
        pingProcesses.append(
            (pair, subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT))
        )

    iperfResults: list[dict[str, Any]] = []
    pingResults: list[dict[str, Any]] = []
    for pair, process in iperfProcesses:
        output, _ = process.communicate(timeout=duration + commandGraceSeconds)
        asn = int(pair["asn"])
        (iperfDir / f"as-{asn}.json").write_text(output, encoding="utf-8")
        parsed = parseIperf(output, process.returncode)
        parsed["asn"] = asn
        iperfResults.append(parsed)
    for pair, process in pingProcesses:
        output, _ = process.communicate(timeout=duration + commandGraceSeconds)
        asn = int(pair["asn"])
        (pingDir / f"as-{asn}.txt").write_text(output, encoding="utf-8")
        parsed = parsePing(output, process.returncode)
        parsed["asn"] = asn
        pingResults.append(parsed)

    throughputs = [float(item["bitsPerSecond"]) for item in iperfResults if item["success"]]
    allRtts = [float(rtt) for item in pingResults for rtt in item["rttSamplesMs"]]
    losses = [float(item["packetLossPercent"]) for item in pingResults if item["packetLossPercent"] is not None]
    result = {
        "configuredPairCount": len(pairs),
        "trafficMode": "tcp-unlimited",
        "iperfSuccessCount": len(throughputs),
        "iperfFailureCount": len(pairs) - len(throughputs),
        "averagePairMbps": statistics.fmean(throughputs) / 1_000_000 if throughputs else None,
        "p50PairMbps": percentile(throughputs, 50) / 1_000_000 if throughputs else None,
        "p05PairMbps": percentile(throughputs, 5) / 1_000_000 if throughputs else None,
        "totalMeasuredMbps": sum(throughputs) / 1_000_000,
        "pingSuccessCount": sum(1 for item in pingResults if item["success"]),
        "averageRttMs": statistics.fmean(allRtts) if allRtts else None,
        "p95RttMs": percentile(allRtts, 95),
        "averagePacketLossPercent": statistics.fmean(losses) if losses else None,
        "iperfResults": iperfResults,
        "pingResults": pingResults,
    }
    result["iperfComplete"] = result["iperfSuccessCount"] == len(pairs)
    result["failedIperfAsns"] = [
        int(item["asn"]) for item in iperfResults if not item["success"]
    ]
    return result


def writeVmSamples(path: Path, samples: list[dict[str, Any]]) -> None:
    """Write ordered VM telemetry to a stable CSV schema."""
    fieldNames = [
        "elapsedSeconds",
        "phase",
        "repeat",
        "cpuPercent",
        "cpuStealPercent",
        "cpuPressureAvg10",
        "memoryUsedBytes",
        "memoryAvailableBytes",
        "memoryTotalBytes",
        "memoryPercent",
        "memoryPressureAvg10",
        "swapUsedBytes",
        "swapTotalBytes",
        "swapPercent",
        "load1",
        "guestRxMbps",
        "guestTxMbps",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldNames)
        writer.writeheader()
        writer.writerows(samples)


def medianField(repeats: list[dict[str, Any]], path: tuple[str, ...]) -> float | None:
    """Return the median non-null numeric value at one nested repeat-summary path."""
    values: list[float] = []
    for repeat in repeats:
        value: Any = repeat
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if value is not None:
            values.append(float(value))
    return statistics.median(values) if values else None


def main() -> int:
    """Build/start the case, execute all repetitions, summarize, and clean containers."""
    args = parseArgs()
    caseDir = args.case_dir.expanduser().resolve()
    resultDir = args.result_dir.expanduser().resolve()
    resultDir.mkdir(parents=True, exist_ok=False)
    composePath = caseDir / "docker-compose.yml"
    topologyPath = caseDir / "topology.json"
    topology = json.loads(topologyPath.read_text(encoding="utf-8"))
    if int(topology["asCount"]) != args.as_count:
        raise ValueError("--as-count does not match topology.json")
    topologyPairs = list(topology["pairs"])
    pairs = buildTrafficPairs(topologyPairs, args)
    compose = composeCommand()
    composeBase = [*compose, "-p", args.project_name, "-f", str(composePath)]
    environment = os.environ.copy()
    environment["COMPOSE_PARALLEL_LIMIT"] = str(args.build_parallelism)
    startedAt = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    monitor = VmMonitor(args.sample_interval)
    summary: dict[str, Any] = {
        "schemaVersion": 1,
        "status": "RUNNING",
        "startedAt": startedAt,
        "hostname": os.uname().nodename,
        "asCount": args.as_count,
        "routerCount": int(topology["routerCount"]),
        "iperfPairCount": int(topology["iperfPairCount"]),
        "routeServerPeering": bool(topology["routeServerPeering"]),
        "trafficDirection": (
            "border-to-remote-sink" if args.traffic_mode == "vm-to-vm-sink" else topology["trafficDirection"]
        ),
        "repeats": [],
    }

    try:
        if args.traffic_mode == "vm-to-vm-sink":
            summary["externalIxNetwork"] = createExternalIxNetwork(args)
        if not args.skip_build:
            runLogged([*composeBase, "build"], resultDir / "compose-build.log", cwd=caseDir, env=environment)
        required = {pair[role] for pair in topologyPairs for role in ("clientService", "serverService")}
        required.add(str(topology["routeServerService"]))
        runLogged(
            [*composeBase, "up", "-d", "--no-deps", "--remove-orphans", *sorted(required)],
            resultDir / "compose-up.log",
            cwd=caseDir,
            env=environment,
        )
        waitForServices(args.project_name, required, timeout=max(120.0, args.startup_wait * 4))
        time.sleep(args.startup_wait)
        summary["bgp"] = inspectBgp(composeBase, topologyPairs) if topology["routeServerPeering"] else {"checked": 0}
        summary["preflight"] = runIperfPreflight(
            composeBase,
            pairs,
            resultDir,
            args.preflight_duration,
            args.command_grace_seconds,
            args.preflight_retries,
        )
        if summary["preflight"]["status"] != "PASS":
            raise RuntimeError(
                f"iperf preflight failed for {summary['preflight']['failureCount']}/{len(pairs)} pairs; "
                f"ASNs={summary['preflight']['failedAsns'][:20]}"
            )
        monitor.start()

        for repeat in range(args.repeats):
            monitor.setPhase("baseline", repeat)
            time.sleep(args.baseline_seconds)
            repeatDir = resultDir / f"repeat-{repeat + 1:02d}"
            monitor.setPhase("traffic", repeat)
            trafficSummary = runTrafficRepeat(
                composeBase,
                pairs,
                repeatDir,
                args.duration,
                args.command_grace_seconds,
                args.ping_interval,
            )
            monitor.setPhase("cooldown", repeat)
            time.sleep(args.cooldown_seconds)
            trafficSummary["vm"] = summarizeVmSamples(monitor.samples, repeat)
            trafficSummary["repeat"] = repeat + 1
            (repeatDir / "summary.json").write_text(
                json.dumps(trafficSummary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            summary["repeats"].append(trafficSummary)

        monitor.stop()
        writeVmSamples(resultDir / "vm_samples.csv", monitor.samples)
        repeats = summary["repeats"]
        completeRepeats = [repeat for repeat in repeats if repeat.get("iperfComplete")]
        summary["trafficQuality"] = {
            "preflightPassed": summary["preflight"]["status"] == "PASS",
            "requiredRepeatCount": args.repeats,
            "completedRepeatCount": len(completeRepeats),
            "incompleteRepeatNumbers": [
                int(repeat["repeat"]) for repeat in repeats if not repeat.get("iperfComplete")
            ],
            "valid": len(completeRepeats) == args.repeats,
        }
        summary["aggregate"] = {
            "repetitionCount": len(repeats),
            "medianIperfSuccessCount": medianField(repeats, ("iperfSuccessCount",)),
            "medianAveragePairMbps": medianField(repeats, ("averagePairMbps",)),
            "medianTotalMeasuredMbps": medianField(repeats, ("totalMeasuredMbps",)),
            "medianAverageRttMs": medianField(repeats, ("averageRttMs",)),
            "medianP95RttMs": medianField(repeats, ("p95RttMs",)),
            "medianPacketLossPercent": medianField(repeats, ("averagePacketLossPercent",)),
            "medianCpuMeanPercent": medianField(repeats, ("vm", "cpuMeanPercent")),
            "medianCpuP95Percent": medianField(repeats, ("vm", "cpuP95Percent")),
            "medianCpuStealMeanPercent": medianField(repeats, ("vm", "cpuStealMeanPercent")),
            "medianCpuPressureAvg10Mean": medianField(repeats, ("vm", "cpuPressureAvg10Mean")),
            "medianMemoryMeanPercent": medianField(repeats, ("vm", "memoryMeanPercent")),
            "medianMemoryPeakBytes": medianField(repeats, ("vm", "memoryPeakBytes")),
            "medianMemoryAvailableMeanBytes": medianField(
                repeats, ("vm", "memoryAvailableMeanBytes")
            ),
            "medianMemoryPressureAvg10Mean": medianField(
                repeats, ("vm", "memoryPressureAvg10Mean")
            ),
            "medianSwapMeanBytes": medianField(repeats, ("vm", "swapMeanBytes")),
            "medianSwapPeakBytes": medianField(repeats, ("vm", "swapPeakBytes")),
            "medianGuestInterfaceAggregateMbps": medianField(
                repeats, ("vm", "guestInterfaceAggregateMeanMbps")
            ),
            "estimatedAvoidedCrossVmMbps": medianField(repeats, ("totalMeasuredMbps",)),
            "estimateAssumption": (
                "measured aggregate pair throughput would otherwise cross a machine boundary"
            ),
        }
        summary["status"] = "PASS"
        returnCode = 0
    except Exception as exc:
        if monitor._thread.is_alive():
            monitor.stop()
            writeVmSamples(resultDir / "vm_samples.csv", monitor.samples)
        summary["status"] = "FAIL"
        summary["error"] = f"{type(exc).__name__}: {exc}"
        returnCode = 1
    finally:
        summary["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        (resultDir / "summary.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if not args.keep_running:
            subprocess.run(
                [*composeBase, "down", "--remove-orphans"],
                cwd=caseDir,
                env=environment,
                stdout=(resultDir / "compose-down.log").open("w", encoding="utf-8"),
                stderr=subprocess.STDOUT,
                check=False,
            )
            if args.traffic_mode == "vm-to-vm-sink":
                subprocess.run(
                    ["docker", "network", "rm", str(args.external_network_name)],
                    stdout=(resultDir / "external-network-down.log").open("w", encoding="utf-8"),
                    stderr=subprocess.STDOUT,
                    check=False,
                )
    return returnCode


if __name__ == "__main__":
    raise SystemExit(main())
