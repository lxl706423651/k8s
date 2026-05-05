#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from _seed_runtime_12node import bootstrap

bootstrap()

SCRIPT_DIR = Path(__file__).resolve().parent
LEGACY_SCRIPT = SCRIPT_DIR / "09_reconvegence.py"

spec = importlib.util.spec_from_file_location("seed_reconvegence_legacy", LEGACY_SCRIPT)
if spec is None or spec.loader is None:
    raise SystemExit(f"Unable to load {LEGACY_SCRIPT}")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

if __name__ == "__main__":
    raise SystemExit(module.main())
