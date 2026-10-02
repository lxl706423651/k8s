"""Optimize splittable incidence classes in a fixed-outside local MILP.
Uses only capacity/topology data and an incumbent. All solver primals checked;
accepted mappings are re-audited. No VM effects and no global optimality claim.
"""
from collections import Counter
import time
import numpy as np
from scipy.optimize import Bounds,LinearConstraint,milp
from scipy.sparse import coo_matrix
from topologyAllocation import audit,common_share
from certifyTopologyStructure import buildIncidenceClasses


def refineClasses(data,mapping,selected,seconds=.2):
    """Optimize selected class indices with integer splittable counts per host."""
    started=time.monotonic();groups=buildIncidenceClasses(data);selected=sorted(set(selected))
    if seconds<=0 or not selected or any(i<0 or i>=len(groups) for i in selected):raise ValueError('invalid window')
    hosts=[h['id'] for h in data['hosts']];k=len(hosts);caps=common_share(data)['countCaps'];initial=audit(data,mapping)
    assert all(initial['counts'][h]<=c for h,c in zip(hosts,caps))
    chosen=[groups[i] for i in selected];inside={v for g in chosen for v in g['members']};outside=Counter(h for v,h in mapping.items() if v not in inside)
    touched=sorted({e for g in chosen for e in g['networks']});weights=[len(g['members']) for g in chosen];t=len(chosen);size=(t+len(touched))*k
    objective=np.zeros(size);objective[t*k:]=1;lower=np.zeros(size);upper=np.ones(size)
    for i,w in enumerate(weights):upper[i*k:(i+1)*k]=w
    rr=[];cc=[];vv=[];lo=[];hi=[]
    def addRow(terms,low=-np.inf,high=np.inf):
        """Append coefficient dictionary terms and finite/infinite row bounds."""
        for col,val in terms.items():rr.append(len(lo));cc.append(col);vv.append(val)
        lo.append(low);hi.append(high)
    for i,w in enumerate(weights):addRow({i*k+h:1 for h in range(k)},w,w)
    for h,host in enumerate(hosts):addRow({i*k+h:1 for i in range(t)},high=caps[h]-outside[host])
    currentSpan=0
    for j,e in enumerate(touched):
        members=data['networks'][e]['members'];fixed={mapping[v] for v in members if v not in inside};incident=[i for i,g in enumerate(chosen) if e in g['networks']]
        currentSpan+=len({mapping[v] for v in members})
        for h,host in enumerate(hosts):
            y=(t+j)*k+h
            if host in fixed:lower[y]=1;continue
            for i in incident:addRow({i*k+h:1,y:-weights[i]},high=0)
            addRow({y:1,**{i*k+h:-1 for i in incident}},high=0)
    addRow({i:1 for i in range(t*k,size)},high=currentSpan)
    matrix=coo_matrix((vv,(rr,cc)),shape=(len(lo),size)).tocsc()
    result=milp(objective,integrality=np.ones(size),bounds=Bounds(lower,upper),constraints=LinearConstraint(matrix,lo,hi),options=dict(time_limit=seconds,mip_rel_gap=0.))
    check=dict(present=result.x is not None,valid=False)
    if result.x is not None and np.all(np.isfinite(result.x)):
        activity=matrix@result.x
        check.update(boundViolation=max(0.,float(np.max(lower-result.x)),float(np.max(result.x-upper))),rowViolation=max(0.,float(np.max(np.asarray(lo)-activity)),float(np.max(activity-np.asarray(hi)))),integralityViolation=float(np.max(abs(result.x-np.round(result.x)))))
        check['valid']=result.status in [0,1] and all(check[x]<=1e-6 for x in ['boundViolation','rowViolation','integralityViolation'])
    candidate=dict(mapping);accepted=False
    if check['valid']:
        proposed=dict(mapping)
        for i,g in enumerate(chosen):
            offset=0
            for h,host in enumerate(hosts):
                amount=int(round(result.x[i*k+h]))
                for v in g['members'][offset:offset+amount]:proposed[v]=host
                offset+=amount
            assert offset==weights[i]
        measured=audit(data,proposed)
        assert all(measured['counts'][h]<=c for h,c in zip(hosts,caps))
        assert abs(measured['connectivity']-initial['connectivity']-result.fun+currentSpan)<1e-6
        if measured['connectivity']<initial['connectivity']:candidate=proposed;accepted=True
    return dict(mapping=candidate,**audit(data,candidate),selectedClasses=selected,windowVertices=len(inside),accepted=accepted,primalCheck=check,status=int(result.status),seconds=time.monotonic()-started,initialCost=initial['connectivity'])
