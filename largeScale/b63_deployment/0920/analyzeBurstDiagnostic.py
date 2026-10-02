#!/usr/bin/env python3
# Validate a completed burst-period manipulation and emit paired descriptive results.
# Reads raw batch/config/source, writes an exclusive JSON report; no fitting or VM changes.
"""Audit equal-allocation, mean-CPU-matched period experiments with fixed canary."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
from fitResponseSurface import selectValid


def analyzeBurst(raw, output):
    """Check all raw period/allocated-memory pairs before reporting paired Q differences."""
    rows,audit=selectValid(raw)
    records=[json.loads(line) for line in raw.read_text().splitlines()]
    records=[r for r in records if r.get('kind')=='pressure']
    periods={'resident_5ms':.005,'resident_50ms':.05}
    if {r['mode'] for r in records}!=set(periods):raise ValueError('unexpected manipulation modes')
    for r in records:
        if not r['children'] or any(c['periodSeconds']!=periods[r['mode']] for c in r['children']):
            raise ValueError('actual injection period differs from design')
    pairs=[]
    for block,cpu,memory in sorted({(r['block'],r['cpuFraction'],r['memoryFraction']) for r in rows}):
        match=lambda r:(r['block'],r['cpuFraction'],r['memoryFraction'])==(block,cpu,memory)
        observed=[r for r in records if match(r)]
        selected={r['mode']:r for r in rows if match(r)}
        if len(observed)!=2 or len({r['allocatedBytes'] for r in observed})!=1 or set(selected)!=set(periods):
            raise ValueError('pair allocation/identity mismatch')
        fast=selected['resident_5ms'];slow=selected['resident_50ms']
        pairs.append(dict(block=block,cpu=cpu,memory=memory,allocatedBytes=observed[0]['allocatedBytes'],
            q5ms=fast['q'],q50ms=slow['q'],difference50Minus5=slow['q']-fast['q'],
            actualCpu5ms=fast['actualPressureCpu'],actualCpu50ms=slow['actualPressureCpu']))
    groups=[]
    for cpu in sorted({r['cpu'] for r in pairs}):
        selected=[r for r in pairs if r['cpu']==cpu]
        groups.append(dict(cpu=cpu,blocks=len(selected),medianPairedDifference=statistics.median(r['difference50Minus5'] for r in selected),
            minimumQ5ms=min(r['q5ms'] for r in selected),minimumQ50ms=min(r['q50ms'] for r in selected),
            belowEta5ms=sum(r['q5ms']<.8 for r in selected),belowEta50ms=sum(r['q50ms']<.8 for r in selected)))
    result=dict(scope='isolated_bare_vm_diagnostic_not_target_worker_holdout',audit=audit,pairs=pairs,groups=groups,
        sourceSha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        note='Period is a manipulation, not a model feature. Mean CPU and allocated bytes matched; no peak CPU guarantee. One adverse event cannot establish a stable period effect or a unique mechanism. No deployment-risk inference.')
    with output.open('x') as f:json.dump(result,f,indent=2);f.write('\n')
    print(json.dumps(groups,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--raw',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();analyzeBurst(a.raw,a.output)
