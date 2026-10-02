#!/usr/bin/env python3
# Explicit second-generation lower-envelope model; retains first-generation failures.
# Fits training only, freezes before fresh calibration; no claim of mean-Q accuracy.
"""Build a monotone empirical lower envelope for restricted-domain admission research."""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np

from fitResponseSurface import selectValid


def fitEnvelope(raw,output):
    """Freeze the coordinatewise monotone minimum of repeated training observations."""
    rows,audit=selectValid(raw)
    cpu=sorted({r['cpuFraction'] for r in rows});memory=sorted({r['memoryFraction'] for r in rows})
    cells={(x,y):[r['q'] for r in rows if r['cpuFraction']==x and r['memoryFraction']==y] for x in cpu for y in memory}
    if any(len(v)<5 for v in cells.values()):raise ValueError('require five observations at each grid node')
    q=np.array([[min(cells[(x,y)]) for y in memory] for x in cpu])
    # Greatest nonincreasing grid lying no higher than every observed grid minimum.
    q=np.minimum.accumulate(np.minimum.accumulate(q,axis=0),axis=1)
    surface=dict(cpu=cpu,memory=memory,q=q.tolist(),margin=0.,interpolation='upper_corner')
    bundle=dict(modelKind='empirical_lower_envelope_v2',surface=surface,trainingAudit=audit,
        sourceSha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),calibrated=False,
        coverageCertified=False,scope='fixed canary and pressure protocol within observed low-load grid',
        note='Lower response envelope, not a conditional mean. Fresh calibration and test required. Original failures retained.')
    with output.open('x') as handle:json.dump(bundle,handle,indent=2);handle.write('\n')
    print(json.dumps(dict(cpu=cpu,memory=memory,q=q.tolist()),indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--raw',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    a=parser.parse_args();fitEnvelope(a.raw,a.output)
