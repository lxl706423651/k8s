#!/usr/bin/env python3
# Read-only full-topology BIRD audit through kubectl, after the VM load gate.
# Checks exact configured protocol counts/names and expected OSPF adjacency counts.
"""Verify every compiled router and route server, including nodes without BGP."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import hashlib
import json
from pathlib import Path
import re
import shlex
import sys

from extractTopology import readBlocks


def loadExistingVerifier():
    """Locate and reuse the repository's VM load gate and BIRD output parsers."""
    root=next(p for p in Path(__file__).resolve().parents if (p/'setup.py').exists() and (p/'seedemu').is_dir())
    path=root/'largeScale/b62_k8s_scale/verifyStartBird.py'
    spec=importlib.util.spec_from_file_location('placementBirdVerifier',path)
    module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
    return module


def readExpectedProtocols(feature):
    """Read concrete BIRD sources from the compiled context associated with `feature`."""
    birdSource=Path(feature['birdSource'])
    if hashlib.sha256(birdSource.read_bytes()).hexdigest()!=feature['birdSha256']:
        raise ValueError('BIRD source changed after feature extraction')
    context=birdSource.parent
    sources=[]
    for line in (context/'Dockerfile').read_text().splitlines():
        fields=shlex.split(line)
        if len(fields)==3 and fields[0]=='COPY' and (fields[2]=='/etc/bird/bird.conf' or
                fields[2].startswith('/etc/bird/conf/') and fields[2].endswith('.conf')):
            sources.append((context/fields[1]).read_text())
    counts=Counter();names=set()
    for text in sources:
        for match,_ in readBlocks(text,r'\bprotocol\s+(\w+)(?:\s+(\w+))?\s*\{'):
            counts[match[1].upper()]+=1
            if match[2]:names.add(match[2])
    if not counts:raise ValueError('no expected protocols')
    return dict(counts=dict(counts),named=sorted(names))


def assessExpected(rows,expected,neighbors,ospfCount):
    """Compare parsed `rows` and raw OSPF `neighbors` with compile-time expectations."""
    failures=[]
    counts=dict(Counter(r['proto'].upper() for r in rows))
    if counts!=expected['counts']:failures.append(dict(reason='protocol_count_mismatch',actual=counts,expected=expected['counts']))
    missing=set(expected['named'])-{r['name'] for r in rows}
    if missing:failures.append(dict(reason='named_protocol_missing',names=sorted(missing)))
    for row in rows:
        if row['state'].lower()!='up':failures.append(dict(reason='protocol_not_up',row=row))
        if re.search(r'error|fail|unreachable|no route|refused|timeout|inactive',row['info'],re.IGNORECASE):
            failures.append(dict(reason='protocol_error_info',row=row))
        if row['proto'].upper()=='BGP' and 'established' not in row['info'].lower():
            failures.append(dict(reason='bgp_not_established',row=row))
    full=len(re.findall(r'\bFull(?:/|\b)',neighbors,re.IGNORECASE))
    if full!=ospfCount:failures.append(dict(reason='ospf_full_count_mismatch',actual=full,expected=ospfCount))
    return failures


def verifyPod(pod,feature,expected,args,helpers):
    """Collect and assess one ready `pod`, preserving every command's raw output."""
    raw={};failures=[]
    commands={'protocols':'birdc show protocols','routes':'birdc show route count'}
    if expected['counts'].get('OSPF',0):commands['neighbors']='birdc show ospf neighbors'
    for label,command in commands.items():
        result=helpers.podExec(str(args.kubeconfig),args.namespace,pod['metadata']['name'],command,60)
        raw[label]=dict(returnCode=result.returncode,stdout=result.stdout,stderr=result.stderr)
        if result.returncode:failures.append(dict(reason='command_failed',command=label))
    rows=helpers.parseProtocolRows(raw['protocols']['stdout'])
    failures.extend(assessExpected(rows,expected,raw.get('neighbors',{}).get('stdout',''),feature['nOSPF']))
    routes,networks=helpers.parseRouteCount(raw['routes']['stdout'])
    if routes is None or networks is None:failures.append(dict(reason='route_count_parse_failed'))
    return dict(workload=feature['id'],pod=pod['metadata']['name'],node=pod['spec']['nodeName'],
                expected=expected,raw=raw,networks=networks,routes=routes,failures=failures,success=not failures)


def executeCli():
    """Audit an explicit live namespace against its complete feature inventory."""
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ['features','inventory','kubeconfig','output']:parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--namespace',default='seedemu-placement0920')
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    helpers=loadExistingVerifier();nodes=helpers.loadInventory(args.inventory)
    load,wait,samples=helpers.waitForClusterLoad(nodes,50,10,600)
    features={p['id']:p for p in json.loads(args.features.read_text())['pods']}
    raw=helpers.kubectlListPods(args.namespace,str(args.kubeconfig),60)
    (args.output/'pods.json').write_text(json.dumps(raw,indent=2)+'\n')
    pods={}
    for pod in raw['items']:
        app=pod['metadata'].get('labels',{}).get('app')
        if app not in features or app in pods:raise ValueError('unknown or duplicate workload')
        if not any(c['type']=='Ready' and c['status']=='True' for c in pod['status'].get('conditions',[])):
            raise ValueError('pod not Ready: '+pod['metadata']['name'])
        pods[app]=pod
    if set(pods)!=set(features):raise ValueError('missing topology workloads')
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures=[pool.submit(verifyPod,pods[key],feature,readExpectedProtocols(feature),args,helpers) for key,feature in features.items()]
        results=[future.result() for future in futures]
    summary=dict(success=all(r['success'] for r in results),load1=load,loadWaitSeconds=wait,
                 loadByNode=samples,checked=len(results),failed=sum(not r['success'] for r in results),results=results)
    (args.output/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps({k:summary[k] for k in ['success','checked','failed','load1']}))
    if not summary['success']:raise SystemExit(1)


if __name__=='__main__':executeCli()
