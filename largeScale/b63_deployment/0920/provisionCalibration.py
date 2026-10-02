#!/usr/bin/env python3
# Read calibration.yaml and create only new named calibration VMs/network.
# Writes cloud-init/disk artifacts in configured storage; never replaces VMs.
"""Provision isolated small VM profiles for the PDF response-surface experiment."""
import argparse
import ipaddress
import json
from pathlib import Path
import shlex
import subprocess
import yaml


def executeCommand(command):
    """Execute argument list `command`, emitting an auditable command trace."""
    print('+ '+shlex.join(command), flush=True)
    return subprocess.run(command, check=True, text=True, capture_output=True).stdout


def provisionProfiles(config):
    """Provision missing resources in `config`; refuse every preexisting name."""
    root = Path(config['storage'])
    network = config['network']
    # Preflight all names before creating any resource.
    for profile in config['profiles']:
        found = subprocess.run(['virsh','dominfo',profile['name']], capture_output=True)
        if found.returncode == 0 or (root/profile['name']).exists():
            raise RuntimeError('Refusing existing domain/storage: '+profile['name'])
    if subprocess.run(['virsh','net-info',network['name']], capture_output=True).returncode == 0:
        raise RuntimeError('Refusing existing network: '+network['name'])
    root.mkdir(parents=True, exist_ok=True)
    xml = root/'network.xml'
    xml.write_text(f"<network><name>{network['name']}</name><forward mode='nat'/>"
                  f"<bridge name='{network['bridge']}' stp='on' delay='0'/>"
                  f"<ip address='{network['gateway']}' netmask='{ipaddress.ip_network(network['cidr']).netmask}'/>"
                  '</network>')
    executeCommand(['virsh','net-define',str(xml)])
    executeCommand(['virsh','net-start',network['name']])
    key = Path(config['sshKey']+'.pub').expanduser().read_text().strip()
    records = []
    for p in config['profiles']:
        folder = root/p['name']
        folder.mkdir()
        user = dict(users=[dict(name=config['sshUser'], groups='adm,sudo', shell='/bin/bash',
                               sudo='ALL=(ALL) NOPASSWD:ALL', ssh_authorized_keys=[key])],
                    ssh_pwauth=False, package_update=False)
        (folder/'user-data').write_text('#cloud-config\n'+yaml.safe_dump(user))
        (folder/'meta-data').write_text(yaml.safe_dump({'instance-id':p['name'],'local-hostname':p['name']}))
        net = {'version':2,'ethernets':{'ens3':{'match':{'macaddress':p['mac']},'set-name':'ens3',
               'addresses':[p['address']+'/24'],'routes':[{'to':'default','via':network['gateway']}],
               'nameservers':{'addresses':['1.1.1.1','8.8.8.8']}}}}
        (folder/'network-config').write_text(yaml.safe_dump(net))
        disk, seed = folder/'disk.qcow2', folder/'seed.img'
        executeCommand(['qemu-img','create','-f','qcow2','-F','qcow2','-b',config['baseImage'],str(disk),'12G'])
        executeCommand(['cloud-localds','--network-config',str(folder/'network-config'),str(seed),
                        str(folder/'user-data'),str(folder/'meta-data')])
        executeCommand(['virt-install','--name',p['name'],'--memory',str(p['memory']*1024),
                        '--vcpus',str(p['cpus']),'--cpu','host-passthrough',
                        '--disk',f'path={disk},format=qcow2,bus=virtio',
                        '--disk',f'path={seed},device=cdrom','--os-variant','ubuntu22.04',
                        '--network',f"network={network['name']},model=virtio,mac={p['mac']}",
                        '--graphics','none','--noautoconsole','--import'])
        for vcpu, host in enumerate(p['hostCpus']):
            executeCommand(['virsh','vcpupin',p['name'],str(vcpu),str(host),'--live','--config'])
        records.append(p)
        (Path('results')/(network['name']+'-profiles.json')).write_text(json.dumps(records, indent=2)+'\n')
    return records


def executeCli():
    """Parse configuration and perform explicitly requested provisioning."""
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=Path('calibration.yaml'))
    args=parser.parse_args()
    provisionProfiles(yaml.safe_load(args.config.read_text()))


if __name__=='__main__':
    executeCli()
