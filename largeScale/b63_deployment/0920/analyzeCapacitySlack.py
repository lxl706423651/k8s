"""Explain integer slot rigidity at the maximum common resource entitlement.
Reads only the established capacity/topology input; outputs a structural bound.
No partition search or VM effects. Count-vector enumeration is deliberately capped.
"""
import math
from buildAllocationFrontier import calculateCapacityBounds


def analyzeCapacitySlack(data, enumerationLimit=10000):
    """Return exact envelope, free slots and feasible count vectors for data.

    enumerationLimit bounds enumeration work; a rigorous combinatorial upper
    bound is reported even when enumeration is omitted. At most K-1 unused
    slots exist at maximum entitlement, independent of topology size N.
    """
    if enumerationLimit < 1:
        raise ValueError('positive enumeration limit required')
    envelope = calculateCapacityBounds(data)
    caps = envelope['countCaps']
    slack = sum(caps)-len(data['vertices'])
    assert 0 <= slack < len(caps)
    upper = math.comb(slack+len(caps)-1, len(caps)-1)
    vectors = []

    def allocateDeficit(prefix, remaining):
        """Enumerate remaining unused slots after the prefix of host deficits."""
        host = len(prefix)
        if host == len(caps)-1:
            if remaining <= caps[host]:
                deficits = prefix+[remaining]
                vectors.append([cap-d for cap, d in zip(caps, deficits)])
            return
        for deficit in range(min(caps[host], remaining)+1):
            allocateDeficit(prefix+[deficit], remaining-deficit)

    if upper <= enumerationLimit:
        allocateDeficit([], slack)
    return dict(capacity=envelope, unusedSlots=slack, unusedSlotUpperBound=len(caps)-1,
                uniqueCountVector=slack == 0, countVectorUpperBound=upper,
                enumerationComplete=upper <= enumerationLimit,
                countVectors=vectors if upper <= enumerationLimit else None,
                scope='Rigidity of the maximum-entitlement envelope, not topology cost optimality')
