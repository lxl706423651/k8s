"""Build proof-path rounds with vectorized independent tree validation.
Equivalent certificate contract to certifyBalancedRouting, without Python per-arc loops.
"""
from collections import Counter
import numpy as np
from scipy.sparse.csgraph import breadth_first_order,connected_components,dijkstra
from certifyBalancedRouting import buildRoutingGraph


def auditRoutingTrees(data,parents):
    """Check incidence arcs and peel all trees, accumulating exact target counts."""
    graph=buildRoutingGraph(data);n=len(data['vertices']);size=graph.shape[0]
    if parents.shape!=(n,size) or not np.issubdtype(parents.dtype,np.integer):raise ValueError('invalid parent table')
    if n*(n-1)//2>=np.iinfo(np.int64).max:raise ValueError('integer load range exceeded')
    graph.data[:]=1
    _,labels=connected_components(graph,directed=False);loads=np.zeros(size-n,dtype=np.int64);pairCount=0;vertices=np.arange(size)
    for source,parent in enumerate(parents):
        reachable=labels==labels[source];nonroot=reachable & (vertices!=source);nodes=vertices[nonroot];ancestors=parent[nodes]
        if parent[source]>=0 or np.any(parent[~reachable]>=0) or np.any(ancestors<0) or np.any(ancestors>=size):raise ValueError('invalid root/reachability')
        if len(nodes) and not np.all(np.asarray(graph[ancestors,nodes]).ravel()==1):raise ValueError('invalid incidence arc')
        children=np.bincount(ancestors,minlength=size);demand=np.zeros(size,dtype=np.int64);demand[source+1:n]=reachable[source+1:n];pairCount+=int(demand.sum())
        leaves=nodes[children[nodes]==0];removed=0
        while len(leaves):
            removed+=len(leaves);up=parent[leaves];np.add.at(demand,up,demand[leaves]);np.add.at(children,up,-1)
            unique=np.unique(up);leaves=unique[(children[unique]==0)&(unique!=source)]
        if removed!=len(nodes):raise ValueError('cycle or incomplete tree')
        loads+=demand[n:]
    return dict(loads=loads,pairCount=pairCount,componentSizes=list(Counter(labels[:n].tolist()).values()))


def buildRoutingRound(data,lengths=None):
    """Build same BFS/weighted trees, then verify with vectorized integer peeling."""
    graph=buildRoutingGraph(data,lengths);n=len(data['vertices']);parents=np.empty((n,graph.shape[0]),dtype=np.int32)
    for source in range(n):
        if lengths is None:_,parent=breadth_first_order(graph,source,directed=True,return_predecessors=True)
        else:_,parent=dijkstra(graph,directed=True,indices=source,return_predecessors=True)
        parents[source]=parent
    return parents,auditRoutingTrees(data,parents)
