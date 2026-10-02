#!/usr/bin/env python3
# Guest root-only steady routing sampler; does not start/stop any process or write routes.
# Requires live Pod/container identity map, archived collectors, exclusive raw JSONL output.
"""Measure routing containers, Pod parents and whole-node counters in one window."""
import argparse
import json
import os
from pathlib import Path
import re
import resource
import time

from measurePressure import readHostMetrics
from measurePodRouting import readCgroup


def locateCgroups(pods):
    """Resolve exact live container IDs in `pods`; refuse absent/ambiguous Pod parents."""
    root=Path('/sys/fs/cgroup/kubepods.slice')
    paths=list(root.rglob('cri-containerd-*.scope'))
    result={}
    for pod in pods:
        cid=pod['containerId']
        if not re.fullmatch(r'[0-9a-f]{64}',cid):raise ValueError('invalid container ID')
        matches=[p for p in paths if p.name=='cri-containerd-'+cid+'.scope']
        if len(matches)!=1:raise ValueError('container cgroup missing or ambiguous: '+pod['workload'])
        path=matches[0]
        if 'pod'+pod['uid'].replace('-','_') not in path.parent.name:raise ValueError('Pod parent differs from live UID')
        if pod['workload'] in result:raise ValueError('duplicate workload')
        result[pod['workload']]=dict(container=path,parent=path.parent)
    return result


def readRoutingSnapshot(paths):
    """Read node plus selected `paths`; cgroup hierarchy levels are not additive."""
    usage=resource.getrusage(resource.RUSAGE_SELF)
    return dict(timestamp=time.time(),monotonic=time.monotonic(),node=readHostMetrics(),
        samplerCpuSeconds=usage.ru_utime+usage.ru_stime,samplerMaxRssKiB=usage.ru_maxrss,
        kubePods=readCgroup(Path('/sys/fs/cgroup/kubepods.slice')),
        containers={name:readCgroup(p['container']) for name,p in paths.items()},
        podParents={name:readCgroup(p['parent']) for name,p in paths.items()})


def collectRouting(podMap,seconds,interval,output):
    """Sample stable identities from `podMap` for `seconds` at `interval` into `output`."""
    if seconds<=0 or interval<=0 or interval>seconds:raise ValueError('invalid sampling interval')
    identities=json.loads(podMap.read_text())
    if identities['node']!=os.uname().nodename:raise ValueError('wrong worker for Pod map')
    paths=locateCgroups(identities['pods'])
    if identities['pods'] and not paths:raise ValueError('no target routing containers')
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('x') as handle:
        def writeRecord(record):
            """Flush each `record` and retain incomplete data if live identities vanish."""
            handle.write(json.dumps(record)+'\n');handle.flush();os.fsync(handle.fileno())
        writeRecord(dict(kind='metadata',scope='real_topology_routing_no_canary',podMap=identities,
            seconds=seconds,interval=interval,cpuCount=os.cpu_count(),clockTicksPerSecond=os.sysconf('SC_CLK_TCK'),
            paths={name:{key:str(value) for key,value in group.items()} for name,group in paths.items()},
            note='Container, Pod parent and kubepods counters overlap; never sum hierarchy levels.'))
        start=time.monotonic()
        while True:
            writeRecord(dict(kind='sample',**readRoutingSnapshot(paths)))
            elapsed=time.monotonic()-start
            if elapsed>=seconds:break
            time.sleep(min(interval,seconds-elapsed))
        if locateCgroups(identities['pods'])!=paths:raise ValueError('container identity changed')
        writeRecord(dict(kind='complete',elapsedSeconds=time.monotonic()-start))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pod-map',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--seconds',type=float,default=180);parser.add_argument('--interval',type=float,default=1)
    args=parser.parse_args();collectRouting(args.pod_map,args.seconds,args.interval,args.output)
