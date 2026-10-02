#!/usr/bin/env python3
# Offline preparation of the real 214-Pod source using verified registry digests.
# Changes only namespace/image identity; final placement still requires solver export.
"""Prepare immutable image references and an isolated namespace for actual deployment."""
import argparse
import copy
import hashlib
import json
from pathlib import Path

import yaml


def prepareSource(source,registryAudit,contextAudit,output):
    """Join `source` with image/context audits and write new, unplaced `output`."""
    registry=json.loads(registryAudit.read_text());context=json.loads(contextAudit.read_text())
    if context['failures']:raise ValueError('image contexts did not match')
    checked={a['image']:a['manifestDigest'] for a in context['audits']}
    refs={}
    for item in registry:
        if not item['configAndLayersMatch'] or checked.get(item['source'])!=item['sourceManifestDigest']:
            raise ValueError('registry/context evidence differs')
        name=item['source'].rsplit('/',1)[1]
        if name in refs:raise ValueError('ambiguous image basename')
        refs[name]=item['destination'].rsplit(':',1)[0]+'@'+item['registryManifestDigest']
    original=[d for d in yaml.safe_load_all(source.read_text()) if d]
    prepared=copy.deepcopy(original);changes=[];namespace='seedemu-placement0920'
    for doc in prepared:
        if doc['kind'] not in ('Deployment','NetworkAttachmentDefinition'):
            raise ValueError('unsupported source kind')
        oldNamespace=doc['metadata']['namespace'];doc['metadata']['namespace']=namespace
        if doc['kind']!='Deployment':continue
        spec=doc['spec']['template']['spec']
        annotations=doc['spec']['template']['metadata'].get('annotations',{})
        attachments=json.loads(annotations.get('k8s.v1.cni.cncf.io/networks','[]'))
        if any(a.get('namespace',oldNamespace)!=oldNamespace for a in attachments):
            raise ValueError('cross-namespace attachment requires explicit modeling')
        # This compiler uses unqualified attachment names. Refuse other variants.
        if any('namespace' in a for a in attachments):raise ValueError('explicit attachment namespace unsupported')
        for container in spec['containers']:
            old=container['image'];name=old.rsplit('/',1)[1]
            if name not in refs:raise ValueError('source image missing from verified registry')
            container['image']=refs[name]
            changes.append(dict(pod=doc['metadata']['name'],old=old,new=container['image']))
    # Reverse the two allowed changes and compare the full parsed manifests.
    for before,after in zip(original,prepared):
        restored=copy.deepcopy(after);restored['metadata']['namespace']=before['metadata']['namespace']
        if before['kind']=='Deployment':
            for a,b in zip(restored['spec']['template']['spec']['containers'],before['spec']['template']['spec']['containers']):a['image']=b['image']
        if restored!=before:raise ValueError('unexpected source mutation')
    output.mkdir(parents=True,exist_ok=False)
    namespaceDoc=dict(apiVersion='v1',kind='Namespace',metadata=dict(name=namespace,labels={'experiment':'placement0920'}))
    (output/'source.yaml').write_text(yaml.safe_dump_all([namespaceDoc,*prepared],sort_keys=False))
    audit=dict(status='prepared_not_placed_or_deployed',sourceSha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        registryAuditSha256=hashlib.sha256(registryAudit.read_bytes()).hexdigest(),
        contextAuditSha256=hashlib.sha256(contextAudit.read_bytes()).hexdigest(),namespace=namespace,
        workloads=len(changes),networks=sum(d['kind']=='NetworkAttachmentDefinition' for d in prepared),
        preservedAllOtherFields=True,images=changes,
        note='Old nodeSelector remains until audited solver export. Do not apply this intermediate source.')
    (output/'audit.json').write_text(json.dumps(audit,indent=2)+'\n')
    print(json.dumps({k:audit[k] for k in ('status','workloads','networks','preservedAllOtherFields')}))


def executeCli():
    """Require source YAML, two successful image audits and a new output directory."""
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ['source','registry-audit','context-audit','output']:parser.add_argument('--'+key,type=Path,required=True)
    args=parser.parse_args();prepareSource(args.source,args.registry_audit,args.context_audit,args.output)


if __name__=='__main__':executeCli()
