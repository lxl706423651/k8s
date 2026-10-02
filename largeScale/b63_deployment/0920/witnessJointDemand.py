"""Construct a capacity-feasible proof-demand assignment to diagnose bound tightness.
This witness concerns the auxiliary demand graph, NOT original LAN cost optimality.
"""
from collections import Counter
from certifyBalancedRouting import calculateSeparatedPairs


def witnessDemandTightness(vertices,terminalSets,weights,caps,knownLower=None):
    """Order vertices by family membership count and audit a greedy host filling."""
    known=set(vertices)
    if len(known)!=len(vertices) or len(terminalSets)!=len(weights) or sum(caps)<len(vertices) or any(type(c)is not int or c<0 for c in caps):raise ValueError('invalid inputs')
    score={v:0 for v in vertices};sets=[]
    for members,w in zip(terminalSets,weights):
        s=set(members)
        if len(s)!=len(members) or not s<=known or type(w)is not int or w<0:raise ValueError('invalid terminal family')
        if not w:continue
        sets.append(s)
        for v in s:score[v]+=1
    order=sorted(range(len(vertices)),key=lambda i:(-score[vertices[i]],i));mapping={};offset=0
    for h in sorted(range(len(caps)),key=lambda h:(-caps[h],h)):
        for i in order[offset:offset+caps[h]]:mapping[vertices[i]]=h
        offset+=caps[h]
    assert set(mapping)==known and all(sum(h==i for h in mapping.values())<=cap for i,cap in enumerate(caps))
    cost=0
    for members,w in zip(terminalSets,weights):
        counts=Counter(mapping[v] for v in members);cost+=w*(len(members)**2-sum(x*x for x in counts.values()))//2
    independent=sum(w*calculateSeparatedPairs(len(members),caps) for members,w in zip(terminalSets,weights));lower=independent if knownLower is None else max(independent,knownLower)
    if cost<lower:raise ValueError('claimed lower exceeds feasible demand witness')
    chain=all(a<=b or b<=a for i,a in enumerate(sets) for b in sets[i+1:])
    if chain and cost!=independent:raise AssertionError('nested-family construction failed')
    return dict(mapping=mapping,demandCut=cost,independentLower=independent,knownLower=lower,exact=cost==lower,nestedChain=chain,scope='Tightness only of auxiliary weighted terminal-pair demand cut; not original partition cost')
