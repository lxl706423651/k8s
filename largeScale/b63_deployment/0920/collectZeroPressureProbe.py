#!/usr/bin/env python3
# Supplemental zero-injection diagnostic only; imports the exact archived canary function.
# Requires 64MiB available for this fixed ~1MiB-data probe, not the injection guard's 20%.
"""Observe a fixed canary where the generic pressure allocator refused a zero allocation."""
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0,'/home/ubuntu/placement-c4m2-default-routing')
import measurePressure


def collectProbe():
    """Collect three bounded windows without pressure workers or allocation beyond the canary."""
    source=Path(measurePressure.__file__)
    print(json.dumps(dict(kind='metadata',scope='supplemental_zero_pressure_guard_diagnostic',
        sourceSha256=hashlib.sha256(source.read_bytes()).hexdigest(),minimumAvailableBytes=64*1024**2,
        note='Not a replacement for the rejected original 15-point window.')),flush=True)
    for index in range(3):
        before=measurePressure.readHostMetrics()
        if before['memory']['MemAvailable']<64*1024**2:raise RuntimeError('insufficient fixed-probe headroom')
        time.sleep(2)
        canary=measurePressure.measureCanary(10)
        print(json.dumps(dict(kind='probe',index=index,timestamp=time.time(),before=before,
            after=measurePressure.readHostMetrics(),canary=canary)),flush=True)
    print(json.dumps(dict(kind='complete',timestamp=time.time())),flush=True)


if __name__=='__main__':collectProbe()
