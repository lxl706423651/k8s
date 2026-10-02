#!/usr/bin/env python3
# Publication-oriented static evidence: frozen tests and actual node/container transfer.
# Plots retain failures; no fitted curves or confidence claims beyond measured data.
"""Visualize response uncertainty and the resource-accounting gap."""
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np


def plotEvidence(output):
    """Write PDF/PNG from preserved experiment summaries into `output`."""
    test=json.loads(Path('results/c4m2-test.json').read_text())['metrics']['predictions']
    fine=json.loads(Path('results/fine-pressure-diagnostic.json').read_text())
    routing=json.loads(Path('results/topology214-routing-analysis/summary.json').read_text())
    fig,axes=plt.subplots(2,2,figsize=(11,8),layout='constrained')
    colors={'resident':'#0072B2','integer':'#D55E00','stream':'#009E73'}
    for mode,offset in [('resident',-.005),('integer',0),('stream',.005)]:
        rows=[r for r in test if r['mode']==mode]
        axes[0,0].scatter([r['cpu']+offset for r in rows],[r['observedQ'] for r in rows],s=12,alpha=.55,label=mode,color=colors[mode])
    axes[0,0].axhline(.8,color='black',ls='--',lw=1)
    axes[0,0].set(xlabel='Commanded CPU / guest vCPU',ylabel='Observed Q',title='(a) Independent c4m2 test: 270 points')
    axes[0,0].legend(fontsize=8)
    for name,color in [('c2m2','#0072B2'),('c4m4','#D55E00')]:
        groups=[r for r in fine[name]['groups'] if r['memory']==.6]
        axes[0,1].plot([r['cpu'] for r in groups],[r['minimumQ'] for r in groups],'o-',color=color,label=name+' min of 5')
        axes[0,1].plot([r['cpu'] for r in groups],[r['meanQ'] for r in groups],':',color=color,label=name+' mean of 5')
    axes[0,1].axhline(.8,color='black',ls='--',lw=1)
    axes[0,1].set(xlabel='Commanded CPU / guest vCPU',ylabel='Q at 60% resident memory',title='(b) Fresh low-load training: variation retained')
    axes[0,1].legend(fontsize=8)
    ns=routing['nodes'];x=np.arange(3);w=.25
    axes[1,0].bar(x-w,[n['containerCpuCores'] for n in ns],w,label='Routing container CPU')
    axes[1,0].bar(x,[n['nodeCpuIncreaseFromEmpty'] for n in ns],w,label='Node increase over empty')
    axes[1,0].bar(x+w,[n['correctedNodeCpuCores'] for n in ns],w,label='Whole node, sampler removed')
    axes[1,0].set(xticks=x,xticklabels=['c2m2','c4m2','c4m4'],ylabel='CPU cores',title='(c) One 180 s real deployment window')
    axes[1,0].legend(fontsize=7)
    axes[1,1].bar(x-w/2,[n['simultaneousContainerPeakGiB'] for n in ns],w,label='Peak container working set')
    axes[1,1].bar(x+w/2,[n['memoryProxyIncreaseFromEmptyGiB'] for n in ns],w,label='Node unavailable-memory increase')
    axes[1,1].set(xticks=x,xticklabels=['c2m2','c4m2','c4m4'],ylabel='GiB (different accounting metrics)',title='(d) Container budget omits node overhead')
    axes[1,1].legend(fontsize=7)
    for ax in axes.flat:ax.grid(axis='y',alpha=.2);ax.set_axisbelow(True)
    output.mkdir(parents=True,exist_ok=True)
    fig.savefig(output/'transfer_limits.pdf');fig.savefig(output/'transfer_limits.png',dpi=160);plt.close(fig)


if __name__=='__main__':plotEvidence(Path('results/transfer-limits-figures'))
