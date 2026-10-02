#!/usr/bin/env python3
# Fit monotone response grids using train-only data; calibrate lower margins separately.
# Requires frozen raw batches and writes model/provenance, without using final test data.
"""Fit the PDF F_type with shape constraints and independent residual calibration."""
import argparse
import ast
import hashlib
import json
import math
from pathlib import Path

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, minimize

from analyzeCalibration import reduceProfile
from solvePlacement import predictQuality


def buildInterpolation(cpuAxis, memoryAxis, points):
    """Build bilinear design matrix for `points` within increasing grid axes."""
    matrix=np.zeros((len(points),len(cpuAxis)*len(memoryAxis)))
    for row,(x,y) in enumerate(points):
        if not cpuAxis[0]<=x<=cpuAxis[-1] or not memoryAxis[0]<=y<=memoryAxis[-1]:
            raise ValueError('refusing extrapolation')
        i=min(len(cpuAxis)-2,max(0,int(np.searchsorted(cpuAxis,x,side='right')-1)))
        j=min(len(memoryAxis)-2,max(0,int(np.searchsorted(memoryAxis,y,side='right')-1)))
        tx=(x-cpuAxis[i])/(cpuAxis[i+1]-cpuAxis[i])
        ty=(y-memoryAxis[j])/(memoryAxis[j+1]-memoryAxis[j])
        for di,wx in ((0,1-tx),(1,tx)):
            for dj,wy in ((0,1-ty),(1,ty)):
                matrix[row,(i+di)*len(memoryAxis)+j+dj]=wx*wy
    return matrix


def fitGrid(cpuAxis,memoryAxis,rows):
    """Constrained least squares on train `rows`; require coverage of every grid node."""
    observed={(r['cpuFraction'],r['memoryFraction']) for r in rows}
    missing={(x,y) for x in cpuAxis for y in memoryAxis}-observed
    if missing:
        raise ValueError('unobserved training grid nodes: '+str(sorted(missing)))
    matrix=buildInterpolation(cpuAxis,memoryAxis,[(r['cpuFraction'],r['memoryFraction']) for r in rows])
    target=np.array([r['q'] for r in rows])
    constraints=[]
    width=len(memoryAxis)
    for i in range(len(cpuAxis)):
        for j in range(width):
            for ni,nj in ((i+1,j),(i,j+1)):
                if ni<len(cpuAxis) and nj<width:
                    row=np.zeros(matrix.shape[1]); row[i*width+j]=1; row[ni*width+nj]=-1
                    constraints.append(row)
    # The objective is convex quadratic. Ridge is intentionally absent: pilot
    # cannot tune a smoothing penalty using final test outcomes.
    objective=lambda z: float(np.mean((matrix@z-target)**2))
    gradient=lambda z: 2*matrix.T@(matrix@z-target)/len(target)
    result=minimize(objective,np.full(matrix.shape[1],np.mean(target)),jac=gradient,
                    constraints=[LinearConstraint(np.array(constraints),0,np.inf)],
                    bounds=Bounds(0,np.inf),method='SLSQP',options={'maxiter':2000,'ftol':1e-12})
    if not result.success:
        raise RuntimeError('monotone fit failed: '+result.message)
    q=result.x.reshape((len(cpuAxis),width))
    if np.max(np.diff(q,axis=0))>1e-8 or np.max(np.diff(q,axis=1))>1e-8:
        raise RuntimeError('shape constraints failed')
    return dict(cpu=cpuAxis,memory=memoryAxis,q=q.tolist(),margin=0.,
                trainingMse=objective(result.x),trainingRows=len(rows))


def selectValid(path):
    """Read raw `path`, reject incomplete/drifting blocks, return diagnostic counts."""
    rows,blocks=reduceProfile(path)
    valid=[r for r in rows if r['status']=='ok' and r['validForFit']]
    if not valid:
        raise ValueError('no valid rows: '+str(path))
    records=[json.loads(line) for line in path.read_text().splitlines()]
    metadata=next(r for r in records if r.get('kind')=='metadata')
    protocol=dict(warmupMode=metadata['config'].get('warmupMode','pressure_only'),
                  warmupSeconds=metadata['config']['warmup'],measurementSeconds=metadata['config']['seconds'])
    capacities={(r['cpuCount'],r['before']['memory']['MemTotal']) for r in records if r.get('status')=='ok'}
    if len(capacities)!=1:
        raise ValueError('guest capacity changed within a raw batch')
    archived=path.parent.parent/'measurePressure.py'
    tree=ast.parse(archived.read_text())
    canary=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='measureCanary')
    signature=hashlib.sha256(ast.dump(canary,include_attributes=False).encode()).hexdigest()
    tracking=metadata['config'].get('budgetTrackingTolerance',dict(relative=.02,absoluteCpuFraction=.0001))
    errors=[r['actualPressureCpu']-r['cpuFraction'] for r in valid]
    tolerances=[max(tracking['absoluteCpuFraction'],tracking['relative']*r['cpuFraction']) for r in valid]
    trackingAudit=dict(rows=len(valid),tolerance=tracking,
        maximumAbsoluteError=max(abs(e) for e in errors),maximumPositiveError=max(errors),
        overBudgetPoints=sum(e>t for e,t in zip(errors,tolerances)),
        underTargetPoints=sum(-e>t for e,t in zip(errors,tolerances)))
    control=metadata['config'].get('pressureControl','legacy_per_cycle_cpu_budget')
    plannedPoints=(len(metadata['config']['cpu'])*len(metadata['config']['memory'])*
                   len(metadata['config']['modes'])*metadata['config']['repeats'])
    if control=='cumulative_cpu_debt_v1' and len(valid)!=plannedPoints:
        raise ValueError(f'new controller batch incomplete/invalid: {len(valid)} valid of {plannedPoints} planned points')
    if control=='cumulative_cpu_debt_v1' and (trackingAudit['overBudgetPoints'] or trackingAudit['underTargetPoints']):
        raise ValueError('new controller failed predeclared CPU tracking gate: '+str(trackingAudit))
    return valid,dict(total=len(rows),valid=len(valid),plannedPoints=plannedPoints,blocks=blocks,
                      pressureControl=control,cpuBudgetTracking=trackingAudit,
                      capacity=list(next(iter(capacities))),canaryAstSha256=signature,measurementProtocol=protocol,
                      sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def readFrozenAudit(bundle,modelPath):
    """Recover missing protocol from hash-matched archived training near `modelPath`."""
    audit=dict(bundle['trainingAudit'])
    if 'measurementProtocol' in audit:
        return audit
    matches=[]
    for raw in modelPath.parent.glob('formal-*-train/*/raw.jsonl'):
        if hashlib.sha256(raw.read_bytes()).hexdigest()==audit['sha256']:
            _,recovered=selectValid(raw)
            if any(recovered[key]!=audit[key] for key in ('capacity','canaryAstSha256')):
                raise ValueError('archived training identity differs from frozen model')
            matches.append(recovered['measurementProtocol'])
    if len(matches)!=1:
        raise ValueError('legacy frozen model needs exactly one hash-matched training archive')
    audit['measurementProtocol']=matches[0]
    return audit


def calibrateMargin(surface,rows,alpha=.05):
    """Use held-out block maxima of optimistic residuals to choose one-sided margin.

    Entire randomization blocks, not per-task samples, are the calibration units.
    With fewer than ceil(1/alpha)-1 blocks, finite-sample 95% conformal rank would
    be infinite. Return empirical max and explicitly deny coverage certification.
    Different designed pressure distributions also preclude exchangeability claims.
    """
    scores={}
    for row in rows:
        prediction=predictQuality(surface,row['cpuFraction'],row['memoryFraction'])
        if prediction is None:
            raise ValueError('calibration point outside fitted domain')
        scores.setdefault(row['block'],[]).append(prediction-row['q'])
    values=sorted(max(0,max(v)) for v in scores.values())
    rank=math.ceil((len(values)+1)*(1-alpha))
    margin=values[min(rank,len(values))-1]
    return dict(margin=margin,alpha=alpha,blockCount=len(values),rank=rank,
                scores=values,finiteConformalRank=rank<=len(values),
                coverageCertified=False,
                interpretation='Empirical block-maximum safety allowance. No distribution-free guarantee: designed splits are not exchangeable.')


def bootstrapErrorInterval(predictions,resamples=10000,seed=20260920):
    """Resample whole observed blocks for a descriptive MAE interval, not deployment coverage."""
    blocks=sorted({r['block'] for r in predictions})
    sums=np.array([sum(abs(r['predictedQ']-r['observedQ']) for r in predictions if r['block']==b) for b in blocks])
    counts=np.array([sum(r['block']==b for r in predictions) for b in blocks])
    draws=np.random.default_rng(seed).integers(0,len(blocks),size=(resamples,len(blocks)))
    errors=np.sum(sums[draws],axis=1)/np.sum(counts[draws],axis=1)
    return dict(mae95PercentileInterval=np.quantile(errors,[.025,.975]).tolist(),
                blocks=len(blocks),resamples=resamples,seed=seed,
                interpretation='Whole-block bootstrap conditional on this designed experiment. Few blocks and temporal dependence limit coverage; not an independent-deployment safety bound.')


def evaluateRows(surface,rows,eta=.8):
    """Report prediction error and safe/unsafe classification on `rows`; no refit."""
    meanSurface={**surface,'margin':0}
    predictions=[]
    for row in rows:
        mean=predictQuality(meanSurface,row['cpuFraction'],row['memoryFraction'])
        lower=predictQuality(surface,row['cpuFraction'],row['memoryFraction'])
        if mean is None:
            raise ValueError('evaluation outside domain')
        predictions.append(dict(block=row['block'],mode=row['mode'],cpu=row['cpuFraction'],
                                memory=row['memoryFraction'],observedQ=row['q'],predictedQ=mean,lowerQ=lower,
                                predictedSafe=lower>=eta,observedSafe=row['q']>=eta))
    error=np.array([abs(r['predictedQ']-r['observedQ']) for r in predictions])
    safe=[r for r in predictions if r['predictedSafe']]
    near=[r for r in predictions if abs(r['observedQ']-eta)<=.15]
    blockReports=[]
    for block in sorted({r['block'] for r in predictions}):
        selected=[r for r in safe if r['block']==block]
        blockReports.append(dict(block=block,predictedSafePoints=len(selected),
            unsafeAmongPredictedSafe=sum(not r['observedSafe'] for r in selected),
            minimumObservedQ=min((r['observedQ'] for r in selected),default=None)))
    return dict(rows=len(rows),blockBootstrap=bootstrapErrorInterval(predictions),safeBlockReports=blockReports,
                blocksWithPredictedSafePoints=sum(r['predictedSafePoints']>0 for r in blockReports),
                blocksWithSafePredictionViolation=sum(r['unsafeAmongPredictedSafe']>0 for r in blockReports),
                worstOptimisticError=max(r['predictedQ']-r['observedQ'] for r in predictions),
                worstLowerBoundViolation=max(0.,max(r['lowerQ']-r['observedQ'] for r in predictions)),
                mae=float(np.mean(error)),p95AbsoluteError=float(np.quantile(error,.95)),
                predictedSafePoints=len(safe),unsafeAmongPredictedSafe=sum(not r['observedSafe'] for r in safe),
                nearThresholdRows=len(near),nearThresholdMae=(float(np.mean([abs(r['predictedQ']-r['observedQ']) for r in near])) if near else None),
                note='Points in a block are dependent. This is not the independent-deployment violation confidence bound.',
                predictions=predictions)


def executeFit(train,calibration,output):
    """Fit train raw `train`, set margin with `calibration`, write `output` bundle."""
    if train.resolve()==calibration.resolve():
        raise ValueError('training and calibration must be different batches')
    trainRows,trainAudit=selectValid(train)
    calRows,calAudit=selectValid(calibration)
    if any(trainAudit[k]!=calAudit[k] for k in ('capacity','canaryAstSha256','measurementProtocol')):
        raise ValueError('training/calibration capacity or canary differs')
    if trainAudit['sha256']==calAudit['sha256']:
        raise ValueError('identical training/calibration contents')
    axes=[sorted({r[field] for r in trainRows}) for field in ('cpuFraction','memoryFraction')]
    if any(len(axis)<2 for axis in axes):
        raise ValueError('two-dimensional calibration required')
    surface=fitGrid(*axes,trainRows)
    surface['interpolation']='upper_corner'
    calibrationReport=calibrateMargin(surface,calRows)
    surface['margin']=calibrationReport['margin']
    report=dict(surface=surface,trainingAudit=trainAudit,calibrationAudit=calAudit,
                calibration=calibrationReport,calibrationPerformance=evaluateRows(surface,calRows),
                status='empirically_calibrated_pending_independent_test')
    report['baselineLowerQ']=predictQuality(surface,surface['cpu'][0],surface['memory'][0])
    if report['baselineLowerQ']<.8:
        report['status']='ineligible_no_safe_region'
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:report[k] for k in ('status','calibration')}))
    if report['status']=='ineligible_no_safe_region':
        raise RuntimeError('calibrated lower surface has no eta=0.8 safe region; preserve test holdout and refine training')


def executeCli():
    """Expose disjoint fit and frozen-model test commands."""
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    fit=sub.add_parser('fit')
    fit.add_argument('--train',type=Path,required=True)
    fit.add_argument('--calibration',type=Path,required=True)
    fit.add_argument('--output',type=Path,required=True)
    test=sub.add_parser('test')
    test.add_argument('--model',type=Path,required=True)
    test.add_argument('--raw',type=Path,required=True)
    test.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.command=='fit':
        executeFit(args.train,args.calibration,args.output)
    else:
        bundle=json.loads(args.model.read_text())
        rows,audit=selectValid(args.raw)
        frozenAudit=readFrozenAudit(bundle,args.model)
        if any(audit[k]!=frozenAudit[k] for k in ('capacity','canaryAstSha256','measurementProtocol')):
            raise ValueError('test capacity or canary differs from frozen model')
        if audit['sha256'] in (bundle['trainingAudit']['sha256'],bundle['calibrationAudit']['sha256']):
            raise ValueError('test data reused from training/calibration')
        result=dict(modelSha256=hashlib.sha256(args.model.read_bytes()).hexdigest(),audit=audit,
                    verifiedTrainingProtocol=frozenAudit['measurementProtocol'],
                    metrics=evaluateRows(bundle['surface'],rows),
                    byMode={mode:evaluateRows(bundle['surface'],[r for r in rows if r['mode']==mode]) for mode in sorted({r['mode'] for r in rows})})
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps({k:v for k,v in result['metrics'].items() if k!='predictions'}))


if __name__=='__main__':
    executeCli()
