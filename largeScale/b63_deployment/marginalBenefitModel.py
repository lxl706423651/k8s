#!/usr/bin/env python3
"""Estimate the marginal locality benefit and VM contention cost of a placement.

The model is deliberately an offline, auditable component.  It does not change
Kubernetes or libvirt state.  It supports two workflows:

* ``replay-e1b`` converts the frozen 214-Pod E1-B sequence and its measured
  results into a weighted communication/VM-contention data set, fits small
  ridge models for the measured metrics, and renders diagnostics.
* ``evaluate`` scores a user supplied before/after placement using the same
  feature definition and a fitted model.

The fitted model is a predictor, not a scheduler.  A caller may use the
predicted marginal score to rank candidate moves and then apply hard admission
checks separately.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from pathlib import Path
from typing import Any, Iterable


HERE = Path(__file__).resolve().parent
DEFAULT_E1B_PLAN = HERE / "experiments/e1_marginal_pods/e1b_topology214_frozen_20260918/plans"
DEFAULT_E1B_METRICS = (
    HERE
    / "experiments/e1_marginal_pods/20260919_e1b_corrected_pilot4/e1b/formal2/analysis/"
    "formal_point_metrics.csv"
)


def read_json(path: Path) -> Any:
    """Read one JSON file."""
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    """Write stable, human-readable JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def finite_float(value: Any, default: float = 0.0) -> float:
    """Convert a possibly missing value to a finite float."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def normalise_weight_file(path: Path | None) -> dict[str, Any]:
    """Load flow/stratum/ASN importance weights from JSON or CSV.

    JSON forms accepted:

    ``{"default": 1, "flowWeights": {"flow-id": 3}}``
    ``{"flow-id": 3}`` (treated as flow weights)

    CSV forms accepted: a header containing ``flowId``/``flow_id`` and
    ``weight``; optional ``stratum`` or ``asn`` columns are also recognised.
    """
    if path is None:
        return {"default": 1.0, "flowWeights": {}, "stratumWeights": {}, "asnWeights": {}}
    if path.suffix.lower() == ".csv":
        flow: dict[str, float] = {}
        stratum: dict[str, float] = {}
        asn: dict[str, float] = {}
        with path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                weight = finite_float(row.get("weight"), 1.0)
                key = (row.get("flowId") or row.get("flow_id") or "").strip()
                if key:
                    flow[key] = weight
                key = (row.get("stratum") or "").strip()
                if key:
                    stratum[key] = weight
                key = (row.get("asn") or "").strip()
                if key:
                    asn[key] = weight
        return {"default": 1.0, "flowWeights": flow, "stratumWeights": stratum, "asnWeights": asn}
    value = read_json(path)
    if not isinstance(value, dict):
        raise ValueError(f"weight file must contain a JSON object: {path}")
    if any(key in value for key in ("default", "flowWeights", "stratumWeights", "asnWeights")):
        return {
            "default": finite_float(value.get("default"), 1.0),
            "flowWeights": {str(k): finite_float(v, 1.0) for k, v in (value.get("flowWeights") or {}).items()},
            "stratumWeights": {
                str(k): finite_float(v, 1.0) for k, v in (value.get("stratumWeights") or {}).items()
            },
            "asnWeights": {str(k): finite_float(v, 1.0) for k, v in (value.get("asnWeights") or {}).items()},
        }
    return {
        "default": 1.0,
        "flowWeights": {str(k): finite_float(v, 1.0) for k, v in value.items()},
        "stratumWeights": {},
        "asnWeights": {},
    }


def endpoint_id(endpoint: Any) -> str:
    """Return a stable endpoint identifier from a string or endpoint object."""
    if isinstance(endpoint, str):
        return endpoint
    if isinstance(endpoint, dict):
        for key in ("id", "app", "pod", "name"):
            if endpoint.get(key):
                return str(endpoint[key])
    return ""


def flow_weight(flow: dict[str, Any], weights: dict[str, Any]) -> float:
    """Resolve a network importance weight for one flow."""
    flow_id = str(flow.get("flowId") or flow.get("id") or "")
    if flow_id in weights.get("flowWeights", {}):
        return finite_float(weights["flowWeights"][flow_id], 1.0)
    stratum = str(flow.get("stratum") or "")
    if stratum in weights.get("stratumWeights", {}):
        return finite_float(weights["stratumWeights"][stratum], 1.0)
    asn = flow.get("asn")
    if asn is None:
        src = flow.get("src") if isinstance(flow.get("src"), dict) else {}
        asn = src.get("asn")
    if str(asn) in weights.get("asnWeights", {}):
        return finite_float(weights["asnWeights"][str(asn)], 1.0)
    return finite_float(flow.get("importanceWeight"), finite_float(weights.get("default"), 1.0))


def profile_defaults(profile: dict[str, Any]) -> dict[str, float]:
    """Normalise one VM profile and preserve optional sensitivity parameters."""
    return {
        "cpuCapacityCores": max(finite_float(profile.get("cpuCapacityCores", profile.get("cpuCores")), 1.0), 1e-9),
        "memoryCapacityMiB": max(
            finite_float(profile.get("memoryCapacityMiB", profile.get("memoryMiB")), 1.0), 1e-9
        ),
        "networkCapacityMbps": max(finite_float(profile.get("networkCapacityMbps"), 1.0), 1e-9),
        "cpuSensitivity": finite_float(profile.get("cpuSensitivity"), 1.0),
        "memorySensitivity": finite_float(profile.get("memorySensitivity"), 1.0),
        "networkSensitivity": finite_float(profile.get("networkSensitivity"), 1.0),
        "cpuKnee": finite_float(profile.get("cpuKnee"), 0.70),
        "memoryKnee": finite_float(profile.get("memoryKnee"), 0.70),
        "networkKnee": finite_float(profile.get("networkKnee"), 0.70),
    }


def pod_resource(pod: dict[str, Any]) -> tuple[float, float, float, float, float]:
    """Return CPU, memory, network demand and CPU/memory weights for a Pod."""
    cpu = finite_float(pod.get("cpuCores", pod.get("cpu")), 0.0)
    memory = finite_float(pod.get("memoryMiB", pod.get("memory")), 0.0)
    network = finite_float(pod.get("networkMbps", pod.get("network")), 0.0)
    cpu_weight = finite_float(pod.get("cpuWeight", pod.get("resourceWeight")), 1.0)
    memory_weight = finite_float(pod.get("memoryWeight", pod.get("resourceWeight")), 1.0)
    return cpu, memory, network, cpu_weight, memory_weight


def hinge(value: float, knee: float) -> float:
    """Positive excess above a profile-specific utilisation knee."""
    return max(0.0, value - knee)


def weighted_crossing_features(
    placement: dict[str, str], flows: Iterable[dict[str, Any]], weights: dict[str, Any]
) -> dict[str, float]:
    """Calculate raw and importance-weighted cross-node offered traffic."""
    raw = 0.0
    weighted = 0.0
    crossing_count = 0
    total_raw = 0.0
    total_weighted = 0.0
    weighted_by_flow: list[dict[str, Any]] = []
    for flow in flows:
        src = endpoint_id(flow.get("src"))
        dst = endpoint_id(flow.get("dst"))
        offered = max(0.0, finite_float(flow.get("offeredMbps", flow.get("rateMbps")), 0.0))
        importance = flow_weight(flow, weights)
        total_raw += offered
        total_weighted += offered * importance
        crosses = bool(src and dst and placement.get(src) != placement.get(dst))
        if crosses:
            raw += offered
            weighted += offered * importance
            crossing_count += 1
        weighted_by_flow.append(
            {
                "flowId": str(flow.get("flowId") or flow.get("id") or ""),
                "src": src,
                "dst": dst,
                "offeredMbps": offered,
                "importanceWeight": importance,
                "crosses": crosses,
                "weightedCrossMbps": offered * importance if crosses else 0.0,
            }
        )
    return {
        "crossingCount": float(crossing_count),
        "rawCrossMbps": raw,
        "weightedCrossMbps": weighted,
        "totalRawMbps": total_raw,
        "totalWeightedMbps": total_weighted,
        "crossingRatio": raw / total_raw if total_raw else 0.0,
        "weightedCrossingRatio": weighted / total_weighted if total_weighted else 0.0,
        "weightedByFlow": weighted_by_flow,
    }


def node_load_features(
    scenario: dict[str, Any],
    nodes: dict[str, dict[str, Any]],
    pods: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Calculate profile-normalised resource and contention features.

    A scenario may provide measured/estimated aggregate ``nodeLoads``.  When
    omitted, loads are formed by summing Pod demands and optional ``baseLoads``.
    Aggregate loads are useful for E1-B, where the placement planner already
    exports per-node CPU and memory estimates.
    """
    placement = {str(k): str(v) for k, v in (scenario.get("placement") or {}).items()}
    explicit = scenario.get("nodeLoads") or {}
    base = scenario.get("baseLoads") or {}
    loads: dict[str, dict[str, float]] = {
        node: {
            "cpuCores": finite_float(base.get(node, {}).get("cpuCores"), 0.0),
            "memoryMiB": finite_float(base.get(node, {}).get("memoryMiB"), 0.0),
            "networkMbps": finite_float(base.get(node, {}).get("networkMbps"), 0.0),
            "weightedCpuCores": 0.0,
            "weightedMemoryMiB": 0.0,
        }
        for node in nodes
    }
    for pod_id, node in placement.items():
        if node not in loads:
            loads[node] = {
                "cpuCores": 0.0,
                "memoryMiB": 0.0,
                "networkMbps": 0.0,
                "weightedCpuCores": 0.0,
                "weightedMemoryMiB": 0.0,
            }
        cpu, memory, network, cpu_weight, memory_weight = pod_resource(pods.get(pod_id, {}))
        loads[node]["cpuCores"] += cpu
        loads[node]["memoryMiB"] += memory
        loads[node]["networkMbps"] += network
        loads[node]["weightedCpuCores"] += cpu * cpu_weight
        loads[node]["weightedMemoryMiB"] += memory * memory_weight
    for node, load in explicit.items():
        target = loads.setdefault(
            str(node),
            {
                "cpuCores": 0.0,
                "memoryMiB": 0.0,
                "networkMbps": 0.0,
                "weightedCpuCores": 0.0,
                "weightedMemoryMiB": 0.0,
            },
        )
        for key in ("cpuCores", "memoryMiB", "networkMbps", "weightedCpuCores", "weightedMemoryMiB"):
            if key in load:
                target[key] = finite_float(load[key], target[key])

    node_rows: dict[str, Any] = {}
    cpu_utils: list[float] = []
    memory_utils: list[float] = []
    network_utils: list[float] = []
    cpu_contention = 0.0
    memory_contention = 0.0
    network_contention = 0.0
    for node, load in sorted(loads.items()):
        profile = profile_defaults(nodes.get(node, {}))
        cpu_util = load["cpuCores"] / profile["cpuCapacityCores"]
        memory_util = load["memoryMiB"] / profile["memoryCapacityMiB"]
        network_util = load["networkMbps"] / profile["networkCapacityMbps"]
        cpu_excess = hinge(cpu_util, profile["cpuKnee"])
        memory_excess = hinge(memory_util, profile["memoryKnee"])
        network_excess = hinge(network_util, profile["networkKnee"])
        cpu_term = profile["cpuSensitivity"] * cpu_excess**2
        memory_term = profile["memorySensitivity"] * memory_excess**2
        network_term = profile["networkSensitivity"] * network_excess**2
        cpu_contention += cpu_term
        memory_contention += memory_term
        network_contention += network_term
        cpu_utils.append(cpu_util)
        memory_utils.append(memory_util)
        network_utils.append(network_util)
        node_rows[node] = {
            "cpuCores": load["cpuCores"],
            "memoryMiB": load["memoryMiB"],
            "networkMbps": load["networkMbps"],
            "cpuUtil": cpu_util,
            "memoryUtil": memory_util,
            "networkUtil": network_util,
            "cpuExcess": cpu_excess,
            "memoryExcess": memory_excess,
            "networkExcess": network_excess,
            "cpuContention": cpu_term,
            "memoryContention": memory_term,
            "networkContention": network_term,
        }
    return {
        "nodes": node_rows,
        "cpuContention": cpu_contention,
        "memoryContention": memory_contention,
        "networkContention": network_contention,
        "totalContention": cpu_contention + memory_contention + network_contention,
        "maxCpuUtil": max(cpu_utils, default=0.0),
        "maxMemoryUtil": max(memory_utils, default=0.0),
        "maxNetworkUtil": max(network_utils, default=0.0),
        "meanCpuUtil": statistics.fmean(cpu_utils) if cpu_utils else 0.0,
        "meanMemoryUtil": statistics.fmean(memory_utils) if memory_utils else 0.0,
        "meanNetworkUtil": statistics.fmean(network_utils) if network_utils else 0.0,
        "weightedCpuCores": sum(item["weightedCpuCores"] for item in loads.values()),
        "weightedMemoryMiB": sum(item["weightedMemoryMiB"] for item in loads.values()),
    }


FEATURE_NAMES = (
    "weightedCrossMbps",
    "cpuContention",
    "memoryContention",
    "networkContention",
    "maxCpuUtil",
    "maxMemoryUtil",
    "maxNetworkUtil",
)

# E1-B has six mappings and one VM profile.  Fitting all seven state features
# would make the replay almost exactly interpolate its own points.  The
# reduced set keeps the replay identifiable while the generic ``evaluate``
# path still accepts the full feature vector from a multi-profile data set.
REPLAY_FEATURE_NAMES = (
    "weightedCrossMbps",
    "cpuContention",
    "memoryContention",
)


def scenario_features(
    scenario: dict[str, Any],
    nodes: dict[str, dict[str, Any]],
    pods: dict[str, dict[str, Any]],
    flows: Iterable[dict[str, Any]],
    weights: dict[str, Any],
) -> dict[str, Any]:
    """Return all model features for one placement scenario."""
    communication = weighted_crossing_features(scenario.get("placement") or {}, flows, weights)
    resources = node_load_features(scenario, nodes, pods)
    output = {
        **{key: communication[key] for key in (
            "crossingCount",
            "rawCrossMbps",
            "weightedCrossMbps",
            "totalRawMbps",
            "totalWeightedMbps",
            "crossingRatio",
            "weightedCrossingRatio",
        )},
        **{key: resources[key] for key in (
            "cpuContention",
            "memoryContention",
            "networkContention",
            "totalContention",
            "maxCpuUtil",
            "maxMemoryUtil",
            "maxNetworkUtil",
            "meanCpuUtil",
            "meanMemoryUtil",
            "meanNetworkUtil",
            "weightedCpuCores",
            "weightedMemoryMiB",
        )},
        "nodes": resources["nodes"],
    }
    return output


def solve_linear(matrix: list[list[float]], vector: list[float]) -> list[float]:
    """Solve a small dense linear system using Gauss-Jordan elimination."""
    n = len(vector)
    augmented = [list(matrix[i]) + [vector[i]] for i in range(n)]
    for column in range(n):
        pivot = max(range(column, n), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            raise ValueError("singular regression matrix")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        scale = augmented[column][column]
        augmented[column] = [value / scale for value in augmented[column]]
        for row in range(n):
            if row == column:
                continue
            scale = augmented[row][column]
            if abs(scale) < 1e-15:
                continue
            augmented[row] = [
                augmented[row][index] - scale * augmented[column][index]
                for index in range(n + 1)
            ]
    return [augmented[index][-1] for index in range(n)]


def ridge_fit(
    rows: list[dict[str, Any]], target: str, feature_names: tuple[str, ...], alpha: float
) -> dict[str, Any]:
    """Fit a ridge model and compute in-sample/leave-one-out diagnostics."""
    if not rows:
        raise ValueError(f"no rows available for target {target}")
    x_rows = [[1.0] + [finite_float(row.get(name), 0.0) for name in feature_names] for row in rows]
    y_values = [finite_float(row.get(target), 0.0) for row in rows]
    dimension = len(feature_names) + 1

    def fit_subset(indices: list[int]) -> list[float]:
        gram = [[0.0 for _ in range(dimension)] for _ in range(dimension)]
        rhs = [0.0 for _ in range(dimension)]
        for index in indices:
            values = x_rows[index]
            for i in range(dimension):
                rhs[i] += values[i] * y_values[index]
                for j in range(dimension):
                    gram[i][j] += values[i] * values[j]
        for i in range(1, dimension):
            gram[i][i] += alpha
        try:
            return solve_linear(gram, rhs)
        except ValueError:
            # A larger diagonal stabiliser makes the warning explicit in the
            # output while allowing a tiny, collinear calibration data set.
            for i in range(dimension):
                gram[i][i] += max(alpha, 1e-6) * 1000.0
            return solve_linear(gram, rhs)

    indices = list(range(len(rows)))
    coefficients = fit_subset(indices)

    def predict(beta: list[float], row: dict[str, Any]) -> float:
        values = [1.0] + [finite_float(row.get(name), 0.0) for name in feature_names]
        return sum(left * right for left, right in zip(beta, values))

    fitted = [predict(coefficients, row) for row in rows]
    residuals = [actual - predicted for actual, predicted in zip(y_values, fitted)]
    y_mean = statistics.fmean(y_values)
    sse = sum(residual * residual for residual in residuals)
    sst = sum((actual - y_mean) ** 2 for actual in y_values)
    loo: list[dict[str, float]] = []
    for held_out in indices:
        train = [index for index in indices if index != held_out]
        if len(train) < dimension:
            loo.append({"actual": y_values[held_out], "predicted": None})
            continue
        beta = fit_subset(train)
        loo.append({"actual": y_values[held_out], "predicted": predict(beta, rows[held_out])})
    loo_errors = [
        item["actual"] - float(item["predicted"])
        for item in loo
        if item["predicted"] is not None
    ]
    return {
        "target": target,
        "featureNames": list(feature_names),
        "alpha": alpha,
        "coefficients": {"intercept": coefficients[0], **{
            name: coefficients[index + 1] for index, name in enumerate(feature_names)
        }},
        "sampleCount": len(rows),
        "parameterCount": dimension,
        "underDetermined": len(rows) < dimension + 2,
        "r2InSample": 1.0 - sse / sst if sst > 1e-15 else None,
        "rmseInSample": math.sqrt(sse / len(rows)) if rows else None,
        "looValidCount": len(loo_errors),
        "looRmse": math.sqrt(sum(error * error for error in loo_errors) / len(loo_errors))
        if loo_errors
        else None,
        "fitted": fitted,
        "leaveOneOut": loo,
    }


def model_predict(model: dict[str, Any], features: dict[str, Any], target: str) -> float:
    """Evaluate one stored response model."""
    response = (model.get("responses") or {}).get(target) or {}
    coefficients = response.get("coefficients") or {}
    value = finite_float(coefficients.get("intercept"), 0.0)
    for name in model.get("featureNames", FEATURE_NAMES):
        value += finite_float(coefficients.get(name), 0.0) * finite_float(features.get(name), 0.0)
    return value


def metric_ratio(after: Any, before: Any, *, benefit_sign: float = 1.0) -> float | None:
    """Return a signed ratio delta, preserving missing measurements."""
    before_value = finite_float(before, math.nan)
    after_value = finite_float(after, math.nan)
    if not math.isfinite(before_value) or not math.isfinite(after_value) or abs(before_value) < 1e-12:
        return None
    return benefit_sign * (after_value / before_value - 1.0)


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    """Read a CSV as dictionaries."""
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def mean_mapping_metrics(metrics_path: Path) -> dict[str, dict[str, Any]]:
    """Aggregate E1-B formal point metrics by mapping."""
    rows = read_csv_rows(metrics_path)
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        if row.get("placementStatus") != "PASS":
            continue
        grouped.setdefault(row["mappingId"], []).append(row)
    output: dict[str, dict[str, Any]] = {}
    numeric_fields = [
        "targetWorkerPodCount",
        "fixedFlowCrossings",
        "xExpectedPayloadMbpsCrossings",
        "udpReceivedMbps",
        "udpDeliveryRatio",
        "tcpGoodputMbps",
        "udpBackgroundOnTimeMean",
        "udpCanaryP99MeanMs",
        "udpMaxPsiSomeAvg10Mean",
        "udpMaxRunQueueMean",
        "tcpBackgroundOnTimeMean",
        "tcpCanaryP99MeanMs",
        "tcpMaxPsiSomeAvg10Mean",
        "tcpMaxRunQueueMean",
    ]
    for mapping_id, values in grouped.items():
        item: dict[str, Any] = {"mappingId": mapping_id, "sampleCount": len(values)}
        for field in numeric_fields:
            numbers = [finite_float(row.get(field), math.nan) for row in values]
            numbers = [number for number in numbers if math.isfinite(number)]
            item[field] = statistics.fmean(numbers) if numbers else None
        item["validUdpWorkloadCount"] = sum(row.get("udpWorkloadValid") == "True" for row in values)
        output[mapping_id] = item
    return output


def e1b_scenarios(
    plan_dir: Path,
    metrics_path: Path,
    weights: dict[str, Any],
    cpu_capacity: float,
    memory_capacity: float,
    network_capacity: float,
    cpu_knee: float,
    memory_knee: float,
    network_knee: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Construct an aggregate scenario table from the frozen E1-B artifacts."""
    sequence = read_json(plan_dir / "placement_sequence.json")
    pair_plan = read_json(plan_dir / "selected_pair_plan.json")
    flows = pair_plan.get("flows", [])
    metrics = mean_mapping_metrics(metrics_path)
    mappings = sequence.get("mappings", [])
    node_names = sorted({
        node
        for mapping in mappings
        for node in (mapping.get("perNode") or {})
    })
    nodes = {
        node: {
            "cpuCapacityCores": cpu_capacity,
            "memoryCapacityMiB": memory_capacity,
            "networkCapacityMbps": network_capacity,
            "cpuKnee": cpu_knee,
            "memoryKnee": memory_knee,
            "networkKnee": network_knee,
        }
        for node in node_names
    }
    scenarios: list[dict[str, Any]] = []
    for mapping in mappings:
        per_node = mapping.get("perNode") or {}
        node_loads = {
            node: {
                "cpuCores": finite_float(value.get("cpuEstimateCores"), 0.0),
                "memoryMiB": finite_float(value.get("memoryEstimateMiB"), 0.0),
            }
            for node, value in per_node.items()
        }
        scenario = {
            "id": mapping["mappingId"],
            "placement": mapping.get("assignments", {}),
            "nodeLoads": node_loads,
            "observed": metrics.get(mapping["mappingId"], {}),
            "planner": {
                "targetWorkerPodCount": mapping.get("targetWorkerPodCount"),
                "fixedFlowCrossings": mapping.get("fixedFlowCrossings"),
                "xExpectedPayloadMbpsCrossings": mapping.get("xExpectedPayloadMbpsCrossings"),
            },
        }
        features = scenario_features(scenario, nodes, {}, flows, weights)
        scenario["features"] = features
        scenarios.append(scenario)
    return {
        "nodes": nodes,
        "pods": {},
        "flows": flows,
        "weights": weights,
        "source": {
            "planDir": str(plan_dir),
            "metrics": str(metrics_path),
            "mappingCount": len(scenarios),
        },
    }, scenarios


def response_rows(
    scenarios: list[dict[str, Any]], baseline_id: str | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build baseline-relative response rows and preserve raw observations."""
    baseline = next(
        (item for item in scenarios if item["id"] == baseline_id),
        scenarios[0] if scenarios else None,
    )
    if baseline is None:
        return [], {}
    base_observed = baseline.get("observed", {})
    response_specs = {
        "tcpGoodputDeltaRatio": ("tcpGoodputMbps", 1.0),
        "udpReceivedDeltaRatio": ("udpReceivedMbps", 1.0),
        "udpDeliveryDeltaRatio": ("udpDeliveryRatio", 1.0),
        "udpWorkloadOnTimeDeltaRatio": ("udpBackgroundOnTimeMean", 1.0),
        "tcpWorkloadOnTimeDeltaRatio": ("tcpBackgroundOnTimeMean", 1.0),
        "udpRttDeltaRatio": ("udpCanaryP99MeanMs", -1.0),
        "tcpRttDeltaRatio": ("tcpCanaryP99MeanMs", -1.0),
    }
    rows: list[dict[str, Any]] = []
    for scenario in scenarios:
        row = {
            "scenarioId": scenario["id"],
            **{name: finite_float(scenario["features"].get(name), 0.0) for name in FEATURE_NAMES},
        }
        observed = scenario.get("observed", {})
        for target, (field, sign) in response_specs.items():
            row[target] = metric_ratio(observed.get(field), base_observed.get(field), benefit_sign=sign)
        row["rawObserved"] = observed
        rows.append(row)
    return rows, {"baselineId": baseline["id"], "baselineObserved": base_observed, "responseSpecs": response_specs}


def weighted_performance_delta(
    after: dict[str, Any],
    before: dict[str, Any],
    metric_weights: dict[str, float],
) -> float:
    """Aggregate measured or predicted response deltas with user weights."""
    score = 0.0
    for metric, weight in metric_weights.items():
        score += finite_float(after.get(metric), 0.0) * finite_float(weight, 0.0)
        score -= finite_float(before.get(metric), 0.0) * finite_float(weight, 0.0)
    return score


def fit_replay_model(
    dataset: dict[str, Any],
    rows: list[dict[str, Any]],
    baseline_info: dict[str, Any],
    alpha: float,
) -> dict[str, Any]:
    """Fit all available E1-B response columns and return a model artifact."""
    targets = [
        "tcpGoodputDeltaRatio",
        "udpReceivedDeltaRatio",
        "udpDeliveryDeltaRatio",
        "udpWorkloadOnTimeDeltaRatio",
        "tcpWorkloadOnTimeDeltaRatio",
        "udpRttDeltaRatio",
        "tcpRttDeltaRatio",
    ]
    responses: dict[str, Any] = {}
    usable_rows: list[dict[str, Any]] = []
    for row in rows:
        if any(row.get(target) is not None for target in targets):
            usable_rows.append(row)
    for target in targets:
        target_rows = [row for row in usable_rows if row.get(target) is not None]
        if target_rows:
            responses[target] = ridge_fit(target_rows, target, REPLAY_FEATURE_NAMES, alpha)
    return {
        "schemaVersion": 1,
        "modelKind": "marginal-locality-contention-ridge",
        "featureNames": list(REPLAY_FEATURE_NAMES),
        "responses": responses,
        "baseline": baseline_info,
        "calibration": dataset.get("source", {}),
        "limitations": [
            "E1-B replay has one VM profile and six mappings; coefficients are a calibration artifact, not a universal model.",
            "Profile transfer requires independent calibration rows for CPU-, memory-, and network-limited VM classes.",
            "Raw throughput, RTT, delivery, and service-rate responses remain available; the caller chooses metric weights.",
        ],
    }


def predicted_marginal(
    model: dict[str, Any],
    before: dict[str, Any],
    after: dict[str, Any],
    metric_weights: dict[str, float],
    communication_weight: float,
    performance_weight: float,
) -> dict[str, Any]:
    """Compare two feature vectors and return an auditable marginal score."""
    communication_saved = finite_float(before.get("weightedCrossMbps"), 0.0) - finite_float(
        after.get("weightedCrossMbps"), 0.0
    )
    response_deltas: dict[str, float] = {}
    for target in (model.get("responses") or {}):
        response_deltas[target] = model_predict(model, after, target) - model_predict(model, before, target)
    performance_delta = sum(
        finite_float(response_deltas.get(metric), 0.0) * finite_float(weight, 0.0)
        for metric, weight in metric_weights.items()
    )
    return {
        "weightedCrossMbpsBefore": finite_float(before.get("weightedCrossMbps"), 0.0),
        "weightedCrossMbpsAfter": finite_float(after.get("weightedCrossMbps"), 0.0),
        "weightedCrossMbpsSaved": communication_saved,
        "contentionBefore": {
            "cpu": finite_float(before.get("cpuContention"), 0.0),
            "memory": finite_float(before.get("memoryContention"), 0.0),
            "network": finite_float(before.get("networkContention"), 0.0),
        },
        "contentionAfter": {
            "cpu": finite_float(after.get("cpuContention"), 0.0),
            "memory": finite_float(after.get("memoryContention"), 0.0),
            "network": finite_float(after.get("networkContention"), 0.0),
        },
        "contentionDelta": finite_float(after.get("totalContention"), 0.0)
        - finite_float(before.get("totalContention"), 0.0),
        "predictedResponseDeltas": response_deltas,
        "weightedPerformanceDelta": performance_delta,
        "netMarginalScore": communication_weight * communication_saved
        + performance_weight * performance_delta,
        "interpretation": (
            "positive means the selected metric weights prefer the after placement; "
            "this score does not override hard admission limits"
        ),
    }


def render_replay_figure(output_dir: Path, rows: list[dict[str, Any]]) -> str | None:
    """Render communication/contention and measured response diagnostics."""
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        return None
    rows = [row for row in rows if row.get("weightedCrossMbps") is not None]
    if not rows:
        return None
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = sorted(rows, key=lambda row: finite_float(row.get("weightedCrossMbps"), 0.0), reverse=True)
    x = [finite_float(row.get("weightedCrossMbps"), 0.0) for row in rows]
    fig, axes = plt.subplots(2, 2, figsize=(13, 8), constrained_layout=True)
    axes[0, 0].plot(x, [finite_float(row.get("cpuContention"), 0.0) for row in rows], "o-", label="CPU contention")
    axes[0, 0].plot(x, [finite_float(row.get("memoryContention"), 0.0) for row in rows], "o-", label="memory contention")
    axes[0, 0].set(title="Resource cost vs weighted cross traffic", xlabel="weighted cross Mbps")
    axes[0, 0].set_ylabel("hinge-squared contention")
    axes[0, 0].legend()
    metric_specs = [
        ("tcpGoodputDeltaRatio", "TCP goodput Δ ratio"),
        ("udpDeliveryDeltaRatio", "UDP delivery Δ ratio"),
        ("udpWorkloadOnTimeDeltaRatio", "UDP on-time Δ ratio"),
    ]
    for axis, (metric, label) in zip(axes.flat[1:], metric_specs):
        values = [row.get(metric) for row in rows]
        axis.plot(x, [finite_float(value, math.nan) for value in values], "o-", label="observed")
        predicted = [row.get("predicted", {}).get(metric) for row in rows]
        if any(value is not None for value in predicted):
            axis.plot(x, [finite_float(value, math.nan) for value in predicted], "x--", label="fit")
        axis.axhline(0.0, color="#777", linewidth=0.8)
        axis.set(xlabel="weighted cross Mbps", ylabel="ratio delta", title=label)
        axis.legend()
        axis.grid(alpha=0.25)
    for axis in axes.flat:
        axis.grid(alpha=0.25)
    path = output_dir / "marginal_benefit_replay.png"
    fig.savefig(path, dpi=180)
    plt.close(fig)
    return str(path)


def adjacent_marginals(
    scenarios: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    model: dict[str, Any],
    primary_sequence: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Compute auditable marginal deltas along the frozen primary sequence."""
    by_id = {scenario["id"]: scenario for scenario in scenarios}
    row_by_id = {row["scenarioId"]: row for row in rows}
    sequence = primary_sequence or [
        scenario["id"]
        for scenario in scenarios
        if scenario["id"].startswith("pi")
    ]
    output: list[dict[str, Any]] = []
    for before_id, after_id in zip(sequence, sequence[1:]):
        before = by_id[before_id]["features"]
        after = by_id[after_id]["features"]
        result = predicted_marginal(
            model,
            before,
            after,
            {
                "tcpGoodputDeltaRatio": 1.0,
                "udpDeliveryDeltaRatio": 1.0,
                "udpWorkloadOnTimeDeltaRatio": 1.0,
                "tcpWorkloadOnTimeDeltaRatio": 1.0,
                "udpRttDeltaRatio": 1.0,
                "tcpRttDeltaRatio": 1.0,
            },
            communication_weight=1.0,
            performance_weight=1.0,
        )
        before_row = row_by_id[before_id]
        after_row = row_by_id[after_id]
        row = {
            "before": before_id,
            "after": after_id,
            "weightedCrossMbpsSaved": result["weightedCrossMbpsSaved"],
            "cpuContentionDelta": finite_float(after.get("cpuContention"))
            - finite_float(before.get("cpuContention")),
            "memoryContentionDelta": finite_float(after.get("memoryContention"))
            - finite_float(before.get("memoryContention")),
            "weightedPerformanceDeltaPredicted": result["weightedPerformanceDelta"],
            "netMarginalScorePredicted": result["netMarginalScore"],
        }
        for target in (
            "tcpGoodputDeltaRatio",
            "udpDeliveryDeltaRatio",
            "udpWorkloadOnTimeDeltaRatio",
            "tcpWorkloadOnTimeDeltaRatio",
            "udpRttDeltaRatio",
            "tcpRttDeltaRatio",
        ):
            row[f"{target}ObservedStep"] = (
                finite_float(after_row.get(target), 0.0) - finite_float(before_row.get(target), 0.0)
            )
            row[f"{target}PredictedStep"] = finite_float(
                result["predictedResponseDeltas"].get(target), 0.0
            )
        output.append(row)
    return output


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write a rectangular CSV."""
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def replay_e1b(arguments: argparse.Namespace) -> int:
    """Replay E1-B and write a fitted model plus diagnostics."""
    plan_dir = arguments.plan_dir.resolve()
    metrics_path = arguments.metrics.resolve()
    output_dir = arguments.output_dir.resolve()
    weights = normalise_weight_file(arguments.network_weights.resolve() if arguments.network_weights else None)
    dataset, scenarios = e1b_scenarios(
        plan_dir,
        metrics_path,
        weights,
        arguments.cpu_capacity,
        arguments.memory_capacity,
        arguments.network_capacity,
        arguments.cpu_knee,
        arguments.memory_knee,
        arguments.network_knee,
    )
    rows, baseline_info = response_rows(scenarios, arguments.baseline_id)
    model = fit_replay_model(dataset, rows, baseline_info, arguments.alpha)
    for row in rows:
        row["predicted"] = {
            target: model_predict(model, row, target)
            for target in model.get("responses", {})
        }
    dataset["scenarios"] = scenarios
    dataset["responseRows"] = rows
    dataset["model"] = model
    primary_sequence = list(
        read_json(plan_dir / "placement_sequence.json").get("primarySequence") or []
    )
    dataset["primarySequence"] = primary_sequence
    marginals = adjacent_marginals(scenarios, rows, model, primary_sequence)
    dataset["primarySequenceMarginals"] = marginals
    write_json(output_dir / "marginal_benefit_dataset.json", dataset)
    write_json(output_dir / "marginal_benefit_model.json", model)
    write_csv(output_dir / "marginal_boundary_steps.csv", marginals)
    image = render_replay_figure(output_dir, rows)
    report = [
        "# 边际收益模型：E1-B 回放",
        "",
        f"- 数据源：`{metrics_path}`",
        f"- 映射数：{len(scenarios)}；基准映射：`{baseline_info.get('baselineId')}`",
        f"- 回放拟合特征：`{', '.join(model.get('featureNames', []))}`；"
        f"完整场景特征：`{', '.join(FEATURE_NAMES)}`",
        "",
        "模型把一条候选放置的收益拆成两部分：",
        "",
        "1. `weightedCrossMbpsSaved`：加入目标 VM 后，重要性加权的跨 VM offered traffic 减少量。",
        "2. `weightedPerformanceDelta`：由 CPU、内存、网络利用率和其 knee 上方的争用特征预测的吞吐/交付/按时率/RTT 变化。",
        "",
        "正的吞吐、交付率和按时率变化表示性能改善；RTT 的响应列已经反向取符号，因此正值也表示改善。",
        "最终净分由调用方提供的通信权重和性能指标权重决定，不能替代 kubelet/CNI 准入检查。",
        "",
        "## 当前数据边界",
        "",
        "本回放只有一个 VM 规格（4 vCPU/8 GiB）和六个静态映射，因此输出用于验证特征和计算链路，不能据此声称跨 VM 规格的普适系数。",
        "下一步应将同一 workload 在 CPU 受限、内存受限和均衡的 VM profile 上重复标定，再把 profile 作为模型输入或分层系数。",
    ]
    if marginals:
        report.extend(
            [
                "",
                "## 主序列的相邻边际",
                "",
                "| before → after | 加权跨机 Mbps 节省 | CPU 争用变化 | 内存争用变化 |",
                "|---|---:|---:|---:|",
            ]
        )
        for marginal in marginals:
            report.append(
                f"| `{marginal['before']} → {marginal['after']}` | "
                f"{marginal['weightedCrossMbpsSaved']:.3f} | "
                f"{marginal['cpuContentionDelta']:.3f} | "
                f"{marginal['memoryContentionDelta']:.3f} |"
            )
        report.extend(
            [
                "",
                "相邻边际表用于识别“继续减少跨机通信但资源代价开始陡增”的区间；"
                "它不把某个区间自动判为正确答案。",
            ]
        )
    if marginals:
        report.append("")
        report.append("机器可读的相邻边际：`marginal_boundary_steps.csv`。")
    if image:
        report.extend(["", f"图：`{Path(image).name}`"])
    (output_dir / "marginal_benefit_report_zh.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(json.dumps({
        "outputDir": str(output_dir),
        "model": str(output_dir / "marginal_benefit_model.json"),
        "dataset": str(output_dir / "marginal_benefit_dataset.json"),
        "figure": image,
        "mappingIds": [scenario["id"] for scenario in scenarios],
    }, indent=2, ensure_ascii=False))
    return 0


def evaluate(arguments: argparse.Namespace) -> int:
    """Score before/after scenarios from one generic JSON input."""
    payload = read_json(arguments.input.resolve())
    model = read_json(arguments.model.resolve())
    profile_overrides: dict[str, Any] = {}
    if arguments.profile_overrides:
        value = read_json(arguments.profile_overrides.resolve())
        if isinstance(value, dict):
            profile_overrides = value
    nodes: dict[str, dict[str, Any]] = {}
    for key, value in (payload.get("nodes") or {}).items():
        node = dict(value)
        profile_id = node.get("profile")
        if profile_id in profile_overrides:
            merged = dict(profile_overrides[profile_id])
            merged.update(node)
            node = merged
        nodes[str(key)] = node
    pods = {str(k): dict(v) for k, v in (payload.get("pods") or {}).items()}
    flows = list(payload.get("flows") or [])
    weights = normalise_weight_file(arguments.network_weights.resolve() if arguments.network_weights else None)
    if not weights.get("flowWeights") and not weights.get("stratumWeights") and not weights.get("asnWeights"):
        weights = payload.get("weights") or weights
    before = scenario_features(payload["before"], nodes, pods, flows, weights)
    after = scenario_features(payload["after"], nodes, pods, flows, weights)
    metric_weights = {
        str(key): finite_float(value, 0.0)
        for key, value in (payload.get("metricWeights") or {
            "tcpGoodputDeltaRatio": 1.0,
            "udpDeliveryDeltaRatio": 1.0,
            "udpWorkloadOnTimeDeltaRatio": 1.0,
            "tcpWorkloadOnTimeDeltaRatio": 1.0,
            "udpRttDeltaRatio": 1.0,
            "tcpRttDeltaRatio": 1.0,
        }).items()
    }
    result = predicted_marginal(
        model,
        before,
        after,
        metric_weights,
        finite_float(payload.get("communicationWeight"), 1.0),
        finite_float(payload.get("performanceWeight"), 1.0),
    )
    result["beforeFeatures"] = before
    result["afterFeatures"] = after
    write_json(arguments.output.resolve(), result)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def parse_arguments() -> argparse.Namespace:
    """Parse subcommands."""
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    replay = subparsers.add_parser("replay-e1b", help="fit/replay the frozen E1-B mappings")
    replay.add_argument("--plan-dir", type=Path, default=DEFAULT_E1B_PLAN)
    replay.add_argument("--metrics", type=Path, default=DEFAULT_E1B_METRICS)
    replay.add_argument("--network-weights", type=Path)
    replay.add_argument("--output-dir", type=Path, required=True)
    replay.add_argument("--baseline-id", default="pi00-target044")
    replay.add_argument("--cpu-capacity", type=float, default=4.0)
    replay.add_argument("--memory-capacity", type=float, default=8192.0)
    replay.add_argument("--network-capacity", type=float, default=1000.0)
    replay.add_argument("--cpu-knee", type=float, default=0.70)
    replay.add_argument("--memory-knee", type=float, default=0.70)
    replay.add_argument("--network-knee", type=float, default=0.70)
    replay.add_argument("--alpha", type=float, default=1e-3)
    replay.set_defaults(handler=replay_e1b)
    score = subparsers.add_parser("evaluate", help="score a before/after candidate move")
    score.add_argument("--input", type=Path, required=True)
    score.add_argument("--model", type=Path, required=True)
    score.add_argument("--network-weights", type=Path)
    score.add_argument("--profile-overrides", type=Path,
                       help="JSON emitted by analyzeVmProfileCalibration.py")
    score.add_argument("--output", type=Path, required=True)
    score.set_defaults(handler=evaluate)
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_arguments()
    raise SystemExit(arguments.handler(arguments))
