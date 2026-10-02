"""Capacity-aware topology lower bounds from all-pairs proof-path congestion.
Paths are mathematical witnesses, never workload inputs or traffic predictions.
Every parent tree is checked; integer loads yield a rational lower certificate.
"""
from collections import Counter
from fractions import Fraction
import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import breadth_first_order,connected_components,dijkstra


def buildRoutingGraph(data, lengths=None):
    """Build vertex/LAN incidence digraph; charge lengths upon entering each LAN."""
    n=len(data['vertices']);m=len(data['networks']);index={v:i for i,v in enumerate(data['vertices'])}
    rows=[];cols=[];weights=[]
    for e,network in enumerate(data['networks']):
        for v in network['members']:
            i=index[v];rows.extend([i,n+e]);cols.extend([n+e,i]);weights.extend([1. if lengths is None else float(lengths[e]),0.])
    return csr_matrix((weights,(rows,cols)),shape=(n+m,n+m))


def calculateSeparatedPairs(n,caps):
    """Minimize separated unordered pairs under caps using convex concentration."""
    if sum(caps)<n or min(caps)<0:raise ValueError('infeasible capacities')
    left=n;counts=[]
    for cap in sorted(caps,reverse=True):
        counts.append(min(cap,left));left-=counts[-1]
    return (n*n-sum(x*x for x in counts))//2


def auditRoutingParents(data,parents):
    """Verify all-root incidence trees and recompute integer unordered-pair LAN loads.
    Each source routes only to higher-index vertex targets, once per unordered pair.
    Disconnected components are allowed; paths never assert cross-component flows.
    """
    graph=buildRoutingGraph(data);n=len(data['vertices']);size=graph.shape[0];m=size-n
    _,labels=connected_components(graph,directed=False)
    if parents.shape!=(n,size) or not np.issubdtype(parents.dtype,np.integer):raise ValueError('invalid parent table')
    neighbors=[set(graph.indices[graph.indptr[v]:graph.indptr[v+1]].tolist()) for v in range(size)]
    loads=np.zeros(m,dtype=np.int64);pairCount=0
    for source,parent in enumerate(parents):
        children=[[] for _ in range(size)]
        for v,p in enumerate(parent):
            if v==source:
                if p>=0:raise ValueError('root has parent')
            elif labels[v]==labels[source]:
                if p<0 or p not in neighbors[v]:raise ValueError('non-incidence tree arc')
                children[int(p)].append(v)
            elif p>=0:raise ValueError('cross-component parent')
        order=[source]
        for v in order:order.extend(children[v])
        if len(order)!=int(np.sum(labels==labels[source])):raise ValueError('cycle or missing tree vertex')
        demand=np.zeros(size,dtype=np.int64)
        demand[source+1:n]=(labels[source+1:n]==labels[source])
        pairCount+=int(demand.sum())
        for v in reversed(order[1:]):demand[int(parent[v])]+=demand[v]
        loads+=demand[n:]
    return dict(loads=loads,pairCount=pairCount,componentSizes=list(Counter(labels[:n].tolist()).values()))


def buildRoutingRound(data,lengths=None):
    """Produce deterministic BFS trees initially, weighted shortest trees subsequently."""
    graph=buildRoutingGraph(data,lengths);n=len(data['vertices']);parents=np.empty((n,graph.shape[0]),dtype=np.int32)
    for source in range(n):
        if lengths is None:_,parent=breadth_first_order(graph,source,directed=True,return_predecessors=True)
        else:_,parent=dijkstra(graph,directed=True,indices=source,return_predecessors=True)
        parents[source]=parent
    audit=auditRoutingParents(data,parents)
    return parents,audit


def calculateRoutingBound(caps,componentSizes,loads,rounds=1):
    """Return exact ceiling of guaranteed separated demands divided by congestion."""
    guaranteed=rounds*sum(calculateSeparatedPairs(n,caps) for n in componentSizes)
    congestion=int(max(loads,default=0))
    ratio=Fraction(guaranteed,congestion) if congestion else Fraction(0)
    return dict(guaranteedSeparatedPairs=guaranteed,maxLanLoad=congestion,
                rationalBound=str(ratio),lowerBound=(ratio.numerator+ratio.denominator-1)//ratio.denominator,
                scope='Complete within-component mathematical pair demands; no service-demand input')
