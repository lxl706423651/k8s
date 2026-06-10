#!/usr/bin/env bash
# Verify that BIRD is responding in router-like pods for one run directory.
#
# Inputs: run directory plus assignment.yaml-derived kubeconfig/namespace.
# Outputs: verify_bird.log, verify_bird_targets.json, verify_bird_summary.json.
# Side effects: read-only kubectl exec calls into existing pods.
# Context: run after start_bird.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

setup_experiment_context "${1:-}"
SEED_KUBECTL_EXEC_TIMEOUT_SECONDS=30
SEED_BIRD_VERIFY_LIST_TIMEOUT_SECONDS=300
SEED_BIRD_VERIFY_TIMEOUT_SECONDS=1800
SEED_BIRD_VERIFY_RETRY_INTERVAL_SECONDS=10
export SEED_KUBECTL_EXEC_TIMEOUT_SECONDS SEED_BIRD_VERIFY_LIST_TIMEOUT_SECONDS
export SEED_BIRD_VERIFY_TIMEOUT_SECONDS SEED_BIRD_VERIFY_RETRY_INTERVAL_SECONDS

begin_stage_logging "verify_bird"
ensure_kubeconfig

echo "Verifying BIRD across router-like pods..."
print_test_context

kubectl get namespace "${SEED_NAMESPACE}" >/dev/null 2>&1 || {
    echo "Namespace ${SEED_NAMESPACE} does not exist." >&2
    exit 1
}

python3 "${SCRIPT_DIR}/verify_bird_helper.py" "${SEED_NAMESPACE}" "${EXPERIMENT_DIR}"

echo "Verify BIRD completed."
