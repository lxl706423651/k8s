"""Build exact capacity certificates and audited candidate allocation frontiers.
Inputs remain physical capacities and LAN membership plus proposed mappings.
Outputs certify capacity/observed dominance, never global cut optimality or QoS.
"""
from collections import Counter
from fractions import Fraction
from topologyAllocation import validate


def calculateCapacityBounds(data):
    """Certify exact fair capacity and next slot threshold for topology-only data."""
    validate(data);n=len(data['vertices']);hosts=data['hosts'];pools={r:sum(Fraction(str(h[r])) for h in hosts) for r in ['cpu','memory']}
    q=[n*min(Fraction(str(h[r]))/pools[r] for r in pools) for h in hosts]
    points=sorted({v/j for v in q for j in range(1,n+1)});left=0;right=len(points)-1;best=None
    while left<=right:
        middle=(left+right)//2;z=points[middle]
        if sum(min(n,v//z) for v in q)>=n:best=z;left=middle+1
        else:right=middle-1
    assert best is not None;caps=[min(n,int(v//best)) for v in q];following=max((v/(cap+1) for v,cap in zip(q,caps) if cap<n),default=Fraction(0))
    return dict(share=float(best),shareExact=str(best),qExact=[str(v) for v in q],countCaps=caps,continuousUpper=float(sum(q)/n),continuousUpperExact=str(sum(q)/n),nextSlotShare=float(following),nextSlotShareExact=str(following),unchangedEnvelopeRelativeLoss=float(1-following/best),scope='Exact fixed-bundle capacity certificate; counts unchanged for nextSlotShare < z <= share. No topology cost optimality claim.')


def buildVerifiedFrontier(data,candidates):
    """Audit candidate {id,mapping} records and return observed nondominated points.

    The data contract is validated once. Exact rational achieved shares prevent
    labelling search-level changes as actual entitlement losses. All candidates
    are kept in audit records, including dominated ones.
    """
    capacity=calculateCapacityBounds(data);q=list(map(Fraction,capacity['qExact']));hostIds=[h['id'] for h in data['hosts']];known=set(hostIds);vertices=set(data['vertices']);target=Fraction(capacity['shareExact']);records=[]
    for candidate in candidates:
        mapping=candidate['mapping']
        if set(mapping)!=vertices or not set(mapping.values())<=known:raise ValueError('incomplete or unknown mapping')
        counts=Counter(mapping.values());z=min(value/counts[host] for host,value in zip(hostIds,q) if counts[host]);cost=sum(len({mapping[v] for v in e['members']})-1 for e in data['networks'])
        records.append(dict(id=candidate['id'],share=float(z),shareExact=str(z),retained=float(z/target),cost=cost,counts={h:counts[h] for h in hostIds},maximumShare=z==target))
    frontier=[r for r in records if not any(Fraction(s['shareExact'])>=Fraction(r['shareExact']) and s['cost']<=r['cost'] and (Fraction(s['shareExact'])>Fraction(r['shareExact']) or s['cost']<r['cost']) for s in records)]
    unique={ (r['shareExact'],r['cost']):r for r in frontier};frontier=sorted(unique.values(),key=lambda r:(-Fraction(r['shareExact']),r['cost']))
    return dict(capacity=capacity,audited=records,frontier=frontier,scope='Observed candidate Pareto frontier, not proof of the global frontier; no automatic fairness sacrifice.')
