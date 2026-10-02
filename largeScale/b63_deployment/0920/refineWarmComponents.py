"""Quality-oriented component MILP with checked incumbent start using highspy.
Only capacity/topology and an incumbent mapping are consumed; no deployment changes.
"""
import random,time
from collections import defaultdict
import numpy as np
from scipy.sparse import coo_matrix
import highspy
from topologyAllocation import audit,common_share


def refineWarmComponents(data,mapping,release=.1,seed=1,seconds=60,logFile=None):
    """Optimize intact retained-LAN components; release=1 is the full original placement MILP."""
    if not 0<=release<=1 or seconds<=0:raise ValueError('invalid search parameters')
    started=time.monotonic();initial=audit(data,mapping);caps=common_share(data)['countCaps'];hosts=[h['id'] for h in data['hosts']];vertices=data['vertices'];k=len(hosts)
    if any(initial['counts'][h]>cap for h,cap in zip(hosts,caps)):raise ValueError('infeasible incumbent')
    index={v:i for i,v in enumerate(vertices)};parent=list(range(len(vertices)));rng=random.Random(seed);retained=[]
    def findRoot(v):
        """Find vertex-index v's representative in the local retained-LAN forest."""
        while parent[v]!=v:parent[v]=parent[parent[v]];v=parent[v]
        return v
    for e,edge in enumerate(data['networks']):
        if len({mapping[v] for v in edge['members']})==1 and rng.random()>=release:
            retained.append(e)
            for v in edge['members'][1:]:parent[findRoot(index[v])]=findRoot(index[edge['members'][0]])
    grouped=defaultdict(list)
    for v in vertices:grouped[findRoot(index[v])].append(v)
    groups=list(grouped.values());owner={v:i for i,g in enumerate(groups) for v in g};active=[]
    for e,edge in enumerate(data['networks']):
        incident=sorted({owner[v] for v in edge['members']})
        if len(incident)>1:active.append((e,incident))
    t=len(groups);size=(t+len(active))*k;cost=np.zeros(size);cost[t*k:]=1;rr=[];cc=[];vv=[];lo=[];hi=[]
    def addRow(terms,low=-np.inf,high=np.inf):
        """Append sparse column/coefficient terms and bounds to the local constraint matrix."""
        for col,value in terms.items():rr.append(len(lo));cc.append(col);vv.append(value)
        lo.append(low);hi.append(high)
    for i in range(t):addRow({i*k+h:1 for h in range(k)},1,1)
    for h in range(k):addRow({i*k+h:len(g) for i,g in enumerate(groups)},high=caps[h])
    for j,(_,incident) in enumerate(active):
        for h in range(k):
            y=(t+j)*k+h
            for i in incident:addRow({i*k+h:1,y:-1},high=0)
            addRow({y:1,**{i*k+h:-1 for i in incident}},high=0)
    addRow({i:1 for i in range(t*k,size)},high=initial['connectivity']+len(active));matrix=coo_matrix((vv,(rr,cc)),shape=(len(lo),size)).tocsr();lo=np.array(lo);hi=np.array(hi)
    def checkVector(x):
        """Check a full candidate x against original binary bounds and all sparse rows."""
        if len(x)!=size or not np.all(np.isfinite(x)):return dict(valid=False)
        activity=matrix@x;check=dict(boundViolation=max(0.,float(max(-x)),float(max(x-1))),rowViolation=max(0.,float(max(lo-activity)),float(max(activity-hi))),integralityViolation=float(max(abs(x[:t*k]-np.round(x[:t*k])))));check['valid']=max(check.values())<=1e-6;return check
    start=np.zeros(size)
    for i,g in enumerate(groups):assert len({mapping[v] for v in g})==1;start[i*k+hosts.index(mapping[g[0]])]=1
    for j,(e,_) in enumerate(active):
        for h in {mapping[v] for v in data['networks'][e]['members']}:start[(t+j)*k+hosts.index(h)]=1
    startCheck=checkVector(start);assert startCheck['valid'] and int(cost@start)-len(active)==initial['connectivity'];h=highspy.Highs();h.setOptionValue('output_flag',bool(logFile));h.setOptionValue('log_to_console',False)
    if logFile:h.setOptionValue('log_file',str(logFile))
    h.setOptionValue('time_limit',float(seconds));h.setOptionValue('mip_rel_gap',0.);h.setOptionValue('threads',1);h.setOptionValue('random_seed',seed)
    def requireOk(status):
        """Reject backend model/start API errors instead of silently running another model."""
        if status!=highspy.HighsStatus.kOk:raise RuntimeError(str(status))
    requireOk(h.addCols(size,cost,np.zeros(size),np.ones(size),0,np.zeros(size+1,dtype=np.int32),np.array([],dtype=np.int32),np.array([],dtype=float)))
    requireOk(h.addRows(len(lo),lo,hi,len(matrix.data),matrix.indptr.astype(np.int32),matrix.indices.astype(np.int32),matrix.data))
    requireOk(h.changeColsIntegrality(t*k,np.arange(t*k,dtype=np.int32),np.ones(t*k,dtype=np.uint8)))
    solution=highspy.HighsSolution();solution.col_value=start;solution.value_valid=True;requireOk(h.setSolution(solution));h.run();sol=h.getSolution();info=h.getInfo();x=np.asarray(sol.col_value);check=checkVector(x) if sol.value_valid else dict(valid=False);candidate=dict(mapping)
    if check['valid']:
        proposed={v:hosts[int(np.argmax(x[i*k:(i+1)*k]))] for i,g in enumerate(groups) for v in g};measured=audit(data,proposed);assert all(measured['counts'][host]<=cap for host,cap in zip(hosts,caps));assert abs(measured['connectivity']+len(active)-cost@x)<1e-6
        if measured['connectivity']<initial['connectivity']:candidate=proposed
    measured=audit(data,candidate)
    return dict(mapping=candidate,connectivity=measured['connectivity'],initialCost=initial['connectivity'],accepted=measured['connectivity']<initial['connectivity'],groups=groups,retained=retained,activeLans=[e for e,_ in active],release=release,startCheck=startCheck,primalCheck=check,status=h.modelStatusToString(h.getModelStatus()),solverReportedBound=float(info.mip_dual_bound)-len(active),seconds=time.monotonic()-started,solverVersion=h.version(),scope='Checked feasible primal; solver bound/status not independently certified; release=1 removes all component restrictions')
