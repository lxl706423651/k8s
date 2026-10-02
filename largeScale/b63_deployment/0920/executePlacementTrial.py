#!/usr/bin/env python3
# Real trial on the explicitly owned 10.97.0.10 cluster; creates/deletes only its verified namespace.
# Requires completed node profiling, exclusive outputs, matching requests and frozen input hashes.
"""Run one placement comparison with live admission, protocol gates and bracketing canaries."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import contextlib
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import yaml

from analyzeRoutingCanary import compareWindows
from refreshPlacementBackground import refreshBackground
from solvePlacement import PlacementModel


def readPodIdentities(pods):
    """Map workload names to Pod UID/container IDs so hidden restarts cannot count as one trial."""
    result={}
    for pod in pods:
        name=pod['metadata']['labels']['app']
        if name in result:raise ValueError('duplicate workload identity')
        statuses=pod['status'].get('containerStatuses',[])
        if not statuses or any(not c.get('ready') or not c.get('containerID') for c in statuses):raise ValueError('container identity not ready')
        result[name]=(pod['metadata']['uid'],tuple(sorted((c['name'],c['containerID'],c.get('restartCount',0)) for c in statuses)))
    return result


def auditResourceBudget(instance, mapping, analysis):
    """Compare planned node budgets with measured increments, keeping signed residuals."""
    if {p['workload']:p['node'] for p in analysis['pods']}!=mapping:raise ValueError('resource analysis mapping differs')
    planned=PlacementModel(instance).auditMapping(mapping,enforceQuality=False)['nodes'];rows=[]
    if {n['node'] for n in analysis['nodes']}!=set(planned):raise ValueError('resource node coverage differs')
    for observed in analysis['nodes']:
        budget=planned[observed['node']];values={}
        for resource,key in [('cpu','nodeCpuIncreaseFromEmpty'),('memory','memoryProxyIncreaseFromEmptyGiB')]:
            actual=observed[key]
            values[resource]=dict(planned=budget[resource],observedIncrement=actual,signedResidual=actual-budget[resource],exceedsPlanned=actual>budget[resource])
        rows.append(dict(node=observed['node'],**values))
    return dict(nodes=rows,independentDeploymentCount=1,
        note='CPU cores and unavailable-memory GiB proxy; rounded reservation budgets are not certified upper bounds. Signed residuals retained, hierarchy levels not summed.')


def createTrialSteps(output,records,logSuffix=''):
    """Create recorded stage/canary/cooldown operations for `output`; suffix preserves prior attempt logs."""
    state=output/'stages.json'
    def run(name, command):
        """Record exact child PID/argv/status for an exclusive stage; never retry."""
        record=dict(stage=name,attempt=1+sum(r['stage']==name for r in records),command=[str(c) for c in command],status='running',started=time.time());records.append(record)
        with (output/(name+logSuffix+'.log')).open('x') as log:
            process=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT);record['pid']=process.pid
            state.write_text(json.dumps(records,indent=2)+'\n');code=process.wait()
        record.update(returnCode=code,status='completed' if code==0 else 'failed',finished=time.time());state.write_text(json.dumps(records,indent=2)+'\n')
        if code:raise RuntimeError('trial stage failed: '+name)
    def collect(phase):
        """Collect three identical-protocol zero-pressure windows, using unique remote basenames."""
        phaseRecord=dict(stage='canary-'+phase,status='running',started=time.time(),workers={})
        records.append(phaseRecord);state.write_text(json.dumps(records,indent=2)+'\n')
        lock=threading.Lock()
        def worker(profile):
            batch=output/(output.name+'-'+profile+'-'+phase)
            command=[sys.executable,'executeCalibration.py','--config','configs/node-'+profile+'-routing-canary.yaml','--output',str(batch)]
            with (output/(profile+'-'+phase+'.log')).open('x') as log:
                process=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT)
                with lock:
                    phaseRecord['workers'][profile]=dict(pid=process.pid,command=command,batch=str(batch),status='running',started=time.time())
                    state.write_text(json.dumps(records,indent=2)+'\n')
                code=process.wait()
                with lock:
                    phaseRecord['workers'][profile].update(returnCode=code,status='completed' if code==0 else 'failed',finished=time.time())
                    state.write_text(json.dumps(records,indent=2)+'\n')
            if code:raise RuntimeError('canary failed: '+profile+' '+phase)
            return profile,batch
        try:
            with ThreadPoolExecutor(max_workers=3) as pool:windows=dict(pool.map(worker,['c2m2','c4m2','c4m4']))
        except BaseException as error:
            phaseRecord.update(status='failed',error=repr(error),finished=time.time());state.write_text(json.dumps(records,indent=2)+'\n')
            raise
        phaseRecord.update(status='completed',finished=time.time());state.write_text(json.dumps(records,indent=2)+'\n')
        return windows
    def cooldown(label):
        """Use the predeclared fixed three-minute quiescence interval, not outcome-based waiting."""
        record=dict(stage=label,status='running',started=time.time(),seconds=180);records.append(record);state.write_text(json.dumps(records,indent=2)+'\n')
        for _ in range(3):time.sleep(60)
        record.update(status='completed',finished=time.time());state.write_text(json.dumps(records,indent=2)+'\n')
    return run,collect,cooldown


def executeTrial(prepared, instancePath, output):
    """Execute one prepared comparison; preserve any failed stage without retry or cleanup."""
    root=Path('results');cluster=root/'deployment-cluster';namespace='seedemu-placement0920'
    if json.loads((root/'node-calibration-wait.json').read_text())['status']!='completed':
        raise ValueError('target-node profiling is not finished; do not disturb it')
    refinement=root/'node-c4m2-refined-stages.json'
    if refinement.exists():
        last=json.loads(refinement.read_text())[-1]
        if not ((last['stage']=='evaluate' and last['status']=='completed') or
                (last['stage']=='test-guard' and last['status']=='stopped_ineligible')):
            raise ValueError('refined profiling/reproduction is unfinished; do not disturb it')
    audit=json.loads((prepared/'diagnostic.json').read_text())
    if audit['instanceSha256']!=hashlib.sha256(instancePath.read_bytes()).hexdigest():raise ValueError('instance changed')
    if audit['deploymentSha256']!=hashlib.sha256((prepared/'deployment.yaml').read_bytes()).hexdigest():raise ValueError('prepared manifest changed')
    instance=json.loads(instancePath.read_text());instance['eta']=audit['eta']
    docs=[d for d in yaml.safe_load_all((prepared/'deployment.yaml').read_text()) if d]
    workloads=[d for d in docs if d['kind']=='Deployment']
    if len(workloads)!=214 or {d['metadata']['name'] for d in workloads}!={p['id'] for p in instance['pods']}:
        raise ValueError('trial requires the complete original 214-workload topology')
    for doc in docs:
        if doc['kind']=='Namespace':
            if doc['metadata']['name']!=namespace:raise ValueError('foreign namespace')
        elif doc['metadata'].get('namespace')!=namespace:raise ValueError('resource outside owned namespace')
    if any('@sha256:' not in c['image'] for d in workloads for c in d['spec']['template']['spec']['containers']):
        raise ValueError('trial image is not immutable')
    kubeconfig=cluster/'kubeconfig.yaml';kube=['kubectl','--kubeconfig',str(kubeconfig)]
    config=yaml.safe_load(kubeconfig.read_text())
    if {c['cluster']['server'] for c in config['clusters']}!={'https://10.97.0.10:6443'}:raise ValueError('wrong cluster')
    if subprocess.check_output([*kube,'get','namespace',namespace,'--ignore-not-found','-o','name'],text=True).strip():
        raise ValueError('namespace occupied; inspect prior trial instead of restarting')
    output.mkdir(parents=True,exist_ok=False);records=[];state=output/'stages.json'
    (output/'instance-original.json').write_bytes(instancePath.read_bytes())
    (output/'prepared-audit.json').write_text(json.dumps(audit,indent=2)+'\n')
    sources={}
    for name in ['executePlacementTrial.py','refreshPlacementBackground.py','analyzeRoutingCanary.py','deployRoutingDiagnostic.py','verifyTopologyProtocols.py','executeCalibration.py','measureNodePressure.py',
                 'executeNodeBaseline.py','analyzeNodeBaseline.py','executeRoutingMeasurement.py','analyzeRoutingResources.py','solvePlacement.py','fitResponseSurface.py','analyzeCalibration.py']:
        source=Path(name).read_bytes();(output/name).write_bytes(source);sources[name]=hashlib.sha256(source).hexdigest()
    (output/'sources.json').write_text(json.dumps(sources,indent=2)+'\n')
    run,collect,cooldown=createTrialSteps(output,records)
    repository=next(p for p in Path(__file__).resolve().parents if (p/'setup.py').exists() and (p/'seedemu').is_dir())
    modulePath=repository/'seedemu/k8sTools/resources/running/manageRunningStage.py'
    spec=importlib.util.spec_from_file_location('placementTrialNative',modulePath);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    running=output/'configRunning.yaml';running.write_text(yaml.safe_dump(dict(outputDir=str(prepared.resolve()),setupConfig=str((cluster/'configK3s.yaml').resolve()))))
    with (output/'prepare-vlan.log').open('x') as log,contextlib.redirect_stdout(log),contextlib.redirect_stderr(log):
        module.prepareMacvlanVlanInterfaces(running,dict(manifest=str((prepared/'deployment.yaml').resolve()),cniMasterInterface='ens3'))
    cooldown('initial-cooldown')
    empty=output/(output.name+'-empty-resources')
    run('empty-resources',[sys.executable,'executeNodeBaseline.py','--config','configs/deploymentCluster.yaml','--output',str(empty),'--seconds','180'])
    run('analyze-empty-resources',[sys.executable,'analyzeNodeBaseline.py',str(empty),'--output',str(output/'empty-resource-analysis')])
    before=collect('empty-before')
    snapshots={}
    for key,args in [('nodes',['get','nodes','-o','json']),('pods',['get','pods','-A','-o','json'])]:
        raw=subprocess.check_output([*kube,*args],text=True);(output/(key+'-before.json')).write_text(raw);snapshots[key]=json.loads(raw)
    refreshed,evidence=refreshBackground(instance,before,snapshots['nodes'],snapshots['pods'])
    (output/'instance-live.json').write_text(json.dumps(refreshed,indent=2)+'\n');(output/'background-audit.json').write_text(json.dumps(evidence,indent=2)+'\n')
    if audit['mappingPolicy']!='kubernetes_default_scheduler':
        mapping=json.loads((prepared/'mapping.json').read_text());liveAudit=PlacementModel(refreshed).auditMapping(mapping,enforceQuality=audit['modelAdmitted'])
        (output/'pre-deployment-admission.json').write_text(json.dumps(liveAudit,indent=2)+'\n')
        if not liveAudit['feasible']:raise ValueError('fresh background/capacity rejects candidate; no Pods deployed')
    deployed=output/'deployment'
    run('deploy',[sys.executable,'deployRoutingDiagnostic.py','--prepared',str(prepared),'--kubeconfig',str(kubeconfig),'--output',str(deployed)])
    shared=['--namespace',namespace,'--inventory',str(cluster/'inventory.yaml'),'--kubeconfig',str(kubeconfig)]
    helpers=repository/'largeScale/b62_k8s_scale';artifacts=['--artifact-dir',str(deployed)]
    run('start-bird',[sys.executable,str(helpers/'start_bird_node_local.py'),*shared,*artifacts,'--load-threshold','50','--parallel-per-node','1','--node-concurrency','3','--start-batch-size','0','--start-delay-seconds','.1','--one-timeout-seconds','45'])
    cooldown('protocol-convergence')
    run('verify-start-bird',[sys.executable,str(helpers/'verifyStartBird.py'),*shared,*artifacts,'--load-threshold','50','--sample-count','10'])
    protocol=[sys.executable,'verifyTopologyProtocols.py','--features','results/topology214_features.json',*shared]
    run('all-protocols',[*protocol,'--output',str(deployed/'all-protocols')])
    run('verify-fib',[sys.executable,str(helpers/'verifyFibRoutes.py'),*shared,*artifacts,'--load-threshold','50','--sample-as-count','0'])
    completeTrial(output,audit,refreshed,before,kube,namespace,shared,run,collect,cooldown)


def completeTrial(output,audit,refreshed,before,kube,namespace,shared,run,collect,cooldown,resourceBatchName=None):
    """Measure/check/clean one unchanged deployment; optional fresh batch name supports audited instrumentation repair."""
    kubeconfig=Path(kube[kube.index('--kubeconfig')+1])
    deployed=output/'deployment'
    protocol=[sys.executable,'verifyTopologyProtocols.py','--features','results/topology214_features.json',*shared]
    resources=output/(resourceBatchName or (output.name+'-routing-resources'))
    run('routing-resources',[sys.executable,'executeRoutingMeasurement.py','--config','configs/deploymentCluster.yaml',
        '--kubeconfig',str(kubeconfig),'--gates',str(deployed),'--mapping',str(deployed/'mapping.json'),'--output',str(resources),'--seconds','180'])
    run('analyze-routing-resources',[sys.executable,'analyzeRoutingResources.py','--batch',str(resources),
        '--model','results/pod-route-model/model.json','--features','results/topology214_features.json',
        '--baseline',str(output/'empty-resource-analysis/summary.json'),'--output',str(output/'routing-resource-analysis')])
    resourceAudit=auditResourceBudget(refreshed,json.loads((deployed/'mapping.json').read_text()),json.loads((output/'routing-resource-analysis/summary.json').read_text()))
    (output/'resource-budget-audit.json').write_text(json.dumps(resourceAudit,indent=2)+'\n')
    loaded=collect('loaded')
    run('post-canary-protocols',[*protocol,'--output',str(output/'post-canary-protocols')])
    mapping=json.loads((deployed/'mapping.json').read_text())
    pods=json.loads(subprocess.check_output([*kube,'-n',namespace,'get','pods','-o','json'],text=True))['items']
    deployments=json.loads(subprocess.check_output([*kube,'-n',namespace,'get','deployments','-o','json'],text=True))['items']
    if len(pods)!=214 or {p['metadata']['labels']['app']:p['spec']['nodeName'] for p in pods}!=mapping or {d['metadata']['name'] for d in deployments}!=set(mapping):
        raise ValueError('live workload ownership/mapping changed; do not delete')
    if any(d['spec']['template']['metadata'].get('annotations',{}).get('placement-research/comparison-method')!=audit['comparisonMethod'] for d in deployments):
        raise ValueError('comparison ownership changed; do not delete')
    initialPods=json.loads((deployed/'pods-ready.json').read_text())['items']
    if readPodIdentities(pods)!=readPodIdentities(initialPods):raise ValueError('Pod or container restarted within trial; preserve for inspection')
    (output/'pods-final.json').write_text(json.dumps(pods,indent=2)+'\n')
    run('remove-owned-namespace',[*kube,'delete','namespace',namespace,'--wait=true','--timeout=300s'])
    cooldown('cleanup-cooldown');after=collect('empty-after')
    for profile in before:compareWindows(before[profile],loaded[profile],after[profile],profile,output/(profile+'-q.json'),audit['eta'])
    actualAudit=PlacementModel(refreshed).auditMapping(mapping)
    (output/'actual-mapping-audit.json').write_text(json.dumps(actualAudit,indent=2)+'\n')
    (output/'complete.json').write_text(json.dumps(dict(finished=time.time(),comparisonMethod=audit['comparisonMethod'],eta=audit['eta'],
        independentDeploymentCount=1,modelAdmitted=audit['modelAdmitted'],cost=PlacementModel(refreshed).calculateCost(mapping),
        resourceBudgetExceeded=any(n[r]['exceedsPlanned'] for n in resourceAudit['nodes'] for r in ['cpu','memory']),
        hardConstraintsSatisfied=PlacementModel(refreshed).auditMapping(mapping,enforceQuality=False)['feasible'],
        qualityReports={p:json.loads((output/(p+'-q.json')).read_text())['valid'] for p in before}),indent=2)+'\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ['prepared','instance','output']:parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args();executeTrial(args.prepared,args.instance,args.output)
