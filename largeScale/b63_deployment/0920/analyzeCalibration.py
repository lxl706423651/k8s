#!/usr/bin/env python3
# Read preserved guest raw JSONL and output per-point Q, diagnostics, and figures.
# Offline only; does not fit from incomplete blocks or turn pilot into final validation.
"""Normalize fixed-canary observations against bracketing low-pressure references."""
import argparse
import csv
import json
import math
from pathlib import Path
import statistics

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def reduceProfile(path):
    """Normalize completed blocks in raw JSONL `path`; preserve invalid observations."""
    raw=[json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    rows=[]
    diagnostics=[]
    for block in sorted({r['block'] for r in raw if 'block' in r}):
        records=[r for r in raw if r.get('block')==block]
        endpoints={r['kind']:r for r in records if r['kind'].startswith('baseline_')}
        if len(endpoints)!=2 or any(r['status']!='ok' for r in endpoints.values()):
            diagnostics.append(dict(block=block,status='incomplete_or_invalid_baseline'))
            continue
        start,end=endpoints['baseline_start'],endpoints['baseline_end']
        drift=max(abs(end['canary'][key]/start['canary'][key]-1) for key in ('throughput','p95Seconds'))
        diagnostics.append(dict(block=block,status='ok' if drift<=.2 else 'baseline_drift',
                                maximumRelativeDrift=drift))
        for r in records:
            if r['kind']!='pressure':
                continue
            row=dict(block=block,cpuFraction=r['cpuFraction'],memoryFraction=r['memoryFraction'],
                     mode=r['mode'],status=r['status'],baselineDrift=drift)
            if r['status']=='ok':
                fraction=(r['timestamp']-start['timestamp'])/(end['timestamp']-start['timestamp'])
                refs={key:math.exp((1-fraction)*math.log(start['canary'][key])+
                                  fraction*math.log(end['canary'][key]))
                      for key in ('throughput','p95Seconds')}
                qthr=r['canary']['throughput']/refs['throughput']
                qlat=refs['p95Seconds']/r['canary']['p95Seconds']
                ticks=[b-a for a,b in zip(r['before']['cpuTicks'],r['after']['cpuTicks'])]
                total=sum(ticks[:8])
                row.update(q=min(qthr,qlat),qThroughput=qthr,qLatency=qlat,
                           throughput=r['canary']['throughput'],p95Ms=r['canary']['p95Seconds']*1000,
                           cpuBusy=1-(ticks[3]+ticks[4])/total,steal=ticks[7]/total,
                           actualPressureCpu=sum(c['cpuSeconds']/c['wallSeconds'] for c in r['children'])/r['cpuCount'],
                           availableMemoryFraction=r['after']['memory']['MemAvailable']/r['after']['memory']['MemTotal'],
                           validForFit=drift<=.2)
            rows.append(row)
    return rows,diagnostics


def analyzeBatch(batch, output):
    """Reduce available profile raw files in `batch` and plot diagnostic Q grids."""
    output.mkdir(parents=True,exist_ok=True)
    report=dict(evidenceKind='real_vm_pilot_diagnostic',profiles={})
    allRows=[]
    for path in sorted(batch.glob('*/raw.jsonl')):
        rows,diagnostics=reduceProfile(path)
        name=path.parent.name
        for r in rows:
            allRows.append(dict(profile=name,**r))
        valid=[r for r in rows if r['status']=='ok' and r['validForFit']]
        profile=dict(totalPoints=len(rows),validPoints=len(valid),blocks=diagnostics,
                     statuses={s:sum(r['status']==s for r in rows) for s in {r['status'] for r in rows}})
        if valid:
            profile.update(minQ=min(r['q'] for r in valid),maxSteal=max(r['steal'] for r in valid),
                           cpuTrackingMae=statistics.mean(abs(r['actualPressureCpu']-r['cpuFraction']) for r in valid))
            fig,axes=plt.subplots(1,2,figsize=(10,4))
            for memory in sorted({r['memoryFraction'] for r in valid}):
                xs=sorted({r['cpuFraction'] for r in valid if r['memoryFraction']==memory})
                ys=[statistics.mean(r['q'] for r in valid if r['memoryFraction']==memory and r['cpuFraction']==cpu) for cpu in xs]
                axes[0].plot(xs,ys,'o-',label=f'resident memory={memory:.0%}')
                for cpu in xs:
                    vals=[r['q'] for r in valid if r['memoryFraction']==memory and r['cpuFraction']==cpu]
                    axes[0].vlines(cpu,min(vals),max(vals),alpha=.4)
            axes[0].axhline(.8,color='black',ls='--',label='eta=0.8')
            axes[0].set(xlabel='Commanded background CPU / vCPU count',ylabel='Q=min(throughput ratio, inverse p95 ratio)',title=name+' pilot (ranges, not confidence intervals)')
            axes[0].legend(fontsize=7)
            axes[1].scatter([r['cpuFraction'] for r in valid],[r['actualPressureCpu'] for r in valid])
            axes[1].plot([0,1],[0,1],'k--')
            axes[1].set(xlabel='Commanded CPU fraction',ylabel='Observed background CPU fraction',title='Injection tracking incl. warmup')
            fig.tight_layout()
            fig.savefig(output/(name+'.png'),dpi=150)
            fig.savefig(output/(name+'.pdf'))
            plt.close(fig)
        report['profiles'][name]=profile
    fields=sorted({k for r in allRows for k in r})
    with (output/'points.csv').open('w') as handle:
        writer=csv.DictWriter(handle,fieldnames=fields)
        writer.writeheader()
        writer.writerows(allRows)
    (output/'summary.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


def executeCli():
    """Read batch/output CLI paths and invoke the offline reducer."""
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('batch',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    analyzeBatch(args.batch,args.output)


if __name__=='__main__':
    executeCli()
