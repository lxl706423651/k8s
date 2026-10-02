"""Recount verified full-graph trees for three complementary proof terminal sets.
A pair in the same block appears twice; a pair in different blocks appears once.
No routing changes or workload measurements. Integer tree validation is retained.
"""
import numpy as np
from scipy.sparse.csgraph import connected_components
from certifyBalancedRouting import buildRoutingGraph


def countCrossingLoads(data,parents,blocks):
    """Verify original parent trees and count pair multiplicities from three blocks."""
    graph=buildRoutingGraph(data);n=len(data['vertices']);size=graph.shape[0];blocks=np.asarray(blocks)
    if blocks.shape!=(n,) or not np.issubdtype(blocks.dtype,np.integer) or set(blocks.tolist())!={0,1,2}:raise ValueError('three nonempty integer blocks required')
    if parents.shape!=(n,size) or not np.issubdtype(parents.dtype,np.integer):raise ValueError('invalid parent array')
    graph.data[:]=1;_,labels=connected_components(graph,directed=False)
    if len(set(labels[:n]))!=1:raise ValueError('this controlled crossing experiment requires connected topology')
    nodes=np.arange(size);loads=np.zeros(size-n,dtype=np.int64)
    for source,parent in enumerate(parents):
        childrenNodes=nodes[nodes!=source];up=parent[childrenNodes]
        if parent[source]>=0 or np.any(up<0) or np.any(up>=size) or not np.all(np.asarray(graph[up,childrenNodes]).ravel()==1):raise ValueError('invalid incidence tree')
        children=np.bincount(up,minlength=size);demand=np.zeros(size,dtype=np.int64);demand[source+1:n]=1+(blocks[source+1:]==blocks[source]).astype(np.int64);leaves=childrenNodes[children[childrenNodes]==0];removed=0
        while len(leaves):
            removed+=len(leaves);up=parent[leaves];np.add.at(demand,up,demand[leaves]);np.add.at(children,up,-1);unique=np.unique(up);leaves=unique[(children[unique]==0)&(unique!=source)]
        if removed!=size-1:raise ValueError('cyclic parent tree')
        loads+=demand[n:]
    return loads
