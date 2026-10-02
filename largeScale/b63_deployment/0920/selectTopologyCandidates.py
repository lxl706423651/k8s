"""Search several capacity envelopes and audit candidates against the true target.
Inputs remain host capacities/topology; levels are public solver search settings.
Returns a max-entitlement mapping and observed nondominated points, not a proof.
"""
import time
from topologyAllocation import common_share,audit
from topologyAllocationKaHyPar import solve


def selectCandidates(data,configuration,seed=11,levels=(1.,.99,.975,.95,.9,.8)):
    """Search data using configuration/seed at levels; retain only audited mappings."""
    start=time.monotonic();target=common_share(data)['share'];candidates=[]
    for level in levels:
        result=solve(data,configuration,level,seed)
        if not result['valid']:continue
        measured=audit(data,result['mapping']);result.update(measured);candidates.append(result)
    eligible=[r for r in candidates if r['share']+1e-9>=target]
    if not eligible:raise RuntimeError('no maximum-entitlement candidate returned')
    best=min(eligible,key=lambda r:(r['connectivity'],-r['share']))
    frontier=[r for r in candidates if not any(q['share']>=r['share']-1e-10 and q['connectivity']<=r['connectivity'] and (q['share']>r['share']+1e-10 or q['connectivity']<r['connectivity']) for q in candidates)]
    return dict(best=best,candidates=candidates,frontier=[dict(share=r['share'],cost=r['connectivity'],searchLevel=r['retention']) for r in frontier],seconds=time.monotonic()-start,target=target)
