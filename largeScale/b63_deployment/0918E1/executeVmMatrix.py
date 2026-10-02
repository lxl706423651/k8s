#!/usr/bin/env python3
"""Compile and execute the configured AS matrix concurrently across its VMs.

Inputs: experiment.yaml, reachable provisioned VMs, and the local SeedEMU source tree.
Outputs: builds/as-NNNN plus one timestamped results/run-* directory containing every VM result/log.
Side effects: copies files over SSH and builds/starts/stops Docker emulations on the declared VMs.
Context: run on the KVM host after provisionVmMatrix.py; no libvirt definitions are changed here.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import ipaddress
import json
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from loadExperiment import loadConfig, resolvePath


SCRIPT_DIR = Path(__file__).resolve().parent


def parseArgs() -> argparse.Namespace:
    """Parse compile/run stage selection and safe preview options."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=SCRIPT_DIR / "experiment.yaml")
    stage = parser.add_mutually_exclusive_group()
    stage.add_argument("--compile-only", action="store_true")
    stage.add_argument("--run-only", action="store_true")
    parser.add_argument("--compile-workers", type=int, default=2)
    parser.add_argument("--skip-build", action="store_true", help="reuse images already present in each VM")
    parser.add_argument("--keep-running", action="store_true", help="do not compose down after collection")
    parser.add_argument("--run-id", help="result batch name; defaults to run-YYYYmmdd-HHMMSS")
    parser.add_argument(
        "--only-as",
        type=int,
        action="append",
        help="limit compile/run to one or more configured AS counts (useful for smoke tests)",
    )
    parser.add_argument("--dry-run", action="store_true", help="print compile/SSH actions without executing them")
    return parser.parse_args()


def requireCommands(names: list[str]) -> None:
    """Fail before execution when required host tools are unavailable."""
    missing = [name for name in names if shutil.which(name) is None]
    if missing:
        raise RuntimeError("missing required commands: " + ", ".join(missing))


def compileCase(config: dict[str, Any], asCount: int, dryRun: bool) -> dict[str, Any]:
    """Compile one topology scale into its isolated build directory."""
    outputDir = resolvePath(config, Path(config["execution"]["buildRoot"]) / f"as-{asCount:04d}")
    command = [
        sys.executable,
        str(SCRIPT_DIR / "createStarTopology.py"),
        str(asCount),
        "--output-dir",
        str(outputDir),
    ]
    if not bool(config["topology"]["routeServerPeering"]):
        command.append("--without-rs-peering")
    if str(config["traffic"].get("mode")) == "vm-to-vm-sink":
        command.append("--external-ix")
    print("+ " + shlex.join(command), flush=True)
    if dryRun:
        return {"asCount": asCount, "status": "DRY_RUN", "outputDir": str(outputDir)}
    started = time.monotonic()
    result = subprocess.run(command, cwd=SCRIPT_DIR, text=True, capture_output=True, check=False)
    record = {
        "asCount": asCount,
        "status": "PASS" if result.returncode == 0 else "FAIL",
        "returnCode": result.returncode,
        "durationSeconds": time.monotonic() - started,
        "outputDir": str(outputDir),
        "stdout": result.stdout[-4000:],
        "stderr": result.stderr[-4000:],
    }
    if result.returncode != 0:
        raise RuntimeError(json.dumps(record, indent=2))
    return record


def sshParts(config: dict[str, Any], node: dict[str, Any]) -> tuple[list[str], str]:
    """Return a stable SSH prefix and user@address target for one VM."""
    ssh = config["vm"]["ssh"]
    keyPath = Path(str(ssh["privateKey"])).expanduser().resolve()
    target = f"{ssh['user']}@{node['address']}"
    options = [
        "-i",
        str(keyPath),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ConnectTimeout={int(config['execution']['sshConnectTimeoutSeconds'])}",
    ]
    return options, target


def runRemote(
    sshOptions: list[str], target: str, tokens: list[str], *, log: Any = None, quiet: bool = False
) -> int:
    """Run an argv-shaped remote command through one explicitly quoted shell string."""
    remote = shlex.join(tokens)
    command = ["ssh", *sshOptions, target, remote]
    print("+ " + shlex.join(command), flush=True)
    stdout = subprocess.DEVNULL if quiet else log
    stderr = subprocess.DEVNULL if quiet else (subprocess.STDOUT if log else None)
    return subprocess.run(command, stdout=stdout, stderr=stderr, check=False).returncode


def rsyncToRemote(
    sshOptions: list[str], target: str, source: Path, destination: str, *, dryRun: bool
) -> None:
    """Copy one local tree without deleting unrelated or previous remote files."""
    sshTransport = shlex.join(["ssh", *sshOptions])
    command = ["rsync", "-az", "-e", sshTransport, f"{source}/", f"{target}:{destination}/"]
    print("+ " + shlex.join(command), flush=True)
    if not dryRun and subprocess.run(command, check=False).returncode != 0:
        raise RuntimeError(f"rsync upload failed: {source} -> {target}:{destination}")


def rsyncFromRemote(
    sshOptions: list[str],
    target: str,
    source: str,
    destination: Path,
    *,
    attempts: int = 1,
    retrySeconds: float = 10,
) -> None:
    """Copy one VM result tree, tolerating transient SSH starvation under load."""
    destination.mkdir(parents=True, exist_ok=True)
    sshTransport = shlex.join(["ssh", *sshOptions])
    command = ["rsync", "-az", "-e", sshTransport, f"{target}:{source}/", f"{destination}/"]
    for attempt in range(1, attempts + 1):
        print("+ " + shlex.join(command) + f"  # attempt {attempt}/{attempts}", flush=True)
        if subprocess.run(command, check=False).returncode == 0:
            return
        if attempt < attempts:
            time.sleep(retrySeconds)
    raise RuntimeError(f"rsync result download failed after {attempts} attempts: {target}:{source}")


def remoteFileExists(sshOptions: list[str], target: str, path: str) -> bool:
    """Check a remote sentinel while suppressing expected transient SSH errors."""
    command = ["ssh", *sshOptions, target, shlex.join(["test", "-f", path])]
    return subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode == 0


def readRemoteFile(sshOptions: list[str], target: str, path: str) -> str:
    """Read one small remote control file after its existence has been observed."""
    command = ["ssh", *sshOptions, target, shlex.join(["cat", path])]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"cannot read remote status file: {target}:{path}")
    return result.stdout.strip()


def rsyncFileToRemote(
    sshOptions: list[str], target: str, source: Path, destination: str, *, dryRun: bool
) -> None:
    """Copy one helper file without recursively uploading local builds/state/results."""
    sshTransport = shlex.join(["ssh", *sshOptions])
    command = ["rsync", "-az", "-e", sshTransport, str(source), f"{target}:{destination}"]
    print("+ " + shlex.join(command), flush=True)
    if not dryRun and subprocess.run(command, check=False).returncode != 0:
        raise RuntimeError(f"rsync file upload failed: {source} -> {target}:{destination}")


def imageArchiveName(reference: str) -> str:
    """Return a filesystem-safe deterministic tar name for one image reference."""
    return reference.replace("/", "_").replace(":", "_").replace("@", "_") + ".tar"


def prepareImageArchives(config: dict[str, Any]) -> dict[str, Path]:
    """Export configured base images once from the host Docker cache."""
    archiveDir = resolvePath(config, "state/images")
    archiveDir.mkdir(parents=True, exist_ok=True)
    archives: dict[str, Path] = {}
    for reference in config["execution"].get("baseImages", []):
        inspect = subprocess.run(
            ["docker", "image", "inspect", str(reference)], text=True, capture_output=True, check=False
        )
        if inspect.returncode != 0:
            raise RuntimeError(f"required base image is absent from host Docker: {reference}")
        archive = archiveDir / imageArchiveName(str(reference))
        if not archive.is_file() or archive.stat().st_size == 0:
            command = ["docker", "save", "--output", str(archive), str(reference)]
            print("+ " + shlex.join(command), flush=True)
            subprocess.run(command, check=True)
        archives[str(reference)] = archive
    return archives


def preloadNodeImages(
    config: dict[str, Any], node: dict[str, Any], archives: dict[str, Path]
) -> dict[str, Any]:
    """Install Compose v2 and load only missing base images into one VM."""
    sshOptions, target = sshParts(config, node)
    remoteRoot = str(config["execution"]["remoteRoot"]).rstrip("/")
    remoteImageDir = f"{remoteRoot}/images"
    remotePluginDir = f"/home/{config['vm']['ssh']['user']}/.docker/cli-plugins"
    remotePlugin = f"{remotePluginDir}/docker-compose"
    if runRemote(sshOptions, target, ["mkdir", "-p", remoteImageDir, remotePluginDir]) != 0:
        raise RuntimeError(f"cannot prepare image directory on {target}")
    composeState = "reused"
    if runRemote(sshOptions, target, ["docker", "compose", "version"], quiet=True) != 0:
        hostPlugin = Path(str(config["execution"]["composePluginPath"])).expanduser().resolve()
        if not hostPlugin.is_file():
            raise FileNotFoundError(f"host Compose plugin is missing: {hostPlugin}")
        rsyncFileToRemote(sshOptions, target, hostPlugin, remotePlugin, dryRun=False)
        if runRemote(sshOptions, target, ["chmod", "0755", remotePlugin]) != 0:
            raise RuntimeError(f"cannot mark Compose plugin executable on {target}")
        composeState = "installed"
    if runRemote(sshOptions, target, ["docker", "compose", "version"], quiet=True) != 0:
        raise RuntimeError(f"Docker Compose v2 is unavailable on {target}")
    loaded: list[str] = []
    reused: list[str] = []
    for reference, archive in archives.items():
        inspect = runRemote(sshOptions, target, ["docker", "image", "inspect", reference], quiet=True)
        if inspect == 0:
            reused.append(reference)
            continue
        remoteArchive = f"{remoteImageDir}/{archive.name}"
        rsyncFileToRemote(sshOptions, target, archive, remoteArchive, dryRun=False)
        if runRemote(sshOptions, target, ["docker", "load", "--input", remoteArchive]) != 0:
            raise RuntimeError(f"docker load failed on {target}: {reference}")
        if runRemote(sshOptions, target, ["docker", "image", "inspect", reference], quiet=True) != 0:
            raise RuntimeError(f"image missing after docker load on {target}: {reference}")
        loaded.append(reference)
    return {
        "vm": node["name"],
        "address": node["address"],
        "composeV2": composeState,
        "loaded": loaded,
        "reused": reused,
    }


def sinkPorts(config: dict[str, Any], nodes: list[dict[str, Any]]) -> list[int]:
    """Return the unique iperf server ports required by the selected source VMs."""
    base = int(config["traffic"]["sinkPortBase"])
    return [
        base + int(node["sourceIndex"]) * 512 + index
        for node in nodes
        for index in range(int(node["asCount"]))
    ]


def prepareSinkVm(config: dict[str, Any], nodes: list[dict[str, Any]], runId: str) -> dict[str, Any]:
    """Configure the sink VM external address and start one iperf3 server per remote pair."""
    sink = config["vm"]["sink"]
    external = config["vm"]["externalNetwork"]
    sshOptions, target = sshParts(config, sink)
    ports = sinkPorts(config, nodes)
    portArguments = " ".join(str(port) for port in ports)
    sinkAddress = str(sink["xvmAddress"])
    prefix = str(ipaddress.ip_network(str(external["cidr"]), strict=True).prefixlen)
    remoteRoot = str(config["execution"]["remoteRoot"]).rstrip("/")
    logDir = f"{remoteRoot}/sink/{runId}"
    script = f"""
set -eu
external_iface=$(ip -o link | awk '$0 ~ /{str(sink['xvmMac']).lower()}/ {{print $2}}' | tr -d ':')
test -n "$external_iface"
sudo ip link set dev "$external_iface" up
sudo ip addr replace {shlex.quote(sinkAddress)}/{shlex.quote(prefix)} dev "$external_iface"
sudo pkill -x iperf3 || true
mkdir -p {shlex.quote(logDir)}
for port in {portArguments}; do
  iperf3 -s -D -p "$port" --logfile {shlex.quote(logDir)}/iperf-$port.log
done
printf '%s\\n' {portArguments} | sort -u > {shlex.quote(logDir)}/expected-ports.txt
missing=""
for attempt in $(seq 1 30); do
  missing=$(comm -23 {shlex.quote(logDir)}/expected-ports.txt <(ss -ltnH | awk '{{address=$4; sub(/^.*:/, "", address); print address}}' | sort -u) || true)
  if [ -z "$missing" ]; then
    break
  fi
  sleep 1
done
if [ -n "$missing" ]; then
  echo "missing sink ports:" >&2
  echo "$missing" >&2
  exit 1
fi
"""
    if runRemote(sshOptions, target, ["bash", "-lc", script]) != 0:
        raise RuntimeError(f"sink VM preparation failed: {target}")
    return {
        "vm": sink["name"],
        "address": sink["address"],
        "externalAddress": sinkAddress,
        "serverCount": len(ports),
        "portBase": int(config["traffic"]["sinkPortBase"]),
        "runId": runId,
    }


def stopSinkVm(config: dict[str, Any]) -> None:
    """Stop only the controlled iperf servers and remove the temporary sink address."""
    sink = config["vm"]["sink"]
    external = config["vm"]["externalNetwork"]
    sshOptions, target = sshParts(config, sink)
    script = (
        f"sudo pkill -x iperf3 || true; "
        f"external_iface=$(ip -o link | awk '$0 ~ /{str(sink['xvmMac']).lower()}/ {{print $2}}' | tr -d ':'); "
        f"test -n \"$external_iface\" && sudo ip addr del {shlex.quote(str(sink['xvmAddress']))}/"
        f"{shlex.quote(str(ipaddress.ip_network(str(external['cidr']), strict=True).prefixlen))} "
        f"dev \"$external_iface\" 2>/dev/null || true"
    )
    runRemote(sshOptions, target, ["bash", "-lc", script], quiet=True)


def executeNode(
    config: dict[str, Any], node: dict[str, Any], runId: str, batchDir: Path, args: argparse.Namespace
) -> dict[str, Any]:
    """Upload, execute, and retrieve one scale on its assigned VM."""
    asCount = int(node["asCount"])
    outputDir = resolvePath(config, Path(config["execution"]["buildRoot"]) / f"as-{asCount:04d}")
    if not (outputDir / "topology.json").is_file():
        raise FileNotFoundError(f"compiled case missing: {outputDir}")
    remoteRoot = str(config["execution"]["remoteRoot"]).rstrip("/")
    remoteCase = f"{remoteRoot}/cases/as-{asCount:04d}"
    remoteResult = f"{remoteRoot}/results/{runId}/as-{asCount:04d}"
    remoteStatus = f"{remoteRoot}/results/{runId}/.as-{asCount:04d}.exitcode"
    remoteLauncherLog = f"{remoteRoot}/results/{runId}/.as-{asCount:04d}.launcher.log"
    remoteRunner = f"{remoteRoot}/runGuestExperiment.py"
    sshOptions, target = sshParts(config, node)
    record: dict[str, Any] = {
        "vm": node["name"],
        "address": node["address"],
        "asCount": asCount,
        "status": "RUNNING",
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }

    mkdirTokens = ["mkdir", "-p", remoteCase, str(Path(remoteResult).parent), remoteRoot]
    if args.dry_run:
        print(f"would SSH {target}: {shlex.join(mkdirTokens)}")
        rsyncToRemote(sshOptions, target, outputDir, remoteCase, dryRun=True)
        return {**record, "status": "DRY_RUN"}
    if runRemote(sshOptions, target, mkdirTokens) != 0:
        raise RuntimeError(f"cannot prepare remote paths on {target}")
    rsyncToRemote(sshOptions, target, outputDir, remoteCase, dryRun=False)
    rsyncFileToRemote(
        sshOptions,
        target,
        SCRIPT_DIR / "runGuestExperiment.py",
        remoteRunner,
        dryRun=False,
    )

    traffic = config["traffic"]
    command = [
        "python3",
        remoteRunner,
        "--case-dir",
        remoteCase,
        "--result-dir",
        remoteResult,
        "--as-count",
        str(asCount),
        "--duration",
        str(traffic["durationSeconds"]),
        "--preflight-duration",
        str(traffic["preflightDurationSeconds"]),
        "--preflight-retries",
        str(traffic.get("preflightRetries", 2)),
        "--command-grace-seconds",
        str(traffic["commandGraceSeconds"]),
        "--repeats",
        str(traffic["repeats"]),
        "--baseline-seconds",
        str(traffic["baselineSeconds"]),
        "--cooldown-seconds",
        str(traffic["cooldownSeconds"]),
        "--ping-interval",
        str(traffic["pingIntervalSeconds"]),
        "--sample-interval",
        str(traffic["sampleIntervalSeconds"]),
        "--startup-wait",
        str(traffic["startupWaitSeconds"]),
        "--project-name",
        f"e1as{asCount:03d}",
        "--build-parallelism",
        str(config["execution"]["buildParallelism"]),
    ]
    if str(traffic.get("mode")) == "vm-to-vm-sink":
        command.extend(
            [
                "--traffic-mode",
                "vm-to-vm-sink",
                "--sink-address",
                str(traffic["sinkAddress"]),
                "--sink-port-base",
                str(traffic["sinkPortBase"]),
                "--source-index",
                str(node["sourceIndex"]),
                "--external-ix-interface",
                str(config["vm"]["externalNetwork"]["parentInterface"]),
                "--external-ix-mac",
                str(node["xvmMac"]),
                "--external-network-name",
                str(config["vm"]["externalNetwork"]["dockerName"]),
                "--external-network-cidr",
                str(config["vm"]["externalNetwork"]["cidr"]),
                "--external-network-gateway",
                str(config["vm"]["externalNetwork"]["gateway"]),
                "--external-network-ip-range-prefix",
                str(config["vm"]["externalNetwork"]["macvlanIpRangePrefix"]),
            ]
        )
    if args.skip_build:
        command.append("--skip-build")
    if args.keep_running:
        command.append("--keep-running")
    logPath = batchDir / f"vm-as-{asCount:04d}.log"
    started = time.monotonic()
    with logPath.open("w", encoding="utf-8") as log:
        commandText = shlex.join(command)
        inner = (
            f"{commandText}; rc=$?; "
            f"printf '%s\\n' \"$rc\" > {shlex.quote(remoteStatus)}"
        )
        outer = (
            f"rm -f {shlex.quote(remoteStatus)} {shlex.quote(remoteLauncherLog)}; "
            f"nohup bash -lc {shlex.quote(inner)} > {shlex.quote(remoteLauncherLog)} 2>&1 < /dev/null &"
        )
        launchCode = runRemote(sshOptions, target, ["bash", "-lc", outer], log=log)
        if launchCode != 0:
            returnCode = launchCode
        else:
            deadline = time.monotonic() + float(config["execution"]["remoteRunTimeoutSeconds"])
            pollSeconds = float(config["execution"]["statusPollSeconds"])
            while time.monotonic() < deadline and not remoteFileExists(sshOptions, target, remoteStatus):
                time.sleep(pollSeconds)
            if not remoteFileExists(sshOptions, target, remoteStatus):
                returnCode = 124
                log.write(f"remote status timeout after {config['execution']['remoteRunTimeoutSeconds']} seconds\n")
            else:
                try:
                    returnCode = int(readRemoteFile(sshOptions, target, remoteStatus))
                except (RuntimeError, ValueError) as exc:
                    returnCode = 125
                    log.write(f"remote status read failed: {exc}\n")
    record["durationSeconds"] = time.monotonic() - started
    record["returnCode"] = returnCode
    record["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    try:
        rsyncFromRemote(
            sshOptions,
            target,
            remoteResult,
            batchDir / f"as-{asCount:04d}",
            attempts=int(config["execution"]["resultDownloadAttempts"]),
            retrySeconds=float(config["execution"]["statusPollSeconds"]),
        )
    except Exception as exc:
        record["downloadError"] = f"{type(exc).__name__}: {exc}"
    record["status"] = "PASS" if returnCode == 0 else "FAIL"
    return record


def writeMatrix(path: Path, data: dict[str, Any]) -> None:
    """Atomically refresh the host-side batch status after each completed VM."""
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> int:
    """Compile selected scales and optionally run all assigned VMs in parallel."""
    args = parseArgs()
    config = loadConfig(args.config)
    requireCommands(["ssh", "rsync", "docker"])
    configuredScales = list(config["topology"]["asCounts"])
    selectedScales = set(args.only_as or configuredScales)
    unknownScales = selectedScales - set(configuredScales)
    if unknownScales:
        raise ValueError(f"--only-as contains scales absent from configuration: {sorted(unknownScales)}")
    scales = [scale for scale in configuredScales if scale in selectedScales]
    compileRecords: list[dict[str, Any]] = []
    if not args.run_only:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.compile_workers)) as executor:
            futures = {executor.submit(compileCase, config, scale, args.dry_run): scale for scale in scales}
            for future in concurrent.futures.as_completed(futures):
                compileRecords.append(future.result())
                print(f"compiled AS={futures[future]}", flush=True)
    if args.compile_only or args.dry_run:
        print(json.dumps({"compile": sorted(compileRecords, key=lambda item: item["asCount"])}, indent=2))
        return 0

    runId = args.run_id or time.strftime("run-%Y%m%d-%H%M%S")
    resultsRoot = resolvePath(config, config["execution"]["resultsRoot"])
    batchDir = resultsRoot / runId
    batchDir.mkdir(parents=True, exist_ok=False)
    shutil.copy2(Path(config["_configPath"]), batchDir / "experiment.yaml")
    matrix: dict[str, Any] = {
        "schemaVersion": 1,
        "runId": runId,
        "status": "RUNNING",
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "compile": sorted(compileRecords, key=lambda item: item["asCount"]),
        "runs": [],
    }
    matrixPath = batchDir / "matrix.json"
    writeMatrix(matrixPath, matrix)

    failures = 0
    nodes = [node for node in config["vm"]["nodes"] if int(node["asCount"]) in selectedScales]
    archives = prepareImageArchives(config)
    matrix["imagePreload"] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(nodes))) as executor:
        futures = {executor.submit(preloadNodeImages, config, node, archives): node for node in nodes}
        for future in concurrent.futures.as_completed(futures):
            node = futures[future]
            try:
                record = future.result()
                record["status"] = "PASS"
            except Exception as exc:
                record = {
                    "vm": node["name"],
                    "address": node["address"],
                    "status": "FAIL",
                    "error": f"{type(exc).__name__}: {exc}",
                }
                failures += 1
            matrix["imagePreload"].append(record)
            matrix["imagePreload"].sort(key=lambda item: str(item["vm"]))
            writeMatrix(matrixPath, matrix)
    if failures:
        matrix["status"] = "FAIL"
        matrix["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        writeMatrix(matrixPath, matrix)
        print(f"image preload failed on {failures} VMs", file=sys.stderr)
        return 1

    sinkPrepared = False
    try:
        if str(config["traffic"].get("mode")) == "vm-to-vm-sink":
            # Mark the sink as requiring cleanup before starting servers so a
            # partially completed preparation cannot leave stale iperf daemons
            # or the temporary address behind.
            sinkPrepared = True
            matrix["sink"] = prepareSinkVm(config, nodes, runId)
            matrix["sink"]["status"] = "PASS"
            writeMatrix(matrixPath, matrix)

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(nodes)) as executor:
            futures = {executor.submit(executeNode, config, node, runId, batchDir, args): node for node in nodes}
            for future in concurrent.futures.as_completed(futures):
                node = futures[future]
                try:
                    record = future.result()
                except Exception as exc:
                    record = {
                        "vm": node["name"],
                        "address": node["address"],
                        "asCount": node["asCount"],
                        "status": "FAIL",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                matrix["runs"].append(record)
                matrix["runs"].sort(key=lambda item: int(item["asCount"]))
                if record["status"] != "PASS":
                    failures += 1
                writeMatrix(matrixPath, matrix)
                print(f"{record['status']}: AS={record['asCount']} VM={record['vm']}", flush=True)
    finally:
        if sinkPrepared:
            stopSinkVm(config)
            if "sink" in matrix:
                matrix["sink"]["stoppedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            writeMatrix(matrixPath, matrix)

    matrix["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    matrix["status"] = "PASS" if failures == 0 else "FAIL"
    writeMatrix(matrixPath, matrix)
    if failures:
        print(f"matrix completed with {failures} failed VM runs", file=sys.stderr)
        return 1

    analysisCommand = [sys.executable, str(SCRIPT_DIR / "analyzeVmMatrix.py"), "--run-dir", str(batchDir)]
    print("+ " + shlex.join(analysisCommand))
    return subprocess.run(analysisCommand, cwd=SCRIPT_DIR, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
