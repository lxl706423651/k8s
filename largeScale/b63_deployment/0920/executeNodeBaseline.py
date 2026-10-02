#!/usr/bin/env python3
# Host runner for idle-node sampling on the dedicated deployment cluster only.
# Requires ready, idle nodes; writes source/config/SSH handles and raw observations.
"""Collect independent deployment-worker background baselines concurrently."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import time

import yaml


def collectWorker(node,config,output,seconds):
    """Collect one owned `node` using `config`; save raw records under `output`."""
    if not node['name'].startswith('placement-deploy0920-') or node['role']!='worker':
        raise ValueError('only dedicated deployment workers may be measured')
    folder=output/node['name'];folder.mkdir()
    options=['-i',str(Path(config['ssh']['key']).expanduser()),'-o','BatchMode=yes',
             '-o','StrictHostKeyChecking=accept-new','-o','ConnectTimeout=5']
    target=config['ssh']['user']+'@'+node['ip']
    actual=subprocess.check_output(['ssh',*options,target,'hostname'],text=True).strip()
    if actual!=node['name']:raise ValueError('SSH hostname differs from requested node')
    remote='/home/'+config['ssh']['user']+'/'+output.name
    subprocess.run(['ssh',*options,target,'mkdir '+shlex.quote(remote)],check=True)
    subprocess.run(['scp',*options,str(output/'collectNodeBaseline.py'),str(output/'measurePressure.py'),target+':'+remote+'/'],check=True)
    command=shlex.join(['python3',remote+'/collectNodeBaseline.py','--seconds',str(seconds),'--output',remote+'/raw.jsonl'])
    manifest=dict(node=node,started=time.time(),remote=remote,command=command,status='running')
    with (folder/'ssh.log').open('x') as log:
        process=subprocess.Popen(['ssh',*options,'-o','ServerAliveInterval=15',target,command],stdout=log,stderr=subprocess.STDOUT)
        manifest['localSshPid']=process.pid
        (folder/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
        code=process.wait()
    manifest.update(returnCode=code,status='completed' if code==0 else 'failed',ended=time.time())
    transfer=subprocess.run(['scp',*options,target+':'+remote+'/raw.jsonl',str(folder/'raw.jsonl')])
    manifest['transferReturnCode']=transfer.returncode
    (folder/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    if code or transfer.returncode:raise RuntimeError('baseline failed; inspect preserved SSH and guest files')
    return manifest


def executeCli():
    """Run workers from explicit cluster config after external readiness validation."""
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--seconds',type=float,default=180)
    args=parser.parse_args();config=yaml.safe_load(args.config.read_text())
    args.output.mkdir(parents=True,exist_ok=False)
    sources={}
    for name in ['collectNodeBaseline.py','measurePressure.py']:
        source=Path(__file__).with_name(name).read_bytes();(args.output/name).write_bytes(source)
        sources[name]=hashlib.sha256(source).hexdigest()
    (args.output/'config.yaml').write_bytes(args.config.read_bytes())
    (args.output/'sources.json').write_text(json.dumps(sources,indent=2)+'\n')
    workers=[n for n in config['nodes'] if n['role']=='worker']
    with ThreadPoolExecutor(max_workers=len(workers)) as pool:
        futures=[pool.submit(collectWorker,n,config,args.output,args.seconds) for n in workers]
        results=[f.result() for f in futures]
    (args.output/'complete.json').write_text(json.dumps(dict(completed=time.time(),workers=results),indent=2)+'\n')


if __name__=='__main__':executeCli()
