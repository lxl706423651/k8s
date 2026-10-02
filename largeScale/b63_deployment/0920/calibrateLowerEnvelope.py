#!/usr/bin/env python3
# Fresh calibration of a frozen training-only response envelope.
# Preserves model identity and rejects source reuse, environment changes and unsafe baseline.
"""Add an empirical independent block margin to a restricted-domain lower envelope."""
import argparse
import copy
import hashlib
import json
from pathlib import Path

from fitResponseSurface import calibrateMargin, selectValid
from solvePlacement import predictQuality


def calibrateEnvelope(modelPath,raw,output):
    """Freeze a new calibrated bundle without changing the `modelPath` training model."""
    model=json.loads(modelPath.read_text());rows,audit=selectValid(raw)
    if model['calibrated']:raise ValueError('model already calibrated')
    if audit['sha256']==model['trainingAudit']['sha256']:raise ValueError('training reused as calibration')
    for key in ['capacity','canaryAstSha256','measurementProtocol']:
        if audit[key]!=model['trainingAudit'][key]:raise ValueError('calibration identity changed: '+key)
    surface=copy.deepcopy(model['surface']);calibration=calibrateMargin(surface,rows);surface['margin']=calibration['margin']
    baseline=predictQuality(surface,0,0)
    result=dict(model,calibrated=True,surface=surface,calibrationAudit=audit,calibration=calibration,
        frozenTrainingModelSha256=hashlib.sha256(modelPath.read_bytes()).hexdigest(),
        baselineLowerQ=baseline,eligibility='eligible_for_independent_test' if baseline>=.8 else 'ineligible_no_safe_region',
        note='Empirical lower response envelope, not mean-Q estimator. No distribution-free safety guarantee; original point-prediction failures remain.')
    with output.open('x') as handle:json.dump(result,handle,indent=2);handle.write('\n')
    print(json.dumps(dict(margin=surface['margin'],baselineLowerQ=baseline,eligibility=result['eligibility']),indent=2))
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ['model','raw','output']:parser.add_argument('--'+name,type=Path,required=True)
    a=parser.parse_args();calibrateEnvelope(a.model,a.raw,a.output)
