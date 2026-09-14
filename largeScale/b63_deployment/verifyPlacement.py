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
    selected = report["placements"][report["metadata"]["selectedAlgorithm"]]
    expected = {pod["name"]: pod["replicas"] for pod in report["pods"]}
    seen = Counter()
    counts = Counter()
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
        if name not in selected or selected[name] != actual or not ready or metadata.get("deletionTimestamp"):
            failures.append({"pod": metadata.get("name"), "workload": name,
                             "expectedNode": selected.get(name), "actualNode": actual,
                             "ready": ready})
    for name, count in expected.items():
        if seen[name] != count:
            failures.append({"workload": name, "expectedReplicas": count, "actualReplicas": seen[name]})
    return {"status": "FAIL" if failures else "PASS", "podCount": len(pods),
            "expectedPodCount": sum(expected.values()), "perNode": dict(counts), "failures": failures}


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
    (args.run_dir / "placement_actual_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
