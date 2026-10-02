#!/usr/bin/env python3
# Explicitly recover the zero-workload-node instrumentation failure without redeploying Pods.
# Preserves failed attempt/data, verifies unchanged ownership, then finishes the existing trial.
"""Resume one interrupted network-only trial after the audited empty-worker fix."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

from executePlacementTrial import completeTrial, createTrialSteps, readPodIdentities


def resumeTrial(output, checkpointFix=False):
    """Resume only the known collector failure at `output`, retaining exact deployment identities."""
    root=Path('results');cluster=root/'deployment-cluster';namespace='seedemu-placement0920'
    state=output/'stages.json';records=json.loads(state.read_text())
    receiptPath=output/'instrumentation-recovery.json'
    previous=json.loads(receiptPath.read_text()) if receiptPath.exists() else None
    suffix='-recovery2' if checkpointFix else '-recovery1'
    if checkpointFix:
        errorLog=root/'node-v3-round1-recovery-resume-network-only.log'
        if not previous or previous.get('checkpointFixAttempt') or "NameError: name 'kubeconfig' is not defined" not in errorLog.read_text():
            raise ValueError('not the exact recorded pre-resource checkpoint failure')
        if records[-1]['stage']!='recovery-protocols' or records[-1]['status']!='completed' or (output/(output.name+'-routing-resources-recovery1')).exists():
            raise ValueError('checkpoint advanced beyond the known pre-resource failure')
    else:
        if records[-1]['stage']!='routing-resources' or records[-1]['status']!='failed':
            raise ValueError('only the terminal routing-resource instrumentation failure can be resumed')
        if previous:raise ValueError('recovery already attempted; inspect it')
    if any((output/(p+'-q.json')).exists() for p in ['c2m2','c4m2','c4m4']):raise ValueError('Q already evaluated')
    failed=output/(output.name+'-routing-resources')
    log=failed/'placement-deploy0920-c4m4/ssh.log'
    if 'ValueError: no target routing containers' not in log.read_text():raise ValueError('unexpected failure cause')
    audit=json.loads((output/'prepared-audit.json').read_text())
    if audit['comparisonMethod']!='network_only' or audit['modelAdmitted']:raise ValueError('wrong trial scope')
    mapping=json.loads((output/'deployment/mapping.json').read_text())
    if 'placement-deploy0920-c4m4' in mapping.values():raise ValueError('failed worker was not empty')
    kube=['kubectl','--kubeconfig',str(cluster/'kubeconfig.yaml')]
    pods=json.loads(subprocess.check_output([*kube,'-n',namespace,'get','pods','-o','json'],text=True))['items']
    initial=json.loads((output/'deployment/pods-ready.json').read_text())['items']
    if len(pods)!=214 or readPodIdentities(pods)!=readPodIdentities(initial):raise ValueError('deployment identities changed')
    if {p['metadata']['labels']['app']:p['spec']['nodeName'] for p in pods}!=mapping:raise ValueError('mapping changed')
    sources={}
    archive=output/('recovery-sources-checkpoint-fix' if checkpointFix else 'recovery-sources');archive.mkdir(exist_ok=False)
    for name in ['resumePlacementTrial.py','executePlacementTrial.py','collectRoutingResources.py','analyzePlacementComparisons.py']:
        raw=Path(name).read_bytes();(archive/name).write_bytes(raw);sources[name]=hashlib.sha256(raw).hexdigest()
    (output/('stages-before-checkpoint-fix.json' if checkpointFix else 'stages-before-resource-recovery.json')).write_bytes(state.read_bytes())
    receipt=dict(started=time.time(),reason='explicit zero-workload worker instrumentation repair before any loaded-Q observation',
        failedStageIndex=len(records)-1,failedBatch=str(failed),failedStage=records[-1],sources=sources,
        unchangedDeploymentIdentities=214,newDeploymentCreated=False,qualityOutcomesUsedToChooseRecovery=False,
        note='Interrupted protocol, not a pristine predeclared execution. Failed raw data retained; fresh synchronized resource window on same Pods. Report separately from uninterrupted replications.')
    if checkpointFix:
        receipt=previous
        receipt['checkpointFixAttempt']=dict(started=time.time(),sources=sources,errorLog=str(errorLog),
            errorLogSha256=hashlib.sha256(errorLog.read_bytes()).hexdigest(),newResourceWindowPreviouslyStarted=False)
        receiptPath.write_text(json.dumps(receipt,indent=2)+'\n')
    else:
        with receiptPath.open('x') as f:json.dump(receipt,f,indent=2);f.write('\n')
    run,collect,cooldown=createTrialSteps(output,records,logSuffix=suffix)
    shared=['--namespace',namespace,'--inventory',str(cluster/'inventory.yaml'),'--kubeconfig',str(cluster/'kubeconfig.yaml')]
    run('recovery-protocols',[sys.executable,'verifyTopologyProtocols.py','--features','results/topology214_features.json',*shared,
        '--output',str(output/('recovery-protocols-checkpoint-fix' if checkpointFix else 'recovery-protocols'))])
    before={p:output/(output.name+'-'+p+'-empty-before') for p in ['c2m2','c4m2','c4m4']}
    refreshed=json.loads((output/'instance-live.json').read_text())
    completeTrial(output,audit,refreshed,before,kube,namespace,shared,run,collect,cooldown,
        resourceBatchName=output.name+'-routing-resources-recovery1')
    receipt.update(finished=time.time(),status='completed')
    (output/'instrumentation-recovery.json').write_text(json.dumps(receipt,indent=2)+'\n')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--continue-after-checkpoint-fix',action='store_true')
    a=p.parse_args();resumeTrial(a.output,a.continue_after_checkpoint_fix)
