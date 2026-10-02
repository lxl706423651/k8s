#!/usr/bin/env python3
"""Aggregate corrected E1-B formal point artifacts without hiding failures.

The formal runner writes one point directory per static mapping.  This tool is
safe to run while the runner is active: incomplete points are reported as
incomplete and are not treated as measurements.  It emits per-point CSV/JSON,
mapping-level mean/sample-SD summaries, and a compact plot when matplotlib is
available.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def numeric(values: list[Any]) -> list[float]:
    return [float(value) for value in values if isinstance(value, (int, float)) and math.isfinite(float(value))]


def mean(values: list[Any]) -> float | None:
    vals = numeric(values)
    return sum(vals) / len(vals) if vals else None


def sample_sd(values: list[Any]) -> float | None:
    vals = numeric(values)
    if len(vals) < 2:
        return 0.0 if vals else None
    avg = sum(vals) / len(vals)
    return math.sqrt(sum((value - avg) ** 2 for value in vals) / (len(vals) - 1))


def phase_metrics(phase: dict[str, Any], prefix: str) -> dict[str, Any]:
    background = phase.get("background") or {}
    canaries = phase.get("canaries") or []
    node_metrics = phase.get("perNodeMetrics") or {}
    p99 = numeric([item.get("latency_p99_ms") for item in canaries])
    status = phase.get("status")
    background_valid = background.get("countConservationOk", True) is not False
    canary_valid = all(
        item.get("status") == "success" and item.get("count_conservation_ok", True) is not False
        for item in canaries
    )
    workload_valid = status == "success" and background_valid and canary_valid
    return {
        f"{prefix}WorkloadStatus": status,
        f"{prefix}WorkloadValid": workload_valid,
        f"{prefix}BackgroundOnTimeMean": background.get("onTimeRate"),
        f"{prefix}BackgroundDropMean": background.get("dropRate"),
        f"{prefix}CanaryP99MeanMs": mean(p99),
        f"{prefix}MaxCpuBusyMean": max(
            numeric([item.get("cpuBusyMean") for item in node_metrics.values()]), default=None),
        f"{prefix}MaxPsiSomeAvg10Mean": max(
            numeric([item.get("cpuPsiSomeAvg10Mean") for item in node_metrics.values()]), default=None),
        f"{prefix}MaxRunQueueMean": max(
            numeric([item.get("runQueueMean") for item in node_metrics.values()]), default=None),
    }


def sequence_index(run_dir: Path) -> dict[str, dict[str, Any]]:
    path = run_dir / "e1b/plans/placement_sequence.json"
    if not path.is_file():
        return {}
    return {item["mappingId"]: item for item in read_json(path).get("mappings", [])}


def point_rows(formal_dir: Path, mapping_index: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for point_path in sorted(formal_dir.glob("block*/[0-9][0-9]-*/point_summary.json")):
        point = read_json(point_path)
        mapping_id = str(point.get("mappingId") or point_path.parent.name.split("-", 1)[-1])
        mapping = mapping_index.get(mapping_id, {})
        placement = point.get("placement") or {}
        udp = point.get("udp") or {}
        tcp = point.get("tcp") or {}
        row: dict[str, Any] = {
            "block": point.get("block"),
            "position": point.get("position"),
            "mappingId": mapping_id,
            "targetWorkerPodCount": mapping.get("targetWorkerPodCount"),
            "fixedFlowCrossings": mapping.get("fixedFlowCrossings"),
            "xExpectedPayloadMbpsCrossings": mapping.get("xExpectedPayloadMbpsCrossings"),
            "placementStatus": placement.get("status"),
            "podCount": placement.get("podCount"),
            "udpStatus": udp.get("status"),
            "udpReceivedMbps": udp.get("receivedMbps"),
            "udpDeliveryRatio": udp.get("deliveryRatio"),
            "udpLossPctMean": udp.get("lossPctMean"),
            "tcpStatus": tcp.get("status"),
            "tcpGoodputMbps": (tcp.get("aggregateGoodputMbps") or {}).get("mean"),
            "artifact": str(point_path),
        }
        row.update(phase_metrics(point.get("udpWorkload") or {}, "udp"))
        row.update(phase_metrics(point.get("tcpWorkload") or {}, "tcp"))
        rows.append(row)
    return rows


def grouped(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = ["udpReceivedMbps", "udpDeliveryRatio", "udpLossPctMean", "tcpGoodputMbps",
            "udpBackgroundOnTimeMean", "udpBackgroundDropMean", "udpCanaryP99MeanMs",
            "tcpBackgroundOnTimeMean", "tcpBackgroundDropMean", "tcpCanaryP99MeanMs",
            "udpMaxPsiSomeAvg10Mean", "tcpMaxPsiSomeAvg10Mean", "udpMaxRunQueueMean",
            "tcpMaxRunQueueMean"]
    workload_prefix = {
        "udpBackgroundOnTimeMean": "udp",
        "udpBackgroundDropMean": "udp",
        "udpCanaryP99MeanMs": "udp",
        "udpMaxPsiSomeAvg10Mean": "udp",
        "udpMaxRunQueueMean": "udp",
        "tcpBackgroundOnTimeMean": "tcp",
        "tcpBackgroundDropMean": "tcp",
        "tcpCanaryP99MeanMs": "tcp",
        "tcpMaxPsiSomeAvg10Mean": "tcp",
        "tcpMaxRunQueueMean": "tcp",
    }
    dataplane_prefix = {
        "udpReceivedMbps": "udp",
        "udpDeliveryRatio": "udp",
        "udpLossPctMean": "udp",
        "tcpGoodputMbps": "tcp",
    }
    result: list[dict[str, Any]] = []
    for mapping_id in sorted({str(row["mappingId"]) for row in rows}):
        selected = [row for row in rows if str(row["mappingId"]) == mapping_id]
        base = selected[0]
        out: dict[str, Any] = {
            "mappingId": mapping_id,
            "targetWorkerPodCount": base.get("targetWorkerPodCount"),
            "fixedFlowCrossings": base.get("fixedFlowCrossings"),
            "pointCount": len(selected),
            "placementPassCount": sum(row.get("placementStatus") == "PASS" for row in selected),
            "udpDataplaneValidCount": sum(row.get("udpStatus") == "success" for row in selected),
            "tcpDataplaneValidCount": sum(row.get("tcpStatus") == "success" for row in selected),
            "udpWorkloadValidCount": sum(row.get("udpWorkloadValid") is True for row in selected),
            "tcpWorkloadValidCount": sum(row.get("tcpWorkloadValid") is True for row in selected),
        }
        for key in keys:
            if key in workload_prefix:
                prefix = workload_prefix[key]
                valid_rows = [row for row in selected if row.get(f"{prefix}WorkloadValid") is True]
            else:
                prefix = dataplane_prefix[key]
                valid_rows = [row for row in selected if row.get(f"{prefix}Status") == "success"]
            vals = [row.get(key) for row in valid_rows]
            out[f"{key}Mean"] = mean(vals)
            out[f"{key}SampleSd"] = sample_sd(vals)
        result.append(out)
    return result


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0]) if rows else ["mappingId"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def plot(path: Path, rows: list[dict[str, Any]]) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        return
    if not rows:
        return
    rows.sort(key=lambda row: (row.get("targetWorkerPodCount") is None, row.get("targetWorkerPodCount") or 0))
    x = [row.get("targetWorkerPodCount") for row in rows]
    fig, axes = plt.subplots(2, 2, figsize=(10, 7), constrained_layout=True)
    for axis, key, title, ylabel in [
        (axes[0, 0], "udpReceivedMbpsMean", "UDP receive", "Mbps"),
        (axes[0, 1], "udpDeliveryRatioMean", "UDP delivery", "ratio"),
        (axes[1, 0], "udpCanaryP99MeanMsMean", "UDP canary P99", "ms"),
        (axes[1, 1], "udpBackgroundOnTimeMeanMean", "UDP background on-time", "ratio"),
    ]:
        y = [row.get(key) for row in rows]
        axis.plot(x, y, marker="o")
        axis.set_xlabel("Target-worker emulation pods")
        axis.set_ylabel(ylabel)
        axis.set_title(title)
        axis.grid(True, alpha=0.3)
    fig.savefig(path, dpi=180)
    plt.close(fig)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--formal-subdir", default="e1b/formal2")
    args = parser.parse_args(argv)
    run_dir = args.run_dir.resolve()
    formal_dir = run_dir / args.formal_subdir
    rows = point_rows(formal_dir, sequence_index(run_dir))
    output = formal_dir / "analysis"
    write_csv(output / "formal_point_metrics.csv", rows)
    grouped_rows = grouped(rows)
    write_csv(output / "formal_mapping_summary.csv", grouped_rows)
    plot(output / "formal_curves.png", grouped_rows)
    def status_counts(key: str) -> dict[str, int]:
        return {
            str(status): sum(1 for row in rows if row.get(key) == status)
            for status in sorted({str(row.get(key)) for row in rows})
        }

    report = {
        "runDir": str(run_dir), "formalDir": str(formal_dir), "pointCount": len(rows),
        "mappingCount": len(grouped_rows), "completedStatusCounts": {
            str(status): sum(1 for row in rows if row.get("udpStatus") == status)
            for status in sorted({str(row.get("udpStatus")) for row in rows})
        },
        "dataQuality": {
            "placementStatusCounts": status_counts("placementStatus"),
            "udpDataplaneStatusCounts": status_counts("udpStatus"),
            "tcpDataplaneStatusCounts": status_counts("tcpStatus"),
            "udpWorkloadStatusCounts": status_counts("udpWorkloadStatus"),
            "tcpWorkloadStatusCounts": status_counts("tcpWorkloadStatus"),
            "udpWorkloadValidCount": sum(row.get("udpWorkloadValid") is True for row in rows),
            "tcpWorkloadValidCount": sum(row.get("tcpWorkloadValid") is True for row in rows),
        },
        "points": rows, "mappings": grouped_rows,
        "plot": str(output / "formal_curves.png"),
    }
    (output / "formal_analysis.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("runDir", "formalDir", "pointCount", "mappingCount", "completedStatusCounts")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
