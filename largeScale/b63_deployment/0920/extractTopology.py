#!/usr/bin/env python3
# Extract four PDF features from compiled SEED manifests, Docker COPYs and BIRD config.
# Writes auditable JSON with source hashes; never starts BIRD or modifies deployments.
"""Extract deploy-time topology features without reusing old hand-set demands."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex

import numpy as np
import yaml


def readBlocks(text, pattern):
    """Yield regex-matched headers and balanced brace contents from generated `text`."""
    text=re.sub(r'#[^\n]*','',text)
    for match in re.finditer(pattern,text):
        opening=match.end()-1
        depth=1
        quoted=False
        escaped=False
        for pos in range(opening+1,len(text)):
            char=text[pos]
            if char=='"' and not escaped:
                quoted=not quoted
            if not quoted:
                depth += (char=='{')-(char=='}')
            if depth==0:
                yield match,text[opening+1:pos]
                break
            escaped=(char=='\\' and not escaped)
        else:
            raise ValueError('unclosed BIRD block')


def readBirdFeatures(directory):
    """Resolve Docker COPY to bird.conf in `directory` and count explicit sessions.

    This extractor supports explicit generated sessions/area/interface names only;
    inherited BGP templates and wildcard OSPF interfaces require a richer parser.
    """
    files=[]
    for line in (directory/'Dockerfile').read_text().splitlines():
        fields=shlex.split(line)
        if len(fields)==3 and fields[0]=='COPY' and fields[2]=='/etc/bird/bird.conf':
            files.append(directory/fields[1])
    if len(files)!=1:
        raise ValueError('expected exactly one explicit bird.conf COPY: '+str(directory))
    source=files[0]
    text=source.read_text()
    # Do not silently ignore extra routing protocols in included files.
    for line in (directory/'Dockerfile').read_text().splitlines():
        fields=shlex.split(line)
        if len(fields)==3 and fields[0]=='COPY' and fields[2].startswith('/etc/bird/conf/'):
            extra=(directory/fields[1]).read_text()
            if re.search(r'\bprotocol\s+(bgp|ospf)\b',extra):
                raise ValueError('routing protocol in include requires explicit parsing')
    sessions=[]
    interfaces={}
    for match,body in readBlocks(text,r'\bprotocol\s+(bgp|ospf)\s+(\w+)\s*\{'):
        if match[1]=='bgp':
            local=re.findall(r'\blocal\s+(?:[\d.:]+\s+)?as\s+(\d+)\s*;',body)
            remote=re.findall(r'\bneighbor\s+([\d.:]+)\s+as\s+(\d+)\s*;',body)
            if len(local)!=1 or len(remote)!=1:
                raise ValueError('unsupported BGP session syntax')
            sessions.append(dict(name=match[2],localAs=int(local[0]),peerAs=int(remote[0][1]),
                                 peerIp=remote[0][0],kind='ibgp' if local[0]==remote[0][1] else 'ebgp'))
        else:
            for area,areaBody in readBlocks(body,r'\barea\s+([\d.]+)\s*\{'):
                for interface,interfaceBody in readBlocks(areaBody,r'\binterface\s+"([^"*?]+)"\s*\{'):
                    if not re.search(r'\bstub\s*;',interfaceBody):
                        interfaces[interface[1]]=area[1]
    expected=len(re.findall(r'\bprotocol\s+(?:bgp|ospf)\b',re.sub(r'#[^\n]*','',text)))
    parsed=len(list(readBlocks(text,r'\bprotocol\s+(bgp|ospf)\s+(\w+)\s*\{')))
    if expected!=parsed:
        raise ValueError('unsupported inherited or unnamed routing protocol')
    return dict(sessions=sessions,ospfInterfaces=interfaces,birdSource=str(source),
                birdSha256=hashlib.sha256(source.read_bytes()).hexdigest())


def extractFeatures(root):
    """Extract from compiled output `root`; validate membership and report rank."""
    report=json.loads((root/'placement_report.json').read_text())
    docs=list(yaml.safe_load_all((root/'k8s.source.yaml').read_text()))
    networks=report['networks']
    pods={}
    for doc in docs:
        if not doc or doc.get('kind')!='Deployment':
            continue
        if doc['spec'].get('replicas',1)!=1:
            raise ValueError('only single-replica emulation deployments are supported')
        template=doc['spec']['template']
        labels=template['metadata']['labels']
        name=doc['metadata']['name']
        images=[c['image'] for c in template['spec']['containers']]
        if len(images)!=1:
            raise ValueError('resource attribution for sidecars requires explicit treatment')
        context=images[0].rsplit('/',1)[-1].split(':')[0]
        bird=readBirdFeatures(root/context)
        attachments=json.loads(template['metadata'].get('annotations',{}).get('k8s.v1.cni.cncf.io/networks','[]'))
        members=[a['name'] for a in attachments]
        if len(set(members))!=len(members):
            raise ValueError('duplicate attachment')
        for net in members:
            if name not in networks[net]['endpoints']:
                raise ValueError('manifest/report membership mismatch')
        pods[name]=dict(id=name,community=labels.get('seedemu.io/asn'),networks=members,
                       nnet=len(members),niBGP=sum(s['kind']=='ibgp' for s in bird['sessions']),
                       neBGP=sum(s['kind']=='ebgp' for s in bird['sessions']),
                       images=images,**bird)
    for pod in pods.values():
        adjacencies=[]
        for net in pod['networks']:
            edge=networks[net]
            local=edge['logicalName']
            area=pod['ospfInterfaces'].get(local)
            if area is None:
                continue
            for peer in edge['endpoints']:
                if peer!=pod['id'] and peer in pods and pods[peer]['ospfInterfaces'].get(local)==area:
                    adjacencies.append(dict(peer=peer,network=net,area=area))
        pod['nOSPF']=len(adjacencies)
        pod['ospfRelations']=adjacencies
    matrix=np.array([[1]+[p[k] for k in ('nnet','niBGP','neBGP','nOSPF')] for p in pods.values()])
    return dict(evidenceKind='compiled_topology_features_not_resource_measurements',
                sources={f:hashlib.sha256((root/f).read_bytes()).hexdigest() for f in ('placement_report.json','k8s.source.yaml')},
                pods=list(pods.values()),networks=[dict(id=name,members=n['endpoints'],weight=n['weight']) for name,n in networks.items()],
                featureColumns=['intercept','nnet','niBGP','neBGP','nOSPF'],
                designRank=int(np.linalg.matrix_rank(matrix)),designColumns=5,
                distinctFeatureRows=len({tuple(row) for row in matrix}),
                note='Configured OSPF neighbor opportunities, not observed live adjacencies; weights are relative importance.')


def executeCli():
    """Parse compiled source/output paths and write extracted features JSON."""
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('compiled',type=Path)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    result=extractFeatures(args.compiled)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:result[k] for k in ('designRank','designColumns','distinctFeatureRows')}))


if __name__=='__main__':
    executeCli()
