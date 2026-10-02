#!/usr/bin/env python3
# Training revision from a failed completed calibration, explicitly promoted to training evidence.
# Only lowers the envelope where monotonicity requires; needs a fresh calibration and holdout.
"""Refine a failed global-margin model without deleting its adverse observations."""
import argparse
import copy
import hashlib
import json
from pathlib import Path

import numpy as np

from fitResponseSurface import selectValid
from solvePlacement import predictQuality


def lowerEnvelope(surface, rows):
    """Insert observed coordinates and lower their upper-pressure closure, never raising `surface`."""
    if surface['margin']!=0:raise ValueError('start from uncalibrated training envelope')
    xs=sorted(set(surface['cpu'])|{r['cpuFraction'] for r in rows})
    ys=sorted(set(surface['memory'])|{r['memoryFraction'] for r in rows})
    values=[[predictQuality(surface,x,y) for y in ys] for x in xs]
    if any(v is None for row in values for v in row):raise ValueError('refinement outside original training domain')
    q=np.array(values)
    for row in rows:
        i=xs.index(row['cpuFraction']);j=ys.index(row['memoryFraction']);q[i,j]=min(q[i,j],row['q'])
    q=np.minimum.accumulate(np.minimum.accumulate(q,axis=0),axis=1)
    return dict(cpu=xs,memory=ys,q=q.tolist(),interpolation='upper_corner',margin=0.)


def refineEnvelope(modelPath, raw, output):
    """Promote completed `raw` into lower-envelope training; freeze a new uncalibrated bundle."""
    model=json.loads(modelPath.read_text())
    if model['calibrated']:raise ValueError('need original training bundle, not globally penalized model')
    rows,audit=selectValid(raw);original=model['trainingAudits'][0]
    if audit['sha256'] in {a['sha256'] for a in model['trainingAudits']}:raise ValueError('repeated training data')
    if any(audit[k]!=original[k] for k in ['capacity','canaryAstSha256','measurementProtocol','pressureControl']):
        raise ValueError('instrument or capacity identity differs')
    revised=copy.deepcopy(model);revised['lower']=lowerEnvelope(model['lower'],rows)
    revised['trainingAudits'].append(audit);revised['trainingRows']+=len(rows)
    revised['modelKind']='median_and_augmented_lower_envelope_v4'
    revised['centralOriginalTrainingL1']=revised.pop('trainingL1')
    revised['centralOriginalTrainingRows']=model['trainingRows']
    revised['refinement']=dict(originalModelSha256=hashlib.sha256(modelPath.read_bytes()).hexdigest(),
        promotedRawSha256=audit['sha256'],originalRole='failed_calibration',newRole='lower_envelope_training',
        centerUnchanged=True,domainUnchanged=True,allAdverseObservationsRetained=True)
    revised['sourceSha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    revised['note']='Failed calibration has become explicit additional lower-envelope training. Central fit is unchanged. Fresh independent calibration and test required; no confidence coverage certified.'
    for row in rows:
        if predictQuality(revised['lower'],row['cpuFraction'],row['memoryFraction'])>row['q']+1e-10:
            raise AssertionError('promoted adverse observation not covered')
    with output.with_suffix('.fit-source.py').open('x') as f:f.write(Path(__file__).read_text())
    with output.open('x') as f:json.dump(revised,f,indent=2);f.write('\n')
    print(json.dumps(dict(lowerTrainingRows=revised['trainingRows'],centralTrainingRows=revised['centralOriginalTrainingRows'],
        baselineTrainingLower=predictQuality(revised['lower'],0,0),freshCalibrationRequired=True),indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ['model','raw','output']:parser.add_argument('--'+name,type=Path,required=True)
    a=parser.parse_args();refineEnvelope(a.model,a.raw,a.output)
