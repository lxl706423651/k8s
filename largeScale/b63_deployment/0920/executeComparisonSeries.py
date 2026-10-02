#!/usr/bin/env python3
# Execute a fixed, order-balanced physical comparison series on the owned cluster.
# No retries, outcome-selected repetitions, or replacement of failed/invalid trials.
"""Run six predeclared method orders with source hashes and per-round audits."""
import argparse
import hashlib
import itertools
import json
from pathlib import Path
import subprocess
import sys
import time


def digest(path):
    """Hash exact local file bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validatePlan(path):
    """Require all six method permutations and identical frozen inputs in each round."""
    plan=json.loads(path.read_text())
    methods=['default','network_only','resource_first']
    rounds=[json.loads(Path(p).read_text()) for p in plan['roundPlans']]
    if len(rounds)!=6 or {tuple(r['order']) for r in rounds}!=set(itertools.permutations(methods)):
        raise ValueError('exactly six distinct method orders required')
    if len(set(plan['roundPlans']))!=6 or [r['round'] for r in rounds]!=list(range(1,7)):
        raise ValueError('round identities changed')
    for r in rounds:
        if r['eta']!=.8 or r['modelAdmitted'] or r['instance']!=plan['instance']:
            raise ValueError('unexpected threshold, instance or admission claim')
        if digest(Path(r['instance']))!=r['instanceSha256']:
            raise ValueError('instance hash mismatch')
        if r['manifests']!=rounds[0]['manifests']:
            raise ValueError('rounds must use identical prepared inputs')
        for item in r['manifests'].values():
            if digest(Path(item['path']))!=item['sha256']:
                raise ValueError('manifest changed')
    return plan,rounds


def executeSeries(planPath):
    """Archive sources, then execute each owned trial and its independent round analysis."""
    plan,rounds=validatePlan(planPath)
    output=Path(plan['outputRoot']);output.mkdir(parents=True,exist_ok=False)
    archive=output/'sources';archive.mkdir()
    files=list(Path('.').glob('*.py'))+[planPath,Path(plan['instance']),
        Path('configs/deploymentCluster.yaml'),Path('results/pod-route-model/model.json'),
        Path('results/topology214_features.json')]
    files += [Path(p) for p in plan['roundPlans']]
    files += [Path('configs/node-'+p+'-routing-canary.yaml') for p in ['c2m2','c4m2','c4m4']]
    for item in rounds[0]['manifests'].values():files+=list(Path(item['path']).parent.glob('*'))
    hashes={}
    for source in files:
        if not source.is_file():continue
        if source.is_absolute():raise ValueError('archive inputs must be workspace-relative')
        hashes[str(source)]=digest(source)
        target=archive/source;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(source.read_bytes())
    (archive/'sha256.json').write_text(json.dumps(hashes,indent=2)+'\n')
    records=[];state=output/'stages.json'
    state.write_text('[]\n')
    for roundPath,r in zip(plan['roundPlans'],rounds):
        paths=[str(output/('round-'+str(r['round'])+'-'+m)) for m in r['order']]
        for method in r['order']+['analyze']:
            if any(digest(Path(p))!=h for p,h in hashes.items()):
                raise ValueError('frozen series source/input changed; no next trial launched')
            if method=='analyze':
                command=[sys.executable,'analyzePlacementComparisons.py','--plan',roundPath,
                         '--trials',*paths,'--output',str(output/('round-'+str(r['round'])+'-analysis'))]
            else:
                command=[sys.executable,'executePlacementTrial.py','--prepared',
                         str(Path(r['manifests'][method]['path']).parent),'--instance',plan['instance'],
                         '--output',str(output/('round-'+str(r['round'])+'-'+method))]
            record=dict(round=r['round'],stage=method,status='running',started=time.time(),command=command)
            records.append(record)
            with (output/('round-'+str(r['round'])+'-'+method+'.log')).open('x') as log:
                child=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT)
                record['pid']=child.pid;state.write_text(json.dumps(records,indent=2)+'\n')
                code=child.wait()
            record.update(status='completed' if code==0 else 'failed',returnCode=code,finished=time.time())
            state.write_text(json.dumps(records,indent=2)+'\n')
            if code:raise RuntimeError('series stage failed; preserve artifacts, no retry')
    (output/'complete.json').write_text(json.dumps(dict(finished=time.time(),rounds=6,
        independentCompletedDeployments=18,planSha256=digest(planPath),
        note='Execution complete only; invalid Q and all limitations remain in round analyses.'),indent=2)+'\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan',type=Path,required=True)
    parser.add_argument('--validate-only',action='store_true')
    args=parser.parse_args()
    if args.validate_only:
        validatePlan(args.plan)
        print('Six balanced rounds and all frozen manifest/instance hashes valid')
    else:executeSeries(args.plan)
