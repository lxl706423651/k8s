"""Joint topology placement and equal resource allocation from host capacities only.

Input: hosts {id,cpu,memory}, vertices [id], networks {id,members}.
No inferred workload demands, extra hosts, traffic profiles, or probe reservations.
"""
import argparse
import collections
import json
import math
from pathlib import Path
import time
import numpy as np
from scipy.optimize import milp, Bounds, LinearConstraint
from scipy.sparse import coo_matrix


def validate(data):
    if set(data)!={'hosts','vertices','networks'}:
        raise ValueError('only hosts, vertices, networks are accepted')
    ids=data['vertices'];hosts=data['hosts']
    if not ids or len(set(ids))!=len(ids) or not hosts or len({h['id'] for h in hosts})!=len(hosts):
        raise ValueError('nonempty unique vertex and host IDs required')
    for h in hosts:
        if set(h)!={'id','cpu','memory'} or any(not math.isfinite(h[r]) or h[r]<=0 for r in ['cpu','memory']):
            raise ValueError('host inputs are positive CPU/memory capacities only')
    for e in data['networks']:
        if set(e)!={'id','members'} or not e['members'] or len(set(e['members']))!=len(e['members']) or not set(e['members'])<=set(ids):
            raise ValueError('invalid unweighted network membership')


def common_share(data):
    """Exact largest fraction of the cluster-wide equal CPU/memory bundle.

    There exists an assignment at share z iff sum floor(q_k/z)>=N,
    where q_k=min(C_k/(sum C/N),M_k/(sum M/N)). No topology is used here.
    """
    validate(data);n=len(data['vertices'])
    bundle={r:sum(h[r] for h in data['hosts'])/n for r in ['cpu','memory']}
    q=[min(h[r]/bundle[r] for r in bundle) for h in data['hosts']]
    candidates=sorted({v/j for v in q for j in range(1,n+1)},reverse=True)
    for z in candidates:
        caps=[min(n,math.floor(v/z+1e-9)) for v in q]
        if sum(caps)>=n:
            return dict(share=z,nominalBundle=bundle,
                        reservation={r:z*bundle[r] for r in bundle},countCaps=caps)
    raise RuntimeError('positive-capacity instance must admit at least one allocation')


def audit(data,mapping):
    validate(data);hosts={h['id']:h for h in data['hosts']}
    if set(mapping)!=set(data['vertices']) or not set(mapping.values())<=set(hosts):
        raise ValueError('invalid complete mapping')
    counts=collections.Counter(mapping.values())
    bundle={r:sum(h[r] for h in hosts.values())/len(mapping) for r in ['cpu','memory']}
    rectangle={r:min(hosts[k][r]/v for k,v in counts.items()) for r in bundle}
    score=sum(len({mapping[v] for v in e['members']})-1 for e in data['networks'])
    return dict(counts={k:counts[k] for k in hosts},connectivity=score,
        commonReservationRectangle=rectangle,
        share=min(rectangle[r]/bundle[r] for r in bundle))


def solve(data,retention=1.,seconds=30):
    if not math.isfinite(retention) or not 0<retention<=1 or seconds<=0:
        raise ValueError('positive time and retention in (0,1] required')
    envelope=common_share(data);z=envelope['share']*retention
    ids=data['vertices'];hosts=data['hosts'];edges=data['networks'];n=len(ids);k=len(hosts)
    caps=[min(n,math.floor(min(h[r]/(z*envelope['nominalBundle'][r]) for r in ['cpu','memory'])+1e-9)) for h in hosts]
    index={v:i for i,v in enumerate(ids)};size=(n+len(edges))*k
    objective=np.zeros(size);objective[n*k:]=1
    rr=[];cc=[];vv=[];lo=[];hi=[]
    def add(terms,l=-np.inf,u=np.inf):
        for c,v in terms.items():rr.append(len(lo));cc.append(c);vv.append(v)
        lo.append(l);hi.append(u)
    for i in range(n):add({i*k+h:1 for h in range(k)},1,1)
    for h in range(k):add({i*k+h:1 for i in range(n)},u=caps[h])
    for j,e in enumerate(edges):
        for h in range(k):
            y=(n+j)*k+h
            for v in e['members']:add({index[v]*k+h:1,y:-1},u=0)
            add({y:1,**{index[v]*k+h:-1 for v in e['members']}},u=0)
    integrality=np.zeros(size);integrality[:n*k]=1
    started=time.monotonic()
    result=milp(objective,integrality=integrality,bounds=Bounds(np.zeros(size),np.ones(size)),
        constraints=LinearConstraint(coo_matrix((vv,(rr,cc)),shape=(len(lo),size)).tocsc(),lo,hi),
        options=dict(time_limit=seconds,mip_rel_gap=0.))
    output=dict(status=int(result.status),message=result.message,seconds=time.monotonic()-started,
                retention=retention,capacityEnvelope=envelope,countCaps=caps)
    if getattr(result,'mip_dual_bound',None) is not None:
        output['connectivityLowerBound']=float(result.mip_dual_bound)-len(edges)
    if result.x is not None:
        x=result.x[:n*k].reshape(n,k)
        if np.max(abs(x-np.round(x)))>1e-6:raise RuntimeError('nonintegral returned solution')
        mapping={v:hosts[int(np.argmax(x[i]))]['id'] for i,v in enumerate(ids)}
        measured=audit(data,mapping)
        assert measured['share']+1e-8>=z
        assert abs(measured['connectivity']-(result.fun-len(edges)))<1e-6
        output.update(mapping=mapping,**measured)
    return output


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('instance',type=Path);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--retention',type=float,default=1.);p.add_argument('--seconds',type=float,default=30.)
    a=p.parse_args();result=solve(json.loads(a.instance.read_text()),a.retention,a.seconds)
    with a.output.open('x') as f:json.dump(result,f,indent=2)
    print(json.dumps({k:v for k,v in result.items() if k!='mapping'}))
