#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
source "${REPO_ROOT}/scripts/env_seedemu.sh"

SEED_KVM_STORAGE_DIR="${SEED_KVM_STORAGE_DIR:-${REPO_ROOT}/output/kvm_lab}"
SEED_K3S_USER="${SEED_K3S_USER:-ubuntu}"
SEED_K3S_SSH_KEY="${SEED_K3S_SSH_KEY:-$HOME/.ssh/id_ed25519}"
SEED_KVM_BOOT_TIMEOUT_SECONDS="${SEED_KVM_BOOT_TIMEOUT_SECONDS:-300}"
SEED_KVM_SHUTDOWN_TIMEOUT_SECONDS="${SEED_KVM_SHUTDOWN_TIMEOUT_SECONDS:-180}"
SEED_KVM_ALLOW_FORCE_POWER_OFF="${SEED_KVM_ALLOW_FORCE_POWER_OFF:-false}"

SEED_K3S_MASTER_NAME="${SEED_K3S_MASTER_NAME:-seed-k3s-master}"
SEED_K3S_WORKER1_NAME="${SEED_K3S_WORKER1_NAME:-seed-k3s-worker1}"
SEED_K3S_WORKER2_NAME="${SEED_K3S_WORKER2_NAME:-seed-k3s-worker2}"
SEED_K3S_MASTER_IP="${SEED_K3S_MASTER_IP:-192.168.122.110}"
SEED_K3S_WORKER1_IP="${SEED_K3S_WORKER1_IP:-192.168.122.111}"
SEED_K3S_WORKER2_IP="${SEED_K3S_WORKER2_IP:-192.168.122.112}"

SSH_OPTS=(
    -o StrictHostKeyChecking=no
    -o UserKnownHostsFile=/dev/null
    -o BatchMode=yes
    -o IdentitiesOnly=yes
    -o IdentityAgent=none
    -o ConnectTimeout=5
    -i "${SEED_K3S_SSH_KEY}"
)

usage() {
    cat <<'EOF'
Usage: ./resize_kvm_cluster_resources.sh

Target resource profile:
  master   -> 64 vCPU, 120 GiB RAM, 400 GiB disk
  worker1  -> 32 vCPU,  60 GiB RAM, 200 GiB disk
  worker2  -> 32 vCPU,  60 GiB RAM, 200 GiB disk

Notes:
  - CPU / memory changes are applied through virsh persistent config.
  - Disk is only grown, never shrunk. If the current virtual disk is larger than
    the target size, the script will keep the larger disk and print a warning.
  - When a disk grows, the script will boot the guest and expand the root
    filesystem from inside the VM.

Optional env vars:
  SEED_KVM_ALLOW_FORCE_POWER_OFF=true   # allow virsh destroy if shutdown hangs
  SEED_KVM_SHUTDOWN_TIMEOUT_SECONDS=180
  SEED_KVM_BOOT_TIMEOUT_SECONDS=300
EOF
}

require_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "Missing required command: $1" >&2
        exit 1
    fi
}

domain_exists() {
    virsh dominfo "$1" >/dev/null 2>&1
}

domain_running() {
    virsh domstate "$1" 2>/dev/null | grep -qi "running"
}

wait_for_ssh() {
    local vm_name="$1"
    local vm_ip="$2"
    local elapsed=0

    while [ "${elapsed}" -lt "${SEED_KVM_BOOT_TIMEOUT_SECONDS}" ]; do
        if ssh "${SSH_OPTS[@]}" "${SEED_K3S_USER}@${vm_ip}" "echo ok" >/dev/null 2>&1; then
            echo "SSH ready: ${vm_name} (${vm_ip})"
            return 0
        fi
        sleep 5
        elapsed=$((elapsed + 5))
    done

    echo "Timeout waiting for SSH on ${vm_name} (${vm_ip})" >&2
    return 1
}

wait_for_shutdown() {
    local vm_name="$1"
    local elapsed=0

    while [ "${elapsed}" -lt "${SEED_KVM_SHUTDOWN_TIMEOUT_SECONDS}" ]; do
        if ! domain_running "${vm_name}"; then
            return 0
        fi
        sleep 5
        elapsed=$((elapsed + 5))
    done
    return 1
}

ensure_stopped() {
    local vm_name="$1"

    if ! domain_running "${vm_name}"; then
        return 0
    fi

    echo "Shutting down ${vm_name}..."
    virsh shutdown "${vm_name}" >/dev/null || true
    if wait_for_shutdown "${vm_name}"; then
        echo "${vm_name} is shut off."
        return 0
    fi

    if [ "${SEED_KVM_ALLOW_FORCE_POWER_OFF}" = "true" ]; then
        echo "Graceful shutdown timed out for ${vm_name}; forcing power off."
        virsh destroy "${vm_name}" >/dev/null
        wait_for_shutdown "${vm_name}"
        return 0
    fi

    echo "Timed out waiting for ${vm_name} to stop. Re-run with SEED_KVM_ALLOW_FORCE_POWER_OFF=true if needed." >&2
    exit 1
}

start_domain() {
    local vm_name="$1"

    if domain_running "${vm_name}"; then
        return 0
    fi
    virsh start "${vm_name}" >/dev/null
}

find_vm_disk_path() {
    local vm_name="$1"
    local candidate="${SEED_KVM_STORAGE_DIR}/disks/${vm_name}.qcow2"

    if [ -f "${candidate}" ]; then
        printf '%s\n' "${candidate}"
        return 0
    fi

    virsh domblklist "${vm_name}" --details 2>/dev/null | awk '$2 == "disk" {print $4; exit}'
}

current_disk_bytes() {
    local disk_path="$1"
    qemu-img info "${disk_path}" 2>/dev/null | sed -n 's/^virtual size:.*(\([0-9][0-9]*\) bytes).*/\1/p' | head -n 1
}

target_spec_for_vm() {
    local vm_name="$1"
    case "${vm_name}" in
        "${SEED_K3S_MASTER_NAME}")
            printf '%s %s %s\n' "64" "122880" "400"
            ;;
        "${SEED_K3S_WORKER1_NAME}"|"${SEED_K3S_WORKER2_NAME}")
            printf '%s %s %s\n' "32" "61440" "200"
            ;;
        *)
            echo "Unsupported VM name: ${vm_name}" >&2
            exit 1
            ;;
    esac
}

ip_for_vm() {
    local vm_name="$1"
    case "${vm_name}" in
        "${SEED_K3S_MASTER_NAME}") printf '%s\n' "${SEED_K3S_MASTER_IP}" ;;
        "${SEED_K3S_WORKER1_NAME}") printf '%s\n' "${SEED_K3S_WORKER1_IP}" ;;
        "${SEED_K3S_WORKER2_NAME}") printf '%s\n' "${SEED_K3S_WORKER2_IP}" ;;
        *)
            echo "Unsupported VM name: ${vm_name}" >&2
            exit 1
            ;;
    esac
}

apply_compute_resize() {
    local vm_name="$1"
    local vcpus="$2"
    local mem_mb="$3"
    local mem_kib=$((mem_mb * 1024))

    echo "Updating compute for ${vm_name}: vcpus=${vcpus} memory_mb=${mem_mb}"
    virsh setmaxmem "${vm_name}" --size "${mem_kib}" --config >/dev/null
    virsh setmem "${vm_name}" --size "${mem_kib}" --config >/dev/null
    virsh setvcpus "${vm_name}" "${vcpus}" --maximum --config >/dev/null
    virsh setvcpus "${vm_name}" "${vcpus}" --config >/dev/null
}

grow_disk_if_needed() {
    local vm_name="$1"
    local target_disk_gb="$2"
    local disk_path current_bytes target_bytes

    disk_path="$(find_vm_disk_path "${vm_name}")"
    if [ -z "${disk_path}" ] || [ ! -f "${disk_path}" ]; then
        echo "Cannot locate disk for ${vm_name}" >&2
        exit 1
    fi

    current_bytes="$(current_disk_bytes "${disk_path}")"
    if [ -z "${current_bytes}" ]; then
        echo "Cannot determine current disk size for ${vm_name}: ${disk_path}" >&2
        exit 1
    fi

    target_bytes=$((target_disk_gb * 1024 * 1024 * 1024))
    if [ "${current_bytes}" -gt "${target_bytes}" ]; then
        echo "Disk for ${vm_name} is already larger than target (${disk_path}); skipping shrink."
        return 1
    fi

    if [ "${current_bytes}" -eq "${target_bytes}" ]; then
        echo "Disk for ${vm_name} already at target size ${target_disk_gb}G."
        return 1
    fi

    echo "Growing disk for ${vm_name} to ${target_disk_gb}G: ${disk_path}"
    qemu-img resize "${disk_path}" "${target_disk_gb}G" >/dev/null
    return 0
}

expand_guest_rootfs() {
    local vm_name="$1"
    local vm_ip="$2"

    echo "Expanding root filesystem inside ${vm_name} (${vm_ip})"
    ssh "${SSH_OPTS[@]}" "${SEED_K3S_USER}@${vm_ip}" '
        set -euo pipefail
        if ! command -v growpart >/dev/null 2>&1; then
            sudo -n apt-get update -y >/dev/null
            sudo -n apt-get install -y cloud-guest-utils >/dev/null
        fi

        root_src="$(findmnt -n -o SOURCE /)"
        root_dev="$(readlink -f "${root_src}")"
        disk_name="$(lsblk -no PKNAME "${root_dev}" | head -n 1)"
        part_num="$(lsblk -no PARTNUM "${root_dev}" | head -n 1)"
        fstype="$(findmnt -n -o FSTYPE /)"

        if [ -z "${disk_name}" ] || [ -z "${part_num}" ]; then
            echo "Unable to determine root partition layout for ${root_dev}" >&2
            exit 1
        fi

        sudo -n growpart "/dev/${disk_name}" "${part_num}" >/dev/null || true

        case "${fstype}" in
            ext2|ext3|ext4)
                sudo -n resize2fs "${root_dev}" >/dev/null
                ;;
            xfs)
                sudo -n xfs_growfs / >/dev/null
                ;;
            *)
                echo "Unsupported root filesystem for online resize: ${fstype}" >&2
                exit 1
                ;;
        esac
    '
}

main() {
    local vm_name vm_ip vcpus mem_mb disk_gb disk_grown

    require_cmd virsh
    require_cmd qemu-img
    require_cmd ssh

    if [ ! -f "${SEED_K3S_SSH_KEY}" ]; then
        echo "SSH key not found: ${SEED_K3S_SSH_KEY}" >&2
        exit 1
    fi

    for vm_name in "${SEED_K3S_MASTER_NAME}" "${SEED_K3S_WORKER1_NAME}" "${SEED_K3S_WORKER2_NAME}"; do
        if ! domain_exists "${vm_name}"; then
            echo "VM does not exist: ${vm_name}" >&2
            exit 1
        fi
    done

    for vm_name in "${SEED_K3S_MASTER_NAME}" "${SEED_K3S_WORKER1_NAME}" "${SEED_K3S_WORKER2_NAME}"; do
        read -r vcpus mem_mb disk_gb < <(target_spec_for_vm "${vm_name}")
        vm_ip="$(ip_for_vm "${vm_name}")"

        ensure_stopped "${vm_name}"
        apply_compute_resize "${vm_name}" "${vcpus}" "${mem_mb}"

        disk_grown=false
        if grow_disk_if_needed "${vm_name}" "${disk_gb}"; then
            disk_grown=true
        fi

        start_domain "${vm_name}"
        wait_for_ssh "${vm_name}" "${vm_ip}"

        if [ "${disk_grown}" = "true" ]; then
            expand_guest_rootfs "${vm_name}" "${vm_ip}"
        fi
    done

    echo ""
    echo "Resize completed."
    echo "Targets:"
    echo "  ${SEED_K3S_MASTER_NAME}: 64 vCPU / 120 GiB / 400 GiB"
    echo "  ${SEED_K3S_WORKER1_NAME}: 32 vCPU / 60 GiB / 200 GiB"
    echo "  ${SEED_K3S_WORKER2_NAME}: 32 vCPU / 60 GiB / 200 GiB"
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    usage
    exit 0
fi

main "$@"
