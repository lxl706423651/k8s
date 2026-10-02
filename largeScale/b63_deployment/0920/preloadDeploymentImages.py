#!/usr/bin/env python3
# Guest-only pre-pull into CRI on owned deployment workers, after pressure sampling.
# Reads verified registry image inventory; writes one log per image and terminal record.
"""Populate worker CRI caches with exact deployment image digests."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import socket
import subprocess


def pullImage(item,output):
    """Pull one verified `item` by immutable digest and preserve command evidence."""
    if not item['configAndLayersMatch'] or not item['destination'].startswith('10.97.0.10:5097/'):
        raise ValueError('unexpected registry image')
    pinned=item['destination'].rsplit(':',1)[0]+'@'+item['registryManifestDigest']
    name=item['destination'].rsplit('/',1)[1].replace(':','_')
    with (output/(name+'.log')).open('x') as handle:
        subprocess.run(['k3s','crictl','pull',pinned],stdout=handle,stderr=subprocess.STDOUT,check=True)
    raw=subprocess.check_output(['k3s','crictl','inspecti',pinned],text=True)
    status=json.loads(raw)['status']
    if pinned not in status.get('repoDigests',[]):raise ValueError('CRI did not record expected digest reference')
    return dict(image=pinned,id=status['id'],size=status.get('size'))


def executeCli():
    """Require verified image inventory and exclusive output on a deployment worker."""
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if socket.gethostname() not in ['placement-deploy0920-'+n for n in ['c2m2','c4m2','c4m4']]:
        raise ValueError('dedicated deployment worker required')
    args.output.mkdir(parents=True,exist_ok=False)
    images=json.loads(args.inventory.read_text())
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(pullImage,item,args.output) for item in images]
        records=[future.result() for future in futures]
    (args.output/'complete.json').write_text(json.dumps(dict(node=socket.gethostname(),images=records),indent=2)+'\n')
    print('COMPLETE',len(records),flush=True)


if __name__=='__main__':executeCli()
