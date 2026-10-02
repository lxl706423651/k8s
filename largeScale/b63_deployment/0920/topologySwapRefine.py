"""Count-preserving boundary swaps after capacity-preserving moves.

Bounded local search; candidate restriction and timeout preclude optimality claims.
"""
from collections import Counter,defaultdict
import itertools,time
from topologyAllocation import audit


def refine(data,mapping,seconds=2.):
 start=time.monotonic();mapping=dict(mapping);initial=audit(data,mapping)
 edges=[e['members'] for e in data['networks']];incident=defaultdict(set)
 for j,e in enumerate(edges):
  for v in e:incident[v].add(j)
 ec=[Counter(mapping[v] for v in e) for e in edges];history=[];timeout=False
 for step in range(100):
  boundary=sorted({v for e in edges if len({mapping[v] for v in e})>1 for v in e})
  if len(boundary)<=256:pairs=itertools.combinations(boundary,2)
  else:
   representatives=defaultdict(list)
   for v in boundary:
    if len(representatives[mapping[v]])<8:representatives[mapping[v]].append(v)
   pairs=((a,b) for a in boundary for h,vs in representatives.items() if h!=mapping[a] for b in vs if a<b)
  best=None;best_delta=0
  for a,b in pairs:
   if time.monotonic()-start>=seconds:timeout=True;break
   ha,hb=mapping[a],mapping[b]
   if ha==hb:continue
   # Shared incident edges retain exactly the same occupancy.
   delta=0
   for j in incident[a]-incident[b]:delta+=(ec[j][hb]==0)-(ec[j][ha]==1)
   for j in incident[b]-incident[a]:delta+=(ec[j][ha]==0)-(ec[j][hb]==1)
   if delta<best_delta:best_delta=delta;best=(a,b,ha,hb)
  if best is None:break
  a,b,ha,hb=best
  for j in incident[a]:ec[j][ha]-=1;ec[j][hb]+=1
  for j in incident[b]:ec[j][hb]-=1;ec[j][ha]+=1
  mapping[a],mapping[b]=hb,ha;history.append(dict(a=a,b=b,delta=best_delta))
  if timeout:break
 result=audit(data,mapping)
 assert result['counts']==initial['counts']
 assert result['connectivity']==initial['connectivity']+sum(h['delta'] for h in history)
 return dict(mapping=mapping,**result,initialConnectivity=initial['connectivity'],history=history,seconds=time.monotonic()-start,timedOut=timeout)
