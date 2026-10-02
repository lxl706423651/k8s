#!/usr/bin/env python3
# Guest root-only: create owned network namespaces and BIRD daemons for isolated calibration.
# Inputs case JSON; outputs raw per-case configs/protocol gates/resource metrics.
# Never enables a BIRD kernel protocol; only its own namespaces/processes are removed.
"""Measure BIRD resource demand over full-rank controlled protocol feature designs."""
import argparse
import json
import os
from pathlib import Path
import random
import re
import subprocess
import time


def executeCommand(command):
    """Execute argument list `command`; retain errors in the caller's run log."""
    return subprocess.run(command,text=True,capture_output=True,check=True).stdout


def createConfiguration(case,peer=False,routeOffset=0):
    """Generate explicit BIRD config for `case`, target/peer side and static-route generation."""
    side=2 if peer else 1
    lines=[f'router id 10.255.0.{side};','protocol device { scan time 1; }',
           'protocol direct { ipv4; interface "r*"; }']
    ibgp,ebgp,ospf=(case[k] for k in ('niBGP','neBGP','nOSPF'))
    if peer:
        lines.append('protocol static advertised { ipv4;')
        for i in range(case['routes']):
            prefix=0xAC100000+(routeOffset*case['routes']+i)*256
            address='.'.join(str((prefix>>shift)&255) for shift in (24,16,8,0))
            lines.append(f'route {address}/24 blackhole;')
        lines.append('}')
    for i in range(ibgp+ebgp):
        targetAs=65000
        peerAs=65000 if i<ibgp else 65100
        localAs,remoteAs=(peerAs,targetAs) if peer else (targetAs,peerAs)
        if case.get('sharedBgpNetwork'):
            localIp=f'10.200.0.{2*i+side}'
            remoteIp=f'10.200.0.{2*i+3-side}'
        else:
            localIp=f'10.200.{i}.{side}';remoteIp=f'10.200.{i}.{3-side}'
        options=('strict bind on;\n passive '+('off' if peer else 'on')+';') if case.get('sharedBgpNetwork') else ''
        lines.append(f'''protocol bgp b{i} {{
 local {localIp} as {localAs};
 neighbor {remoteIp} as {remoteAs};
 {options}
 connect retry time 1;
 hold time 30;
 ipv4 {{ import all; export all; next hop self; }};
}}''')
    if ospf:
        lines.append('protocol ospf o1 { ipv4 { import all; export none; }; area 0 {')
        offset=int(bool(ibgp+ebgp)) if case.get("sharedBgpNetwork") else ibgp+ebgp
        for i in range(offset,offset+ospf):
            lines.append(f'interface "r{i}" {{ type ptp; hello 1; dead 4; }};')
        lines.append('}; }')
    return '\n'.join(lines)+'\n'


def readProcess(pid):
    """Read BIRD `pid` CPU scheduler nanoseconds, RSS/PSS and guest interference."""
    status=Path(f'/proc/{pid}/status').read_text()
    rss=int(re.search(r'^VmRSS:\s+(\d+)',status,re.M)[1])*1024
    smaps=Path(f'/proc/{pid}/smaps_rollup').read_text()
    pss=int(re.search(r'^Pss:\s+(\d+)',smaps,re.M)[1])*1024
    return dict(time=time.monotonic(),cpuNs=int(Path(f'/proc/{pid}/schedstat').read_text().split()[0]),
                rssBytes=rss,pssBytes=pss,load=list(os.getloadavg()),
                cpuTicks=list(map(int,Path('/proc/stat').read_text().splitlines()[0].split()[1:])))


def verifyProtocols(socket,case,timeout=60):
    """Verify every configured BGP/OSPF target session for `case` below load1=50."""
    begin=time.monotonic()
    last=''
    neighbors=''
    while time.monotonic()-begin<timeout:
        if os.getloadavg()[0]>=50:
            time.sleep(1);continue
        result=subprocess.run(['birdc','-s',str(socket),'show','protocols'],text=True,capture_output=True)
        last=result.stdout+result.stderr
        good=result.returncode==0
        for i in range(case['niBGP']+case['neBGP']):
            line=next((line for line in last.splitlines() if re.match(rf'^b{i}\s',line)), '')
            good=good and 'Established' in line and bool(re.search(r'\bup\b',line))
        if case['nOSPF']:
            neighbors=executeCommand(['birdc','-s',str(socket),'show','ospf','neighbors'])
            good=good and len(re.findall(r'\bFull\b',neighbors,re.I))==case['nOSPF']
        if good:
            return dict(success=True,load1=os.getloadavg()[0],waitSeconds=time.monotonic()-begin,
                        protocols=last,ospfNeighbors=neighbors,
                        routes=executeCommand(['birdc','-s',str(socket),'show','route','count']))
        time.sleep(1)
    raise RuntimeError('Protocol gate failed: '+last+'\n'+neighbors)


def measureCase(case,folder,seconds):
    """Create owned target/peer namespaces, verify and measure `case`, then clean up."""
    names=['pl0920target','pl0920peer']
    live={line.split()[0] for line in executeCommand(['ip','netns','list']).splitlines()}
    if any(name in live for name in names):
        raise RuntimeError('owned namespace exists: inspect previous run, do not overwrite')
    folder.mkdir(parents=True,exist_ok=False)
    processes=[];created=[];logs=[]
    try:
        for name in names:
            executeCommand(['ip','netns','add',name]);created.append(name)
            executeCommand(['ip','-n',name,'link','set','lo','up'])
        active=case['niBGP']+case['neBGP']+case['nOSPF']
        if case['nnet']<active:
            raise ValueError('one network required per active relation in this design')
        for i in range(case['nnet']):
            a,b=f'plta{i}',f'pltb{i}'
            executeCommand(['ip','link','add',a,'type','veth','peer','name',b])
            for side,(name,device) in enumerate(zip(names,(a,b)),1):
                executeCommand(['ip','link','set',device,'netns',name])
                executeCommand(['ip','-n',name,'link','set',device,'name',f'r{i}'])
                executeCommand(['ip','-n',name,'addr','add',f'10.200.{i}.{side}/30','dev',f'r{i}'])
                executeCommand(['ip','-n',name,'link','set',f'r{i}','up'])
        for peer,name in ((False,names[0]),(True,names[1])):
            suffix='peer' if peer else 'target'
            conf=folder/(suffix+'.conf');conf.write_text(createConfiguration(case,peer))
            executeCommand(['bird','-p','-c',str(conf)])
            log=(folder/(suffix+'.log')).open('w');logs.append(log)
            processes.append(subprocess.Popen(['ip','netns','exec',name,'bird','-f','-c',str(conf),
                '-s',str(folder/(suffix+'.ctl')),'-P',str(folder/(suffix+'.pid'))],stdout=log,stderr=subprocess.STDOUT))
        time.sleep(1)
        gate=verifyProtocols(folder/'target.ctl',case)
        peerGate=verifyProtocols(folder/'peer.ctl',case)
        (folder/'protocol_gate.json').write_text(json.dumps(dict(target=gate,peer=peerGate),indent=2))
        # Steady state is measured independently of the initial convergence work.
        time.sleep(3)
        before=readProcess(processes[0].pid)
        samples=[]
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            samples.append(readProcess(processes[0].pid));time.sleep(.5)
        after=readProcess(processes[0].pid)
        postGate=verifyProtocols(folder/'target.ctl',case)
        return dict(status='ok',case=case,seconds=after['time']-before['time'],
                    cpuCores=(after['cpuNs']-before['cpuNs'])/1e9/(after['time']-before['time']),
                    memoryGiB=max(s['rssBytes'] for s in samples)/(1024**3),
                    before=before,after=after,samples=samples,postGate=postGate,
                    limitation='BIRD process CPU/RSS only; excludes container shell, kernel network work and K3s overhead.')
    finally:
        for p in processes:
            p.terminate()
        for p in processes:
            try:p.wait(timeout=5)
            except subprocess.TimeoutExpired:p.kill();p.wait()
        for log in logs:log.close()
        for name in reversed(created):
            executeCommand(['ip','netns','del',name])


def executeCli():
    """Execute a frozen list of cases on an isolated guest; stop on stage failure."""
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    config=json.loads(args.config.read_text())
    args.output.mkdir(parents=True,exist_ok=False)
    (args.output/'config.json').write_text(json.dumps(config,indent=2))
    order=list(config['cases']);random.Random(config['seed']).shuffle(order)
    with (args.output/'raw.jsonl').open('x') as handle:
        for i,case in enumerate(order):
            print('START '+case['id'],flush=True)
            result=measureCase(case,(args.output/f'{i:03d}-{case["id"]}').resolve(),config['seconds'])
            handle.write(json.dumps(result)+'\n');handle.flush();os.fsync(handle.fileno())
            print(json.dumps({k:result[k] for k in ('status','cpuCores','memoryGiB')}),flush=True)


if __name__=='__main__':
    executeCli()
