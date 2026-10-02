#!/usr/bin/env python3
# Guest root-only: measure full SEED-image container cgroups in owned netns fixtures.
# Requires Docker, cached image, birdc and measureRouting.py; produces configs/gates/JSONL.
# Creates/removes only pl0920podtarget/pl0920podpeer and pl0920-route-target container.
"""Calibrate route-container working set and CPU with explicit protocol verification."""
import argparse
import json
import os
from pathlib import Path
import random
import subprocess
import time

from measureRouting import createConfiguration,executeCommand,verifyProtocols


def readCgroup(path):
    """Read v2 container `path` usage and conventional working set without inactive file pages."""
    cpu=dict(line.split() for line in (path/'cpu.stat').read_text().splitlines())
    memory=dict(line.split() for line in (path/'memory.stat').read_text().splitlines())
    current=int((path/'memory.current').read_text())
    inactive=int(memory.get('inactive_file',0))
    events=dict(line.split() for line in (path/'memory.events').read_text().splitlines())
    return dict(time=time.monotonic(),cpuUsec=int(cpu['usage_usec']),memoryCurrent=current,
                workingSet=max(0,current-inactive),anonBytes=int(memory['anon']),
                memoryEvents={k:int(v) for k,v in events.items()},load=list(os.getloadavg()))


def measureContainer(case,folder,seconds,image):
    """Measure `case` in cached SEED `image`, persisting `folder`; clean owned resources."""
    names=['pl0920podtarget','pl0920podpeer'];container='pl0920-route-target'
    live={line.split()[0] for line in executeCommand(['ip','netns','list']).splitlines()}
    if any(n in live for n in names):
        raise RuntimeError('owned namespace remains; inspect instead of overwriting')
    if subprocess.run(['docker','inspect',container],capture_output=True).returncode==0:
        raise RuntimeError('owned container remains; inspect instead of overwriting')
    folder.mkdir(parents=True,exist_ok=False)
    (folder/'target.conf').write_text(createConfiguration(case,False))
    (folder/'peer.conf').write_text(createConfiguration(case,True))
    created=[];peer=None;log=None;started=False
    try:
        # SEED's steady entrypoint is tail; transient address setup is replaced by
        # the controlled fixture. BIRD is explicitly started after links exist.
        executeCommand(['docker','run','-d','--name',container,'--network','none','--privileged',
                        '--mount',f'type=bind,source={folder},target=/work',
                        '--entrypoint','/bin/sh',image,'-c','exec tail -f /dev/null'])
        started=True
        pid=int(executeCommand(['docker','inspect','--format','{{.State.Pid}}',container]).strip())
        cgroups=Path(f'/proc/{pid}/cgroup').read_text().splitlines()
        entry=next((line[3:] for line in cgroups if line.startswith('0::')),None)
        if entry is None:raise RuntimeError('cgroup v2 required')
        cgroup=Path('/sys/fs/cgroup')/entry.lstrip('/')
        executeCommand(['ip','netns','attach',names[0],str(pid)]);created.append(names[0])
        executeCommand(['ip','netns','add',names[1]]);created.append(names[1])
        for name in names:executeCommand(['ip','-n',name,'link','set','lo','up'])
        sessions=case['niBGP']+case['neBGP']
        required=case['nOSPF']+(int(bool(sessions)) if case.get('sharedBgpNetwork') else sessions)
        if case['nnet']<required:
            raise ValueError('too few networks for explicit relation fixture')
        for i in range(case['nnet']):
            a,b=f'ppda{i}',f'ppdb{i}'
            executeCommand(['ip','link','add',a,'type','veth','peer','name',b])
            for side,(name,device) in enumerate(zip(names,(a,b)),1):
                executeCommand(['ip','link','set',device,'netns',name])
                executeCommand(['ip','-n',name,'link','set',device,'name',f'r{i}'])
                prefix=24 if i==0 and sessions and case.get('sharedBgpNetwork') else 30
                executeCommand(['ip','-n',name,'addr','add',f'10.200.{i}.{side}/{prefix}','dev',f'r{i}'])
                executeCommand(['ip','-n',name,'link','set',f'r{i}','up'])
        if case.get('sharedBgpNetwork'):
            if sessions>126:
                raise ValueError('shared /24 fixture supports at most 126 BGP sessions')
            for j in range(1,sessions):
                for side,name in enumerate(names,1):
                    executeCommand(['ip','-n',name,'addr','add',f'10.200.0.{2*j+side}/24','dev','r0'])
        executeCommand(['docker','exec',container,'bird','-p','-c','/work/target.conf'])
        executeCommand(['docker','exec','-d',container,'bird','-f','-c','/work/target.conf',
                        '-s','/work/target.ctl','-P','/work/target.pid'])
        log=(folder/'peer.log').open('w')
        peer=subprocess.Popen(['ip','netns','exec',names[1],'bird','-f','-c',str(folder/'peer.conf'),
                               '-s',str(folder/'peer.ctl'),'-P',str(folder/'peer.pid')],stdout=log,stderr=subprocess.STDOUT)
        time.sleep(1)
        gates=dict(target=verifyProtocols(folder/'target.ctl',case),peer=verifyProtocols(folder/'peer.ctl',case))
        (folder/'protocol_gate.json').write_text(json.dumps(gates,indent=2))
        # Explicit route installation is absent: the fixture has no kernel protocol.
        time.sleep(3)
        before=readCgroup(cgroup);samples=[]
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            samples.append(readCgroup(cgroup));time.sleep(.5)
        after=readCgroup(cgroup)
        post=verifyProtocols(folder/'target.ctl',case)
        if after['memoryEvents'].get('oom_kill',0):raise RuntimeError('container OOM')
        return dict(status='ok',scope='seed_router_container_cgroup',case=case,postGate=post,
                    cpuCores=(after['cpuUsec']-before['cpuUsec'])/1e6/(after['time']-before['time']),
                    memoryGiB=max(s['workingSet'] for s in samples)/1024**3,
                    memoryCurrentMaxGiB=max(s['memoryCurrent'] for s in samples)/1024**3,
                    before=before,after=after,samples=samples,
                    limitations='Includes target cgroup charges and tail+BIRD; excludes daemon/shim/kubelet and sandbox overhead, and uncharged shared kernel work.')
    finally:
        if peer is not None:
            peer.terminate()
            try:peer.wait(timeout=5)
            except subprocess.TimeoutExpired:peer.kill();peer.wait()
        if log is not None:log.close()
        for name in reversed(created):executeCommand(['ip','netns','del',name])
        if started:executeCommand(['docker','rm','-f',container])


def executeCli():
    """Execute frozen sample config in a new guest output directory."""
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--image',required=True)
    args=parser.parse_args();config=json.loads(args.config.read_text())
    args.output.mkdir(parents=True,exist_ok=False)
    for source in (Path(__file__),Path(__file__).with_name('measureRouting.py')):
        (args.output/source.name).write_bytes(source.read_bytes())
    imageId=executeCommand(['docker','image','inspect',args.image,'--format','{{.Id}}']).strip()
    (args.output/'metadata.json').write_text(json.dumps(dict(image=imageId,config=config,started=time.time()),indent=2))
    order=list(config['cases']);random.Random(config['seed']).shuffle(order)
    with (args.output/'raw.jsonl').open('x') as handle:
        for i,case in enumerate(order):
            print('START '+case['id'],flush=True)
            result=measureContainer(case,(args.output/f'{i:03d}-{case["id"]}').resolve(),config['seconds'],imageId)
            handle.write(json.dumps(result)+'\n');handle.flush();os.fsync(handle.fileno())
            print(json.dumps({k:result[k] for k in ('status','cpuCores','memoryGiB')}),flush=True)
    (args.output/'complete.json').write_text(json.dumps(dict(completed=time.time(),cases=len(order))))


if __name__=='__main__':
    executeCli()
