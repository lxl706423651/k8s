"""Refine capacity-only mappings by equal-size LAN fragment exchanges.
Inputs are topology and a feasible mapping; output is a nonworsening mapping.
No VM or deployment side effects. Run from the research workspace.
"""
from collections import Counter,defaultdict
import itertools,time
from topologyAllocation import audit


def refineBlocks(data,mapping,seconds=3.,maxSteps=50):
    """Exchange equal-size edge fragments in data; bound search by seconds/maxSteps."""
    start=time.monotonic();mapping=dict(mapping);before=audit(data,mapping)
    edges=[e['members'] for e in data['networks']];incident=defaultdict(set)
    for j,e in enumerate(edges):
        for v in e:incident[v].add(j)
    history=[];timedOut=False
    for step in range(maxSteps):
        groups=defaultdict(set)
        for edge in edges:
            fragments=defaultdict(list)
            for v in edge:fragments[mapping[v]].append(v)
            for vs in fragments.values():
                if 2<=len(vs)<=32:groups[len(vs)].add(tuple(sorted(vs)))
        ec=[Counter(mapping[v] for v in e) for e in edges];best=None;bestDelta=0
        for size in sorted(groups):
            for left,right in itertools.combinations(sorted(groups[size]),2):
                if time.monotonic()-start>=seconds:timedOut=True;break
                a,b=mapping[left[0]],mapping[right[0]]
                if a==b:continue
                deltaByEdge=defaultdict(Counter)
                for group,src,dst in [(left,a,b),(right,b,a)]:
                    for v in group:
                        for j in incident[v]:deltaByEdge[j][src]-=1;deltaByEdge[j][dst]+=1
                delta=sum((ec[j][h]+dv>0)-(ec[j][h]>0) for j,updates in deltaByEdge.items() for h,dv in updates.items())
                if delta<bestDelta:bestDelta=delta;best=(left,right,a,b)
            if timedOut:break
        if best is None:break
        left,right,a,b=best
        for v in left:mapping[v]=b
        for v in right:mapping[v]=a
        history.append(dict(left=left,right=right,delta=bestDelta))
        if timedOut:break
    after=audit(data,mapping)
    assert after['counts']==before['counts']
    assert after['connectivity']==before['connectivity']+sum(r['delta'] for r in history)
    return dict(mapping=mapping,**after,history=history,initialConnectivity=before['connectivity'],seconds=time.monotonic()-start,timedOut=timedOut)
