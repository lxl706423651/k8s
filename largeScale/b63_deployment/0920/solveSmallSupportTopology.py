"""Solve low-incidence-class topologies exactly by finite support enumeration.
Input stays capacity/topology only. Explicit state limit rejects large T/K;
never substitutes incomplete enumeration for an optimality certificate.
"""
from itertools import product
from analyzeCapacitySlack import analyzeCapacitySlack
from certifyTopologyStructure import buildIncidenceClasses,checkSupportHall
from allocateTopologySupports import allocateSupportedClasses


def solveSmallSupports(data,stateLimit=100000):
    """Enumerate all class host supports within stateLimit, then reconstruct optimum."""
    groups=buildIncidenceClasses(data);k=len(data['hosts']);weights=[len(g['members']) for g in groups]
    if stateLimit<1:raise ValueError('positive state limit required')
    # Even one class has at least K possible singleton supports.
    if k>stateLimit or (1<<k)-1>stateLimit:raise ValueError('host support space exceeds limit')
    options=[];states=1
    for w in weights:
        choices=[a for a in range(1,1<<k) if a.bit_count()<=w];states*=len(choices)
        if states>stateLimit:raise ValueError('class support space exceeds limit')
        options.append(choices)
    capacity=analyzeCapacitySlack(data);caps=capacity['capacity']['countCaps'];best=None;feasible=0;checked=0
    for masks in product(*options):
        checked+=1
        if not checkSupportHall(weights,masks,caps):continue
        feasible+=1;unions=[0]*len(data['networks'])
        for group,mask in zip(groups,masks):
            for e in group['networks']:unions[e]|=mask
        cost=sum(a.bit_count()-1 for a in unions)
        if best is None or cost<best[0]:best=(cost,list(masks))
    assert checked==states and best is not None
    result=allocateSupportedClasses(data,caps,best[1]);assert result['cost']==best[0]
    return dict(**result,optimal=True,proof='Complete support enumeration plus exact Hall feasibility and integer flow reconstruction',states=states,checked=checked,feasibleSupports=feasible,classes=len(groups),supports=best[1],capacity=capacity)
