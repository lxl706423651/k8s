#!/usr/bin/env bash
# Enable BIRD kernel export inside router-like Kubernetes pods for B62.
#
# Inputs: optional experiment directory argument resolved by lib.sh.
# Outputs: start_bird_kernel.log, start_bird_kernel_targets.json, and
#          start_bird_kernel_summary.json.
# Side effects: writes /etc/bird/conf/kernel.conf in workload containers and
#               reloads BIRD through node-local crictl/nsenter over SSH. Nodes
#               are processed concurrently; pods on each node are processed
#               serially, then each node waits for load to settle.
# Context: run after start_bird.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

KERNEL_SWITCH_DELAY_SECONDS=0.3
KERNEL_LOAD_THRESHOLD=80
KERNEL_LOAD_CHECK_INTERVAL_SECONDS=20
KERNEL_NODE_CONCURRENCY=0
KERNEL_PARALLEL_PER_NODE=1
KERNEL_SWITCH_BATCH_SIZE=0
KERNEL_SWITCH_BATCH_COOLDOWN_SECONDS=0
KERNEL_LOAD_MAX_WAIT_SECONDS=0
KERNEL_EXEC_TIMEOUT_SECONDS=45
KERNEL_BIRDC_TIMEOUT_SECONDS=10
KERNEL_EXPORT_MODE=all
KERNEL_SCAN_BASE_SECONDS=6000
KERNEL_SCAN_JITTER_SECONDS=120
KERNEL_NODE_TIMEOUT_SECONDS=36000
KUBECTL_LIST_TIMEOUT_SECONDS=60

setup_experiment_context "${1:-}"
begin_stage_logging "start_bird_kernel"
ensure_kubeconfig
KUBECONFIG_PATH="${SEED_KUBECONFIG_PATH}"
INVENTORY_PATH="${B62_INVENTORY_PATH}"

echo "Switching BIRD kernel export mode..."
print_test_context
echo "KERNEL_EXPORT_MODE=${KERNEL_EXPORT_MODE}"
echo "strategy=node-local-nsenter"
echo "KERNEL_NODE_CONCURRENCY=${KERNEL_NODE_CONCURRENCY}"
echo "KERNEL_PARALLEL_PER_NODE=${KERNEL_PARALLEL_PER_NODE}"
echo "KERNEL_SWITCH_BATCH_SIZE=${KERNEL_SWITCH_BATCH_SIZE}"
echo "KERNEL_SWITCH_BATCH_COOLDOWN_SECONDS=${KERNEL_SWITCH_BATCH_COOLDOWN_SECONDS}"
echo "KERNEL_SWITCH_DELAY_SECONDS=${KERNEL_SWITCH_DELAY_SECONDS}"
echo "KERNEL_LOAD_THRESHOLD=${KERNEL_LOAD_THRESHOLD}"
echo "KERNEL_LOAD_CHECK_INTERVAL_SECONDS=${KERNEL_LOAD_CHECK_INTERVAL_SECONDS}"
echo "KERNEL_LOAD_MAX_WAIT_SECONDS=${KERNEL_LOAD_MAX_WAIT_SECONDS}"

kubectl --kubeconfig "${KUBECONFIG_PATH}" get namespace "${SEED_NAMESPACE}" >/dev/null 2>&1 || {
    echo "Namespace ${SEED_NAMESPACE} does not exist." >&2
    exit 1
}
require_file "${INVENTORY_PATH}"

python3 "${SCRIPT_DIR}/start_bird_kernel_node_local.py" \
    --namespace "${SEED_NAMESPACE}" \
    --artifact-dir "${EXPERIMENT_DIR}" \
    --inventory "${INVENTORY_PATH}" \
    --kubeconfig "${KUBECONFIG_PATH}" \
    --pod-list-timeout-seconds "${KUBECTL_LIST_TIMEOUT_SECONDS}" \
    --node-concurrency "${KERNEL_NODE_CONCURRENCY}" \
    --parallel-per-node "${KERNEL_PARALLEL_PER_NODE}" \
    --switch-sleep-seconds "${KERNEL_SWITCH_DELAY_SECONDS}" \
    --switch-batch-size "${KERNEL_SWITCH_BATCH_SIZE}" \
    --switch-batch-cooldown-seconds "${KERNEL_SWITCH_BATCH_COOLDOWN_SECONDS}" \
    --one-timeout-seconds "${KERNEL_EXEC_TIMEOUT_SECONDS}" \
    --node-timeout-seconds "${KERNEL_NODE_TIMEOUT_SECONDS}" \
    --birdc-timeout-seconds "${KERNEL_BIRDC_TIMEOUT_SECONDS}" \
    --export-mode "${KERNEL_EXPORT_MODE}" \
    --scan-base-seconds "${KERNEL_SCAN_BASE_SECONDS}" \
    --scan-jitter-seconds "${KERNEL_SCAN_JITTER_SECONDS}" \
    --load-threshold "${KERNEL_LOAD_THRESHOLD}" \
    --load-check-interval-seconds "${KERNEL_LOAD_CHECK_INTERVAL_SECONDS}" \
    --load-max-wait-seconds "${KERNEL_LOAD_MAX_WAIT_SECONDS}"

echo "Start BIRD kernel completed."
