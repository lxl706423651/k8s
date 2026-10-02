#!/usr/bin/env python3
"""Create tables, hypothesis checks, and fixed-format plots for a B63 study."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

from performanceStudy import extractRows, performancePenalty, readJson


def fileSha256(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def writeCsv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def detectTurningPoints(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for load in sorted({row["loadMillicoresPerPod"] for row in rows}):
        group = [row for row in rows if row["loadMillicoresPerPod"] == load
                 and row.get("communicationPairRatio") is not None and performancePenalty(row) is not None]
        group.sort(key=lambda row: float(row["communicationPairRatio"]))
        penalties = [performancePenalty(row) for row in group]
        best = min(range(len(group)), key=lambda index: penalties[index]) if group else None
        interior = best is not None and 0 < best < len(group) - 1
        result.append({"loadMillicoresPerPod": load, "pointCount": len(group),
                       "bestVariant": group[best]["variant"] if best is not None else None,
                       "bestCommunicationPairRatio": group[best]["communicationPairRatio"] if best is not None else None,
                       "interiorTurningPointObserved": interior})
    return result


def runOutcomes(progress: dict[str, Any]) -> list[dict[str, Any]]:
    """Preserve workflow failures and missing load points beside numeric rows."""
    configured = [float(value) for value in
                  ((progress.get("protocol") or {}).get("loadSweepCpuMillicores") or [])]
    outcomes = []
    for entry in progress.get("runs") or []:
        run = Path(str(entry.get("runDir") or ""))
        sweep = readJson(run / "load_sweep/summary.json")
        if not sweep:
            sweep = readJson(run / "load_sweep/summary.partial.json")
        attempted: list[float] = []
        measured: list[float] = []
        evidence: list[str] = []
        for level in sweep.get("levels") or []:
            load = float(level.get("cpuMillicoresPerPod") or 0)
            attempted.append(load)
            if level.get("metrics"):
                measured.append(load)
            if level.get("loadExitCode") not in {None, 0}:
                evidence.append(f"load {load:g}m application exit={level['loadExitCode']}")
            for gate, value in (level.get("routingGates") or {}).items():
                if value.get("exitCode") not in {None, 0}:
                    evidence.append(f"load {load:g}m {gate} gate exit={value['exitCode']}")
            if level.get("dataplaneExitCode") not in {None, 0}:
                evidence.append(f"load {load:g}m dataplane exit={level['dataplaneExitCode']}")
        summary = readJson(run / "summary.json")
        failed_stages = [str(item.get("name")) for item in summary.get("stages") or []
                         if item.get("status") == "FAIL"]
        if failed_stages:
            evidence.append("failed stages: " + ", ".join(failed_stages))
        if entry.get("status") == "FAIL" and not evidence:
            evidence.append(
                f"run exit={entry.get('exitCode')}; inspect console.log and summary.json")
        outcomes.append({
            "variant": entry.get("id"), "algorithm": entry.get("algorithm"),
            "runStatus": entry.get("status"), "cleanupStatus": entry.get("cleanupStatus"),
            "exitCode": entry.get("exitCode"), "sweepStatus": sweep.get("status"),
            "attemptedLoadsMillicores": attempted, "measuredLoadsMillicores": measured,
            "missingConfiguredLoadsMillicores": [value for value in configured if value not in measured],
            "failureEvidence": evidence,
        })
    return outcomes


def makePlots(root: Path, rows: list[dict[str, Any]]) -> list[str]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return []
    complete = [row for row in rows if row.get("communicationPairRatio") is not None
                and row.get("stressRttMs") is not None and row.get("stressGoodputMbps") is not None]
    if not complete:
        return []
    artifacts = []
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.8))
    for load in sorted({row["loadMillicoresPerPod"] for row in complete}):
        group = sorted((row for row in complete if row["loadMillicoresPerPod"] == load),
                       key=lambda row: float(row["communicationPairRatio"]))
        x = [row["communicationPairRatio"] for row in group]
        axes[0].plot(x, [row["stressRttMs"] for row in group], "o-", label=f"{load:g}m/Pod")
        axes[1].plot(x, [row["stressGoodputMbps"] for row in group], "o-", label=f"{load:g}m/Pod")
        for axis, key in ((axes[0], "stressRttMs"), (axes[1], "stressGoodputMbps")):
            for row in group:
                axis.annotate(str(row["variant"]), (row["communicationPairRatio"], row[key]), fontsize=7)
    axes[0].set(xlabel="Cross-host weighted pair ratio", ylabel="Loaded RTT (ms)")
    axes[1].set(xlabel="Cross-host weighted pair ratio", ylabel="Stress aggregate goodput (Mbps)")
    for axis in axes:
        axis.grid(True, alpha=.25)
        axis.legend(fontsize=8)
    fig.tight_layout()
    for suffix in ("png", "svg"):
        path = root / f"locality_contention_curve.{suffix}"
        fig.savefig(path, dpi=180)
        artifacts.append(str(path))
    plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(12, 4.8))
    for variant in sorted({str(row["variant"]) for row in complete}):
        group = sorted((row for row in complete if str(row["variant"]) == variant),
                       key=lambda row: float(row["loadMillicoresPerPod"]))
        axes[0].plot([row["loadMillicoresPerPod"] for row in group], [row["stressRttMs"] for row in group], "o-", label=variant)
        axes[1].plot([row["loadMillicoresPerPod"] for row in group], [row["stressGoodputMbps"] for row in group], "o-", label=variant)
    axes[0].set(xlabel="External CPU load (millicores/Pod)", ylabel="Loaded RTT (ms)")
    axes[1].set(xlabel="External CPU load (millicores/Pod)", ylabel="Stress aggregate goodput (Mbps)")
    for axis in axes:
        axis.grid(True, alpha=.25)
        axis.legend(fontsize=7)
    fig.tight_layout()
    for suffix in ("png", "svg"):
        path = root / f"load_response_curve.{suffix}"
        fig.savefig(path, dpi=180)
        artifacts.append(str(path))
    plt.close(fig)
    return artifacts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    root = args.run_dir.expanduser().resolve()
    rows, progress = extractRows(root)
    source_config = {}
    try:
        import yaml
        source_config = yaml.safe_load((root / "source_input.yaml").read_text()) or {}
    except (OSError, ValueError):
        source_config = {}
    configured_model = str(((source_config.get("placement") or {}).get("performanceModel") or "")).strip()
    model_path = Path(configured_model).expanduser() if configured_model else root / "performance_model.json"
    if configured_model and not model_path.is_absolute():
        model_path = ((root / "source_input.yaml").parent / model_path).resolve()
    model = readJson(model_path) if str(model_path) and model_path.is_file() else {}
    for row in rows:
        row["performancePenalty"] = performancePenalty(row)
        coefficients = model.get("coefficients") or {}
        if model and row.get("communicationPairRatio") is not None and row.get("contentionFeature") is not None:
            row["predictedPerformancePenalty"] = (
                float(model.get("intercept") or 0)
                + float(coefficients.get("communicationCost") or 0) * float(row["communicationPairRatio"])
                + float(coefficients.get("contentionCost") or 0) * float(row["contentionFeature"])
            )
            row["predictionAbsoluteError"] = (abs(row["performancePenalty"] - row["predictedPerformancePenalty"])
                                                if row["performancePenalty"] is not None else None)
    signatures = {row.get("sampleSignature") for row in rows if row.get("sampleSignature")}
    sample_status = "MATCH" if len(signatures) == 1 and rows else "INCOMPLETE" if not signatures else "MISMATCH"
    turning = detectTurningPoints(rows)
    outcomes = runOutcomes(progress)
    plots = makePlots(root, rows)
    validation = []
    for load in sorted({row["loadMillicoresPerPod"] for row in rows}):
        group = [row for row in rows if row["loadMillicoresPerPod"] == load and row["performancePenalty"] is not None]
        proposed = next((row for row in group if row["algorithm"] == "performance-model"), None)
        baselines = [row for row in group if row["algorithm"] in {"crescent", "crescent-resource-aware"}]
        validation.append({
            "loadMillicoresPerPod": load,
            "proposedVariant": proposed.get("variant") if proposed else None,
            "proposedMeasuredPenalty": proposed.get("performancePenalty") if proposed else None,
            "proposedPredictedPenalty": proposed.get("predictedPerformancePenalty") if proposed else None,
            "baselineMeasuredPenalties": {row["variant"]: row["performancePenalty"] for row in baselines},
            "proposedBetterThanEveryBaseline": (bool(proposed and baselines) and all(
                proposed["performancePenalty"] < row["performancePenalty"] for row in baselines)),
        })
    result = {"schemaVersion": 1, "studyStatus": progress.get("status"),
              "postprocessing": {
                  "python": __import__("sys").executable,
                  "analyzerSha256": fileSha256(Path(__file__)),
                  "extractorSha256": fileSha256(Path(__file__).with_name("performanceStudy.py")),
              },
              "sampleComparison": sample_status, "turningPoints": turning,
              "runOutcomes": outcomes,
              "hypothesisObservedAtAnyLoad": any(item["interiorTurningPointObserved"] for item in turning),
              "independentValidation": validation,
              "model": ({"path": str(model_path), "trainingDataHash": model.get("trainingDataHash"),
                         "trainingTopologySize": model.get("trainingTopologySize"),
                         "evaluationTopologySize": (progress.get("protocol") or {}).get("fixedTopologySize"),
                         "topologyHeldOut": model.get("trainingTopologySize") != (progress.get("protocol") or {}).get("fixedTopologySize")}
                        if model else None),
              "codeCorrectnessSeparateFromHypothesis": True, "rows": rows, "plots": plots}
    (root / "study_analysis.json").write_text(json.dumps(result, indent=2) + "\n")
    writeCsv(root / "study_metrics.csv", rows)
    lines = ["# B63 性能放置实验", "", f"批次状态：{progress.get('status')}；采样方向：{sample_status}。", "",
             "| 变体 | 算法 | 负载 m/Pod | 跨机加权边比例 | 争用特征 | RTT ms | stress 吞吐 Mbps | 失败率 | 性能惩罚 | 预测惩罚 |",
             "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    def fmt(value: Any) -> str:
        return "—" if value is None else f"{value:.5g}" if isinstance(value, float) else str(value)
    for row in rows:
        lines.append(f"| {row['variant']} | {row['algorithm']} | {fmt(row['loadMillicoresPerPod'])} | "
                     f"{fmt(row['communicationPairRatio'])} | {fmt(row['contentionFeature'])} | "
                     f"{fmt(row['stressRttMs'])} | {fmt(row['stressGoodputMbps'])} | "
                     f"{fmt(row['stressFailureRate'])} | {fmt(row['performancePenalty'])} | "
                     f"{fmt(row.get('predictedPerformancePenalty'))} |")
    lines += ["", "## 流程结果与缺测负载", "",
              "失败和缺测是实验结果；数值表不会用插值或零值补齐。", "",
              "| 变体 | 运行 | 清理 | 已实测负载 m/Pod | 缺测负载 m/Pod | 失败证据 |",
              "|---|---|---|---|---|---|"]
    for item in outcomes:
        measured = ", ".join(f"{value:g}" for value in item["measuredLoadsMillicores"]) or "—"
        missing = ", ".join(f"{value:g}" for value in item["missingConfiguredLoadsMillicores"]) or "—"
        evidence = "; ".join(item["failureEvidence"]) or "—"
        lines.append(f"| {item['variant']} | {item['runStatus']} | {item['cleanupStatus']} | "
                     f"{measured} | {missing} | {evidence} |")
    lines += ["", "## 机械判定的内部拐点", "",
              "内部拐点定义为：按跨机加权边比例排序后，性能惩罚最小值位于两个端点之间。没有观察到时照实报告。", "",
              "| 负载 m/Pod | 点数 | 最优变体 | 跨机加权边比例 | 观察到内部拐点 |", "|---:|---:|---|---:|---|"]
    for item in turning:
        lines.append(f"| {fmt(item['loadMillicoresPerPod'])} | {item['pointCount']} | {fmt(item['bestVariant'])} | "
                     f"{fmt(item['bestCommunicationPairRatio'])} | {item['interiorTurningPointObserved']} |")
    if any(item["proposedVariant"] for item in validation):
        lines += ["", "## 独立验证", "",
                  "| 负载 m/Pod | 本文方法实测惩罚 | 本文方法预测惩罚 | 两个基线都更差 |", "|---:|---:|---:|---|"]
        for item in validation:
            if item["proposedVariant"]:
                lines.append(f"| {fmt(item['loadMillicoresPerPod'])} | {fmt(item['proposedMeasuredPenalty'])} | "
                             f"{fmt(item['proposedPredictedPenalty'])} | {item['proposedBetterThanEveryBaseline']} |")
    lines += ["", "PASS 只表示实验流程和预设检查完成，不表示研究假设成立。"]
    (root / "study_report.md").write_text("\n".join(lines) + "\n")
    print(f"wrote {root / 'study_report.md'}; samples={sample_status}; plots={len(plots)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
