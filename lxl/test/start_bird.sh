#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

setup_experiment_context "${1:-}"
begin_stage_logging "start_bird"
source "${SCRIPT_DIR}/config/runtime.sh"
ensure_kubeconfig

echo "Starting BIRD on router-like pods..."
print_test_context

kubectl get namespace "${SEED_NAMESPACE}" >/dev/null 2>&1 || {
    echo "Namespace ${SEED_NAMESPACE} does not exist." >&2
    exit 1
}

kubectl -n "${SEED_NAMESPACE}" get pods --no-headers >/dev/null 2>&1 || {
    echo "Unable to list pods in namespace ${SEED_NAMESPACE}." >&2
    exit 1
}

python3 "${SCRIPT_DIR}/start_bird_helper.py" "${SEED_NAMESPACE}" "${EXPERIMENT_DIR}"

echo "Start BIRD completed."
