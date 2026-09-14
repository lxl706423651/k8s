#!/usr/bin/env python3
"""Collect host performance metrics with one schema for full-flow comparisons.

This file is intentionally identical to the native runtime implementation at
the time of the benchmark so both coordinators emit the same CSV/JSON schema.
"""

from pathlib import Path
import runpy


_implementation = Path(__file__).resolve().parents[3] / "multihost" / "performanceMetrics.py"
if not _implementation.is_file():
    raise RuntimeError(f"missing shared performance metrics implementation: {_implementation}")
globals().update(runpy.run_path(str(_implementation)))
