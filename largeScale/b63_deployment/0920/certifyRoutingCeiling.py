"""Certify an upper ceiling on uniform all-pairs congestion LOWER BOUNDS.
This is not an upper/lower optimal partition certificate. Integer LAN lengths
and feasible distance potentials bound every possible routing's congestion.
"""
from fractions import Fraction
import numpy as np
from scipy.sparse.csgraph import dijkstra,connected_components
from certifyBalancedRouting import buildRoutingGraph,calculateSeparatedPairs


def auditDistancePotentials(data,lengths,potentials):
    """Verify nonnegative integer LAN lengths and all incidence arc inequalities."""
    n=len(data['vertices']);graph=buildRoutingGraph(data,lengths);size=graph.shape[0]
    if len(lengths)!=len(data['networks']) or not np.issubdtype(lengths.dtype,np.integer) or np.any(lengths<0):raise ValueError('invalid integer lengths')
    if potentials.shape!=(n,size) or not np.issubdtype(potentials.dtype,np.integer):raise ValueError('invalid integer potentials')
    _,labels=connected_components(graph,directed=False);coo=graph.tocoo();total=0
    for source,p in enumerate(potentials):
        reachable=labels==labels[source]
        if p[source]!=0 or np.any(p[reachable]<0) or np.any(p[~reachable]!=-1):raise ValueError('invalid reachability potential')
        arcs=reachable[coo.row]
        # Keep intermediate distances within int64, and exact stored arc lengths.
        if np.any(p[coo.col[arcs]]-p[coo.row[arcs]]>coo.data[arcs]):raise ValueError('distance inequality violated')
        total+=sum(int(x) for x in p[source+1:n] if x>=0)
    return total,[int(sum(labels[:n]==label)) for label in sorted(set(labels[:n]))]


def certifyUniformCeiling(data,caps,lengths):
    """Build integer shortest potentials and exact upper limit on routing bound."""
    lengths=np.asarray(lengths,dtype=np.int64)
    if max(lengths,default=0)*(len(data['vertices'])+len(data['networks']))>=2**52:raise ValueError('exact float distance range exceeded')
    graph=buildRoutingGraph(data,lengths);n=len(data['vertices']);potentials=np.empty((n,graph.shape[0]),dtype=np.int64)
    for source in range(n):
        distances=dijkstra(graph,directed=True,indices=source);finite=np.isfinite(distances)
        if np.any(distances[finite]!=np.round(distances[finite])):raise ValueError('noninteger shortest distance')
        potentials[source]=-1;potentials[source,finite]=distances[finite].astype(np.int64)
    total,components=auditDistancePotentials(data,lengths,potentials)
    demand=sum(calculateSeparatedPairs(n,caps) for n in components)
    if total<=0:raise ValueError('zero potential total gives no ceiling')
    upper=Fraction(demand*sum(int(x) for x in lengths),total)
    return potentials,dict(potentialTotal=total,lanLengthSum=sum(int(x) for x in lengths),guaranteedPairs=demand,rationalCeiling=str(upper),ceilingOfIntegerLowerBound=(upper.numerator+upper.denominator-1)//upper.denominator,scope='Upper limit only on this complete uniform-pair congestion bound, NOT on optimal partition cost')
