#!/usr/bin/env python3
# Offline check of archived image COPY payloads against actual compiled build contexts.
# Requires OCI archive/provenance/source directory; writes content audit, never extracts files.
"""Bind the deployed image contents to the topology configurations used for features."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import shlex
import tarfile


def verifyContexts(archive,provenance,source,output):
    """Compare each final-layer COPY in verified `archive` with `source` build files."""
    inventory=json.loads(provenance.read_text())
    if hashlib.sha256(archive.read_bytes()).hexdigest()!=inventory['archiveSha256']:
        raise ValueError('archive differs from verified provenance')
    audits=[]
    with tarfile.open(archive) as outer:
        for image in inventory['images']:
            name=image['reference'].rsplit('/',1)[1].split(':',1)[0]
            context=source/name;dockerfile=context/'Dockerfile'
            copies=[]
            for line in dockerfile.read_text().splitlines():
                tokens=shlex.split(line)
                if tokens and tokens[0]=='COPY':
                    if len(tokens)!=3 or tokens[1].startswith('--'):raise ValueError('unsupported COPY form')
                    local=(context/tokens[1]).resolve()
                    if not local.is_relative_to(context.resolve()) or not local.is_file():raise ValueError('invalid COPY source')
                    copies.append((tokens[2].lstrip('/'),local))
            if not copies:raise ValueError('context has no COPY inputs')
            blob=outer.extractfile('blobs/sha256/'+image['layers'][-1].split(':')[1]).read()
            files=[]
            with tarfile.open(fileobj=io.BytesIO(blob),mode='r:*') as layer:
                # Compiler packs all node-specific COPY inputs into its final layer.
                # Refuse other layouts rather than accidentally compare stale lower layers.
                for destination,local in copies:
                    member=layer.getmember(destination)
                    if not member.isfile():raise ValueError('COPY destination is not a regular final-layer file')
                    actual=hashlib.sha256(layer.extractfile(member).read()).hexdigest()
                    expected=hashlib.sha256(local.read_bytes()).hexdigest()
                    files.append(dict(destination='/'+destination,expectedSha256=expected,actualSha256=actual,match=actual==expected))
            audits.append(dict(image=image['reference'],manifestDigest=image['manifestDigest'],
                               dockerfileSha256=hashlib.sha256(dockerfile.read_bytes()).hexdigest(),files=files))
    failures=[dict(image=a['image'],file=f) for a in audits for f in a['files'] if not f['match']]
    result=dict(archiveSha256=inventory['archiveSha256'],images=len(audits),
                checkedFiles=sum(len(a['files']) for a in audits),failures=failures,audits=audits,
                scope='Exact COPY file contents only; not runtime routes, permissions, startup success or performance.')
    output.write_text(json.dumps(result,indent=2)+'\n')
    if failures:raise ValueError('image/source COPY mismatches; see '+str(output))
    print(json.dumps({k:result[k] for k in ('images','checkedFiles','failures')}))


def executeCli():
    """Require archive, provenance, compiler source and output audit paths."""
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ['archive','provenance','source','output']:parser.add_argument('--'+key,type=Path,required=True)
    args=parser.parse_args();verifyContexts(args.archive,args.provenance,args.source,args.output)


if __name__=='__main__':executeCli()
