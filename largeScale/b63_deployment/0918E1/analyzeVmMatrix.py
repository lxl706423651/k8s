#!/usr/bin/env python3
"""Aggregate one 0918E1 matrix, detect a saturation knee, and draw the curves.

Inputs: a results/run-* directory containing as-NNNN/summary.json files.
Outputs: scale_metrics.csv, curve_summary.json, curve_report.md, and curve.png in that run.
Side effects: reads experiment artifacts and writes only derived files beside them.
Context: called automatically after a complete matrix or manually for partial/recovered results.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import yaml


SCRIPT_DIR = Path(__file__).resolve().parent


def parseArgs() -> argparse.Namespace:
    """Parse an explicit run directory or select the newest local batch."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path)
    return parser.parse_args()


def findRunDir(explicit: Path | None) -> Path:
    """Resolve the requested batch, defaulting to the newest run-* directory."""
    if explicit is not None:
        return explicit.expanduser().resolve()
    candidates = sorted((SCRIPT_DIR / "results").glob("run-*"), reverse=True)
    if not candidates:
        raise FileNotFoundError("no results/run-* directory found")
    return candidates[0].resolve()


def percentChange(current: float | None, baseline: float | None) -> float | None:
    """Return signed percent change while preserving missing/zero baselines."""
    if current is None or baseline in (None, 0):
        return None
    return (current - baseline) * 100.0 / baseline


def linearPercentile(values: list[float], percentage: float) -> float | None:
    """Return a linearly interpolated percentile for repeat-level values."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentage / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def repeatOutliers(values: list[float]) -> list[int]:
    """Return one-based repeat indexes outside Tukey's 1.5-IQR fences."""
    if len(values) < 4:
        return []
    lowerQuartile = float(linearPercentile(values, 25) or 0.0)
    upperQuartile = float(linearPercentile(values, 75) or 0.0)
    spread = upperQuartile - lowerQuartile
    lowerFence = lowerQuartile - 1.5 * spread
    upperFence = upperQuartile + 1.5 * spread
    return [index + 1 for index, value in enumerate(values) if value < lowerFence or value > upperFence]


def loadRows(runDir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load every valid scale summary and compute baseline-relative changes."""
    config = yaml.safe_load((runDir / "experiment.yaml").read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    for summaryPath in sorted(runDir.glob("as-*/summary.json")):
        summary = json.loads(summaryPath.read_text(encoding="utf-8"))
        aggregate = summary.get("aggregate") or {}
        repeats = summary.get("repeats") or []
        pairValues = [
            float(repeat["averagePairMbps"])
            for repeat in repeats
            if repeat.get("averagePairMbps") is not None
        ]
        rttValues = [
            float(repeat["averageRttMs"])
            for repeat in repeats
            if repeat.get("averageRttMs") is not None
        ]
        successValues = [int(repeat.get("iperfSuccessCount", 0)) for repeat in repeats]
        quality = summary.get("trafficQuality") or {}
        preflight = summary.get("preflight") or {}
        requiredRepeatCount = int(quality.get("requiredRepeatCount", len(repeats)))
        completedRepeatCount = quality.get("completedRepeatCount")
        if completedRepeatCount is None:
            completedRepeatCount = sum(
                1
                for repeat in repeats
                if bool(
                    repeat.get(
                        "iperfComplete",
                        int(repeat.get("iperfSuccessCount", 0)) == int(summary["iperfPairCount"]),
                    )
                )
                and int(repeat.get("iperfSuccessCount", 0)) == int(summary["iperfPairCount"])
            )
        trafficQualityValid = quality.get("valid")
        if trafficQualityValid is None:
            trafficQualityValid = completedRepeatCount == requiredRepeatCount
        rows.append(
            {
                "asCount": int(summary["asCount"]),
                "status": summary.get("status"),
                "configuredPairs": int(summary["iperfPairCount"]),
                "successfulPairs": aggregate.get("medianIperfSuccessCount"),
                "averagePairMbps": aggregate.get("medianAveragePairMbps"),
                "totalMeasuredMbps": aggregate.get("medianTotalMeasuredMbps"),
                "averageRttMs": aggregate.get("medianAverageRttMs"),
                "p95RttMs": aggregate.get("medianP95RttMs"),
                "packetLossPercent": aggregate.get("medianPacketLossPercent"),
                "cpuMeanPercent": aggregate.get("medianCpuMeanPercent"),
                "cpuP95Percent": aggregate.get("medianCpuP95Percent"),
                "cpuStealMeanPercent": aggregate.get("medianCpuStealMeanPercent"),
                "cpuPressureAvg10Mean": aggregate.get("medianCpuPressureAvg10Mean"),
                "memoryMeanPercent": aggregate.get("medianMemoryMeanPercent"),
                "memoryPeakGiB": (
                    float(aggregate["medianMemoryPeakBytes"]) / (1024**3)
                    if aggregate.get("medianMemoryPeakBytes") is not None
                    else None
                ),
                "memoryAvailableMeanGiB": (
                    float(aggregate["medianMemoryAvailableMeanBytes"]) / (1024**3)
                    if aggregate.get("medianMemoryAvailableMeanBytes") is not None
                    else None
                ),
                "memoryPressureAvg10Mean": aggregate.get("medianMemoryPressureAvg10Mean"),
                "swapMeanGiB": (
                    float(aggregate["medianSwapMeanBytes"]) / (1024**3)
                    if aggregate.get("medianSwapMeanBytes") is not None
                    else None
                ),
                "swapPeakGiB": (
                    float(aggregate["medianSwapPeakBytes"]) / (1024**3)
                    if aggregate.get("medianSwapPeakBytes") is not None
                    else None
                ),
                "guestInterfaceAggregateMbps": aggregate.get("medianGuestInterfaceAggregateMbps"),
                "estimatedAvoidedCrossVmMbps": aggregate.get("estimatedAvoidedCrossVmMbps"),
                "preflightStatus": preflight.get("status"),
                "preflightSuccessCount": preflight.get("successCount"),
                "requiredRepeatCount": requiredRepeatCount,
                "completedRepeatCount": completedRepeatCount,
                "trafficQualityValid": trafficQualityValid,
                "iperfSuccessMin": min(successValues) if successValues else None,
                "iperfSuccessMax": max(successValues) if successValues else None,
                "pairBandwidthStddevMbps": statistics.pstdev(pairValues) if len(pairValues) > 1 else 0.0,
                "rttStddevMs": statistics.pstdev(rttValues) if len(rttValues) > 1 else 0.0,
                "pairBandwidthOutlierRepeats": ",".join(map(str, repeatOutliers(pairValues))),
                "rttOutlierRepeats": ",".join(map(str, repeatOutliers(rttValues))),
            }
        )
    rows.sort(key=lambda row: row["asCount"])
    if not rows:
        raise FileNotFoundError(f"no as-*/summary.json files found under {runDir}")
    baseline = rows[0]
    for row in rows:
        row["averagePairBandwidthChangePercent"] = percentChange(
            row["averagePairMbps"], baseline["averagePairMbps"]
        )
        row["totalBandwidthChangePercent"] = percentChange(
            row["totalMeasuredMbps"], baseline["totalMeasuredMbps"]
        )
        row["averageRttChangePercent"] = percentChange(row["averageRttMs"], baseline["averageRttMs"])
    return rows, config


def loadFailedScales(runDir: Path) -> list[dict[str, Any]]:
    """Load failed scale records without mixing them into measured curve rows."""
    failures: list[dict[str, Any]] = []
    matrixPath = runDir / "matrix.json"
    matrix = json.loads(matrixPath.read_text(encoding="utf-8")) if matrixPath.is_file() else {}
    for run in matrix.get("runs", []):
        if run.get("status") == "PASS":
            continue
        asCount = int(run["asCount"])
        failurePath = runDir / f"as-{asCount:04d}" / "failure.json"
        summaryPath = runDir / f"as-{asCount:04d}" / "summary.json"
        if failurePath.is_file():
            detail = json.loads(failurePath.read_text(encoding="utf-8"))
        elif summaryPath.is_file():
            detail = json.loads(summaryPath.read_text(encoding="utf-8"))
        else:
            detail = {}
        failures.append({**run, "failureDetail": detail})
    return sorted(failures, key=lambda item: int(item["asCount"]))


def detectKnee(rows: list[dict[str, Any]], analysis: dict[str, Any]) -> dict[str, Any]:
    """Find the first scale meeting a documented degradation or high-CPU decline rule."""
    requiredMetrics = ("averagePairMbps", "totalMeasuredMbps", "averageRttMs")
    valid = [
        row
        for row in rows
        if row["status"] == "PASS" and all(row.get(metric) is not None for metric in requiredMetrics)
    ]
    if len(valid) < 2:
        return {"status": "INSUFFICIENT_DATA", "asCount": None, "reason": "有效 PASS 规模少于两个"}
    baselineRows = valid[: min(2, len(valid))]
    baselinePair = sum(float(row["averagePairMbps"]) for row in baselineRows) / len(baselineRows)
    baselineRtt = sum(float(row["averageRttMs"]) for row in baselineRows) / len(baselineRows)
    bandwidthLimit = baselinePair * (1.0 - float(analysis["bandwidthDropRatio"]))
    latencyLimit = baselineRtt * (1.0 + float(analysis["latencyRiseRatio"]))
    totalDrop = float(analysis["totalBandwidthDropRatio"])
    highCpu = float(analysis["highCpuPercent"])

    for index, row in enumerate(valid[1:], start=1):
        pairDegraded = float(row["averagePairMbps"]) <= bandwidthLimit
        latencyDegraded = float(row["averageRttMs"]) >= latencyLimit
        previous = valid[index - 1]
        totalDeclined = float(row["totalMeasuredMbps"]) <= float(previous["totalMeasuredMbps"]) * (1 - totalDrop)
        cpuHigh = float(row["cpuMeanPercent"] or 0) >= highCpu
        if pairDegraded and latencyDegraded:
            return {
                "status": "DETECTED",
                "asCount": row["asCount"],
                "reason": "平均每对带宽下降且平均 RTT 上升均越过配置门限",
                "pairBandwidthLimitMbps": bandwidthLimit,
                "latencyLimitMs": latencyLimit,
            }
        if totalDeclined and cpuHigh:
            return {
                "status": "DETECTED",
                "asCount": row["asCount"],
                "reason": "VM CPU 高负载时总吞吐较上一规模下降",
                "previousTotalMbps": previous["totalMeasuredMbps"],
                "currentTotalMbps": row["totalMeasuredMbps"],
            }
    return {
        "status": "NOT_OBSERVED",
        "asCount": None,
        "reason": "完整测量规模均未越过配置的带宽/时延拐点门限",
        "pairBandwidthLimitMbps": bandwidthLimit,
        "latencyLimitMs": latencyLimit,
    }


def detectCapacityBoundary(
    rows: list[dict[str, Any]], failedScales: list[dict[str, Any]]
) -> dict[str, Any]:
    """Describe the first hard execution failure separately from metric degradation."""
    passed = sorted(int(row["asCount"]) for row in rows if row.get("status") == "PASS")
    failed = sorted(int(item["asCount"]) for item in failedScales)
    if not passed or not failed:
        return {"status": "NOT_OBSERVED", "lastSuccessfulAsCount": max(passed) if passed else None}
    firstFailed = min(failed)
    earlierPassed = [scale for scale in passed if scale < firstFailed]
    return {
        "status": "DETECTED",
        "lastSuccessfulAsCount": max(earlierPassed) if earlierPassed else None,
        "firstFailedAsCount": firstFailed,
        "reason": "首个失败规模未完成配置的三次重复，属于完整运行边界",
    }


def writeCsv(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write the full scale table with stable columns."""
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def drawCurves(
    path: Path, rows: list[dict[str, Any]], knee: dict[str, Any], failedScales: list[dict[str, Any]]
) -> None:
    """Draw bandwidth, latency, and VM resource curves on a four-panel figure."""
    scales = [row["asCount"] for row in rows]
    figure, axes = plt.subplots(2, 2, figsize=(12, 8), constrained_layout=True)
    axes[0, 0].plot(scales, [row["averagePairMbps"] for row in rows], marker="o", label="Mean per pair")
    axes[0, 0].set_ylabel("Mbps")
    axes[0, 0].set_title("Per-pair throughput")
    axes[0, 1].plot(scales, [row["totalMeasuredMbps"] for row in rows], marker="o", color="tab:green")
    axes[0, 1].set_ylabel("Mbps")
    axes[0, 1].set_title("Total measured throughput")
    axes[1, 0].plot(scales, [row["averageRttMs"] for row in rows], marker="o", label="Mean RTT")
    axes[1, 0].plot(scales, [row["p95RttMs"] for row in rows], marker="s", label="p95 RTT")
    axes[1, 0].set_ylabel("ms")
    axes[1, 0].set_title("Latency under iperf load")
    axes[1, 0].legend()
    axes[1, 1].plot(scales, [row["cpuMeanPercent"] for row in rows], marker="o", label="CPU mean %")
    axes[1, 1].plot(scales, [row["memoryMeanPercent"] for row in rows], marker="s", label="Memory mean %")
    axes[1, 1].set_ylabel("percent")
    axes[1, 1].set_title("VM utilization")
    axes[1, 1].legend()
    for axis in axes.flat:
        axis.set_xlabel("AS count / iperf pair count")
        axis.grid(alpha=0.3)
        if knee.get("asCount") is not None:
            axis.axvline(int(knee["asCount"]), color="tab:red", linestyle="--", alpha=0.7)
        for failed in failedScales:
            axis.axvline(
                int(failed["asCount"]), color="black", linestyle=":", alpha=0.6, label="failed scale"
            )
    figure.suptitle("0918E1 single-VM star topology saturation")
    figure.savefig(path, dpi=160)
    plt.close(figure)


def formatNumber(value: Any, digits: int = 2) -> str:
    """Format optional numeric values for the Markdown report."""
    return "NA" if value is None else f"{float(value):.{digits}f}"


def writeReport(
    path: Path,
    rows: list[dict[str, Any]],
    knee: dict[str, Any],
    capacityBoundary: dict[str, Any],
    failedScales: list[dict[str, Any]],
) -> None:
    """Write a concise Chinese interpretation with the counterfactual caveat visible."""
    lines = [
        "# 0918E1 曲线结果",
        "",
        f"性能曲线拐点：**{knee['status']}**；AS 数：{knee.get('asCount') or '未观察到'}；原因：{knee['reason']}。",
        (
            f"完整运行边界：**{capacityBoundary['status']}**；最后完整成功："
            f"{capacityBoundary.get('lastSuccessfulAsCount') or '无'} AS；首个失败："
            f"{capacityBoundary.get('firstFailedAsCount') or '无'} AS。"
        ),
        "",
        "| AS/iperf 对 | 成功对数 | 平均每对 Mbps | 总 Mbps | 平均 RTT ms | p95 RTT ms | CPU 均值 % | 内存均值 % | 估算减少跨机 Mbps |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        lines.append(
            "| {asCount} | {success} | {pair} | {total} | {rtt} | {p95} | {cpu} | {memory} | {avoided} |".format(
                asCount=row["asCount"],
                success=formatNumber(row["successfulPairs"], 0),
                pair=formatNumber(row["averagePairMbps"]),
                total=formatNumber(row["totalMeasuredMbps"]),
                rtt=formatNumber(row["averageRttMs"], 3),
                p95=formatNumber(row["p95RttMs"], 3),
                cpu=formatNumber(row["cpuMeanPercent"]),
                memory=formatNumber(row["memoryMeanPercent"]),
                avoided=formatNumber(row["estimatedAvoidedCrossVmMbps"]),
            )
        )
    lines.extend(
        [
            "",
            "“估算减少跨机 Mbps”取所有成功 iperf 对的实测总吞吐，假设这些流若不共置都会跨机；它不是物理网卡上的直接节省量。",
            "主带宽指标来自 iperf3；guest 接口计数会因 bridge/veth 重复计数，仅作为诊断字段保存在 CSV。",
            f"分布异常判定使用每个规模 {rows[0]['requiredRepeatCount']} 次重复的 Tukey 1.5×IQR 规则；异常重复编号写在下方，不能把它们解释成稳定趋势。",
        ]
    )
    lines.extend(
        [
            "",
            "## 重复分布与异常",
            "",
            "| AS | 预检成功/配置 | 完整重复/要求 | 成功对范围 | 每对带宽标准差 Mbps | RTT 标准差 ms | 带宽异常重复 | RTT 异常重复 |",
            "| ---: | ---: | ---: | ---: | ---: | ---: | :--- | :--- |",
        ]
    )
    for row in rows:
        lines.append(
            "| {asCount} | {preflight}/{configured} | {complete}/{required} | {minimum}–{maximum} | {bandwidthStddev} | {rttStddev} | {bandwidthOutliers} | {rttOutliers} |".format(
                asCount=row["asCount"],
                preflight=formatNumber(row["preflightSuccessCount"], 0),
                configured=row["configuredPairs"],
                complete=formatNumber(row["completedRepeatCount"], 0),
                required=formatNumber(row["requiredRepeatCount"], 0),
                minimum=formatNumber(row["iperfSuccessMin"], 0),
                maximum=formatNumber(row["iperfSuccessMax"], 0),
                bandwidthStddev=formatNumber(row["pairBandwidthStddevMbps"]),
                rttStddev=formatNumber(row["rttStddevMs"], 4),
                bandwidthOutliers=row["pairBandwidthOutlierRepeats"] or "无",
                rttOutliers=row["rttOutlierRepeats"] or "无",
            )
        )
    degraded = [row for row in rows if row.get("trafficQualityValid") is False]
    if degraded:
        lines.extend(
            [
                "",
                f"以下规模没有 {rows[0]['requiredRepeatCount']} 次全部成功的完整重复，应优先分析原始 iperf JSON，而不是只看中位数：",
            ]
        )
        for row in degraded:
            lines.append(
                f"- {row['asCount']} AS：成功对范围 {row['iperfSuccessMin']}–{row['iperfSuccessMax']}，"
                f"完整重复 {row['completedRepeatCount']}/{row['requiredRepeatCount']}。"
            )
    if failedScales:
        lines.extend(["", "## 未形成有效测量的规模", ""])
        for failed in failedScales:
            detail = failed.get("failureDetail") or {}
            reason = (
                detail.get("error")
                or detail.get("reason")
                or failed.get("downloadError")
                or "runner failed"
            )
            lines.append(f"- {failed['asCount']} AS：{reason}")
        lines.append("失败规模不参与带宽/时延曲线和自动拐点计算，避免把缺失值当作性能值。")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    """Aggregate artifacts and emit machine-readable plus visual reports."""
    args = parseArgs()
    runDir = findRunDir(args.run_dir)
    rows, config = loadRows(runDir)
    failedScales = loadFailedScales(runDir)
    knee = detectKnee(rows, config["analysis"])
    capacityBoundary = detectCapacityBoundary(rows, failedScales)
    summary = {
        "schemaVersion": 1,
        "runDir": str(runDir),
        "scaleCount": len(rows),
        "baselineAsCount": rows[0]["asCount"],
        "knee": knee,
        "capacityBoundary": capacityBoundary,
        "failedScales": failedScales,
        "rows": rows,
    }
    writeCsv(runDir / "scale_metrics.csv", rows)
    (runDir / "curve_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    writeReport(runDir / "curve_report.md", rows, knee, capacityBoundary, failedScales)
    drawCurves(runDir / "curve.png", rows, knee, failedScales)
    print(
        json.dumps(
            {
                "status": "PASS",
                "runDir": str(runDir),
                "knee": knee,
                "capacityBoundary": capacityBoundary,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
