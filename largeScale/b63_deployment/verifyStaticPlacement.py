#!/usr/bin/env python3
"""Validate B63 placement offline before image preload and deployment.

Inputs: run-local assignment, cluster inventory, baseline/deploy manifests and
placement report. Output: placement_static_summary.json (or --output).
Only reads saved inputs and writes the validation report; never contacts VMs.
"""
from __future__ import annotations

import argparse
import copy
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import statistics

import yaml

import runPlacementExperiment as placement


def compareValues(expected, actual, path: str = "root") -> list[str]:
    """Compare nested metrics, allowing only floating point roundoff."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        errors = []
        if expected.keys() != actual.keys():
            errors.append(f"{path}: keys differ")
        for key in expected.keys() & actual.keys():
            errors.extend(compareValues(expected[key], actual[key], f"{path}.{key}"))
        return errors
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return [f"{path}: lengths differ"]
        return [error for i, (left, right) in enumerate(zip(expected, actual))
                for error in compareValues(left, right, f"{path}[{i}]")]
    if type(expected) in (int, float) and type(actual) in (int, float):
        equal = (expected == actual if type(expected) is type(actual) is int else
                 math.isclose(expected, actual, rel_tol=1e-9, abs_tol=1e-9))
    else:
        equal = type(expected) is type(actual) and expected == actual
    return [] if equal else [f"{path}: expected {expected!r}, got {actual!r}"]


def summarizeCounts(counts: dict[str, int]) -> dict:
    """Summarize unweighted node counts with population variance (divide by N)."""
    values = list(counts.values())
    return {"perNode": counts, "nodeCount": len(values), "total": sum(values),
            "mean": statistics.mean(values), "variance": statistics.pvariance(values),
            "standardDeviation": statistics.pstdev(values), "min": min(values),
            "max": max(values)}


def validateStaticPlacement(source: list[dict], deployed: list[dict], report: dict,
                            nodes: list, args: argparse.Namespace) -> dict:
    """Check source/deployed docs and saved report against nodes and algorithm args.

    Reuse the placement model for metric recomputation; capacity/pressure scores
    are estimates, not additional hard admission thresholds.
    """
    failures = []
    networks = placement.parseNetworks(source)
    pods = placement.parsePods(source, networks)
    placement.assignNetworkWeights(networks, pods, args)
    placement.estimatePodResources(pods, networks, args)
    names = [pod.name for pod in pods]
    node_names = [node.name for node in nodes]
    selected_name = placement.normalizeAlgorithmName(args.algorithm)
    mapping = report["placements"][selected_name]
    if not pods or not nodes:
        failures.append("empty workload or node set")
    if len(set(names)) != len(names) or len(set(node_names)) != len(node_names):
        failures.append("duplicate workload or node names")
    if any(pod.replicas <= 0 or source[pod.docIndex].get('spec', {}).get('replicas', 1) <= 0
           for pod in pods):
        failures.append("workload replicas must be positive")
    if set(mapping) != set(names):
        failures.append("selected placement does not cover exactly the source workloads")
    eligible = {node.name for node in nodes if node.schedulable or args.include_master}
    if any(node not in eligible for node in mapping.values()):
        failures.append("selected placement references an unknown or excluded node")
    if report['metadata']['selectedAlgorithm'] != selected_name:
        failures.append("selected algorithm differs from frozen configuration")
    if report['metadata'].get('includeMaster', args.include_master) != args.include_master:
        failures.append("node eligibility differs from frozen configuration")
    failures.extend(compareValues([asdict(pod) | {"nicCount": pod.nicCount} for pod in pods],
                                  report['pods'], 'pods'))
    failures.extend(compareValues({n: asdict(v) for n, v in networks.items()},
                                  report['networks'], 'networks'))
    # Node addresses/SSH details are not placement inputs; compare resource/role fields.
    fields = ('name', 'role', 'cpu', 'memoryMb', 'schedulable')
    failures.extend(compareValues(
        {n.name: {k: getattr(n, k) for k in fields} for n in nodes},
        {n['name']: {k: n[k] for k in fields} for n in report['nodes']}, 'nodes'))
    metrics = {}
    distributions = {}
    if not failures:
        expected_docs = copy.deepcopy(source)
        default_scheduler = selected_name == 'kubernetes-default-scheduler'
        placement.injectPlacement(expected_docs, pods, mapping,
                                  inject_resources=args.inject_resource_requests,
                                  pinning_mode='none' if default_scheduler else args.pinning_mode)
        if default_scheduler:
            from runDefaultSchedulerBaseline import prepareSchedulerManifest
            prepareSchedulerManifest(expected_docs, pods[0].namespace,
                                     None if args.include_master else [n.name for n in nodes if n.name in eligible])
        if expected_docs != deployed:
            failures.append("deployed manifest differs from source plus permitted placement injection")
        baseline = placement.readExistingPlacement(pods, source)
        candidates = {selected_name: mapping}
        if baseline:
            candidates['manifest-existing'] = baseline
            failures.extend(compareValues(baseline, report['placements'].get('manifest-existing'),
                                          'baselinePlacement'))
        for name, candidate in candidates.items():
            if set(candidate) != set(names) or not set(candidate.values()) <= set(node_names):
                failures.append(f"{name}: incomplete placement or unknown node")
                continue
            metric = placement.evaluatePlacementWithArgs(pods, nodes, networks, candidate, args)
            metrics[name] = metric
            failures.extend(compareValues(metric, report['staticValidation'].get(name),
                                          f'staticValidation.{name}'))
            distributions[name] = {
                'pods': summarizeCounts({n: v['podCount'] for n, v in metric['perNode'].items()}),
                'vlanParents': summarizeCounts(metric['fabricMetrics']['perNodeVlanParents']),
            }
    digest = hashlib.sha256(json.dumps(mapping, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'status': 'FAIL' if failures else 'PASS', 'success': not failures,
            'algorithm': selected_name, 'podCount': sum(pod.replicas for pod in pods),
            'placementKind': ('scheduler-simulation' if selected_name == 'kubernetes-default-scheduler'
                              else 'hard-placement'),
            'workloadCount': len(pods), 'nodeCount': len(nodes),
            'placementSha256': digest, 'distributions': distributions,
            'recomputedMetrics': metrics, 'failures': failures,
            'varianceDefinition': 'population variance; unweighted nodes; divide by N'}


def loadRunInputs(run: Path) -> tuple:
    """Read frozen config/model inputs from run; never query the live cluster."""
    from runB63LargeExperiment import createPlacementArgs
    config = yaml.safe_load((run / 'assignment.yaml').read_text())
    args = placement.parseArgs(createPlacementArgs(config))
    args.inventory = run / 'inventory.yaml'
    args.config_k3s = run / 'configK3s.yaml'
    args.kvm_config = run / 'cluster-input.yaml'
    return (placement.loadYamlDocs(run / 'output/k8s.b62.yaml'),
            placement.loadYamlDocs(run / 'output/k8s.yaml'),
            json.loads((run / 'output/placement_report.json').read_text()),
            placement.loadNodeInfo(args), args)


def main() -> int:
    """Resolve saved run inputs and always persist a PASS/FAIL validation result."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    cli = parser.parse_args()
    run = cli.run_dir.resolve()
    output = cli.output or run / 'placement_static_summary.json'
    try:
        result = validateStaticPlacement(*loadRunInputs(run))
    except (Exception, SystemExit) as exc:
        result = {'status': 'FAIL', 'success': False, 'failures': [str(exc)]}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
    print(json.dumps({k: v for k, v in result.items() if k not in
                      {'recomputedMetrics', 'distributions'}}, indent=2))
    return 0 if result['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
