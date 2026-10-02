"""Pack integer proof columns without enumerating dense zero-weight single-column vectors.
Same LP proposal and exact-ratio acceptance as combineTerminalLoads; offline proof utility.
"""
from fractions import Fraction
import numpy as np
from scipy.optimize import linprog


def combineProofColumns(columns,guarantees,scale=1000000):
    """Pack nonnegative integer LAN columns with certified integer demand guarantees."""
    matrix=np.asarray(columns,dtype=np.int64)
    if matrix.ndim!=2 or not matrix.shape[0] or matrix.shape[1]!=len(guarantees) or np.any(matrix<0) or not all(type(d)is int and d>=0 for d in guarantees) or max(guarantees,default=0)<=0:raise ValueError('invalid proof matrix')
    maxima=matrix.max(axis=0)
    if np.any(maxima<=0):raise ValueError('positive column load required')
    ratios=[Fraction(d,int(b)) for d,b in zip(guarantees,maxima)];bestIndex=max(range(len(ratios)),key=ratios.__getitem__);weights=[int(i==bestIndex) for i in range(len(ratios))];ratio=ratios[bestIndex];numerator=guarantees[bestIndex];denominator=int(maxima[bestIndex]);solution=linprog(-np.array(guarantees)/max(guarantees),A_ub=matrix/max(1,int(matrix.max())),b_ub=np.ones(len(matrix)),bounds=(0,None),method='highs')
    if solution.x is not None and np.all(np.isfinite(solution.x)) and np.all(solution.x>=0) and max(solution.x)>0:
        proposed=[int(round(x/max(solution.x)*scale)) for x in solution.x];active=[(i,w) for i,w in enumerate(proposed) if w];loads=[sum(int(row[i])*w for i,w in active) for row in matrix];num=sum(d*w for d,w in zip(guarantees,proposed));den=max(loads);candidate=Fraction(num,den)
        if candidate>ratio:ratio,weights,numerator,denominator=candidate,proposed,num,den
    return dict(weights=weights,guaranteedSeparatedPairs=numerator,maxLanLoad=denominator,rationalBound=str(ratio),lowerBound=(ratio.numerator+ratio.denominator-1)//ratio.denominator,solverStatus=int(solution.status),scope='Certified integer demand/load mixture; LP only proposes weights')
