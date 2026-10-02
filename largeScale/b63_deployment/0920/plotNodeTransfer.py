#!/usr/bin/env python3
# Static held-out node-overhead transfer figures; three nodes are one deployment block.
"""Compare frozen fixture-only and corrected node resource predictions."""
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def plotTransfer(output):
    """Plot preserved held-out predictions without refitting or adding confidence bands."""
    data=json.loads(Path('results/node-overhead-transfer-evaluation.json').read_text())['nodes']
    fig,axes=plt.subplots(1,2,figsize=(10,4.5),layout='constrained')
    for ax,metric,label in zip(axes,['cpuCores','memoryGiB'],['Node CPU increase (cores)','Node unavailable-memory increase (GiB)']):
        observed=[r[metric]['observed'] for r in data];corrected=[r[metric]['prediction'] for r in data];fixture=[r[metric]['fixtureOnly'] for r in data]
        ax.scatter(observed,fixture,marker='x',s=65,label='Fixture only',color='#D55E00')
        ax.scatter(observed,corrected,s=55,label='Frozen correction',color='#0072B2')
        for row,x,y0,y1 in zip(data,observed,fixture,corrected):
            ax.plot([x,x],[y0,y1],color='grey',lw=.7,alpha=.5)
            name=row['node'].rsplit('-',1)[-1]
            offset=(-76,25) if metric=='cpuCores' and name=='c4m2' else ((-76,-23) if metric=='cpuCores' and name=='c4m4' else (5,5))
            ax.annotate(name+' / '+str(row['pods'])+' Pods',(x,y1),xytext=offset,textcoords='offset points',fontsize=8,
                arrowprops=dict(arrowstyle='-',lw=.6,color='grey') if offset[0]<0 else None)
        upper=max(observed+corrected)*1.2
        ax.plot([0,upper],[0,upper],'k--',lw=1,label='Exact prediction')
        ax.set(xlim=(0,upper),ylim=(0,upper),xlabel='Observed '+label.lower(),ylabel='Predicted increment',title=label)
        ax.legend(fontsize=8,loc='upper left');ax.grid(alpha=.2)
    fig.suptitle('Held-out 36 / 84 / 94 placement; one deployment block',fontsize=12)
    output.mkdir(parents=True,exist_ok=True);fig.savefig(output/'node_transfer.pdf');fig.savefig(output/'node_transfer.png',dpi=160);plt.close(fig)


if __name__=='__main__':plotTransfer(Path('results/node-transfer-figures'))
