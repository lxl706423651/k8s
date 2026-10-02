#!/usr/bin/env python3
# Host orchestrator: copy the fixed collector/config into dedicated VMs and run serially.
# Retains source/config hashes and SSH output; never retries a started experiment.
"""Execute a reproducible batch on owned calibration or deployment experiment VMs."""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import time
import yaml


def executeBatch(config, destination):
    """Run profiles in `config` serially and save every guest raw file in destination."""
    destination.mkdir(parents=True,exist_ok=False)
    collector=config.get('collectorSource','measurePressure.py')
    if collector not in ['measurePressure.py','measureNodePressure.py','measureBurstPressure.py']:
        raise ValueError('unsupported collector source')
    source=Path(__file__).with_name(collector)
    (destination/'measurePressure.py').write_bytes(source.read_bytes())
    (destination/'config.yaml').write_text(yaml.safe_dump(config))
    experiment=destination/'experiment.json'
    experiment.write_text(json.dumps(config['pilot'],indent=2))
    manifest=dict(started=time.time(),collectorSource=collector,sourceSha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                  configSha256=hashlib.sha256(experiment.read_bytes()).hexdigest(),profiles=[])
    manifestPath=destination/'manifest.json'
    options=['-i',str(Path(config['sshKey']).expanduser()),'-o','BatchMode=yes',
             '-o','StrictHostKeyChecking=accept-new','-o','ConnectTimeout=5']
    for profile in config['profiles']:
        if not profile['name'].startswith(('placement-0920-','placement-deploy0920-')):
            raise ValueError('non-calibration VM name')
        target=config['sshUser']+'@'+profile['address']
        folder=destination/profile['id']
        folder.mkdir()
        readiness=subprocess.run(['ssh',*options,target,'cloud-init status'],text=True,capture_output=True,check=True)
        (folder/'readiness.txt').write_text(readiness.stdout)
        if not any('status: '+state in readiness.stdout for state in ('done','disabled')):
            raise RuntimeError('Guest cloud-init is not terminal; do not measure startup activity')
        hostname=subprocess.check_output(['ssh',*options,target,'hostname'],text=True).strip()
        if hostname!=profile['name']:
            raise RuntimeError('SSH guest hostname does not match the experiment profile')
        remote='/home/'+config['sshUser']+'/placement-'+destination.name
        subprocess.run(['ssh',*options,target,'mkdir -p '+shlex.quote(remote)],check=True)
        subprocess.run(['scp',*options,str(destination/'measurePressure.py'),str(experiment),target+':'+remote+'/'],check=True)
        record=dict(profile=profile,start=time.time(),status='running',remote=remote)
        manifest['profiles'].append(record)
        manifestPath.write_text(json.dumps(manifest,indent=2))
        print('START '+profile['id'],flush=True)
        with (folder/'ssh.log').open('w') as log:
            command=shlex.join(['python3',remote+'/measurePressure.py','--config',remote+'/experiment.json',
                               '--output',remote+'/raw.jsonl'])
            process=subprocess.Popen(['ssh',*options,'-o','ServerAliveInterval=15',target,command],stdout=log,stderr=subprocess.STDOUT)
            record['localSshPid']=process.pid
            manifestPath.write_text(json.dumps(manifest,indent=2))
            code=process.wait()
        record.update(status='completed' if code==0 else 'failed',returnCode=code,end=time.time())
        transfer=subprocess.run(['scp',*options,target+':'+remote+'/raw.jsonl',str(folder/'raw.jsonl')])
        record['transferReturnCode']=transfer.returncode
        manifestPath.write_text(json.dumps(manifest,indent=2))
        if code or transfer.returncode:
            raise RuntimeError('Experiment/transfer failed; inspect guest PID and raw data before any retry')
        print('DONE '+profile['id'],flush=True)
    manifest['completed']=time.time()
    manifestPath.write_text(json.dumps(manifest,indent=2))


def executeCli():
    """Read YAML configuration and require a new output directory."""
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=Path('calibration.yaml'))
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    executeBatch(yaml.safe_load(args.config.read_text()),args.output)


if __name__=='__main__':
    executeCli()
