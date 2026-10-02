#!/usr/bin/env python3
# Validate optimizer math using synthetic fixtures and exhaustive assignments.
# Produces a JSON audit; no synthetic value is a platform calibration result.
"""Independent pairwise-cost oracle, feasibility gates and small exact benchmarks."""
import copy
import itertools
import json
import random
import time
from pathlib import Path

from solvePlacement import PlacementModel, predictQuality


def createFixture(seed=0, size=6):
    """Generate a synthetic regression instance of `size` Pods using `seed`."""
    rng = random.Random(seed)
    return dict(evidenceKind='synthetic_algorithm_validation_only', eta=.8,
        pods=[dict(id=f'p{i}', cpu=rng.choice([.3,.5,.7]), memory=.2,
                   community=str(i//3)) for i in range(size)],
        nodes=[dict(id=f'n{i}', cpu=c, memory=3, slots=size, profile='test')
               for i, c in enumerate([2, 2.5, 3])],
        networks=[dict(id=f'e{i}', members=rng.sample([f'p{j}' for j in range(size)],
                  rng.randint(2, min(5, size))), weight=rng.uniform(.2, 3)) for i in range(size)],
        costs=[[0,1,2],[1,0,1.5],[2,1.5,0]],
        surfaces={'test': dict(cpu=[0,.5,1], memory=[0,.5,1],
                              q=[[1, .95,.85],[.95,.9,.8],[.6,.5,.4]], margin=.02)})


def calculatePairCost(instance, mapping):
    """Independent direct member-pair implementation of PDF Eq. (21)."""
    nodeIndex = {n['id']: i for i, n in enumerate(instance['nodes'])}
    result = 0
    for edge in instance['networks']:
        pairs = list(itertools.combinations(edge['members'], 2))
        if pairs:
            result += edge['weight'] * sum(instance['costs'][nodeIndex[mapping[a]]]
                     [nodeIndex[mapping[b]]] for a,b in pairs) / len(pairs)
    return result


def executeValidation():
    """Run independent checks and persist benchmark results under results/."""
    start = time.monotonic()
    fixture = createFixture()
    model = PlacementModel(fixture)
    count = 0
    for assignment in itertools.product(model.nodes, repeat=len(model.pods)):
        mapping = dict(zip(model.pods, assignment))
        assert abs(model.calculateCost(mapping) - calculatePairCost(fixture, mapping)) < 1e-10
        count += 1
    surface = fixture['surfaces']['test']
    assert predictQuality(surface, 1.01, .2) is None
    assert abs(predictQuality(surface, .5, .5)-.88) < 1e-10
    assert not model.auditMapping({'p0': 'missing'})['feasible']
    assert not model.auditMapping({})['feasible']
    constrained = copy.deepcopy(fixture)
    constrained['pods'][0]['allowedNodes'] = ['n2']
    constrained['pods'][0]['networkTokens'] = ['trunk0:100']
    constrained['nodes'][2]['networkTokens'] = ['trunk0:100']
    constrained['nodes'][0]['slots'] = 0
    cm = PlacementModel(constrained)
    assert not cm.auditMapping({'p0': 'n0'}, complete=False)['feasible']
    assert cm.auditMapping({'p0': 'n2'}, complete=False)['feasible']
    impossible = copy.deepcopy(fixture)
    impossible['pods'][0]['cpu'] = 100
    im = PlacementModel(impossible)
    assert im.solvePlacement()['status'] == 'no_feasible_solution_found'
    assert im.solveExact()['status'] == 'proven_infeasible'
    # Construct a 10-Pod community whose group move is observable independently.
    groupFixture = createFixture(size=20)
    for pod in groupFixture['pods']:
        pod['community'] = 'A'
    gm = PlacementModel(groupFixture)
    mapping = {f'p{i}': 'n0' if i < 10 else 'n1' for i in range(20)}
    assert any(len(c) == 10 for c in gm.generateChanges(mapping, ('group',)))
    rows = []
    for seed in range(20):
        instance = createFixture(seed)
        model = PlacementModel(instance)
        exact = model.solveExact()
        result = model.solvePlacement()
        assert exact['status'] == 'optimal' and result['status'] == 'feasible'
        assert model.auditMapping(result['mapping'])['feasible']
        assert result['cost'] >= exact['cost'] - 1e-9
        assert result == model.solvePlacement()
        for attempt in result['attempts']:
            trace = attempt.get('trace', [])
            assert all(b < a-1e-10 for a,b in zip(trace, trace[1:]))
        rows.append(dict(seed=seed, exact=exact['cost'], heuristic=result['cost'],
                         absoluteGap=result['cost']-exact['cost'],
                         feasibleAssignments=exact['feasibleAssignments']))
    report = dict(evidenceKind='synthetic_algorithm_validation_only',
                  pairwiseFormulaAssignments=count, benchmarkInstances=rows,
                  maximumAbsoluteGap=max(r['absoluteGap'] for r in rows),
                  optimalMatches=sum(r['absoluteGap'] < 1e-9 for r in rows),
                  durationSeconds=time.monotonic()-start,
                  checks=['exact pairwise cost', 'no extrapolation', 'bilinear interpolation',
                          'unknown IDs', 'complete assignment', 'allowed nodes', 'CNI/VLAN tokens',
                          'Pod slots', 'proven infeasibility vs heuristic failure',
                          '10-Pod group candidate', 'strict descent', 'deterministic seed',
                          'feasibility and optimality gaps on 20 instances'])
    Path('results/algorithm_validation.json').write_text(json.dumps(report, indent=2)+'\n')
    Path('results/synthetic_instance.json').write_text(json.dumps(fixture, indent=2)+'\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    executeValidation()
