#!/usr/bin/env bash
# Verify Linux FIB route-table growth after start_bird_kernel.sh.
#
# Inputs: one experiment run directory resolved by lib.sh.
# Outputs: verify_after_fib_write.log, verify_after_fib_write_targets.json, and
#          verify_after_fib_write_summary.json under the run directory.
# Side effects: read-only kubectl exec calls into 10 fixed brd pods from
#               different ASNs.
# Context: run after start_bird_kernel.sh; waits for all VM load1 values to
#          fall below VERIFY_FIB_LOAD_THRESHOLD before route checks.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

VERIFY_FIB_LOAD_THRESHOLD=80
VERIFY_FIB_LOAD_CHECK_INTERVAL_SECONDS=30
VERIFY_FIB_LOAD_MAX_WAIT_SECONDS=0
VERIFY_FIB_EXEC_TIMEOUT_SECONDS=60
VERIFY_FIB_LIST_TIMEOUT_SECONDS=300
VERIFY_FIB_NODE_CONCURRENCY=0
VERIFY_FIB_SAMPLE_AS_COUNT=10

setup_experiment_context "${1:-}"
begin_stage_logging "verify_after_fib_write"
ensure_kubeconfig

echo "Verifying Linux FIB after start-kernel..."
print_test_context
echo "VERIFY_FIB_LOAD_THRESHOLD=${VERIFY_FIB_LOAD_THRESHOLD}"
echo "VERIFY_FIB_LOAD_CHECK_INTERVAL_SECONDS=${VERIFY_FIB_LOAD_CHECK_INTERVAL_SECONDS}"
echo "VERIFY_FIB_LOAD_MAX_WAIT_SECONDS=${VERIFY_FIB_LOAD_MAX_WAIT_SECONDS}"
echo "VERIFY_FIB_EXEC_TIMEOUT_SECONDS=${VERIFY_FIB_EXEC_TIMEOUT_SECONDS}"
echo "VERIFY_FIB_LIST_TIMEOUT_SECONDS=${VERIFY_FIB_LIST_TIMEOUT_SECONDS}"
echo "VERIFY_FIB_NODE_CONCURRENCY=${VERIFY_FIB_NODE_CONCURRENCY}"
echo "VERIFY_FIB_SAMPLE_AS_COUNT=${VERIFY_FIB_SAMPLE_AS_COUNT}"

kubectl --kubeconfig "${KUBECONFIG}" get namespace "${SEED_NAMESPACE}" >/dev/null 2>&1 || {
    echo "Namespace ${SEED_NAMESPACE} does not exist." >&2
    exit 1
}
require_file "${SEED_CLUSTER_INVENTORY_PATH}"

python3 "${SCRIPT_DIR}/verifyFibRoutes.py" \
    --namespace "${SEED_NAMESPACE}" \
    --artifact-dir "${EXPERIMENT_DIR}" \
    --inventory "${SEED_CLUSTER_INVENTORY_PATH}" \
    --kubeconfig "${KUBECONFIG}" \
    --load-threshold "${VERIFY_FIB_LOAD_THRESHOLD}" \
    --load-check-interval-seconds "${VERIFY_FIB_LOAD_CHECK_INTERVAL_SECONDS}" \
    --load-max-wait-seconds "${VERIFY_FIB_LOAD_MAX_WAIT_SECONDS}" \
    --exec-timeout-seconds "${VERIFY_FIB_EXEC_TIMEOUT_SECONDS}" \
    --list-timeout-seconds "${VERIFY_FIB_LIST_TIMEOUT_SECONDS}" \
    --node-concurrency "${VERIFY_FIB_NODE_CONCURRENCY}" \
    --sample-as-count "${VERIFY_FIB_SAMPLE_AS_COUNT}"

echo "verify-after-fib-write completed."
