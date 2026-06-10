#!/usr/bin/env bash
# Build the B62 KVM/K3s/Kube-OVN cluster from assignment.yaml.
#
# Inputs: assignment.yaml, renderAssignmentConfig.py, prepareLibvirtDhcp.py,
#         and k8sTools.py.
# Outputs: configKvmOvn.yaml, configK3s.yaml, kubeconfig.yaml, and
#          cluster.inventory.yaml.
# Side effects: creates or updates KVM VMs, installs K3s, installs Kube-OVN,
#               and configures the in-cluster registry.
# Context: run from this directory on the KVM host.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ASSIGNMENT_FILE="${2:-${SCRIPT_DIR}/assignment.yaml}"
RUN_DIR="${1:-${SCRIPT_DIR}/runs/build_cluster_$(date +%Y%m%d_%H%M%S)}"
LOG_FILE="${RUN_DIR}/build-cluster.log"
NODE_READY_TIMEOUT_SECONDS=1800
CONFIG_KVM_OVN="${SCRIPT_DIR}/configKvmOvn.yaml"
CONFIG_K3S="${SCRIPT_DIR}/configK3s.yaml"
KUBECONFIG_OUT="${SCRIPT_DIR}/kubeconfig.yaml"
INVENTORY_OUT="${SCRIPT_DIR}/cluster.inventory.yaml"
LOCK_FILE="${SCRIPT_DIR}/.cluster-lifecycle.lock"
CHILD_PID=""

mkdir -p "${RUN_DIR}"
exec > >(tee -a "${LOG_FILE}") 2>&1

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    echo "ERROR: another B62 cluster lifecycle command is running; wait for it to finish before building." >&2
    exit 1
fi

terminate_process_tree() {
    # Terminate a tracked child and its descendants so interrupted builds do not
    # leave k8sTools/Ansible processes racing later destroy or build commands.
    # $1=pid, $2=signal name or number.
    local pid="${1:-}"
    local signal="${2:-TERM}"
    local child
    [ -n "${pid}" ] || return 0
    for child in $(pgrep -P "${pid}" 2>/dev/null || true); do
        terminate_process_tree "${child}" "${signal}"
    done
    kill "-${signal}" "${pid}" 2>/dev/null || true
}

on_interrupt() {
    local rc=$?
    if [ -n "${CHILD_PID}" ]; then
        echo "Interrupted; terminating build child process tree rooted at ${CHILD_PID}."
        terminate_process_tree "${CHILD_PID}" TERM
        sleep 2
        terminate_process_tree "${CHILD_PID}" KILL
    fi
    exit "${rc}"
}
trap on_interrupt INT TERM

run_tracked() {
    # Run a command while allowing the interrupt trap to clean its descendants.
    "$@" &
    CHILD_PID=$!
    set +e
    wait "${CHILD_PID}"
    local rc=$?
    set -e
    CHILD_PID=""
    return "${rc}"
}

assert_no_conflicting_cleanup() {
    # Old cleanup processes can survive a timed-out destroy and remove K3s from
    # a newly rebuilt VM. Refuse to build until those processes are gone.
    local pattern='[s]eedemu-k8s-tools-destroy|[d]estroyPhysicalCluster.py|[c]leanKubeOvnFabric.py|[h]elm uninstall kube-ovn'
    local matches
    matches="$(pgrep -af "${pattern}" || true)"
    if [ -n "${matches}" ]; then
        echo "ERROR: found cleanup process(es) that can race with build:" >&2
        echo "${matches}" >&2
        echo "Wait for them to finish or terminate them before rebuilding." >&2
        exit 1
    fi
}

echo "===== build-cluster started $(date --iso-8601=seconds) ====="
assert_no_conflicting_cleanup
python3 "${SCRIPT_DIR}/renderAssignmentConfig.py" --assignment "${ASSIGNMENT_FILE}" --run-dir "${RUN_DIR}" prepare

sudo -n python3 "${SCRIPT_DIR}/prepareLibvirtDhcp.py" "${CONFIG_KVM_OVN}"

run_tracked python3 "${SCRIPT_DIR}/k8sTools.py" build \
    --input "${CONFIG_KVM_OVN}" \
    --config-k3s "${CONFIG_K3S}" \
    --kubeconfig "${KUBECONFIG_OUT}" \
    --inventory "${INVENTORY_OUT}" \
    --keep-temp

kubectl --kubeconfig "${KUBECONFIG_OUT}" get nodes -o wide
kubectl --kubeconfig "${KUBECONFIG_OUT}" wait --for=condition=Ready nodes --all --timeout="${NODE_READY_TIMEOUT_SECONDS}s"
kubectl --kubeconfig "${KUBECONFIG_OUT}" -n kube-system get pods -o wide

echo "===== build-cluster completed $(date --iso-8601=seconds) ====="
