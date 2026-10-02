#!/usr/bin/env python3
# Offline reduction of complete idle K3s node baselines; no capacity is auto-selected.
# Inputs batch/output directories; writes JSON/CSV and comparable node time series.
"""Summarize background occupancy without confusing available memory with Pod RSS."""
import argparse
import csv
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np


def analyzeBaseline(batch,output):
    """Require completed `batch`; summarize workers into new artifacts in `output`."""
    complete=json.loads((batch/'complete.json').read_text())
    report=[]
    fig,axes=plt.subplots(2,1,figsize=(9,6),sharex=True)
    for worker in complete['workers']:
        if worker['returnCode'] or worker['transferReturnCode']:raise ValueError('unsuccessful worker')
        name=worker['node']['name'];raw=batch/name/'raw.jsonl'
        records=[json.loads(line) for line in raw.read_text().splitlines()]
        if records[-1]['kind']!='complete':raise ValueError('missing completion record')
        metadata=records[0];samples=[r for r in records if r['kind']=='sample']
        reduced=[r['reduced'] for r in samples if 'reduced' in r]
        if len(reduced)<2:raise ValueError('too few samples')
        if any(r['seconds']<=0 for r in reduced):raise ValueError('nonpositive interval')
        weights=np.array([r['seconds'] for r in reduced])
        cpu=np.array([r['estimatedBackgroundCpuCores'] for r in reduced])
        memory=np.array([r['unavailableMemoryGiB'] for r in reduced])
        steal=np.array([r['stealCpuEquivalent'] for r in reduced])
        third=max(1,len(reduced)//3)
        summary=dict(node=name,cpuCount=metadata['cpuCount'],
            guestMemoryGiB=samples[0]['host']['memory']['MemTotal']/1024**3,
            seconds=float(sum(weights)),samples=len(reduced),
            meanBackgroundCpuCores=float(np.average(cpu,weights=weights)),
            p95BackgroundCpuCores=float(np.quantile(cpu,.95)),
            maximumBackgroundCpuCores=float(max(cpu)),
            meanUnavailableMemoryGiB=float(np.average(memory,weights=weights)),
            p95UnavailableMemoryGiB=float(np.quantile(memory,.95)),
            memoryLastThirdMinusFirstThirdGiB=float(np.mean(memory[-third:])-np.mean(memory[:third])),
            cpuLastThirdMinusFirstThirdCores=float(np.mean(cpu[-third:])-np.mean(cpu[:third])),
            maximumStealCpuEquivalent=float(max(steal)),sha256=hashlib.sha256(raw.read_bytes()).hexdigest())
        report.append(summary)
        elapsed=np.cumsum(weights)
        axes[0].plot(elapsed,cpu,label=name.rsplit('-',1)[-1],alpha=.8)
        axes[1].plot(elapsed,memory,label=name.rsplit('-',1)[-1],alpha=.8)
    axes[0].set_ylabel('Background CPU (cores)');axes[1].set_ylabel('Unavailable memory (GiB)')
    axes[1].set_xlabel('Elapsed seconds')
    for ax in axes:ax.legend();ax.grid(alpha=.2)
    fig.suptitle('Idle K3s workers: measured occupancy, not selected Pod capacity')
    fig.tight_layout();output.mkdir(parents=True,exist_ok=True)
    fig.savefig(output/'node_baseline.png',dpi=160);fig.savefig(output/'node_baseline.pdf');plt.close(fig)
    with (output/'nodes.csv').open('w') as handle:
        writer=csv.DictWriter(handle,fieldnames=list(report[0]));writer.writeheader();writer.writerows(report)
    result=dict(scope='idle_k3s_workers_no_canary',nodes=report,
        note='P95 is a descriptive temporal quantile, not a safety confidence bound. MemTotal-MemAvailable is a pressure proxy; do not sum it with overlapping cgroups.')
    (output/'summary.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result,indent=2))


def executeCli():
    """Reduce completed baseline batch from explicit paths."""
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('batch',type=Path);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();analyzeBaseline(args.batch,args.output)


if __name__=='__main__':executeCli()
