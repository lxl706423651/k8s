"""KaHyPar backend for the capacity-only model; no capacity relaxation.

KaHyPar is an existing third-party partitioning algorithm, not a new algorithm
contribution. The caller supplies a pinned official km1 INI configuration.
"""
import argparse
import json
import math
from pathlib import Path
import time
from topologyAllocation import common_share, audit


def solve(data,configuration,retention=1.,seed=11):
    import kahypar
    if not math.isfinite(retention) or not 0<retention<=1:raise ValueError('retention must be in (0,1]')
    envelope=common_share(data);z=envelope['share']*retention
    hosts=data['hosts'];ids=data['vertices'];index={v:i for i,v in enumerate(ids)}
    caps=[min(len(ids),math.floor(min(h[r]/(z*envelope['nominalBundle'][r]) for r in ['cpu','memory'])+1e-9)) for h in hosts]
    pins=[];offset=[0]
    for e in data['networks']:
        pins.extend(index[v] for v in e['members']);offset.append(len(pins))
    graph=kahypar.Hypergraph(len(ids),len(data['networks']),offset,pins,len(hosts))
    context=kahypar.Context();context.loadINIconfiguration(str(configuration))
    context.setK(len(hosts));context.setEpsilon(0.);context.setSeed(seed);context.suppressOutput(True)
    context.setCustomTargetBlockWeights(caps)
    start=time.monotonic();kahypar.partition(graph,context);elapsed=time.monotonic()-start
    blocks=[graph.blockID(i) for i in range(len(ids))]
    if any(b<0 or b>=len(hosts) for b in blocks):raise RuntimeError('backend returned incomplete assignment')
    mapping={v:hosts[b]['id'] for v,b in zip(ids,blocks)};measured=audit(data,mapping)
    valid=all(measured['counts'][h['id']]<=cap for h,cap in zip(hosts,caps))
    assert measured['connectivity']==kahypar.connectivityMinusOne(graph)
    return dict(engine='KaHyPar',seed=seed,retention=retention,capacityEnvelope=envelope,countCaps=caps,
                valid=valid,partitionSeconds=elapsed,mapping=mapping,**measured)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('instance',type=Path);p.add_argument('--configuration',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--retention',type=float,default=1.)
    p.add_argument('--seed',type=int,default=11);args=p.parse_args()
    result=solve(json.loads(args.instance.read_text()),args.configuration,args.retention,args.seed)
    with args.output.open('x') as f:json.dump(result,f,indent=2)
    print(json.dumps({k:v for k,v in result.items() if k!='mapping'}))
    if not result['valid']:raise SystemExit('partition violates original count capacities; result is diagnostic only')
