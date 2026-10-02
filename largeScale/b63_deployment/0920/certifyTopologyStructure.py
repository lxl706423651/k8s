"""Certify topology-only lower bounds with integral hypergraph flows.
Consumes the existing capacity/topology schema and count caps. No VM effects.
Certificates are actual integer flows, checked against the input network.
"""
from collections import Counter
import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import maximum_flow


def buildIncidenceClasses(data):
    """Group vertices of data by identical incident LAN indices, retaining members."""
    incidence = {v: [] for v in data['vertices']}
    for e, network in enumerate(data['networks']):
        for v in network['members']:
            incidence[v].append(e)
    groups = {}
    for v, signature in incidence.items():
        groups.setdefault(tuple(signature), []).append(v)
    return [dict(networks=list(signature), members=members)
            for signature, members in groups.items()]


def buildLawlerNetwork(data):
    """Build integer directed LAN gadget and topology spanning-forest pairs."""
    n = len(data['vertices']); m = len(data['networks'])
    index = {v: i for i, v in enumerate(data['vertices'])}
    rows = []; cols = []; values = []; pairs = []; parent = list(range(n))
    def findRoot(v):
        """Find and compress the local disjoint-set root of vertex v."""
        while parent[v] != v:
            parent[v] = parent[parent[v]]; v = parent[v]
        return v
    for e, network in enumerate(data['networks']):
        a, b = n+2*e, n+2*e+1
        rows.append(a); cols.append(b); values.append(1)
        members = [index[v] for v in network['members']]
        for v in members:
            rows.extend([v, b]); cols.extend([a, v]); values.extend([m+1, m+1])
            left, right = findRoot(members[0]), findRoot(v)
            if left != right:
                parent[right] = left; pairs.append((members[0], v))
    matrix = csr_matrix((np.array(values, dtype=np.int64), (rows, cols)),
                        shape=(n+2*m, n+2*m))
    return matrix, pairs, list(Counter(findRoot(v) for v in range(n)).values())


def verifyFlowWitness(matrix, source, sink, value, arcs):
    """Independently verify positive integer flow arcs and conservation."""
    balance = [0]*matrix.shape[0]; seen = set()
    for u, v, amount in arcs:
        if any(type(x) is not int for x in (u, v, amount)):
            raise ValueError('noninteger flow witness')
        if not (0 <= u < len(balance) and 0 <= v < len(balance)):
            raise ValueError('unknown flow node')
        if (u,v) in seen or amount <= 0 or amount > int(matrix[u,v]):
            raise ValueError('invalid flow capacity or duplicate arc')
        seen.add((u,v)); balance[u] += amount; balance[v] -= amount
    expected = [0]*len(balance); expected[source] = value; expected[sink] = -value
    if balance != expected or type(value) is not int or value < 0:
        raise ValueError('flow conservation failure')


def calculateFlowProfile(n, witnesses):
    """Return cardinality cut lower bounds from verified pair-flow witnesses."""
    profile = [None]*(n+1)
    for threshold in sorted({0} | {w['value'] for w in witnesses}):
        parent = list(range(n))
        def findRoot(v):
            """Find the current threshold component of vertex v."""
            while parent[v] != v:
                parent[v] = parent[parent[v]]; v = parent[v]
            return v
        for w in witnesses:
            if w['value'] > threshold:
                a, b = findRoot(w['source']), findRoot(w['sink'])
                parent[a] = b
        bits = 1
        for size in Counter(findRoot(v) for v in range(n)).values():
            bits |= bits << size
        for size in range(n+1):
            if profile[size] is None and (bits >> size) & 1:
                profile[size] = threshold
    assert all(v is not None for v in profile)
    return profile


def calculateStructuralBounds(data, caps, countVectors, witnesses):
    """Combine separately valid bounds; countVectors must enumerate feasible counts."""
    n = len(data['vertices']); ordered = sorted(caps, reverse=True)
    def countRequiredHosts(size):
        """Return minimum host count whose capacities cover size vertices."""
        total = 0
        for k, cap in enumerate(ordered, 1):
            total += cap
            if total >= size: return k
        raise ValueError('infeasible capacities')
    _, _, components = buildLawlerNetwork(data)
    lanBound = sum(countRequiredHosts(len(e['members']))-1 for e in data['networks'])
    componentBound = sum(countRequiredHosts(size)-1 for size in components)
    profile = calculateFlowProfile(n, witnesses)
    flowBound = (min(sum(profile[x] for x in counts) for counts in countVectors)+1)//2
    return dict(lanBound=lanBound, componentBound=componentBound,
                flowBound=flowBound, lowerBound=max(lanBound,componentBound,flowBound),
                cutProfile=profile, componentSizes=components)


def certifyPairFlows(data, progress=None):
    """Compute and verify one max-flow per forest pair; optional progress callback."""
    matrix, pairs, _ = buildLawlerNetwork(data); witnesses = []
    for i, (source, sink) in enumerate(pairs):
        result = maximum_flow(matrix, source, sink, method='dinic')
        coo = result.flow.tocoo()
        arcs = [[int(u),int(v),int(f)] for u,v,f in zip(coo.row,coo.col,coo.data) if f>0]
        witness = dict(source=source,sink=sink,value=int(result.flow_value),arcs=arcs)
        verifyFlowWitness(matrix, **witness); witnesses.append(witness)
        if progress and (i+1)%100 == 0: progress(i+1,len(pairs))
    return witnesses


def checkSupportHall(weights, masks, caps):
    """Check all host-subset Hall inequalities for positive class supports.
    Each class can split; mask denotes exactly its nonempty hosts. Exponential
    in number of hosts, intended for small physical clusters only.
    """
    k = len(caps)
    if len(weights) != len(masks) or any(w<1 or a<1 or a >= 1<<k or a.bit_count()>w
                                      for w,a in zip(weights,masks)):
        return False
    for hosts in range(1,1<<k):
        demand = sum(w if a & hosts == a else (a & hosts).bit_count()
                     for w,a in zip(weights,masks))
        if demand > sum(cap for h,cap in enumerate(caps) if hosts & (1<<h)):
            return False
    return True
