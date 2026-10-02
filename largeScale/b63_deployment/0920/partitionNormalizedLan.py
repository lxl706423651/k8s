"""Partition a normalized LAN clique without rounding its smallest edges to zero.

Reads capacity/topology and pinned KaHyPar configuration. Chooses integer scale
from LAN sizes, checks int32 weight sum, and reports a rounding-error bound.
No VM effects. This graph surrogate remains different from LAN connectivity.
"""
from collections import defaultdict
from fractions import Fraction
import itertools
import math

from topologyAllocation import audit, common_share


def partitionNormalizedLan(data, configuration, seed=1):
    """Partition data with configuration and seed; preserve positive pair weights.

    Keep the previous scale 100000 whenever all single-LAN pair weights round
    positively. Otherwise increase it to the largest LAN pair count. Refuse
    int32 overflow, and report an additive bound for any graph cut objective.
    """
    import kahypar
    envelope = common_share(data)
    pairMaximum = max((math.comb(len(e['members']), 2) for e in data['networks']), default=0)
    scale = 100000 if pairMaximum < 200000 else pairMaximum
    pairs = defaultdict(Fraction)
    for edge in data['networks']:
        members = sorted(edge['members'])
        if len(members) < 2:
            continue
        weight = Fraction(1, math.comb(len(members), 2))
        for pair in itertools.combinations(members, 2):
            pairs[pair] += weight
    index = {v: i for i, v in enumerate(data['vertices'])}
    pins, offsets, weights = [], [0], []
    for (a, b), weight in sorted(pairs.items()):
        rounded = round(scale*weight)
        if rounded <= 0:
            raise ValueError('positive pair weight lost to integer rounding')
        pins.extend([index[a], index[b]])
        offsets.append(len(pins)); weights.append(rounded)
    if sum(weights) >= 2**31:
        raise ValueError('normalized clique weights exceed conservative int32 range')
    k = len(data['hosts'])
    graph = kahypar.Hypergraph(len(index), len(weights), offsets, pins, k, weights, [1]*len(index))
    context = kahypar.Context()
    context.loadINIconfiguration(str(configuration))
    context.setK(k); context.setEpsilon(0.); context.setSeed(seed); context.suppressOutput(True)
    context.setCustomTargetBlockWeights(envelope['countCaps'])
    kahypar.partition(graph, context)
    mapping = {v: data['hosts'][graph.blockID(i)]['id'] for v, i in index.items()}
    measured = audit(data, mapping)
    valid = all(measured['counts'][h['id']] <= cap for h, cap in zip(data['hosts'], envelope['countCaps']))
    return dict(mapping=mapping, **measured, valid=valid, weightScale=scale,
                cliqueEdges=len(weights), graphCut=kahypar.connectivityMinusOne(graph),
                additiveGraphCutErrorBound=len(weights)/(2*scale),
                totalIntegerWeight=sum(weights), scope='Normalized graph surrogate; original LAN cost independently audited')
