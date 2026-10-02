#!/usr/bin/env python3
# Offline figures from completed cross-environment evaluation JSON; no model mutation.
# Reads the three named transfer evaluations under results and writes a standalone figure.
"""Plot K3s observations alongside each frozen lower response bound."""
import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def plotTransfer(results,output):
    """Plot completed profile evaluations from `results` into `output`."""
    fig,axes=plt.subplots(1,3,figsize=(12,3.8),sharey=True)
    for ax,name in zip(axes,['c2m2','c4m2','c4m4']):
        report=json.loads((results/(name+'-k3s-transfer-evaluation.json')).read_text())
        rows=report['metrics']['predictions']
        for memory,marker in [(0,'o'),(.3,'^')]:
            selected=[r for r in rows if r['memory']==memory]
            ax.scatter([r['cpu'] for r in selected],[r['observedQ'] for r in selected],
                       alpha=.65,s=22,marker=marker,label=f'Observed, memory={memory:g}')
        unique={r['cpu']:r for r in rows if r['memory']==0}
        xs=sorted(unique)
        ax.step(xs,[unique[x]['lowerQ'] for x in xs],'k--',where='pre',label='Frozen lower Q, memory=0')
        ax.axhline(.8,color='gray',linestyle=':',label='Preset eta=0.8')
        ax.set(title=name,xlabel='Injected CPU / guest CPU',ylim=(-.05,1.08))
        ax.grid(alpha=.2)
    axes[0].set_ylabel('Normalized canary Q')
    axes[1].legend(loc='lower left',fontsize=7)
    fig.suptitle('Bare-VM to K3s diagnostics: 50 points/profile; not final deployment validation',fontsize=11)
    fig.tight_layout();output.mkdir(parents=True,exist_ok=True)
    fig.savefig(output/'transfer_quality.png',dpi=180);fig.savefig(output/'transfer_quality.pdf');plt.close(fig)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--results',type=Path,default=Path('results'));parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();plotTransfer(args.results,args.output)
