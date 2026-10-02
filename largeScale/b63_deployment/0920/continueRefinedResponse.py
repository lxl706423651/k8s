#!/usr/bin/env python3
# Wait for the exact existing c4m2 reproduction, then calibrate/test the frozen revised envelope.
# Exclusive outputs, no retries, original failed model and untouched holdout remain preserved.
"""Independently validate the explicitly retrained lower envelope."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time


def continueRefinement():
    """Sequence one new c4m2 calibration/test after the owned diagnostic naturally finishes."""
    root=Path('results');state=root/'node-c4m2-refined-stages.json'
    if state.exists():raise ValueError('refined pipeline already queued; inspect it')
    prior=root/'node-c4m2-tail-diagnostic/manifest.json';manifest=json.loads(prior.read_text())
    pid=manifest['profiles'][0]['localSshPid'];records=[dict(stage='wait-diagnostic',pid=pid,status='running',started=time.time())]
    state.write_text(json.dumps(records,indent=2)+'\n')
    archive=root/'node-c4m2-refined-sources';archive.mkdir(exist_ok=False);hashes={}
    for name in ['continueRefinedResponse.py','refineResponseEnvelope.py','calibrateDualResponse.py','evaluateDualResponse.py','fitResponseSurface.py','analyzeCalibration.py','measureNodePressure.py','executeCalibration.py','solvePlacement.py']:
        content=Path(name).read_bytes();(archive/name).write_bytes(content);hashes[name]=hashlib.sha256(content).hexdigest()
    hashes['frozenModel']=hashlib.sha256((root/'node-c4m2-refined-train-model.json').read_bytes()).hexdigest()
    (archive/'sha256.json').write_text(json.dumps(hashes,indent=2)+'\n')
    while 'completed' not in manifest:
        proc=Path('/proc')/str(pid)/'cmdline'
        alive=proc.exists() and b'placement-node-c4m2-tail-diagnostic' in proc.read_bytes()
        if not alive and manifest['profiles'][0]['status'] not in ['completed','running']:
            raise RuntimeError('diagnostic failed; do not restart')
        # The SSH can finish before the parent completes SCP. Inspect its parent-side manifest.
        if not alive:
            matches=[]
            for p in Path('/proc').iterdir():
                if not p.name.isdigit():continue
                try:command=(p/'cmdline').read_bytes().split(b'\0')
                except (FileNotFoundError,PermissionError):continue
                if len(command)>1 and command[1]==b'executeCalibration.py' and b'results/node-c4m2-tail-diagnostic' in command:matches.append(p.name)
            if not matches:
                manifest=json.loads(prior.read_text())
                if 'completed' not in manifest:raise RuntimeError('diagnostic ended without completion')
                break
        time.sleep(5);manifest=json.loads(prior.read_text())
    if any(p['returnCode'] or p['transferReturnCode'] for p in manifest['profiles']):raise RuntimeError('diagnostic unsuccessful')
    records[0].update(status='completed',finished=time.time());state.write_text(json.dumps(records,indent=2)+'\n')
    def run(stage,command):
        """Execute and archive exact subprocess stage without retries."""
        record=dict(stage=stage,command=command,status='running',started=time.time());records.append(record)
        with (root/('node-c4m2-refined-'+stage+'.log')).open('x') as log:
            child=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT);record['pid']=child.pid;state.write_text(json.dumps(records,indent=2)+'\n');code=child.wait()
        record.update(returnCode=code,status='completed' if code==0 else 'failed',finished=time.time());state.write_text(json.dumps(records,indent=2)+'\n')
        if code:raise RuntimeError('refined stage failed: '+stage)
    run('calibration',[sys.executable,str(archive/'executeCalibration.py'),'--config','configs/node-c4m2-refined-calibration.yaml','--output','results/node-c4m2-refined-calibration'])
    run('freeze-calibration',[sys.executable,str(archive/'calibrateDualResponse.py'),'--model','results/node-c4m2-refined-train-model.json',
        '--raw','results/node-c4m2-refined-calibration/c4m2/raw.jsonl','--output','results/node-c4m2-refined-model.json'])
    model=json.loads((root/'node-c4m2-refined-model.json').read_text())
    if model['eligibility']!='eligible_for_independent_test':
        records.append(dict(stage='test-guard',status='stopped_ineligible',time=time.time()));state.write_text(json.dumps(records,indent=2)+'\n');return
    run('test',[sys.executable,str(archive/'executeCalibration.py'),'--config','configs/node-c4m2-refined-test.yaml','--output','results/node-c4m2-refined-test'])
    run('evaluate',[sys.executable,str(archive/'evaluateDualResponse.py'),'--model','results/node-c4m2-refined-model.json',
        '--raw','results/node-c4m2-refined-test/c4m2/raw.jsonl','--output','results/node-c4m2-refined-test-evaluation.json'])
    print('COMPLETE independent refined response validation',flush=True)


if __name__=='__main__':continueRefinement()
