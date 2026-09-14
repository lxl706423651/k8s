#!/usr/bin/env python3
"""Forward the historical helper name to B62's implementation.

Inputs, outputs and side effects follow the B62 helper CLI. Importing also
exposes the shared definitions; no cluster operation runs on import.
"""
from pathlib import Path
import runpy
import sys

_SHARED = Path(__file__).resolve().parents[2] / "b62_k8s_scale" / Path(__file__).name
sys.path.insert(0, str(_SHARED.parent))
globals().update(runpy.run_path(str(_SHARED), run_name=__name__))
