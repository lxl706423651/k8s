#!/usr/bin/env python3
# Fresh calibration for separate central and admission surfaces; never refits either.
"""Freeze an independent empirical allowance on the dual model's admission envelope."""
import argparse
import copy
import hashlib
import json
from pathlib import Path

from fitResponseSurface import calibrateMargin, selectValid
from solvePlacement import predictQuality


def calibrateDual(modelPath,raw,output):
    """Calibrate only the lower surface of frozen training `modelPath` using new `raw`."""
    model=json.loads(modelPath.read_text());rows,audit=selectValid(raw)
    if model['calibrated'] or audit['sha256'] in [a['sha256'] for a in model['trainingAudits']]:raise ValueError('invalid calibration reuse')
    if any(audit[k]!=model['trainingAudits'][0][k] for k in ['capacity','canaryAstSha256','measurementProtocol']):raise ValueError('calibration identity changed')
    if audit.get('pressureControl','legacy_per_cycle_cpu_budget')!=model['trainingAudits'][0].get('pressureControl','legacy_per_cycle_cpu_budget'):
        raise ValueError('calibration pressure controller changed')
    model=copy.deepcopy(model);calibration=calibrateMargin(model['lower'],rows);model['lower']['margin']=calibration['margin']
    baseline=predictQuality(model['lower'],0,0)
    model.update(calibrated=True,calibration=calibration,calibrationAudit=audit,baselineLowerQ=baseline,
        frozenTrainingSha256=hashlib.sha256(modelPath.read_bytes()).hexdigest(),
        eligibility='eligible_for_independent_test' if baseline>=.8 else 'ineligible_no_safe_region')
    with output.open('x') as handle:json.dump(model,handle,indent=2);handle.write('\n')
    print(json.dumps(dict(margin=calibration['margin'],baselineLowerQ=baseline,eligibility=model['eligibility']),indent=2))
    return model


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ['model','raw','output']:parser.add_argument('--'+name,type=Path,required=True)
    a=parser.parse_args();calibrateDual(a.model,a.raw,a.output)
