#!/usr/bin/env python3
# Actual default-scheduler control, sequenced after the owned transfer experiment.
# Uses explicit namespace/cluster guards, immutable prepared content and full protocol gates.
"""Measure real scheduler placement and bracket its routing canary with empty references."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time

import yaml


def executeControl():
    """Run one exclusive default-scheduler control after prior owned work completes."""
    results=Path('results');cluster=results/'deployment-cluster';prepared=results/'default-scheduler-deployment'
    state=results/'default-scheduler-stages.json'
    if state.exists():raise ValueError('scheduler control already started')
    config=yaml.safe_load((cluster/'kubeconfig.yaml').read_text())
    if {c['cluster']['server'] for c in config['clusters']}!={'https://10.97.0.10:6443'}:raise ValueError('wrong cluster')
    records=[];state.write_text('[]\n')
    while not (results/'routing-transfer-complete.json').exists():
        proc=Path('/proc/3222441/cmdline')
        if not proc.exists() or b'executeRoutingTransfer.py' not in proc.read_bytes():raise RuntimeError('prior experiment ended without completion; inspect it')
        time.sleep(5)
    namespace='seedemu-placement0920';kube=['kubectl','--kubeconfig',str(cluster/'kubeconfig.yaml')]
    if subprocess.check_output([*kube,'get','namespace',namespace,'--ignore-not-found','-o','name'],text=True).strip():raise ValueError('namespace not empty after prior work')
    def run(name,command):
        """Record each exclusive stage's exact argv, PID and completion status."""
        record=dict(stage=name,command=[str(c) for c in command],started=time.time(),status='running');records.append(record)
        print('START',name,flush=True)
        with (results/('default-scheduler-'+name+'.log')).open('x') as log:
            process=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT);record['pid']=process.pid;state.write_text(json.dumps(records,indent=2));code=process.wait()
        record.update(returnCode=code,status='completed' if code==0 else 'failed',finished=time.time());state.write_text(json.dumps(records,indent=2))
        if code:raise RuntimeError('stage failed: '+name)
    def canaries(phase):
        """Collect fixed zero-pressure canary windows on the three eligible workers."""
        def worker(profile):
            command=[sys.executable,'executeCalibration.py','--config','configs/'+profile+'-routing-canary.yaml','--output','results/'+profile+'-default-'+phase]
            with (results/(profile+'-default-'+phase+'.log')).open('x') as log:
                return subprocess.run(command,stdout=log,stderr=subprocess.STDOUT).returncode
        with ThreadPoolExecutor(max_workers=3) as pool:codes=list(pool.map(worker,['c2m2','c4m2','c4m4']))
        if any(codes):raise RuntimeError('canary window failed: '+phase)
        (results/('default-'+phase+'-complete.json')).write_text(json.dumps(dict(completed=time.time(),returnCodes=codes)))
    repository=next(p for p in Path(__file__).resolve().parents if (p/'setup.py').exists() and (p/'seedemu').is_dir())
    modulePath=repository/'seedemu/k8sTools/resources/running/manageRunningStage.py'
    spec=importlib.util.spec_from_file_location('defaultControlNative',modulePath);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    running=prepared/'configRunning.yaml';running.write_text(yaml.safe_dump(dict(outputDir=str(prepared.resolve()),setupConfig=str((cluster/'configK3s.yaml').resolve()))))
    module.prepareMacvlanVlanInterfaces(running,dict(manifest=str((prepared/'deployment.yaml').resolve()),cniMasterInterface='ens3'))
    canaries('empty-before')
    run('deploy',[sys.executable,'deployRoutingDiagnostic.py','--prepared',str(prepared),'--kubeconfig',str(cluster/'kubeconfig.yaml'),'--output','results/default-scheduler-run'])
    shared=['--namespace',namespace,'--inventory',str(cluster/'inventory.yaml'),'--kubeconfig',str(cluster/'kubeconfig.yaml')]
    helpers=repository/'largeScale/b62_k8s_scale';artifacts=['--artifact-dir','results/default-scheduler-run']
    run('start-bird',[sys.executable,str(helpers/'start_bird_node_local.py'),*shared,*artifacts,'--load-threshold','50','--parallel-per-node','1','--node-concurrency','3','--start-batch-size','0','--start-delay-seconds','.1','--one-timeout-seconds','45'])
    print('WAIT fixed 180-second convergence',flush=True)
    for _ in range(3):time.sleep(60)
    run('verify-start-bird',[sys.executable,str(helpers/'verifyStartBird.py'),*shared,*artifacts,'--load-threshold','50','--sample-count','10'])
    protocol=[sys.executable,'verifyTopologyProtocols.py','--features','results/topology214_features.json',*shared]
    run('verify-all-protocols',[*protocol,'--output','results/default-scheduler-run/all-protocols'])
    run('verify-fib',[sys.executable,str(helpers/'verifyFibRoutes.py'),*shared,*artifacts,'--load-threshold','50','--sample-as-count','0'])
    canaries('routing')
    run('post-canary-protocols',[*protocol,'--output','results/default-scheduler-post-protocols'])
    mapping=json.loads((results/'default-scheduler-run/mapping.json').read_text())
    raw=subprocess.check_output([*kube,'-n',namespace,'get','pods','-o','json'],text=True);(results/'default-scheduler-final-pods.json').write_text(raw)
    pods=json.loads(raw)['items'];actual={p['metadata']['labels']['app']:p['spec']['nodeName'] for p in pods}
    deployments=json.loads(subprocess.check_output([*kube,'-n',namespace,'get','deployments','-o','json'],text=True))['items']
    if actual!=mapping or len(pods)!=214 or {d['metadata']['name'] for d in deployments}!=set(mapping):raise ValueError('live ownership/mapping changed')
    if any(d['spec']['template']['metadata'].get('annotations',{}).get('placement-research/evidence')!='routing-resource-diagnostic-not-q-admitted' for d in deployments):raise ValueError('ownership annotation missing')
    run('remove-owned-namespace',[*kube,'delete','namespace',namespace,'--wait=true','--timeout=300s'])
    # Empty-reference canary must not sample immediate post-deletion garbage collection.
    print('WAIT fixed 180-second cleanup cooldown',flush=True)
    for _ in range(3):time.sleep(60)
    canaries('empty-after')
    for profile in ['c2m2','c4m2','c4m4']:
        run('evaluate-'+profile,[sys.executable,'analyzeRoutingCanary.py','--profile',profile,
            '--before','results/'+profile+'-default-empty-before','--loaded','results/'+profile+'-default-routing',
            '--after','results/'+profile+'-default-empty-after','--output','results/'+profile+'-default-q.json'])
    (results/'default-scheduler-complete.json').write_text(json.dumps(dict(completed=time.time(),qualityGuarantee=False,
        evidence='actual_default_scheduler_control_zero_extra_task_budget',independentDeploymentCount=1)))
    print('COMPLETE actual scheduler control',flush=True)


if __name__=='__main__':executeControl()
