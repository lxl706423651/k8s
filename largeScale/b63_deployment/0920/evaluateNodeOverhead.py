#!/usr/bin/env python3
# Held-out placement evaluation of frozen per-Pod node-overhead correction.
# Never updates coefficients from validation results or calls them physical components.
"""Compare node increment predictions to a new independently placed topology window."""
import argparse
import hashlib
import json
from pathlib import Path


def evaluateOverhead(modelPath,analysisPath,output):
    """Evaluate frozen `modelPath` on distinct completed placement `analysisPath`."""
    model=json.loads(modelPath.read_text());test=json.loads(analysisPath.read_text())
    testHash=hashlib.sha256(analysisPath.read_bytes()).hexdigest()
    if testHash==model['trainingAnalysisSha256']:raise ValueError('training is not independent validation')
    if test['modelSha256']!=model['fixtureModelSha256']:raise ValueError('fixture model changed')
    rows=[]
    for node in test['nodes']:
        pods=[p for p in test['pods'] if p['node']==node['node']];row=dict(node=node['node'],pods=len(pods))
        for metric,observedKey,intercept in [('cpuCores','nodeCpuIncreaseFromEmpty','additionalCpuIntercept'),
                ('memoryGiB','memoryProxyIncreaseFromEmptyGiB','additionalMemoryGiBIntercept')]:
            fixture=sum(p[metric]['predicted'] for p in pods)
            prediction=fixture+len(pods)*model[intercept];observed=node[observedKey]
            row[metric]=dict(fixtureOnly=fixture,prediction=prediction,observed=observed,
                signedError=prediction-observed,underprediction=observed>prediction)
        rows.append(row)
    result=dict(modelSha256=hashlib.sha256(modelPath.read_bytes()).hexdigest(),testAnalysisSha256=testHash,
        nodes=rows,independentDeploymentCount=1,
        note='One held-out placement; node measurements share a deployment. No independent-deployment probability certification.')
    with output.open('x') as handle:json.dump(result,handle,indent=2);handle.write('\n')
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ['model','analysis','output']:parser.add_argument('--'+name,type=Path,required=True)
    a=parser.parse_args();evaluateOverhead(a.model,a.analysis,a.output)
