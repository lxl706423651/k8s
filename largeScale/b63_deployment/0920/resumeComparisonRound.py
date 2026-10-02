#!/usr/bin/env python3
# Explicit revised-code continuation after the recorded empty-worker collector failure.
# Preserves the original failed queue; resumes existing Pods, then runs the planned last control.
"""Finish the frozen comparison order with an auditable instrumentation repair."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time


def resumeRound(planPath, checkpointFix=False):
    """Verify the known failed round and exact allowed code changes before any resumed action."""
    root=Path('results');label='recovery2' if checkpointFix else 'recovery'
    state=root/('node-v3-round1-'+label+'-stages.json')
    if state.exists():raise ValueError('recovery queue already exists; inspect it')
    prior=json.loads((root/'node-v3-round1-stages.json').read_text())
    if prior[-1]['stage']!='network_only' or prior[-1]['status']!='failed':raise ValueError('original queue not terminal at expected failure')
    original=json.loads((root/'node-v3-round1-sources/sha256.json').read_text())
    changed={p for p,h in original.items() if hashlib.sha256(Path(p).read_bytes()).hexdigest()!=h}
    allowed={'collectRoutingResources.py','executePlacementTrial.py','analyzePlacementComparisons.py'}
    if changed!=allowed:raise ValueError('unexpected changes since frozen round: '+str(changed))
    plan=json.loads(planPath.read_text())
    if plan['order']!=['default','network_only','resource_first']:raise ValueError('method order changed')
    if checkpointFix:
        previous=json.loads((root/'node-v3-round1-recovery-stages.json').read_text())
        if previous[-1]['status']!='failed':raise ValueError('prior recovery is not terminal')
    archive=root/('node-v3-round1-'+label+'-sources');archive.mkdir(exist_ok=False);hashes={}
    for name in list(original)+['resumePlacementTrial.py','resumeComparisonRound.py']:
        raw=Path(name).read_bytes();hashes[name]=hashlib.sha256(raw).hexdigest()
        target=archive/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
    (archive/'sha256.json').write_text(json.dumps(hashes,indent=2)+'\n')
    (archive/'changes.json').write_text(json.dumps({p:dict(before=original[p],after=hashes[p]) for p in sorted(changed)},indent=2)+'\n')
    records=[]
    with state.open('x') as f:f.write('[]\n')
    trials=[str(root/('node-v3-round1-'+m)) for m in plan['order']]
    commands=[('resume-network-only',[sys.executable,'resumePlacementTrial.py','--output',trials[1]]+(['--continue-after-checkpoint-fix'] if checkpointFix else [])),
              ('resource-first',[sys.executable,'executePlacementTrial.py','--prepared','results/node-v3-prepared-resource_first',
               '--instance',plan['instance'],'--output',trials[2]]),
              ('analyze',[sys.executable,'analyzePlacementComparisons.py','--plan',str(planPath),'--trials',*trials,
               '--output','results/node-v3-round1-analysis'])]
    for name,command in commands:
        if any(hashlib.sha256(Path(p).read_bytes()).hexdigest()!=h for p,h in hashes.items()):raise ValueError('recovery sources/inputs changed')
        record=dict(stage=name,status='running',command=command,started=time.time());records.append(record)
        with (root/('node-v3-round1-'+label+'-'+name+'.log')).open('x') as log:
            child=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT);record['pid']=child.pid
            state.write_text(json.dumps(records,indent=2)+'\n');code=child.wait()
        record.update(returnCode=code,status='completed' if code==0 else 'failed',finished=time.time())
        state.write_text(json.dumps(records,indent=2)+'\n')
        if code:raise RuntimeError('recovery stage failed; no retry: '+name)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--plan',type=Path,required=True)
    p.add_argument('--continue-after-checkpoint-fix',action='store_true')
    a=p.parse_args();resumeRound(a.plan,a.continue_after_checkpoint_fix)
