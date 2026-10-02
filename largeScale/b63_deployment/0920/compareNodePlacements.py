#!/usr/bin/env python3
# Offline comparison on one frozen instance; creates exclusive per-method results and eta scan.
# Does not launch Pods. Q-disabled baselines retain explicit non-admission labels.
"""Compare resource balancing, network-only search and the PDF constrained objective."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import time

from buildDeploymentDiagnostic import deriveCardinalityBound
from solvePlacement import PlacementModel


def buildResourceFirst(model):
    """Place largest demands on least-loaded hard-feasible nodes, without a Q gate."""
    dominant=lambda pod:min(max(pod['cpu']/node['cpu'],pod['memory']/node['memory']) for node in model.nodes.values())
    order=sorted(model.pods.values(),key=lambda pod:(-dominant(pod),pod['id']))
    mapping={}
    for pod in order:
        candidates=[]
        for name,node in sorted(model.nodes.items()):
            trial={**mapping,pod['id']:name}
            audit=model.auditMapping(trial,complete=False,enforceQuality=False)
            if not audit['feasible']:continue
            pressure=audit['nodes'][name]
            cpu=pressure['cpu']/node['cpu'];memory=pressure['memory']/node['memory']
            candidates.append((max(cpu,memory),cpu+memory,name))
        if not candidates:return dict(status='no_feasible_solution_found',note='Resource greedy construction failed, not an infeasibility proof.')
        mapping[pod['id']]=min(candidates)[2]
    return dict(status='feasible',mapping=mapping,cost=model.calculateCost(mapping),
        performanceConstraintEnabled=False,audit=model.auditMapping(mapping,enforceQuality=False))


def comparePlacements(instancePath, output, starts, seeds):
    """Keep one instance's budgets/surfaces fixed while changing method and eta only."""
    if starts<1 or not seeds:raise ValueError('positive starts and nonempty seeds required')
    instance=json.loads(instancePath.read_text());PlacementModel(instance)
    output.mkdir(parents=True,exist_ok=False)
    (output/'instance.json').write_bytes(instancePath.read_bytes())
    records=[]
    settings=[('resource_first',.8,False),('network_only',.8,False)]+[('full',eta,True) for eta in [.7,.75,.8,.85,.9,.95]]
    for method,eta,enforce in settings:
        for seed in (seeds[:1] if method=='resource_first' else seeds):
            changed=copy.deepcopy(instance);changed['eta']=eta;model=PlacementModel(changed)
            start=time.monotonic();bound=deriveCardinalityBound(changed)
            if enforce and bound['certifiedInfeasibleForThisInstance']:
                result=dict(status='proven_infeasible_for_empirical_instance',certificate=bound)
            elif method=='resource_first':result=buildResourceFirst(model)
            else:result=model.solvePlacement(starts=starts,seed=seed,enforceQuality=enforce)
            if 'mapping' in result:
                result['qualityAudit']=model.auditMapping(result['mapping'])
                result['modelAdmitted']=enforce and result['qualityAudit']['feasible']
            else:result['modelAdmitted']=False
            result.update(method=method,eta=eta,seed=seed,seconds=time.monotonic()-start,
                instanceSha256=hashlib.sha256(instancePath.read_bytes()).hexdigest(),
                profileAcceptancePassed=instance.get('profileAcceptancePassed',False),
                actualPlacementValidated=False)
            name=method+'-eta'+str(eta)+'-seed'+str(seed)+'.json'
            (output/name).write_text(json.dumps(result,indent=2)+'\n')
            records.append(dict(file=name,**{key:result.get(key) for key in ['method','eta','seed','status','cost','seconds','modelAdmitted','profileAcceptancePassed']}))
            (output/'summary.json').write_text(json.dumps(records,indent=2)+'\n')
            print(json.dumps(records[-1]),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--starts',type=int,default=3)
    parser.add_argument('--seeds',type=int,nargs='+',default=[11,29,47])
    args=parser.parse_args();comparePlacements(args.instance,args.output,args.starts,args.seeds)
