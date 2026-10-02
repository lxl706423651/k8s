#!/usr/bin/env python3
# Host-only sequential baseline -> parallel transfer diagnostics on the new cluster.
# Requires externally verified readiness; exclusive outputs prevent accidental reruns.
"""Collect idle-node baselines, then test predefined pressure points on K3s workers."""
import argparse
import json
import re
from pathlib import Path
import subprocess
import sys
import time


def executePipeline(suffix=""):
    """Use fixed deployment configs; archive handles and never restart failed stages."""
    if suffix and not re.fullmatch(r"[a-z0-9-]+",suffix):raise ValueError("invalid batch suffix")
    tail="-"+suffix if suffix else ""
    root=Path('results')
    print('START idle baseline',flush=True)
    with (root/('k3s-node-baseline'+tail+'.log')).open('x') as log:
        subprocess.run([sys.executable,'executeNodeBaseline.py','--config','configs/deploymentCluster.yaml',
                        '--output',str(root/('k3s-node-baseline'+tail)),'--seconds','180'],stdout=log,stderr=subprocess.STDOUT,check=True)
    print('START transfer diagnostics',flush=True)
    handles=[]
    processes=[]
    logs=[]
    try:
        for profile in ['c2m2','c4m2','c4m4']:
            command=[sys.executable,'executeCalibration.py','--config',f'configs/{profile}-k3s-transfer.yaml',
                     '--output',str(root/(profile+'-k3s-transfer'+tail))]
            log=(root/(profile+'-k3s-transfer'+tail+'.log')).open('x');logs.append(log)
            process=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT);processes.append(process)
            handles.append(dict(profile=profile,pid=process.pid,command=command,started=time.time()))
        (root/('k3s-transfer-handles'+tail+'.json')).write_text(json.dumps(handles,indent=2)+'\n')
        codes=[p.wait() for p in processes]
        if any(codes):raise RuntimeError('transfer diagnostic failed: '+str(codes))
    finally:
        for log in logs:log.close()
    print('COMPLETE K3s transfer collection (analysis still required)',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suffix',default='')
    executePipeline(parser.parse_args().suffix)
