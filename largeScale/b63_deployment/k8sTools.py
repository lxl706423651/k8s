#!/usr/bin/env python3
"""Run the b63 SeedEMU Kubernetes workflow through seedemu.k8sTools.

This file is intentionally a thin example-local entrypoint. The reusable
implementation lives in seedemu.k8sTools.K8sTools.
"""
from pathlib import Path
import sys


def findRepoRoot(start: Path) -> Path:
    """Return the SeedEMU repository root above start."""
    for candidate in (start, *start.parents):
        if (candidate / "setup.py").is_file() and (candidate / "seedemu").is_dir():
            return candidate
    raise RuntimeError(f"Cannot find SeedEMU repository root above {start}")


REPO_ROOT = findRepoRoot(Path(__file__).resolve())
repo_root = str(REPO_ROOT)
if repo_root in sys.path:
    sys.path.remove(repo_root)
sys.path.insert(0, repo_root)

from seedemu.k8sTools import K8sTools


if __name__ == "__main__":
    raise SystemExit(K8sTools().runCli())
