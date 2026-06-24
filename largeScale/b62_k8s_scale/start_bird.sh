#!/usr/bin/env bash
# Start BIRD inside router-like Kubernetes pods for the active B62 experiment.
# Inputs: optional experiment directory argument resolved by lib.sh.
# Outputs: start_bird.log, start_bird_targets.json, start_bird_summary.json.
# Side effects: checks the target namespace and starts BIRD processes through
# node-local crictl/nsenter over SSH. Nodes are processed concurrently; pods on
# each node are processed serially with node-load cooldowns.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

KUBECTL_LIST_TIMEOUT_SECONDS=60
BIRD_START_DELAY_SECONDS=0.1
BIRD_LOAD_THRESHOLD=80
BIRD_LOAD_CHECK_INTERVAL_SECONDS=20
BIRD_NODE_CONCURRENCY=0
BIRD_PARALLEL_PER_NODE=1
BIRD_START_BATCH_SIZE=0
BIRD_START_BATCH_COOLDOWN_SECONDS=0
BIRD_LOAD_MAX_WAIT_SECONDS=0
BIRD_START_EXEC_TIMEOUT_SECONDS=45
BIRD_NODE_TIMEOUT_SECONDS=36000

setup_experiment_context "${1:-}"
begin_stage_logging "start_bird"
ensure_kubeconfig
KUBECONFIG_PATH="${SEED_KUBECONFIG_PATH}"
INVENTORY_PATH="${B62_INVENTORY_PATH}"

echo "Starting BIRD on router-like pods..."
print_test_context
echo "strategy=node-local-nsenter"
echo "BIRD_NODE_CONCURRENCY=${BIRD_NODE_CONCURRENCY}"
echo "BIRD_PARALLEL_PER_NODE=${BIRD_PARALLEL_PER_NODE}"
echo "BIRD_START_BATCH_SIZE=${BIRD_START_BATCH_SIZE}"
echo "BIRD_START_BATCH_COOLDOWN_SECONDS=${BIRD_START_BATCH_COOLDOWN_SECONDS}"
echo "BIRD_START_DELAY_SECONDS=${BIRD_START_DELAY_SECONDS}"
echo "BIRD_LOAD_THRESHOLD=${BIRD_LOAD_THRESHOLD}"
echo "BIRD_LOAD_CHECK_INTERVAL_SECONDS=${BIRD_LOAD_CHECK_INTERVAL_SECONDS}"
echo "BIRD_LOAD_MAX_WAIT_SECONDS=${BIRD_LOAD_MAX_WAIT_SECONDS}"

kubectl --kubeconfig "${KUBECONFIG_PATH}" --request-timeout="${KUBECTL_LIST_TIMEOUT_SECONDS}s" get namespace "${SEED_NAMESPACE}" >/dev/null 2>&1 || {
    echo "Namespace ${SEED_NAMESPACE} does not exist." >&2
    exit 1
}
require_file "${INVENTORY_PATH}"

python3 "${SCRIPT_DIR}/start_bird_node_local.py" \
    --namespace "${SEED_NAMESPACE}" \
    --artifact-dir "${EXPERIMENT_DIR}" \
    --inventory "${INVENTORY_PATH}" \
    --kubeconfig "${KUBECONFIG_PATH}" \
    --pod-list-timeout-seconds "${KUBECTL_LIST_TIMEOUT_SECONDS}" \
    --node-concurrency "${BIRD_NODE_CONCURRENCY}" \
    --parallel-per-node "${BIRD_PARALLEL_PER_NODE}" \
    --start-delay-seconds "${BIRD_START_DELAY_SECONDS}" \
    --start-batch-size "${BIRD_START_BATCH_SIZE}" \
    --start-batch-cooldown-seconds "${BIRD_START_BATCH_COOLDOWN_SECONDS}" \
    --one-timeout-seconds "${BIRD_START_EXEC_TIMEOUT_SECONDS}" \
    --node-timeout-seconds "${BIRD_NODE_TIMEOUT_SECONDS}" \
    --load-threshold "${BIRD_LOAD_THRESHOLD}" \
    --load-check-interval-seconds "${BIRD_LOAD_CHECK_INTERVAL_SECONDS}" \
    --load-max-wait-seconds "${BIRD_LOAD_MAX_WAIT_SECONDS}"

echo "Start BIRD completed."
