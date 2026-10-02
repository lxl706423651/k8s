#!/usr/bin/env python3
# Guest-only, read-only baseline sampler for an otherwise idle deployment node.
# Inputs: duration/interval/output. Writes exclusive JSONL; never starts load or services.
"""Measure node background CPU, available-memory pressure and cgroup diagnostics."""
import argparse
import json
import os
from pathlib import Path
import resource
import time

from measurePressure import readHostMetrics


def readCgroupDiagnostics():
    """Read selected cgroup-v2 counters; nested groups must not be added together."""
    result={}
    root=Path('/sys/fs/cgroup')
    for name in ['system.slice','user.slice','system.slice/k3s.service',
                 'system.slice/k3s-agent.service','system.slice/containerd.service',
                 'system.slice/qemu-guest-agent.service']:
        folder=root/name
        values={}
        for field in ['memory.current','memory.stat','cpu.stat']:
            path=folder/field
            if path.exists():values[field]=path.read_text().strip()
        if values:result[name]=values
    return result


def readSample():
    """Snapshot guest counters and sampler overhead without modifying the node."""
    usage=resource.getrusage(resource.RUSAGE_SELF)
    return dict(timestamp=time.time(),monotonic=time.monotonic(),host=readHostMetrics(),
                samplerCpuSeconds=usage.ru_utime+usage.ru_stime,
                samplerMaxRssKiB=usage.ru_maxrss,cgroups=readCgroupDiagnostics())


def calculateInterval(before,after,ticksPerSecond):
    """Reduce consecutive snapshots using CPU tick rate, excluding steal and idle."""
    elapsed=after['monotonic']-before['monotonic']
    ticks=[b-a for a,b in zip(before['host']['cpuTicks'],after['host']['cpuTicks'])]
    # guest/guest_nice are already included in user/nice. Never count them twice.
    busy=sum(ticks[i] for i in (0,1,2,5,6))/ticksPerSecond/elapsed
    overhead=(after['samplerCpuSeconds']-before['samplerCpuSeconds'])/elapsed
    return dict(seconds=elapsed,totalBusyCpuCores=busy,samplerCpuCores=overhead,
                estimatedBackgroundCpuCores=max(0.,busy-overhead),
                stealCpuEquivalent=ticks[7]/ticksPerSecond/elapsed,
                unavailableMemoryGiB=(after['host']['memory']['MemTotal']-
                    after['host']['memory']['MemAvailable'])/1024**3)


def collectBaseline(seconds,interval,output):
    """Collect for `seconds` with sample `interval`, writing a new `output` file."""
    if seconds<=0 or interval<=0 or interval>seconds:raise ValueError('invalid sampling duration')
    output.parent.mkdir(parents=True,exist_ok=True)
    ticks=os.sysconf('SC_CLK_TCK')
    start=readSample()
    with output.open('x') as handle:
        def writeRecord(record):
            """Flush one `record` to preserve partial observations on interruption."""
            handle.write(json.dumps(record)+'\n');handle.flush();os.fsync(handle.fileno())
        writeRecord(dict(kind='metadata',uname=list(os.uname()),cpuCount=os.cpu_count(),
            clockTicksPerSecond=ticks,seconds=seconds,interval=interval,
            scope='idle_node_background_no_canary_or_emulation_load',
            note='MemTotal-MemAvailable is a pressure proxy, not a Pod working-set sum. Cgroups overlap.'))
        writeRecord(dict(kind='sample',**start))
        before=start
        while time.monotonic()-start['monotonic']<seconds:
            time.sleep(min(interval,max(0.,seconds-(time.monotonic()-start['monotonic']))))
            after=readSample()
            writeRecord(dict(kind='sample',**after,reduced=calculateInterval(before,after,ticks)))
            before=after
        writeRecord(dict(kind='complete',timestamp=time.time(),actualSeconds=before['monotonic']-start['monotonic']))


def executeCli():
    """Parse explicit measurement duration and output; do not configure services."""
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds',type=float,default=180)
    parser.add_argument('--interval',type=float,default=1)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();collectBaseline(args.seconds,args.interval,args.output)


if __name__=='__main__':executeCli()
