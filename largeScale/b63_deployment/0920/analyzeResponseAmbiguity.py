#!/usr/bin/env python3
# Post-test diagnostic only; quantifies observed conflicts at identical PDF inputs.
# Does not alter frozen models or pretend finite observations prove population limits.
"""Audit deterministic response ambiguity when only aggregate CPU/memory are inputs."""
import argparse
import hashlib
import json
from pathlib import Path
import statistics


def countUnavoidableErrors(observations, tolerance):
    """Count minimum errors above `tolerance` for any single prediction of `observations`."""
    values=sorted(observations)
    # All points within tolerance of a scalar lie in one interval of width 2*tolerance.
    right=0;maximum=0
    for left,value in enumerate(values):
        while right<len(values) and values[right]<=value+2*tolerance+1e-12:right+=1
        maximum=max(maximum,right-left)
    return len(values)-maximum



def analyzeAmbiguity(evaluation,output,raw=None):
    """Group completed evaluation or raw-budget observations; never fit a deployable model."""
    if raw is not None:
        from fitResponseSurface import selectValid
        selected,audit=selectValid(raw)
        rows=[dict(cpu=r['cpuFraction'],memory=r['memoryFraction'],observedQ=r['q'],mode=r['mode'],actualCpu=r['actualPressureCpu']) for r in selected]
        scope='post_hoc_raw_input_ambiguity_diagnostic';source=raw
    else:
        data=json.loads(evaluation.read_text());rows=data['metrics']['predictions'];audit=data.get('audit')
        scope='post_test_input_ambiguity_diagnostic';source=evaluation
    groups=[]
    for cpu,memory in sorted({(r['cpu'],r['memory']) for r in rows}):
        selected=[r for r in rows if r['cpu']==cpu and r['memory']==memory]
        qs=[r['observedQ'] for r in selected];median=statistics.median(qs)
        groups.append(dict(cpu=cpu,memory=memory,count=len(qs),minimumQ=min(qs),maximumQ=max(qs),
            medianQ=median,minimumInSampleAbsoluteErrorSum=sum(abs(q-median) for q in qs),
            minimumPossibleWorstAbsoluteError=(max(qs)-min(qs))/2,
            minimumErrorsAbovePoint15=countUnavoidableErrors(qs,.15),
            actualCpuRange=[min(r['actualCpu'] for r in selected),max(r['actualCpu'] for r in selected)] if all('actualCpu' in r for r in selected) else None,
            thresholdDisagreement=min(qs)<.8<=max(qs),modes=sorted({r['mode'] for r in selected}),
            observations=qs))
    result=dict(scope=scope,sourceSha256=hashlib.sha256(source.read_bytes()).hexdigest(),audit=audit,groups=groups,
        minimumInSampleMaeForAnyDeterministicBudgetOnlyFunction=sum(g['minimumInSampleAbsoluteErrorSum'] for g in groups)/len(rows),
        minimumErrorsAbovePoint15ForAnyDeterministicBudgetOnlyFunction=sum(g['minimumErrorsAbovePoint15'] for g in groups),
        observations=len(rows),
        conflictingInputPairs=sum(g['thresholdDisagreement'] for g in groups),
        note='Oracle per-input medians and half-ranges use observed outcomes: finite-sample descriptive bounds, not trained predictors or population impossibility theorems. Inputs are commanded budgets; inspect actual tracking before interpreting equivalence.')
    with output.open('x') as handle:json.dump(result,handle,indent=2);handle.write('\n')
    print(json.dumps({k:v for k,v in result.items() if k!='groups'},indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);source=parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--evaluation',type=Path);source.add_argument('--raw',type=Path);parser.add_argument('--output',type=Path,required=True)
    a=parser.parse_args();analyzeAmbiguity(a.evaluation,a.output,a.raw)
