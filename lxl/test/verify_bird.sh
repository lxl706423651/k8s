#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

setup_experiment_context "${1:-}"
begin_stage_logging "verify_bird"
source "${SCRIPT_DIR}/config/runtime.sh"
ensure_kubeconfig

echo "Verifying BIRD across router-like pods..."
print_test_context

kubectl get namespace "${SEED_NAMESPACE}" >/dev/null 2>&1 || {
    echo "Namespace ${SEED_NAMESPACE} does not exist." >&2
    exit 1
}

python3 "${SCRIPT_DIR}/verify_bird_helper.py" "${SEED_NAMESPACE}" "${EXPERIMENT_DIR}" "bird"

echo "Verify BIRD completed."
