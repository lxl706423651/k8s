#!/usr/bin/env python3
# Offline comparison of completed pressure-only and full-workload warmup diagnostics.
# Requires raw JSONL controls; writes figures/JSON without fitting response surfaces.
"""Show repeated low-load order association and whether added warmup removes it."""
import argparse
import json
from pathlib import Path
import statistics

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from analyzeCalibration import reduceProfile


def readDiagnostic(path):
    """Read completed `path`, preserving order and refusing incomplete blocks."""
    raw=[json.loads(line) for line in path.read_text().splitlines()]
    metadata=next(r for r in raw if r['kind']=='metadata')
    rows,blocks=reduceProfile(path)
    if len(blocks)!=metadata['config']['repeats'] or any(b['status']!='ok' for b in blocks):
        raise ValueError('diagnostic blocks incomplete or invalid')
    order={}
    for record in raw:
        if record['kind']=='pressure':order.setdefault(record['block'],[]).append(record['cpuFraction'])
    if any(r['status']!='ok' or not r['validForFit'] for r in rows):raise ValueError('invalid point')
    return rows,order,metadata['config']


def analyzeWarmup(control,warm,output):
    """Compare paired designs `control` and `warm`; save descriptive `output` artifacts."""
    first,firstOrder,firstConfig=readDiagnostic(control)
    second,secondOrder,secondConfig=readDiagnostic(warm)
    keys=['seed','repeats','seconds','warmup','cpu','memory','modes']
    if any(firstConfig[k]!=secondConfig[k] for k in keys) or firstOrder!=secondOrder:
        raise ValueError('diagnostics do not share a fixed design/order')
    if firstConfig.get('warmupMode','pressure_only')!='pressure_only' or secondConfig.get('warmupMode')!='full_workload':
        raise ValueError('expected pressure-only control and full-workload warmup')
    output.mkdir(parents=True,exist_ok=True)
    report={}
    fig,axes=plt.subplots(1,2,figsize=(10,3.8),sharey=True)
    for ax,(label,rows) in zip(axes,[('Pressure-only warmup',first),('Full-workload warmup',second)]):
        for cpu in firstConfig['cpu']:
            selected=sorted([r for r in rows if r['cpuFraction']==cpu],key=lambda r:r['block'])
            ax.plot([r['block'] for r in selected],[r['q'] for r in selected],'o-',label=f'CPU pressure {cpu:g}')
        ax.axhline(.8,color='black',linestyle='--',label='Preset eta = 0.8')
        ax.set(title=label,xlabel='Randomization block',ylim=(0,1.08))
        ax.set_xticks(sorted(firstOrder))
        ax.grid(alpha=.2)
        groups={}
        for position in ('first','second'):
            selected=[r for r in rows if r['cpuFraction']==.1 and
                      (firstOrder[r['block']][0]==.1)==(position=='first')]
            groups[position]=dict(count=len(selected),unsafe=sum(r['q']<.8 for r in selected),
                                 medianQ=statistics.median(r['q'] for r in selected) if selected else None)
        report[label]=dict(rows=len(rows),unsafe=sum(r['q']<.8 for r in rows),
                          minQ=min(r['q'] for r in rows),cpuPointOneByOrder=groups)
    axes[0].set_ylabel('Normalized canary Q (higher is better)')
    axes[1].legend(loc='lower right',fontsize=8)
    fig.suptitle('Diagnostic comparison: same design, sequential batches; no causal claim',fontsize=10)
    fig.tight_layout();fig.savefig(output/'warmup_order.png',dpi=180);fig.savefig(output/'warmup_order.pdf');plt.close(fig)
    report['note']='Descriptive order association, not randomized between-protocol causal evidence. Not a final holdout.'
    (output/'summary.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))


def executeCli():
    """Require completed control/warmup raw paths and an output directory."""
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ['control','warm','output']:parser.add_argument('--'+key,type=Path,required=True)
    args=parser.parse_args();analyzeWarmup(args.control,args.warm,args.output)


if __name__=='__main__':executeCli()
