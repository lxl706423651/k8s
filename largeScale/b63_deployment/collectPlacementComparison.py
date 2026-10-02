#!/usr/bin/env python3
"""Collect serial placement results from a runPlacementComparison directory.

Reads preserved JSON/log artifacts only; no Kubernetes or VM access. Writes
comparison_summary.json, comparison_metrics.csv, comparison_stages.csv and
comparison.md, including partial/failed runs and measurement sample checks.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import re
import statistics


def readJson(path: Path) -> dict | list:
    """Load JSON path if available, retaining incomplete artifacts as unavailable."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


def readMetric(data: dict, *keys):
    """Read nested keys from data, returning None for missing measurements."""
    value = data
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def readProvisionedVlans(run: Path, node_names: list[str]) -> dict:
    """Parse successful deploy's planned parent counts, including zero-count nodes."""
    log = run / 'deploy.orchestrator.log'
    if not log.exists() or not node_names:
        return {}
    counts = {name: 0 for name in node_names}
    matches = re.findall(r'\[vlan-cni\] node=(\S+) ip=\S+ planned=(\d+)', log.read_text())
    if not matches:
        return {}
    for name, count in matches:
        counts[name] = int(count)
    values = list(counts.values())
    return {'perNode': counts, 'mean': statistics.mean(values),
            'variance': statistics.pvariance(values), 'total': sum(values)}


def createSampleSignature(run: Path) -> dict | None:
    """Record logical endpoints and per-round samples, excluding runtime placement."""
    data = readJson(run / 'dataplane/latency_throughput_summary.json')
    directions = readJson(run / 'dataplane/resolved_directions.json')
    baseline = readJson(run / 'dataplane/iperf_baseline_results.json')
    if not data or not isinstance(directions, list) or not isinstance(baseline, list):
        return None
    def endpoints(row):
        return (row['directionId'], row['stratum'], row['src']['id'], row['src']['targetIp'],
                row['dst']['id'], row['dst']['targetIp'])
    return {'topologyHash': data['metadata']['topologyHash'],
            'pairPlanVersion': data['metadata']['pairPlanVersion'],
            'pairSeed': data['metadata']['pairSeed'],
            'directions': sorted(endpoints(r) for r in directions),
            'baseline': sorted(r['directionId'] for r in baseline),
            'stress': [{'level': r['level'], 'repetition': r['repetition'],
                        'directions': r['selectedDirections'], 'canaries': r['canaryDirections']}
                       for r in data['stress']['rounds']]}


def writeCsv(path: Path, rows: list[dict]) -> None:
    """Write heterogeneous rows to path; missing values remain empty, never zero."""
    with path.open('w') as handle:
        if not rows:
            return
        fields = list(dict.fromkeys(k for row in rows for k in row))
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def collectComparison(root: Path) -> dict:
    """Collect all entries in root/comparison_runs.json, including interrupted runs."""
    progress = readJson(root / 'comparison_runs.json')
    rows, stages, details, signatures = [], [], [], {}
    for entry in progress.get('runs', []):
        run = Path(entry['runDir'])
        summary = readJson(run / 'summary.json')
        static = readJson(run / 'placement_static_summary.json')
        actual = readJson(run / 'placement_actual_summary.json')
        dataplane = readJson(run / 'dataplane/latency_throughput_summary.json')
        report = readJson(run / 'output/placement_report.json')
        algorithm = entry['algorithm']
        # Real K8s placement is only known after readiness. Never substitute its simulation.
        dist = actual.get('distributions', {}) if actual.get('status') == 'PASS' else {}
        provisioned = readProvisionedVlans(run, [n['name'] for n in report.get('nodes', [])])
        if not any(s['name'] == 'deploy' and s['status'] == 'PASS' for s in summary.get('stages', [])):
            provisioned = {}
        row = {'algorithm': algorithm, 'status': entry['status'],
               'cleanupStatus': entry['cleanupStatus'], 'runDir': str(run),
               'staticStatus': static.get('status'), 'actualStatus': actual.get('status'),
               'staticPlacementKind': static.get('placementKind'),
               'podMean': readMetric(dist, 'pods', 'mean'),
               'podVariance': readMetric(dist, 'pods', 'variance'),
               'usedVlanParentMean': readMetric(dist, 'vlanParents', 'mean'),
               'usedVlanParentVariance': readMetric(dist, 'vlanParents', 'variance'),
               'provisionedVlanParentMean': provisioned.get('mean'),
               'provisionedVlanParentVariance': provisioned.get('variance'),
               'pingOk': readMetric(dataplane, 'ping', 'ok'),
               'pingTotal': readMetric(dataplane, 'ping', 'total'),
               'pingMeanRttMs': readMetric(dataplane, 'ping', 'avgRttMs', 'avg'),
               'pingP95RttMs': readMetric(dataplane, 'ping', 'avgRttMs', 'p95'),
               'baselineTcpOk': readMetric(dataplane, 'baselineTcpIperf', 'ok'),
               'baselineTcpTotal': readMetric(dataplane, 'baselineTcpIperf', 'total'),
               'baselineTcpMeanMbps': readMetric(dataplane, 'baselineTcpIperf', 'flowMbps', 'avg')}
        for level in (8, 16, 32, 48):
            for key, label in [('aggregateGoodputMbps', 'MeanTotalMbps'), ('failureRate', 'FailureRate'),
                               ('loadedPingAvgRttMs', 'LoadedMeanRttMs'), ('fairness', 'Fairness')]:
                row[f'stress{level}{label}'] = readMetric(dataplane, 'stress', 'byLevel', str(level), key, 'avg')
        rows.append(row)
        for stage in summary.get('stages', []):
            stages.append({'algorithm': algorithm, 'stage': stage['name'], 'status': stage['status'],
                           'durationSeconds': stage.get('durationSeconds')})
        details.append({'algorithm': algorithm, 'actualDistributions': dist,
                        'provisionedVlanParents': provisioned,
                        'staticSimulationOrPlan': static.get('distributions', {}),
                        'stressRounds': dataplane.get('stress', {}).get('rounds', [])})
        signatures[algorithm] = createSampleSignature(run)
    available = [s for s in signatures.values() if s is not None]
    sample_status = ('INCOMPLETE' if len(available) != len(progress.get('algorithms', [])) or not available else
                     'MATCH' if all(s == available[0] for s in available) else 'MISMATCH')
    result = {'status': progress.get('status'), 'sampleComparison': sample_status,
              'rows': rows, 'details': details,
              'notes': ['Default-scheduler static results are simulations; distributions use observed placement.',
                        'VLAN used counts are inferred from observed Pods; provisioned counts come from deploy plans.',
                        'RTT describes successful-direction averages; baseline TCP is batched, not 48 concurrent flows.',
                        'Workflow PASS does not mean every measured flow succeeded.']}
    (root / 'comparison_summary.json').write_text(json.dumps(result, indent=2, ensure_ascii=False)+'\n')
    writeCsv(root / 'comparison_metrics.csv', rows)
    writeCsv(root / 'comparison_stages.csv', stages)
    def fmt(v):
        return '—' if v is None else f'{v:.3f}' if isinstance(v, float) else str(v)
    lines = ['# 分区算法串行实验比较', '', f'批次状态：{result["status"]}；动态测试方向核对：{sample_status}。', '',
             '| 算法 | 状态 | Pod 均值 / 方差 | 已配置 VLAN 均值 / 方差 | ping 成功/总数 | RTT ms | TCP 成功/总数 | TCP 均值 Mbps | stress48 总 Mbps | stress48 失败率 |',
             '|---|---|---|---|---|---:|---|---:|---:|---:|']
    for r in rows:
        lines.append(f'| {r["algorithm"]} | {r["status"]} | {fmt(r["podMean"])} / {fmt(r["podVariance"])} | '
                     f'{fmt(r["provisionedVlanParentMean"])} / {fmt(r["provisionedVlanParentVariance"])} | '
                     f'{fmt(r["pingOk"])}/{fmt(r["pingTotal"])} | {fmt(r["pingMeanRttMs"])} | '
                     f'{fmt(r["baselineTcpOk"])}/{fmt(r["baselineTcpTotal"])} | {fmt(r["baselineTcpMeanMbps"])} | '
                     f'{fmt(r["stress48MeanTotalMbps"])} | {fmt(r["stress48FailureRate"])} |')
    lines += ['', '默认调度的静态分配是模型估计，真实分布取自 placement_actual_summary.json。未完成或未测量字段为空，不计为 0。',
              '', '默认调度需要在所有节点预加载镜像，并预先配置可能使用的 VLAN；已配置 VLAN 数与 Pod 实际使用的 VLAN 数分别记录。',
              '', 'RTT/P95 基于成功方向的平均 RTT；基线 TCP 为分批测量的成功流均值；stress 每档统计三轮，失败率单位为比例。',
              '', 'sampleComparison=MISMATCH 时测试方向不同，不应直接将性能差异归因于分区算法；INCOMPLETE 表示尚未收齐测量。',
              '', '各阶段耗时见 comparison_stages.csv；逐节点分布和逐轮 stress 见 comparison_summary.json。',
              '', '| 算法 | 阶段 | 状态 | 秒 |', '|---|---|---|---:|']
    lines += [f'| {r["algorithm"]} | {r["stage"]} | {r["status"]} | {fmt(r["durationSeconds"])} |' for r in stages]
    (root / 'comparison.md').write_text('\n'.join(lines)+'\n')
    return result


def main() -> int:
    """Regenerate an existing batch's comparison files without touching its cluster."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    args = parser.parse_args()
    root = args.run_dir.expanduser().resolve()
    if not (root / 'comparison_runs.json').is_file():
        parser.error('missing comparison_runs.json')
    result = collectComparison(root)
    print(f'{root / "comparison.md"}: {result["status"]}, samples={result["sampleComparison"]}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
