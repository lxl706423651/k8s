#!/usr/bin/env python3
# Evaluate a frozen resource regression on independently collected container cases.
# Reports convex-hull support and underbudget observations; never refits on test rows.
"""Audit independent Pod-demand prediction errors and empirical upper allowances."""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from scipy.optimize import linprog
from analyzeRouting import validateGate

FEATURES=['intercept','nnet','niBGP','neBGP','nOSPF']


def checkSupport(features,support):
    """Return whether four-dimensional `features` lie in the training convex hull."""
    points=np.asarray(support,dtype=float)
    result=linprog(np.zeros(len(points)),A_eq=np.vstack([points.T,np.ones(len(points))]),
                   b_eq=np.r_[features,1.],bounds=(0,None),method='highs')
    return bool(result.success)


def evaluateDemand(modelPath,raw,output):
    """Evaluate frozen `modelPath` against raw independent cases, writing `output`."""
    model=json.loads(modelPath.read_text())
    if model['scope']!='seed_router_container_cgroup':
        raise ValueError('full container cgroup model required')
    if hashlib.sha256(raw.read_bytes()).hexdigest()==model['rawSha256']:
        raise ValueError('cannot use training data as test')
    metadata=json.loads((raw.parent/'metadata.json').read_text())
    if metadata['image']!=model['imageDigest']:
        raise ValueError('test image differs from calibrated image')
    rows=[json.loads(line) for line in raw.read_text().splitlines()]
    if any(r['status']!='ok' or r['scope']!=model['scope'] for r in rows):
        raise ValueError('invalid or incompatible resource samples')
    predictions=[]
    for row in rows:
        validateGate(row['postGate'],row['case'])
        files=list(raw.parent.glob('*-'+row['case']['id']+'/protocol_gate.json'))
        if len(files)!=1:raise ValueError('missing initial protocol evidence')
        gates=json.loads(files[0].read_text())
        validateGate(gates['target'],row['case'])
        validateGate(gates['peer'],row['case'],peer=True)
        features=[row['case'][k] for k in FEATURES[1:]]
        supported=checkSupport(features,model['supportFeatures']) and row['case']['routes'] in model['routeScales']
        p=dict(case=row['case']['id'],supported=supported,features=features)
        for metric in ('cpuCores','memoryGiB'):
            params=model['metrics'][metric]
            nominal=float(np.dot([1]+features,[params['coefficients'][k] for k in FEATURES]))
            upper=nominal+params['maximumUnderpredictionCvResidual']
            p[metric]=dict(observed=row[metric],prediction=nominal,upper=upper,underbudget=row[metric]>upper)
        predictions.append(p)
    summary={}
    for metric in ('cpuCores','memoryGiB'):
        for group in ('all','supported'):
            selected=[r[metric] for r in predictions if group=='all' or r['supported']]
            errors=[abs(r['prediction']-r['observed']) for r in selected]
            summary[metric+'_'+group]=dict(count=len(selected),mae=float(np.mean(errors)) if errors else None,
                relativeMae=float(np.mean([abs(r['prediction']-r['observed'])/max(r['observed'],1e-12) for r in selected])) if selected else None,
                p95AbsoluteError=float(np.quantile(errors,.95)) if errors else None,
                underbudgetCount=sum(r['underbudget'] for r in selected))
    output.parent.mkdir(parents=True,exist_ok=True)
    result=dict(modelSha256=hashlib.sha256(modelPath.read_bytes()).hexdigest(),testSha256=hashlib.sha256(raw.read_bytes()).hexdigest(),
                summary=summary,predictions=predictions,
                note='CV residual allowance is empirical, not a universal probabilistic bound. Out-of-support observations remain reported.')
    output.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(summary,indent=2))


def executeCli():
    """Require frozen model, independent raw data and output JSON paths."""
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('model','raw','output'):parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args();evaluateDemand(args.model,args.raw,args.output)


if __name__=='__main__':
    executeCli()
