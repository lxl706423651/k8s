#!/usr/bin/env bash
#set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/env_9node.sh"

NAMESPACE="${SEED_NAMESPACE:-seedemu-k3s-real-topo}"

if [ -z "${EXPERIMENT_DIR:-}" ]; then
  echo "Error: EXPERIMENT_DIR is not set."
  echo "Example:"
  echo "  export EXPERIMENT_DIR=/home/lxl/k8s/lxl/logs/20260416_xxxxxx_214"
  exit 1
fi

if [ ! -d "${EXPERIMENT_DIR}" ]; then
  mkdir -p "${EXPERIMENT_DIR}"
fi

echo "=== Full Flow (9-node) ==="
echo "EXPERIMENT_DIR=${EXPERIMENT_DIR}"
echo "NAMESPACE=${NAMESPACE}"

run_step() {
  local step_name="$1"
  shift
  echo ""
  echo ">>> ${step_name}"
  "$@"
}

#run_step "03_compile_9node" "${SCRIPT_DIR}/03_compile_9node"
run_step "04_build_9node" "${SCRIPT_DIR}/04_build_9node"
run_step "05_deploy-batched_9node" "${SCRIPT_DIR}/05_deploy-batched_9node"

# ⬇️ 新增的 wait-ready 步骤
run_step "wait-ready" "${SCRIPT_DIR}/wait-ready"

run_step "07_seed_k8s_start_bird0130_9node.py" python3 -u "${SCRIPT_DIR}/07_seed_k8s_start_bird0130_9node.py" "${NAMESPACE}" "${EXPERIMENT_DIR}"
run_step "08_seed_k8s_start_bird_kernel_9node.py" python3 -u "${SCRIPT_DIR}/08_seed_k8s_start_bird_kernel_9node.py" "${NAMESPACE}" "${EXPERIMENT_DIR}"
run_step "09_reconvegence.py" python3 -u "${SCRIPT_DIR}/09_reconvegence.py" "${NAMESPACE}" "${EXPERIMENT_DIR}"

echo ""
echo "Full flow completed successfully."