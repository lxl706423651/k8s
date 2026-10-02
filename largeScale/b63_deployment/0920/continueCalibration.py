#!/usr/bin/env python3
# Host-only continuation of an already-started training batch, then cal/fit/test.
# Waits only while the exact manifest-recorded SSH process remains live; never restarts training.
"""Advance a frozen profile pipeline without reusing test observations for fitting."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def waitTraining(batch):
    """Wait on live SSH PID in `batch`; refuse missing handles or failed training."""
    manifest=batch/'manifest.json'
    while True:
        data=json.loads(manifest.read_text())
        if data.get('completed'):
            if not all(p['status']=='completed' and p['transferReturnCode']==0 for p in data['profiles']):
                raise RuntimeError('training manifest contradicts completion')
            return
        profile=data['profiles'][-1]
        pid=profile.get('localSshPid')
        if not pid:
            raise RuntimeError('training has no live process handle')
        try:
            cmdline=Path(f'/proc/{pid}/cmdline').read_bytes().replace(b'\0',b' ').decode()
        except FileNotFoundError:
            # Give the controller a short opportunity to flush terminal state;
            # this is not a restart and never treats timeout as success.
            time.sleep(2)
            data=json.loads(manifest.read_text())
            if data.get('completed'):
                return
            raise RuntimeError('training process absent without confirmed completion')
        if 'ssh ' not in cmdline or profile['remote'] not in cmdline:
            raise RuntimeError('recorded PID does not belong to this experiment')
        time.sleep(5)


def executeStage(command,log):
    """Run `command` to completion and preserve log, failing without retries."""
    print('START '+' '.join(command),flush=True)
    with log.open('x') as handle:
        subprocess.run(command,stdout=handle,stderr=subprocess.STDOUT,check=True)


def executeCli():
    """Continue an existing profile training job into disjoint calibration and test."""
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile',required=True,choices=['c2m2','c4m2','c4m4'])
    args=parser.parse_args()
    name=args.profile
    root=Path('results')
    train=root/('formal-'+name+'-train')
    cal=root/('formal-'+name+'-calibrate')
    test=root/('formal-'+name+'-test')
    print('WAIT '+str(train),flush=True)
    waitTraining(train)
    executeStage([sys.executable,'executeCalibration.py','--config',f'configs/{name}-calibrate.yaml','--output',str(cal)],root/(f'formal-{name}-calibrate.log'))
    executeStage([sys.executable,'fitResponseSurface.py','fit','--train',str(train/name/'raw.jsonl'),
                  '--calibration',str(cal/name/'raw.jsonl'),'--output',str(root/(name+'-surface.json'))],root/(name+'-fit.log'))
    # Model file exists before any final test observations are produced.
    executeStage([sys.executable,'executeCalibration.py','--config',f'configs/{name}-test.yaml','--output',str(test)],root/(f'formal-{name}-test.log'))
    executeStage([sys.executable,'fitResponseSurface.py','test','--model',str(root/(name+'-surface.json')),
                  '--raw',str(test/name/'raw.jsonl'),'--output',str(root/(name+'-test.json'))],root/(name+'-test-analysis.log'))
    print('COMPLETE '+name,flush=True)


if __name__=='__main__':
    executeCli()
