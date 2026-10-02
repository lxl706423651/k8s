#!/usr/bin/env python3
"""Apply vCPU/memory profiles to the 0918E1 libvirt domains.

Inputs: experiment.yaml plus source and optional sink resource sizes.
Outputs: an auditable command trace and refreshed running VMs.
Side effects: shuts down, reconfigures, starts, and SSH-validates only configured experiment domains.
Context: used between placement-benefit experiment profiles before executeVmMatrix.py runs.
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from loadExperiment import loadConfig


SCRIPT_DIR = Path(__file__).resolve().parent


def parseArgs() -> argparse.Namespace:
    """Parse the source/sink resource profile to apply."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=SCRIPT_DIR / "experiment.yaml")
    parser.add_argument("--source-vcpus", type=int, required=True)
    parser.add_argument("--source-memory-gib", type=int, required=True)
    parser.add_argument("--sink-vcpus", type=int)
    parser.add_argument("--sink-memory-gib", type=int)
    parser.add_argument("--shutdown-timeout", type=float, default=180)
    parser.add_argument("--skip-wait", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--force-shutdown",
        action="store_true",
        help="power off a configured VM if graceful shutdown exceeds --shutdown-timeout",
    )
    return parser.parse_args()


def runCommand(command: list[str], *, dryRun: bool = False, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Print and optionally execute one resource-management command."""
    print("+ " + shlex.join(command), flush=True)
    if dryRun:
        return subprocess.CompletedProcess(command, 0, "", "")
    return subprocess.run(command, text=True, capture_output=True, check=check)


def domainState(name: str) -> str:
    """Return the current libvirt domain state string."""
    result = subprocess.run(["virsh", "domstate", name], text=True, capture_output=True, check=True)
    return result.stdout.strip()


def configuredVcpuMaximum(name: str) -> int:
    """Return the persistent maximum vCPU count for one domain."""
    result = subprocess.run(["virsh", "vcpucount", name], text=True, capture_output=True, check=True)
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[0] == "maximum" and fields[1] == "config":
            return int(fields[2])
    raise RuntimeError(f"cannot parse maximum config vCPU count for {name}")


def configuredMaxMemoryKiB(name: str) -> int:
    """Return the persistent max memory in KiB from virsh dominfo."""
    result = subprocess.run(["virsh", "dominfo", name], text=True, capture_output=True, check=True)
    match = re.search(r"^Max memory:\s+(\d+)\s+KiB$", result.stdout, flags=re.MULTILINE)
    if not match:
        raise RuntimeError(f"cannot parse Max memory for {name}")
    return int(match.group(1))


def waitForShutoff(name: str, timeout: float, *, force: bool, dryRun: bool) -> None:
    """Wait for a domain to shut off after virsh shutdown."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if dryRun or domainState(name) == "shut off":
            return
        time.sleep(3)
    if not force:
        raise TimeoutError(f"{name} did not shut off within {timeout:.0f}s")
    runCommand(["virsh", "destroy", name], dryRun=dryRun)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if dryRun or domainState(name) == "shut off":
            return
        time.sleep(2)
    raise TimeoutError(f"{name} did not enter shut off after force shutdown")


def sshCommand(config: dict[str, Any], node: dict[str, Any], remote: str) -> list[str]:
    """Build one non-interactive SSH command for readiness checks."""
    ssh = config["vm"]["ssh"]
    return [
        "ssh",
        "-i",
        str(Path(str(ssh["privateKey"])).expanduser().resolve()),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=10",
        f"{ssh['user']}@{node['address']}",
        remote,
    ]


def waitForReady(config: dict[str, Any], node: dict[str, Any], timeout: float = 900) -> None:
    """Wait until SSH, cloud-init, and Docker are ready after a VM start."""
    deadline = time.monotonic() + timeout
    last = ""
    command = sshCommand(
        config,
        node,
        "cloud-init status --wait >/dev/null && sudo docker info >/dev/null && echo READY",
    )
    while time.monotonic() < deadline:
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        if result.returncode == 0 and "READY" in result.stdout:
            print(f"ready: {node['name']}", flush=True)
            return
        last = (result.stderr or result.stdout).strip()[-400:]
        time.sleep(10)
    raise TimeoutError(f"{node['name']} did not become ready: {last}")


def applyResources(
    config: dict[str, Any],
    node: dict[str, Any],
    vcpus: int,
    memoryGiB: int,
    args: argparse.Namespace,
) -> None:
    """Reconfigure one exact domain and start it again."""
    name = str(node["name"])
    memoryKiB = int(memoryGiB * 1024 * 1024)
    state = domainState(name)
    if state != "shut off":
        runCommand(["virsh", "shutdown", name], dryRun=args.dry_run)
        waitForShutoff(
            name,
            args.shutdown_timeout,
            force=args.force_shutdown,
            dryRun=args.dry_run,
        )

    currentMaxVcpus = configuredVcpuMaximum(name) if not args.dry_run else vcpus
    if vcpus > currentMaxVcpus:
        runCommand(["virsh", "setvcpus", name, str(vcpus), "--maximum", "--config"], dryRun=args.dry_run)
        runCommand(["virsh", "setvcpus", name, str(vcpus), "--config"], dryRun=args.dry_run)
    else:
        runCommand(["virsh", "setvcpus", name, str(vcpus), "--config"], dryRun=args.dry_run)
        runCommand(["virsh", "setvcpus", name, str(vcpus), "--maximum", "--config"], dryRun=args.dry_run)

    currentMaxMemory = configuredMaxMemoryKiB(name) if not args.dry_run else memoryKiB
    if memoryKiB > currentMaxMemory:
        runCommand(["virsh", "setmaxmem", name, str(memoryKiB), "--config"], dryRun=args.dry_run)
        runCommand(["virsh", "setmem", name, str(memoryKiB), "--config"], dryRun=args.dry_run)
    else:
        runCommand(["virsh", "setmem", name, str(memoryKiB), "--config"], dryRun=args.dry_run)
        runCommand(["virsh", "setmaxmem", name, str(memoryKiB), "--config"], dryRun=args.dry_run)

    runCommand(["virsh", "start", name], dryRun=args.dry_run)
    if not args.skip_wait and not args.dry_run:
        waitForReady(config, node)


def main() -> int:
    """Apply source and sink resource profiles to configured domains."""
    args = parseArgs()
    config = loadConfig(args.config)
    sinkVcpus = args.sink_vcpus or args.source_vcpus
    sinkMemoryGiB = args.sink_memory_gib or args.source_memory_gib
    for node in config["vm"]["nodes"]:
        applyResources(config, node, args.source_vcpus, args.source_memory_gib, args)
    applyResources(config, config["vm"]["sink"], sinkVcpus, sinkMemoryGiB, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
