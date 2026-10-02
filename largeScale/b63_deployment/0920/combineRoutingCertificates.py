"""Combine existing integer path-load witnesses with verified integer weights.
LP suggests weights only; final rational bound uses exact integer arithmetic.
No solver status is treated as a correctness certificate.
"""
import numpy as np
from scipy.optimize import linprog
from certifyBalancedRouting import calculateSeparatedPairs
from fractions import Fraction


def combineRoutingLoads(loadColumns,componentSizes,caps,scale=1000000):
    """Return auditable nonnegative integer round weights and exact congestion ratio."""
    columns=np.asarray(loadColumns,dtype=np.int64)
    if columns.ndim!=2 or np.any(columns<0) or scale<1:raise ValueError('invalid load matrix/scale')
    m,r=columns.shape
    if r==0:raise ValueError('at least one routing required')
    normalizer=max(1,int(columns.max()))
    result=linprog(-np.ones(r),A_ub=columns/normalizer,b_ub=np.ones(m),bounds=(0,None),method='highs')
    candidates=[[1]*r]+[[int(i==j) for i in range(r)] for j in range(r)]
    if result.x is not None and np.all(np.isfinite(result.x)) and np.all(result.x>=0) and max(result.x)>0:
        candidates.append([int(round(v/max(result.x)*scale)) for v in result.x])
    demand=sum(calculateSeparatedPairs(n,caps) for n in componentSizes);best=None
    for weights in candidates:
        # Python integers prevent accumulation overflow and eliminate FP proof error.
        loads=[sum(int(a)*w for a,w in zip(row,weights)) for row in columns]
        congestion=max(loads,default=0);ratio=Fraction(demand*sum(weights),congestion) if congestion else Fraction(0)
        if best is None or ratio>best[0]:best=(ratio,weights,congestion)
    ratio,weights,congestion=best
    return dict(weights=weights,maxLanLoad=congestion,guaranteedSeparatedPairs=demand*sum(weights),rationalBound=str(ratio),lowerBound=(ratio.numerator+ratio.denominator-1)//ratio.denominator,solverStatus=int(result.status),scope='Integer-weight certificate independently evaluated; no LP optimality claim')
