#!/usr/bin/env python3
"""Fit a frozen two-feature B63 performance model from declared E1 loads."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from performanceStudy import extractRows, performancePenalty


def solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    """Solve a small dense system with pivoted Gaussian elimination."""
    augmented = [row[:] + [value] for row, value in zip(matrix, vector)]
    for column in range(len(vector)):
        pivot = max(range(column, len(vector)), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            raise ValueError("training design is singular; use distinct placement/load points")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        scale = augmented[column][column]
        augmented[column] = [value / scale for value in augmented[column]]
        for row in range(len(vector)):
            if row == column:
                continue
            scale = augmented[row][column]
            augmented[row] = [left - scale * right for left, right in zip(augmented[row], augmented[column])]
    return [augmented[index][-1] for index in range(len(vector))]


def fit(samples: list[dict[str, Any]], ridge: float) -> dict[str, Any]:
    x = [[1.0, float(row["communicationPairRatio"]), float(row["contentionFeature"])] for row in samples]
    y = [float(row["performancePenalty"]) for row in samples]
    xtx = [[sum(row[i] * row[j] for row in x) for j in range(3)] for i in range(3)]
    xty = [sum(row[i] * value for row, value in zip(x, y)) for i in range(3)]
    xtx[1][1] += ridge
    xtx[2][2] += ridge
    coefficients = solve(xtx, xty)
    predictions = [sum(a * b for a, b in zip(coefficients, row)) for row in x]
    mean = sum(y) / len(y)
    residual = sum((actual - predicted) ** 2 for actual, predicted in zip(y, predictions))
    total = sum((actual - mean) ** 2 for actual in y)
    return {"intercept": coefficients[0], "communicationCost": coefficients[1],
            "contentionCost": coefficients[2], "rSquared": 1.0 - residual / total if total else None,
            "trainingPredictions": predictions}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ridge", type=float, default=1e-8)
    args = parser.parse_args(argv)
    root = args.run_dir.expanduser().resolve()
    rows, progress = extractRows(root)
    protocol = progress.get("protocol") or {}
    declared = {float(value) for value in protocol.get("calibrationLoadsMillicores", [])}
    if not declared:
        parser.error("study protocol does not declare calibrationLoadsMillicores")
    samples = []
    for row in rows:
        penalty = performancePenalty(row)
        if row.get("loadMillicoresPerPod") in declared and penalty is not None:
            samples.append(dict(row, performancePenalty=penalty))
    if len(samples) < 4:
        parser.error(f"need at least four complete declared calibration samples, found {len(samples)}")
    training_projection = [{key: row.get(key) for key in (
        "variant", "algorithm", "loadMillicoresPerPod", "communicationPairRatio",
        "contentionFeature", "performancePenalty", "sampleSignature")}
        for row in samples]
    training_hash = hashlib.sha256(json.dumps(training_projection, sort_keys=True).encode()).hexdigest()
    fitted = fit(samples, args.ridge)
    model = {
        "schemaVersion": 1,
        "modelType": "ridge-linear-availability-adjusted-rtt-goodput-penalty",
        "response": "log(stress RTT)-log(stress aggregate goodput)-2*log(max(1-failureRate,1e-6))",
        "features": ["communicationPairRatio", "contentionFeature"],
        "coefficients": {"communicationCost": fitted["communicationCost"],
                         "contentionCost": fitted["contentionCost"]},
        "intercept": fitted["intercept"], "contentionKnee": float(protocol.get("contentionKnee", 0.70)),
        "ridge": args.ridge, "trainingRSquared": fitted["rSquared"],
        "trainingDataHash": training_hash,
        "trainingTopologySize": (progress.get("protocol") or {}).get("fixedTopologySize"),
        "trainingSourceConfigSha256": (progress.get("protocol") or {}).get("sourceConfigSha256"),
        "trainingLoadsMillicores": sorted(declared),
        "excludedHoldoutLoadsMillicores": protocol.get("holdoutLoadsMillicores", []),
        "trainingSamples": training_projection,
        "negativeCoefficientsPreserved": True,
    }
    output = args.output.expanduser().resolve() if args.output else root / "performance_model.json"
    output.write_text(json.dumps(model, indent=2) + "\n")
    print(f"wrote {output}; samples={len(samples)} R2={fitted['rSquared']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
