"""Combine different proof-terminal sets with original-LAN congestion witnesses.
Each column has its own capacity-proved separated-pair guarantee. Exact integer
weights and loads certify the result; LP output is only a candidate generator.
"""
from fractions import Fraction
import numpy as np
from scipy.optimize import linprog


def combineTerminalLoads(columns,guarantees,scale=1000000):
    """Pack nonnegative load columns with integer separated-pair guarantees."""
    matrix=np.asarray(columns,dtype=np.int64);r=matrix.shape[1]
    if len(guarantees)!=r or np.any(matrix<0) or not all(type(x)is int and x>=0 for x in guarantees) or max(guarantees,default=0)==0:raise ValueError('invalid terminal certificate matrix')
    solution=linprog(-np.asarray(guarantees)/max(guarantees),A_ub=matrix/max(1,int(matrix.max())),b_ub=np.ones(len(matrix)),bounds=(0,None),method='highs');candidates=[[int(i==j) for i in range(r)] for j in range(r)]
    if solution.x is not None and np.all(np.isfinite(solution.x)) and np.all(solution.x>=0) and max(solution.x)>0:candidates.append([int(round(x/max(solution.x)*scale)) for x in solution.x])
    best=None
    for weights in candidates:
        loads=[sum(int(x)*w for x,w in zip(row,weights)) for row in matrix];denominator=max(loads);numerator=sum(d*w for d,w in zip(guarantees,weights));ratio=Fraction(numerator,denominator) if denominator else Fraction(0)
        if best is None or ratio>best[0]:best=(ratio,weights,numerator,denominator)
    ratio,weights,numerator,denominator=best
    return dict(weights=weights,guaranteedSeparatedPairs=numerator,maxLanLoad=denominator,rationalBound=str(ratio),lowerBound=(ratio.numerator+ratio.denominator-1)//ratio.denominator,solverStatus=int(solution.status),scope='Different mathematical terminal sets, same original capacities and LANs; exact integer witness')
