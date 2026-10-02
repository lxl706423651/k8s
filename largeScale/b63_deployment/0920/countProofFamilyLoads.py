"""Count many topology-derived terminal-family proofs on shared original path trees.
Consumes original topology, checked parent arrays and terminal lists; no workload inputs.
"""
import numpy as np
from buildBalancedRouting import auditRoutingTrees


def countProofFamilyLoads(data,parents,families):
    """Validate parents and batch unordered-pair LAN loads for each family of vertices."""
    audit=auditRoutingTrees(data,parents);n=len(data['vertices']);size=parents.shape[1];index={v:i for i,v in enumerate(data['vertices'])};membership=np.zeros((n,len(families)),dtype=np.int64)
    for j,family in enumerate(families):
        if len(set(family))!=len(family) or not set(family)<=set(index):raise ValueError('invalid terminal family')
        for v in family:membership[index[v],j]=1
    if audit['componentSizes']!=[n]:raise ValueError('connected topology required')
    loads=np.zeros((size-n,len(families)),dtype=np.int64);allNodes=np.arange(size)
    for source,parent in enumerate(parents):
        nodes=allNodes[allNodes!=source];children=np.bincount(parent[nodes],minlength=size);demand=np.zeros((size,len(families)),dtype=np.int64);demand[source+1:n]=membership[source+1:]*membership[source];leaves=nodes[children[nodes]==0]
        while len(leaves):
            up=parent[leaves];np.add.at(demand,up,demand[leaves]);np.add.at(children,up,-1);unique=np.unique(up);leaves=unique[(children[unique]==0)&(unique!=source)]
        loads+=demand[n:]
    return loads
