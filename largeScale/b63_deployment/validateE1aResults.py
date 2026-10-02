#!/usr/bin/env python3
"""Validate E1-A point completeness, accounting, sampling, and cleanup."""
from __future__ import annotations

import argparse
import json
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def libvirtNames(kind: str) -> list[str]:
    """Return all libvirt domain or network names."""
    command = ["virsh", "list", "--all", "--name"] if kind == "domain" \
        else ["virsh", "net-list", "--all", "--name"]
    return subprocess.run(command, check=True, text=True, capture_output=True).stdout.split()


def validate(runDir: Path, checkCleanup: bool) -> dict[str, Any]:
    """Validate a completed formal run and return an auditable result."""
    plan = json.loads((runDir / "plans" / "formal_plan.json").read_text(encoding="utf-8"))
    points = [json.loads(path.read_text(encoding="utf-8"))
              for path in sorted((runDir / "raw").glob("**/point_summary.json"))]
    expected = {(int(item["blockId"]), item["group"], int(item["N"]))
                for item in plan["points"]}
    actual = {(int(item["blockId"]), item["group"], int(item["N"])) for item in points}
    pointIds = [item["pointId"] for item in points]
    expectedRepetitions = int(plan["phaseConfig"]["repetitions"])
    expectedSamples = int(plan["phaseConfig"]["measurementSeconds"]) - 1
    cells = Counter((item["group"], int(item["N"])) for item in points)
    statuses = Counter(item["status"] for item in points)
    workers = Counter(item["workerName"] for item in points)
    checks = {
        "summaryCountMatchesPlan": len(points) == len(plan["points"]),
        "uniquePointIds": len(set(pointIds)) == len(pointIds),
        "planMatchesSummaries": expected == actual,
        "expectedRepetitionsPerCell": bool(cells) and set(cells.values()) == {expectedRepetitions},
        "validStatusesOnly": set(statuses) <= {"success", "workload_overload"},
        "canaryCountConservation": all(item["canary"]["count_conservation_ok"] for item in points),
        "backgroundCountConservation": all(item["background"]["countConservationOk"] for item in points),
        "reportedBackgroundPodsMatchN": all(
            item["background"]["reportedPods"] == item["N"] for item in points),
        "pointPodCountIncludesCanary": all(item["pointPodCount"] == item["N"] + 1 for item in points),
        "fullNodeSampling": all(item["nodeMetrics"]["sampleCount"] >= expectedSamples for item in points),
        "noCgroupCpuThrottling": all(
            item["nodeMetrics"]["cgroupThrottledUsecDelta"] == 0 for item in points),
    }
    cleanup: dict[str, Any] = {"checked": checkCleanup}
    if checkCleanup:
        manifest = json.loads((runDir / "cleanup_manifest.json").read_text(encoding="utf-8"))
        domains = libvirtNames("domain")
        networks = libvirtNames("network")
        cleanup.update({
            "destroyReturncode": manifest.get("destroy", {}).get("returncode"),
            "ownedVmsAbsent": not any(name in domains for name in manifest["ownedVmNames"]),
            "ownedNetworkAbsent": manifest["ownedLibvirtNetwork"] not in networks,
            "ownedStorageAbsent": not Path(manifest["ownedStorageDir"]).exists(),
            "preservedDomains": sorted(domains),
            "preservedNetworks": sorted(networks),
        })
        checks["cleanupComplete"] = (
            cleanup["destroyReturncode"] == 0 and cleanup["ownedVmsAbsent"]
            and cleanup["ownedNetworkAbsent"] and cleanup["ownedStorageAbsent"]
        )
    n60 = sorted((item for item in points if item["group"] == "loaded" and item["N"] == 60),
                 key=lambda item: item["blockId"])
    result = {
        "validatedAt": datetime.now(timezone.utc).isoformat(),
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "pointCount": len(points),
        "cellCount": len(cells),
        "statusCounts": dict(sorted(statuses.items())),
        "workerPointTotals": dict(sorted(workers.items())),
        "allIdleSucceeded": all(item["status"] == "success" for item in points
                                if item["group"] == "idle"),
        "nodeMetrics": {
            "maxCpuStealMean": max(item["nodeMetrics"]["cpuStealMean"] for item in points),
            "maxCgroupThrottledUsecDelta": max(
                item["nodeMetrics"]["cgroupThrottledUsecDelta"] for item in points),
            "minMemoryAvailableKiB": min(
                item["nodeMetrics"]["memoryAvailableMinKiB"] for item in points),
            "minSampleCount": min(item["nodeMetrics"]["sampleCount"] for item in points),
        },
        "loadedN60": [{
            "blockId": item["blockId"],
            "workerName": item["workerName"],
            "status": item["status"],
            "canaryP99Ms": item["canary"]["latency_p99_ms"],
            "canaryOnTimeRate": item["canaryOnTimeRate"],
            "cpuBusyMean": item["nodeMetrics"]["cpuBusyMean"],
            "cpuStealMean": item["nodeMetrics"]["cpuStealMean"],
        } for item in n60],
        "cleanup": cleanup,
    }
    output = runDir / "metrics" / "validation.json"
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def parseArguments() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--check-cleanup", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parseArguments()
    validation = validate(arguments.run_dir.resolve(), arguments.check_cleanup)
    print(json.dumps(validation, indent=2))
    raise SystemExit(0 if validation["status"] == "PASS" else 1)
