#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_PATH="${1:-${SCRIPT_DIR}/cluster_config.example.yaml}"
HELPER="${SCRIPT_DIR}/cluster_config.py"

eval "$(python3 "${HELPER}" "${CONFIG_PATH}" kvm-env)"

SSH_PUB_KEY=""
EXISTING_VMS_TSV=""
PLANNED_NODES_TSV=""
RESOLVED_NODES_TSV="${CONFIG_PATH%.*}.resolved-nodes.tsv"
REUSING_RESOLVED_PLAN="false"

usage() {
    cat <<EOF
Usage: $0 <cluster_config.yaml>

Create/start KVM guests described by the YAML config.
This script does not install K3s and does not write K3s inventory.
EOF
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "Missing required command: $1" >&2
        exit 1
    }
}

cleanup_tmp() {
    [ -n "${EXISTING_VMS_TSV}" ] && rm -f "${EXISTING_VMS_TSV}"
    [ -n "${PLANNED_NODES_TSV}" ] && rm -f "${PLANNED_NODES_TSV}"
}

domain_exists() {
    virsh dominfo "$1" >/dev/null 2>&1
}

domain_running() {
    virsh domstate "$1" 2>/dev/null | grep -qi "running"
}

prepare_dirs() {
    mkdir -p "${SEED_KVM_STORAGE_DIR}/base"
    mkdir -p "${SEED_KVM_DISK_DIR}"
    mkdir -p "${SEED_KVM_CLOUD_INIT_DIR}"
    mkdir -p "$(dirname "${SEED_KVM_BASE_IMAGE_PATH}")"
}

ensure_network() {
    if ! virsh net-info "${SEED_KVM_NETWORK}" >/dev/null 2>&1; then
        echo "Libvirt network not found: ${SEED_KVM_NETWORK}" >&2
        exit 1
    fi
    virsh net-start "${SEED_KVM_NETWORK}" >/dev/null 2>&1 || true
    virsh net-autostart "${SEED_KVM_NETWORK}" >/dev/null 2>&1 || true
}

collect_existing_vms() {
    EXISTING_VMS_TSV="$(mktemp "${SEED_KVM_STORAGE_DIR}/existing-vms.XXXXXX.tsv")"
    PLANNED_NODES_TSV="$(mktemp "${SEED_KVM_STORAGE_DIR}/planned-vms.XXXXXX.tsv")"

    if [ -f "${RESOLVED_NODES_TSV}" ]; then
        REUSING_RESOLVED_PLAN="true"
        cp "${RESOLVED_NODES_TSV}" "${PLANNED_NODES_TSV}"
        echo "Using existing resolved node plan: ${RESOLVED_NODES_TSV}"
        awk -F '\t' '{printf "  %-24s role=%-6s ip=%-15s mac=%s vcpus=%s memory_mb=%s disk_gb=%s\n", $1, $2, $3, $4, $5, $6, $7}' "${PLANNED_NODES_TSV}"
        return
    fi

    virsh list --all --name 2>/dev/null | awk 'NF {print $1 "\t\t"}' >> "${EXISTING_VMS_TSV}"

    virsh net-dumpxml "${SEED_KVM_NETWORK}" 2>/dev/null | python3 -c '
import sys
import xml.etree.ElementTree as ET

root = ET.fromstring(sys.stdin.read())
for host in root.findall(".//host"):
    print("\t".join([
        host.get("name") or "",
        host.get("ip") or "",
        (host.get("mac") or "").lower(),
    ]))
' >> "${EXISTING_VMS_TSV}"

    virsh net-dhcp-leases "${SEED_KVM_NETWORK}" 2>/dev/null | awk '
        NR <= 2 {next}
        NF >= 5 {
          name=$6
          if (name == "-" || name == "") name=""
          ip=$5
          sub("/.*", "", ip)
          print name "\t" ip "\t" tolower($3)
        }
    ' >> "${EXISTING_VMS_TSV}" || true

    python3 "${HELPER}" "${CONFIG_PATH}" nodes-tsv --existing-tsv "${EXISTING_VMS_TSV}" > "${PLANNED_NODES_TSV}"
    cp "${PLANNED_NODES_TSV}" "${RESOLVED_NODES_TSV}"
    echo "Planned KVM nodes:"
    awk -F '\t' '{printf "  %-24s role=%-6s ip=%-15s mac=%s vcpus=%s memory_mb=%s disk_gb=%s\n", $1, $2, $3, $4, $5, $6, $7}' "${PLANNED_NODES_TSV}"
    echo "Resolved node plan: ${RESOLVED_NODES_TSV}"
}

download_base_image() {
    if [ -f "${SEED_KVM_BASE_IMAGE_PATH}" ]; then
        return
    fi
    if [ -n "${SEED_KVM_LEGACY_BASE_IMAGE_PATH:-}" ] && [ -f "${SEED_KVM_LEGACY_BASE_IMAGE_PATH}" ]; then
        echo "Using existing base image from ${SEED_KVM_LEGACY_BASE_IMAGE_PATH}"
        ln -s "${SEED_KVM_LEGACY_BASE_IMAGE_PATH}" "${SEED_KVM_BASE_IMAGE_PATH}"
        return
    fi
    local output_image=""
    output_image="$(find /home/lxl/k8s/output -type f -name "$(basename "${SEED_KVM_BASE_IMAGE_PATH}")" -print -quit 2>/dev/null || true)"
    if [ -n "${output_image}" ]; then
        echo "Using existing base image from ${output_image}"
        ln -s "${output_image}" "${SEED_KVM_BASE_IMAGE_PATH}"
        return
    fi
    echo "Downloading Ubuntu cloud image to ${SEED_KVM_BASE_IMAGE_PATH}"
    curl -fL "${SEED_KVM_BASE_IMAGE_URL}" -o "${SEED_KVM_BASE_IMAGE_PATH}"
}

load_ssh_pubkey() {
    if [ -f "${SEED_SSH_KEY}.pub" ]; then
        SSH_PUB_KEY="$(tr -d '\n' < "${SEED_SSH_KEY}.pub")"
        return
    fi
    if [ -f "${SEED_SSH_KEY}" ]; then
        SSH_PUB_KEY="$(ssh-keygen -y -f "${SEED_SSH_KEY}" 2>/dev/null | tr -d '\n')"
        [ -n "${SSH_PUB_KEY}" ] && return
    fi
    echo "Cannot read SSH key. Set ssh.key in ${CONFIG_PATH} to a valid private key." >&2
    exit 1
}

update_dhcp_host() {
    local vm_name="$1"
    local vm_ip="$2"
    local vm_mac="$3"
    local host_xml="<host mac='${vm_mac}' name='${vm_name}' ip='${vm_ip}'/>"
    virsh net-update "${SEED_KVM_NETWORK}" delete ip-dhcp-host "${host_xml}" --live --config >/dev/null 2>&1 || true
    virsh net-update "${SEED_KVM_NETWORK}" add ip-dhcp-host "${host_xml}" --live --config >/dev/null
}

create_vm_cloud_init() {
    local vm_name="$1"
    local vm_dir="${SEED_KVM_CLOUD_INIT_DIR}/${vm_name}"
    mkdir -p "${vm_dir}"

    cat > "${vm_dir}/user-data.yaml" <<EOF
#cloud-config
users:
  - default
  - name: ${SEED_SSH_USER}
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    groups: [sudo]
    ssh_authorized_keys:
      - ${SSH_PUB_KEY}
package_update: true
packages:
  - qemu-guest-agent
runcmd:
  - [ systemctl, enable, --now, qemu-guest-agent ]
EOF

    cat > "${vm_dir}/meta-data.yaml" <<EOF
instance-id: ${vm_name}
local-hostname: ${vm_name}
EOF
}

create_vm_disk() {
    local vm_name="$1"
    local disk_gb="$2"
    local vm_disk="${SEED_KVM_DISK_DIR}/${vm_name}.qcow2"
    if [ -f "${vm_disk}" ]; then
        return
    fi
    qemu-img create -f qcow2 -F qcow2 -b "${SEED_KVM_BASE_IMAGE_PATH}" "${vm_disk}" "${disk_gb}G" >/dev/null
}

create_or_start_vm() {
    local vm_name="$1"
    local vm_ip="$2"
    local vm_mac="$3"
    local vcpus="$4"
    local memory_mb="$5"
    local disk_gb="$6"
    local vm_disk="${SEED_KVM_DISK_DIR}/${vm_name}.qcow2"
    local vm_cloud_dir="${SEED_KVM_CLOUD_INIT_DIR}/${vm_name}"

    if domain_exists "${vm_name}"; then
        if [ "${SEED_KVM_ALLOW_EXISTING}" != "true" ] && [ "${REUSING_RESOLVED_PLAN}" != "true" ]; then
            echo "Refusing to reuse existing VM '${vm_name}'. Set kvm.allow_existing: true only if this is intentional." >&2
            exit 1
        fi
        echo "VM exists: ${vm_name}"
        if ! domain_running "${vm_name}"; then
            virsh start "${vm_name}" >/dev/null
        fi
        return
    fi

    echo "Creating VM: ${vm_name} ip=${vm_ip} vcpus=${vcpus} memory_mb=${memory_mb} disk_gb=${disk_gb}"
    virt-install \
        --name "${vm_name}" \
        --memory "${memory_mb}" \
        --vcpus "${vcpus}" \
        --cpu host-passthrough \
        --import \
        --os-variant generic \
        --network "network=${SEED_KVM_NETWORK},model=virtio,mac=${vm_mac}" \
        --disk "path=${vm_disk},format=qcow2,bus=virtio" \
        --graphics none \
        --noautoconsole \
        --cloud-init "user-data=${vm_cloud_dir}/user-data.yaml,meta-data=${vm_cloud_dir}/meta-data.yaml" >/dev/null
}

check_network_conflict() {
    local vm_name="$1"
    local vm_ip="$2"
    local vm_mac="$3"

    virsh net-dumpxml "${SEED_KVM_NETWORK}" | python3 -c '
import sys
import xml.etree.ElementTree as ET

name, ip, mac = sys.argv[1], sys.argv[2], sys.argv[3].lower()
root = ET.fromstring(sys.stdin.read())
for host in root.findall(".//host"):
    h_name = host.get("name") or ""
    h_ip = host.get("ip") or ""
    h_mac = (host.get("mac") or "").lower()
    same_host = h_name == name and h_ip == ip and h_mac == mac
    if h_ip == ip and not same_host:
        raise SystemExit(f"IP {ip} is already reserved by host name={h_name} mac={h_mac}")
    if h_mac == mac and not same_host:
        raise SystemExit(f"MAC {mac} is already reserved by host name={h_name} ip={h_ip}")
' "${vm_name}" "${vm_ip}" "${vm_mac}"

    if virsh net-dhcp-leases "${SEED_KVM_NETWORK}" 2>/dev/null | awk -v ip="${vm_ip}" -v mac="${vm_mac,,}" -v name="${vm_name}" '
        NR <= 2 {next}
        {
          lease_mac=tolower($3); lease_ip=$5; sub("/.*", "", lease_ip); lease_name=$6
          if ((lease_ip == ip || lease_mac == mac) && !(lease_ip == ip && lease_mac == mac && lease_name == name)) {
            print "DHCP lease conflict: name=" lease_name " mac=" lease_mac " ip=" lease_ip > "/dev/stderr"
            exit 1
          }
        }
    '; then
        return
    fi
    exit 1
}

check_create_conflicts() {
    local vm_name="$1"
    local vm_ip="$2"
    local vm_mac="$3"
    local vm_disk="${SEED_KVM_DISK_DIR}/${vm_name}.qcow2"

    if domain_exists "${vm_name}" && [ "${SEED_KVM_ALLOW_EXISTING}" != "true" ] && [ "${REUSING_RESOLVED_PLAN}" != "true" ]; then
        echo "VM name conflict: ${vm_name} already exists. Choose a new name or set kvm.allow_existing: true intentionally." >&2
        exit 1
    fi
    if [ -e "${vm_disk}" ] && ! domain_exists "${vm_name}" ]; then
        echo "Disk conflict: ${vm_disk} already exists but VM ${vm_name} is not defined." >&2
        exit 1
    fi
    check_network_conflict "${vm_name}" "${vm_ip}" "${vm_mac}"
}

wait_for_ssh() {
    local vm_name="$1"
    local vm_ip="$2"
    local elapsed=0
    while [ "${elapsed}" -lt "${SEED_KVM_BOOT_TIMEOUT_SECONDS}" ]; do
        if ssh -o StrictHostKeyChecking=no \
               -n \
               -o UserKnownHostsFile=/dev/null \
               -o LogLevel=ERROR \
               -o BatchMode=yes \
               -o IdentitiesOnly=yes \
               -o IdentityAgent=none \
               -o ConnectTimeout=5 \
               -i "${SEED_SSH_KEY}" \
               "${SEED_SSH_USER}@${vm_ip}" "echo ok" >/dev/null 2>&1; then
            echo "SSH ready: ${vm_name} (${vm_ip})"
            return 0
        fi
        sleep 5
        elapsed=$((elapsed + 5))
    done
    echo "Timeout waiting for SSH on ${vm_name} (${vm_ip})" >&2
    return 1
}

main() {
    if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
        usage
        exit 0
    fi
    require_cmd python3
    require_cmd virsh
    require_cmd virt-install
    require_cmd qemu-img
    require_cmd curl
    require_cmd ssh
    require_cmd ssh-keygen
    require_cmd awk

    [ -f "${SEED_SSH_KEY}" ] || {
        echo "SSH key not found: ${SEED_SSH_KEY}" >&2
        exit 1
    }
    trap cleanup_tmp EXIT

    prepare_dirs
    ensure_network
    collect_existing_vms
    download_base_image
    load_ssh_pubkey

    while IFS=$'\t' read -r name role ip mac vcpus memory_mb disk_gb; do
        check_create_conflicts "${name}" "${ip}" "${mac}"
    done < "${PLANNED_NODES_TSV}"

    while IFS=$'\t' read -r name role ip mac vcpus memory_mb disk_gb; do
        update_dhcp_host "${name}" "${ip}" "${mac}"
        create_vm_cloud_init "${name}"
        create_vm_disk "${name}" "${disk_gb}"
        create_or_start_vm "${name}" "${ip}" "${mac}" "${vcpus}" "${memory_mb}" "${disk_gb}"
    done < "${PLANNED_NODES_TSV}"

    while IFS=$'\t' read -r name role ip mac vcpus memory_mb disk_gb; do
        wait_for_ssh "${name}" "${ip}"
    done < "${PLANNED_NODES_TSV}"

    echo "KVM VMs are ready."
    echo "Next step: ${SCRIPT_DIR}/unlock_vm_limits_from_yaml.sh ${CONFIG_PATH}"
}

main "$@"
