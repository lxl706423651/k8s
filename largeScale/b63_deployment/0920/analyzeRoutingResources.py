#!/usr/bin/env python3
# Offline actual-deployment transfer audit; never fits the frozen fixture model.
# Reads completed node windows and compares non-overlapping accounting levels separately.
"""Report resource transfer errors and node overhead for one actual deployment block."""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np

from evaluatePodDemand import FEATURES, checkSupport


def reduceCgroup(samples, level, name=None):
    """Reduce cumulative CPU and peak working set for one cgroup `level` and `name`."""
    rows=[s[level][name] if name is not None else s[level] for s in samples]
    elapsed=rows[-1]['time']-rows[0]['time']
    if elapsed<=0:raise ValueError('invalid cgroup sample duration')
    cpu=(rows[-1]['cpuUsec']-rows[0]['cpuUsec'])/1e6/elapsed
    if cpu<0:raise ValueError('cgroup counter reset')
    return dict(cpuCores=cpu,memoryGiB=max(r['workingSet'] for r in rows)/1024**3,
        meanMemoryGiB=float(np.mean([r['workingSet'] for r in rows]))/1024**3,
        memoryEventDelta={k:rows[-1]['memoryEvents'][k]-rows[0]['memoryEvents'][k] for k in rows[0]['memoryEvents']})


def analyzeResources(batch,modelPath,featuresPath,baselinePath,output):
    """Compare completed `batch` with frozen `modelPath` and preceding empty baseline."""
    if not (batch/'complete.json').exists():raise ValueError('measurement not complete')
    if not json.loads((batch/'post-protocols/summary.json').read_text())['success']:raise ValueError('post protocols failed')
    model=json.loads(modelPath.read_text());features={p['id']:p for p in json.loads(featuresPath.read_text())['pods']}
    baselines={n['node']:n for n in json.loads(baselinePath.read_text())['nodes']}
    pods=[];nodes=[];hashes={}
    for raw in sorted(batch.glob('*/raw.jsonl')):
        rows=[json.loads(line) for line in raw.read_text().splitlines()]
        if rows[-1]['kind']!='complete':raise ValueError('incomplete node')
        meta=rows[0];samples=[r for r in rows if r['kind']=='sample'];name=meta['podMap']['node']
        hashes[name]=hashlib.sha256(raw.read_bytes()).hexdigest()
        names=[p['workload'] for p in meta['podMap']['pods']]
        nodePods=[]
        for workload in names:
            f=features[workload];x=[1]+[f[k] for k in FEATURES[1:]]
            observed=reduceCgroup(samples,'containers',workload)
            parent=reduceCgroup(samples,'podParents',workload)
            row=dict(workload=workload,node=name,featureHullSupported=checkSupport(x[1:],model['supportFeatures']),
                scopeTransferValidated=False,container=observed,podParent=parent)
            for metric in ['cpuCores','memoryGiB']:
                params=model['metrics'][metric];pred=float(np.dot(x,[params['coefficients'][k] for k in FEATURES]))
                upper=pred+params['maximumUnderpredictionCvResidual']
                row[metric]=dict(predicted=pred,upper=upper,observed=observed[metric],underbudget=observed[metric]>upper)
            pods.append(row);nodePods.append(row)
        first,last=samples[0],samples[-1];elapsed=last['monotonic']-first['monotonic']
        ticks=[b-a for a,b in zip(first['node']['cpuTicks'],last['node']['cpuTicks'])]
        busy=sum(ticks[i] for i in (0,1,2,5,6))/meta['clockTicksPerSecond']/elapsed
        sampler=(last['samplerCpuSeconds']-first['samplerCpuSeconds'])/elapsed
        containers=sum(p['container']['cpuCores'] for p in nodePods)
        parents=sum(p['podParent']['cpuCores'] for p in nodePods)
        kube=reduceCgroup(samples,'kubePods')
        mem=float(np.mean([(s['node']['memory']['MemTotal']-s['node']['memory']['MemAvailable'])/1024**3 for s in samples]))
        baseline=baselines[name]
        nodes.append(dict(node=name,pods=len(names),seconds=elapsed,nodeBusyCpuCores=busy,samplerCpuCores=sampler,
            correctedNodeCpuCores=busy-sampler,containerCpuCores=containers,podParentCpuCores=parents,kubePods=kube,
            podParentMinusContainerCpuCores=parents-containers,
            nodeMinusSamplerMinusPodParentsCpuCores=busy-sampler-parents,
            nodeCpuIncreaseFromEmpty=busy-sampler-baseline['meanBackgroundCpuCores'],
            meanUnavailableMemoryGiB=mem,memoryProxyIncreaseFromEmptyGiB=mem-baseline['meanUnavailableMemoryGiB'],
            simultaneousContainerPeakGiB=max(sum(s['containers'][n]['workingSet'] for n in names) for s in samples)/1024**3,
            simultaneousPodParentPeakGiB=max(sum(s['podParents'][n]['workingSet'] for n in names) for s in samples)/1024**3))
    if {p['workload'] for p in pods}!=set(features):raise ValueError('topology coverage mismatch')
    summary={}
    for metric in ['cpuCores','memoryGiB']:
        records=[p[metric] for p in pods];errors=[abs(r['observed']-r['predicted']) for r in records]
        summary[metric]=dict(mae=float(np.mean(errors)),p95AbsoluteError=float(np.quantile(errors,.95)),
            relativeMae=float(np.mean([abs(r['observed']-r['predicted'])/max(r['observed'],1e-12) for r in records])),
            underbudgetCount=sum(r['underbudget'] for r in records),totalObserved=sum(r['observed'] for r in records),
            totalPredicted=sum(r['predicted'] for r in records),totalUpper=sum(r['upper'] for r in records))
    result=dict(scope='one_real_deployment_transfer_block_no_canary',modelSha256=hashlib.sha256(modelPath.read_bytes()).hexdigest(),
        rawSha256=hashes,summary=summary,nodes=nodes,pods=pods,
        limitations=['214 Pods share one deployment and are not independent trials.',
            'Actual bash/start.sh, kernel/FIB, routing scale and timers differ from the fixture.',
            'Peak working set over 180 seconds differs from fixture 20-second peak.',
            'Container, Pod parent and kubepods hierarchy levels overlap and must not be added.',
            'MemTotal-MemAvailable is a different metric from cgroup working set.',
            'Signed accounting residuals are retained; concurrent reads have temporal skew.'])
    output.mkdir(parents=True,exist_ok=False);(output/'summary.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(dict(summary=summary,nodes=nodes),indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ['batch','model','features','baseline','output']:parser.add_argument('--'+name,type=Path,required=True)
    a=parser.parse_args();analyzeResources(a.batch,a.model,a.features,a.baseline,a.output)
