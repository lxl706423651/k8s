#!/usr/bin/env python3
# Evaluate a frozen bare-VM surface against a completed K3s diagnostic batch.
# Records capacity differences explicitly; never changes a model or claims a holdout pass.
"""Measure cross-environment prediction error before any capacity-axis conversion."""
import argparse
import hashlib
import json
from pathlib import Path

from fitResponseSurface import selectValid,readFrozenAudit,evaluateRows


def evaluateTransfer(model,raw,output):
    """Compare immutable `model` to complete diagnostic `raw`; write `output` JSON."""
    bundle=json.loads(model.read_text());frozen=readFrozenAudit(bundle,model)
    manifest=json.loads((raw.parent.parent/'manifest.json').read_text())
    if not manifest.get('completed') or any(p['status']!='completed' or p['transferReturnCode'] for p in manifest['profiles']):
        raise ValueError('transfer batch is incomplete or failed')
    rows,audit=selectValid(raw)
    for key in ['canaryAstSha256','measurementProtocol']:
        if frozen[key]!=audit[key]:raise ValueError('transfer probe/protocol differs from fitted model')
    if audit['sha256'] in [bundle['trainingAudit']['sha256'],bundle['calibrationAudit']['sha256']]:
        raise ValueError('transfer reused source fit data')
    records=[json.loads(line) for line in raw.read_text().splitlines()]
    config=next(r['config'] for r in records if r['kind']=='metadata')
    expected=config['repeats']*len(config['cpu'])*len(config['memory'])*len(config['modes'])
    metrics=evaluateRows(bundle['surface'],rows)
    ratios=[b/a for a,b in zip(frozen['capacity'],audit['capacity'])]
    report=dict(scope='bare_vm_to_k3s_diagnostic_not_final_deployment_validation',
        modelSha256=hashlib.sha256(model.read_bytes()).hexdigest(),frozenModelStatus=bundle['status'],
        originalCapacity=frozen['capacity'],targetCapacity=audit['capacity'],capacityRatios=ratios,
        exactCapacityMatch=frozen['capacity']==audit['capacity'],audit=audit,expectedPoints=expected,
        allPointsValid=len(rows)==expected,metrics=metrics,
        errorThresholdsMetOnValidPoints=metrics['mae']<=.08 and metrics['p95AbsoluteError']<=.15,
        note='Compared at each batch injection/guest-capacity coordinates. Background, K3s overhead and effective Pod budgets still require accounting. Passing diagnostic errors does not certify deployment or revive an ineligible surface.')
    with output.open('x') as handle:handle.write(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:report[k] for k in ['frozenModelStatus','capacityRatios','allPointsValid','errorThresholdsMetOnValidPoints']}))
    print(json.dumps({k:v for k,v in metrics.items() if k not in ['predictions','safeBlockReports']}))


def executeCli():
    """Require frozen model, completed transfer data and result paths."""
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ['model','raw','output']:parser.add_argument('--'+key,type=Path,required=True)
    args=parser.parse_args();evaluateTransfer(args.model,args.raw,args.output)


if __name__=='__main__':executeCli()
