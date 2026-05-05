#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
KUBECONFIG_PATH="${REPO_ROOT}/output/kubeconfigs/seedemu-k3s.yaml"
TARGET_VCPUS="${SEED_WORKER_TARGET_VCPUS:-24}"
TARGET_MEMORY_KIB="${SEED_WORKER_TARGET_MEMORY_KIB:-50331648}"
SSH_KEY="${SEED_K3S_SSH_KEY:-$HOME/.ssh/id_ed25519}"
SSH_USER="${SEED_K3S_USER:-ubuntu}"

SSH_OPTS=(
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
  -o LogLevel=ERROR
  -o BatchMode=yes
  -o IdentitiesOnly=yes
  -o IdentityAgent=none
  -o ConnectTimeout=10
  -o ServerAliveInterval=30
  -o ServerAliveCountMax=3
  -i "${SSH_KEY}"
)

if [ -f "${SCRIPT_DIR}/env_12node.sh" ]; then
    # shellcheck disable=SC1091
    source "${SCRIPT_DIR}/env_12node.sh" >/dev/null 2>&1 || true
elif [ -f "${SCRIPT_DIR}/env_9node.sh" ]; then
    # shellcheck disable=SC1091
    source "${SCRIPT_DIR}/env_9node.sh" >/dev/null 2>&1 || true
fi

node_ip_from_name() {
    local vm_name="$1"
    local idx="${vm_name##*worker}"
    printf '192.168.122.%s\n' "$((110 + idx))"
}

wait_for_state() {
    local vm_name="$1"
    local target="$2"
    local timeout="${3:-180}"
    local elapsed=0
    while [ "${elapsed}" -lt "${timeout}" ]; do
        if sudo -n virsh domstate "${vm_name}" | grep -qi "${target}"; then
            return 0
        fi
        sleep 5
        elapsed=$((elapsed + 5))
    done
    return 1
}

wait_for_ssh() {
    local vm_name="$1"
    local vm_ip="$2"
    local elapsed=0
    while [ "${elapsed}" -lt 300 ]; do
        if ssh "${SSH_OPTS[@]}" "${SSH_USER}@${vm_ip}" "echo ok" >/dev/null 2>&1; then
            return 0
        fi
        sleep 5
        elapsed=$((elapsed + 5))
    done
    echo "Timeout waiting for SSH on ${vm_name} (${vm_ip})" >&2
    return 1
}

wait_for_node_ready() {
    local node_name="$1"
    export KUBECONFIG="${KUBECONFIG_PATH}"
    kubectl wait --for=condition=Ready "node/${node_name}" --timeout=300s >/dev/null
}

main() {
    export KUBECONFIG="${KUBECONFIG_PATH}"

    if kubectl get ns seedemu-k3s-real-topo >/dev/null 2>&1; then
        echo "Namespace seedemu-k3s-real-topo still exists. Refusing to resize workers while workload namespace is present." >&2
        exit 1
    fi

    mapfile -t workers < <(sudo -n virsh list --all --name | awk '/^seed-k3s-worker[0-9]+$/ {print}' | sort -V)
    [ "${#workers[@]}" -gt 0 ] || { echo "No worker domains found." >&2; exit 1; }

    local vm_name vm_ip
    for vm_name in "${workers[@]}"; do
        vm_ip="$(node_ip_from_name "${vm_name}")"
        echo "Resizing ${vm_name} -> ${TARGET_VCPUS} vCPU / 48GiB"

        sudo -n virsh shutdown "${vm_name}" >/dev/null 2>&1 || true
        if ! wait_for_state "${vm_name}" "shut off" 180; then
            sudo -n virsh destroy "${vm_name}" >/dev/null
            wait_for_state "${vm_name}" "shut off" 60
        fi

        sudo -n virsh setmaxmem "${vm_name}" "${TARGET_MEMORY_KIB}" --config >/dev/null
        sudo -n virsh setmem "${vm_name}" "${TARGET_MEMORY_KIB}" --config >/dev/null
        sudo -n virsh setvcpus "${vm_name}" "${TARGET_VCPUS}" --maximum --config >/dev/null
        sudo -n virsh setvcpus "${vm_name}" "${TARGET_VCPUS}" --config >/dev/null
        sudo -n virsh start "${vm_name}" >/dev/null

        wait_for_ssh "${vm_name}" "${vm_ip}"
        ssh "${SSH_OPTS[@]}" "${SSH_USER}@${vm_ip}" "sudo -n systemctl restart k3s-agent" >/dev/null
        wait_for_node_ready "${vm_name}"
        sudo -n virsh dominfo "${vm_name}" | egrep 'CPU\\(s\\)|Max memory|Used memory|State'
    done
}

main "$@"
