#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INPUT_PATH="${1:-${SCRIPT_DIR}/kvm_template.yaml}"
HELPER="${SCRIPT_DIR}/cluster_config.py"

if [[ "${INPUT_PATH}" == *.tsv ]]; then
    RESOLVED_NODES_TSV="${INPUT_PATH}"
else
    RESOLVED_NODES_TSV="${INPUT_PATH%.*}.resolved-nodes.tsv"
fi

KVM_NETWORK="${KVM_NETWORK:-default}"
DISK_DIR="${DISK_DIR:-/data/lxl/k8s/origin_k8s/setup/disks}"
CLOUD_INIT_DIR="${CLOUD_INIT_DIR:-${SCRIPT_DIR}/cloud-init}"
DNSMASQ_STATUS="${DNSMASQ_STATUS:-/var/lib/libvirt/dnsmasq/virbr0.status}"
SETUP_OUTPUT_GLOB="${SETUP_OUTPUT_GLOB:-${SCRIPT_DIR}/seedemu-k3s.*}"

usage() {
    cat <<EOF
Usage: $0 [kvm.yaml | resolved-nodes.tsv]

Clean KVM guests recorded in a resolved-nodes TSV file and verify that no
domain, DHCP reservation, DHCP lease, disk, cloud-init directory, or resolved
plan remains.

Default input:
  ${SCRIPT_DIR}/kvm_template.yaml

Default resolved plan:
  ${SCRIPT_DIR}/kvm_template.resolved-nodes.tsv

Environment overrides:
  KVM_NETWORK=${KVM_NETWORK}
  DISK_DIR=${DISK_DIR}
  CLOUD_INIT_DIR=${CLOUD_INIT_DIR}
  DNSMASQ_STATUS=${DNSMASQ_STATUS}
  SETUP_OUTPUT_GLOB=${SETUP_OUTPUT_GLOB}
EOF
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "Missing required command: $1" >&2
        exit 1
    }
}

require_resolved_plan() {
    if [ ! -s "${RESOLVED_NODES_TSV}" ]; then
        echo "Resolved node plan not found or empty: ${RESOLVED_NODES_TSV}" >&2
        echo "Run kvm_from_yaml.sh first, or pass an explicit *.resolved-nodes.tsv file." >&2
        exit 1
    fi
}

load_config_defaults() {
    if [[ "${INPUT_PATH}" == *.tsv ]]; then
        return
    fi
    if [ -f "${INPUT_PATH}" ]; then
        # Reuse the same defaults/overrides as kvm_from_yaml.sh so cleanup
        # follows the actual disk/network/cloud-init locations for this config.
        eval "$(python3 "${HELPER}" "${INPUT_PATH}" kvm-env)"
        KVM_NETWORK="${KVM_NETWORK:-${SEED_KVM_NETWORK}}"
        DISK_DIR="${DISK_DIR:-${SEED_KVM_DISK_DIR}}"
        CLOUD_INIT_DIR="${CLOUD_INIT_DIR:-${SEED_KVM_CLOUD_INIT_DIR}}"
        KVM_NETWORK="${SEED_KVM_NETWORK:-${KVM_NETWORK}}"
        DISK_DIR="${SEED_KVM_DISK_DIR:-${DISK_DIR}}"
        CLOUD_INIT_DIR="${SEED_KVM_CLOUD_INIT_DIR:-${CLOUD_INIT_DIR}}"
    fi
}

print_plan() {
    echo "Cleaning KVM nodes from: ${RESOLVED_NODES_TSV}"
    echo "KVM network: ${KVM_NETWORK}"
    echo "Disk dir: ${DISK_DIR}"
    echo "Cloud-init dir: ${CLOUD_INIT_DIR}"
    awk -F '\t' '{printf "  %-24s role=%-6s ip=%-15s mac=%s\n", $1, $2, $3, $4}' "${RESOLVED_NODES_TSV}"
}

cleanup_domains_reservations_and_files() {
    while IFS=$'\t' read -r name role ip mac vcpus memory_mb disk_gb; do
        [ -n "${name}" ] || continue
        echo "Cleaning VM ${name} (${ip}, ${mac})"

        if virsh dominfo "${name}" >/dev/null 2>&1; then
            virsh destroy "${name}" >/dev/null 2>&1 || true
            virsh undefine "${name}" --nvram --remove-all-storage >/dev/null 2>&1 \
                || virsh undefine "${name}" --nvram >/dev/null 2>&1 \
                || virsh undefine "${name}" >/dev/null
        fi

        if [ -n "${ip}" ] && [ -n "${mac}" ]; then
            local host_xml="<host mac='${mac}' name='${name}' ip='${ip}'/>"
            virsh net-update "${KVM_NETWORK}" delete ip-dhcp-host "${host_xml}" --live --config >/dev/null 2>&1 || true
        fi

        rm -rf "${CLOUD_INIT_DIR}/${name}"
        rm -f "${DISK_DIR}/${name}.qcow2"
    done < "${RESOLVED_NODES_TSV}"
}

cleanup_dnsmasq_stale_leases() {
    [ -f "${DNSMASQ_STATUS}" ] || return 0

    local tmp_json
    tmp_json="$(mktemp)"
    awk -F '\t' 'NF >= 4 {print $1 "\t" $3 "\t" tolower($4)}' "${RESOLVED_NODES_TSV}" > "${tmp_json}.nodes"

    sudo cp "${DNSMASQ_STATUS}" "${DNSMASQ_STATUS}.bak.$(date +%Y%m%d_%H%M%S)"
    sudo python3 - "${DNSMASQ_STATUS}" "${tmp_json}.nodes" <<'PY'
import json
import sys
from pathlib import Path

status_path = Path(sys.argv[1])
nodes_path = Path(sys.argv[2])

remove_names = set()
remove_ips = set()
remove_macs = set()
for line in nodes_path.read_text(encoding="utf-8").splitlines():
    parts = line.split("\t")
    if len(parts) < 3:
        continue
    name, ip, mac = parts[:3]
    if name:
        remove_names.add(name)
    if ip:
        remove_ips.add(ip)
    if mac:
        remove_macs.add(mac.lower())

try:
    data = json.loads(status_path.read_text(encoding="utf-8") or "[]")
except json.JSONDecodeError:
    raise SystemExit(f"Invalid dnsmasq status JSON: {status_path}")

filtered = [
    item for item in data
    if not (
        item.get("hostname") in remove_names
        or item.get("ip-address") in remove_ips
        or str(item.get("mac-address", "")).lower() in remove_macs
    )
]
status_path.write_text(json.dumps(filtered, indent=2) + "\n", encoding="utf-8")
PY
    rm -f "${tmp_json}" "${tmp_json}.nodes"
}

remove_resolved_plan() {
    rm -f "${RESOLVED_NODES_TSV}"
}

cleanup_setup_outputs() {
    echo "Cleaning stale setup outputs matching: ${SETUP_OUTPUT_GLOB}"
    rm -f ${SETUP_OUTPUT_GLOB}
}

verify_clean() {
    local failed=0
    local tmp_patterns
    tmp_patterns="$(mktemp)"

    awk -F '\t' 'NF >= 4 {
        if ($1 != "") print $1
        if ($3 != "") print $3
        if ($4 != "") print tolower($4)
    }' "${RESOLVED_NODES_TSV}.verify-copy" > "${tmp_patterns}"

    echo "Verifying cleanup..."

    while IFS=$'\t' read -r name role ip mac vcpus memory_mb disk_gb; do
        [ -n "${name}" ] || continue
        if virsh dominfo "${name}" >/dev/null 2>&1; then
            echo "Residual domain: ${name}" >&2
            failed=1
        fi
        if [ -e "${DISK_DIR}/${name}.qcow2" ]; then
            echo "Residual disk: ${DISK_DIR}/${name}.qcow2" >&2
            failed=1
        fi
        if [ -e "${CLOUD_INIT_DIR}/${name}" ]; then
            echo "Residual cloud-init dir: ${CLOUD_INIT_DIR}/${name}" >&2
            failed=1
        fi
    done < "${RESOLVED_NODES_TSV}.verify-copy"

    if virsh net-dumpxml "${KVM_NETWORK}" 2>/dev/null | grep -F -f "${tmp_patterns}" >/dev/null 2>&1; then
        echo "Residual DHCP reservation in libvirt network ${KVM_NETWORK}" >&2
        virsh net-dumpxml "${KVM_NETWORK}" | grep -F -f "${tmp_patterns}" >&2 || true
        failed=1
    fi

    if virsh net-dhcp-leases "${KVM_NETWORK}" 2>/dev/null | grep -F -f "${tmp_patterns}" >/dev/null 2>&1; then
        echo "Residual DHCP lease in libvirt network ${KVM_NETWORK}" >&2
        virsh net-dhcp-leases "${KVM_NETWORK}" | grep -F -f "${tmp_patterns}" >&2 || true
        failed=1
    fi

    if [ -e "${RESOLVED_NODES_TSV}" ]; then
        echo "Residual resolved plan: ${RESOLVED_NODES_TSV}" >&2
        failed=1
    fi

    if compgen -G "${SETUP_OUTPUT_GLOB}" >/dev/null; then
        echo "Residual setup output files matching ${SETUP_OUTPUT_GLOB}" >&2
        compgen -G "${SETUP_OUTPUT_GLOB}" >&2 || true
        failed=1
    fi

    rm -f "${tmp_patterns}" "${RESOLVED_NODES_TSV}.verify-copy"

    if [ "${failed}" -ne 0 ]; then
        echo "Cleanup verification failed." >&2
        exit 1
    fi
    echo "Cleanup verification passed."
}

main() {
    if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
        usage
        exit 0
    fi

    require_cmd awk
    require_cmd grep
    require_cmd mktemp
    require_cmd python3
    require_cmd sudo
    require_cmd virsh

    load_config_defaults
    require_resolved_plan
    print_plan

    cp "${RESOLVED_NODES_TSV}" "${RESOLVED_NODES_TSV}.verify-copy"
    cleanup_domains_reservations_and_files
    cleanup_dnsmasq_stale_leases
    remove_resolved_plan
    cleanup_setup_outputs
    verify_clean
}

main "$@"
