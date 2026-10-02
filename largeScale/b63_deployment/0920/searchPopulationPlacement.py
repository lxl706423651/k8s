"""Topology/capacity-only population placement with native starts, component repair and recombination.
Quality-oriented fixed work schedule; all raw parents and intermediate mappings returned for audit.
"""
import time
from topologyAllocationKaHyPar import solve
from refineTargetedComponents import refineTargetedComponents
from recombinePlacement import recombinePlacement


def searchPopulationPlacement(data,configuration,seedStart,starts=200,parentCount=10,passes=2,recombineSeconds=30,repairSeconds=10,solverSeed=290937,progress=None):
    """Generate feasible native parents, repair top unique maps, then recombine without historical inputs."""
    if starts<1 or parentCount<1 or passes<1:raise ValueError('positive work schedule required')
    began=time.monotonic();native=[];pool=[];seen=set()
    for seed in range(seedStart,seedStart+starts):
        result=solve(data,configuration,1.,seed);native.append(result);key=tuple(result['mapping'][v] for v in data['vertices'])
        if result['valid'] and key not in seen:seen.add(key);pool.append(result)
        if progress and (seed-seedStart+1)%20==0:progress(dict(phase='native',completed=seed-seedStart+1,best=min(r['connectivity'] for r in pool),seconds=time.monotonic()-began))
    if not pool:raise RuntimeError('no feasible native parent')
    repaired=[]
    for parent in sorted(pool,key=lambda r:(r['connectivity'],r['seed']))[:parentCount]:
        result=refineTargetedComponents(data,parent['mapping'],[],seed=parent['seed'],seconds=repairSeconds);repaired.append(dict(seed=parent['seed'],result=result))
        if progress:progress(dict(phase='repair',seed=parent['seed'],initial=parent['connectivity'],cost=result['connectivity'],seconds=time.monotonic()-began))
    donors=sorted(repaired,key=lambda r:(r['result']['connectivity'],r['seed']));mapping=dict(donors[0]['result']['mapping']);calls=[]
    for cycle in range(passes):
        for i,donor in enumerate(donors):
            result=recombinePlacement(data,mapping,donor['result']['mapping'],seed=solverSeed,seconds=recombineSeconds);polished=refineTargetedComponents(data,result['mapping'],[],seed=solverSeed,seconds=repairSeconds);calls.append(dict(passIndex=cycle,donorIndex=i,donorSeed=donor['seed'],recombination=result,repair=polished));mapping=dict(polished['mapping'])
            if progress:progress(dict(phase='recombine',passIndex=cycle,donorIndex=i,initial=result['initialCost'],recombined=result['connectivity'],cost=polished['connectivity'],status=result['status'],seconds=time.monotonic()-began))
    return dict(native=native,repaired=repaired,calls=calls,mapping=mapping,cost=calls[-1]['repair']['connectivity'],bestNative=min(r['connectivity'] for r in pool),bestRepaired=min(r['result']['connectivity'] for r in repaired),seconds=time.monotonic()-began,scope='Fresh input only; native baseline shares initial200 starts; additional optimization work is explicit, not an equal-time claim')
