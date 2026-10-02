#!/usr/bin/env python3
# Offline preparation of a clearly labeled, unadmitted routing calibration placement.
# Uses immutable source; creates fixed round-robin mapping solely for platform measurement.
"""Prepare actual topology routing-resource diagnostics, not a safe optimizer result."""
import argparse
from collections import Counter
import copy
import hashlib
import json
from pathlib import Path

import yaml


def prepareDiagnostic(source,nodes,systemPods,output,counts=None):
    """Pin `source` to ready worker `nodes`, checking current system-Pod slots."""
    original=[d for d in yaml.safe_load_all(source.read_text()) if d]
    docs=copy.deepcopy(original)
    live=json.loads(nodes.read_text())['items'];system=json.loads(systemPods.read_text())['items']
    workers={n['metadata']['name']:n for n in live if n['metadata']['name'] in
             ['placement-deploy0920-'+x for x in ['c2m2','c4m2','c4m4']]}
    if len(workers)!=3:raise ValueError('expected three dedicated workers')
    for n in workers.values():
        if not any(c['type']=='Ready' and c['status']=='True' for c in n['status']['conditions']):raise ValueError('node not ready')
    workloads=sorted([d for d in docs if d['kind']=='Deployment'],key=lambda d:d['metadata']['name'])
    names=sorted(workers);mapping={}
    mappingPolicy='explicit_diagnostic_counts' if counts is not None else 'sorted_workload_round_robin'
    if counts is not None:
        if set(counts)!=set(names) or any(not isinstance(v,int) or v<0 for v in counts.values()) or sum(counts.values())!=len(workloads):
            raise ValueError('explicit diagnostic counts must cover every workload and worker')
        sequence=[name for name in names for _ in range(counts[name])]
    else:
        sequence=[names[i%len(names)] for i in range(len(workloads))]
    for i,doc in enumerate(workloads):
        spec=doc['spec']['template']['spec']
        if doc['spec'].get('replicas',1)!=1 or spec.get('affinity') or spec.get('topologySpreadConstraints'):
            raise ValueError('unsupported diagnostic scheduling constraint')
        selectors=spec.pop('nodeSelector',{})
        if set(selectors)-{'kubernetes.io/hostname'}:raise ValueError('unsupported selector')
        node=sequence[i];spec['nodeName']=node;mapping[doc['metadata']['name']]=node
        doc['spec']['template']['metadata'].setdefault('annotations',{})['placement-research/evidence']='routing-resource-diagnostic-not-q-admitted'
    counts=Counter(mapping.values());systemCounts=Counter(p['spec'].get('nodeName') for p in system if p['status']['phase'] not in ['Succeeded','Failed'])
    for name,count in counts.items():
        if count+systemCounts[name]>int(workers[name]['status']['allocatable']['pods']):raise ValueError('Pod slots exceeded')
    for doc in docs:
        if doc['metadata'].get('namespace','seedemu-placement0920')!='seedemu-placement0920':raise ValueError('unexpected namespace')
    output.mkdir(parents=True,exist_ok=False)
    (output/'deployment.yaml').write_text(yaml.safe_dump_all(docs,sort_keys=False))
    (output/'mapping.json').write_text(json.dumps(mapping,indent=2)+'\n')
    (output/'diagnostic.json').write_text(json.dumps(dict(evidenceKind='routing_resource_calibration_not_model_admitted',
        qualityGuarantee=False,extraTaskBudgets=0,mappingPolicy=mappingPolicy,
        sourceSha256=hashlib.sha256(source.read_bytes()).hexdigest(),counts=dict(counts),
        systemPodCounts=dict(systemCounts),note='No claim of Q feasibility. Not accepted by safe exportDeployment workflow.'),indent=2)+'\n')
    print(json.dumps(dict(workloads=len(mapping),counts=dict(counts),qualityGuarantee=False)))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ['source','nodes','system-pods','output']:parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--counts',type=Path)
    args=parser.parse_args();prepareDiagnostic(args.source,args.nodes,args.system_pods,args.output,
        json.loads(args.counts.read_text()) if args.counts else None)
