#!/usr/bin/env python3
"""Read live Pods and compare every workload with the B63 placement report.

Inputs: run directory, kubeconfig and namespace. Output: placement_actual_summary.json.
Reads Kubernetes only; run after the shared wait-ready stage and before BIRD.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import subprocess


def validatePlacement(report: dict, pods: list[dict]) -> dict:
    """Compare report's selected mapping/replicas with live pods, including readiness."""
    default_scheduler = report['metadata']['selectedAlgorithm'] == 'kubernetes-default-scheduler'
    selected = report["placements"][report["metadata"]["selectedAlgorithm"]]
    expected = {pod["name"]: pod["replicas"] for pod in report["pods"]}
    seen = Counter()
    counts = Counter({n['name']: 0 for n in report.get('nodes', [])})
    eligible = {n['name'] for n in report.get('nodes', [])
                if n['schedulable'] or report['metadata'].get('includeMaster', False)}
    actual_mapping = {}
    failures = []
    for pod in pods:
        metadata = pod.get("metadata", {})
        labels = metadata.get("labels", {})
        name = labels.get("app", metadata.get("name"))
        actual = pod.get("spec", {}).get("nodeName")
        seen[name] += 1
        counts[actual] += 1
        ready = any(c.get("type") == "Ready" and c.get("status") == "True"
                    for c in pod.get("status", {}).get("conditions", []))
        spec = pod.get('spec', {})
        valid_node = (actual in eligible if default_scheduler else selected.get(name) == actual)
        scheduler_ok = (not default_scheduler or
                        (spec.get('schedulerName', 'default-scheduler') == 'default-scheduler'
                         and not spec.get('nodeSelector', {}).get('kubernetes.io/hostname')))
        if name not in selected or not valid_node or not scheduler_ok or not ready or metadata.get("deletionTimestamp"):
            failures.append({"pod": metadata.get("name"), "workload": name,
                             "expectedNode": None if default_scheduler else selected.get(name), "actualNode": actual,
                             "ready": ready})
        if name in actual_mapping and actual_mapping[name] != actual:
            failures.append({'workload': name, 'reason': 'replicas span nodes; model requires one node per workload'})
        actual_mapping[name] = actual
    for name, count in expected.items():
        if seen[name] != count:
            failures.append({"workload": name, "expectedReplicas": count, "actualReplicas": seen[name]})
    return {"status": "FAIL" if failures else "PASS", "podCount": len(pods),
            "expectedPodCount": sum(expected.values()), "perNode": dict(counts), "failures": failures,
            "placementKind": "observed-scheduler" if default_scheduler else "verified-hard-placement",
            "actualPlacement": actual_mapping}


def evaluateActualPlacement(run: Path, mapping: dict) -> dict:
    """Recompute Pod/VLAN model metrics for observed mapping using frozen run inputs."""
    import runPlacementExperiment as placement
    from verifyStaticPlacement import loadRunInputs, summarizeCounts
    source, _, _, nodes, args = loadRunInputs(run)
    networks = placement.parseNetworks(source)
    workloads = placement.parsePods(source, networks)
    placement.assignNetworkWeights(networks, workloads, args)
    placement.estimatePodResources(workloads, networks, args)
    metrics = placement.evaluatePlacementWithArgs(workloads, nodes, networks, mapping, args)
    return {'metrics': metrics, 'distributions': {
        'pods': summarizeCounts({n: v['podCount'] for n, v in metrics['perNode'].items()}),
        'vlanParents': summarizeCounts(metrics['fabricMetrics']['perNodeVlanParents']),
    }}


def main() -> int:
    """Fetch scoped live Pods and persist the placement comparison."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--namespace", required=True)
    args = parser.parse_args()
    report = json.loads((args.run_dir / "output/placement_report.json").read_text())
    result = subprocess.run(["kubectl", "--kubeconfig", args.kubeconfig,
                             "--request-timeout=120s", "-n", args.namespace,
                             "get", "pods", "-l", "seedemu.io/workload=seedemu", "-o", "json"],
                            check=True, capture_output=True, text=True, timeout=150)
    summary = validatePlacement(report, json.loads(result.stdout)["items"])
    if summary['status'] == 'PASS':
        try:
            summary.update(evaluateActualPlacement(args.run_dir, summary['actualPlacement']))
        except Exception as exc:
            summary['status'] = 'FAIL'
            summary['failures'].append({'reason': f'actual metric evaluation failed: {exc}'})
    (args.run_dir / "placement_actual_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
