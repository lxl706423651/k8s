#!/usr/bin/env python3
# Reduce synthetic algorithm benchmark JSONL into a table and labeled static figures.
# These outputs validate algorithm behavior; they are never evidence of measured VM Q.
"""Summarize ablation cost, feasibility and threshold tradeoffs without hiding failures."""
import argparse
import csv
import json
from pathlib import Path
import statistics

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def analyzeBenchmarks(path,output):
    """Read benchmark JSONL `path`, summarize all outcomes into `output`."""
    rows=[json.loads(line) for line in path.read_text().splitlines()]
    output.mkdir(parents=True,exist_ok=True)
    summary=[]
    for size in sorted({r['size'] for r in rows}):
        for method in dict.fromkeys(r['method'] for r in rows if r['size']==size):
            items=[r for r in rows if r['size']==size and r['method']==method]
            feasible=[r for r in items if r['status']=='feasible']
            summary.append(dict(size=size,method=method,runs=len(items),feasible=len(feasible),
                medianCost=statistics.median(r['cost'] for r in feasible) if feasible else None,
                medianTotalEvaluationSeconds=statistics.median(r['seconds'] for r in items),
                qViolationNodes=sum(r['qViolationNodes'] or 0 for r in feasible),
                failedStarts=sum(r['failedStarts'] for r in items)))
    with (output/'summary.csv').open('w') as handle:
        writer=csv.DictWriter(handle,fieldnames=list(summary[0]));writer.writeheader();writer.writerows(summary)
    size=max(r['size'] for r in rows)
    methods=['full','move_only','no_group','one_start','no_quality']
    fig,axes=plt.subplots(1,2,figsize=(11,4))
    for i,method in enumerate(methods):
        vals=[r['cost'] for r in rows if r['size']==size and r['method']==method and r['status']=='feasible']
        axes[0].scatter([i]*len(vals),vals,label=method,s=20)
        if vals:axes[0].plot([i-.2,i+.2],[statistics.median(vals)]*2,color='black')
    axes[0].set_xticks(range(len(methods)),methods,rotation=20)
    axes[0].set(ylabel='Synthetic weighted cut cost',title=f'{size} Pods: all seeds and median')
    for seed in sorted({r['seed'] for r in rows}):
        points=sorted([r for r in rows if r['size']==size and r['seed']==seed and r['method'] in ('eta07','full','eta09') and r['status']=='feasible'],key=lambda r:r['eta'])
        axes[1].plot([r['eta'] for r in points],[r['cost'] for r in points],'o-',label=f'seed {seed}')
    axes[1].set(xlabel='Required model Q threshold',ylabel='Synthetic weighted cut cost',title='Heuristic threshold sweep (no physical measurements)')
    axes[1].legend()
    fig.suptitle('SYNTHETIC ALGORITHM VALIDATION — not measured VM performance',fontsize=10)
    fig.tight_layout();fig.savefig(output/'ablations.png',dpi=160);fig.savefig(output/'ablations.pdf');plt.close(fig)
    (output/'summary.json').write_text(json.dumps(dict(evidenceKind='synthetic_algorithm_benchmark_not_platform_evidence',rows=summary),indent=2)+'\n')
    print(json.dumps(summary,indent=2))


def executeCli():
    """Parse raw and output paths for offline benchmark reduction."""
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('raw',type=Path);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();analyzeBenchmarks(args.raw,args.output)


if __name__=='__main__':
    executeCli()
