"""Monotone, capacity-preserving refinement of an existing hypergraph partition.

Uses vertex moves and moves of the same-host members of a hyperedge. No demand
or traffic inputs. This is local search, with no global optimality guarantee.
"""
from collections import Counter,defaultdict
import math,time
from topologyAllocation import common_share,audit


def refine(data,mapping,retention=1.,seconds=5.,max_steps=200):
    if not 0<retention<=1 or seconds<=0:raise ValueError('invalid search parameters')
    start=time.monotonic();mapping=dict(mapping);initial=audit(data,mapping);env=common_share(data)
    target=retention*env['share'];hosts=[h['id'] for h in data['hosts']]
    caps={h['id']:math.floor(min(h[r]/(target*env['nominalBundle'][r]) for r in ['cpu','memory'])+1e-9) for h in data['hosts']}
    counts=Counter(mapping.values());assert all(counts[h]<=caps[h] for h in hosts)
    edges=[e['members'] for e in data['networks']];incident=defaultdict(list)
    for j,e in enumerate(edges):
        for v in e:incident[v].append(j)
    occupancy=[Counter(mapping[v] for v in e) for e in edges];history=[];timeout=False
    for step in range(max_steps):
        groups=[(v,) for v in data['vertices']]
        for e in edges:
            byhost=defaultdict(list)
            for v in e:byhost[mapping[v]].append(v)
            groups.extend(tuple(vs) for vs in byhost.values() if len(vs)>1)
        best=None;best_delta=0
        for group in groups:
            if time.monotonic()-start>=seconds:timeout=True;break
            src=mapping[group[0]];changed=Counter(j for v in group for j in incident[v])
            for dst in hosts:
                if dst==src or counts[dst]+len(group)>caps[dst]:continue
                delta=sum((occupancy[j][dst]==0)-(occupancy[j][src]==q) for j,q in changed.items())
                if delta<best_delta:best_delta=delta;best=(group,src,dst,changed)
        if best is None:break
        group,src,dst,changed=best
        counts[src]-=len(group);counts[dst]+=len(group)
        for v in group:mapping[v]=dst
        for j,q in changed.items():occupancy[j][src]-=q;occupancy[j][dst]+=q
        history.append(dict(delta=best_delta,vertices=len(group),source=src,target=dst))
        if timeout:break
    result=audit(data,mapping)
    assert result['connectivity']==initial['connectivity']+sum(x['delta'] for x in history)
    assert result['share']>=target-1e-8
    return dict(mapping=mapping,**result,initialConnectivity=initial['connectivity'],history=history,
                seconds=time.monotonic()-start,timedOut=timeout,retention=retention)
