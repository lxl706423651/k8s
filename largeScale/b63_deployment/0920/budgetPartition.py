"""CPU/memory-budget partitioning. No application profiling or Q predictor inputs.

The MILP supports the unit cross-host-cost instance; returns a dual bound and gap.
Resource growth is a planning guarantee conditional on the supplied budget bounds.
"""
import argparse
import collections
import itertools
import json
import math
from pathlib import Path
import time
import numpy as np
from scipy.optimize import milp, Bounds, LinearConstraint
from scipy.sparse import coo_matrix


class BudgetModel:
    def __init__(self, instance):
        self.instance = instance
        self.pods = {p['id']: p for p in instance['pods']}
        self.nodes = {n['id']: n for n in instance['nodes']}
        if len(self.pods) != len(instance['pods']) or len(self.nodes) != len(instance['nodes']):
            raise ValueError('duplicate identifiers')
        if not self.pods or not self.nodes:
            raise ValueError('nonempty instance required')
        for items, positive in [(self.pods, False), (self.nodes, True)]:
            for item in items.values():
                for r in ['cpu', 'memory']:
                    v = item[r]
                    if not math.isfinite(v) or v < 0 or (positive and v == 0):
                        raise ValueError('invalid budget or capacity')
        for e in instance['networks']:
            if len(e['members']) != len(set(e['members'])) or not set(e['members']) <= self.pods.keys():
                raise ValueError('invalid hyperedge members')
            if not math.isfinite(e['weight']) or e['weight'] < 0:
                raise ValueError('invalid communication weight')

    def auditMapping(self, mapping, enforceQuality=False):
        if enforceQuality:
            raise ValueError('this model makes no Q prediction')
        failures = []
        if set(mapping) != set(self.pods) or not set(mapping.values()) <= self.nodes.keys():
            return dict(feasible=False, failures=['incomplete or unknown mapping'], nodes={})
        rows = {}
        for name, node in self.nodes.items():
            members = [self.pods[p] for p in mapping if mapping[p] == name]
            row = {r: sum(p[r] for p in members) for r in ['cpu', 'memory']}
            row['pods'] = len(members)
            if any(row[r] > node[r] + 1e-9 for r in ['cpu', 'memory']) or row['pods'] > node['slots']:
                failures.append(name + ': capacity')
            for p in members:
                if name not in p.get('allowedNodes', self.nodes) or not set(p.get('networkTokens', [])) <= set(node.get('networkTokens', [])):
                    failures.append(p['id'] + ': incompatible')
            rows[name] = row
        return dict(feasible=not failures, failures=failures, nodes=rows)

    def calculateCost(self, mapping):
        nodes = list(self.nodes); total = 0.
        for e in self.instance['networks']:
            pairs = list(itertools.combinations(e['members'], 2))
            if pairs:
                total += e['weight']/len(pairs)*sum(self.instance['costs'][nodes.index(mapping[a])][nodes.index(mapping[b])] for a, b in pairs)
        return total


def metrics(instance,mapping):
    model=BudgetModel(instance)
    audit=model.auditMapping(mapping,enforceQuality=False)
    utilization=max(max(r['cpu']/model.nodes[k]['cpu'],r['memory']/model.nodes[k]['memory'])
                    for k,r in audit['nodes'].items())
    return dict(feasible=audit['feasible'],cost=model.calculateCost(mapping),U=utilization,
                uniformBudgetGrowth=1/utilization-1 if utilization else None,counts={k:r['pods'] for k,r in audit['nodes'].items()})


def solve(instance,tau=None,seconds=30):
    model=BudgetModel(instance); pods=list(model.pods);nodes=list(model.nodes)
    n,k=len(pods),len(nodes)
    weights=collections.defaultdict(float)
    for e in instance['networks']:
        pairs=list(itertools.combinations(sorted(e['members']),2))
        for p in pairs:weights[p]+=e['weight']/len(pairs)
    # Exact formulation here is limited to the instance's uniform unit host costs.
    assert all(instance['costs'][a][b]==(0 if a==b else 1) for a in range(k) for b in range(k))
    pairs=list(weights); size=n*k+len(pairs)+1; u=size-1
    index={p:i for i,p in enumerate(pods)}
    objective=np.zeros(size)
    if tau is None:objective[u]=1
    else:
        for j,p in enumerate(pairs):objective[n*k+j]=weights[p]
    rr=[];cc=[];vv=[];lo=[];hi=[]
    def add(terms,lower=-np.inf,upper=np.inf):
        r=len(lo)
        for c,v in terms.items():rr.append(r);cc.append(c);vv.append(v)
        lo.append(lower);hi.append(upper)
    for i,p in enumerate(pods):add({i*k+h:1 for h in range(k)},1,1)
    for h,name in enumerate(nodes):
        node=model.nodes[name]
        for resource in ['cpu','memory']:
            terms={i*k+h:model.pods[p][resource]/node[resource] for i,p in enumerate(pods)}
            terms[u]=-1;add(terms,upper=0)
        add({i*k+h:1 for i in range(n)},upper=node['slots'])
    for j,(a,b) in enumerate(pairs):
        y=n*k+j
        for h in range(k):
            add({index[a]*k+h:1,index[b]*k+h:-1,y:-1},upper=0)
            add({index[b]*k+h:1,index[a]*k+h:-1,y:-1},upper=0)
    upper=np.ones(size);lower=np.zeros(size)
    upper[u]=1 if tau is None else tau
    for i,p in enumerate(pods):
        for h,name in enumerate(nodes):
            if name not in model.pods[p].get('allowedNodes',nodes) or not set(model.pods[p].get('networkTokens',[]))<=set(model.nodes[name].get('networkTokens',[])):
                upper[i*k+h]=0
    integrality=np.zeros(size);integrality[:n*k]=1
    matrix=coo_matrix((vv,(rr,cc)),shape=(len(lo),size)).tocsc()
    begin=time.monotonic()
    result=milp(objective,integrality=integrality,bounds=Bounds(lower,upper),
                constraints=LinearConstraint(matrix,lo,hi),
                options=dict(time_limit=seconds,mip_rel_gap=0.001))
    output=dict(tau=tau,seconds=time.monotonic()-begin,status=int(result.status),message=result.message,
                objective='min_U' if tau is None else 'min_communication',
                lowerBound=float(result.mip_dual_bound) if getattr(result,'mip_dual_bound',None) is not None else None,
                mipGap=float(result.mip_gap) if getattr(result,'mip_gap',None) is not None else None)
    if result.x is not None:
        assignment=result.x[:n*k].reshape(n,k)
        assert np.max(abs(assignment-np.round(assignment)))<1e-5
        mapping={p:nodes[int(np.argmax(assignment[i]))] for i,p in enumerate(pods)}
        m=metrics(instance,mapping);assert m['feasible']
        if tau is not None:assert m['U']<=tau+1e-7
        output.update(mapping=mapping,**m)
        assert abs((m['U'] if tau is None else m['cost'])-result.fun)<1e-5
    return output



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('instance', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--max-utilization', type=float)
    parser.add_argument('--seconds', type=float, default=30)
    args = parser.parse_args()
    if args.max_utilization is not None and not 0 < args.max_utilization <= 1:
        parser.error('max-utilization must be in (0,1]')
    if not math.isfinite(args.seconds) or args.seconds <= 0:
        parser.error('seconds must be finite and positive')
    result = solve(json.loads(args.instance.read_text()), args.max_utilization, args.seconds)
    with args.output.open('x') as f:
        json.dump(result, f, indent=2)
    print(json.dumps({k:v for k,v in result.items() if k!='mapping'}))


if __name__ == '__main__':
    main()
