#!/usr/bin/env python3
# Actual routing Q relative to temporally bracketing empty-cluster canary references.
# Zero injected pressure only; preserve invalid blocks and avoid counting tasks as trials.
"""Audit paired empty/routing/empty fixed-canary transfer observations."""
import argparse
import ast
import hashlib
import json
import math
from pathlib import Path
import statistics
from collections import Counter


class ObservationWindowError(ValueError):
    """Completed sampling window lacks valid observations; retain it as an invalid result."""

    def __init__(self,path,rows,errors):
        """Attach raw provenance and status counts, without inventing a Q value."""
        super().__init__('completed window lacks all 15 valid zero-pressure observations')
        self.audit=dict(rawPath=str(path),rawSha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            validObservations=len(rows),statusCounts=dict(Counter(r['status'] for r in rows+errors)))


def readWindow(batch,profile):
    """Read complete same-profile zero-pressure `batch` and fixed probe identity."""
    manifest=json.loads((batch/'manifest.json').read_text())
    if 'completed' not in manifest or any(p['status']!='completed' or p['transferReturnCode'] for p in manifest['profiles']):
        raise ValueError('incomplete canary window')
    path=batch/profile/'raw.jsonl';raw=[json.loads(line) for line in path.read_text().splitlines()]
    rows=[r for r in raw if r.get('status')=='ok']
    errors=[r for r in raw if r.get('status') not in (None,'ok')]
    if errors or len(rows)!=15:raise ObservationWindowError(path,rows,errors)
    if any(r['cpuFraction'] or r['memoryFraction'] for r in rows):raise ValueError('injected extra pressure present')
    metadata=next(r for r in raw if r.get('kind')=='metadata')
    tree=ast.parse((batch/'measurePressure.py').read_text())
    function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='measureCanary')
    identity=dict(canaryAstSha256=hashlib.sha256(ast.dump(function,include_attributes=False).encode()).hexdigest(),
        capacity=sorted({(r['cpuCount'],r['before']['memory']['MemTotal']) for r in rows}),
        seconds=metadata['config']['seconds'],warmup=metadata['config']['warmup'],warmupMode=metadata['config'].get('warmupMode','pressure_only'),
        pressureControl=metadata['config'].get('pressureControl','legacy_per_cycle_cpu_budget'),
        memoryReserveFraction=metadata['config'].get('memoryReserveFraction',.2))
    return rows,identity,hashlib.sha256(path.read_bytes()).hexdigest()


def compareWindows(before,loaded,after,profile,output,eta=.8):
    """Compute Q using log interpolation between empty references bracketing `loaded`."""
    if not 0<eta<=1:raise ValueError('eta must be in (0,1]')
    try:
        windows=[readWindow(p,profile) for p in [before,loaded,after]]
    except ObservationWindowError as error:
        result=dict(profile=profile,valid=False,qualityUnknown=True,reason=str(error),windowAudit=error.audit,
            minimumQ=None,meanQ=None,observationsBelowEta=None,eta=eta,independentDeploymentCount=1,
            note='Rejected/missing observations are not Q=0, not Q>=eta, and are not silently dropped. Original raw data retained.')
        with output.open('x') as handle:json.dump(result,handle,indent=2);handle.write('\n')
        print(json.dumps(result,indent=2));return
    if not windows[0][1]==windows[1][1]==windows[2][1]:raise ValueError('canary identity or guest capacity changed')
    pre,middle,post=[w[0] for w in windows]
    if not max(r['timestamp'] for r in pre)<min(r['timestamp'] for r in middle) or not max(r['timestamp'] for r in middle)<min(r['timestamp'] for r in post):
        raise ValueError('references do not bracket routing observations')
    centers=[statistics.mean(r['timestamp'] for r in group) for group in [pre,post]]
    refs=[{k:math.exp(statistics.mean(math.log(r['canary'][k]) for r in group)) for k in ['throughput','p95Seconds']} for group in [pre,post]]
    drift=max(abs(refs[1][k]/refs[0][k]-1) for k in refs[0])
    rows=[]
    for record in middle:
        fraction=(record['timestamp']-centers[0])/(centers[1]-centers[0])
        reference={k:math.exp((1-fraction)*math.log(refs[0][k])+fraction*math.log(refs[1][k])) for k in refs[0]}
        q=min(record['canary']['throughput']/reference['throughput'],reference['p95Seconds']/record['canary']['p95Seconds'])
        rows.append(dict(block=record['block'],kind=record['kind'],timestamp=record['timestamp'],q=q,reference=reference,
            throughput=record['canary']['throughput'],p95Seconds=record['canary']['p95Seconds']))
    result=dict(profile=profile,valid=drift<=.2,maximumReferenceDrift=drift,minimumQ=min(r['q'] for r in rows),
        meanQ=statistics.mean(r['q'] for r in rows),observationsBelowEta=sum(r['q']<eta for r in rows),eta=eta,
        independentDeploymentCount=1,identity=windows[0][1],rawSha256=[w[2] for w in windows],observations=rows,
        note='One routing deployment with 5 dependent temporal blocks; not 15 independent deployments. Empty-reference interpolation does not remove all environment or cache effects.')
    with output.open('x') as handle:json.dump(result,handle,indent=2);handle.write('\n')
    print(json.dumps({k:v for k,v in result.items() if k!='observations'},indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ['before','loaded','after','output']:parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--profile',required=True);parser.add_argument('--eta',type=float,default=.8)
    a=parser.parse_args();compareWindows(a.before,a.loaded,a.after,a.profile,a.output,a.eta)
