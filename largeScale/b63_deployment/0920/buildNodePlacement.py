#!/usr/bin/env python3
# Offline target-worker instance assembly from completed, hash-matched independent tests.
# Writes a new diagnostic instance and provenance; never deploys or alters frozen models.
"""Integrate actual-worker response profiles with rounded Pod reservation budgets."""
import argparse
import copy
import hashlib
import json
import math
from pathlib import Path

import yaml

from buildDeploymentDiagnostic import deriveCardinalityBound, estimateBackground
from evaluatePodDemand import FEATURES
from fitResponseSurface import selectValid
from solvePlacement import validateInstance


def readJson(path):
    """Read one required completed artifact at `path`."""
    return json.loads(path.read_text())


def hashFile(path):
    """Return the SHA256 of exact artifact bytes at `path`."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def readMemoryReserve(path):
    """Read the declared instrument reserve from raw metadata, rather than assume 64MiB."""
    with path.open() as handle:
        metadata=json.loads(next(handle))
    if metadata.get('kind')!='metadata':raise ValueError('missing leading metadata: '+str(path))
    fraction=metadata['config']['memoryReserveFraction']
    if isinstance(fraction,bool) or not isinstance(fraction,(int,float)) or not .1<=fraction<=.5:
        raise ValueError('invalid declared memory reserve')
    return fraction


def transformSurface(surface, background, cpuCapacity, memoryCapacity):
    """Map raw guest-fraction `surface` into the explicit effective capacity frame."""
    converted=copy.deepcopy(surface)
    converted['cpu']=[(1+background['systemCpuMedian']+background['guestCpu']*x)/cpuCapacity for x in surface['cpu']]
    converted['memory']=[(background['memoryProxyMedianGiB']+background['guestMemoryGiB']*y)/memoryCapacity for y in surface['memory']]
    return converted


def readExtraBudgets(path, podIds):
    """Read optional per-Pod CPU-core/GiB budgets at `path`; reject unknown IDs or types."""
    if path is None:return {}
    budgets=yaml.safe_load(path.read_text())
    if not isinstance(budgets,dict) or set(budgets)-set(podIds):
        raise ValueError('extra budgets must map known Pod IDs to cpu/memory')
    for name,budget in budgets.items():
        if not isinstance(budget,dict) or set(budget)!={'cpu','memory'}:
            raise ValueError('only cpu and memory budget inputs supported: '+name)
        if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<0 for v in budget.values()):
            raise ValueError('extra budgets must be finite nonnegative numbers: '+name)
    return budgets


def readEvidenceSources(root, path=None):
    """Resolve explicit per-worker evidence paths relative to `root`; require all split roles."""
    profiles=['c2m2','c4m2','c4m4']
    sources={p:dict(model=f'node-{p}-model.json',evaluation=f'node-{p}-test-evaluation.json',
        training=[f'node-{p}-train/{p}/raw.jsonl'],
        calibration=f'node-{p}-calibration/{p}/raw.jsonl',test=f'node-{p}-test/{p}/raw.jsonl') for p in profiles}
    if path is not None:sources=yaml.safe_load(path.read_text())
    if not isinstance(sources,dict) or set(sources)!=set(profiles):
        raise ValueError('evidence sources must specify exactly the three target workers')
    for profile,source in sources.items():
        if not isinstance(source,dict) or set(source)!={'model','evaluation','training','calibration','test'}:
            raise ValueError('explicit model/evaluation/training/calibration/test required: '+profile)
        if not isinstance(source['training'],list) or not source['training']:
            raise ValueError('training must list all training raw files: '+profile)
        paths=source['training']+[source[k] for k in ['model','evaluation','calibration','test']]
        if any(not isinstance(p,str) or not p for p in paths):raise ValueError('invalid evidence path')
        for key in source:
            source[key]=[root/p for p in source[key]] if key=='training' else root/source[key]
    return sources


def buildInstance(root, output, extraPath=None, sourcesPath=None):
    """Read frozen evidence under `root`; write exclusive `output` without fitting to tests."""
    sources=readEvidenceSources(root,sourcesPath)
    features=readJson(root/'topology214_features.json')
    extras=readExtraBudgets(extraPath,[pod['id'] for pod in features['pods']])
    fixturePath=root/'pod-route-model/model.json';fixture=readJson(fixturePath)
    correction=readJson(root/'node-overhead-correction-train.json')
    if correction['fixtureModelSha256']!=hashFile(fixturePath):
        raise ValueError('node correction belongs to another container model')
    live=readJson(root/'deployment-cluster/nodes-pre-diagnostic.json')['items']
    system=readJson(root/'deployment-cluster/pods-pre-diagnostic.json')['items']
    instance=dict(eta=.8,pods=[],nodes=[],networks=features['networks'],surfaces={},
        costs=[[0,1,1],[1,0,1],[1,1,0]],evidenceKind='target_worker_integration_pending_actual_placement_test',
        qualityTransferValidated=False)
    provenance={};allAccepted=True
    for profile in ['c2m2','c4m2','c4m4']:
        source=sources[profile];modelPath=source['model'];model=readJson(modelPath)
        testPath=source['evaluation']
        stopped=model.get('eligibility')=='ineligible_no_safe_region'
        if stopped and (testPath.exists() or source['test'].parent.exists()):
            raise ValueError('guard-stopped profile unexpectedly has holdout artifacts: '+profile)
        if not stopped and not testPath.exists():raise ValueError('independent test evaluation not completed: '+profile)
        test=None if stopped else readJson(testPath)
        if not model['calibrated'] or (test is not None and test['modelSha256']!=hashFile(modelPath)):
            raise ValueError('test/model mismatch: '+profile)
        raw=source['training'][0]
        reservePaths=source['training']+[source['calibration']]+([] if stopped else [source['test']])
        reserves={readMemoryReserve(p) for p in reservePaths}
        if len(reserves)!=1:raise ValueError('split memory reserve mismatch: '+profile)
        reserveFraction=reserves.pop()
        trainingAudits=[selectValid(p)[1] for p in source['training']]
        training=trainingAudits[0]
        if [a['sha256'] for a in trainingAudits]!=[a['sha256'] for a in model['trainingAudits']]:
            raise ValueError('all training sources must match model in order: '+profile)
        seen=set()
        for audit in trainingAudits:
            if audit['sha256'] in seen:raise ValueError('duplicate training evidence: '+profile)
            seen.add(audit['sha256'])
            if any(audit[k]!=training[k] for k in ['capacity','canaryAstSha256','measurementProtocol','pressureControl']):
                raise ValueError('training instrument/capacity mismatch: '+profile)
        # Recheck all raw splits with the current complete-point and tracking gates.
        for split,key in [('calibration','calibrationAudit'),('test',None)]:
            if split=='test' and stopped:continue
            _,audit=selectValid(source[split])
            expected=model[key] if key else test['audit']
            if audit['sha256']!=expected['sha256'] or audit['pressureControl']!='cumulative_cpu_debt_v1':
                raise ValueError('split identity/controller mismatch: '+profile+' '+split)
            if audit['sha256'] in seen:raise ValueError('training/calibration/test evidence reused: '+profile)
            seen.add(audit['sha256'])
            if any(audit[k]!=training[k] for k in ['capacity','canaryAstSha256','measurementProtocol']):
                raise ValueError('split instrument/capacity mismatch: '+profile+' '+split)
        if training['pressureControl']!='cumulative_cpu_debt_v1':
            raise ValueError('legacy training is not an actual-worker profile')
        background=estimateBackground(raw)
        cpuCapacity=background['guestCpu']-1-max(.1,background['systemCpuP95'])
        memoryReserve=max(reserveFraction*background['guestMemoryGiB'],.0625)
        memoryCapacity=background['guestMemoryGiB']-background['memoryProxyP95GiB']-memoryReserve
        if min(cpuCapacity,memoryCapacity)<=0:raise ValueError('no effective capacity')
        name='placement-deploy0920-'+profile
        node=next(n for n in live if n['metadata']['name']==name)
        slots=int(node['status']['allocatable']['pods'])-sum(p['spec'].get('nodeName')==name and p['status']['phase'] not in ['Succeeded','Failed'] for p in system)
        instance['surfaces'][name]=transformSurface(model['lower'],background,cpuCapacity,memoryCapacity)
        instance['nodes'].append(dict(id=name,profile=name,cpu=cpuCapacity,memory=memoryCapacity,
            memoryReserveFraction=reserveFraction,
            baseCpu=1+background['systemCpuMedian'],baseMemory=background['memoryProxyMedianGiB'],
            slots=slots,networkTokens=[edge['id'] for edge in features['networks']],
            canaryAstSha256=training['canaryAstSha256'],measurementProtocol=training['measurementProtocol']))
        metrics=test['metrics'] if test else {}
        accepted=bool(test and test['accuracyPassed'] and metrics['predictedSafePoints']>0 and metrics['unsafeAmongPredictedSafe']==0)
        allAccepted=allAccepted and accepted
        provenance[profile]=dict(sources={k:[str(p) for p in v] if isinstance(v,list) else str(v) for k,v in source.items()},
            trainingSha256=[a['sha256'] for a in trainingAudits],
            modelSha256=hashFile(modelPath),evaluationSha256=hashFile(testPath) if test else None,
            testStatus='not_run_guard_stopped' if stopped else 'completed',
            background=background,memoryReserveGiB=memoryReserve,profileAcceptancePassed=accepted,centralAccuracyPassed=test['accuracyPassed'] if test else None,
            predictedSafePoints=metrics.get('predictedSafePoints'),unsafeAmongPredictedSafe=metrics.get('unsafeAmongPredictedSafe'),
            nominal=transformSurface(model['nominal'],background,cpuCapacity,memoryCapacity))
    for pod in features['pods']:
        x=[1]+[pod[k] for k in FEATURES[1:]]
        nominal={metric:sum(v*fixture['metrics'][metric]['coefficients'][key] for v,key in zip(x,FEATURES))
                 for metric in ['cpuCores','memoryGiB']}
        # Model the exact reservations exported to Kubernetes, including rounding.
        extra=extras.get(pod['id'],dict(cpu=0.,memory=0.))
        cpu=math.ceil((nominal['cpuCores']+correction['additionalCpuIntercept']+extra['cpu'])*1000-1e-10)/1000
        memory=math.ceil((nominal['memoryGiB']+correction['additionalMemoryGiBIntercept']+extra['memory'])*1024-1e-10)/1024
        instance['pods'].append(dict(id=pod['id'],community=pod['community'],cpu=cpu,memory=memory,networkTokens=pod['networks']))
    instance['profileAcceptancePassed']=allAccepted
    validateInstance(instance)
    bounds=[dict(eta=eta,**deriveCardinalityBound({**instance,'eta':eta})) for eta in [.7,.75,.8,.85,.9,.95]]
    output.mkdir(parents=True,exist_ok=False)
    artifacts=dict(instance=instance,provenance=dict(profiles=provenance,extraTaskBudgets=extras,
        extraBudgetSha256=hashFile(extraPath) if extraPath else None,
        evidenceSourcesSha256=hashFile(sourcesPath) if sourcesPath else None,
        reservationPolicy='Fixture nominal plus frozen node correction, rounded upward to 1m CPU and 1MiB before solving. All prospective comparison methods must use these same requests.',
        backgroundPolicy='Training reference background is the integration anchor. Memory capacity reserves max(instrument fraction times guest memory, 64MiB). A fresh pre-deployment baseline and live slots/CNI check are still required; estimated demand does not guarantee observed headroom.',
        note='Passing profile tests does not certify physical workload transfer or deployment-level coverage. Failed profile tests remain explicit; do not call such a mapping validated safe.'),
        cardinality_bounds=bounds)
    for name,value in artifacts.items():(output/(name+'.json')).write_text(json.dumps(value,indent=2)+'\n')
    print(json.dumps(dict(profileAcceptancePassed=allAccepted,bounds=[dict(eta=b['eta'],total=b['totalOptimisticSlots']) for b in bounds]),indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path('results'))
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--extra-budgets',type=Path,help='YAML mapping Pod IDs to cpu cores and memory GiB; omitted means zero')
    parser.add_argument('--evidence-sources',type=Path,help='Explicit per-worker split paths relative to --root; omitted selects original v3 evidence')
    args=parser.parse_args();buildInstance(args.root,args.output,args.extra_budgets,args.evidence_sources)
