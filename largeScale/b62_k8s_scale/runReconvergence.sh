#!/usr/bin/env bash
# Measure BIRD reconvergence for the active B62 experiment.
#
# Inputs: one experiment run directory. The script reads assignment.yaml,
# cluster.inventory.yaml, and kubeconfig.yaml through lib.sh.
# Outputs: reconvergence.log, reconvergence_targets.json, and
#          reconvergence_summary.json in the run directory.
# Side effects: sends `birdc down` to configured chaos pods, then restarts
#               BIRD on those pods after failure-side convergence is observed.
# Context: run after start_bird_kernel.sh and verifyFibRoutes.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

RECONVERGENCE_LOAD_THRESHOLD=80
RECONVERGENCE_LOAD_CHECK_INTERVAL_SECONDS=15
RECONVERGENCE_ROUTE_CHECK_INTERVAL_SECONDS=10
RECONVERGENCE_PHASE_TIMEOUT_SECONDS=1800
RECONVERGENCE_EXEC_TIMEOUT_SECONDS=45
RECONVERGENCE_LIST_TIMEOUT_SECONDS=300

setup_experiment_context "${1:-}"
begin_stage_logging "reconvergence"
ensure_kubeconfig
KUBECONFIG_PATH="${SEED_KUBECONFIG_PATH}"
INVENTORY_PATH="${B62_INVENTORY_PATH}"

echo "Measuring BIRD reconvergence..."
print_test_context
echo "RECONVERGENCE_LOAD_THRESHOLD=${RECONVERGENCE_LOAD_THRESHOLD}"
echo "RECONVERGENCE_LOAD_CHECK_INTERVAL_SECONDS=${RECONVERGENCE_LOAD_CHECK_INTERVAL_SECONDS}"
echo "RECONVERGENCE_ROUTE_CHECK_INTERVAL_SECONDS=${RECONVERGENCE_ROUTE_CHECK_INTERVAL_SECONDS}"
echo "RECONVERGENCE_PHASE_TIMEOUT_SECONDS=${RECONVERGENCE_PHASE_TIMEOUT_SECONDS}"
echo "RECONVERGENCE_EXEC_TIMEOUT_SECONDS=${RECONVERGENCE_EXEC_TIMEOUT_SECONDS}"

kubectl --kubeconfig "${KUBECONFIG_PATH}" --request-timeout="${RECONVERGENCE_LIST_TIMEOUT_SECONDS}s" get namespace "${SEED_NAMESPACE}" >/dev/null 2>&1 || {
    echo "Namespace ${SEED_NAMESPACE} does not exist." >&2
    exit 1
}
require_file "${INVENTORY_PATH}"

python3 "${SCRIPT_DIR}/measureReconvergence.py" \
    --namespace "${SEED_NAMESPACE}" \
    --artifact-dir "${EXPERIMENT_DIR}" \
    --inventory "${INVENTORY_PATH}" \
    --kubeconfig "${KUBECONFIG_PATH}" \
    --load-threshold "${RECONVERGENCE_LOAD_THRESHOLD}" \
    --load-check-interval-seconds "${RECONVERGENCE_LOAD_CHECK_INTERVAL_SECONDS}" \
    --route-check-interval-seconds "${RECONVERGENCE_ROUTE_CHECK_INTERVAL_SECONDS}" \
    --phase-timeout-seconds "${RECONVERGENCE_PHASE_TIMEOUT_SECONDS}" \
    --exec-timeout-seconds "${RECONVERGENCE_EXEC_TIMEOUT_SECONDS}" \
    --list-timeout-seconds "${RECONVERGENCE_LIST_TIMEOUT_SECONDS}"

echo "Reconvergence measurement completed."
