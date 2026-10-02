"""Refine a capacity-feasible mapping by jointly reassigning a small window.

Pure topology/capacity input; no VM effects. MILP optimality applies only to the
chosen window with all outside vertices fixed, never to the full partition.
"""
from collections import Counter, defaultdict
import random
import time

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_matrix

from topologyAllocation import audit, common_share


def selectWindow(data, mapping, limit=48, seed=1):
    """Choose up to limit vertices from data around a cut LAN using seed.

    Reserve boundary representatives from every occupied host to permit count
    exchanges when no free slot exists. This is a heuristic, not a coverage claim.
    """
    if limit < len(data['hosts']):
        raise ValueError('window must permit one representative per host')
    rng = random.Random(seed)
    incident = defaultdict(list)
    boundary = defaultdict(set)
    cut = []
    for edge in data['networks']:
        members = edge['members']
        for vertex in members:
            incident[vertex].append(members)
        if len({mapping[v] for v in members}) > 1:
            cut.append(members)
            for vertex in members:
                boundary[mapping[vertex]].add(vertex)
    if not cut:
        return []
    # Small cut LANs avoid always spending the window on a giant shared LAN.
    short = [e for e in cut if len(e) <= max(2, limit // 2)]
    anchor = list(rng.choice(short or cut))
    rng.shuffle(anchor)
    chosen = set()
    reserve = max(1, limit // (4 * len(data['hosts'])))
    for host in sorted(boundary):
        vertices = sorted(boundary[host])
        rng.shuffle(vertices)
        chosen.update(vertices[:reserve])
    queue = anchor + sorted(chosen)
    visited = set()
    while queue and len(chosen) < limit:
        vertex = queue.pop(0)
        if vertex in visited:
            continue
        visited.add(vertex)
        chosen.add(vertex)
        neighbors = sorted({v for edge in incident[vertex] for v in edge} - visited)
        rng.shuffle(neighbors)
        queue.extend(neighbors[:limit])
    return sorted(chosen)


def refineWindow(data, mapping, vertices, seconds=0.5):
    """Optimize vertices jointly; outside mapping stays fixed, data defines LANs.

    seconds bounds the MILP backend, not Python model construction. Results retain
    the incumbent on timeout/no improvement and record local-only certificates.
    """
    started = time.monotonic()
    if seconds <= 0:
        raise ValueError('positive solver time required')
    initial = audit(data, mapping)
    hosts = [h['id'] for h in data['hosts']]
    caps = common_share(data)['countCaps']
    if len(caps) != len(hosts) or any(initial['counts'][h] > c for h, c in zip(hosts, caps)):
        raise ValueError('initial mapping violates capacities')
    selected = sorted(set(vertices))
    if not selected or not set(selected) <= set(data['vertices']):
        raise ValueError('nonempty known window required')
    index = {v: i for i, v in enumerate(selected)}
    outside = Counter(h for v, h in mapping.items() if v not in index)
    touched = [e for e in data['networks'] if any(v in index for v in e['members'])]
    k = len(hosts)
    size = (len(selected) + len(touched)) * k
    objective = np.zeros(size)
    objective[len(selected)*k:] = 1
    lower = np.zeros(size)
    upper = np.ones(size)
    rr, cc, values, lo, hi = [], [], [], [], []

    def addConstraint(terms, low=-np.inf, high=np.inf):
        """Append sparse variable/coefficient terms with low/high bounds."""
        for column, value in terms.items():
            rr.append(len(lo)); cc.append(column); values.append(value)
        lo.append(low); hi.append(high)

    for i in range(len(selected)):
        addConstraint({i*k+h: 1 for h in range(k)}, 1, 1)
    for h, host in enumerate(hosts):
        addConstraint({i*k+h: 1 for i in range(len(selected))}, high=caps[h]-outside[host])
    currentSpan = 0
    for j, edge in enumerate(touched):
        inside = [index[v] for v in edge['members'] if v in index]
        fixed = {mapping[v] for v in edge['members'] if v not in index}
        currentSpan += len({mapping[v] for v in edge['members']})
        for h, host in enumerate(hosts):
            y = (len(selected)+j)*k+h
            if host in fixed:
                lower[y] = 1
                continue
            for i in inside:
                addConstraint({i*k+h: 1, y: -1}, high=0)
            addConstraint({y: 1, **{i*k+h: -1 for i in inside}}, high=0)
    # The incumbent is feasible; no worse local cost need be explored.
    addConstraint({i: 1 for i in range(len(selected)*k, size)}, high=currentSpan)
    integers = np.zeros(size)
    integers[:len(selected)*k] = 1
    result = milp(objective, integrality=integers, bounds=Bounds(lower, upper),
                  constraints=LinearConstraint(coo_matrix((values, (rr, cc)), shape=(len(lo), size)).tocsc(), lo, hi),
                  options=dict(time_limit=seconds, mip_rel_gap=0.))
    candidate = dict(mapping)
    accepted = False
    if result.x is not None:
        assignment = result.x[:len(selected)*k].reshape(len(selected), k)
        if np.max(abs(assignment-np.round(assignment))) > 1e-6:
            raise RuntimeError('nonintegral local incumbent')
        proposed = dict(mapping)
        proposed.update({v: hosts[int(np.argmax(assignment[i]))] for i, v in enumerate(selected)})
        measured = audit(data, proposed)
        assert all(measured['counts'][h] <= c for h, c in zip(hosts, caps))
        assert abs(measured['connectivity'] - initial['connectivity'] - result.fun + currentSpan) < 1e-6
        if measured['connectivity'] < initial['connectivity']:
            candidate = proposed
            accepted = True
    final = audit(data, candidate)
    bound = getattr(result, 'mip_dual_bound', None)
    return dict(mapping=candidate, **final, accepted=accepted, initialCost=initial['connectivity'],
                status=int(result.status), localOptimal=result.status == 0,
                localGlobalCostBound=None if bound is None else float(bound)+initial['connectivity']-currentSpan,
                window=selected, touchedLans=len(touched), seconds=time.monotonic()-started,
                scope='Only this window with outside vertices fixed; not a global optimum certificate')
