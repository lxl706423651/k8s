#!/usr/bin/env bash
# Start BIRD inside all router-like b63 workload pods.
#
# Inputs: experiment directory and an already-ready SeedEMU namespace.
# Outputs: start_bird.log, start_bird_targets.json, start_bird_summary.json.
# Side effects: kubectl exec starts BIRD processes inside workload pods.
# Context: run after wait-ready.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

KUBECTL_EXEC_TIMEOUT_SECONDS=30
BIRD_START_EXEC_TIMEOUT_SECONDS=45
BIRD_START_RETRIES=2
BIRD_START_RETRY_BACKOFF_SECONDS=1
BIRD_LOAD_THRESHOLD=40
BIRD_LOAD_CHECK_INTERVAL_SECONDS=20
BIRD_LOAD_WAIT_TIMEOUT_SECONDS=600
BIRD_POST_START_SETTLE_SECONDS=60

setup_experiment_context "$@"
begin_stage_logging "start_bird"
ensure_kubeconfig
KUBECONFIG_PATH="${SEED_KUBECONFIG_PATH}"

echo "Starting BIRD on router-like pods..."
print_test_context

kubectl --kubeconfig "${KUBECONFIG_PATH}" get namespace "${SEED_NAMESPACE}" >/dev/null 2>&1 || {
    echo "Namespace ${SEED_NAMESPACE} does not exist." >&2
    exit 1
}

kubectl --kubeconfig "${KUBECONFIG_PATH}" -n "${SEED_NAMESPACE}" get pods --no-headers >/dev/null 2>&1 || {
    echo "Unable to list pods in namespace ${SEED_NAMESPACE}." >&2
    exit 1
}

python3 "${SCRIPT_DIR}/start_bird_helper.py" \
    --namespace "${SEED_NAMESPACE}" \
    --artifact-dir "${EXPERIMENT_DIR}" \
    --kubeconfig "${KUBECONFIG_PATH}" \
    --kubectl-exec-timeout-seconds "${KUBECTL_EXEC_TIMEOUT_SECONDS}" \
    --start-exec-timeout-seconds "${BIRD_START_EXEC_TIMEOUT_SECONDS}" \
    --retries "${BIRD_START_RETRIES}" \
    --retry-backoff-seconds "${BIRD_START_RETRY_BACKOFF_SECONDS}" \
    --load-threshold "${BIRD_LOAD_THRESHOLD}" \
    --load-check-interval-seconds "${BIRD_LOAD_CHECK_INTERVAL_SECONDS}" \
    --load-wait-timeout-seconds "${BIRD_LOAD_WAIT_TIMEOUT_SECONDS}" \
    --post-start-settle-seconds "${BIRD_POST_START_SETTLE_SECONDS}"

echo "Start BIRD completed."
