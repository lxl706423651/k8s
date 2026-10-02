#!/usr/bin/env python3
# Apply only the prepared unadmitted routing diagnostic to the isolated new cluster.
# Creates its namespace/NADs/Deployments in small ready-gated batches; never deletes/retries.
"""Deploy the actual topology for resource calibration, retaining every apply log."""
import argparse
import json
from pathlib import Path
import subprocess

import yaml


def executeKubectl(kubeconfig,arguments,log,inputText=None):
    """Run explicit kubectl `arguments`, saving stdout/stderr in new `log`."""
    with log.open('x') as handle:
        result=subprocess.run(['kubectl','--kubeconfig',str(kubeconfig),*arguments],input=inputText,
                              text=True,stdout=handle,stderr=subprocess.STDOUT)
    if result.returncode:raise RuntimeError('kubectl failed: '+str(log))


def deployDiagnostic(prepared,kubeconfig,output):
    """Deploy prepared diagnostic in `prepared`; preserve all artifacts under new `output`."""
    audit=json.loads((prepared/'diagnostic.json').read_text())
    if audit['evidenceKind']!='routing_resource_calibration_not_model_admitted' or audit['qualityGuarantee']:
        raise ValueError('this runner only handles explicitly unadmitted diagnostic data')
    cluster=yaml.safe_load(kubeconfig.read_text())
    if {c['cluster']['server'] for c in cluster['clusters']}!={'https://10.97.0.10:6443'}:
        raise ValueError('wrong target cluster')
    namespace='seedemu-placement0920'
    existing=subprocess.check_output(['kubectl','--kubeconfig',str(kubeconfig),'get','namespace',namespace,'--ignore-not-found','-o','name'],text=True)
    if existing.strip():raise ValueError('namespace exists; inspect current deployment instead of restarting')
    output.mkdir(parents=True,exist_ok=False)
    docs=[d for d in yaml.safe_load_all((prepared/'deployment.yaml').read_text()) if d]
    infrastructure=[d for d in docs if d['kind']!='Deployment'];workloads=[d for d in docs if d['kind']=='Deployment']
    executeKubectl(kubeconfig,['apply','-f','-'],output/'apply-infrastructure.log',yaml.safe_dump_all(infrastructure))
    for start in range(0,len(workloads),12):
        batch=workloads[start:start+12];label=f'batch-{start//12:03d}'
        executeKubectl(kubeconfig,['apply','-f','-'],output/(label+'-apply.log'),yaml.safe_dump_all(batch))
        executeKubectl(kubeconfig,['-n',namespace,'wait','--for=condition=Available','--timeout=180s',
                       *['deployment/'+d['metadata']['name'] for d in batch]],output/(label+'-ready.log'))
        print('READY',min(start+12,len(workloads)),len(workloads),flush=True)
    raw=subprocess.check_output(['kubectl','--kubeconfig',str(kubeconfig),'-n',namespace,'get','pods','-o','json'],text=True)
    (output/'pods-ready.json').write_text(raw)
    pods=json.loads(raw)['items'];actual={}
    dynamic=audit.get('mappingPolicy')=='kubernetes_default_scheduler'
    mapping=None if dynamic else json.loads((prepared/'mapping.json').read_text())
    for pod in pods:
        app=pod['metadata'].get('labels',{}).get('app')
        if app in actual:raise ValueError('duplicate workload Pod')
        actual[app]=pod['spec']['nodeName']
        if not any(c['type']=='Ready' and c['status']=='True' for c in pod['status'].get('conditions',[])):
            raise ValueError('Pod lost Ready after batches')
    if dynamic:
        if set(actual)!={d['metadata']['name'] for d in workloads} or not set(actual.values())<=set(audit['allowedNodes']):
            raise ValueError('scheduler mapping outside workload/worker contract')
    elif actual!=mapping:raise ValueError('actual mapping differs from frozen diagnostic mapping')
    (output/'mapping.json').write_text(json.dumps(actual,indent=2)+'\n')
    (output/'deployment-complete.json').write_text(json.dumps(dict(pods=len(pods),mappingMatches=None if dynamic else True,
        mappingMode='observed_default_scheduler' if dynamic else 'verified_frozen_mapping',
        evidenceKind=audit['evidenceKind'],note='Kubernetes Ready only; BIRD/FIB and resource measurement remain required.'),indent=2)+'\n')
    print('COMPLETE deployment; BIRD/FIB gates not yet run',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ['prepared','kubeconfig','output']:parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args();deployDiagnostic(args.prepared,args.kubeconfig,args.output)
