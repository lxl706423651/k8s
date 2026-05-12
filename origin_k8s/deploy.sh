#!/usr/bin/env bash
set -euo pipefail

OUTPUT_DIR="${1:-/home/lxl/k8s/origin_k8s/emulate/output}"
KUBECONFIG_PATH="${2:-/home/lxl/k8s/output/kubeconfigs/seedemu-k3s.yaml}"
REGISTRY_PREFIX="${3:-192.168.122.110:5000}"
MAKEFILE="/home/lxl/k8s/origin_k8s/running/Makefile"

[ -f "${MAKEFILE}" ] || {
    echo "Missing runtime Makefile: ${MAKEFILE}" >&2
    exit 1
}

exec make -C "$(dirname "${MAKEFILE}")" up OUTPUT_DIR="${OUTPUT_DIR}" KUBECONFIG="${KUBECONFIG_PATH}" REGISTRY_PREFIX="${REGISTRY_PREFIX}"
