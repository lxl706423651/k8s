#!/usr/bin/env python3
# Offline matched-request manifests for real placement comparisons on the owned cluster.
# Keeps diagnostic/no-guarantee status until actual protocol and Q measurements complete.
"""Prepare comparable manifests without presenting Q-disabled controls as safe exports."""
import argparse
import copy
import hashlib
import json
import math
from pathlib import Path

import yaml

from exportDeployment import parseQuantity, renderDeployment
from solvePlacement import PlacementModel


def renderComparison(instance, solution, source, mode):
    """Preserve `source` content, use identical requests and enforce the chosen mapping contract."""
    if mode not in ['full','resource_first','network_only','default']:raise ValueError('unknown comparison mode')
    model=PlacementModel(instance);documents=copy.deepcopy(source);seen=set()
    for doc in documents:
        if doc['kind']!='Deployment':continue
        name=doc['metadata']['name']
        if name not in model.pods or name in seen:raise ValueError('unknown or repeated source workload')
        seen.add(name);spec=doc['spec']['template']['spec'];pod=model.pods[name]
        if (doc['spec'].get('replicas',1)!=1 or spec.get('affinity') or spec.get('topologySpreadConstraints') or spec.get('initContainers') or len(spec['containers'])!=1):
            raise ValueError('unsupported source scheduling/containers')
        selector=spec.pop('nodeSelector',{})
        if set(selector)-{'kubernetes.io/hostname'}:raise ValueError('unsupported selector')
        spec.pop('nodeName',None)
        annotations=doc['spec']['template']['metadata'].setdefault('annotations',{})
        attachments=json.loads(annotations.get('k8s.v1.cni.cncf.io/networks','[]'))
        if {a['name'] for a in attachments}!={e['id'] for e in instance['networks'] if name in e['members']}:
            raise ValueError('source/model network mismatch')
        resources=spec['containers'][0].setdefault('resources',{});requests=resources.setdefault('requests',{})
        for key,scale,suffix in [('cpu',1000,'m'),('memory',1024,'Mi')]:
            rounded=math.ceil(pod[key]*scale-1e-10)/scale
            if abs(rounded-pod[key])>1e-10:raise ValueError('instance must account for request rounding before solving')
            if parseQuantity(requests.get(key,'0'),key)>rounded+1e-10:raise ValueError('source request exceeds model budget')
            if key in resources.get('limits',{}) and parseQuantity(resources['limits'][key],key)+1e-10<rounded:
                raise ValueError('source limit below model request')
            requests[key]=str(round(rounded*scale))+suffix
    if seen!=set(model.pods):raise ValueError('source misses modeled workloads')
    admission=False;exportAudit=None
    if mode=='full':
        if not instance.get('profileAcceptancePassed'):raise ValueError('independent profile acceptance not passed')
        if not solution or solution.get('method')!='full':raise ValueError('need a full-method solution')
        documents,exportAudit=renderDeployment(instance,solution,documents);admission=True
    elif mode!='default':
        if not solution or solution.get('method')!=mode or 'mapping' not in solution:raise ValueError('wrong baseline solution')
        audit=model.auditMapping(solution['mapping'],enforceQuality=False)
        if not audit['feasible']:raise ValueError('baseline violates hard constraints')
        if abs(model.calculateCost(solution['mapping'])-solution['cost'])>1e-7:raise ValueError('baseline objective changed')
        for doc in documents:
            if doc['kind']=='Deployment':doc['spec']['template']['spec']['nodeName']=solution['mapping'][doc['metadata']['name']]
    else:
        for doc in documents:
            if doc['kind']=='Deployment':
                doc['spec']['template']['spec']['affinity']={'nodeAffinity':{'requiredDuringSchedulingIgnoredDuringExecution':{'nodeSelectorTerms':[
                    {'matchExpressions':[{'key':'kubernetes.io/hostname','operator':'In','values':sorted(model.nodes)}]}]}}}
    for doc in documents:
        if doc['kind']=='Deployment':
            annotations=doc['spec']['template']['metadata']['annotations']
            annotations['placement-research/evidence']='routing-resource-diagnostic-not-q-admitted'
            annotations['placement-research/comparison-method']=mode
            annotations['placement-research/model-admitted']=str(admission).lower()
    return documents,dict(evidenceKind='routing_resource_calibration_not_model_admitted',qualityGuarantee=False,
        modelAdmitted=admission,actualPlacementValidated=False,comparisonMethod=mode,
        mappingPolicy='kubernetes_default_scheduler' if mode=='default' else 'frozen_comparison_mapping',
        allowedNodes=sorted(model.nodes),workloads=len(seen),exportAudit=exportAudit,
        note='Legacy diagnostic ownership marker means no validated physical performance guarantee. The separate modelAdmitted field records mathematical Q admission only; controls remain false.')


def prepareComparison(instancePath, sourcePath, solutionPath, mode, output):
    """Hash-check frozen inputs and write an exclusive offline prepared directory."""
    instance=json.loads(instancePath.read_text());solution=json.loads(solutionPath.read_text()) if solutionPath else None
    if solution:
        if solution['instanceSha256']!=hashlib.sha256(instancePath.read_bytes()).hexdigest():raise ValueError('solution belongs to another instance')
        instance['eta']=solution['eta']
    source=[d for d in yaml.safe_load_all(sourcePath.read_text()) if d]
    documents,audit=renderComparison(instance,solution,source,mode)
    audit.update(eta=instance['eta'],instanceSha256=hashlib.sha256(instancePath.read_bytes()).hexdigest(),sourceSha256=hashlib.sha256(sourcePath.read_bytes()).hexdigest(),
        solutionSha256=hashlib.sha256(solutionPath.read_bytes()).hexdigest() if solutionPath else None)
    output.mkdir(parents=True,exist_ok=False)
    (output/'deployment.yaml').write_text(yaml.safe_dump_all(documents,sort_keys=False))
    audit['deploymentSha256']=hashlib.sha256((output/'deployment.yaml').read_bytes()).hexdigest()
    (output/'diagnostic.json').write_text(json.dumps(audit,indent=2)+'\n')
    if mode!='default':(output/'mapping.json').write_text(json.dumps(solution['mapping'],indent=2)+'\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ['instance','source','output']:parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--solution',type=Path)
    parser.add_argument('--mode',choices=['full','resource_first','network_only','default'],required=True)
    args=parser.parse_args();prepareComparison(args.instance,args.source,args.solution,args.mode,args.output)
