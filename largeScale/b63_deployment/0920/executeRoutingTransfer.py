#!/usr/bin/env python3
# Second placement transfer experiment in the owned 10.97.0.10 cluster only.
# Archives every stage, refuses reruns, checks owned workload identities before replacement.
"""Validate frozen node-overhead correction on a different real topology distribution."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time

import yaml


RESULTS=Path('results')
CLUSTER=RESULTS/'deployment-cluster'
PREPARED=RESULTS/'routing-transfer-deployment'
NAMESPACE='seedemu-placement0920'
STATE=RESULTS/'routing-transfer-stages.json'


def executePipeline():
    """Run exclusive second diagnostic; all removals are identity-gated and archived."""
    if STATE.exists():raise ValueError('transfer experiment already started; inspect stage records')
    cluster=yaml.safe_load((CLUSTER/'kubeconfig.yaml').read_text())
    if {c['cluster']['server'] for c in cluster['clusters']}!={'https://10.97.0.10:6443'}:raise ValueError('wrong cluster')
    records=[]
    def run(name,command):
        """Run one named command, preserving its PID, complete argv and return code."""
        record=dict(stage=name,command=[str(c) for c in command],started=time.time(),status='running');records.append(record)
        print('START',name,flush=True)
        with (RESULTS/('routing-transfer-'+name+'.log')).open('x') as log:
            child=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT);record['pid']=child.pid
            STATE.write_text(json.dumps(records,indent=2)+'\n');code=child.wait()
        record.update(status='completed' if code==0 else 'failed',returnCode=code,finished=time.time())
        STATE.write_text(json.dumps(records,indent=2)+'\n')
        if code:raise RuntimeError('failed stage '+name)
    def removeOwned(name,mappingPath):
        """Require exact current Pod and Deployment ownership before deleting this namespace."""
        command=['kubectl','--kubeconfig',str(CLUSTER/'kubeconfig.yaml'),'-n',NAMESPACE]
        raw=subprocess.check_output([*command,'get','pods','-o','json'],text=True)
        (RESULTS/('routing-transfer-'+name+'-pods.json')).write_text(raw)
        pods=json.loads(raw)['items'];mapping=json.loads(mappingPath.read_text())
        actual={p['metadata']['labels']['app']:p['spec']['nodeName'] for p in pods}
        if actual!=mapping or len(pods)!=len(mapping):raise ValueError('live Pod ownership/mapping mismatch')
        deployments=json.loads(subprocess.check_output([*command,'get','deployments','-o','json'],text=True))['items']
        if {d['metadata']['name'] for d in deployments}!=set(mapping):raise ValueError('unexpected deployment')
        if any(d['spec']['template']['metadata'].get('annotations',{}).get('placement-research/evidence')!='routing-resource-diagnostic-not-q-admitted' for d in deployments):
            raise ValueError('missing diagnostic ownership evidence')
        run(name,['kubectl','--kubeconfig',str(CLUSTER/'kubeconfig.yaml'),'delete','namespace',NAMESPACE,'--wait=true','--timeout=300s'])
    def collectCanaries(suffix):
        """Collect three workers in parallel with fixed zero-pressure canary configs."""
        def worker(profile):
            command=[sys.executable,'executeCalibration.py','--config','configs/'+profile+'-routing-canary.yaml',
                     '--output','results/'+profile+'-'+suffix]
            # Each nested runner has exclusive output and its own PID manifest.
            with (RESULTS/(profile+'-'+suffix+'.log')).open('x') as log:
                return subprocess.run(command,stdout=log,stderr=subprocess.STDOUT).returncode
        with ThreadPoolExecutor(max_workers=3) as pool:codes=list(pool.map(worker,['c2m2','c4m2','c4m4']))
        if any(codes):raise RuntimeError('canary collection failed: '+str(codes))
        (RESULTS/(suffix+'-complete.json')).write_text(json.dumps(dict(completed=time.time(),returnCodes=codes)))
    completion=RESULTS/'routing-canary-complete.json';deadline=time.monotonic()+900
    while not completion.exists():
        if time.monotonic()>deadline:raise RuntimeError('prior canary completion absent; inspect before proceeding')
        time.sleep(5)
    if not json.loads(completion.read_text())['postGate']:raise ValueError('prior canary gate incomplete')
    removeOwned('remove-first-placement',RESULTS/'routing-diagnostic-deployment/mapping.json')
    # Reuse the current native workflow for new per-node CNI parents.
    repository=next(p for p in Path(__file__).resolve().parents if (p/'seedemu').is_dir() and (p/'setup.py').exists())
    helper=repository/'seedemu/k8sTools/resources/running/manageRunningStage.py'
    spec=importlib.util.spec_from_file_location('nativeRunning',helper);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    config=PREPARED/'configRunning.yaml'
    config.write_text(yaml.safe_dump(dict(outputDir=str(PREPARED.resolve()),setupConfig=str((CLUSTER/'configK3s.yaml').resolve()))))
    module.prepareMacvlanVlanInterfaces(config,dict(manifest=str((PREPARED/'deployment.yaml').resolve()),cniMasterInterface='ens3'))
    run('empty-baseline',[sys.executable,'executeNodeBaseline.py','--config','configs/deploymentCluster.yaml','--output','results/k3s-transfer-empty-baseline','--seconds','180'])
    run('analyze-empty-baseline',[sys.executable,'analyzeNodeBaseline.py','results/k3s-transfer-empty-baseline','--output','results/k3s-transfer-empty-baseline-analysis'])
    collectCanaries('transfer-empty-before')
    run('deploy',[sys.executable,'deployRoutingDiagnostic.py','--prepared',str(PREPARED),'--kubeconfig',str(CLUSTER/'kubeconfig.yaml'),'--output','results/routing-transfer-run'])
    shared=['--namespace',NAMESPACE,'--inventory',str(CLUSTER/'inventory.yaml'),'--kubeconfig',str(CLUSTER/'kubeconfig.yaml')]
    helpers=repository/'largeScale/b62_k8s_scale';artifacts=['--artifact-dir','results/routing-transfer-run']
    run('start-bird',[sys.executable,str(helpers/'start_bird_node_local.py'),*shared,*artifacts,'--load-threshold','50',
        '--parallel-per-node','1','--node-concurrency','3','--start-batch-size','0','--start-delay-seconds','.1','--one-timeout-seconds','45'])
    print('WAIT fixed 180-second convergence',flush=True)
    for _ in range(3):time.sleep(60)
    run('verify-start-bird',[sys.executable,str(helpers/'verifyStartBird.py'),*shared,*artifacts,'--load-threshold','50','--sample-count','10'])
    protocol=[sys.executable,'verifyTopologyProtocols.py','--features','results/topology214_features.json',*shared]
    run('verify-all-protocols',[*protocol,'--output','results/routing-transfer-run/all-protocols'])
    run('verify-fib',[sys.executable,str(helpers/'verifyFibRoutes.py'),*shared,*artifacts,'--load-threshold','50','--sample-as-count','0'])
    run('resources',[sys.executable,'executeRoutingMeasurement.py','--config','configs/deploymentCluster.yaml','--kubeconfig',str(CLUSTER/'kubeconfig.yaml'),
        '--gates','results/routing-transfer-run','--mapping',str(PREPARED/'mapping.json'),'--output','results/topology214-transfer-resources','--seconds','180'])
    run('analyze-resources',[sys.executable,'analyzeRoutingResources.py','--batch','results/topology214-transfer-resources',
        '--model','results/pod-route-model/model.json','--features','results/topology214_features.json',
        '--baseline','results/k3s-transfer-empty-baseline-analysis/summary.json','--output','results/topology214-transfer-analysis'])
    collectCanaries('transfer-routing-canary')
    run('post-canary-protocols',[*protocol,'--output','results/transfer-routing-canary-post-protocols'])
    removeOwned('remove-second-placement',PREPARED/'mapping.json')
    collectCanaries('transfer-empty-after')
    (RESULTS/'routing-transfer-complete.json').write_text(json.dumps(dict(completed=time.time(),scope='independent_distribution_transfer_not_q_admitted')))
    print('COMPLETE transfer experiment',flush=True)


if __name__=='__main__':executePipeline()
