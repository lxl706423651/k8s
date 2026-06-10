#!/usr/bin/env bash
# Start BIRD inside router-like Kubernetes pods for the active B62 experiment.
# Inputs: optional experiment directory argument resolved by lib.sh.
# Outputs: start_bird.log, start_bird_targets.json, start_bird_summary.json.
# Side effects: checks the target namespace and starts BIRD processes in pods
# through kubectl exec. Nodes are processed concurrently; pods on each node are
# processed serially, matching /home/lxl/k8s/lxl/start-bird behavior.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

KUBECTL_LIST_TIMEOUT_SECONDS=60
BIRD_START_DELAY_SECONDS=0.08
BIRD_LOAD_THRESHOLD=40
BIRD_LOAD_CHECK_INTERVAL_SECONDS=20
BIRD_KUBECTL_EXEC_TIMEOUT_SECONDS=30
BIRD_START_EXEC_TIMEOUT_SECONDS=45
BIRD_START_RETRIES=2
BIRD_START_RETRY_BACKOFF_SECONDS=1
BIRD_POST_START_SETTLE_SECONDS=60
BIRD_PHASE_TIMEOUT_SECONDS=1200
BIRD_PHASE3_PROGRESS_EVERY=200

setup_experiment_context "${1:-}"
begin_stage_logging "start_bird"
ensure_kubeconfig
KUBECONFIG_PATH="${SEED_KUBECONFIG_PATH}"

echo "Starting BIRD on router-like pods..."
print_test_context

kubectl --kubeconfig "${KUBECONFIG_PATH}" --request-timeout="${KUBECTL_LIST_TIMEOUT_SECONDS}s" get namespace "${SEED_NAMESPACE}" >/dev/null 2>&1 || {
    echo "Namespace ${SEED_NAMESPACE} does not exist." >&2
    exit 1
}

python3 "${SCRIPT_DIR}/start_bird_helper.py" \
    --namespace "${SEED_NAMESPACE}" \
    --artifact-dir "${EXPERIMENT_DIR}" \
    --kubeconfig "${KUBECONFIG_PATH}" \
    --pod-list-timeout-seconds "${KUBECTL_LIST_TIMEOUT_SECONDS}" \
    --kubectl-exec-timeout-seconds "${BIRD_KUBECTL_EXEC_TIMEOUT_SECONDS}" \
    --start-exec-timeout-seconds "${BIRD_START_EXEC_TIMEOUT_SECONDS}" \
    --retries "${BIRD_START_RETRIES}" \
    --retry-backoff-seconds "${BIRD_START_RETRY_BACKOFF_SECONDS}" \
    --start-delay-seconds "${BIRD_START_DELAY_SECONDS}" \
    --load-threshold "${BIRD_LOAD_THRESHOLD}" \
    --load-check-interval-seconds "${BIRD_LOAD_CHECK_INTERVAL_SECONDS}" \
    --post-start-settle-seconds "${BIRD_POST_START_SETTLE_SECONDS}" \
    --phase-timeout-seconds "${BIRD_PHASE_TIMEOUT_SECONDS}" \
    --phase3-progress-every "${BIRD_PHASE3_PROGRESS_EVERY}"

echo "Start BIRD completed."
