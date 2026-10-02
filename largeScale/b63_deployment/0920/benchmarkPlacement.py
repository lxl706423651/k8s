#!/usr/bin/env python3
# Offline synthetic scalability and ablation experiments with explicit evidence labels.
# Outputs incremental JSONL; no fitted coefficients and no live workloads are used.
"""Benchmark heuristic objective/constraints, operators and eta tradeoffs."""
import argparse
import json
from pathlib import Path
import random
import time

from solvePlacement import PlacementModel


def createBenchmark(size,seed,eta):
    """Construct `size` Pods in 12-member communication communities using `seed`."""
    rng=random.Random(seed)
    count=max(4,(size+11)//12)
    pods=[dict(id=f'p{i:04d}',cpu=rng.uniform(.08,.16),memory=.03,community=str(i//12)) for i in range(size)]
    groups=[[p['id'] for p in pods[i:i+12]] for i in range(0,size,12)]
    edges=[dict(id=f'community{i}',members=g,weight=10.) for i,g in enumerate(groups)]
    edges += [dict(id=f'cross{i}',members=rng.sample([p['id'] for p in pods],2),weight=.2) for i in range(size)]
    return dict(evidenceKind='synthetic_algorithm_benchmark_not_platform_evidence',eta=eta,pods=pods,
                nodes=[dict(id=f'n{i}',cpu=4.,memory=2.,slots=50,profile='synthetic') for i in range(count)],
                networks=edges,costs=[[0 if i==j else 1 for j in range(count)] for i in range(count)],
                surfaces={'synthetic':dict(cpu=[0,.5,1],memory=[0,.5,1],q=[[1,.98,.96],[.9,.88,.86],[.6,.58,.56]],margin=.02)})


def executeBenchmarks(sizes,seeds,output):
    """Run frozen ablations for `sizes`/`seeds`, preserving each finished result."""
    settings=[('full',('move','swap','group'),3,True,.8),
              ('move_only',('move',),3,True,.8),
              ('no_group',('move','swap'),3,True,.8),
              ('one_start',('move','swap','group'),1,True,.8),
              ('no_quality',('move','swap','group'),3,False,.8),
              ('eta07',('move','swap','group'),3,True,.7),
              ('eta09',('move','swap','group'),3,True,.9)]
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('x') as handle:
        for size in sizes:
            for seed in seeds:
                for label,operators,starts,enforce,eta in settings:
                    instance=createBenchmark(size,seed,eta)
                    model=PlacementModel(instance)
                    begin=time.monotonic()
                    result=model.solvePlacement(starts=starts,seed=seed,operators=operators,enforceQuality=enforce)
                    audit=model.auditMapping(result['mapping']) if 'mapping' in result else None
                    initial=model.buildInitial('resource',seed)
                    row=dict(evidenceKind=instance['evidenceKind'],size=size,seed=seed,method=label,eta=eta,
                             seconds=time.monotonic()-begin,status=result['status'],cost=result.get('cost'),
                             resourceInitialCost=model.calculateCost(initial) if initial else None,
                             qViolationNodes=sum(m['qLower'] is None or m['qLower']<eta-1e-9 for m in audit['nodes'].values()) if audit else None,
                             minimumQ=min(m['qLower'] for m in audit['nodes'].values()) if audit else None,
                             failedStarts=sum(a['status']=='construction_failed' for a in result['attempts']),
                             iterations=[len(a.get('trace',[]))-1 for a in result['attempts'] if 'trace' in a],
                             mapping=result.get('mapping'))
                    handle.write(json.dumps(row)+'\n');handle.flush()
                    print(json.dumps({k:v for k,v in row.items() if k!='mapping'}),flush=True)


def executeCli():
    """Require fresh output and explicit bounded instance sizes."""
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sizes',nargs='+',type=int,default=[24,48])
    parser.add_argument('--seeds',nargs='+',type=int,default=[11,29,47])
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    executeBenchmarks(args.sizes,args.seeds,args.output)


if __name__=='__main__':
    executeCli()
