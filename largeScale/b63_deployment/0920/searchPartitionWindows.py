"""Deadline-limited incumbent refinement with fixed or multi-scale exchange windows.
Consumes topology/capacities and a feasible mapping; no profiling or VM changes.
"""
import time
from topologyAllocation import audit,common_share
from refineAllocationWindowChecked import selectWindow,refineWindow


def searchPartitionWindows(data,mapping,budgetSeconds=15,seed=1,mode='fixed'):
    """Refine mapping within a wall-time selection deadline; retain late calls only as logs."""
    if budgetSeconds<=0 or mode not in ['fixed','multiscale']:raise ValueError('invalid policy')
    initial=audit(data,mapping);caps=common_share(data)['countCaps'];hosts=[h['id'] for h in data['hosts']]
    if any(initial['counts'][h]>cap for h,cap in zip(hosts,caps)):raise ValueError('infeasible starting mapping')
    best=dict(mapping);cost=initial['connectivity'];calls=[];start=time.monotonic();i=0
    while time.monotonic()-start<budgetSeconds:
        scale=0 if mode=='fixed' else i%3;limit=[48,96,192][scale];seconds=[.3,.6,1.2][scale];window=selectWindow(data,best,limit=limit,seed=seed+i*1000);remaining=budgetSeconds-(time.monotonic()-start)
        if not window or remaining<=0:break
        result=refineWindow(data,best,window,seconds=min(seconds,remaining));elapsed=time.monotonic()-start;onTime=elapsed<=budgetSeconds
        if onTime and result['connectivity']<cost:best=dict(result['mapping']);cost=result['connectivity']
        calls.append(dict(index=i,limit=limit,elapsed=elapsed,onTime=onTime,result=result));i+=1
    return dict(mode=mode,seed=seed,budgetSeconds=budgetSeconds,initialCost=initial['connectivity'],best=dict(mapping=best,**audit(data,best)),calls=calls,totalSeconds=time.monotonic()-start,scope='Shared feasible incumbent; capacity/model setup outside selection budget, window selection/model/solver/audit inside; late calls excluded')
