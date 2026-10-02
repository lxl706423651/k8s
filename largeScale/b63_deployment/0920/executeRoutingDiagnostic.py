#!/usr/bin/env python3
# Host pipeline: empty-node baseline, prepared diagnostic deployment, BIRD, two gates.
# Creates only the owned experiment workload; no cleanup or automatic reruns on failure.
"""Execute real-topology resource diagnostics before final model admission experiments."""
import json
from pathlib import Path
import subprocess
import sys
import time


def executePipeline():
    """Use fixed owned-cluster artifacts; preserve stage timings and all command logs."""
    repository=next(p for p in Path(__file__).resolve().parents if (p/'setup.py').exists() and (p/'seedemu').is_dir())
    helpers=repository/'largeScale/b62_k8s_scale';results=Path('results')
    cluster=results/'deployment-cluster';run=results/'routing-diagnostic-run'
    state=results/'routing-diagnostic-stages.json'
    if state.exists():raise ValueError('pipeline already started; inspect its recorded state')
    records=[]
    def executeStage(name,command):
        """Run one `command`, recording `name`, return code and immutable log."""
        entry=dict(stage=name,command=[str(c) for c in command],started=time.time(),status='running')
        records.append(entry);state.write_text(json.dumps(records,indent=2)+'\n')
        print('START',name,flush=True)
        with (results/('routing-diagnostic-'+name+'.log')).open('x') as log:
            process=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT)
            entry['pid']=process.pid;state.write_text(json.dumps(records,indent=2)+'\n')
            code=process.wait()
        entry.update(returnCode=code,finished=time.time(),status='completed' if code==0 else 'failed')
        state.write_text(json.dumps(records,indent=2)+'\n')
        if code:raise RuntimeError('stage failed: '+name)
    executeStage('empty-baseline',[sys.executable,'executeNodeBaseline.py','--config','configs/deploymentCluster.yaml',
        '--output',str(results/'k3s-routing-empty-baseline'),'--seconds','180'])
    executeStage('deploy',[sys.executable,'deployRoutingDiagnostic.py','--prepared',str(results/'routing-diagnostic-deployment'),
        '--kubeconfig',str(cluster/'kubeconfig.yaml'),'--output',str(run)])
    shared=['--namespace','seedemu-placement0920','--inventory',str(cluster/'inventory.yaml'),
            '--kubeconfig',str(cluster/'kubeconfig.yaml')]
    executeStage('start-bird',[sys.executable,str(helpers/'start_bird_node_local.py'),*shared,'--artifact-dir',str(run),
        '--load-threshold','50','--parallel-per-node','1','--node-concurrency','3',
        '--start-batch-size','0','--start-delay-seconds','.1','--one-timeout-seconds','45'])
    # Fixed convergence window was chosen before deployment observations.
    print('WAIT fixed 180-second convergence window',flush=True)
    for _ in range(3):time.sleep(60)
    executeStage('verify-start-bird',[sys.executable,str(helpers/'verifyStartBird.py'),*shared,
        '--artifact-dir',str(run),'--load-threshold','50','--sample-count','10'])
    executeStage('verify-all-protocols',[sys.executable,'verifyTopologyProtocols.py','--features',str(results/'topology214_features.json'),
        *shared,'--output',str(run/'all-protocols')])
    executeStage('verify-fib',[sys.executable,str(helpers/'verifyFibRoutes.py'),*shared,
        '--artifact-dir',str(run),'--load-threshold','50','--sample-as-count','0'])
    print('COMPLETE routing gates; resource collection still required',flush=True)


if __name__=='__main__':executePipeline()
