#!/usr/bin/env python3
# Solve PDF Eq. (22) from a JSON instance and measured response tables.
# Outputs mapping/constraint audit only; does not modify a live cluster.
"""Static hypergraph placement with hard resource and measured-Q constraints."""
from __future__ import annotations

import argparse
import itertools
import json
import math
import random
from collections import Counter
from pathlib import Path

import numpy as np


def predictQuality(surface, cpu, memory):
    """Interpolate a monotone grid `surface` at normalized cpu/memory pressure.

    Upper-corner mode uses the higher-pressure cell corner to avoid assuming
    smooth behavior across a sharp knee; bilinear remains available for fixtures.
    q contains the fitted grid; margin is a separately calibrated one-sided
    residual allowance. Reject extrapolation instead of claiming a guarantee.
    """
    xs, ys = surface['cpu'], surface['memory']
    tolerance = 1e-12  # Float summation at an exact grid knot is not extrapolation.
    if not xs[0]-tolerance <= cpu <= xs[-1]+tolerance or not ys[0]-tolerance <= memory <= ys[-1]+tolerance:
        return None
    cpu = min(xs[-1], max(xs[0], cpu))
    memory = min(ys[-1], max(ys[0], memory))
    if surface.get('interpolation', 'bilinear') == 'upper_corner':
        i = min(len(xs)-1, int(np.searchsorted(xs, cpu-tolerance, side='left')))
        j = min(len(ys)-1, int(np.searchsorted(ys, memory-tolerance, side='left')))
        return max(0.0, surface['q'][i][j] - surface['margin'])
    values = [float(np.interp(memory, ys, row)) for row in surface['q']]
    return max(0.0, float(np.interp(cpu, xs, values)) - surface['margin'])


def validateInstance(instance):
    """Reject invalid units, IDs, grids and unsupported compatibility inputs."""
    pods, nodes = instance['pods'], instance['nodes']
    if not pods or not nodes or not 0 < instance.get('eta', .8) <= 1:
        raise ValueError('nonempty pods/nodes and eta in (0,1] required')
    for items in (pods, nodes, instance['networks']):
        if len({p['id'] for p in items}) != len(items):
            raise ValueError('duplicate IDs')
    nodeIds = {n['id'] for n in nodes}
    podIds = {p['id'] for p in pods}
    for pod in pods:
        if any(not math.isfinite(pod[r]) or pod[r] < 0 for r in ('cpu', 'memory')):
            raise ValueError('Pod demands must be finite and nonnegative')
        if not set(pod.get('allowedNodes', nodeIds)) <= nodeIds:
            raise ValueError('unknown allowedNodes')
    for node in nodes:
        if any(not math.isfinite(node[r]) or node[r] <= 0 for r in ('cpu', 'memory')):
            raise ValueError('capacities must be positive')
        if node['slots'] < 0 or int(node['slots']) != node['slots']:
            raise ValueError('slots must be a nonnegative integer')
        for r in ('baseCpu', 'baseMemory'):
            if not math.isfinite(node.get(r, 0)) or node.get(r, 0) < 0:
                raise ValueError('background occupancy must be nonnegative')
        surface = instance['surfaces'][node['profile']]
        if surface.get('interpolation', 'bilinear') not in ('bilinear', 'upper_corner'):
            raise ValueError('unknown interpolation mode')
        xs, ys, q = surface['cpu'], surface['memory'], np.array(surface['q'])
        if any(len(a) < 2 or any(not math.isfinite(v) or v < 0 for v in a)
               or any(b <= a0 for a0, b in zip(a, a[1:])) for a in (xs, ys)):
            raise ValueError('pressure axes must be finite, nonnegative and increasing')
        if q.shape != (len(xs), len(ys)) or not np.isfinite(q).all() or (q < 0).any():
            raise ValueError('invalid response grid')
        if (np.diff(q, axis=0) > 1e-9).any() or (np.diff(q, axis=1) > 1e-9).any():
            raise ValueError('response grid must be nonincreasing on both axes')
        if not math.isfinite(surface['margin']) or surface['margin'] < 0:
            raise ValueError('invalid uncertainty margin')
    for edge in instance['networks']:
        if len(set(edge['members'])) != len(edge['members']) or not set(edge['members']) <= podIds:
            raise ValueError('invalid hyperedge membership')
        if not math.isfinite(edge['weight']) or edge['weight'] <= 0:
            raise ValueError('communication weights must be positive')
    costs = np.array(instance['costs'])
    if (costs.shape != (len(nodes), len(nodes)) or not np.isfinite(costs).all()
            or (costs < 0).any() or not np.allclose(costs, costs.T)
            or np.any(np.diag(costs) != 0)):
        raise ValueError('costs must be a symmetric nonnegative matrix with zero diagonal')


class PlacementModel:
    """Evaluate complete or partial mappings against one validated instance."""

    def __init__(self, instance):
        """Index immutable pods, nodes and networks from `instance`."""
        validateInstance(instance)
        self.instance = instance
        self.pods = {p['id']: p for p in instance['pods']}
        self.nodes = {n['id']: n for n in instance['nodes']}
        self.index = {name: i for i, name in enumerate(self.nodes)}
        self.eta = instance.get('eta', .8)
        self.incident = {v: [] for v in self.pods}
        self.edgeWeights = []
        for index, edge in enumerate(instance['networks']):
            size = len(edge['members'])
            self.edgeWeights.append(edge['weight']/math.comb(size, 2) if size > 1 else 0.)
            for v in edge['members']:
                self.incident[v].append(index)

    def calculateCost(self, mapping):
        """Evaluate Eq. (21); normalize by the full hyperedge pair count."""
        total = 0.0
        for edge in self.instance['networks']:
            n = len(edge['members'])
            if n < 2:
                continue
            counts = Counter(mapping[v] for v in edge['members'] if v in mapping)
            for a, b in itertools.combinations(sorted(counts), 2):
                total += (edge['weight'] / math.comb(n, 2)
                          * self.instance['costs'][self.index[a]][self.index[b]]
                          * counts[a] * counts[b])
        return total

    def auditMapping(self, mapping, complete=True, enforceQuality=True, nodesToCheck=None):
        """Return auditable resource/Q/deployment failures for `mapping`.

        C is the Pod resource budget after system reservations; base occupancy
        appears only in pressure, exactly as in PDF Eq. (16),(17),(22).
        Per-Pod network tokens encode required CNI/VLAN capabilities explicitly.
        nodesToCheck optionally restricts a local move audit to affected VM names;
        callers must already have a fully audited feasible starting mapping.
        """
        failures, metrics = [], {}
        if not set(mapping) <= self.pods.keys() or not set(mapping.values()) <= self.nodes.keys():
            return {'feasible': False, 'failures': ['unknown mapping ID'], 'nodes': {}}
        if complete and set(mapping) != self.pods.keys():
            failures.append('mapping is incomplete')
        for name, node in self.nodes.items():
            if nodesToCheck is not None and name not in nodesToCheck:
                continue
            members = [self.pods[v] for v, k in mapping.items() if k == name]
            cpu, memory = (sum(p[r] for p in members) for r in ('cpu', 'memory'))
            rc = (cpu + node.get('baseCpu', 0)) / node['cpu']
            rm = (memory + node.get('baseMemory', 0)) / node['memory']
            q = predictQuality(self.instance['surfaces'][node['profile']], rc, rm)
            if cpu > node['cpu'] + 1e-9 or memory > node['memory'] + 1e-9:
                failures.append(f'{name}: resource capacity')
            if len(members) > node['slots']:
                failures.append(f'{name}: Pod slots')
            tokens = {t for p in members for t in p.get('networkTokens', [])}
            if not tokens <= set(node.get('networkTokens', [])):
                failures.append(f'{name}: CNI/VLAN compatibility')
            for pod in members:
                if name not in pod.get('allowedNodes', self.nodes):
                    failures.append(f'{pod["id"]}: node compatibility')
            if enforceQuality and (q is None or q + 1e-9 < self.eta):
                failures.append(f'{name}: uncalibrated pressure or Q below eta')
            metrics[name] = dict(cpu=cpu, memory=memory, rhoCpu=rc, rhoMemory=rm,
                                 qLower=q, pods=len(members))
        return dict(feasible=not failures, failures=failures, nodes=metrics)

    def buildInitial(self, mode, seed, enforceQuality=True):
        """Greedily construct a feasible mapping; failure is not infeasibility proof."""
        rng = random.Random(seed)
        order = sorted(self.pods)
        dominant = lambda v: min(max(self.pods[v]['cpu']/n['cpu'],
                                     self.pods[v]['memory']/n['memory'])
                                for n in self.nodes.values())
        degree = Counter()
        for e in self.instance['networks']:
            for v in e['members']:
                degree[v] += e['weight']
        if mode == 'resource':
            order.sort(key=lambda v: (-dominant(v), v))
        elif mode == 'network':
            order.sort(key=lambda v: (-degree[v], -dominant(v), v))
        elif mode == 'random':
            rng.shuffle(order)
        else:
            raise ValueError('unknown initialization mode')
        mapping = {}
        for v in order:
            candidates = []
            for k in sorted(self.nodes):
                trial = {**mapping, v: k}
                audit = self.auditMapping(trial, complete=False, enforceQuality=enforceQuality)
                if audit['feasible']:
                    candidates.append((self.calculateCost(trial), rng.random() if mode == 'random' else 0, k))
            if not candidates:
                return None
            mapping[v] = min(candidates)[2]
        return mapping

    def generateChanges(self, mapping, operators):
        """Yield move, swap and 10–20 Pod network/AS community moves."""
        ids = sorted(mapping)
        if 'move' in operators:
            for v in ids:
                for k in sorted(self.nodes):
                    if k != mapping[v]:
                        yield {v: k}
        if 'swap' in operators:
            for a, b in itertools.combinations(ids, 2):
                if mapping[a] != mapping[b]:
                    yield {a: mapping[b], b: mapping[a]}
        if 'group' in operators:
            groups = [e['members'] for e in self.instance['networks']]
            asns = sorted({str(p['community']) for p in self.pods.values() if 'community' in p})
            groups += [[v for v, p in self.pods.items() if str(p.get('community')) == a] for a in asns]
            seen = set()
            for group in groups:
                for source in sorted(self.nodes):
                    members = sorted(v for v in group if mapping[v] == source)
                    for size in (10, 15, 20):
                        for offset in range(0, len(members), size):
                            batch = tuple(members[offset:offset+size])
                            if len(batch) < 10 or batch in seen:
                                continue
                            seen.add(batch)
                            for target in sorted(self.nodes):
                                if target != source:
                                    yield dict.fromkeys(batch, target)

    def calculateEdgeCost(self, index, counts):
        """Return exact Eq. (21) contribution for edge `index` and VM `counts`."""
        return self.edgeWeights[index] * sum(
            self.instance['costs'][self.index[a]][self.index[b]]*counts[a]*counts[b]
            for a,b in itertools.combinations(sorted(k for k,v in counts.items() if v),2))

    def calculateChangeCost(self, change, mapping, edgeCounts, edgeCosts):
        """Return exact communication delta for `change` from cached full `mapping`.

        edgeCounts and edgeCosts must describe the current mapping, never an older
        search step. Only hyperedges incident to changed Pods can contribute.
        """
        affected={index for v in change for index in self.incident[v]}
        delta=0.
        for index in sorted(affected):
            counts=edgeCounts[index].copy()
            for v in change:
                if index in self.incident[v]:
                    counts[mapping[v]]-=1
                    counts[change[v]]+=1
            delta+=self.calculateEdgeCost(index,counts)-edgeCosts[index]
        return delta

    def improveMapping(self, mapping, operators=('move', 'swap', 'group'),
                       enforceQuality=True, maxSteps=1000):
        """Accept only feasible strict communication improvements; retain trace."""
        if not self.auditMapping(mapping, enforceQuality=enforceQuality)['feasible']:
            raise ValueError('local search requires a fully feasible starting mapping')
        trace = [self.calculateCost(mapping)]
        for _ in range(maxSteps):
            best, bestCost = None, trace[-1]
            edgeCounts=[Counter(mapping[v] for v in e['members']) for e in self.instance['networks']]
            edgeCosts=[self.calculateEdgeCost(i,c) for i,c in enumerate(edgeCounts)]
            for change in self.generateChanges(mapping, operators):
                cost = trace[-1] + self.calculateChangeCost(change,mapping,edgeCounts,edgeCosts)
                if cost >= bestCost - 1e-10:
                    continue
                trial = {**mapping, **change}
                affected=set(change.values()) | {mapping[v] for v in change}
                if self.auditMapping(trial, enforceQuality=enforceQuality,
                                     nodesToCheck=affected)['feasible']:
                    best, bestCost = trial, cost
            if best is None:
                return mapping, trace, True
            mapping = best
            verifiedCost=self.calculateCost(mapping)
            if abs(verifiedCost-bestCost)>1e-7:
                raise RuntimeError('incremental objective disagrees with full recomputation')
            trace.append(verifiedCost)
        return mapping, trace, False

    def solvePlacement(self, starts=12, seed=20260920, operators=('move', 'swap', 'group'),
                       enforceQuality=True):
        """Search deterministic resource/network/random starts, returning best feasible."""
        candidates, attempts = [], []
        for i in range(starts):
            mode = ('resource', 'network', 'random')[min(i, 2)]
            mapping = self.buildInitial(mode, seed+i, enforceQuality)
            if mapping is None:
                attempts.append(dict(start=i, mode=mode, status='construction_failed'))
                continue
            mapping, trace, converged = self.improveMapping(mapping, operators, enforceQuality)
            candidates.append((trace[-1], tuple(sorted(mapping.items())), mapping))
            attempts.append(dict(start=i, mode=mode, status='feasible', trace=trace, converged=converged))
        if not candidates:
            return dict(status='no_feasible_solution_found', attempts=attempts,
                        note='Heuristic failure does not prove infeasibility.')
        cost, _, mapping = min(candidates)
        return dict(status='feasible', mapping=mapping, cost=cost, attempts=attempts,
                    audit=self.auditMapping(mapping, enforceQuality=enforceQuality),
                    eta=self.eta, performanceConstraintEnabled=enforceQuality)

    def solveExact(self, maxAssignments=1000000, enforceQuality=True):
        """Enumerate a bounded small instance as an independent optimality oracle."""
        if len(self.nodes)**len(self.pods) > maxAssignments:
            raise ValueError('exhaustive enumeration limit exceeded')
        best, count = None, 0
        for values in itertools.product(sorted(self.nodes), repeat=len(self.pods)):
            mapping = dict(zip(sorted(self.pods), values))
            if self.auditMapping(mapping, enforceQuality=enforceQuality)['feasible']:
                count += 1
                cost = self.calculateCost(mapping)
                if best is None or cost < best['cost']:
                    best = dict(mapping=mapping, cost=cost)
        return dict(status='optimal' if best else 'proven_infeasible', feasibleAssignments=count,
                    **(best or {}))


def executeCli():
    """Read JSON instance and write placement result; never deploy workloads."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('instance', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--starts', type=int, default=12)
    parser.add_argument('--exact', action='store_true')
    args = parser.parse_args()
    model = PlacementModel(json.loads(args.instance.read_text()))
    result = model.solveExact() if args.exact else model.solvePlacement(starts=args.starts)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps({k: result[k] for k in ('status', 'cost') if k in result}))


if __name__ == '__main__':
    executeCli()
