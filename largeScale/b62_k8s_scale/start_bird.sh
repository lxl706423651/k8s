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
BIRD_PRE_START_HOST_LOAD_THRESHOLD=80
BIRD_PRE_START_HOST_LOAD_CHECK_INTERVAL_SECONDS=30
BIRD_PRE_START_HOST_LOAD_MAX_WAIT_SECONDS=36000

wait_for_host_load_before_start() {
    local started_at
    local now
    local waited
    local load_average
    started_at="$(date +%s)"
    while true; do
        load_average="$(awk '{print $1}' /proc/loadavg)"
        if awk -v load_value="${load_average}" -v limit="${BIRD_PRE_START_HOST_LOAD_THRESHOLD}" 'BEGIN { exit !(load_value < limit) }'; then
            echo "pre-start host load gate passed: load1=${load_average} threshold=${BIRD_PRE_START_HOST_LOAD_THRESHOLD}"
            return 0
        fi
        now="$(date +%s)"
        waited=$((now - started_at))
        if [ "${BIRD_PRE_START_HOST_LOAD_MAX_WAIT_SECONDS}" -gt 0 ] &&
           [ "${waited}" -ge "${BIRD_PRE_START_HOST_LOAD_MAX_WAIT_SECONDS}" ]; then
            echo "pre-start host load gate timeout: load1=${load_average} threshold=${BIRD_PRE_START_HOST_LOAD_THRESHOLD} waited=${waited}" >&2
            return 1
        fi
        echo "pre-start host load gate waiting: load1=${load_average} threshold=${BIRD_PRE_START_HOST_LOAD_THRESHOLD} waited=${waited}s sleep=${BIRD_PRE_START_HOST_LOAD_CHECK_INTERVAL_SECONDS}s"
        sleep "${BIRD_PRE_START_HOST_LOAD_CHECK_INTERVAL_SECONDS}"
    done
}

if [[ "${1:-}" == "--runtime" ]]; then
    RUNTIME_BACKEND="${2:-}"
    shift 2
    NATIVE_ARTIFACT_DIR=""
    NATIVE_INVENTORY=""
    NATIVE_EXPERIMENT_ID=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --artifact-dir) NATIVE_ARTIFACT_DIR="${2:-}"; shift 2 ;;
            --inventory) NATIVE_INVENTORY="${2:-}"; shift 2 ;;
            --experiment-id) NATIVE_EXPERIMENT_ID="${2:-}"; shift 2 ;;
            *) echo "Unknown native start-bird option: $1" >&2; exit 2 ;;
        esac
    done
    if [[ "${RUNTIME_BACKEND}" != "docker" ]]; then
        echo "Unsupported start-bird runtime: ${RUNTIME_BACKEND}" >&2
        exit 2
    fi
    require_file "${NATIVE_INVENTORY}"
    [[ -n "${NATIVE_ARTIFACT_DIR}" && -n "${NATIVE_EXPERIMENT_ID}" ]] || {
        echo "--artifact-dir and --experiment-id are required for Docker runtime" >&2
        exit 2
    }
    mkdir -p "${NATIVE_ARTIFACT_DIR}"
    echo "Starting BIRD on native Docker containers through the B62 entrypoint..."
    echo "strategy=node-local-docker-exec"
    echo "BIRD_NODE_CONCURRENCY=${BIRD_NODE_CONCURRENCY}"
    echo "BIRD_PARALLEL_PER_NODE=${BIRD_PARALLEL_PER_NODE}"
    echo "BIRD_START_BATCH_SIZE=${BIRD_START_BATCH_SIZE}"
    echo "BIRD_START_BATCH_COOLDOWN_SECONDS=${BIRD_START_BATCH_COOLDOWN_SECONDS}"
    echo "BIRD_START_DELAY_SECONDS=${BIRD_START_DELAY_SECONDS}"
    echo "BIRD_LOAD_THRESHOLD=${BIRD_LOAD_THRESHOLD}"
    echo "BIRD_LOAD_CHECK_INTERVAL_SECONDS=${BIRD_LOAD_CHECK_INTERVAL_SECONDS}"
    echo "BIRD_LOAD_MAX_WAIT_SECONDS=${BIRD_LOAD_MAX_WAIT_SECONDS}"
    echo "BIRD_PRE_START_HOST_LOAD_THRESHOLD=${BIRD_PRE_START_HOST_LOAD_THRESHOLD}"
    wait_for_host_load_before_start
    python3 "${SCRIPT_DIR}/runDockerBirdStage.py" \
        --stage start-bird \
        --artifact-dir "${NATIVE_ARTIFACT_DIR}" \
        --inventory "${NATIVE_INVENTORY}" \
        --experiment-id "${NATIVE_EXPERIMENT_ID}" \
        --node-concurrency "${BIRD_NODE_CONCURRENCY}" \
        --action-sleep-seconds "${BIRD_START_DELAY_SECONDS}" \
        --start-batch-size "${BIRD_START_BATCH_SIZE}" \
        --start-batch-cooldown-seconds "${BIRD_START_BATCH_COOLDOWN_SECONDS}" \
        --one-timeout-seconds "${BIRD_START_EXEC_TIMEOUT_SECONDS}" \
        --node-timeout-seconds "${BIRD_NODE_TIMEOUT_SECONDS}" \
        --load-threshold "${BIRD_LOAD_THRESHOLD}" \
        --load-check-interval-seconds "${BIRD_LOAD_CHECK_INTERVAL_SECONDS}" \
        --load-max-wait-seconds "${BIRD_LOAD_MAX_WAIT_SECONDS}"
    echo "Start BIRD completed."
    exit 0
fi

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
echo "BIRD_PRE_START_HOST_LOAD_THRESHOLD=${BIRD_PRE_START_HOST_LOAD_THRESHOLD}"
echo "BIRD_PRE_START_HOST_LOAD_CHECK_INTERVAL_SECONDS=${BIRD_PRE_START_HOST_LOAD_CHECK_INTERVAL_SECONDS}"
echo "BIRD_PRE_START_HOST_LOAD_MAX_WAIT_SECONDS=${BIRD_PRE_START_HOST_LOAD_MAX_WAIT_SECONDS}"

kubectl --kubeconfig "${KUBECONFIG_PATH}" --request-timeout="${KUBECTL_LIST_TIMEOUT_SECONDS}s" get namespace "${SEED_NAMESPACE}" >/dev/null 2>&1 || {
    echo "Namespace ${SEED_NAMESPACE} does not exist." >&2
    exit 1
}
require_file "${INVENTORY_PATH}"

wait_for_host_load_before_start

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
