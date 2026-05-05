#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

source "${SCRIPT_DIR}/env_9node.sh"
source "${SCRIPT_DIR}/01_cluster_nodes_9node.sh"
seed_load_cluster_nodes

export KUBECONFIG="${REPO_ROOT}/output/kubeconfigs/${SEED_K3S_CLUSTER_NAME}.yaml"

NAMESPACE="${SEED_NAMESPACE}"
MASTER_IP="${SEED_K3S_MASTER_IP}"
SSH_USER="${SEED_K3S_USER}"
SSH_KEY="${SEED_K3S_SSH_KEY}"
REPAIR_SCRIPT_LOCAL="${SCRIPT_DIR}/repair_k3s_state_db_seedemu_namespace.py"
REPAIR_SCRIPT_REMOTE="/tmp/repair_k3s_state_db_seedemu_namespace.py"
WAIT_IP_SECONDS="${SEED_RECOVER_WAIT_IP_SECONDS:-120}"
WAIT_READY_SECONDS="${SEED_RECOVER_WAIT_READY_SECONDS:-300}"

SSH_OPTS=(
  -i "${SSH_KEY}"
  -o BatchMode=yes
  -o ConnectTimeout=5
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
  -o LogLevel=ERROR
)

log() { printf '[recover] %s\n' "$*"; }
warn() { printf '[recover][warn] %s\n' "$*" >&2; }

remote_master() {
    ssh "${SSH_OPTS[@]}" "${SSH_USER}@${MASTER_IP}" "$@"
}

vm_names() {
    printf '%s\n' "${SEED_NODE_NAMES[@]}"
}

wait_vm_ips() {
    local deadline now vm
    deadline=$(( $(date +%s) + WAIT_IP_SECONDS ))
    while true; do
        local missing=0
        for vm in "${SEED_NODE_NAMES[@]}"; do
            if ! sudo -n virsh domifaddr "${vm}" 2>/dev/null | grep -q 'ipv4'; then
                missing=$((missing + 1))
            fi
        done
        if [ "${missing}" -eq 0 ]; then
            return 0
        fi
        now=$(date +%s)
        if [ "${now}" -ge "${deadline}" ]; then
            return 1
        fi
        sleep 5
    done
}

wait_cluster_ready() {
    local deadline now
    deadline=$(( $(date +%s) + WAIT_READY_SECONDS ))
    while true; do
        if kubectl get nodes -o wide >/tmp/recover_nodes.out 2>/tmp/recover_nodes.err; then
            local not_ready
            not_ready="$(awk 'NR>1 && $2 != "Ready" {c++} END{print c+0}' /tmp/recover_nodes.out)"
            if [ "${not_ready}" -eq 0 ]; then
                cat /tmp/recover_nodes.out
                rm -f /tmp/recover_nodes.out /tmp/recover_nodes.err
                return 0
            fi
        fi
        now=$(date +%s)
        if [ "${now}" -ge "${deadline}" ]; then
            warn "cluster did not become fully Ready within ${WAIT_READY_SECONDS}s"
            [ -f /tmp/recover_nodes.out ] && cat /tmp/recover_nodes.out || true
            [ -f /tmp/recover_nodes.err ] && cat /tmp/recover_nodes.err >&2 || true
            rm -f /tmp/recover_nodes.out /tmp/recover_nodes.err
            return 1
        fi
        sleep 5
    done
}

log "Step 1/5: stop obvious local wrappers for this experiment"
pkill -f '/home/lxl/k8s/lxl/11_run_full_flow_9node.sh' || true
pkill -f '/home/lxl/k8s/lxl/wait-ready' || true
pkill -f '/home/lxl/k8s/lxl/05_deploy-batched_9node' || true

log "Step 2/5: reboot all VMs, then force power-cycle if needed"
for vm in "${SEED_NODE_NAMES[@]}"; do
    sudo -n virsh reboot "${vm}" >/dev/null 2>&1 || sudo -n virsh reset "${vm}" >/dev/null 2>&1 || true
done
sleep 20

for vm in "${SEED_NODE_NAMES[@]}"; do
    if ! sudo -n virsh domifaddr "${vm}" 2>/dev/null | grep -q 'ipv4'; then
        warn "${vm} still has no IPv4 after reboot; forcing destroy/start"
        sudo -n virsh destroy "${vm}" >/dev/null 2>&1 || true
        sleep 2
        sudo -n virsh start "${vm}" >/dev/null
    fi
done

if ! wait_vm_ips; then
    warn "some VMs still have no IPv4 after forced restart:"
    for vm in "${SEED_NODE_NAMES[@]}"; do
        echo "===== ${vm} ====="
        sudo -n virsh domstate --reason "${vm}" || true
        sudo -n virsh domifaddr "${vm}" || true
    done
fi

log "Step 3/5: repair master k3s state DB for namespace ${NAMESPACE}"
scp "${SSH_OPTS[@]}" "${REPAIR_SCRIPT_LOCAL}" "${SSH_USER}@${MASTER_IP}:${REPAIR_SCRIPT_REMOTE}" >/dev/null
remote_master "sudo -n systemctl stop k3s"
remote_master "sudo -n python3 ${REPAIR_SCRIPT_REMOTE}"
remote_master "sudo -n systemctl start k3s"
sleep 10

log "Step 4/5: restart worker k3s-agent services"
for i in "${!SEED_WORKER_NODE_IPS[@]}"; do
    ssh "${SSH_OPTS[@]}" "${SSH_USER}@${SEED_WORKER_NODE_IPS[$i]}" \
        "sudo -n systemctl restart k3s-agent" >/dev/null 2>&1 || \
        warn "failed to restart k3s-agent on ${SEED_WORKER_NODE_NAMES[$i]}"
done

log "Step 5/5: wait for cluster Ready and verify namespace removal"
wait_cluster_ready

if kubectl get ns "${NAMESPACE}" >/dev/null 2>&1; then
    warn "namespace ${NAMESPACE} still exists after recovery:"
    kubectl get ns "${NAMESPACE}" -o yaml || true
    exit 1
fi

log "namespace ${NAMESPACE} is absent"
log "cluster recovered and ready for redeploy"
