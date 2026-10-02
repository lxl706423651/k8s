#!/usr/bin/env python3
"""Estimate placement benefit and resource boundaries from 0918E1 run outputs.

Inputs: one or more profile-labelled run directories and an optional AS weight CSV.
Outputs: benefit_model.csv, benefit_summary.json, and benefit_report.md under an output directory.
Side effects: writes only derived analysis artifacts; never changes VMs or experiment results.
Context: run after profile sweeps to compare avoided cross-VM traffic against local VM degradation.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
FIRST_ASN = 64512


def parseArgs() -> argparse.Namespace:
    """Parse labelled run directories and model thresholds."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run",
        action="append",
        required=True,
        help="profile=results/run-id; may be repeated",
    )
    parser.add_argument("--weights", type=Path, help="CSV with asn,weight,demandMbps columns")
    parser.add_argument("--default-demand-mbps", type=float)
    parser.add_argument("--out-dir", type=Path, default=SCRIPT_DIR / "results" / "benefit-model")
    parser.add_argument("--min-average-retention", type=float, default=0.90)
    parser.add_argument("--max-loss-percent", type=float, default=10.0)
    parser.add_argument("--max-cpu-percent", type=float, default=85.0)
    parser.add_argument("--max-memory-percent", type=float, default=80.0)
    parser.add_argument("--latency-penalty-mbps-per-ms", type=float, default=0.0)
    parser.add_argument("--loss-penalty-mbps-per-percent", type=float, default=0.0)
    parser.add_argument("--cpu-penalty-mbps-per-percent", type=float, default=0.0)
    parser.add_argument("--memory-penalty-mbps-per-percent", type=float, default=0.0)
    return parser.parse_args()


def parseRunSpecs(specs: list[str]) -> list[tuple[str, Path]]:
    """Return profile label and run directory pairs."""
    runs: list[tuple[str, Path]] = []
    for spec in specs:
        if "=" not in spec:
            raise ValueError("--run must be profile=path")
        label, rawPath = spec.split("=", 1)
        path = Path(rawPath).expanduser()
        if not path.is_absolute():
            path = (SCRIPT_DIR / path).resolve()
        if not (path / "scale_metrics.csv").is_file():
            raise FileNotFoundError(f"missing scale_metrics.csv: {path}")
        runs.append((label, path))
    return runs


def optionalFloat(value: str | None) -> float | None:
    """Parse an optional CSV numeric cell."""
    if value is None or value == "":
        return None
    return float(value)


def loadRunRows(profile: str, runDir: Path) -> list[dict[str, Any]]:
    """Load one analyzer CSV and attach profile/run metadata."""
    rows: list[dict[str, Any]] = []
    with (runDir / "scale_metrics.csv").open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            parsed: dict[str, Any] = {
                "profile": profile,
                "runDir": str(runDir),
                "asCount": int(row["asCount"]),
                "status": row.get("status"),
                "configuredPairs": int(row["configuredPairs"]),
                "preflightStatus": row.get("preflightStatus"),
                "preflightSuccessCount": optionalFloat(row.get("preflightSuccessCount")),
                "requiredRepeatCount": optionalFloat(row.get("requiredRepeatCount")),
                "completedRepeatCount": optionalFloat(row.get("completedRepeatCount")),
                "trafficQualityValid": row.get("trafficQualityValid") == "True",
            }
            for key in (
                "averagePairMbps",
                "totalMeasuredMbps",
                "averageRttMs",
                "p95RttMs",
                "packetLossPercent",
                "cpuMeanPercent",
                "cpuP95Percent",
                "cpuStealMeanPercent",
                "cpuPressureAvg10Mean",
                "memoryMeanPercent",
                "memoryAvailableMeanGiB",
                "memoryPressureAvg10Mean",
                "swapMeanGiB",
                "guestInterfaceAggregateMbps",
            ):
                parsed[key] = optionalFloat(row.get(key))
            rows.append(parsed)
    return sorted(rows, key=lambda item: item["asCount"])


def loadWeights(path: Path | None) -> dict[int, dict[str, float]]:
    """Load optional AS weights and demand values keyed by ASN."""
    if path is None:
        return {}
    weights: dict[int, dict[str, float]] = {}
    with path.expanduser().open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            rawAsn = (row.get("asn") or "").strip()
            if not rawAsn or rawAsn.startswith("#"):
                continue
            asn = int(rawAsn)
            weights[asn] = {
                "weight": float(row.get("weight") or 1.0),
                "demandMbps": float(row["demandMbps"]) if row.get("demandMbps") else 0.0,
            }
    return weights


def inferDefaultDemand(runs: list[list[dict[str, Any]]], explicit: float | None) -> float:
    """Choose a stable default per-AS demand for weighted avoided-traffic estimates."""
    if explicit is not None:
        return explicit
    firstRows = [row for row in runs[0] if row["status"] == "PASS" and row.get("averagePairMbps")]
    if not firstRows:
        raise ValueError("cannot infer default demand: first run has no PASS averagePairMbps")
    return float(firstRows[0]["averagePairMbps"])


def avoidedTraffic(asCount: int, weights: dict[int, dict[str, float]], defaultDemand: float) -> dict[str, float]:
    """Return weighted and unweighted avoided traffic for the first asCount ASNs."""
    weighted = 0.0
    unweighted = 0.0
    weightSum = 0.0
    for index in range(asCount):
        asn = FIRST_ASN + index
        item = weights.get(asn, {})
        weight = float(item.get("weight", 1.0))
        demand = float(item.get("demandMbps") or defaultDemand)
        unweighted += demand
        weighted += weight * demand
        weightSum += weight
    return {
        "avoidedCrossVmMbps": unweighted,
        "weightedAvoidedCrossVmMbps": weighted,
        "weightSum": weightSum,
    }


def isSafe(row: dict[str, Any], baselinePair: float, args: argparse.Namespace) -> bool:
    """Evaluate one scale against conservative safety thresholds."""
    if row["status"] != "PASS" or row.get("preflightStatus") != "PASS":
        return False
    requiredRepeats = row.get("requiredRepeatCount")
    completedRepeats = row.get("completedRepeatCount")
    if (
        requiredRepeats is not None
        and completedRepeats is not None
        and completedRepeats < requiredRepeats
    ):
        return False
    if row.get("trafficQualityValid") is False:
        return False
    averagePair = row.get("averagePairMbps")
    if averagePair is None or baselinePair <= 0:
        return False
    if averagePair / baselinePair < args.min_average_retention:
        return False
    if (row.get("packetLossPercent") or 0.0) > args.max_loss_percent:
        return False
    if (row.get("cpuMeanPercent") or 0.0) > args.max_cpu_percent:
        return False
    if (row.get("memoryMeanPercent") or 0.0) > args.max_memory_percent:
        return False
    return True


def modelRows(
    profile: str,
    rows: list[dict[str, Any]],
    weights: dict[int, dict[str, float]],
    defaultDemand: float,
    args: argparse.Namespace,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Compute cumulative and marginal placement benefit for one profile."""
    passRows = [row for row in rows if row["status"] == "PASS" and row.get("averagePairMbps")]
    baselinePair = float(passRows[0]["averagePairMbps"]) if passRows else 0.0
    output: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    safeScales: list[int] = []
    unsafeScales: list[int] = []
    firstFailure: int | None = None
    for row in rows:
        avoided = avoidedTraffic(int(row["asCount"]), weights, defaultDemand)
        modeled = {**row, **avoided}
        modeled["averagePairRetention"] = (
            None
            if baselinePair <= 0 or row.get("averagePairMbps") is None
            else float(row["averagePairMbps"]) / baselinePair
        )
        if previous is None:
            modeled.update(
                {
                    "deltaAsCount": None,
                    "deltaWeightedAvoidedMbps": None,
                    "deltaTotalMeasuredMbps": None,
                    "deltaAveragePairMbps": None,
                    "deltaAverageRttMs": None,
                    "deltaPacketLossPercent": None,
                    "deltaCpuMeanPercent": None,
                    "deltaMemoryMeanPercent": None,
                    "marginalScoreMbps": None,
                }
            )
        else:
            deltaWeighted = modeled["weightedAvoidedCrossVmMbps"] - previous["weightedAvoidedCrossVmMbps"]
            deltaTotal = (
                None
                if modeled.get("totalMeasuredMbps") is None or previous.get("totalMeasuredMbps") is None
                else modeled["totalMeasuredMbps"] - previous["totalMeasuredMbps"]
            )
            deltaRtt = (
                None
                if modeled.get("averageRttMs") is None or previous.get("averageRttMs") is None
                else modeled["averageRttMs"] - previous["averageRttMs"]
            )
            deltaLoss = (
                None
                if modeled.get("packetLossPercent") is None or previous.get("packetLossPercent") is None
                else modeled["packetLossPercent"] - previous["packetLossPercent"]
            )
            deltaCpu = (
                None
                if modeled.get("cpuMeanPercent") is None or previous.get("cpuMeanPercent") is None
                else modeled["cpuMeanPercent"] - previous["cpuMeanPercent"]
            )
            deltaMemory = (
                None
                if modeled.get("memoryMeanPercent") is None or previous.get("memoryMeanPercent") is None
                else modeled["memoryMeanPercent"] - previous["memoryMeanPercent"]
            )
            score = deltaWeighted
            if deltaTotal is not None:
                score += deltaTotal
            if deltaRtt is not None and deltaRtt > 0:
                score -= deltaRtt * args.latency_penalty_mbps_per_ms
            if deltaLoss is not None and deltaLoss > 0:
                score -= deltaLoss * args.loss_penalty_mbps_per_percent
            if deltaCpu is not None and deltaCpu > 0:
                score -= deltaCpu * args.cpu_penalty_mbps_per_percent
            if deltaMemory is not None and deltaMemory > 0:
                score -= deltaMemory * args.memory_penalty_mbps_per_percent
            modeled.update(
                {
                    "deltaAsCount": modeled["asCount"] - previous["asCount"],
                    "deltaWeightedAvoidedMbps": deltaWeighted,
                    "deltaTotalMeasuredMbps": deltaTotal,
                    "deltaAveragePairMbps": (
                        None
                        if modeled.get("averagePairMbps") is None or previous.get("averagePairMbps") is None
                        else modeled["averagePairMbps"] - previous["averagePairMbps"]
                    ),
                    "deltaAverageRttMs": deltaRtt,
                    "deltaPacketLossPercent": deltaLoss,
                    "deltaCpuMeanPercent": deltaCpu,
                    "deltaMemoryMeanPercent": deltaMemory,
                    # A failed or otherwise incomparable scale still gets
                    # traffic deltas, but must not be presented as a usable
                    # placement score.
                    "marginalScoreMbps": (
                        score
                        if row["status"] == "PASS"
                        and previous.get("status") == "PASS"
                        and deltaTotal is not None
                        else None
                    ),
                }
            )
        if isSafe(row, baselinePair, args):
            safeScales.append(int(row["asCount"]))
        else:
            unsafeScales.append(int(row["asCount"]))
            if firstFailure is None:
                firstFailure = int(row["asCount"])
        output.append(modeled)
        previous = modeled
    contiguousSafeBoundary: int | None = None
    for row in rows:
        if int(row["asCount"]) in unsafeScales:
            break
        contiguousSafeBoundary = int(row["asCount"])
    summary = {
        "profile": profile,
        "baselineAveragePairMbps": baselinePair,
        "unsafeScales": unsafeScales,
        "lastSafeAsCount": max(safeScales) if safeScales else None,
        "contiguousSafeBoundaryAsCount": contiguousSafeBoundary,
        "safeBoundaryAsCount": max(safeScales) if safeScales else None,
        "firstUnsafeAsCount": firstFailure,
        "firstHardFailureAsCount": next(
            (int(row["asCount"]) for row in rows if row["status"] != "PASS"),
            None,
        ),
    }
    return output, summary


def writeCsv(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write model rows with a stable union of keys."""
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def fmt(value: Any, digits: int = 2) -> str:
    """Format optional report numbers."""
    return "NA" if value is None or value == "" else f"{float(value):.{digits}f}"


def writeReport(path: Path, rows: list[dict[str, Any]], summaries: list[dict[str, Any]]) -> None:
    """Write a compact Chinese report for profile comparison."""
    lines = [
        "# 0918E1 边际收益模型",
        "",
        "该报告把减少跨机流量的收益与同一 VM 内吞吐、时延、丢包、CPU 和内存代价放在同一张表里。",
        "默认权重缺失时，每个 AS 使用同一个需求值；提供 `--weights` 后会按 ASN 权重和需求重算。",
        "安全判断按每个规模的实测结果独立进行；若结果非单调，必须同时看首个不安全规模和连续安全边界，不能只看最大安全规模。",
        "",
        "## Profile 边界",
        "",
        "| Profile | 连续安全边界 AS | 最后一个安全 AS | 不安全规模 | 首个硬失败 AS | 基线每对 Mbps |",
        "| --- | ---: | ---: | --- | ---: | ---: |",
    ]
    for summary in summaries:
        lines.append(
            "| {profile} | {contiguous} | {last} | {unsafe} | {hard} | {baseline} |".format(
                profile=summary["profile"],
                contiguous=summary.get("contiguousSafeBoundaryAsCount") or "NA",
                last=summary.get("lastSafeAsCount") or "NA",
                unsafe=",".join(str(value) for value in summary.get("unsafeScales", [])) or "none",
                hard=summary.get("firstHardFailureAsCount") or "NA",
                baseline=fmt(summary.get("baselineAveragePairMbps")),
            )
        )
    lines.extend(
        [
            "",
            "## 边际变化",
            "",
            "| Profile | AS | 加权避免跨机 Mbps | 边际避免 Mbps | 总带宽 Mbps | 边际总带宽 Mbps | 平均每对 Mbps | RTT ms | 丢包 % | CPU % | 内存 % | 分数 |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in rows:
        lines.append(
            "| {profile} | {asCount} | {avoided} | {deltaAvoided} | {total} | {deltaTotal} | {pair} | {rtt} | {loss} | {cpu} | {mem} | {score} |".format(
                profile=row["profile"],
                asCount=row["asCount"],
                avoided=fmt(row.get("weightedAvoidedCrossVmMbps")),
                deltaAvoided=fmt(row.get("deltaWeightedAvoidedMbps")),
                total=fmt(row.get("totalMeasuredMbps")),
                deltaTotal=fmt(row.get("deltaTotalMeasuredMbps")),
                pair=fmt(row.get("averagePairMbps")),
                rtt=fmt(row.get("averageRttMs"), 3),
                loss=fmt(row.get("packetLossPercent")),
                cpu=fmt(row.get("cpuMeanPercent")),
                mem=fmt(row.get("memoryMeanPercent")),
                score=fmt(row.get("marginalScoreMbps")),
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    """Build the profile benefit model artifacts."""
    args = parseArgs()
    runs = parseRunSpecs(args.run)
    loadedRuns = [(profile, loadRunRows(profile, path)) for profile, path in runs]
    defaultDemand = inferDefaultDemand([rows for _, rows in loadedRuns], args.default_demand_mbps)
    weights = loadWeights(args.weights)
    allRows: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    for profile, rows in loadedRuns:
        modeled, summary = modelRows(profile, rows, weights, defaultDemand, args)
        allRows.extend(modeled)
        summaries.append(summary)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    writeCsv(args.out_dir / "benefit_model.csv", allRows)
    (args.out_dir / "benefit_summary.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "defaultDemandMbps": defaultDemand,
                "weightSource": str(args.weights) if args.weights else "uniform",
                "summaries": summaries,
                "rows": allRows,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    writeReport(args.out_dir / "benefit_report.md", allRows, summaries)
    print(json.dumps({"status": "PASS", "outDir": str(args.out_dir)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
