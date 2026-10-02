#!/usr/bin/env python3
# Export an audited placement into preserved SEED YAML plus per-node image inventories.
# Offline only: never calls kubectl, changes a cluster, or pulls images.
"""Render deterministic nodeName placement and resource reservations from a solution."""
import argparse
import copy
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import re

import yaml
from solvePlacement import PlacementModel


def parseQuantity(value,resource):
    """Convert Kubernetes `value` for cpu/memory into cores/GiB; reject unknown units."""
    raw=str(value)
    match=re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)([a-zA-Z]*)',raw)
    if not match:
        raise ValueError('unsupported resource quantity: '+raw)
    number=Decimal(match[1]);suffix=match[2]
    if resource=='cpu':
        factors={'':Decimal(1),'m':Decimal('.001'),'u':Decimal('.000001'),'n':Decimal('.000000001')}
    else:
        factors={unit:Decimal(base)**power/(Decimal(1024)**3)
                 for base,units in ((1024,['','Ki','Mi','Gi','Ti','Pi','Ei']),
                                    (1000,['','K','M','G','T','P','E']))
                 for power,unit in enumerate(units)}
    if suffix not in factors:
        raise ValueError('unsupported quantity suffix: '+raw)
    return float(number*factors[suffix])


def renderDeployment(instance,solution,documents):
    """Audit `solution`, preserve `documents`, pin workloads and reserve model demands.

    Supports single-replica Deployment and Pod with exactly one regular container,
    no init containers and no affinity clauses. Unsupported scheduling constraints
    are rejected instead of silently stripping them. Hostname selector is replaced
    by the new nodeName; other node selectors must match supplied node labels.
    """
    model=PlacementModel(instance)
    if solution.get('status') not in ('feasible','optimal'):
        raise ValueError('solution does not contain a feasible mapping')
    if solution.get('performanceConstraintEnabled') is False:
        raise ValueError('cannot publish Q-disabled ablation as safe deployment')
    mapping=solution['mapping']
    audit=model.auditMapping(mapping)
    if not audit['feasible']:
        raise ValueError('placement audit failed: '+json.dumps(audit['failures']))
    if abs(model.calculateCost(mapping)-solution['cost'])>1e-7:
        raise ValueError('solution objective disagrees with recomputed communication cost')
    result=copy.deepcopy(documents)
    seen=set();inventory={k:set() for k in model.nodes}
    reservations={k:dict(cpu=0.,memory=0.) for k in model.nodes}
    changed=[]
    graphMembership={v:set() for v in model.pods}
    for edge in instance['networks']:
        for v in edge['members']:
            graphMembership[v].add(edge['id'])
    for doc in result:
        if not doc or doc.get('kind') not in ('Deployment','Pod'):
            continue
        name=doc['metadata']['name']
        if name not in mapping or name in seen:
            raise ValueError('unknown/duplicate workload '+name)
        seen.add(name)
        if doc['kind']=='Deployment':
            if doc['spec'].get('replicas',1)!=1:
                raise ValueError('multi-replica workload requires per-replica mapping')
            metadata=doc['spec']['template']['metadata'];spec=doc['spec']['template']['spec']
        else:
            metadata=doc['metadata'];spec=doc['spec']
        raw=metadata.get('annotations',{}).get('k8s.v1.cni.cncf.io/networks','[]')
        attachments=json.loads(raw) if isinstance(raw,str) else raw
        if not isinstance(attachments,list) or any(not isinstance(a,dict) or 'name' not in a for a in attachments):
            raise ValueError('only explicit JSON Multus attachments are supported')
        if {a['name'] for a in attachments}!=graphMembership[name]:
            raise ValueError('source/model network mismatch: '+name)
        if (spec.get('affinity') or spec.get('topologySpreadConstraints') or spec.get('initContainers')
                or len(spec['containers'])!=1):
            raise ValueError('unsupported affinity/spread/init/sidecar constraint: '+name)
        node=mapping[name]
        selectors=spec.get('nodeSelector',{})
        for key,value in selectors.items():
            if key!='kubernetes.io/hostname' and model.nodes[node].get('labels',{}).get(key)!=value:
                raise ValueError('node label incompatible with source selector: '+name)
        selectors.pop('kubernetes.io/hostname',None)
        if not selectors:
            spec.pop('nodeSelector',None)
        spec['nodeName']=node
        container=spec['containers'][0]
        resources=container.setdefault('resources',{})
        requests=resources.setdefault('requests',{})
        for resource in ('cpu','memory'):
            demand=max(model.pods[name][resource],parseQuantity(requests.get(resource,'0'),resource))
            divisor=1000 if resource=='cpu' else 1024
            rounded=math.ceil(demand*divisor-1e-10)/divisor
            limit=resources.get('limits',{}).get(resource)
            if limit is not None and parseQuantity(limit,resource)+1e-10<rounded:
                raise ValueError('existing container limit below required request: '+name+' '+resource)
            requests[resource]=str(round(rounded*divisor))+('m' if resource=='cpu' else 'Mi')
            reservations[node][resource]+=rounded
        inventory[node].add(container['image'])
        changed.append(dict(workload=name,node=node,requests=requests))
    if seen!=set(mapping):
        raise ValueError('source missing mapped workloads: '+str(sorted(set(mapping)-seen)))
    for node,totals in reservations.items():
        if any(totals[r]>model.nodes[node][r]+1e-9 for r in ('cpu','memory')):
            raise ValueError('rounded/preserved requests exceed node capacity: '+node)
    return result,dict(mapping=mapping,audit=audit,reservations=reservations,changes=changed,
                       imagesByNode={k:sorted(v) for k,v in inventory.items()},
                       mutableImages=sorted({image for images in inventory.values() for image in images if '@sha256:' not in image}),
                       notice='Offline artifacts only. Require live allocatable/taint/CNI/image and actual placement verification before claiming deployment success.')


def executeCli():
    """Read model/solution/source and create a new export directory without deployment."""
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('instance','solution','source','output'):
        parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    instance=json.loads(args.instance.read_text());solution=json.loads(args.solution.read_text())
    docs=list(yaml.safe_load_all(args.source.read_text()))
    rendered,report=renderDeployment(instance,solution,docs)
    report['sourceHashes']={name:hashlib.sha256(getattr(args,name).read_bytes()).hexdigest() for name in ('instance','solution','source')}
    args.output.mkdir(parents=True,exist_ok=False)
    (args.output/'deployment.yaml').write_text(yaml.safe_dump_all(rendered,sort_keys=False))
    (args.output/'mapping.json').write_text(json.dumps(report['mapping'],indent=2)+'\n')
    (args.output/'images_by_node.json').write_text(json.dumps(report['imagesByNode'],indent=2)+'\n')
    (args.output/'export_audit.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(dict(workloads=len(report['mapping']),nodes=len(report['imagesByNode']),mutableImages=len(report['mutableImages']))))


if __name__=='__main__':
    executeCli()
