"""Refine an incumbent by moving whole components of retained currently internal LANs.
Randomly release some internal LANs to enlarge the neighborhood; original budgets persist.
"""
import random,time
from collections import defaultdict
import numpy as np
from scipy.optimize import Bounds,LinearConstraint,milp
from scipy.sparse import coo_matrix
from topologyAllocation import audit,common_share


def refinePartitionComponents(data,mapping,release=.1,seed=1,seconds=1.):
    """Keep each retained-internal-LAN component intact and optimize component host choices."""
    if not 0<=release<=1 or seconds<=0:raise ValueError('invalid search parameters')
    start=time.monotonic();initial=audit(data,mapping);caps=common_share(data)['countCaps'];hosts=[h['id'] for h in data['hosts']];vertices=data['vertices'];n=len(vertices);index={v:i for i,v in enumerate(vertices)};parents=list(range(n));rng=random.Random(seed);retained=[]
    def findRoot(v):
        while parents[v]!=v:parents[v]=parents[parents[v]];v=parents[v]
        return v
    for e,edge in enumerate(data['networks']):
        members=edge['members']
        if len({mapping[v] for v in members})==1 and rng.random()>=release:
            retained.append(e)
            for v in members[1:]:parents[findRoot(index[v])]=findRoot(index[members[0]])
    groups=defaultdict(list)
    for v in vertices:groups[findRoot(index[v])].append(v)
    groups=list(groups.values());owner={v:i for i,g in enumerate(groups) for v in g};k=len(hosts);t=len(groups);networks=[sorted({owner[v] for v in e['members']}) for e in data['networks']];size=(t+len(networks))*k;cost=np.zeros(size);cost[t*k:]=1;rr=[];cc=[];vv=[];lo=[];hi=[]
    def addRow(terms,low=-np.inf,high=np.inf):
        for j,v in terms.items():rr.append(len(lo));cc.append(j);vv.append(v)
        lo.append(low);hi.append(high)
    for i in range(t):addRow({i*k+h:1 for h in range(k)},1,1)
    for h in range(k):addRow({i*k+h:len(g) for i,g in enumerate(groups)},high=caps[h])
    for e,incident in enumerate(networks):
        for h in range(k):
            y=(t+e)*k+h
            for i in incident:addRow({i*k+h:1,y:-1},high=0)
            addRow({y:1,**{i*k+h:-1 for i in incident}},high=0)
    addRow({i:1 for i in range(t*k,size)},high=initial['connectivity']+len(networks));matrix=coo_matrix((vv,(rr,cc)),shape=(len(lo),size)).tocsc();integrality=np.zeros(size);integrality[:t*k]=1;result=milp(cost,integrality=integrality,bounds=Bounds(np.zeros(size),np.ones(size)),constraints=LinearConstraint(matrix,lo,hi),options=dict(time_limit=seconds,mip_rel_gap=0.));check=dict(present=result.x is not None,valid=False);candidate=dict(mapping)
    if result.x is not None and np.all(np.isfinite(result.x)):
        x=result.x;activity=matrix@x;check.update(boundViolation=max(0.,float(max(-x)),float(max(x-1))),rowViolation=max(0.,float(max(np.asarray(lo)-activity)),float(max(activity-np.asarray(hi)))),integralityViolation=float(max(abs(x[:t*k]-np.round(x[:t*k])))));check['valid']=result.status in [0,1] and max(check[v] for v in ['boundViolation','rowViolation','integralityViolation'])<=1e-6
        if check['valid']:
            proposed={v:hosts[int(np.argmax(x[i*k:(i+1)*k]))] for i,g in enumerate(groups) for v in g};measured=audit(data,proposed);assert all(measured['counts'][h]<=c for h,c in zip(hosts,caps));assert abs(measured['connectivity']+len(networks)-result.fun)<1e-6
            if measured['connectivity']<initial['connectivity']:candidate=proposed
    measured=audit(data,candidate)
    return dict(mapping=candidate,connectivity=measured['connectivity'],initialCost=initial['connectivity'],accepted=measured['connectivity']<initial['connectivity'],groups=groups,retained=retained,release=release,status=int(result.status),primalCheck=check,seconds=time.monotonic()-start,scope='Restricted component neighborhood; no original global optimality claim')
