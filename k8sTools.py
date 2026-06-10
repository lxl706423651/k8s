#!/usr/bin/env python3
"""Run the local SeedEMU Kubernetes workflow through seedemu.k8sTools.

This is a thin repository-local entrypoint. The reusable implementation lives
in seedemu.k8sTools.K8sTools.
"""
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent
repo_root = str(REPO_ROOT)
if repo_root in sys.path:
    sys.path.remove(repo_root)
sys.path.insert(0, repo_root)

from seedemu.k8sTools import K8sTools


if __name__ == "__main__":
    raise SystemExit(K8sTools().runCli())
