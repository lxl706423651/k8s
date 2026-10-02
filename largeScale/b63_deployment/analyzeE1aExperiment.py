#!/usr/bin/env python3
"""Analyze E1-A point records and render the contention curves.

Inputs are one E1-A run directory containing raw point summaries. Outputs are
CSV/JSON metrics, PNG/PDF figures, and a Chinese report. The script is offline
and does not modify Kubernetes or libvirt resources.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any


def readPoints(runDir: Path) -> list[dict[str, Any]]:
    """Load every point summary below runDir/raw in stable order."""
    points: list[dict[str, Any]] = []
    for path in sorted((runDir / "raw").glob("**/point_summary.json")):
        item = json.loads(path.read_text(encoding="utf-8"))
        item["source"] = str(path.relative_to(runDir))
        points.append(item)
    return points


def median(values: list[float]) -> float | None:
    """Return a finite median, or None for an empty input."""
    finite = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    return statistics.median(finite) if finite else None


def aggregatePoints(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate independent point records by phase, group, and N."""
    grouped: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for point in points:
        grouped[(point["phase"], point["group"], int(point["N"]))].append(point)
    rows: list[dict[str, Any]] = []
    for (phase, group, podCount), values in sorted(grouped.items()):
        latencies = [item.get("canary", {}).get("latency_p99_ms") for item in values]
        onTime = [item.get("canaryOnTimeRate") for item in values]
        rows.append({
            "phase": phase,
            "group": group,
            "N": podCount,
            "repetitions": len(values),
            "successCount": sum(item.get("status") in {"success", "workload_overload"} for item in values),
            "workloadOverloadCount": sum(item.get("status") == "workload_overload" for item in values),
            "workloadOverloadRate": (
                sum(item.get("status") == "workload_overload" for item in values) / len(values)
            ),
            "canaryP99MedianMs": median(latencies),
            "canaryP99MinMs": min((x for x in latencies if x is not None), default=None),
            "canaryP99MaxMs": max((x for x in latencies if x is not None), default=None),
            "canaryOnTimeMedian": median(onTime),
            "cpuBusyMedian": median([item.get("nodeMetrics", {}).get("cpuBusyMean") for item in values]),
            "runQueueMedian": median([item.get("nodeMetrics", {}).get("runQueueMean") for item in values]),
            "cpuPsiSomeAvg10Median": median([item.get("nodeMetrics", {}).get("cpuPsiSomeAvg10Mean") for item in values]),
            "backgroundCompletionRateMedian": median([item.get("background", {}).get("completionRate") for item in values]),
            "backgroundDropRateMedian": median([item.get("background", {}).get("dropRate") for item in values]),
        })
    return rows


def addMarginalMetrics(rows: list[dict[str, Any]]) -> None:
    """Add baseline ratios and observed-step marginal latency changes in place."""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row["phase"], row["group"])].append(row)
    for values in grouped.values():
        values.sort(key=lambda item: item["N"])
        baseline = next((item["canaryP99MedianMs"] for item in values if item["N"] == 0), None)
        previous: dict[str, Any] | None = None
        for item in values:
            latency = item["canaryP99MedianMs"]
            item["relativeToN0"] = latency / baseline - 1.0 if latency is not None and baseline else None
            item["marginalStepPods"] = item["N"] - previous["N"] if previous else None
            item["marginalP99Ms"] = latency - previous["canaryP99MedianMs"] \
                if previous and latency is not None and previous["canaryP99MedianMs"] is not None else None
            previous = item


def fitLine(points: list[tuple[float, float]]) -> tuple[float, float, float]:
    """Fit y=a+b*x and return intercept, slope, and squared error."""
    xMean = statistics.fmean(x for x, _ in points)
    yMean = statistics.fmean(y for _, y in points)
    denominator = sum((x - xMean) ** 2 for x, _ in points)
    slope = sum((x - xMean) * (y - yMean) for x, y in points) / denominator if denominator else 0.0
    intercept = yMean - slope * xMean
    error = sum((y - intercept - slope * x) ** 2 for x, y in points)
    return intercept, slope, error


def detectKnee(rows: list[dict[str, Any]], phase: str, slowdownReference: float,
               onTimeThreshold: float = 0.99) -> dict[str, Any]:
    """Detect a descriptive knee without treating it as a required outcome."""
    loaded = [row for row in rows if row["phase"] == phase and row["group"] == "loaded"
              and row["canaryP99MedianMs"] is not None]
    loaded.sort(key=lambda item: item["N"])
    thresholdN = next((row["N"] for row in loaded
                       if row.get("relativeToN0") is not None and row["relativeToN0"] >= slowdownReference), None)
    firstOverloadIndex = next((index for index, row in enumerate(loaded)
                               if row.get("workloadOverloadCount", 0) > 0), None)
    firstOverloadN = loaded[firstOverloadIndex]["N"] if firstOverloadIndex is not None else None
    lastNonOverloadN = loaded[firstOverloadIndex - 1]["N"] \
        if firstOverloadIndex is not None and firstOverloadIndex > 0 else None
    firstCanaryLateN = next((row["N"] for row in loaded
                             if row.get("canaryOnTimeMedian") is not None
                             and row["canaryOnTimeMedian"] < onTimeThreshold), None)
    firstCpuSaturationN = next((row["N"] for row in loaded
                                if row.get("cpuBusyMedian") is not None
                                and row["cpuBusyMedian"] >= 0.99), None)
    firstBackgroundIncompleteN = next((row["N"] for row in loaded
                                       if row.get("backgroundCompletionRateMedian") is not None
                                       and row["backgroundCompletionRateMedian"] < 0.99), None)
    stableSuccess = [row for row in loaded if row.get("workloadOverloadCount", 0) == 0]
    stableOverload = [row for row in loaded
                      if row.get("repetitions", 0) > 0
                      and row.get("workloadOverloadCount", 0) == row["repetitions"]]
    lastStableSuccessN = max((row["N"] for row in stableSuccess), default=None)
    firstStableOverloadN = min((row["N"] for row in stableOverload), default=None)
    transitionRows = [row for row in loaded
                      if 0 < row.get("workloadOverloadCount", 0) < row.get("repetitions", 0)]
    output: dict[str, Any] = {
        "phase": phase,
        "slowdownReference": slowdownReference,
        "firstReferenceCrossingN": thresholdN,
        "onTimeThreshold": onTimeThreshold,
        "firstCanaryBelowOnTimeThresholdN": firstCanaryLateN,
        "firstCpuBusyAtLeast99PercentN": firstCpuSaturationN,
        "firstBackgroundCompletionBelow99PercentN": firstBackgroundIncompleteN,
        "firstWorkloadOverloadN": firstOverloadN,
        "lastNonOverloadN": lastNonOverloadN,
        "operationalTransitionBracket": [lastNonOverloadN, firstOverloadN]
        if lastNonOverloadN is not None and firstOverloadN is not None else None,
        "operationalTransitionObserved": firstOverloadN is not None,
        "lastStableSuccessN": lastStableSuccessN,
        "firstStableOverloadN": firstStableOverloadN,
        "repeatabilityTransitionBracket": [lastStableSuccessN, firstStableOverloadN]
        if lastStableSuccessN is not None and firstStableOverloadN is not None
        and lastStableSuccessN < firstStableOverloadN else None,
        "mixedOutcomeN": [row["N"] for row in transitionRows],
        "segmentedKneeN": None,
        "segmentedSseImprovement": None,
        "clearKnee": False,
        "reason": "insufficient_points",
    }
    samples = [(float(row["N"]), float(row["canaryP99MedianMs"])) for row in loaded]
    if len(samples) < 6:
        return output
    _, _, straightError = fitLine(samples)
    candidates: list[tuple[float, int, float, float]] = []
    for split in range(3, len(samples) - 2):
        _, leftSlope, leftError = fitLine(samples[:split])
        _, rightSlope, rightError = fitLine(samples[split - 1:])
        candidates.append((leftError + rightError, split - 1, leftSlope, rightSlope))
    segmentedError, index, leftSlope, rightSlope = min(candidates)
    improvement = 1.0 - segmentedError / straightError if straightError > 0 else 0.0
    clear = improvement >= 0.25 and rightSlope > max(leftSlope * 1.5, leftSlope + 0.001)
    output.update({
        "segmentedKneeN": int(samples[index][0]),
        "segmentedSseImprovement": improvement,
        "lowRegionSlopeMsPerPod": leftSlope,
        "highRegionSlopeMsPerPod": rightSlope,
        "clearKnee": clear,
        "reason": "piecewise_fit_supported" if clear else "piecewise_fit_not_decisive",
    })
    output["selectedKneeN"] = output["segmentedKneeN"] if clear else firstOverloadN
    output["selectionBasis"] = "segmented_latency_fit" if clear else (
        "first_predeclared_overload_status" if firstOverloadN is not None else "none")
    return output


def writeCsv(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write a rectangular aggregate CSV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def renderFigures(runDir: Path, rows: list[dict[str, Any]], phase: str, knee: dict[str, Any]) -> list[str]:
    """Render latency, timeliness, and node-pressure curves with matplotlib."""
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        return []
    figureDir = runDir / "figures"
    figureDir.mkdir(parents=True, exist_ok=True)
    selected = [row for row in rows if row["phase"] == phase]
    colors = {"loaded": "#c0392b", "idle": "#2471a3"}
    outputs: list[str] = []

    fig, axes = plt.subplots(2, 2, figsize=(14, 8.5), constrained_layout=True)
    for group in ("loaded", "idle"):
        values = sorted((row for row in selected if row["group"] == group), key=lambda item: item["N"])
        if not values:
            continue
        x = [row["N"] for row in values]
        y = [row["canaryP99MedianMs"] for row in values]
        lower = [max(0.0, value - row["canaryP99MinMs"]) for value, row in zip(y, values)]
        upper = [max(0.0, row["canaryP99MaxMs"] - value) for value, row in zip(y, values)]
        axes[0, 0].errorbar(x, y, yerr=[lower, upper], marker="o", capsize=3,
                            label=group, color=colors[group])
        axes[0, 1].plot(x, [row["canaryOnTimeMedian"] for row in values], marker="o", label=group, color=colors[group])
        axes[1, 0].plot(x, [row["cpuBusyMedian"] for row in values], marker="o", label=group, color=colors[group])
        axes[1, 1].plot(x, [row["runQueueMedian"] for row in values], marker="o", label=group, color=colors[group])
    if knee.get("selectedKneeN") is not None:
        label = "segmented knee" if knee.get("clearKnee") else "first overload point"
        axes[0, 0].axvline(knee["selectedKneeN"], linestyle="--", color="#7d3c98", label=label)
    repeatableBracket = knee.get("repeatabilityTransitionBracket")
    if repeatableBracket:
        axes[0, 0].axvspan(repeatableBracket[0], repeatableBracket[1], alpha=0.12,
                           color="#f39c12", label="repeatability transition")
    axes[0, 0].set(title="Canary completion latency", ylabel="P99 (ms)")
    axes[0, 1].set(title="Canary on-time rate", ylabel="rate", ylim=(0, 1.02))
    axes[1, 0].set(title="Worker CPU busy", ylabel="fraction", xlabel="background workload Pods N")
    axes[1, 1].set(title="Worker run queue", ylabel="runnable tasks", xlabel="background workload Pods N")
    for axis in axes.flat:
        axis.grid(alpha=0.25)
        axis.legend()
    for suffix in ("png", "pdf"):
        path = figureDir / f"e1a_{phase}_curves.{suffix}"
        fig.savefig(path, dpi=180)
        outputs.append(str(path.relative_to(runDir)))
    plt.close(fig)

    focus = [row for row in selected if row["N"] <= 100]
    fig, axes = plt.subplots(2, 2, figsize=(14, 8.5), constrained_layout=True)
    for group in ("loaded", "idle"):
        values = sorted((row for row in focus if row["group"] == group), key=lambda item: item["N"])
        if not values:
            continue
        x = [row["N"] for row in values]
        y = [row["canaryP99MedianMs"] for row in values]
        lower = [max(0.0, value - row["canaryP99MinMs"]) for value, row in zip(y, values)]
        upper = [max(0.0, row["canaryP99MaxMs"] - value) for value, row in zip(y, values)]
        axes[0, 0].errorbar(x, y, yerr=[lower, upper], marker="o", capsize=3,
                            label=group, color=colors[group])
        axes[0, 1].plot(x, [row["workloadOverloadRate"] for row in values], marker="o",
                        label=group, color=colors[group])
        axes[1, 0].plot(x, [row["cpuBusyMedian"] for row in values], marker="o",
                        label=group, color=colors[group])
        axes[1, 1].plot(x, [row["runQueueMedian"] for row in values], marker="o",
                        label=group, color=colors[group])
    if repeatableBracket:
        for axis in axes.flat:
            axis.axvspan(repeatableBracket[0], repeatableBracket[1], alpha=0.12,
                         color="#f39c12")
    if knee.get("firstStableOverloadN") is not None:
        axes[0, 1].axvline(knee["firstStableOverloadN"], linestyle="--",
                           color="#7d3c98", label="first stable overload")
    axes[0, 0].set(title="Canary P99 near the transition", ylabel="P99 (ms)")
    axes[0, 1].set(title="Predeclared overload fraction", ylabel="fraction", ylim=(-0.03, 1.03))
    axes[1, 0].set(title="Worker CPU busy", ylabel="fraction")
    axes[1, 1].set(title="Worker run queue", ylabel="runnable tasks")
    fig.supxlabel("background workload Pods N")
    for axis in axes.flat:
        axis.grid(alpha=0.25)
        axis.legend()
    for suffix in ("png", "pdf"):
        path = figureDir / f"e1a_{phase}_transition.{suffix}"
        fig.savefig(path, dpi=180)
        outputs.append(str(path.relative_to(runDir)))
    plt.close(fig)
    return outputs


def writeReport(runDir: Path, points: list[dict[str, Any]], rows: list[dict[str, Any]], knee: dict[str, Any], phase: str) -> None:
    """Write a concise Chinese evidence report with validity boundaries."""
    selected = [point for point in points if point["phase"] == phase]
    groups = sorted({point["group"] for point in selected})
    statusCounts: dict[str, int] = defaultdict(int)
    for point in selected:
        statusCounts[point.get("status", "missing")] += 1
    if knee.get("clearKnee"):
        finding = (f"分段拟合支持候选拐点 N={knee['segmentedKneeN']}，"
                   f"SSE 改善 {knee['segmentedSseImprovement']:.1%}。")
    elif knee.get("operationalTransitionObserved"):
        finding = (f"分段时延拟合尚不明确，但冻结状态判据首次在 N="
                   f"{knee['firstWorkloadOverloadN']} 进入 workload_overload；首次观测转折区间为 "
                   f"({knee['lastNonOverloadN']}, {knee['firstWorkloadOverloadN']}]。")
    else:
        finding = "当前数据没有给出明确的分段拐点或过载转折；这不属于脚本失败。"
    lines = [
        "# E1-A 单 VM Pod 数量扫描报告",
        "",
        f"分析阶段：`{phase}`；有效点记录：{len(selected)}；组别：{', '.join(groups) or '无'}。",
        "",
        finding,
        "",
        (f"跨重复块的稳定正常/稳定过载边界为 "
         f"({knee.get('lastStableSuccessN')}, {knee.get('firstStableOverloadN')}]；"
         f"出现混合结果的 N 为 {knee.get('mixedOutcomeN', [])}。")
        if knee.get("repeatabilityTransitionBracket") else
        "当前重复数据尚未形成稳定正常到稳定过载的完整边界。",
        "",
        (f"Loaded 组的 CPU busy 中位数首次达到 99% 的 N 为 "
         f"{knee.get('firstCpuBusyAtLeast99PercentN', '未观察到')}；背景任务完成率中位数首次低于 99% 的 N 为 "
         f"{knee.get('firstBackgroundCompletionBelow99PercentN', '未观察到')}。"),
        "",
        f"预设的相对 N=0 P99 恶化参考线为 {knee['slowdownReference']:.0%}；首次跨线 N："
        f"{knee['firstReferenceCrossingN'] if knee['firstReferenceCrossingN'] is not None else '未观察到'}。",
        "",
        "状态计数：" + "，".join(f"{key}={value}" for key, value in sorted(statusCounts.items())) + "。",
        "",
        "该结果只刻画给定 4 vCPU/8 GiB VM 和冻结工作画像下的本地共置代价。"
        "它尚未验证 SEED 拓扑中的通信局部性与争用权衡，不能代替 E1-B。",
        "",
        "## 聚合数据",
        "",
        "| group | N | repeats | overload | canary P99 median [min,max] ms | on-time | CPU busy | run queue |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in (item for item in rows if item["phase"] == phase):
        formatValue = lambda value: "NA" if value is None else f"{value:.6g}"
        latencyRange = (f"{formatValue(row['canaryP99MedianMs'])} "
                        f"[{formatValue(row['canaryP99MinMs'])},{formatValue(row['canaryP99MaxMs'])}]")
        lines.append(f"| {row['group']} | {row['N']} | {row['repetitions']} | "
                     f"{row['workloadOverloadCount']}/{row['repetitions']} | {latencyRange} | "
                     f"{formatValue(row['canaryOnTimeMedian'])} | "
                     f"{formatValue(row['cpuBusyMedian'])} | {formatValue(row['runQueueMedian'])} |")
    (runDir / "report_zh.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def analyzeExperiment(runDir: Path, phase: str = "auto") -> dict[str, Any]:
    """Analyze runDir and return the serializable knee summary."""
    points = readPoints(runDir)
    if not points:
        raise ValueError(f"no point summaries under {runDir / 'raw'}")
    availablePhases = {point["phase"] for point in points}
    selectedPhase = ("formal" if "formal" in availablePhases else "pilot") if phase == "auto" else phase
    rows = aggregatePoints(points)
    addMarginalMetrics(rows)
    metadataPath = runDir / "run_metadata.json"
    metadata = json.loads(metadataPath.read_text(encoding="utf-8")) if metadataPath.exists() else {}
    slowdown = float(metadata.get("relativeSlowdownReference", 0.20))
    onTimeThreshold = float(metadata.get("overloadOnTimeThreshold", 0.99))
    knee = detectKnee(rows, selectedPhase, slowdown, onTimeThreshold)
    writeCsv(runDir / "metrics" / "e1a_summary.csv", rows)
    (runDir / "metrics").mkdir(parents=True, exist_ok=True)
    (runDir / "metrics" / "knee.json").write_text(json.dumps(knee, indent=2) + "\n", encoding="utf-8")
    knee["figures"] = renderFigures(runDir, rows, selectedPhase, knee)
    writeReport(runDir, points, rows, knee, selectedPhase)
    return knee


def parseArguments() -> argparse.Namespace:
    """Parse offline analyzer arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--phase", choices=("auto", "pilot", "formal"), default="auto")
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parseArguments()
    print(json.dumps(analyzeExperiment(arguments.run_dir.resolve(), arguments.phase), indent=2))
