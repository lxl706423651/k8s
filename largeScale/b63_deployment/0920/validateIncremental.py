#!/usr/bin/env python3
# Compare incremental objective deltas against full independent recomputation.
# Uses exhaustive small assignments and deterministic larger group-move cases.
"""Regression evidence for incident-edge and affected-VM candidate evaluation."""
from collections import Counter
import itertools
import json
from pathlib import Path
import random

from benchmarkPlacement import createBenchmark
from solvePlacement import PlacementModel
from validatePlacement import createFixture


def validateChanges(model,mapping):
    """Check all candidate changes for `mapping` against the full objective."""
    counts=[Counter(mapping[v] for v in e['members']) for e in model.instance['networks']]
    costs=[model.calculateEdgeCost(i,c) for i,c in enumerate(counts)]
    original=model.calculateCost(mapping)
    checked=0;maximum=0.
    for change in model.generateChanges(mapping,('move','swap','group')):
        delta=model.calculateChangeCost(change,mapping,counts,costs)
        complete=model.calculateCost({**mapping,**change})-original
        error=abs(delta-complete)
        assert error<1e-9,(change,delta,complete)
        maximum=max(maximum,error);checked+=1
        # Restricted feasibility must agree with full feasibility when the
        # unchanged nodes are already known feasible.
        if model.auditMapping(mapping)['feasible']:
            target={**mapping,**change}
            affected=set(change.values())|{mapping[v] for v in change}
            assert model.auditMapping(target)['feasible']==model.auditMapping(target,nodesToCheck=affected)['feasible']
    return checked,maximum


def executeValidation():
    """Persist exhaustive and group candidate regression counts under results/."""
    model=PlacementModel(createFixture())
    checked=0;maximum=0.
    for assignment in itertools.product(model.nodes,repeat=len(model.pods)):
        count,error=validateChanges(model,dict(zip(model.pods,assignment)))
        checked+=count;maximum=max(maximum,error)
    larger=PlacementModel(createBenchmark(24,11,.8))
    mapping={v:list(larger.nodes)[i//12] for i,v in enumerate(larger.pods)}
    assert any(len(change)>=10 for change in larger.generateChanges(mapping,('group',)))
    count,error=validateChanges(larger,mapping);checked+=count;maximum=max(maximum,error)
    for seed in range(5):
        rng=random.Random(seed)
        mapping={v:rng.choice(list(larger.nodes)) for v in larger.pods}
        count,error=validateChanges(larger,mapping);checked+=count;maximum=max(maximum,error)
    before=json.loads(Path('results/algorithm_validation_before_incremental.json').read_text())
    after=json.loads(Path('results/algorithm_validation.json').read_text())
    gaps=[a['heuristic']-b['heuristic'] for a,b in zip(after['benchmarkInstances'],before['benchmarkInstances'])]
    report=dict(candidateDeltasChecked=checked,maximumAbsoluteDeltaError=maximum,
                objectiveChangesOn20SmallInstances=gaps,
                note='Exact delta/full equivalence; restricted feasibility checked only from feasible starts.')
    Path('results/incremental_validation.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


if __name__=='__main__':
    executeValidation()
