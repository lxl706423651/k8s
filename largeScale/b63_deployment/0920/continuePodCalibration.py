#!/usr/bin/env python3
# Wait for the exact existing Pod-resource training SSH, archive data, freeze fit, test.
# Does not restart missing training; creates only the independent test on the route VM.
"""Continue full-container demand calibration with a frozen independent test."""
import json
from pathlib import Path
import subprocess
import sys
import time

SSH=['ssh','-i',str(Path('~/.ssh/id_ed25519').expanduser()),'-o','BatchMode=yes','ubuntu@10.99.0.11']


def locateTraining():
    """Find exactly one live SSH for the already-started pod-route-train command."""
    matches=[]
    for directory in Path('/proc').iterdir():
        if not directory.name.isdigit():continue
        try:
            if (directory/'comm').read_text().strip()!='ssh':continue
            cmd=(directory/'cmdline').read_bytes().replace(b'\0',b' ').decode()
            if '--output /home/ubuntu/pod-route-train ' in cmd:
                matches.append((int(directory.name),cmd))
        except (FileNotFoundError,PermissionError,ProcessLookupError):continue
    if len(matches)!=1:raise RuntimeError('expected one live training SSH; inspect completion manually')
    return matches[0]


def fetchBatch(name):
    """Check terminal marker and fetch owned guest batch `name`, omitting sockets."""
    subprocess.run(SSH+[f'test -f /home/ubuntu/{name}/complete.json'],check=True)
    archive=Path('results')/(name+'.tar.gz')
    with archive.open('xb') as handle:
        subprocess.run(SSH+[f'tar --exclude="*.ctl" -czf - -C /home/ubuntu {name}'],stdout=handle,check=True)
    subprocess.run(['tar','-xzf',str(archive),'-C','results'],check=True)
    marker=json.loads((Path('results')/name/'complete.json').read_text())
    rows=(Path('results')/name/'raw.jsonl').read_text().splitlines()
    if len(rows)!=marker['cases']:raise RuntimeError('raw row count disagrees with complete marker')


def executeStage(command,path):
    """Execute `command` with new log `path`, failing without retries."""
    print('START '+' '.join(command),flush=True)
    with path.open('x') as handle:subprocess.run(command,stdout=handle,stderr=subprocess.STDOUT,check=True)


def executePipeline():
    """Monitor current training, freeze its model, then run the untouched test list."""
    pid,command=locateTraining()
    Path('results/pod-pipeline-handle.json').write_text(json.dumps(dict(trainingSshPid=pid,command=command,started=time.time()),indent=2))
    print('WAIT training SSH '+str(pid),flush=True)
    while True:
        try:current=Path(f'/proc/{pid}/cmdline').read_bytes().replace(b'\0',b' ').decode()
        except FileNotFoundError:break
        if current!=command:raise RuntimeError('training PID identity changed')
        time.sleep(5)
    fetchBatch('pod-route-train')
    executeStage([sys.executable,'analyzeRouting.py','results/pod-route-train/raw.jsonl',
                  '--output','results/pod-route-model'],Path('results/pod-route-fit.log'))
    testCommand='sudo python3 /home/ubuntu/measurePodRouting.py --config /home/ubuntu/pod-route-test.json --output /home/ubuntu/pod-route-test --image 10.92.0.10:5092/brdnode_1277_r10:latest'
    executeStage(SSH+[testCommand],Path('results/pod-route-test.log'))
    fetchBatch('pod-route-test')
    executeStage([sys.executable,'evaluatePodDemand.py','--model','results/pod-route-model/model.json',
                  '--raw','results/pod-route-test/raw.jsonl','--output','results/pod-route-evaluation.json'],
                  Path('results/pod-route-evaluation.log'))
    print('COMPLETE pod demand pipeline',flush=True)


if __name__=='__main__':
    executePipeline()
