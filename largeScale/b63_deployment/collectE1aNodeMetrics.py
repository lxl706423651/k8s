#!/usr/bin/env python3
"""Collect low-overhead Linux node metrics for one bounded E1-A point.

Inputs are an absolute realtime epoch, duration, and sample interval. Output is
JSON Lines on stdout. The script is copied to and executed on the dedicated
worker VM; it only reads procfs/cgroupfs and creates no persistent state.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any


def readKeyValues(path: Path) -> dict[str, int]:
    """Read whitespace-separated key/value counters from path."""
    values: dict[str, int] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[1].lstrip("-").isdigit():
                values[fields[0]] = int(fields[1])
    except OSError:
        pass
    return values


def readCpuStat() -> dict[str, int]:
    """Read aggregate CPU and selected scheduler counters from /proc/stat."""
    output: dict[str, int] = {}
    for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if not fields:
            continue
        if fields[0] == "cpu":
            names = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal", "guest", "guest_nice")
            output.update({f"cpu_{name}": int(value) for name, value in zip(names, fields[1:])})
        elif fields[0] in {"ctxt", "processes", "procs_running", "procs_blocked", "softirq"}:
            output[fields[0]] = int(fields[1])
    return output


def readPressure(resource: str) -> dict[str, float | int]:
    """Read CPU or memory PSI averages and total stall time."""
    output: dict[str, float | int] = {}
    path = Path("/proc/pressure") / resource
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            fields = line.split()
            prefix = fields[0]
            for token in fields[1:]:
                name, value = token.split("=", 1)
                output[f"psi_{resource}_{prefix}_{name}"] = int(value) if name == "total" else float(value)
    except OSError:
        pass
    return output


def readMemory() -> dict[str, int]:
    """Read selected memory counters in KiB."""
    wanted = {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}
    output: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        name, value = line.split(":", 1)
        if name in wanted:
            output[f"memory_{name.lower()}_kib"] = int(value.split()[0])
    return output


def readSample() -> dict[str, Any]:
    """Return one timestamped node snapshot."""
    loadFields = Path("/proc/loadavg").read_text(encoding="utf-8").split()
    sample: dict[str, Any] = {
        "timestamp_unix_ns": time.time_ns(),
        "monotonic_ns": time.monotonic_ns(),
        "load1": float(loadFields[0]),
        "load5": float(loadFields[1]),
        "load15": float(loadFields[2]),
        "run_queue": int(loadFields[3].split("/", 1)[0]),
        "process_count": int(loadFields[3].split("/", 1)[1]),
    }
    sample.update(readCpuStat())
    sample.update(readPressure("cpu"))
    sample.update(readPressure("memory"))
    sample.update(readMemory())
    for candidate in (Path("/sys/fs/cgroup/cpu.stat"), Path("/sys/fs/cgroup/cpu,cpuacct/cpu.stat")):
        if candidate.exists():
            sample.update({f"cgroup_{key}": value for key, value in readKeyValues(candidate).items()})
            break
    return sample


def collectMetrics(epochUnixNs: int, durationSeconds: float, intervalSeconds: float) -> None:
    """Wait for epoch and emit samples until the bounded duration ends."""
    delay = (epochUnixNs - time.time_ns()) / 1e9
    if delay > 0:
        time.sleep(delay)
    deadline = time.monotonic() + durationSeconds
    nextSample = time.monotonic()
    while time.monotonic() <= deadline:
        print(json.dumps(readSample(), sort_keys=True), flush=True)
        nextSample += intervalSeconds
        time.sleep(max(0.0, nextSample - time.monotonic()))


def parseArguments() -> argparse.Namespace:
    """Parse the bounded collector command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--epoch-unix-ns", type=int, required=True)
    parser.add_argument("--duration-seconds", type=float, required=True)
    parser.add_argument("--interval-seconds", type=float, default=1.0)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parseArguments()
    collectMetrics(arguments.epoch_unix_ns, arguments.duration_seconds, arguments.interval_seconds)
