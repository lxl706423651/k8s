#!/usr/bin/env python3
# Wait for the exact existing refinement coordinator, then execute the frozen trial round.
# Creates/deletes only the owned trial namespace via executePlacementTrial; no retries.
"""Continue predeclared physical comparisons after independent profiling completes."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time


def readProcessIdentity(pid):
    """Identify a live process by command and kernel start tick, avoiding PID reuse."""
    proc=Path('/proc')/str(pid)
    try:
        command=(proc/'cmdline').read_bytes().split(b'\0')
        start=(proc/'stat').read_text().rsplit(')',1)[1].split()[19]
    except FileNotFoundError:return None
    return dict(command=[x.decode() for x in command if x],startTick=start)


def continueComparison(planPath, waitPid):
    """Validate frozen `planPath`, wait for exact `waitPid`, then run all methods in order."""
    root=Path('results');state=root/'node-v3-round1-stages.json'
    if state.exists():raise ValueError('round already queued; inspect existing process/state')
    identity=readProcessIdentity(waitPid)
    if not identity or 'continueRefinedResponse.py' not in identity['command']:
        raise ValueError('expected live refinement coordinator not found')
    plan=json.loads(planPath.read_text())
    if set(plan['order'])!={'default','network_only','resource_first'} or len(plan['order'])!=3 or plan['round']!=1:
        raise ValueError('unexpected predeclared comparison round')
    archive=root/'node-v3-round1-sources';archive.mkdir(exist_ok=False)
    files=list(Path('.').glob('*.py'))+[planPath,Path(plan['instance']),Path('configs/deploymentCluster.yaml'),
        root/'pod-route-model/model.json',root/'topology214_features.json']
    files += [Path('configs/node-'+p+'-routing-canary.yaml') for p in ['c2m2','c4m2','c4m4']]
    for method in plan['order']:files+=list(Path(plan['manifests'][method]['path']).parent.glob('*'))
    hashes={}
    for source in files:
        if not source.is_file():continue
        raw=source.read_bytes();hashes[str(source)]=hashlib.sha256(raw).hexdigest()
        target=archive/source;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
    if hashes[plan['instance']]!=plan['instanceSha256'] or any(hashes[v['path']]!=v['sha256'] for v in plan['manifests'].values()):
        raise ValueError('frozen plan inputs changed')
    (archive/'sha256.json').write_text(json.dumps(hashes,indent=2)+'\n')
    records=[dict(stage='wait-refinement',status='running',pid=waitPid,identity=identity,started=time.time())]
    with state.open('x') as f:json.dump(records,f,indent=2);f.write('\n')
    def save():
        """Persist current coordinator stages in the owned state file."""
        state.write_text(json.dumps(records,indent=2)+'\n')
    try:
        while True:
            try:stages=json.loads((root/'node-c4m2-refined-stages.json').read_text())
            except json.JSONDecodeError:
                if readProcessIdentity(waitPid)!=identity:raise RuntimeError('terminal refinement state unreadable')
                time.sleep(.2);continue
            last=stages[-1]
            if last['stage']=='evaluate' and last['status']=='completed':break
            if last['stage']=='test-guard' and last['status']=='stopped_ineligible':break
            if last['status']=='failed':raise RuntimeError('refinement failed; preserve it without trial launch')
            if readProcessIdentity(waitPid)!=identity:
                # The final state may have been committed between our first read and exit.
                last=json.loads((root/'node-c4m2-refined-stages.json').read_text())[-1]
                if ((last['stage']=='evaluate' and last['status']=='completed') or
                    (last['stage']=='test-guard' and last['status']=='stopped_ineligible')):break
                raise RuntimeError('refinement coordinator ended without terminal evidence')
            time.sleep(10)
        records[0].update(status='completed',finished=time.time());save()
        for method in plan['order']+['analyze']:
            for name,digest in hashes.items():
                if hashlib.sha256(Path(name).read_bytes()).hexdigest()!=digest:
                    raise ValueError('queued source/input changed: '+name)
            trialPaths=[str(root/('node-v3-round1-'+m)) for m in plan['order']]
            if method=='analyze':
                command=[sys.executable,'analyzePlacementComparisons.py','--plan',str(planPath),
                         '--trials',*trialPaths,'--output',str(root/'node-v3-round1-analysis')]
            else:
                command=[sys.executable,'executePlacementTrial.py','--prepared',str(Path(plan['manifests'][method]['path']).parent),
                         '--instance',plan['instance'],'--output',str(root/('node-v3-round1-'+method))]
            record=dict(stage=method,status='running',command=command,started=time.time());records.append(record)
            with (root/('node-v3-round1-'+method+'.log')).open('x') as log:
                child=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT);record['pid']=child.pid;save();code=child.wait()
            record.update(returnCode=code,status='completed' if code==0 else 'failed',finished=time.time());save()
            if code:raise RuntimeError('comparison failed: '+method)
    except BaseException as error:
        if records[-1]['status']=='running':records[-1].update(status='failed',finished=time.time())
        records[-1]['error']=repr(error);save();raise


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--plan',type=Path,required=True);p.add_argument('--wait-pid',type=int,required=True)
    a=p.parse_args();continueComparison(a.plan,a.wait_pid)
