"""Budgeted capacity-feasible partition search using audited representations.
Inputs are capacity/topology, pinned KaHyPar configuration, seed and search budget.
No workload profiles or live mutations. Late completed calls cannot become outputs.
"""
from collections import defaultdict
from fractions import Fraction
import itertools,time
from topologyAllocation import common_share,audit
from topologyAllocationKaHyPar import solve


def partitionNormalized(data,configuration,seed):
    """Partition a per-LAN normalized clique; audit the original LAN objective.

    configuration is a pinned km1 INI; seed fixes the backend RNG. Integer edge
    weights use scale 100000. Conversion time is included by the outer search.
    """
    import kahypar
    env=common_share(data);n=len(data['vertices']);k=len(data['hosts']);indices={v:i for i,v in enumerate(data['vertices'])};pairs=defaultdict(Fraction)
    for edge in data['networks']:
        members=sorted(edge['members']);length=len(members)
        if length<2:continue
        weight=Fraction(2,length*(length-1))
        for pair in itertools.combinations(members,2):pairs[pair]+=weight
    pins=[];offset=[0];weights=[]
    for (a,b),weight in sorted(pairs.items()):
        rounded=round(100000*weight)
        if rounded<=0:raise ValueError('normalized graph precision insufficient for this LAN size')
        pins.extend([indices[a],indices[b]]);offset.append(len(pins));weights.append(rounded)
    if sum(weights)>=2**31:raise ValueError('normalized graph aggregate weight exceeds conservative int32 range')
    graph=kahypar.Hypergraph(n,len(weights),offset,pins,k,weights,[1]*n);ctx=kahypar.Context();ctx.loadINIconfiguration(str(configuration));ctx.setK(k);ctx.setEpsilon(0.);ctx.setSeed(seed);ctx.suppressOutput(True);ctx.setCustomTargetBlockWeights(env['countCaps']);kahypar.partition(graph,ctx)
    mapping={v:data['hosts'][graph.blockID(i)]['id'] for v,i in indices.items()};measured=audit(data,mapping);valid=all(measured['counts'][h['id']]<=cap for h,cap in zip(data['hosts'],env['countCaps']))
    return dict(mapping=mapping,valid=valid,**measured)


def searchAllocation(data,configuration,budgetSeconds=15,seed=2027,mode='native'):
    """Search data under budgetSeconds; mode native or one normalized candidate.

    Common capacity calculation is identical setup outside the search budget.
    Representation construction, backend calls and their validation are timed.
    An in-flight C++ call may finish late and is recorded but cannot be selected.
    Default stays native until evidence justifies a different policy.
    """
    if budgetSeconds<=0 or mode not in ['native','portfolio']:raise ValueError('invalid search settings')
    target=common_share(data)['share'];begin=time.monotonic();calls=[];best=None;i=0
    while time.monotonic()-begin<budgetSeconds:
        kind='normalized' if mode=='portfolio' and i==1 else 'native';callSeed=seed+i*1000
        result=partitionNormalized(data,configuration,callSeed) if kind=='normalized' else solve(data,configuration,1.,callSeed)
        elapsed=time.monotonic()-begin;eligible=result['valid'] and result['share']+1e-9>=target;onTime=elapsed<=budgetSeconds
        calls.append(dict(index=i,representation=kind,seed=callSeed,elapsed=elapsed,onTime=onTime,eligible=eligible,result=result))
        if onTime and eligible and (best is None or result['connectivity']<best['connectivity']):best=dict(mapping=result['mapping'],connectivity=result['connectivity'],share=result['share'])
        i+=1
    return dict(mode=mode,seed=seed,budgetSeconds=budgetSeconds,target=target,calls=calls,best=best,totalSeconds=time.monotonic()-begin,scope='Strict maximum-share search; late calls excluded; no global topology optimum certificate')
