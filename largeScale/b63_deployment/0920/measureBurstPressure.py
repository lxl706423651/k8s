#!/usr/bin/env python3
# Isolated burst-period diagnostic: same average CPU/allocated memory, fixed canary unchanged.
# Writes raw JSONL and progress to stdout; owns/cleans only its child processes.
"""Measure a fixed SHA256 canary under randomized CPU/Memory pressure blocks."""
import argparse
import hashlib
import json
import multiprocessing as mp
import os
from pathlib import Path
import random
import resource
import statistics
import time


def readCpuPlacement():
    """Record current process CPU affinity and last scheduler CPU for diagnostics."""
    fields=Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()
    return dict(allowedCpus=sorted(os.sched_getaffinity(0)),lastCpu=int(fields[36]))


def readHostMetrics():
    """Read guest aggregate CPU ticks, memory and PSI without privileged access."""
    memory = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key, value = line.split(':', 1)
        memory[key] = int(value.split()[0])*1024
    cpuLines=[line.split() for line in Path('/proc/stat').read_text().splitlines() if line.startswith('cpu')]
    return dict(cpuTicks=list(map(int,cpuLines[0][1:])),
                perCpuTicks={line[0]:list(map(int,line[1:])) for line in cpuLines[1:]},
                processPlacement=readCpuPlacement(),
                memory={k: memory[k] for k in ('MemTotal','MemAvailable','SwapTotal','SwapFree')},
                psi={name:Path('/proc/pressure/'+name).read_text() for name in ('cpu','memory','io')},
                load=list(os.getloadavg()))


def injectPressure(fraction, memoryBytes, mode, ready, stop, output):
    """Consume per-core `fraction` and `memoryBytes`; emit actual CPU/RSS on exit.

    Cumulative CPU debt includes loop overhead and chunk overshoot.
    resident mode uses SHA256 compute while retaining touched memory; stream mode
    walks that allocation within the same CPU-time budget. Never allocates host RAM.
    """
    try:
        periods = {"resident_5ms": .005, "resident_50ms": .05}
        period = periods[mode]
        mode = "resident"
        array = bytearray(memoryBytes)
        for i in range(0, len(array), 4096):
            array[i] = 1
        data = b'a'*(64*1024)
        ready.put(dict(pid=os.getpid(), allocated=memoryBytes))
        begin, cpuBegin = time.monotonic(), time.process_time()
        index = 0
        integer = 1
        while not stop.is_set():
            if fraction == 0:
                stop.wait()
                break
            wallStart, cpuStart = time.monotonic(), time.process_time()
            # Pay back work-chunk and loop-overhead CPU on later periods.
            # Cap positive credit to one period so scheduler stalls cannot bank bursts.
            remaining = fraction*(wallStart-begin+period)-(cpuStart-cpuBegin)
            budget = max(0., min(fraction*period, remaining))
            while time.process_time()-cpuStart < budget:
                if mode == 'stream' and len(array):
                    # Stream through the allocation in 64KiB chunks; finer CPU-budget resolution.
                    end = min(len(array),index+64*1024)
                    hashlib.sha256(memoryview(array)[index:end]).digest()
                    index = 0 if end == len(array) else end
                elif mode == 'integer':
                    integer = (integer * 1664525 + 1013904223) & 0xffffffff
                else:
                    hashlib.sha256(data).digest()
            stop.wait(max(0, period-(time.monotonic()-wallStart)))
        output.put(dict(pid=os.getpid(), periodSeconds=period, cpuSeconds=time.process_time()-cpuBegin,
                        wallSeconds=time.monotonic()-begin,
                        maxRssKiB=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                        processPlacement=readCpuPlacement()))
    except BaseException as error:
        ready.put(dict(error=repr(error)))
        raise


def measureCanary(seconds):
    """Measure fixed 8×1MiB SHA256 tasks for `seconds`; report raw task latencies."""
    data=b'x'*(1024*1024)
    latencies=[]
    begin,cpuBegin=time.monotonic(),time.process_time()
    while time.monotonic()-begin < seconds:
        start=time.monotonic()
        for _ in range(8):
            hashlib.sha256(data).digest()
        latencies.append(time.monotonic()-start)
    elapsed=time.monotonic()-begin
    ordered=sorted(latencies)
    return dict(tasks=len(latencies), seconds=elapsed, throughput=len(latencies)/elapsed,
                p95Seconds=ordered[min(len(ordered)-1,int(.95*len(ordered)))],
                medianSeconds=statistics.median(latencies),latencySeconds=latencies,
                cpuSeconds=time.process_time()-cpuBegin)


def measurePoint(cpuFraction, memoryFraction, mode, seconds, warmup, warmupMode="pressure_only", memoryReserveFraction=.2):
    """Run one bounded pressure point and clean owned children even on exceptions."""
    cpus=os.cpu_count()
    total=readHostMetrics()['memory']['MemTotal']
    allocated=int(total*memoryFraction)
    # Preserve the declared memory reserve (at least 64MiB) before any allocation.
    if allocated > readHostMetrics()['memory']['MemAvailable']-max(memoryReserveFraction*total,64*1024**2):
        return dict(status='rejected_memory_headroom', cpuFraction=cpuFraction,
                    memoryFraction=memoryFraction,mode=mode)
    stop,ready,output=mp.Event(),mp.Queue(),mp.Queue()
    processes=[]
    childMetrics=[]
    try:
        if cpuFraction or memoryFraction:
            for _ in range(cpus):
                p=mp.Process(target=injectPressure,args=(cpuFraction,allocated//cpus,mode,ready,stop,output))
                p.start()
                processes.append(p)
            for _ in processes:
                item=ready.get(timeout=30)
                if 'error' in item:
                    raise RuntimeError(item['error'])
        if warmupMode=='full_workload':
            measureCanary(warmup)
        elif warmupMode=='pressure_only':
            time.sleep(warmup)
        else:
            raise ValueError('unknown warmupMode')
        before=readHostMetrics()
        measurement=measureCanary(seconds)
        after=readHostMetrics()
        if any(not p.is_alive() for p in processes):
            raise RuntimeError('pressure child terminated during measurement')
        stop.set()
        for _ in processes:
            childMetrics.append(output.get(timeout=10))
        return dict(status='ok',cpuFraction=cpuFraction,memoryFraction=memoryFraction,mode=mode,
                    cpuCount=cpus,allocatedBytes=allocated,before=before,after=after,
                    canary=measurement,children=childMetrics)
    finally:
        stop.set()
        for p in processes:
            p.join(timeout=5)
            if p.is_alive():
                p.terminate()
                p.join(timeout=5)


def executeExperiment(config, destination):
    """Execute randomized blocks from `config`, flushing every raw record to disk."""
    if not .1<=config.get('memoryReserveFraction',.2)<=.5:
        raise ValueError('memory reserve fraction outside declared safe range')
    rng=random.Random(config['seed'])
    destination.parent.mkdir(parents=True,exist_ok=True)
    with destination.open('x') as handle:
        def writeRecord(record):
            """Persist one `record` and emit compact progress; no raw data discarded."""
            record['timestamp']=time.time()
            handle.write(json.dumps(record)+'\n')
            handle.flush()
            os.fsync(handle.fileno())
            print(json.dumps({k:v for k,v in record.items() if k not in ('before','after','canary','children')}),flush=True)
        writeRecord(dict(kind='metadata',config=config,uname=list(os.uname()),initial=readHostMetrics()))
        for block in range(config['repeats']):
            points=[(c,m,mode) for c in config['cpu'] for m in config['memory'] for mode in config['modes']]
            rng.shuffle(points)
            for label,point in [('baseline_start',(0,0,'resident'))]+[('pressure',p) for p in points]+[('baseline_end',(0,0,'resident'))]:
                record=measurePoint(*point,config['seconds'],config['warmup'],config.get('warmupMode','pressure_only'),config.get('memoryReserveFraction',.2))
                writeRecord(dict(kind=label,block=block,**record))


def executeCli():
    """Read JSON config and exclusive output file supplied by host runner."""
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    executeExperiment(json.loads(args.config.read_text()),args.output)


if __name__=='__main__':
    executeCli()
