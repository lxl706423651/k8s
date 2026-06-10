#!/usr/bin/env bash
# Enable BIRD kernel export inside router-like Kubernetes pods for B62.
#
# Inputs: optional experiment directory argument resolved by lib.sh.
# Outputs: start_bird_kernel.log, start_bird_kernel_targets.json, and
#          start_bird_kernel_summary.json.
# Side effects: writes /etc/bird/conf/kernel.conf in workload containers and
#               reloads BIRD through kubectl exec. Nodes are processed
#               concurrently; pods on each node are processed serially,
#               matching /home/lxl/k8s/lxl/start-kernel behavior.
# Context: run after start_bird.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

KERNEL_SWITCH_DELAY_SECONDS=0.3
KERNEL_LOAD_THRESHOLD=40
KERNEL_LOAD_CHECK_INTERVAL_SECONDS=20
KERNEL_KUBECTL_EXEC_TIMEOUT_SECONDS=30
KERNEL_EXEC_TIMEOUT_SECONDS=45
KERNEL_BIRDC_TIMEOUT_SECONDS=10
KERNEL_EXPORT_MODE=all
KERNEL_SCAN_BASE_SECONDS=6000
KERNEL_SCAN_JITTER_SECONDS=120
KERNEL_SWITCH_RETRIES=2
KERNEL_SWITCH_RETRY_BACKOFF_SECONDS=1
KERNEL_POST_SWITCH_SETTLE_SECONDS=15

setup_experiment_context "${1:-}"
begin_stage_logging "start_bird_kernel"
ensure_kubeconfig
KUBECONFIG_PATH="${SEED_KUBECONFIG_PATH}"

echo "Switching BIRD kernel export mode..."
print_test_context
echo "KERNEL_EXPORT_MODE=${KERNEL_EXPORT_MODE}"

kubectl --kubeconfig "${KUBECONFIG_PATH}" get namespace "${SEED_NAMESPACE}" >/dev/null 2>&1 || {
    echo "Namespace ${SEED_NAMESPACE} does not exist." >&2
    exit 1
}

python3 "${SCRIPT_DIR}/start_bird_kernel_helper.py" \
    --namespace "${SEED_NAMESPACE}" \
    --artifact-dir "${EXPERIMENT_DIR}" \
    --kubeconfig "${KUBECONFIG_PATH}" \
    --kubectl-exec-timeout-seconds "${KERNEL_KUBECTL_EXEC_TIMEOUT_SECONDS}" \
    --kernel-exec-timeout-seconds "${KERNEL_EXEC_TIMEOUT_SECONDS}" \
    --birdc-timeout-seconds "${KERNEL_BIRDC_TIMEOUT_SECONDS}" \
    --export-mode "${KERNEL_EXPORT_MODE}" \
    --scan-base-seconds "${KERNEL_SCAN_BASE_SECONDS}" \
    --scan-jitter-seconds "${KERNEL_SCAN_JITTER_SECONDS}" \
    --retries "${KERNEL_SWITCH_RETRIES}" \
    --retry-backoff-seconds "${KERNEL_SWITCH_RETRY_BACKOFF_SECONDS}" \
    --switch-delay-seconds "${KERNEL_SWITCH_DELAY_SECONDS}" \
    --load-threshold "${KERNEL_LOAD_THRESHOLD}" \
    --load-check-interval-seconds "${KERNEL_LOAD_CHECK_INTERVAL_SECONDS}" \
    --post-switch-settle-seconds "${KERNEL_POST_SWITCH_SETTLE_SECONDS}"

echo "Start BIRD kernel completed."
