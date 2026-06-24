#!/usr/bin/env bash
# Verify BIRD process availability inside router-like b63 workload pods.
#
# Inputs: experiment directory and a namespace where BIRD has been started.
# Outputs: verify_bird.log, verify_bird_targets.json, verify_bird_summary.json.
# Side effects: read-only kubectl exec checks.
# Context: run before the BGP protocol-level test.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

setup_experiment_context "$@"
begin_stage_logging "verify_bird"
ensure_kubeconfig

echo "Verifying BIRD across router-like pods..."
print_test_context

kubectl get namespace "${SEED_NAMESPACE}" >/dev/null 2>&1 || {
    echo "Namespace ${SEED_NAMESPACE} does not exist." >&2
    exit 1
}

python3 "${SCRIPT_DIR}/verify_bird_helper.py" \
    --namespace "${SEED_NAMESPACE}" \
    --artifact-dir "${EXPERIMENT_DIR}" \
    --kubeconfig "${KUBECONFIG}" \
    --kubectl-exec-timeout-seconds "${SEED_KUBECTL_EXEC_TIMEOUT_SECONDS}" \
    --verify-timeout-seconds "${SEED_BIRD_VERIFY_TIMEOUT_SECONDS}" \
    --verify-retry-interval-seconds "${SEED_BIRD_VERIFY_RETRY_INTERVAL_SECONDS}"

echo "Verify BIRD completed."
