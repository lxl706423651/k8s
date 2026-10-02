#!/usr/bin/env python3
# Static reviewer figures from completed independent evaluation and CPU-budget audit artifacts.
# Writes PDF/PNG and input hashes; does not fit models or remove outliers.
"""Show central prediction error separately from admission and injection validity."""
import argparse
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np


def plotValidation(evaluationPath, budgetPath, output):
    """Render all completed points from hash-matched `evaluationPath` and `budgetPath`."""
    evaluation=json.loads(evaluationPath.read_text());budget=json.loads(budgetPath.read_text())
    if evaluation['audit']['sha256']!=budget['rawSha256']:raise ValueError('evaluation and tracking audit belong to different samples')
    rows=evaluation['metrics']['predictions'];tracking=[r for r in budget['observations'] if r['status']=='ok' and r['validReference']]
    if len(rows)!=len(tracking):raise ValueError('tracking/evaluation point count mismatch')
    colors={'resident':'#0072B2','stream':'#009E73','integer':'#D55E00'}
    fig,axes=plt.subplots(1,3,figsize=(13,4.4),layout='constrained')
    for mode in sorted({r['mode'] for r in rows}):
        selected=[r for r in rows if r['mode']==mode]
        axes[0].scatter([r['observedQ'] for r in selected],[r['predictedQ'] for r in selected],s=19,alpha=.65,label=mode,color=colors[mode])
        t=[r for r in tracking if r['mode']==mode]
        axes[2].scatter([r['cpu'] for r in t],[r['actualCpuFraction'] for r in t],s=19,alpha=.65,label=mode,color=colors[mode])
    axes[0].plot([0,1.05],[0,1.05],'k--',lw=1)
    axes[0].set(xlabel='Observed Q',ylabel='Predicted central Q',title='(a) Independent central prediction')
    axes[0].legend(fontsize=8)
    errors=np.sort([abs(r['predictedQ']-r['observedQ']) for r in rows])
    axes[1].step(errors,np.arange(1,len(errors)+1)/len(errors),where='post',color='#0072B2')
    axes[1].axvline(.15,color='#D55E00',ls='--',label='P95 error limit = 0.15')
    axes[1].axhline(.95,color='black',ls=':',lw=1)
    metrics=evaluation['metrics']
    axes[1].set(xlabel='Absolute central prediction error',ylabel='Empirical cumulative fraction',ylim=(0,1.03),title=f"(b) MAE {metrics['mae']:.3f}; P95 {metrics['p95AbsoluteError']:.3f}")
    axes[1].legend(fontsize=8,loc='lower right')
    maximum=max(max(r['cpu'],r['actualCpuFraction']) for r in tracking)*1.04
    axes[2].plot([0,maximum],[0,maximum],'k--',lw=1)
    axes[2].set(xlabel='Commanded CPU / guest vCPU',ylabel='Measured CPU / guest vCPU',title='(c) Injection tracking, including warmup')
    axes[2].legend(fontsize=8)
    for ax in axes:ax.grid(alpha=.2);ax.set_axisbelow(True)
    caption=(f"{len(rows)} points in {evaluation['metrics']['blockBootstrap']['blocks']} pressure blocks; "
        f"lower-envelope admission: {metrics['unsafeAmongPredictedSafe']} violations / {metrics['predictedSafePoints']} admitted points. "
        'Pressure blocks are not independent deployments.')
    fig.supxlabel(caption,fontsize=9)
    output.mkdir(parents=True,exist_ok=False)
    fig.savefig(output/'response_validation.pdf');fig.savefig(output/'response_validation.png',dpi=160);plt.close(fig)
    (output/'provenance.json').write_text(json.dumps(dict(evaluationSha256=hashlib.sha256(evaluationPath.read_bytes()).hexdigest(),
        budgetAuditSha256=hashlib.sha256(budgetPath.read_bytes()).hexdigest(),pressureControl=budget['pressureControl'],
        legacyToleranceIsPostHoc=budget['legacyToleranceIsPostHoc'],allEvaluatedPointsRetained=True),indent=2)+'\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ['evaluation','budget-audit','output']:parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args();plotValidation(args.evaluation,args.budget_audit,args.output)
