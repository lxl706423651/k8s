#!/usr/bin/env python3
"""Render the E1-B Pod-placement tradeoff figure.

Inputs:
- E1-B formal mapping summary CSV.
- E1-B point metrics CSV with artifact paths.
- Frozen placement sequence JSON for the primary mapping order.

Outputs:
- PNG/PDF figure showing target-VM Pod count versus cross-VM bandwidth saved,
  UDP delivery rate, and on-time background task processing speed.
- CSV summary with the values used in the figure.

Side effects:
- Creates only local files under the requested output directory.

Execution context:
- Run from the B63 deployment directory after E1-B formal analysis has
  generated its `analysis/` artifacts.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent
DEFAULT_FORMAL = HERE / "experiments/e1_marginal_pods/20260919_e1b_corrected_pilot4/e1b/formal2"
DEFAULT_MAPPING_SUMMARY = DEFAULT_FORMAL / "analysis/formal_mapping_summary.csv"
DEFAULT_POINT_METRICS = DEFAULT_FORMAL / "analysis/formal_point_metrics.csv"
DEFAULT_PLACEMENT_SEQUENCE = (
    HERE / "experiments/e1_marginal_pods/e1b_topology214_frozen_20260918/plans/placement_sequence.json"
)


def readJson(path: Path) -> Any:
    """Read one JSON file."""
    return json.loads(path.read_text(encoding="utf-8"))


def readCsv(path: Path) -> list[dict[str, str]]:
    """Read one CSV file as dictionaries."""
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def finiteFloat(value: Any, default: float = math.nan) -> float:
    """Convert a value to a finite float when possible."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def sampleSd(values: list[float]) -> float:
    """Return sample standard deviation, or zero for one value."""
    return statistics.stdev(values) if len(values) > 1 else 0.0


def loadPrimarySequence(path: Path) -> list[str]:
    """Load the frozen primary placement order."""
    value = readJson(path)
    sequence = value.get("primarySequence") or []
    if not sequence:
        sequence = [
            item.get("mappingId")
            for item in value.get("mappings", [])
            if str(item.get("mappingId", "")).startswith("pi")
        ]
    return [str(item) for item in sequence]


def collectTaskSpeed(pointMetrics: Path, workloadMeasurementSeconds: float) -> dict[str, dict[str, float]]:
    """Aggregate on-time background task processing speed per mapping.

    Only workload-valid UDP phase points are included. The speed is the number
    of background tasks completed before their deadline divided by the frozen
    workload measurement duration.
    """
    grouped: dict[str, list[float]] = {}
    for row in readCsv(pointMetrics):
        if row.get("udpWorkloadValid") != "True":
            continue
        artifact = Path(row.get("artifact", ""))
        if not artifact.is_file():
            continue
        point = readJson(artifact)
        background = point.get("udpWorkload", {}).get("background", {})
        completedOnTime = finiteFloat(background.get("completed_on_time"))
        if not math.isfinite(completedOnTime):
            continue
        grouped.setdefault(row["mappingId"], []).append(completedOnTime / workloadMeasurementSeconds)
    return {
        mappingId: {
            "taskSpeedJobsPerSecondMean": statistics.fmean(values),
            "taskSpeedJobsPerSecondSampleSd": sampleSd(values),
            "taskSpeedSampleCount": len(values),
        }
        for mappingId, values in grouped.items()
    }


def buildRows(arguments: argparse.Namespace) -> list[dict[str, Any]]:
    """Build one row per primary E1-B mapping for plotting."""
    summary = {row["mappingId"]: row for row in readCsv(arguments.mapping_summary)}
    sequence = loadPrimarySequence(arguments.placement_sequence)
    speeds = collectTaskSpeed(arguments.point_metrics, arguments.workload_measurement_seconds)
    baselineCrossings = max(
        int(finiteFloat(summary[mappingId]["fixedFlowCrossings"], 0.0))
        for mappingId in sequence
        if mappingId in summary
    )
    rows: list[dict[str, Any]] = []
    for mappingId in sequence:
        if mappingId not in summary:
            continue
        source = summary[mappingId]
        fixedCrossings = int(finiteFloat(source["fixedFlowCrossings"], 0.0))
        row: dict[str, Any] = {
            "mappingId": mappingId,
            "targetWorkerPodCount": int(finiteFloat(source["targetWorkerPodCount"], 0.0)),
            "fixedFlowCrossings": fixedCrossings,
            "crossVmOfferedMbps": fixedCrossings * arguments.offered_mbps_per_flow,
            "crossVmBandwidthSavedMbps": (baselineCrossings - fixedCrossings) * arguments.offered_mbps_per_flow,
            "udpDeliveryRatioMean": finiteFloat(source["udpDeliveryRatioMean"]),
            "udpDeliveryRatioSampleSd": finiteFloat(source["udpDeliveryRatioSampleSd"], 0.0),
            "udpWorkloadOnTimeRateMean": finiteFloat(source["udpBackgroundOnTimeMeanMean"]),
            "udpWorkloadOnTimeRateSampleSd": finiteFloat(source["udpBackgroundOnTimeMeanSampleSd"], 0.0),
            "udpWorkloadValidCount": int(finiteFloat(source["udpWorkloadValidCount"], 0.0)),
        }
        row.update(speeds.get(mappingId, {
            "taskSpeedJobsPerSecondMean": math.nan,
            "taskSpeedJobsPerSecondSampleSd": math.nan,
            "taskSpeedSampleCount": 0,
        }))
        rows.append(row)
    rows.sort(key=lambda item: item["targetWorkerPodCount"])
    return rows


def writeCsv(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write summary rows to CSV."""
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def renderFigure(rows: list[dict[str, Any]], outputDir: Path) -> list[Path]:
    """Render the requested three-panel figure."""
    import matplotlib.pyplot as plt

    outputDir.mkdir(parents=True, exist_ok=True)
    x = [row["targetWorkerPodCount"] for row in rows]
    mappingLabels = [row["mappingId"].split("-")[0] for row in rows]

    fig, axes = plt.subplots(3, 1, figsize=(9.2, 10.2), sharex=True, constrained_layout=True)
    fig.suptitle("E1-B placement tradeoff (VM: 4 workers × 4 vCPU / 8 GiB; topology: 214 Pods)", fontsize=13)

    axes[0].plot(x, [row["crossVmBandwidthSavedMbps"] for row in rows],
                 marker="o", linewidth=2.2, color="#1f77b4")
    axes[0].set_ylabel("Cross-VM bandwidth\nsaved (Mbps)")
    axes[0].set_ylim(bottom=-10)

    axes[1].errorbar(
        x,
        [row["udpDeliveryRatioMean"] for row in rows],
        yerr=[row["udpDeliveryRatioSampleSd"] for row in rows],
        marker="o",
        capsize=3,
        linewidth=2.2,
        color="#d62728",
    )
    axes[1].set_ylabel("UDP delivery rate")
    axes[1].set_ylim(0.0, 1.02)

    axes[2].errorbar(
        x,
        [row["taskSpeedJobsPerSecondMean"] for row in rows],
        yerr=[row["taskSpeedJobsPerSecondSampleSd"] for row in rows],
        marker="o",
        capsize=3,
        linewidth=2.2,
        color="#2ca02c",
        label="on-time background tasks",
    )
    axes[2].set_ylabel("On-time task\nprocessing speed (jobs/s)")
    axes[2].set_xlabel("Pods placed on the target VM")

    for axis in axes:
        axis.grid(alpha=0.28)
        axis.set_xticks(x)
    axes[2].set_xticklabels([f"{count}\n{label}" for count, label in zip(x, mappingLabels)])
    axes[1].axvspan(64, 74, color="#f39c12", alpha=0.12, label="observed transition")
    axes[1].legend(loc="lower left")
    axes[2].text(
        0.99,
        0.03,
        "pi04 workload speed uses 4 valid formal UDP repeats",
        transform=axes[2].transAxes,
        ha="right",
        va="bottom",
        fontsize=8,
        color="#555",
    )

    outputs: list[Path] = []
    for suffix in ("png", "pdf"):
        path = outputDir / f"e1b_pod_placement_tradeoff.{suffix}"
        fig.savefig(path, dpi=180)
        outputs.append(path)
    plt.close(fig)
    return outputs


def parseArguments() -> argparse.Namespace:
    """Parse CLI options."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mapping-summary", type=Path, default=DEFAULT_MAPPING_SUMMARY)
    parser.add_argument("--point-metrics", type=Path, default=DEFAULT_POINT_METRICS)
    parser.add_argument("--placement-sequence", type=Path, default=DEFAULT_PLACEMENT_SEQUENCE)
    parser.add_argument("--output-dir", type=Path,
                        default=HERE / "experiments/e1_marginal_pods/20260920_tradeoff_plot")
    parser.add_argument("--offered-mbps-per-flow", type=float, default=25.0)
    parser.add_argument("--workload-measurement-seconds", type=float, default=80.0)
    return parser.parse_args()


def main(arguments: argparse.Namespace) -> int:
    """Create the figure and data table."""
    rows = buildRows(arguments)
    if not rows:
        raise RuntimeError("no primary E1-B rows available for plotting")
    outputDir = arguments.output_dir.resolve()
    writeCsv(outputDir / "e1b_pod_placement_tradeoff.csv", rows)
    outputs = renderFigure(rows, outputDir)
    print(json.dumps({
        "outputDir": str(outputDir),
        "figure": [str(path) for path in outputs],
        "csv": str(outputDir / "e1b_pod_placement_tradeoff.csv"),
        "rows": rows,
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(parseArguments()))
