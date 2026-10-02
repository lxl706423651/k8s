"""Reconstruct exact node allocations from incidence-class host supports.
Only topology and count capacities are used. Classes remain splittable.
This is an exact feasibility subproblem, not a new partition heuristic.
"""
from collections import Counter
import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import maximum_flow
from certifyTopologyStructure import buildIncidenceClasses,checkSupportHall,verifyFlowWitness


def allocateSupportedClasses(data, caps, masks):
    """Allocate data's ordered incidence classes to exactly masks within caps.
    Raises ValueError for impossible supports; checks integer flow and final cost.
    """
    groups=buildIncidenceClasses(data);weights=[len(g['members']) for g in groups]
    k=len(caps);t=len(groups)
    if k!=len(data['hosts']) or not checkSupportHall(weights,masks,caps):
        raise ValueError('infeasible class supports')
    base=[sum(bool(a & (1<<h)) for a in masks) for h in range(k)]
    demands=[w-a.bit_count() for w,a in zip(weights,masks)]
    source=t+k;sink=source+1;rows=[];cols=[];values=[]
    for i,(d,a) in enumerate(zip(demands,masks)):
        rows.append(source);cols.append(i);values.append(d)
        for h in range(k):
            if a & (1<<h):rows.append(i);cols.append(t+h);values.append(d)
    for h in range(k):
        rows.append(t+h);cols.append(sink);values.append(caps[h]-base[h])
    matrix=csr_matrix((np.array(values,dtype=np.int64),(rows,cols)),shape=(sink+1,sink+1))
    result=maximum_flow(matrix,source,sink);coo=result.flow.tocoo()
    arcs=[[int(u),int(v),int(f)] for u,v,f in zip(coo.row,coo.col,coo.data) if f>0]
    verifyFlowWitness(matrix,source,sink,int(result.flow_value),arcs)
    if result.flow_value!=sum(demands):raise AssertionError('Hall/flow disagreement')
    mapping={}
    for i,(group,a) in enumerate(zip(groups,masks)):
        offset=0
        for h in range(k):
            if a & (1<<h):
                amount=1+int(result.flow[i,t+h])
                for v in group['members'][offset:offset+amount]:mapping[v]=data['hosts'][h]['id']
                offset+=amount
        assert offset==weights[i]
    counts=Counter(mapping.values())
    assert set(mapping)==set(data['vertices']) and all(counts[h['id']]<=cap for h,cap in zip(data['hosts'],caps))
    actual=[]
    for group in groups:
        used={mapping[v] for v in group['members']}
        actual.append(sum(1<<h for h,host in enumerate(data['hosts']) if host['id'] in used))
    assert actual==masks
    cost=sum(len({mapping[v] for v in e['members']})-1 for e in data['networks'])
    return dict(mapping=mapping,cost=cost,flowValue=int(result.flow_value),flowArcs=arcs)
