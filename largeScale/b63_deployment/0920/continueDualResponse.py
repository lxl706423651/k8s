#!/usr/bin/env python3
# Continue c4m2 training refinement through fresh calibration and untouched final test.
# Never reuses the original final test as validation for the redesigned model.
"""Run the independent dual-response pipeline after the exact current training job."""
import json
from pathlib import Path
import subprocess
import sys
import time

from calibrateDualResponse import calibrateDual
from fitDualResponse import fitDual


def continuePipeline():
    """Wait for existing knee training; freeze train/calibration before new test starts."""
    root=Path('results');state=root/'c4m2-dual-stages.json'
    if state.exists():raise ValueError('dual continuation already started')
    batch=root/'c4m2-knee-train';manifestPath=batch/'manifest.json';manifest=json.loads(manifestPath.read_text())
    pid=manifest['profiles'][0]['localSshPid'];records=[dict(stage='wait-existing-training',pid=pid,started=time.time(),status='waiting')]
    state.write_text(json.dumps(records,indent=2));missingSince=None
    while True:
        manifest=json.loads(manifestPath.read_text())
        if 'completed' in manifest:break
        if any(r['status']=='failed' for r in manifest['profiles']):raise RuntimeError('knee training failed')
        proc=Path('/proc/'+str(pid)+'/cmdline')
        if proc.exists():
            if b'c4m2-knee-train' not in proc.read_bytes():raise RuntimeError('training PID identity changed')
            missingSince=None
        elif missingSince is None:missingSince=time.monotonic()
        elif time.monotonic()-missingSince>60:raise RuntimeError('training exited without completed transfer')
        time.sleep(5)
    records[0].update(status='completed',finished=time.time());state.write_text(json.dumps(records,indent=2))
    train=root/'c4m2-dual-train.json'
    fitDual([root/'formal-c4m2-train/c4m2/raw.jsonl',batch/'c4m2/raw.jsonl'],train)
    def run(name,command):
        """Record and run one exclusive calibration/test stage."""
        record=dict(stage=name,command=command,started=time.time(),status='running');records.append(record)
        with (root/('c4m2-dual-'+name+'.log')).open('x') as log:
            child=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT);record['pid']=child.pid;state.write_text(json.dumps(records,indent=2));code=child.wait()
        record.update(returnCode=code,status='completed' if code==0 else 'failed',finished=time.time());state.write_text(json.dumps(records,indent=2))
        if code:raise RuntimeError('dual stage failed: '+name)
    run('calibration',[sys.executable,'executeCalibration.py','--config','configs/c4m2-dual-calibration.yaml','--output','results/c4m2-dual-calibration'])
    frozen=root/'c4m2-dual-model.json';model=calibrateDual(train,root/'c4m2-dual-calibration/c4m2/raw.jsonl',frozen)
    if model['eligibility']!='eligible_for_independent_test':
        records.append(dict(stage='test-guard',status='stopped_ineligible',time=time.time()));state.write_text(json.dumps(records,indent=2));return
    run('test',[sys.executable,'executeCalibration.py','--config','configs/c4m2-dual-test.yaml','--output','results/c4m2-dual-test'])
    run('evaluate',[sys.executable,'evaluateDualResponse.py','--model',str(frozen),'--raw','results/c4m2-dual-test/c4m2/raw.jsonl','--output','results/c4m2-dual-test-evaluation.json'])
    print('COMPLETE dual response pipeline',flush=True)


if __name__=='__main__':continuePipeline()
