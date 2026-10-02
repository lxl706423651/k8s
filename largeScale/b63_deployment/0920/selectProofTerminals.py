"""Select proof-only terminal vertices from the incidence graph's 2-core.
Original partition vertices and physical capacities remain unchanged.
"""
from collections import deque
from certifyBalancedRouting import buildRoutingGraph


def selectCoreTerminals(data,caps):
    """Return the induced topology on incidence-2-core vertices, or no strict set."""
    graph=buildRoutingGraph(data);n=len(data['vertices']);size=graph.shape[0];degree=[int(graph.indptr[i+1]-graph.indptr[i]) for i in range(size)];active=[True]*size;queue=deque(i for i,d in enumerate(degree) if d<2)
    while queue:
        vertex=queue.popleft()
        if not active[vertex]:continue
        active[vertex]=False
        for neighbor in graph.indices[graph.indptr[vertex]:graph.indptr[vertex+1]]:
            if active[neighbor]:
                degree[neighbor]-=1
                if degree[neighbor]<2:queue.append(int(neighbor))
    vertices=[v for i,v in enumerate(data['vertices']) if active[i]]
    metadata=dict(originalVertices=n,coreVertices=len(vertices),largestHostCap=max(caps),strictSubset=0<len(vertices)<n,capacityActivated=len(vertices)>max(caps))
    if not metadata['strictSubset'] or not metadata['capacityActivated']:return None,metadata
    chosen=set(vertices);networks=[dict(id=e['id'],members=[v for v in e['members'] if v in chosen]) for e in data['networks'] if any(v in chosen for v in e['members'])]
    return dict(data,vertices=vertices,networks=networks),metadata
