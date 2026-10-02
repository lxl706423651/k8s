#!/usr/bin/env python3
"""Summarize corrected E1-B pilot/formal point artifacts.

The analyzer never changes placement or drops failed points. It emits one row
per measured mapping and marks incomplete/harness-invalid rows explicitly.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from typing import Any


def readJson(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def pointRows(runDir: Path) -> list[dict[str, Any]]:
    root = runDir / "e1b/pilot"
    rows: list[dict[str, Any]] = []
    for pointDir in sorted(root.glob("*/")):
        summaryPath = pointDir / "point_summary.json"
        if not summaryPath.is_file():
            continue
        point = readJson(summaryPath)
        placement = point.get("placement") or {}
        udp = point.get("udp") or {}
        tcp = point.get("tcp") or {}
        workload = point.get("workload") or {}
        mappingId = str(point.get("mappingId") or pointDir.name)
        mapping = {}
        sequencePath = runDir / "e1b/plans/placement_sequence.json"
        if sequencePath.is_file():
            for item in readJson(sequencePath).get("mappings") or []:
                if item.get("mappingId") == mappingId:
                    mapping = item
                    break
        canaries = workload.get("canaries") or []
        p99 = [float(item["latency_p99_ms"]) for item in canaries
               if item.get("latency_p99_ms") is not None]
        nodeMetrics = workload.get("perNodeMetrics") or {}
        rows.append({
            "mappingId": mappingId,
            "targetWorkerPodCount": mapping.get("targetWorkerPodCount"),
            "fixedFlowCrossings": mapping.get("fixedFlowCrossings"),
            "xExpectedPayloadMbpsCrossings": mapping.get("xExpectedPayloadMbpsCrossings"),
            "placementStatus": placement.get("status"),
            "podCount": placement.get("podCount"),
            "preflightPassed": (point.get("preflight") or {}).get("passed"),
            "udpStatus": udp.get("status"),
            "udpOfferedMbps": udp.get("offeredMbps"),
            "udpActualSendMbps": udp.get("actualSendMbps"),
            "udpReceivedMbps": udp.get("receivedMbps"),
            "udpDeliveryRatio": udp.get("deliveryRatio"),
            "udpLossPctMean": udp.get("lossPctMean"),
            "tcpStatus": tcp.get("status"),
            "tcpGoodputMbps": ((tcp.get("aggregateGoodputMbps") or {}).get("mean")),
            "workloadStatus": workload.get("status"),
            "backgroundOnTimeRate": (workload.get("background") or {}).get("onTimeRate"),
            "backgroundDropRate": (workload.get("background") or {}).get("dropRate"),
            "canaryP99MeanMs": mean(p99),
            "maxCpuBusyMean": max((float(item.get("cpuBusyMean")) for item in nodeMetrics.values()
                                    if item.get("cpuBusyMean") is not None), default=None),
            "maxCpuPsiSomeAvg10Mean": max((float(item.get("cpuPsiSomeAvg10Mean")) for item in nodeMetrics.values()
                                            if item.get("cpuPsiSomeAvg10Mean") is not None), default=None),
            "maxRunQueueMean": max((float(item.get("runQueueMean")) for item in nodeMetrics.values()
                                     if item.get("runQueueMean") is not None), default=None),
            "artifact": str(summaryPath),
        })
    return rows


def writeCsv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0]) if rows else ["mappingId"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def plotRows(path: Path, rows: list[dict[str, Any]]) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        return
    valid = [row for row in rows if row.get("udpStatus") == "success"]
    if not valid:
        return
    x = [row.get("targetWorkerPodCount") for row in valid]
    fig, axes = plt.subplots(2, 2, figsize=(10, 7), constrained_layout=True)
    axes = axes.ravel()
    series = [
        ("udpDeliveryRatio", "UDP delivery ratio", "ratio"),
        ("udpLossPctMean", "UDP mean loss", "%"),
        ("tcpGoodputMbps", "TCP aggregate goodput", "Mbps"),
        ("canaryP99MeanMs", "Canary P99", "ms"),
    ]
    for axis, (key, title, ylabel) in zip(axes, series):
        y = [row.get(key) for row in valid]
        axis.plot(x, y, marker="o")
        axis.set_title(title)
        axis.set_xlabel("Pod count on target worker")
        axis.set_ylabel(ylabel)
        axis.grid(True, alpha=0.3)
    fig.savefig(path, dpi=180)
    plt.close(fig)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args(argv)
    runDir = args.run_dir.resolve()
    rows = pointRows(runDir)
    output = runDir / "e1b/analysis"
    writeCsv(output / "pilot_metrics.csv", rows)
    plotRows(output / "pilot_metrics.png", rows)
    statusCounts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("udpStatus") or "missing")
        statusCounts[status] = statusCounts.get(status, 0) + 1
    report = {
        "runDir": str(runDir), "pointCount": len(rows), "statusCounts": statusCounts,
        "validUdpPointCount": sum(row.get("udpStatus") == "success" for row in rows),
        "rows": rows,
        "plot": str(output / "pilot_metrics.png"),
    }
    (output / "pilot_analysis.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("runDir", "pointCount", "statusCounts", "validUdpPointCount")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
