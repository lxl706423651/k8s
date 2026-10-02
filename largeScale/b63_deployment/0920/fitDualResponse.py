#!/usr/bin/env python3
# Merge training-only grids for a conditional-median response and a distinct lower envelope.
# The original test is never read; any redesigned model requires fresh calibration/test.
"""Separate central prediction accuracy from conservative admission response."""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from scipy.optimize import linprog
from scipy.sparse import csr_matrix, eye, hstack, vstack

from fitResponseSurface import selectValid


def fitMedianGrid(cpu,memory,rows):
    """Solve convex grid L1 isotonic regression using all repeated training `rows`."""
    indices={(x,y):i*len(memory)+j for i,x in enumerate(cpu) for j,y in enumerate(memory)}
    n=len(rows);size=len(indices)
    design=csr_matrix((np.ones(n),(np.arange(n),[indices[(r['cpuFraction'],r['memoryFraction'])] for r in rows])),shape=(n,size))
    target=np.array([r['q'] for r in rows]);absolute=eye(n,format='csr')
    constraints=[]
    for i,x in enumerate(cpu):
        for j,y in enumerate(memory):
            for ni,nj in [(i+1,j),(i,j+1)]:
                if ni<len(cpu) and nj<len(memory):
                    row=np.zeros(size);row[ni*len(memory)+nj]=1;row[i*len(memory)+j]=-1;constraints.append(row)
    monotone=csr_matrix(np.array(constraints))
    matrix=vstack([hstack([design,-absolute]),hstack([-design,-absolute]),hstack([monotone,csr_matrix((len(constraints),n))])],format='csr')
    result=linprog(np.r_[np.zeros(size),np.ones(n)/n],A_ub=matrix,b_ub=np.r_[target,-target,np.zeros(len(constraints))],bounds=(0,None),method='highs')
    if not result.success:raise RuntimeError(result.message)
    q=result.x[:size].reshape(len(cpu),len(memory))
    if np.max(np.diff(q,axis=0))>1e-8 or np.max(np.diff(q,axis=1))>1e-8:raise RuntimeError('median grid nonmonotone')
    return q,float(result.fun)


def fitDual(rawPaths,output):
    """Fit two explicitly different surfaces from complete training batches `rawPaths`."""
    rows=[];audits=[]
    for path in rawPaths:
        selected,audit=selectValid(path)
        if any(a['sha256']==audit['sha256'] for a in audits):raise ValueError('duplicate raw batch')
        if audits and any(audit[k]!=audits[0][k] for k in ['capacity','canaryAstSha256','measurementProtocol']):raise ValueError('training identity changed')
        if audits and audit.get('pressureControl','legacy_per_cycle_cpu_budget')!=audits[0].get('pressureControl','legacy_per_cycle_cpu_budget'):
            raise ValueError('pressure controller changed across training batches')
        rows.extend(selected);audits.append(audit)
    cpu=sorted({r['cpuFraction'] for r in rows});memory=sorted({r['memoryFraction'] for r in rows})
    cells={(x,y):[r['q'] for r in rows if r['cpuFraction']==x and r['memoryFraction']==y] for x in cpu for y in memory}
    if any(len(v)<5 for v in cells.values()):raise ValueError('incomplete repeated training grid')
    median,error=fitMedianGrid(cpu,memory,rows)
    lower=np.array([[min(cells[(x,y)]) for y in memory] for x in cpu])
    lower=np.minimum.accumulate(np.minimum.accumulate(lower,axis=0),axis=1)
    if np.any(lower>median+1e-8):raise ValueError('lower grid exceeds central grid')
    result=dict(modelKind='median_and_lower_envelope_v3',trainingAudits=audits,trainingRows=len(rows),trainingL1=error,
        nominal=dict(cpu=cpu,memory=memory,q=median.tolist(),interpolation='bilinear',margin=0.),
        lower=dict(cpu=cpu,memory=memory,q=lower.tolist(),interpolation='upper_corner',margin=0.),
        calibrated=False,coverageCertified=False,sourceSha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        note='Central surface estimates conditional median, not mean. Admission uses separate lower envelope; fresh calibration/test required.')
    with output.with_suffix('.fit-source.py').open('x') as handle:handle.write(Path(__file__).read_text())
    with output.open('x') as handle:json.dump(result,handle,indent=2);handle.write('\n')
    print(json.dumps(dict(trainingRows=len(rows),trainingL1=error,cpu=cpu,memory=memory),indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--raw',type=Path,nargs='+',required=True);parser.add_argument('--output',type=Path,required=True)
    a=parser.parse_args();fitDual(a.raw,a.output)
