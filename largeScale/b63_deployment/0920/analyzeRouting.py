#!/usr/bin/env python3
# Analyze measured BIRD process resource samples; fit nonnegative topology regressions.
# Outputs diagnostic coefficients and leave-feature-combination-out errors, not Pod guarantees.
"""Test whether the PDF's four features explain controlled routing resource demand."""
import argparse
import csv
import json
import re
import hashlib
from pathlib import Path

import numpy as np
from scipy.optimize import nnls

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

FEATURES=['intercept','nnet','niBGP','neBGP','nOSPF']


def validateGate(gate,case,peer=False):
    """Recheck saved protocol/load/RIB evidence for `case` on target or peer."""
    if not gate['success'] or gate['load1']>=50:
        raise ValueError('invalid load/protocol gate')
    protocols={}
    for line in gate['protocols'].splitlines():
        fields=line.split()
        if len(fields)>=4 and fields[1] in ('Device','Direct','BGP','OSPF','Static','Kernel'):
            protocols[fields[0]]=fields
            if fields[3]!='up':raise ValueError('inactive protocol: '+line)
    required=['device1','direct1']+[f'b{i}' for i in range(case['niBGP']+case['neBGP'])]
    if case['nOSPF']:required.append('o1')
    if peer:required.append('advertised')
    if any(name not in protocols for name in required):
        raise ValueError('missing expected protocol')
    for name,fields in protocols.items():
        if fields[1]=='BGP' and 'Established' not in fields:
            raise ValueError('BGP not established')
    if len(re.findall(r'\bFull\b',gate['ospfNeighbors'],re.I))!=case['nOSPF']:
        raise ValueError('OSPF neighbor count mismatch')
    expected=case['nnet']+(case['routes'] if peer or case['niBGP']+case['neBGP'] else 0)
    counts=[int(n) for n in re.findall(r'for (\d+) networks',gate['routes'])]
    if not counts or max(counts)<expected:
        raise ValueError('RIB prefixes missing: '+gate['routes'])


def analyzeRouting(raw,output):
    """Fit and cross-validate complete process observations in raw JSONL `raw`."""
    rows=[json.loads(line) for line in raw.read_text().splitlines()]
    if any(r['status']!='ok' or not r['postGate']['success'] for r in rows):
        raise ValueError('failed route samples must be diagnosed before fitting')
    for row in rows:
        validateGate(row['postGate'],row['case'])
        files=list(raw.parent.glob('*-'+row['case']['id']+'/protocol_gate.json'))
        if len(files)!=1:raise ValueError('missing/ambiguous initial protocol evidence')
        gates=json.loads(files[0].read_text())
        validateGate(gates['target'],row['case'])
        validateGate(gates['peer'],row['case'],peer=True)
    x=np.array([[1]+[r['case'][k] for k in FEATURES[1:]] for r in rows],dtype=float)
    keys=[tuple(row) for row in x]
    rank=int(np.linalg.matrix_rank(x))
    if rank!=len(FEATURES):
        raise ValueError('resource coefficients not identifiable: rank '+str(rank))
    scopes={r.get('scope','bird_process_only_pilot') for r in rows}
    if len(scopes)!=1:raise ValueError('mixed measurement scopes')
    scope=scopes.pop()
    report=dict(scope=scope,rawSha256=hashlib.sha256(raw.read_bytes()).hexdigest(),featureColumns=FEATURES,rank=rank,
                rows=len(rows),distinctCombinations=len(set(keys)),
                supportFeatures=sorted([list(key[1:]) for key in set(keys)]),
                routeScales=sorted({r['case']['routes'] for r in rows}),
                conditionNumber=float(np.linalg.cond(x)),metrics={},
                limitations=['CPU/RSS exclude kernel, container entrypoints and Kubernetes overhead.',
                             'Cross-validation is diagnostic. These are not independently held-out final tests.',
                             'Only steady control-plane state at fixed advertised-prefix scale was measured.'])
    if scope=='seed_router_container_cgroup':
        report['limitations']=['Container cgroup steady-state CPU and working set, not entire VM costs.', 'Excludes daemon/shim/kubelet/sandbox and uncharged kernel work.', 'Grouped CV is diagnostic; independent tests are still required.']
        metadata=raw.parent/'metadata.json'
        report['imageDigest']=json.loads(metadata.read_text())['image']
    output.mkdir(parents=True,exist_ok=True)
    table=[]
    fig,axes=plt.subplots(1,2,figsize=(10,4))
    for ax,metric in zip(axes,['cpuCores','memoryGiB']):
        y=np.array([r[metric] for r in rows])
        coefficients,_=nnls(x,y)
        cv=np.full(len(rows),np.nan)
        for key in sorted(set(keys)):
            test=np.array([k==key for k in keys]);train=~test
            if np.linalg.matrix_rank(x[train])<len(FEATURES):
                raise ValueError('CV training fold loses identifiability')
            fitted,_=nnls(x[train],y[train]);cv[test]=x[test]@fitted
        errors=np.abs(cv-y)
        summary=dict(coefficients=dict(zip(FEATURES,coefficients.tolist())),
                     trainingMae=float(np.mean(np.abs(x@coefficients-y))),
                     groupedCvMae=float(np.mean(errors)),groupedCvP95AbsoluteError=float(np.quantile(errors,.95)),
                     groupedCvRelativeMae=float(np.mean(errors/np.maximum(y,1e-12))),
                     observedMin=float(min(y)),observedMax=float(max(y)),
                     maximumUnderpredictionCvResidual=float(max(0,np.max(y-cv))))
        report['metrics'][metric]=summary
        for i,r in enumerate(rows):
            table.append(dict(case=r['case']['id'],metric=metric,observed=float(y[i]),
                              fitted=float(x[i]@coefficients),groupedCvPrediction=float(cv[i]),
                              **{k:r['case'][k] for k in FEATURES[1:]}))
        scale=1000 if metric=='cpuCores' else 1024
        ax.scatter(y*scale,cv*scale,s=22,alpha=.7)
        limits=[min(min(y),min(cv))*scale,max(max(y),max(cv))*scale]
        ax.plot(limits,limits,'k--')
        unit='millicores' if metric=='cpuCores' else 'MiB'
        ax.set(xlabel='Observed '+unit,ylabel='Held-combination-out prediction '+unit,
               title=f'{metric}: relative MAE {summary["groupedCvRelativeMae"]:.1%}')
    fig.suptitle('Real routing resource measurements: '+scope,fontsize=10)
    fig.tight_layout();fig.savefig(output/'resource_cv.png',dpi=160);fig.savefig(output/'resource_cv.pdf');plt.close(fig)
    with (output/'predictions.csv').open('w') as handle:
        writer=csv.DictWriter(handle,fieldnames=list(table[0]));writer.writeheader();writer.writerows(table)
    (output/'model.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


def executeCli():
    """Read raw and output paths and run diagnostic resource regression."""
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('raw',type=Path);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();analyzeRouting(args.raw,args.output)


if __name__=='__main__':
    executeCli()
