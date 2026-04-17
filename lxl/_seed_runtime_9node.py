#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
ENV_FILE = SCRIPT_DIR / "env_9node.sh"
NODES_HELPER = SCRIPT_DIR / "01_cluster_nodes_9node.sh"

try:
    sys.stdout.reconfigure(line_buffering=True, write_through=True)
    sys.stderr.reconfigure(line_buffering=True, write_through=True)
except Exception:
    pass

os.environ.setdefault("PYTHONUNBUFFERED", "1")


def _load_shell_env(command: str) -> None:
    result = subprocess.run(
        ["bash", "-lc", command],
        check=True,
        text=True,
        capture_output=True,
    )
    for line in result.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key] = value


def load_env_9node() -> None:
    if ENV_FILE.is_file():
        _load_shell_env(f"source {ENV_FILE!s} >/dev/null 2>&1 && env")


def load_cluster_nodes_9node() -> None:
    if not NODES_HELPER.is_file():
        return
    _load_shell_env(
        "set -a && "
        f"source {ENV_FILE!s} >/dev/null 2>&1 && "
        f"source {NODES_HELPER!s} >/dev/null 2>&1 && "
        "seed_load_cluster_nodes && "
        "SEED_NODE_NAMES_JOINED=\"${SEED_NODE_NAMES[*]}\" && "
        "SEED_NODE_IPS_JOINED=\"${SEED_NODE_IPS[*]}\" && "
        "SEED_WORKER_NODE_NAMES_JOINED=\"${SEED_WORKER_NODE_NAMES[*]}\" && "
        "SEED_WORKER_NODE_IPS_JOINED=\"${SEED_WORKER_NODE_IPS[*]}\" && "
        "export SEED_NODE_NAMES_JOINED SEED_NODE_IPS_JOINED "
        "SEED_WORKER_NODE_NAMES_JOINED SEED_WORKER_NODE_IPS_JOINED "
        "SEED_MASTER_NODE_NAME SEED_MASTER_NODE_IP && env"
    )


def bootstrap() -> None:
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    load_env_9node()
    load_cluster_nodes_9node()
