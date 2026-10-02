#!/usr/bin/env python3
# Wait for exact existing calibration, freeze lower margin, then collect a fresh test.
# Does not restart a missing process or bypass an ineligible model's test gate.
"""Continue a second-generation restricted-domain envelope experiment."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

from calibrateLowerEnvelope import calibrateEnvelope
from fitResponseSurface import evaluateRows, selectValid


def continueTest(profile):
    """Wait for `profile` calibration and run its predeclared fresh three-mode test."""
    batch=Path('results/'+profile+'-envelope-calibration');manifestPath=batch/'manifest.json'
    state=Path('results/'+profile+'-envelope-continuation.json')
    if state.exists():raise ValueError('continuation already exists')
    original=json.loads(manifestPath.read_text());pid=original['profiles'][0]['localSshPid']
    handle=dict(calibrationSshPid=pid,started=time.time(),status='waiting')
    state.write_text(json.dumps(handle,indent=2)+'\n');missingSince=None
    while True:
        manifest=json.loads(manifestPath.read_text())
        if 'completed' in manifest:break
        if any(p['status']=='failed' for p in manifest['profiles']):raise RuntimeError('calibration failed')
        proc=Path('/proc/'+str(pid)+'/cmdline')
        if proc.exists():
            cmd=proc.read_bytes()
            if b'measurePressure.py' not in cmd or batch.name.encode() not in cmd:raise RuntimeError('calibration PID identity changed')
            missingSince=None
        elif missingSince is None:missingSince=time.monotonic()
        elif time.monotonic()-missingSince>60:raise RuntimeError('calibration exited without completion manifest; inspect transfer')
        time.sleep(5)
    frozen=Path('results/'+profile+'-envelope-model.json')
    model=calibrateEnvelope(Path('results/'+profile+'-envelope-train.json'),batch/profile/'raw.jsonl',frozen)
    if model['eligibility']!='eligible_for_independent_test':
        handle.update(status='stopped_ineligible',finished=time.time());state.write_text(json.dumps(handle,indent=2));return
    command=[sys.executable,'executeCalibration.py','--config','configs/'+profile+'-envelope-test.yaml','--output','results/'+profile+'-envelope-test']
    with open('results/'+profile+'-envelope-test.log','x') as log:
        child=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT);handle.update(status='testing',testControllerPid=child.pid,command=command)
        state.write_text(json.dumps(handle,indent=2));code=child.wait()
    if code:raise RuntimeError('independent test failed')
    rows,audit=selectValid(Path('results/'+profile+'-envelope-test')/profile/'raw.jsonl')
    for key in ['capacity','canaryAstSha256','measurementProtocol']:
        if audit[key]!=model['trainingAudit'][key]:raise ValueError('test identity changed')
    result=dict(modelSha256=hashlib.sha256(frozen.read_bytes()).hexdigest(),testAudit=audit,
        estimator='lower_envelope_not_conditional_mean',metrics=evaluateRows(model['surface'],rows),
        note='Lower envelope conservatism and admission audit. Does not replace the original mean-Q accuracy test.')
    Path('results/'+profile+'-envelope-test-evaluation.json').write_text(json.dumps(result,indent=2)+'\n')
    handle.update(status='completed',finished=time.time());state.write_text(json.dumps(handle,indent=2)+'\n')
    print('COMPLETE',profile,flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('profile',choices=['c2m2','c4m4']);continueTest(parser.parse_args().profile)
