#!/usr/bin/env python3
# Audit one predeclared comparison round; writes JSON/Markdown in an exclusive directory.
# Reads trial artifacts and recomputes canary Q; no cluster access, fitting, or exclusions.
"""Compare matched-request physical trials without counting nodes as deployments."""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile

from analyzeRoutingCanary import compareWindows
from executePlacementTrial import auditResourceBudget, readPodIdentities
from solvePlacement import PlacementModel


def hashFile(path):
    """Hash exact artifact bytes at `path`."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def analyzeTrial(path, method, plan):
    """Audit `path` for expected `method` under the frozen round `plan`."""
    if not (path/'complete.json').exists():
        stages=json.loads((path/'stages.json').read_text()) if (path/'stages.json').exists() else []
        return dict(method=method,status='incomplete',lastStage=stages[-1] if stages else None,
                    independentCompletedDeployments=0,qualityValid=None,minimumQ=None)
    read=lambda name:json.loads((path/name).read_text())
    complete=read('complete.json');prepared=read('prepared-audit.json');stages=read('stages.json')
    if complete['comparisonMethod']!=method or complete['eta']!=plan['eta'] or complete['independentDeploymentCount']!=1:
        raise ValueError('trial method/eta/replication mismatch')
    if prepared['instanceSha256']!=plan['instanceSha256'] or hashFile(path/'instance-original.json')!=plan['instanceSha256']:
        raise ValueError('trial budget/model instance differs from plan')
    if prepared['deploymentSha256']!=plan['manifests'][method]['sha256']:
        raise ValueError('trial manifest differs from plan')
    expected={'deploy','start-bird','verify-start-bird','all-protocols','verify-fib',
              'routing-resources','post-canary-protocols','canary-empty-before','canary-loaded','canary-empty-after','remove-owned-namespace'}
    recovery=read('instrumentation-recovery.json') if (path/'instrumentation-recovery.json').exists() else None
    failures=[i for i,s in enumerate(stages) if s['status']!='completed']
    if recovery:
        allowed=recovery['failedStageIndex']
        if recovery.get('status')!='completed' or failures!=[allowed] or stages[allowed]!=recovery['failedStage']:
            raise ValueError('unresolved or altered instrumentation failure')
        if stages[allowed]['stage']!='routing-resources' or not any(s['stage']=='routing-resources' and s['status']=='completed' for s in stages[allowed+1:]):
            raise ValueError('failed resource stage lacks completed recovery')
        failures=[]
    if not expected<={s['stage'] for s in stages if s['status']=='completed'} or failures:
        raise ValueError('completed trial lacks successful required stages')
    mapping=read('deployment/mapping.json');live=read('instance-live.json');model=PlacementModel(live)
    initial=read('deployment/pods-ready.json')['items'];final=read('pods-final.json')
    if readPodIdentities(initial)!=readPodIdentities(final):raise ValueError('trial workload identity changed')
    if {p['metadata']['labels']['app']:p['spec']['nodeName'] for p in final}!=mapping or len(mapping)!=214:
        raise ValueError('actual full-topology mapping changed')
    cost=model.calculateCost(mapping);hard=model.auditMapping(mapping,enforceQuality=False)['feasible']
    if abs(cost-complete['cost'])>1e-7 or hard!=complete['hardConstraintsSatisfied']:
        raise ValueError('trial objective/hard constraint audit mismatch')
    resource=auditResourceBudget(live,mapping,read('routing-resource-analysis/summary.json'))
    if resource!=read('resource-budget-audit.json'):raise ValueError('resource audit mismatch')
    quality={}
    with tempfile.TemporaryDirectory() as temp:
        for profile in ['c2m2','c4m2','c4m4']:
            batches=[path/(path.name+'-'+profile+'-'+phase) for phase in ['empty-before','loaded','empty-after']]
            result=Path(temp)/(profile+'.json')
            compareWindows(*batches,profile,result,plan['eta'])
            recomputed=json.loads(result.read_text())
            if recomputed!=read(profile+'-q.json'):raise ValueError('Q report differs from raw recomputation')
            quality[profile]=recomputed
    valid=all(q['valid'] for q in quality.values())
    violation=any(q['valid'] and q['observationsBelowEta']>0 for q in quality.values())
    return dict(method=method,status='completed',independentCompletedDeployments=1,cost=cost,
        modelAdmitted=complete['modelAdmitted'],hardConstraintsSatisfied=hard,
        uninterruptedProtocol=recovery is None,instrumentationRecovery=recovery,
        resourceBudgetExceeded=any(n[r]['exceedsPlanned'] for n in resource['nodes'] for r in ['cpu','memory']),
        qualityValid=valid,minimumQ=min(q['minimumQ'] for q in quality.values()) if valid else None,
        deploymentQualityFailure=True if violation else (False if valid else None),
        nodeQuality=quality,resourceAudit=resource,
        artifactHashes={name:hashFile(path/name) for name in ['complete.json','stages.json','instance-live.json','prepared-audit.json','pods-final.json']})


def analyzeComparisons(planPath, trials, output):
    """Read ordered trial paths, preserve incomplete entries, and write a descriptive round report."""
    plan=json.loads(planPath.read_text())
    if len(trials)!=len(plan['order']) or len({p.resolve() for p in trials})!=len(trials):
        raise ValueError('one distinct trial directory per predeclared method required, in planned order')
    rows=[analyzeTrial(path,method,plan) for path,method in zip(trials,plan['order'])]
    result=dict(planSha256=hashFile(planPath),trials=[str(p) for p in trials],rows=rows,
        allTrialsCompleted=all(r['status']=='completed' for r in rows),
        independentCompletedDeployments=sum(r['independentCompletedDeployments'] for r in rows),
        deploymentRiskCertified=False,
        note='Each method has one deployment in this round. Three nodes and 15 windows per node do not add independent deployments. No significance or deployment-risk guarantee. Invalid Q remains unknown; observations are not silently excluded.')
    output.mkdir(parents=True,exist_ok=False)
    (output/'summary.json').write_text(json.dumps(result,indent=2)+'\n')
    lines=['# Matched-request comparison round','', '| Method | Status | Cost | Valid Q | Min Q | Resource budget exceeded |',
           '|---|---|---:|---|---:|---|']
    for r in rows:lines.append('| '+' | '.join(str(r.get(k,'—')) for k in ['method','status','cost','qualityValid','minimumQ','resourceBudgetExceeded'])+' |')
    lines+=['',result['note']]
    (output/'summary.md').write_text('\n'.join(lines)+'\n')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--plan',type=Path,required=True);p.add_argument('--trials',type=Path,nargs='+',required=True)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();analyzeComparisons(a.plan,a.trials,a.output)
