#!/usr/bin/env bash
# Verify BIRD protocol health after start_bird.sh for the active B62 run.
#
# Inputs: one experiment run directory resolved by lib.sh.
# Outputs: verify_after_start_bird.log, verify_after_start_bird_targets.json,
#          and verify_after_start_bird_summary.json under the run directory.
# Side effects: read-only kubectl exec calls into selected brd pods.
# Context: run after start_bird.sh; waits for all VM load1 values to fall below
#          VERIFY_START_BIRD_LOAD_THRESHOLD before protocol checks.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

VERIFY_START_BIRD_SAMPLE_COUNT=10
VERIFY_START_BIRD_LOAD_THRESHOLD=80
VERIFY_START_BIRD_LOAD_CHECK_INTERVAL_SECONDS=30
VERIFY_START_BIRD_LOAD_MAX_WAIT_SECONDS=0
VERIFY_START_BIRD_EXEC_TIMEOUT_SECONDS=60
VERIFY_START_BIRD_LIST_TIMEOUT_SECONDS=300
VERIFY_START_BIRD_PROTOCOL_WAIT_SECONDS=300
VERIFY_START_BIRD_PROTOCOL_CHECK_INTERVAL_SECONDS=20

setup_experiment_context "${1:-}"
begin_stage_logging "verify_after_start_bird"
ensure_kubeconfig

echo "Verifying BIRD protocol health after start-bird..."
print_test_context
echo "VERIFY_START_BIRD_SAMPLE_COUNT=${VERIFY_START_BIRD_SAMPLE_COUNT}"
echo "VERIFY_START_BIRD_LOAD_THRESHOLD=${VERIFY_START_BIRD_LOAD_THRESHOLD}"
echo "VERIFY_START_BIRD_LOAD_CHECK_INTERVAL_SECONDS=${VERIFY_START_BIRD_LOAD_CHECK_INTERVAL_SECONDS}"
echo "VERIFY_START_BIRD_LOAD_MAX_WAIT_SECONDS=${VERIFY_START_BIRD_LOAD_MAX_WAIT_SECONDS}"
echo "VERIFY_START_BIRD_EXEC_TIMEOUT_SECONDS=${VERIFY_START_BIRD_EXEC_TIMEOUT_SECONDS}"
echo "VERIFY_START_BIRD_LIST_TIMEOUT_SECONDS=${VERIFY_START_BIRD_LIST_TIMEOUT_SECONDS}"
echo "VERIFY_START_BIRD_PROTOCOL_WAIT_SECONDS=${VERIFY_START_BIRD_PROTOCOL_WAIT_SECONDS}"
echo "VERIFY_START_BIRD_PROTOCOL_CHECK_INTERVAL_SECONDS=${VERIFY_START_BIRD_PROTOCOL_CHECK_INTERVAL_SECONDS}"

kubectl --kubeconfig "${KUBECONFIG}" get namespace "${SEED_NAMESPACE}" >/dev/null 2>&1 || {
    echo "Namespace ${SEED_NAMESPACE} does not exist." >&2
    exit 1
}
require_file "${SEED_CLUSTER_INVENTORY_PATH}"

python3 "${SCRIPT_DIR}/verifyStartBird.py" \
    --namespace "${SEED_NAMESPACE}" \
    --artifact-dir "${EXPERIMENT_DIR}" \
    --inventory "${SEED_CLUSTER_INVENTORY_PATH}" \
    --kubeconfig "${KUBECONFIG}" \
    --sample-count "${VERIFY_START_BIRD_SAMPLE_COUNT}" \
    --load-threshold "${VERIFY_START_BIRD_LOAD_THRESHOLD}" \
    --load-check-interval-seconds "${VERIFY_START_BIRD_LOAD_CHECK_INTERVAL_SECONDS}" \
    --load-max-wait-seconds "${VERIFY_START_BIRD_LOAD_MAX_WAIT_SECONDS}" \
    --exec-timeout-seconds "${VERIFY_START_BIRD_EXEC_TIMEOUT_SECONDS}" \
    --list-timeout-seconds "${VERIFY_START_BIRD_LIST_TIMEOUT_SECONDS}" \
    --protocol-wait-seconds "${VERIFY_START_BIRD_PROTOCOL_WAIT_SECONDS}" \
    --protocol-check-interval-seconds "${VERIFY_START_BIRD_PROTOCOL_CHECK_INTERVAL_SECONDS}"

echo "verify-after-start-bird completed."
