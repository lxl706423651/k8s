#!/usr/bin/env python3
# Host runner for actual-topology resource windows after protocol and FIB gates.
# Saves live Pod identity maps and archived samplers, then collects workers in parallel.
"""Measure full Kubernetes routing overhead without treating a diagnostic as admission."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time

import yaml


def measureWorker(node,pods,config,output,seconds):
    """Measure one owned `node` and its current `pods`, preserving SSH and data."""
    name=node['name'];folder=output/name;folder.mkdir()
    identities=[]
    for pod in pods:
        statuses=pod['status']['containerStatuses']
        if len(statuses)!=1:raise ValueError('single-container routing Pod required')
        identities.append(dict(workload=pod['metadata']['labels']['app'],pod=pod['metadata']['name'],
            uid=pod['metadata']['uid'],containerId=statuses[0]['containerID'].removeprefix('containerd://'),
            restartCount=statuses[0]['restartCount']))
    podMap=folder/'pod-map.json';podMap.write_text(json.dumps(dict(node=name,pods=identities),indent=2)+'\n')
    options=['-i',str(Path(config['ssh']['key']).expanduser()),'-o','BatchMode=yes','-o','StrictHostKeyChecking=accept-new']
    target=config['ssh']['user']+'@'+node['ip'];remote='/home/'+config['ssh']['user']+'/'+output.name
    subprocess.run(['ssh',*options,target,'mkdir '+shlex.quote(remote)],check=True)
    files=[str(output/f) for f in ['collectRoutingResources.py','measurePressure.py','measurePodRouting.py','measureRouting.py']]
    subprocess.run(['scp',*options,*files,str(podMap),target+':'+remote+'/'],check=True)
    command=shlex.join(['sudo','python3',remote+'/collectRoutingResources.py','--pod-map',remote+'/pod-map.json',
                       '--seconds',str(seconds),'--output',remote+'/raw.jsonl'])
    manifest=dict(node=name,pods=len(pods),command=command,remote=remote,started=time.time(),status='running')
    with (folder/'ssh.log').open('x') as log:
        process=subprocess.Popen(['ssh',*options,'-o','ServerAliveInterval=15',target,command],stdout=log,stderr=subprocess.STDOUT)
        manifest['localSshPid']=process.pid;(folder/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
        code=process.wait()
    transfer=subprocess.run(['scp',*options,target+':'+remote+'/raw.jsonl',str(folder/'raw.jsonl')])
    manifest.update(status='completed' if code==0 else 'failed',returnCode=code,transferReturnCode=transfer.returncode,ended=time.time())
    (folder/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    if code or transfer.returncode:raise RuntimeError('routing collection failed for '+name)
    return manifest


def executeMeasurement(configPath,kubeconfig,gates,output,seconds,mappingPath=Path('results/routing-diagnostic-deployment/mapping.json')):
    """Require successful `gates`, then measure current topology and recheck protocols."""
    for name in ['verify_after_start_bird_summary.json','verify_after_fib_write_summary.json','all-protocols/summary.json']:
        if not json.loads((gates/name).read_text())['success']:raise ValueError('routing gate failed: '+name)
    config=yaml.safe_load(configPath.read_text());namespace='seedemu-placement0920'
    raw=subprocess.check_output(['kubectl','--kubeconfig',str(kubeconfig),'-n',namespace,'get','pods','-o','json'],text=True)
    pods=json.loads(raw)['items'];mapping=json.loads(mappingPath.read_text())
    actual={p['metadata']['labels']['app']:p['spec']['nodeName'] for p in pods}
    if actual!=mapping or len(pods)!=len(mapping):raise ValueError('live diagnostic mapping changed')
    output.mkdir(parents=True,exist_ok=False);(output/'pods-before.json').write_text(raw)
    (output/'mapping.json').write_text(json.dumps(mapping,indent=2)+'\n')
    hashes={}
    for name in ['collectRoutingResources.py','measurePressure.py','measurePodRouting.py','measureRouting.py']:
        content=Path(__file__).with_name(name).read_bytes();(output/name).write_bytes(content);hashes[name]=hashlib.sha256(content).hexdigest()
    (output/'sources.json').write_text(json.dumps(hashes,indent=2)+'\n')
    workers=[n for n in config['nodes'] if n['role']=='worker']
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures=[pool.submit(measureWorker,n,[p for p in pods if p['spec']['nodeName']==n['name']],config,output,seconds) for n in workers]
        records=[f.result() for f in futures]
    command=[sys.executable,'verifyTopologyProtocols.py','--features','results/topology214_features.json',
             '--inventory','results/deployment-cluster/inventory.yaml','--kubeconfig',str(kubeconfig),'--output',str(output/'post-protocols')]
    with (output/'post-protocols.log').open('x') as log:subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,check=True)
    (output/'complete.json').write_text(json.dumps(dict(scope='real_topology_resource_diagnostic_not_model_admitted',
        workers=records,completed=time.time(),postProtocolGate=True),indent=2)+'\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ['config','kubeconfig','gates','output']:parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--seconds',type=float,default=180)
    parser.add_argument('--mapping',type=Path,default=Path('results/routing-diagnostic-deployment/mapping.json'))
    args=parser.parse_args();executeMeasurement(args.config,args.kubeconfig,args.gates,args.output,args.seconds,args.mapping)
