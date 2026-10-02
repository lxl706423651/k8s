#!/usr/bin/env python3
# Held-out accuracy belongs to central predictions; admission uses a distinct lower surface.
"""Evaluate a frozen dual-response model without conflating conservatism and accuracy."""
import argparse
import hashlib
import json
from pathlib import Path

from fitResponseSurface import evaluateRows, selectValid
from solvePlacement import predictQuality


def evaluateDual(modelPath,raw,output):
    """Report central error and lower-envelope violations on independent `raw`."""
    model=json.loads(modelPath.read_text());rows,audit=selectValid(raw)
    used=[a['sha256'] for a in model['trainingAudits']]+[model['calibrationAudit']['sha256']]
    if not model['calibrated'] or audit['sha256'] in used:raise ValueError('not an independent test')
    if any(audit[k]!=model['trainingAudits'][0][k] for k in ['capacity','canaryAstSha256','measurementProtocol']):raise ValueError('test identity changed')
    if audit.get('pressureControl','legacy_per_cycle_cpu_budget')!=model['trainingAudits'][0].get('pressureControl','legacy_per_cycle_cpu_budget'):
        raise ValueError('test pressure controller changed')
    metrics=evaluateRows(model['nominal'],rows);predictions=metrics['predictions']
    for prediction,row in zip(predictions,rows):
        lower=predictQuality(model['lower'],row['cpuFraction'],row['memoryFraction'])
        if lower is None:raise ValueError('test outside admission domain')
        prediction.update(lowerQ=lower,predictedSafe=lower>=.8)
    safe=[p for p in predictions if p['predictedSafe']];blocks=[]
    for block in sorted({p['block'] for p in predictions}):
        selected=[p for p in safe if p['block']==block]
        blocks.append(dict(block=block,predictedSafePoints=len(selected),unsafeAmongPredictedSafe=sum(not p['observedSafe'] for p in selected),minimumObservedQ=min((p['observedQ'] for p in selected),default=None)))
    metrics.update(predictedSafePoints=len(safe),unsafeAmongPredictedSafe=sum(not p['observedSafe'] for p in safe),
        safeBlockReports=blocks,blocksWithPredictedSafePoints=sum(b['predictedSafePoints']>0 for b in blocks),
        blocksWithSafePredictionViolation=sum(b['unsafeAmongPredictedSafe']>0 for b in blocks),
        worstLowerBoundViolation=max(0,max(p['lowerQ']-p['observedQ'] for p in predictions)),
        note='Accuracy refers to central conditional-median surface; admission uses independently calibrated lower envelope. Blocks are not independent deployments.')
    result=dict(modelSha256=hashlib.sha256(modelPath.read_bytes()).hexdigest(),audit=audit,metrics=metrics,
        accuracyThresholds=dict(mae=.08,p95AbsoluteError=.15),accuracyPassed=metrics['mae']<=.08 and metrics['p95AbsoluteError']<=.15)
    with output.open('x') as handle:json.dump(result,handle,indent=2);handle.write('\n')
    print(json.dumps({k:v for k,v in metrics.items() if k!='predictions'},indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ['model','raw','output']:parser.add_argument('--'+name,type=Path,required=True)
    a=parser.parse_args();evaluateDual(a.model,a.raw,a.output)
