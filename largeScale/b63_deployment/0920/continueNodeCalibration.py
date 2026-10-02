#!/usr/bin/env python3
# Target-worker recalibration only after the full scheduler-control experiment completes.
# Separate node branches run concurrently; each freezes training/calibration before its test.
"""Calibrate restricted low-load response directly on the actual K3s worker environment."""
from concurrent.futures import ThreadPoolExecutor
import json
import hashlib
from pathlib import Path
import subprocess
import sys
import time

from calibrateDualResponse import calibrateDual
from fitDualResponse import fitDual


def calibrateWorker(profile):
    """Execute predeclared three-way splits on one explicitly named owned worker."""
    root=Path('results');prefix='node-'+profile;state=root/(prefix+'-stages.json')
    if state.exists():raise ValueError('node branch already started')
    records=[]
    def run(stage,command):
        """Preserve exclusive stage log and exact child PID without retries."""
        record=dict(stage=stage,command=command,status='running',started=time.time());records.append(record)
        with (root/(prefix+'-'+stage+'.log')).open('x') as log:
            child=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT);record['pid']=child.pid;state.write_text(json.dumps(records,indent=2));code=child.wait()
        record.update(returnCode=code,status='completed' if code==0 else 'failed',finished=time.time());state.write_text(json.dumps(records,indent=2))
        if code:raise RuntimeError('node calibration failed: '+profile+' '+stage)
    for stage in ['train','calibration']:
        run(stage,[sys.executable,'executeCalibration.py','--config','configs/'+prefix+'-'+stage+'.yaml','--output','results/'+prefix+'-'+stage])
        if stage=='train':fitDual([root/(prefix+'-train')/profile/'raw.jsonl'],root/(prefix+'-train-model.json'))
    model=calibrateDual(root/(prefix+'-train-model.json'),root/(prefix+'-calibration')/profile/'raw.jsonl',root/(prefix+'-model.json'))
    if model['eligibility']!='eligible_for_independent_test':
        records.append(dict(stage='test-guard',status='stopped_ineligible',time=time.time()));state.write_text(json.dumps(records,indent=2));return dict(profile=profile,status='ineligible')
    run('test',[sys.executable,'executeCalibration.py','--config','configs/'+prefix+'-test.yaml','--output','results/'+prefix+'-test'])
    run('evaluate',[sys.executable,'evaluateDualResponse.py','--model','results/'+prefix+'-model.json','--raw','results/'+prefix+'-test/'+profile+'/raw.jsonl','--output','results/'+prefix+'-test-evaluation.json'])
    return dict(profile=profile,status='completed')


def continueCalibration():
    """Wait on the exact scheduler controller; refuse missing completion or occupied namespace."""
    root=Path('results');handle=root/'node-calibration-wait.json'
    if handle.exists():raise ValueError('target-worker recalibration already queued')
    completion=root/'default-scheduler-complete.json';matches=[]
    if not completion.exists():
        for p in Path('/proc').iterdir():
            if not p.name.isdigit():continue
            try:command=(p/'cmdline').read_bytes().split(b'\0')
            except (FileNotFoundError,PermissionError):continue
            if len(command)>1 and command[1]==b'executeSchedulerControl.py':matches.append(int(p.name))
        if len(matches)!=1:raise ValueError('need exact live scheduler-control controller')
    record=dict(started=time.time(),controllerPid=matches[0] if matches else None,status='waiting',scope='actual_worker_recalibration_not_bare_profile_reuse')
    handle.write_text(json.dumps(record,indent=2))
    while not completion.exists():
        process=Path('/proc/'+str(record['controllerPid'])+'/cmdline')
        if not process.exists() or b'executeSchedulerControl.py' not in process.read_bytes():raise RuntimeError('scheduler control ended without completion')
        time.sleep(5)
    namespace=subprocess.check_output(['kubectl','--kubeconfig','results/deployment-cluster/kubeconfig.yaml','get','namespace','seedemu-placement0920','--ignore-not-found','-o','name'],text=True)
    if namespace.strip():raise ValueError('target cluster still has experiment Pods')
    archive=root/'node-calibration-sources';archive.mkdir(exist_ok=False)
    hashes={}
    for name in ['continueNodeCalibration.py','executeCalibration.py','measureNodePressure.py',
                 'fitResponseSurface.py','fitDualResponse.py','calibrateDualResponse.py',
                 'evaluateDualResponse.py','analyzeCalibration.py','solvePlacement.py']:
        source=Path(__file__).with_name(name).read_bytes();(archive/name).write_bytes(source)
        hashes[name]=hashlib.sha256(source).hexdigest()
    (archive/'sha256.json').write_text(json.dumps(hashes,indent=2)+'\n')
    record.update(status='calibrating',calibrationStarted=time.time());handle.write_text(json.dumps(record,indent=2))
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures=[pool.submit(calibrateWorker,p) for p in ['c2m2','c4m2','c4m4']]
        outcomes=[future.result() for future in futures]
    record.update(status='completed',finished=time.time(),outcomes=outcomes);handle.write_text(json.dumps(record,indent=2))
    print('COMPLETE target-worker calibration',flush=True)


if __name__=='__main__':continueCalibration()
