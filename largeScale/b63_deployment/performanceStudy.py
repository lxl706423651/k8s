#!/usr/bin/env python3
"""Read-only feature and measurement extraction for B63 performance studies."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any


def readJson(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def nested(data: dict[str, Any], *keys: str) -> Any:
    value: Any = data
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def placementContention(report: dict[str, Any], algorithm: str, load_millicores: float,
                        knee: float) -> dict[str, Any]:
    """Compute the predeployment contention feature for one saved mapping."""
    nodes = report.get("nodes") or []
    include_master = bool((report.get("metadata") or {}).get("includeMaster", True))
    eligible = [node for node in nodes if include_master or node.get("schedulable")]
    mapping = (report.get("placements") or {}).get(algorithm) or {}
    pods = report.get("pods") or []
    total_cpu_weight = sum(max(float(pod.get("cpu") or 0), 1e-6) * max(int(pod.get("replicas") or 1), 1)
                           for pod in pods)
    total_memory_weight = sum(max(float(pod.get("memoryMb") or 0), 1.0) * max(int(pod.get("replicas") or 1), 1)
                              for pod in pods)
    total_cpu = sum(float(node.get("cpu") or 0) for node in eligible)
    total_memory = sum(float(node.get("memoryMb") or 0) for node in eligible)
    target = float(nested(report, "metadata", "researchPlacement", "resourceTargetUtilization") or 0.70)
    cpu_load = {str(node.get("name")): 0.0 for node in eligible}
    memory_load = {str(node.get("name")): 0.0 for node in eligible}
    pod_count = {str(node.get("name")): 0 for node in eligible}
    for pod in pods:
        name = str(pod.get("name") or "")
        node = mapping.get(name)
        if node not in cpu_load:
            continue
        replicas = max(int(pod.get("replicas") or 1), 1)
        cpu_weight = max(float(pod.get("cpu") or 0), 1e-6) * replicas
        memory_weight = max(float(pod.get("memoryMb") or 0), 1.0) * replicas
        cpu_load[node] += cpu_weight / max(total_cpu_weight, 1e-12) * total_cpu * min(target, 0.35)
        cpu_load[node] += load_millicores / 1000.0 * replicas
        memory_load[node] += memory_weight / max(total_memory_weight, 1e-12) * total_memory * target
        pod_count[node] += replicas
    per_node = {}
    total_contention = 0.0
    for node in eligible:
        name = str(node.get("name") or "")
        cpu_util = cpu_load[name] / max(float(node.get("cpu") or 0), 1e-9)
        memory_util = memory_load[name] / max(float(node.get("memoryMb") or 0), 1e-9)
        value = max(0.0, cpu_util - knee) ** 2 + max(0.0, memory_util - knee) ** 2
        total_contention += value
        per_node[name] = {"cpuUtilization": cpu_util, "memoryUtilization": memory_util,
                          "podCount": pod_count[name], "contention": value}
    return {"total": total_contention, "knee": knee, "perNode": per_node,
            "maxCpuUtilization": max((item["cpuUtilization"] for item in per_node.values()), default=0.0),
            "maxMemoryUtilization": max((item["memoryUtilization"] for item in per_node.values()), default=0.0)}


def sampleSignature(metrics: dict[str, Any]) -> str | None:
    metadata = metrics.get("metadata") or {}
    rounds = nested(metrics, "stress", "rounds") or []
    if not metadata or not rounds:
        return None
    content = {
        "topologyHash": metadata.get("topologyHash"),
        "pairPlanVersion": metadata.get("pairPlanVersion"),
        "pairSeed": metadata.get("pairSeed"),
        "rounds": [{"level": row.get("level"), "repetition": row.get("repetition"),
                    "selectedDirections": row.get("selectedDirections"),
                    "canaryDirections": row.get("canaryDirections")} for row in rounds],
    }
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def extractRows(root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Extract one row per variant/load from a serial study directory."""
    progress = readJson(root / "study_runs.json")
    rows: list[dict[str, Any]] = []
    for entry in progress.get("runs") or []:
        run = Path(str(entry.get("runDir") or ""))
        report = readJson(run / "output/placement_report.json")
        sweep = readJson(run / "load_sweep/summary.json")
        if not sweep:
            sweep = readJson(run / "load_sweep/summary.partial.json")
        algorithm = str(entry.get("algorithm") or "")
        validation = nested(report, "staticValidation", algorithm) or {}
        communication = nested(validation, "networkCosts", "pair")
        admission = nested(report, "metadata", "researchPlacement", "admission") or {}
        for level in sweep.get("levels") or []:
            load = float(level.get("cpuMillicoresPerPod") or 0)
            metrics = level.get("metrics") or {}
            stress_levels = nested(metrics, "stress", "byLevel") or {}
            numeric_levels = sorted((int(key), value) for key, value in stress_levels.items() if str(key).isdigit())
            stress_level, stress = numeric_levels[-1] if numeric_levels else (None, {})
            contention = placementContention(report, algorithm, load, float(
                nested(report, "metadata", "researchPlacement", "contentionKnee") or 0.70))
            host = level.get("hostMetrics") or {}
            node_util = nested(host, "nodeUtilization") or {}
            cpu_peaks = [float(item.get("cpuPeakPct") or 0) for item in node_util.values()]
            row = {
                "variant": entry.get("id"), "algorithm": algorithm,
                "runStatus": entry.get("status"), "sweepStatus": sweep.get("status"),
                "runDir": str(run), "loadMillicoresPerPod": load,
                "communicationPairRatio": communication,
                "contentionFeature": contention["total"],
                "predictedMaxCpuUtilization": contention["maxCpuUtilization"],
                "predictedMaxMemoryUtilization": contention["maxMemoryUtilization"],
                "pingRttMs": nested(metrics, "ping", "avgRttMs", "avg"),
                "pingFailureRate": nested(metrics, "ping", "failureRate"),
                "baselineTcpMeanMbps": nested(metrics, "baselineTcpIperf", "flowMbps", "avg"),
                "baselineTcpFailureRate": nested(metrics, "baselineTcpIperf", "failureRate"),
                "stressLevel": stress_level,
                "stressGoodputMbps": nested(stress, "aggregateGoodputMbps", "avg"),
                "stressRttMs": nested(stress, "loadedPingAvgRttMs", "avg"),
                "stressFailureRate": nested(stress, "failureRate", "avg"),
                "stressFairness": nested(stress, "fairness", "avg"),
                "observedHostCpuPeakPct": max(cpu_peaks) if cpu_peaks else None,
                "sampleSignature": sampleSignature(metrics),
                "admissionStatus": admission.get("status"),
                "allPodsRemainOnline": sweep.get("allPodsRemainOnline"),
            }
            if row["stressRttMs"] is None:
                row["stressRttMs"] = row["pingRttMs"]
            rows.append(row)
    return rows, progress


def performancePenalty(row: dict[str, Any]) -> float | None:
    rtt = row.get("stressRttMs")
    goodput = row.get("stressGoodputMbps")
    failure = row.get("stressFailureRate")
    if rtt is None or goodput is None or float(rtt) <= 0 or float(goodput) <= 0:
        return None
    # Dataplane summaries report RTT only for successful probes and goodput only
    # for completed flows.  Convert both to attempted-work quantities before
    # combining them: effective RTT grows as 1/success and effective goodput
    # falls as success.  The log ratio therefore contributes -2*log(success).
    # Keep a finite floor so a 100% failure remains serializable and visibly bad.
    success = max(1.0 - min(max(float(failure or 0), 0.0), 1.0), 1e-6)
    return math.log(float(rtt)) - math.log(float(goodput)) - 2.0 * math.log(success)
