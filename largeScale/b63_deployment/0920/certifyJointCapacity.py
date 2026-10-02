"""Joint capacity lower bound for overlapping mathematical terminal demands.
Only topology-derived terminal sets, integer proof weights and original count caps
are consumed. A compressed min-cost transport is verified by integer potentials.
"""
from collections import defaultdict
import networkx as nx


def buildJointTransport(vertices,terminalSets,weights,caps):
    """Build demand-signature classes and per-host unavoidable incident demand."""
    if len(set(vertices))!=len(vertices) or len(terminalSets)!=len(weights) or sum(caps)<len(vertices) or any(type(c)is not int or c<0 for c in caps):raise ValueError('invalid vertices/caps')
    known=set(vertices);membership={v:[] for v in vertices};families=[];positive=[]
    for members,w in zip(terminalSets,weights):
        if type(w)is not int or w<0 or len(set(members))!=len(members) or not set(members)<=known:raise ValueError('invalid proof family')
        if w==0:continue
        i=len(families);families.append(list(members));positive.append(w)
        for v in members:membership[v].append(i)
    groups=defaultdict(list)
    for v in vertices:groups[tuple(membership[v])].append(v)
    signatures=list(groups);counts=[len(groups[s]) for s in signatures];costs=[]
    for t,signature in enumerate(signatures):
        neighbors=[];degree=0;signature=set(signature)
        for u,other in enumerate(signatures):
            amount=counts[u]-int(t==u);weight=sum(positive[i] for i in signature.intersection(other));neighbors.append((weight,amount));degree+=weight*amount
        neighbors.sort(reverse=True);row=[]
        for cap in caps:
            left=max(0,cap-1);internal=0
            for weight,amount in neighbors:
                taken=min(left,amount);internal+=taken*weight;left-=taken
            row.append(degree-internal)
        costs.append(row)
    return dict(classes=[dict(signature=list(s),members=groups[s]) for s in signatures],counts=counts,costs=costs,caps=list(caps),families=families,weights=positive)


def buildResidualArcs(model,flows):
    """Return all positive-capacity residual arcs of a proposed transport flow."""
    counts=model['counts'];caps=model['caps'];costs=model['costs'];t=len(counts);k=len(caps);sink=t+k;arcs=[]
    if len(flows)!=t or any(len(row)!=k for row in flows):raise ValueError('invalid flow shape')
    for i,row in enumerate(flows):
        if any(type(x)is not int or x<0 for x in row) or sum(row)!=counts[i]:raise ValueError('class conservation violated')
        for h,amount in enumerate(row):
            capacity=min(counts[i],caps[h]);cost=costs[i][h]
            if amount>capacity:raise ValueError('class/host arc capacity violated')
            if amount<capacity:arcs.append((i,t+h,cost))
            if amount>0:arcs.append((t+h,i,-cost))
    for h,cap in enumerate(caps):
        amount=sum(row[h] for row in flows)
        if amount>cap:raise ValueError('host capacity violated')
        if amount<cap:arcs.append((t+h,sink,0))
        if amount>0:arcs.append((sink,t+h,0))
    return arcs


def verifyJointTransport(model,certificate):
    """Check integral primal and nonnegative reduced costs on every residual arc."""
    flows=certificate['flows'];potentials=certificate['potentials'];arcs=buildResidualArcs(model,flows);size=len(model['counts'])+len(model['caps'])+1
    if len(potentials)!=size or any(type(x)is not int for x in potentials):raise ValueError('invalid dual potentials')
    if any(cost+potentials[u]-potentials[v]<0 for u,v,cost in arcs):raise ValueError('negative reduced residual cost')
    cost=sum(x*c for row,costRow in zip(flows,model['costs']) for x,c in zip(row,costRow))
    if certificate['transportCost']!=cost or certificate['jointDemandLower']!=(cost+1)//2:raise ValueError('incorrect joint bound')
    return True


def certifyJointDemand(vertices,terminalSets,weights,caps):
    """Compute and independently verify a joint lower bound on cut proof demand."""
    model=buildJointTransport(vertices,terminalSets,weights,caps);t=len(model['counts']);k=len(caps);sink=t+k;graph=nx.DiGraph()
    for i,count in enumerate(model['counts']):graph.add_node(i,demand=-count)
    for h,cap in enumerate(caps):graph.add_node(t+h,demand=0);graph.add_edge(t+h,sink,capacity=cap,weight=0)
    graph.add_node(sink,demand=len(vertices))
    for i,count in enumerate(model['counts']):
        for h,cap in enumerate(caps):graph.add_edge(i,t+h,capacity=min(count,cap),weight=model['costs'][i][h])
    objective,flow=nx.network_simplex(graph);flows=[[int(flow[i][t+h]) for h in range(k)] for i in range(t)];arcs=buildResidualArcs(model,flows);potentials=[0]*(sink+1)
    for _ in range(sink):
        changed=False
        for u,v,cost in arcs:
            if potentials[v]>potentials[u]+cost:potentials[v]=potentials[u]+cost;changed=True
        if not changed:break
    result=dict(flows=flows,potentials=potentials,transportCost=int(objective),jointDemandLower=(int(objective)+1)//2,classes=t,scope='Exact min-cost transport certificate; lower bound on demand cut, not exact partition optimum')
    verifyJointTransport(model,result)
    return result
