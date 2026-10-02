#!/usr/bin/env python3
"""Summarize heterogeneous VM E1-A calibration runs.

This is an offline reducer.  It reads the artifacts written by
``runVmProfileCalibration.py``/``executeE1aExperiment.py`` and emits profile
curves, observed transition brackets, and a conservative profile override
table for the marginal benefit model.  It never changes VM or Kubernetes state.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from pathlib import Path
from typing import Any

import yaml


HERE = Path(__file__).resolve().parent


def finite(value: Any, default: float = 0.0) -> float:
    """Return a finite float."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def read_json(path: Path) -> Any:
    """Read JSON."""
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    """Write JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_csv(path: Path) -> list[dict[str, str]]:
    """Read CSV rows."""
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def linear_slope(points: list[tuple[float, float]]) -> float | None:
    """Fit a non-intercept slope after centering x and y."""
    if len(points) < 2:
        return None
    x_mean = statistics.fmean(item[0] for item in points)
    y_mean = statistics.fmean(item[1] for item in points)
    denominator = sum((x - x_mean) ** 2 for x, _ in points)
    return sum((x - x_mean) * (y - y_mean) for x, y in points) / denominator if denominator else None


def raw_point_index(run_dir: Path) -> dict[tuple[str, int, str], list[dict[str, Any]]]:
    """Index raw point summaries by phase/N/group."""
    result: dict[tuple[str, int, str], list[dict[str, Any]]] = {}
    for path in sorted((run_dir / "raw").glob("**/point_summary.json")):
        item = read_json(path)
        key = (str(item.get("phase")), int(item.get("N", 0)), str(item.get("group")))
        result.setdefault(key, []).append(item)
    return result


def point_memory_available(index: dict[tuple[str, int, str], list[dict[str, Any]]], n: int) -> float | None:
    """Return the median minimum available memory for loaded N."""
    values: list[float] = []
    for item in index.get(("pilot", n, "loaded"), []):
        value = item.get("nodeMetrics", {}).get("memoryAvailableMinKiB")
        if value is not None:
            values.append(finite(value, math.nan))
    values = [value for value in values if math.isfinite(value)]
    return statistics.median(values) if values else None


def penalty(row: dict[str, str]) -> float:
    """Form a transparent service penalty from available E1-A outputs."""
    terms: list[float] = []
    for field in ("canaryOnTimeMedian", "backgroundCompletionRateMedian"):
        value = row.get(field)
        if value not in (None, ""):
            terms.append(max(0.0, 1.0 - finite(value, 1.0)))
    p99 = finite(row.get("canaryP99MedianMs"), math.nan)
    baseline = finite(row.get("_baselineP99Ms"), math.nan)
    if math.isfinite(p99) and math.isfinite(baseline) and baseline > 0:
        terms.append(max(0.0, p99 / baseline - 1.0))
    return statistics.fmean(terms) if terms else 0.0


def summarize_profile(profile_dir: Path) -> dict[str, Any]:
    """Reduce one generated profile run."""
    run_dir = profile_dir / "run"
    config_path = profile_dir / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    profile = config.get("_profile", {})
    worker_vcpus = int(config.get("workers", {}).get("vcpus", profile.get("vcpus", 0)))
    worker_memory = int(config.get("workers", {}).get("memoryGiB", profile.get("memoryGiB", 0)))
    summary_path = run_dir / "metrics/e1a_summary.csv"
    knee_path = run_dir / "metrics/knee.json"
    if not summary_path.exists():
        return {
            "profile": profile_dir.name,
            "vcpus": worker_vcpus,
            "memoryGiB": worker_memory,
            "status": "missing_summary",
        }
    rows = read_csv(summary_path)
    loaded = [row for row in rows if row.get("phase") == "pilot" and row.get("group") == "loaded"]
    loaded.sort(key=lambda row: int(row.get("N", 0)))
    baseline_row = next((row for row in loaded if int(row.get("N", 0)) == 0), None)
    baseline_p99 = finite(baseline_row.get("canaryP99MedianMs"), math.nan) if baseline_row else math.nan
    for row in loaded:
        row["_baselineP99Ms"] = baseline_p99
        row["_penalty"] = penalty(row)
    index = raw_point_index(run_dir)
    baseline_available = point_memory_available(index, 0)
    curve: list[dict[str, Any]] = []
    for row in loaded:
        n = int(row.get("N", 0))
        available = point_memory_available(index, n)
        capacity_kib = worker_memory * 1024 * 1024
        memory_used_fraction = 1.0 - available / capacity_kib if available is not None and capacity_kib else None
        memory_delta_fraction = (
            (baseline_available - available) / capacity_kib
            if available is not None and baseline_available is not None and capacity_kib
            else None
        )
        curve.append(
            {
                "N": n,
                "status": row.get("workloadOverloadCount", 0),
                "cpuBusy": finite(row.get("cpuBusyMedian"), math.nan),
                "cpuPsiSomeAvg10": finite(row.get("cpuPsiSomeAvg10Median"), math.nan),
                "runQueue": finite(row.get("runQueueMedian"), math.nan),
                "canaryOnTime": finite(row.get("canaryOnTimeMedian"), math.nan),
                "backgroundCompletion": finite(row.get("backgroundCompletionRateMedian"), math.nan),
                "p99Ms": finite(row.get("canaryP99MedianMs"), math.nan),
                "penalty": finite(row.get("_penalty"), 0.0),
                "memoryAvailableMinKiB": available,
                "memoryUsedFraction": memory_used_fraction,
                "memoryDeltaFraction": memory_delta_fraction,
            }
        )
    knee = read_json(knee_path) if knee_path.exists() else {}
    last_success = knee.get("lastStableSuccessN")
    first_overload = knee.get("firstStableOverloadN") or knee.get("firstWorkloadOverloadN")
    baseline_valid = bool(
        baseline_row
        and finite(baseline_row.get("workloadOverloadCount"), 0.0) == 0.0
        and finite(baseline_row.get("canaryOnTimeMedian"), 1.0) >= 0.99
    )
    calibration_usable = baseline_valid and (first_overload is None or int(first_overload) > 0)
    first_overload_row = next((item for item in curve if item["N"] == first_overload), None)
    stable_rows = [item for item in curve if last_success is not None and item["N"] <= last_success]
    overload_rows = [
        item for item in curve
        if first_overload is not None and item["N"] >= first_overload
        and math.isfinite(item["cpuBusy"]) and math.isfinite(item["penalty"])
    ]
    cpu_slope = linear_slope([
        (max(0.0, item["cpuBusy"] - (first_overload_row or {}).get("cpuBusy", 0.0)), item["penalty"])
        for item in overload_rows
    ])
    memory_points = [
        (max(0.0, finite(item.get("memoryDeltaFraction"), 0.0)), item["penalty"])
        for item in curve
        if item.get("memoryDeltaFraction") is not None
    ]
    memory_slope = linear_slope(memory_points)
    right_censored = first_overload is None
    recommended_cpu_knee = (
        finite(first_overload_row.get("cpuBusy"), 0.70)
        if first_overload_row and not right_censored
        else 0.70
    )
    recommended_memory_knee = (
        finite(first_overload_row.get("memoryUsedFraction"), 0.70)
        if first_overload_row and first_overload_row.get("memoryUsedFraction") is not None and not right_censored
        else 0.70
    )
    return {
        "profile": profile_dir.name,
        "vcpus": worker_vcpus,
        "memoryGiB": worker_memory,
        "status": "ok",
        "calibrationUsable": calibration_usable,
        "baselineValid": baseline_valid,
        "lastStableSuccessN": last_success,
        "firstWorkloadOverloadN": knee.get("firstWorkloadOverloadN"),
        "firstStableOverloadN": knee.get("firstStableOverloadN"),
        "operationalTransitionBracket": knee.get("operationalTransitionBracket"),
        "repeatabilityTransitionBracket": knee.get("repeatabilityTransitionBracket"),
        "firstOverloadCpuBusy": first_overload_row.get("cpuBusy") if first_overload_row else None,
        "firstOverloadMemoryUsedFraction": (
            first_overload_row.get("memoryUsedFraction") if first_overload_row else None
        ),
        "cpuPenaltySlopeAboveFirstOverload": cpu_slope,
        "memoryPenaltySlope": memory_slope,
        "baselineMemoryAvailableMinKiB": baseline_available,
        "curve": curve,
        "recommendedNodeProfile": {
            "cpuCapacityCores": float(worker_vcpus),
            "memoryCapacityMiB": float(worker_memory * 1024),
            "cpuKnee": recommended_cpu_knee,
            "memoryKnee": recommended_memory_knee,
            "cpuSensitivity": max(0.0, cpu_slope or 1.0) if not right_censored else 1.0,
            "memorySensitivity": max(0.0, memory_slope or 1.0) if not right_censored else 1.0,
            "networkSensitivity": 1.0,
            "calibrationWarning": (
                (
                    "pilot baseline invalid; do not use this profile for coefficient fitting"
                    if not calibration_usable
                    else (
                        "pilot is right-censored at the largest tested N; coefficients are provisional"
                        if first_overload is None
                        else "pilot-only profile; coefficients are provisional and need formal repeats"
                    )
                )
            ),
        },
    }


def render_figure(output_dir: Path, profiles: list[dict[str, Any]]) -> str | None:
    """Render profile knee curves when matplotlib is available."""
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        return None
    usable = [profile for profile in profiles if profile.get("status") == "ok"]
    if not usable:
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    fig, axes = plt.subplots(1, 3, figsize=(16, 4.8), constrained_layout=True)
    for profile in usable:
        label = f"{profile['profile']} ({profile['vcpus']}c/{profile['memoryGiB']}G)"
        curve = profile["curve"]
        x = [item["N"] for item in curve]
        axes[0].plot(x, [item["cpuBusy"] for item in curve], "o-", label=label)
        axes[1].plot(x, [item["penalty"] for item in curve], "o-", label=label)
        axes[2].plot(x, [item["runQueue"] for item in curve], "o-", label=label)
        bracket = profile.get("repeatabilityTransitionBracket") or profile.get("operationalTransitionBracket")
        if bracket:
            axes[1].axvspan(bracket[0], bracket[1], alpha=0.08)
    axes[0].set(title="CPU busy", xlabel="loaded Pods N", ylabel="fraction")
    axes[1].set(title="Service penalty", xlabel="loaded Pods N", ylabel="relative penalty")
    axes[2].set(title="Run queue", xlabel="loaded Pods N", ylabel="runnable tasks")
    for axis in axes:
        axis.grid(alpha=0.25)
        axis.legend(fontsize=8)
    path = output_dir / "vm_profile_calibration_curves.png"
    fig.savefig(path, dpi=180)
    plt.close(fig)
    return str(path)


def main(arguments: argparse.Namespace) -> int:
    """Analyze all profile directories below one calibration root."""
    roots = [path.resolve() for path in arguments.input_dir]
    profile_dirs = [
        path
        for root in roots
        for path in sorted(root.iterdir())
        if (path / "config.yaml").exists()
    ]
    profiles = [summarize_profile(path) for path in profile_dirs]
    output_dir = arguments.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    figure = render_figure(output_dir, profiles)
    write_json(output_dir / "vm_profile_calibration.json", {"profiles": profiles, "figure": figure})
    overrides = {
        profile["profile"]: profile["recommendedNodeProfile"]
        for profile in profiles
        if profile.get("status") == "ok"
        and profile.get("calibrationUsable")
        and profile.get("recommendedNodeProfile")
    }
    write_json(output_dir / "node_profile_overrides.json", overrides)
    rows = []
    for profile in profiles:
        rows.append({key: value for key, value in profile.items() if key != "curve" and key != "recommendedNodeProfile"})
    if rows:
        fields = sorted({key for row in rows for key in row})
        with (output_dir / "vm_profile_calibration.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
    report = [
        "# VM profile 标定结果",
        "",
        "这些结果来自同一 E1-A workload 在不同 worker VM 规格上的 pilot 扫描，"
        "用于估计 profile-specific knee 和争用敏感度。pilot 结果不能替代正式重复实验。",
        "",
        "| profile | VM | 稳定正常上界 | 首次过载 | 过载前后区间 | 标定口径 |",
        "|---|---|---:|---:|---|---|",
    ]
    for profile in profiles:
        report.append(
            f"| {profile.get('profile')} | {profile.get('vcpus')} vCPU/{profile.get('memoryGiB')} GiB | "
            f"{profile.get('lastStableSuccessN')} | {profile.get('firstWorkloadOverloadN')} | "
            f"{profile.get('repeatabilityTransitionBracket') or profile.get('operationalTransitionBracket')} | "
            f"{'可用于初步拟合' if profile.get('calibrationUsable') else '基线无效/仅记录'} |"
        )
    report.extend(
        [
            "",
            "模型使用 `node_profile_overrides.json` 中的容量和 provisional sensitivity；"
            "正式 E2 应在每个 profile 至少做 3 次重复，并把未用于拟合的 profile/拓扑留作 E3。",
        ]
    )
    (output_dir / "report_zh.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(json.dumps({
        "outputDir": str(output_dir),
        "profileCount": len(profiles),
        "figure": figure,
        "overrides": str(output_dir / "node_profile_overrides.json"),
    }, indent=2, ensure_ascii=False))
    return 0


def parse_arguments() -> argparse.Namespace:
    """Parse CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-dir",
        type=Path,
        nargs="+",
        default=[HERE / "experiments/e1_marginal_pods/20260920_vm_profile_calibration_pilot"],
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main(parse_arguments()))
