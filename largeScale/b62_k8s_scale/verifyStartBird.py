#!/usr/bin/env python3
"""Verify BIRD protocol health after the B62 start-bird stage.

Inputs are the namespace, run artifact directory, cluster inventory, and
kubeconfig. The script waits until every VM in the inventory has load1 below
the configured threshold, selects one fixed brd pod from each of the first N
ASNs, and checks BIRD protocols through kubectl exec.

Outputs are verify_after_start_bird_targets.json and
verify_after_start_bird_summary.json in the run directory.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


EXIT_VERIFY_FAILED = 61
TARGET_RE = re.compile(r"^as(?P<asn>\d+)brd-r(?P<rid>\d+)-")
OUTPUT_MARKER = "__SEEDEMU_START_BIRD_ROUTE_COUNT__"
BAD_INFO_RE = re.compile(r"(error|fail|unreachable|no route|refused|timeout|inactive)", re.IGNORECASE)


@dataclass
class InventoryNode:
    name: str
    host: str
    user: str
    key: str


@dataclass
class BirdTarget:
    asn: int
    routerId: int
    pod: str
    node: str


def nowUtc() -> str:
    """Return a compact UTC timestamp for logs."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log(message: str) -> None:
    """Print one timestamped log line."""
    print(f"[{nowUtc()}] {message}", flush=True)


def runCommand(cmd: list[str], timeout: int | None) -> subprocess.CompletedProcess[str]:
    """Run one local command and convert timeout into return code 124."""
    try:
        return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else f"timeout after {timeout}s"
        return subprocess.CompletedProcess(cmd, 124, stdout, stderr)


def loadInventory(path: Path) -> dict[str, InventoryNode]:
    """Load node SSH metadata from cluster.inventory.yaml."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    nodes: dict[str, InventoryNode] = {}
    for item in data.get("nodes", []):
        name = str(item.get("name", "")).strip()
        host = str(item.get("managementIp") or item.get("management_ip") or item.get("ip") or "").strip()
        ssh = item.get("ssh", {}) or {}
        if not name or not host:
            continue
        nodes[name] = InventoryNode(
            name=name,
            host=host,
            user=str(ssh.get("user") or item.get("sshUser") or "ubuntu"),
            key=str(Path(str(ssh.get("key") or item.get("sshKey") or "~/.ssh/id_ed25519")).expanduser()),
        )
    if not nodes:
        raise SystemExit(f"No nodes found in {path}")
    return nodes


def sshCommand(node: InventoryNode, remote: str, timeout: int | None) -> subprocess.CompletedProcess[str]:
    """Run one remote shell command on a VM through SSH."""
    return runCommand(
        [
            "ssh",
            "-i",
            node.key,
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=8",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "LogLevel=ERROR",
            f"{node.user}@{node.host}",
            remote,
        ],
        timeout=timeout,
    )


def readNodeLoad(node: InventoryNode) -> tuple[str, float | None, str]:
    """Read load1 from one VM."""
    result = sshCommand(node, "awk '{print $1}' /proc/loadavg", timeout=12)
    if result.returncode != 0:
        return node.name, None, (result.stderr or result.stdout).strip()
    try:
        return node.name, float(result.stdout.strip().split()[0]), ""
    except (IndexError, ValueError) as exc:
        return node.name, None, str(exc)


def collectNodeLoads(nodes: dict[str, InventoryNode]) -> dict[str, dict[str, Any]]:
    """Collect load1 values from every VM in parallel."""
    samples: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(16, max(1, len(nodes)))) as pool:
        futures = {pool.submit(readNodeLoad, node): name for name, node in nodes.items()}
        for future in as_completed(futures):
            name, load1, error = future.result()
            samples[name] = {"load1": load1, "error": error}
    return dict(sorted(samples.items()))


def waitForClusterLoad(
    nodes: dict[str, InventoryNode],
    threshold: float,
    interval_seconds: int,
    max_wait_seconds: int,
) -> tuple[float, int, dict[str, dict[str, Any]]]:
    """Wait until every VM load1 is below threshold and return final samples."""
    started = time.time()
    interval_seconds = max(1, interval_seconds)
    while True:
        samples = collectNodeLoads(nodes)
        loads = [item["load1"] for item in samples.values() if isinstance(item.get("load1"), float)]
        max_load = max(loads) if loads else float("inf")
        blocked = [
            f"{name}={item['load1'] if item.get('load1') is not None else item.get('error')}"
            for name, item in samples.items()
            if item.get("load1") is None or float(item["load1"]) >= threshold
        ]
        waited = int(time.time() - started)
        if not blocked:
            log(f"load gate passed max_load1={max_load:.2f} waited={waited}s")
            return max_load, waited, samples
        log(f"load gate waiting threshold={threshold:g} waited={waited}s blocked={'; '.join(blocked[:8])}")
        if max_wait_seconds > 0 and waited >= max_wait_seconds:
            raise TimeoutError(f"load1 did not fall below {threshold:g} within {max_wait_seconds}s")
        time.sleep(interval_seconds)


def kubectlListPods(namespace: str, kubeconfig: str, timeout: int) -> dict[str, Any]:
    """Return raw Kubernetes pod JSON for one namespace."""
    result = runCommand(
        [
            "kubectl",
            "--kubeconfig",
            kubeconfig,
            f"--request-timeout={timeout}s",
            "-n",
            namespace,
            "get",
            "pods",
            "-o",
            "json",
        ],
        timeout=timeout,
    )
    result.check_returncode()
    return json.loads(result.stdout)


def selectTargets(namespace: str, kubeconfig: str, sample_count: int, list_timeout: int) -> list[BirdTarget]:
    """Select the smallest-r brd pod from each of the first sample_count ASNs."""
    data = kubectlListPods(namespace, kubeconfig, list_timeout)
    best_by_asn: dict[int, BirdTarget] = {}
    for item in data.get("items", []):
        metadata = item.get("metadata", {}) or {}
        labels = metadata.get("labels", {}) or {}
        status = item.get("status", {}) or {}
        if status.get("phase") != "Running":
            continue
        if labels.get("seedemu.io/workload") != "seedemu" or labels.get("seedemu.io/role") != "brd":
            continue
        pod = str(metadata.get("name", ""))
        match = TARGET_RE.match(pod)
        if not match:
            continue
        asn = int(str(labels.get("seedemu.io/asn") or match.group("asn")))
        router_id = int(match.group("rid"))
        target = BirdTarget(
            asn=asn,
            routerId=router_id,
            pod=pod,
            node=str((item.get("spec", {}) or {}).get("nodeName", "")),
        )
        current = best_by_asn.get(asn)
        if current is None or (target.routerId, target.pod) < (current.routerId, current.pod):
            best_by_asn[asn] = target
    selected = [best_by_asn[asn] for asn in sorted(best_by_asn)[:sample_count]]
    if len(selected) < sample_count:
        raise RuntimeError(f"only found {len(selected)} distinct AS brd pods; need {sample_count}")
    return selected


def podExec(kubeconfig: str, namespace: str, pod: str, shell_cmd: str, timeout: int) -> subprocess.CompletedProcess[str]:
    """Run a read-only command inside one selected pod through kubectl exec."""
    return runCommand(
        [
            "kubectl",
            "--kubeconfig",
            kubeconfig,
            "-n",
            namespace,
            "exec",
            pod,
            "--",
            "sh",
            "-lc",
            shell_cmd,
        ],
        timeout=timeout,
    )


def parseProtocolRows(output: str) -> list[dict[str, str]]:
    """Parse `birdc show protocols` rows into dictionaries."""
    rows: list[dict[str, str]] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("BIRD ") or stripped.startswith("Name "):
            continue
        parts = stripped.split(None, 5)
        if len(parts) < 4:
            continue
        rows.append(
            {
                "name": parts[0],
                "proto": parts[1],
                "table": parts[2],
                "state": parts[3],
                "since": parts[4] if len(parts) >= 5 else "",
                "info": parts[5] if len(parts) >= 6 else "",
                "line": stripped,
            }
        )
    return rows


def splitCommandOutput(output: str) -> tuple[str, str]:
    """Split combined protocol and route-count output."""
    if OUTPUT_MARKER not in output:
        return output, ""
    return output.split(OUTPUT_MARKER, 1)


def parseRouteCount(output: str) -> tuple[int | None, int | None]:
    """Parse total route and network counts from `birdc show route count`."""
    match = re.search(r"Total:\s+(\d+)\s+of\s+\d+\s+routes\s+for\s+(\d+)\s+networks", output, re.IGNORECASE)
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def assessProtocols(rows: list[dict[str, str]], route_output: str) -> tuple[list[dict[str, Any]], dict[str, int | None]]:
    """Return protocol failures plus route metrics for one BIRD instance."""
    failures: list[dict[str, Any]] = []
    bgp_count = 0
    established_bgp = 0
    for row in rows:
        proto = row["proto"].upper()
        state = row["state"].lower()
        info = row.get("info", "")
        if proto == "BGP" or row["name"].lower().startswith(("bgp", "ebgp", "ibgp")):
            bgp_count += 1
            if state == "up" and "established" in info.lower():
                established_bgp += 1
                continue
            failures.append({**row, "reason": "bgp_not_established"})
            continue
        if state != "up" or BAD_INFO_RE.search(info):
            failures.append({**row, "reason": "protocol_not_up"})
    routes, networks = parseRouteCount(route_output)
    if bgp_count == 0:
        failures.append({"name": "<none>", "proto": "BGP", "state": "missing", "info": "no BGP protocols found"})
    if routes is None or networks is None or routes <= 0 or networks <= 0:
        failures.append({"name": "<routes>", "proto": "Route", "state": "invalid", "info": route_output.strip()})
    return failures, {
        "routes": routes,
        "networks": networks,
        "bgpProtocols": bgp_count,
        "establishedBgpProtocols": established_bgp,
    }


def checkTarget(
    target: BirdTarget,
    nodes: dict[str, InventoryNode],
    kubeconfig: str,
    namespace: str,
    exec_timeout: int,
) -> dict[str, Any]:
    """Check one selected brd pod and return a per-sample result."""
    node = nodes.get(target.node)
    if node is None:
        return {**asdict(target), "passed": False, "failureReason": "node_missing_from_inventory"}
    command = (
        "birdc show protocols; rc1=$?; "
        f"printf '\\n{OUTPUT_MARKER}\\n'; "
        "birdc show route count; rc2=$?; "
        'if [ "$rc1" -ne 0 ]; then exit "$rc1"; fi; exit "$rc2"'
    )
    result = podExec(kubeconfig, namespace, target.pod, command, timeout=exec_timeout)
    protocols_output, route_output = splitCommandOutput(result.stdout)
    rows = parseProtocolRows(protocols_output)
    failures, metrics = assessProtocols(rows, route_output)
    passed = result.returncode == 0 and not failures
    payload: dict[str, Any] = {
        **asdict(target),
        "passed": passed,
        "returnCode": result.returncode,
        "metrics": metrics,
    }
    if not passed:
        payload["failureReason"] = "protocol_check_failed" if result.returncode == 0 else "birdc_command_failed"
        payload["stderr"] = (result.stderr or "").strip()
        payload["failedProtocols"] = failures[:50]
        payload["protocolsRaw"] = protocols_output.strip()
        payload["routeCountRaw"] = route_output.strip()
    return payload


def checkTargets(
    targets: list[BirdTarget],
    nodes: dict[str, InventoryNode],
    kubeconfig: str,
    namespace: str,
    exec_timeout: int,
) -> list[dict[str, Any]]:
    """Check selected targets in parallel and return deterministic results."""
    checks: list[dict[str, Any]] = []
    grouped: dict[str, list[BirdTarget]] = defaultdict(list)
    for target in targets:
        grouped[target.node].append(target)
    with ThreadPoolExecutor(max_workers=max(1, len(grouped))) as pool:
        futures = [
            pool.submit(checkTarget, target, nodes, kubeconfig, namespace, exec_timeout)
            for target in targets
        ]
        for future in as_completed(futures):
            checks.append(future.result())
    checks.sort(key=lambda item: (int(item["asn"]), int(item["routerId"]), str(item["pod"])))
    return checks


def parseArgs(argv: list[str]) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--artifact-dir", required=True, type=Path)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--sample-count", type=int, default=10)
    parser.add_argument("--load-threshold", type=float, default=50.0)
    parser.add_argument("--load-check-interval-seconds", type=int, default=30)
    parser.add_argument("--load-max-wait-seconds", type=int, default=0)
    parser.add_argument("--exec-timeout-seconds", type=int, default=60)
    parser.add_argument("--list-timeout-seconds", type=int, default=300)
    parser.add_argument("--protocol-wait-seconds", type=int, default=300)
    parser.add_argument("--protocol-check-interval-seconds", type=int, default=20)
    return parser.parse_args(argv)


def main() -> int:
    """CLI entrypoint."""
    args = parseArgs(sys.argv[1:])
    started = time.time()
    args.artifact_dir.mkdir(parents=True, exist_ok=True)
    targets_path = args.artifact_dir / "verify_after_start_bird_targets.json"
    summary_path = args.artifact_dir / "verify_after_start_bird_summary.json"

    summary: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "namespace": args.namespace,
        "success": False,
        "status": "FAIL",
        "load1": None,
        "load_wait_seconds": None,
        "sample_count": 0,
        "selected_containers": [],
        "failed_samples": [],
        "protocol_attempts": 0,
        "protocol_convergence_wait_seconds": 0,
        "protocol_wait_seconds": args.protocol_wait_seconds,
        "duration_seconds": None,
        "strategy": "first-10-as-smallest-r-brd-kubectl-exec",
    }
    try:
        nodes = loadInventory(args.inventory)
        max_load, load_wait, load_samples = waitForClusterLoad(
            nodes,
            threshold=args.load_threshold,
            interval_seconds=args.load_check_interval_seconds,
            max_wait_seconds=args.load_max_wait_seconds,
        )
        targets = selectTargets(args.namespace, args.kubeconfig, args.sample_count, args.list_timeout_seconds)
        targets_path.write_text(json.dumps([asdict(target) for target in targets], indent=2), encoding="utf-8")
        log("selected targets: " + ", ".join(f"AS{target.asn}:{target.pod}@{target.node}" for target in targets))

        protocol_started = time.time()
        protocol_attempts = 0
        checks: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        interval = max(1, args.protocol_check_interval_seconds)
        while True:
            protocol_attempts += 1
            checks = checkTargets(targets, nodes, args.kubeconfig, args.namespace, args.exec_timeout_seconds)
            failures = [item for item in checks if not item.get("passed")]
            if not failures:
                break
            waited = int(time.time() - protocol_started)
            if args.protocol_wait_seconds <= 0 or waited >= args.protocol_wait_seconds:
                break
            failure_summary = ", ".join(
                f"AS{item.get('asn')}:{len(item.get('failedProtocols', []))}"
                for item in failures[:8]
            )
            log(
                "protocol convergence waiting "
                f"attempt={protocol_attempts} waited={waited}s failures={len(failures)} {failure_summary}"
            )
            remaining = args.protocol_wait_seconds - waited
            time.sleep(min(interval, max(1, remaining)))
        summary.update(
            {
                "success": not failures,
                "status": "PASS" if not failures else "FAIL",
                "load1": max_load,
                "load_wait_seconds": load_wait,
                "node_loads": load_samples,
                "sample_count": len(checks),
                "selected_containers": checks,
                "failed_samples": failures,
                "protocol_attempts": protocol_attempts,
                "protocol_convergence_wait_seconds": int(time.time() - protocol_started),
                "duration_seconds": round(time.time() - started, 2),
            }
        )
        summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
        if failures:
            for item in failures:
                print(f"FAIL {item.get('pod')} AS{item.get('asn')} {item.get('failureReason', '')}", file=sys.stderr)
            return EXIT_VERIFY_FAILED
        log(f"verify-after-start-bird passed samples={len(checks)}")
        return 0
    except Exception as exc:
        summary["failure_reason"] = str(exc)
        summary["duration_seconds"] = round(time.time() - started, 2)
        summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
        print(f"verify-after-start-bird failed: {exc}", file=sys.stderr)
        return EXIT_VERIFY_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
