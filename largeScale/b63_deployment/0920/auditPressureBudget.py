#!/usr/bin/env python3
# Report measured versus declared injected CPU for preserved complete pressure batches.
# Diagnostic for legacy data; never edits frozen model errors or silently filters test rows.
"""Audit work-chunk CPU overshoot before interpreting budget-consistency experiments."""
import argparse
import hashlib
import json
from pathlib import Path

from analyzeCalibration import reduceProfile


def auditBudget(raw,output):
    """Audit complete `raw`; apply recorded tolerance or label the legacy post-hoc diagnostic."""
    rows,blocks=reduceProfile(raw);records=[json.loads(l) for l in raw.read_text().splitlines()]
    config=next(r['config'] for r in records if r.get('kind')=='metadata')
    tolerance=config.get('budgetTrackingTolerance',dict(relative=.02,absoluteCpuFraction=.0001))
    observations=[]
    for row in rows:
        if row['status']!='ok':
            observations.append(dict(block=row['block'],mode=row['mode'],cpu=row['cpuFraction'],memory=row['memoryFraction'],status=row['status']));continue
        requested=row['cpuFraction'];actual=row['actualPressureCpu'];allowed=max(tolerance['absoluteCpuFraction'],tolerance['relative']*requested)
        observations.append(dict(block=row['block'],mode=row['mode'],cpu=requested,memory=row['memoryFraction'],
            actualCpuFraction=actual,ratio=actual/requested if requested else None,q=row['q'],status='ok',
            overBudget=actual-requested>allowed,underTarget=requested-actual>allowed,
            validReference=row['validForFit']))
    groups={}
    for mode in sorted({r['mode'] for r in observations}):
        selected=[r for r in observations if r['mode']==mode and r['status']=='ok']
        groups[mode]=dict(rows=len(selected),overBudget=sum(r['overBudget'] for r in selected),underTarget=sum(r['underTarget'] for r in selected),
            maximumPositiveFractionError=max((r['actualCpuFraction']-r['cpu'] for r in selected),default=None),
            maximumRatio=max((r['ratio'] for r in selected if r['ratio'] is not None),default=None))
    result=dict(rawSha256=hashlib.sha256(raw.read_bytes()).hexdigest(),pressureControl=config.get('pressureControl','legacy_per_cycle_cpu_budget'),
        tolerance=tolerance,legacyToleranceIsPostHoc='budgetTrackingTolerance' not in config,blocks=blocks,groups=groups,observations=observations,
        note='Actual CPU uses child process CPU/wall rates across warmup plus sampling. Legacy over-budget rows remain in original test metrics; they cannot establish equal-actual-budget transfer failure.')
    with output.open('x') as handle:json.dump(result,handle,indent=2);handle.write('\n')
    print(json.dumps(groups,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ['raw','output']:parser.add_argument('--'+name,type=Path,required=True)
    a=parser.parse_args();auditBudget(a.raw,a.output)
