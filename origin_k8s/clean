#!/usr/bin/env bash
set -euo pipefail

OUTPUT_DIR="${1:-/home/lxl/k8s/origin_k8s/emulate/output}"
KUBECONFIG_PATH="${2:-/home/lxl/k8s/output/kubeconfigs/seedemu-k3s.yaml}"
MAKEFILE="/home/lxl/k8s/origin_k8s/running/Makefile"

exec make -C "$(dirname "${MAKEFILE}")" clean OUTPUT_DIR="${OUTPUT_DIR}" KUBECONFIG="${KUBECONFIG_PATH}"
