#!/usr/bin/env python3
# Supplemental short probe audit; never replaces the rejected default-scheduler window.
# Uses archived fixed-canary implementation and verifies the full topology stayed identical.
"""Normalize three explicitly separate zero-injection diagnostic probe observations."""
import hashlib
import json
import math
from pathlib import Path
import statistics

from analyzeRoutingCanary import readWindow


def evaluateSupplement():
    """Audit supplemental c4m2 observations against completed empty references."""
    root=Path('results');folder=root/'default-zero-pressure-diagnostic';completion=json.loads((folder/'complete.json').read_text())
    if completion['returnCode'] or not completion['fullTopologyIdentityStable']:raise ValueError('supplemental identity/completion failed')
    before,identity,preHash=readWindow(root/'c4m2-default-empty-before','c4m2')
    after,other,postHash=readWindow(root/'c4m2-default-empty-after','c4m2')
    if identity!=other:raise ValueError('reference identities differ')
    records=[json.loads(l) for l in (folder/'raw.jsonl').read_text().splitlines()]
    if records[-1]['kind']!='complete':raise ValueError('short probe incomplete')
    if records[0]['sourceSha256']!=hashlib.sha256((root/'c4m2-default-empty-before/measurePressure.py').read_bytes()).hexdigest():raise ValueError('probe source differs')
    samples=[r for r in records if r['kind']=='probe']
    if len(samples)!=3:raise ValueError('expected three supplemental samples')
    for sample in samples:
        for key in ['before','after']:
            observed=[len(sample[key]['perCpuTicks']),sample[key]['memory']['MemTotal']]
            if observed!=list(identity['capacity'][0]):raise ValueError('supplemental capacity differs')
    refs=[{key:math.exp(statistics.mean(math.log(r['canary'][key]) for r in group)) for key in ['throughput','p95Seconds']} for group in [before,after]]
    centers=[statistics.mean(r['timestamp'] for r in group) for group in [before,after]]
    drift=max(abs(refs[1][k]/refs[0][k]-1) for k in refs[0]);observations=[]
    stages=json.loads((root/'default-scheduler-stages.json').read_text())
    for sample in samples:
        if not max(r['timestamp'] for r in before)<sample['timestamp']<min(r['timestamp'] for r in after):raise ValueError('references do not bracket probe')
        fraction=(sample['timestamp']-centers[0])/(centers[1]-centers[0])
        reference={k:math.exp((1-fraction)*math.log(refs[0][k])+fraction*math.log(refs[1][k])) for k in refs[0]}
        q=min(sample['canary']['throughput']/reference['throughput'],reference['p95Seconds']/sample['canary']['p95Seconds'])
        start=sample['timestamp']-sample['canary']['seconds']
        overlap=[s['stage'] for s in stages if 'protocol' in s['stage'] and s['started']<sample['timestamp'] and s.get('finished',s['started'])>start]
        observations.append(dict(index=sample['index'],q=q,timestamp=sample['timestamp'],overlappingProtocolStages=overlap))
    result=dict(scope='supplementary_three_window_probe_not_original_control_test',valid=drift<=.2,
        minimumQ=min(r['q'] for r in observations),maximumReferenceDrift=drift,observations=observations,
        rawSha256=hashlib.sha256((folder/'raw.jsonl').read_bytes()).hexdigest(),referenceSha256=[preHash,postHash],
        originalWindowStatus='rejected_memory_headroom_all_15_observations',
        note='Uses separate 64MiB fixed-probe guard, not the original 20% injection guard. Protocol-check overlap is reported. Does not replace the rejected control window or add independent deployments.')
    with (folder/'evaluation.json').open('x') as handle:json.dump(result,handle,indent=2);handle.write('\n')
    print(json.dumps(result,indent=2))


if __name__=='__main__':evaluateSupplement()
