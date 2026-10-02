#!/usr/bin/env python3
# Run only on the owned deployment master after loading the preserved OCI archive.
# Retags/pushes 214 images to its private registry; verifies config and layer digests.
"""Mirror archived topology images into the isolated experiment registry."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import socket
import subprocess
import urllib.request


def mirrorImage(image,output):
    """Push one archived `image`; verify registry manifest content in `output`."""
    source=image['reference']
    if not source.startswith(('10.92.0.10:5092/brdnode_','10.92.0.10:5092/rs_')):raise ValueError('unexpected source image')
    name=source.split('/',1)[1];destination='10.97.0.10:5097/'+name
    subprocess.run(['docker','tag',source,destination],check=True)
    with (output/(name.replace(':','_')+'.log')).open('x') as handle:
        subprocess.run(['docker','push',destination],stdout=handle,stderr=subprocess.STDOUT,check=True)
    repository,tag=name.rsplit(':',1)
    request=urllib.request.Request(f'http://10.97.0.10:5097/v2/{repository}/manifests/{tag}',
        headers={'Accept':'application/vnd.docker.distribution.manifest.v2+json, application/vnd.oci.image.manifest.v1+json'})
    with urllib.request.urlopen(request,timeout=30) as response:
        manifest=json.load(response);digest=response.headers.get('Docker-Content-Digest')
    if manifest['config']['digest']!=image['configDigest'] or [x['digest'] for x in manifest['layers']]!=image['layers']:
        raise ValueError('pushed image content differs from archived manifest')
    result=dict(source=source,destination=destination,sourceManifestDigest=image['manifestDigest'],
                registryManifestDigest=digest,configAndLayersMatch=True)
    print(json.dumps(result),flush=True)
    return result


def executeCli():
    """Read checked inventory and create exclusive registry-verification output."""
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if socket.gethostname()!='placement-deploy0920-master':raise ValueError('owned deployment master required')
    inventory=json.loads(args.inventory.read_text())
    if not inventory['allBlobHashesVerified']:raise ValueError('archive was not verified')
    args.output.mkdir(parents=True,exist_ok=False)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures=[pool.submit(mirrorImage,i,args.output) for i in inventory['images']]
        results=[future.result() for future in futures]
    (args.output/'verified-images.json').write_text(json.dumps(results,indent=2)+'\n')


if __name__=='__main__':executeCli()
