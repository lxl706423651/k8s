#!/usr/bin/env bash
# Build a K3s cluster from configK3s.yaml. This intentionally refuses
# to infer cluster membership from ambient environment variables.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -s ./configK3s.yaml ]; then
    echo "[K8sPre] Missing configK3s.yaml." >&2
    echo "[K8sPre] Run bash ./installKvmVms.sh first, or provide configK3s.yaml for existing VMs." >&2
    exit 1
fi

echo "[K8sPre] Building Kubernetes/K3s cluster from configK3s.yaml..."
bash ./applyK3sCluster.sh ./configK3s.yaml

echo "[K8sPre] Kubernetes/K3s build finished."
