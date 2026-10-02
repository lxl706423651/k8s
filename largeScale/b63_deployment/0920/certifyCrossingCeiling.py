"""Certify fixed-demand routing ceilings from integer distance potentials.
Consumes topology, original caps and three proof blocks; produces no deployment changes.
"""
from fractions import Fraction
import numpy as np
from certifyRoutingCeiling import certifyUniformCeiling, auditDistancePotentials


def auditCrossingCeiling(data, blocks, guarantee, lengths, potentials):
    """Check potentials and bound fixed guarantee/congestion for the given proof blocks."""
    auditDistancePotentials(data,lengths,potentials)
    n=len(data['vertices']);blocks=np.asarray(blocks)
    if blocks.shape!=(n,) or set(blocks.tolist())!={0,1,2} or type(guarantee)is not int or guarantee<0:
        raise ValueError('invalid proof blocks or fixed guarantee')
    total=0
    for source in range(n):
        row=potentials[source,source+1:n]
        if np.any(row<0):raise ValueError('connected proof topology required')
        total+=sum(int(d)*(1+int(blocks[source]==blocks[target])) for target,d in enumerate(row,source+1))
    if total<=0:raise ValueError('positive potential demand required')
    ratio=Fraction(guarantee*sum(map(int,lengths)),total)
    return dict(weightedDistanceTotal=total,guarantee=guarantee,rationalCeiling=str(ratio),ceilingOfIntegerLowerBound=(ratio.numerator+ratio.denominator-1)//ratio.denominator,scope='Fixed proof demand and fixed certified numerator only; any routing or routing mixture; NOT original partition optimum')


def certifyCrossingCeiling(data,caps,blocks,guarantee,lengths):
    """Generate integer potentials for topology/caps, then audit the specified proof demand."""
    potentials,_=certifyUniformCeiling(data,caps,lengths)
    return potentials,auditCrossingCeiling(data,blocks,guarantee,lengths,potentials)
