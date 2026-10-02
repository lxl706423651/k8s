#!/usr/bin/env python3
# Offline integration diagnostic, not a validated safe-deployment instance.
# Converts injected-pressure axes to explicit effective-budget coordinates and retains caveats.
"""Audit whether current empirical profiles could admit the real topology at all."""
import copy
import hashlib
import json
from pathlib import Path
import statistics

import numpy as np

from evaluatePodDemand import FEATURES
from solvePlacement import predictQuality, validateInstance


def estimateBackground(raw):
    """Estimate fixed-probe background from zero-pressure references in completed `raw`."""
    records=[json.loads(line) for line in raw.read_text().splitlines()]
    rows=[r for r in records if r.get('kind') in ['baseline_start','baseline_end'] and r.get('status')=='ok']
    if len(rows)!=10:raise ValueError('require five complete reference pairs')
    capacities={(r['cpuCount'],r['before']['memory']['MemTotal']) for r in rows}
    if len(capacities)!=1:raise ValueError('changing capacity')
    cpus,memory=next(iter(capacities));background=[];memoryProxy=[]
    for row in rows:
        if row['cpuFraction'] or row['memoryFraction']:raise ValueError('nonzero reference pressure')
        ticks=[b-a for a,b in zip(row['before']['cpuTicks'],row['after']['cpuTicks'])]
        if sum(ticks[:8])<=0:raise ValueError('invalid tick interval')
        # Ratios cancel USER_HZ; guest fields are already included and not double-counted.
        busy=cpus*sum(ticks[i] for i in [0,1,2,5,6])/sum(ticks[:8])
        background.append(busy-row['canary']['cpuSeconds']/row['canary']['seconds'])
        memoryProxy.append(statistics.mean((row[s]['memory']['MemTotal']-row[s]['memory']['MemAvailable'])/1024**3 for s in ['before','after']))
    return dict(guestCpu=cpus,guestMemoryGiB=memory/1024**3,
        systemCpuMedian=max(0.,statistics.median(background)),systemCpuP95=max(0.,float(np.quantile(background,.95))),
        signedSystemCpuSamples=background,memoryProxyMedianGiB=statistics.median(memoryProxy),memoryProxyP95GiB=float(np.quantile(memoryProxy,.95)),
        memoryProxySamplesGiB=memoryProxy,sha256=hashlib.sha256(raw.read_bytes()).hexdigest(),
        note='CPU background uses aggregate tick ratios minus canary CPU rate; retains signed measurement noise. Memory is unavailable-memory proxy, not RSS.')


def deriveCardinalityBound(instance):
    """Compute an optimistic per-node Pod-count bound; insufficient sum certifies model infeasibility."""
    rows=[]
    cpu=sorted(p['cpu'] for p in instance['pods']);memory=sorted(p['memory'] for p in instance['pods'])
    for node in instance['nodes']:
        surface=instance['surfaces'][node['profile']];possible=[]
        for count in range(min(node['slots'],len(cpu))+1):
            c=sum(cpu[:count]);m=sum(memory[:count])
            if c>node['cpu']+1e-9 or m>node['memory']+1e-9:continue
            # CPU/memory minima may belong to different Pods: this relaxes, never tightens, feasibility.
            # A feasible supported query must also lie above the support floor.
            rc=max((node.get('baseCpu',0)+c)/node['cpu'],surface['cpu'][0])
            rm=max((node.get('baseMemory',0)+m)/node['memory'],surface['memory'][0])
            q=predictQuality(surface,rc,rm)
            if q is not None and q+1e-9>=instance['eta']:possible.append(count)
        rows.append(dict(node=node['id'],optimisticMaximumPods=max(possible,default=0),supportedCounts=possible))
    upper=sum(r['optimisticMaximumPods'] for r in rows)
    impossibleNodes=[r['node'] for r in rows if not r['supportedCounts']]
    return dict(nodes=rows,totalOptimisticSlots=upper,requiredPods=len(cpu),
        nodesWithoutAnyFeasibleCount=impossibleNodes,
        certifiedInfeasibleForThisInstance=bool(impossibleNodes) or upper<len(cpu),
        note='Necessary cardinality bound on this empirical instance, not physical impossibility or certification of model accuracy.')


def buildDiagnostic(output):
    """Combine current frozen profiles and explicit reserves without claiming transfer validation."""
    root=Path('results');features=json.loads((root/'topology214_features.json').read_text())
    demand=json.loads((root/'pod-route-model/model.json').read_text());overhead=json.loads((root/'node-overhead-correction-train.json').read_text())
    paths={'c2m2':('c2m2-envelope-model.json','c2m2-fine-train/c2m2/raw.jsonl'),
           'c4m2':('c4m2-surface.json','formal-c4m2-train/c4m2/raw.jsonl'),
           'c4m4':('c4m4-envelope-model.json','c4m4-fine-train/c4m4/raw.jsonl')}
    instance=dict(eta=.8,pods=[],nodes=[],networks=features['networks'],surfaces={},costs=[[0,1,1],[1,0,1],[1,1,0]],
        evidenceKind='unvalidated_coordinate_integration_diagnostic',qualityTransferValidated=False)
    provenance={}
    live=json.loads((root/'deployment-cluster/nodes-pre-diagnostic.json').read_text())['items']
    system=json.loads((root/'deployment-cluster/pods-pre-diagnostic.json').read_text())['items']
    for profile,(modelFile,trainFile) in paths.items():
        model=json.loads((root/modelFile).read_text());training=estimateBackground(root/trainFile)
        target=estimateBackground(root/(profile+'-transfer-empty-before')/profile/'raw.jsonl')
        name='placement-deploy0920-'+profile;node=next(n for n in live if n['metadata']['name']==name)
        reserveCpu=1+max(.1,target['systemCpuP95']);reserveMemory=target['memoryProxyP95GiB']+.0625
        capacityCpu=target['guestCpu']-reserveCpu;capacityMemory=target['guestMemoryGiB']-reserveMemory
        if min(capacityCpu,capacityMemory)<=0:raise ValueError('no effective Pod capacity')
        # Both frames include the SAME one-core canary budget. It cancels in raw-grid queries.
        baseCalCpu=1+training['systemCpuMedian'];baseTargetCpu=1+target['systemCpuMedian']
        surface=copy.deepcopy(model['surface']);surface['cpu']=[(baseCalCpu+training['guestCpu']*x)/capacityCpu for x in surface['cpu']]
        surface['memory']=[(training['memoryProxyMedianGiB']+training['guestMemoryGiB']*y)/capacityMemory for y in surface['memory']]
        instance['surfaces'][name]=surface
        slots=int(node['status']['allocatable']['pods'])-sum(p['spec'].get('nodeName')==name and p['status']['phase'] not in ['Succeeded','Failed'] for p in system)
        instance['nodes'].append(dict(id=name,profile=name,cpu=capacityCpu,memory=capacityMemory,slots=slots,
            baseCpu=baseTargetCpu,baseMemory=target['memoryProxyMedianGiB'],networkTokens=[e['id'] for e in features['networks']]))
        provenance[profile]=dict(modelSha256=hashlib.sha256((root/modelFile).read_bytes()).hexdigest(),training=training,target=target,
            reservedCpuCores=reserveCpu,reservedMemoryGiB=reserveMemory,probeBudgetCpuCores=1,
            capacityPolicy='CPU reserves one canary core plus max(.1, measured system P95); memory reserves empty proxy P95 plus 64MiB. Pressure axes transformed, not reused.',
            backgroundOnlyLowerQ=predictQuality(surface,baseTargetCpu/capacityCpu,target['memoryProxyMedianGiB']/capacityMemory))
    for pod in features['pods']:
        x=[1]+[pod[k] for k in FEATURES[1:]];values={}
        for metric in ['cpuCores','memoryGiB']:
            values[metric]=float(np.dot(x,[demand['metrics'][metric]['coefficients'][k] for k in FEATURES]))
        instance['pods'].append(dict(id=pod['id'],community=pod['community'],cpu=values['cpuCores']+overhead['additionalCpuIntercept'],
            memory=values['memoryGiB']+overhead['additionalMemoryGiBIntercept'],networkTokens=pod['networks']))
    validateInstance(instance)
    bounds=[]
    for eta in [.7,.75,.8,.85,.9,.95]:
        changed={**instance,'eta':eta};bounds.append(dict(eta=eta,**deriveCardinalityBound(changed)))
    output.mkdir(parents=True,exist_ok=False)
    (output/'instance.json').write_text(json.dumps(instance,indent=2)+'\n')
    (output/'provenance.json').write_text(json.dumps(dict(profiles=provenance,extraTaskBudgets=0,
        warning='Integration diagnostic only: includes a failed-accuracy original profile and newly calibrated envelopes awaiting tests. Axis algebra and resource correction do not establish physical transfer. Do not export as certified safe.'),indent=2)+'\n')
    (output/'cardinality-bounds.json').write_text(json.dumps(bounds,indent=2)+'\n')
    print(json.dumps(dict(backgroundLowerQ={p:v['backgroundOnlyLowerQ'] for p,v in provenance.items()},bounds=[{k:v for k,v in b.items() if k!='nodes'} for b in bounds]),indent=2))


if __name__=='__main__':buildDiagnostic(Path('results/deployment-coordinate-diagnostic'))
