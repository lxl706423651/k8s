#!/usr/bin/env python3
# Offline actual-default-scheduler control; preserves immutable images and workload content.
# Removes only stale pinning and restricts eligibility to the same owned three workers.
"""Prepare a default Kubernetes scheduler control with no Q-admission claim."""
import argparse
import copy
import hashlib
import json
from pathlib import Path

import yaml


def prepareControl(source,output):
    """Write an unpinned scheduler control from owned immutable `source`."""
    original=[d for d in yaml.safe_load_all(source.read_text()) if d];docs=copy.deepcopy(original)
    allowed=['placement-deploy0920-'+p for p in ['c2m2','c4m2','c4m4']];count=0
    for document in docs:
        if document['kind']!='Deployment':continue
        count+=1;spec=document['spec']['template']['spec']
        if document['spec'].get('replicas',1)!=1 or spec.get('affinity') or spec.get('topologySpreadConstraints'):raise ValueError('unsupported scheduling input')
        selector=spec.pop('nodeSelector',{})
        if set(selector)-{'kubernetes.io/hostname'}:raise ValueError('unexpected selector')
        spec.pop('nodeName',None)
        spec['affinity']={'nodeAffinity':{'requiredDuringSchedulingIgnoredDuringExecution':{'nodeSelectorTerms':[
            {'matchExpressions':[{'key':'kubernetes.io/hostname','operator':'In','values':allowed}]}]}}}
        document['spec']['template']['metadata'].setdefault('annotations',{})['placement-research/evidence']='routing-resource-diagnostic-not-q-admitted'
        # Only pinning/eligibility and evidence annotation may differ from immutable source.
        restored=copy.deepcopy(document);restoredSpec=restored['spec']['template']['spec'];restoredSpec.pop('affinity')
        originalDocument=next(d for d in original if d['kind']=='Deployment' and d['metadata']['name']==document['metadata']['name'])
        sourceSpec=originalDocument['spec']['template']['spec']
        for key in ['nodeSelector','nodeName']:
            if key in sourceSpec:restoredSpec[key]=copy.deepcopy(sourceSpec[key])
        restored['spec']['template']['metadata']=copy.deepcopy(originalDocument['spec']['template']['metadata'])
        if restored!=originalDocument:raise ValueError('control changed workload content')
    if count!=214:raise ValueError('expected complete 214-workload topology')
    output.mkdir(parents=True,exist_ok=False)
    (output/'deployment.yaml').write_text(yaml.safe_dump_all(docs,sort_keys=False))
    (output/'diagnostic.json').write_text(json.dumps(dict(evidenceKind='routing_resource_calibration_not_model_admitted',
        purpose='actual_default_kubernetes_scheduler_control',qualityGuarantee=False,extraTaskBudgets=0,
        mappingPolicy='kubernetes_default_scheduler',allowedNodes=allowed,workloads=count,
        sourceSha256=hashlib.sha256(source.read_bytes()).hexdigest(),preservedWorkloadContent=True,
        note='Original zero resource requests preserved as in the two resource diagnostics. Not a model-admitted placement or comparison under nonzero user-task budgets.'),indent=2)+'\n')
    print(json.dumps(dict(workloads=count,allowedNodes=allowed,preservedWorkloadContent=True)))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ['source','output']:parser.add_argument('--'+name,type=Path,required=True)
    a=parser.parse_args();prepareControl(a.source,a.output)
