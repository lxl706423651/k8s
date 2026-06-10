#!/usr/bin/env bash
# Destroy the B62 K3s/KVM cluster and its dedicated libvirt networks.
#
# Inputs: configK3s.yaml, configKvmOvn.yaml, assignment.yaml, and k8sTools.py.
# Outputs: destroy-cluster log under the provided run directory.
# Side effects: removes K3s/Kube-OVN state and KVM resources recorded in the
#               selected configK3s YAML, then destroys and undefines matching
#               B62 libvirt networks so old NAT CIDRs cannot block a rebuild.
# Context: run only when the VM/K3s cluster should be removed.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="${1:-${SCRIPT_DIR}/runs/destroy_cluster_$(date +%Y%m%d_%H%M%S)}"
LOG_FILE="${RUN_DIR}/destroy-cluster.log"
CONFIG_K3S="${SCRIPT_DIR}/configK3s.yaml"
CONFIG_KVM="${SCRIPT_DIR}/configKvmOvn.yaml"
ASSIGNMENT_FILE="${SCRIPT_DIR}/assignment.yaml"
LOCK_FILE="${SCRIPT_DIR}/.cluster-lifecycle.lock"
CHILD_PID=""

mkdir -p "${RUN_DIR}"
exec > >(tee -a "${LOG_FILE}") 2>&1

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    echo "ERROR: another B62 cluster lifecycle command is running; wait for it to finish before destroying." >&2
    exit 1
fi

terminate_process_tree() {
    # Terminate a tracked child and its descendants so interrupted destroys do
    # not leave uninstall scripts racing a later rebuild.
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
        echo "Interrupted; terminating destroy child process tree rooted at ${CHILD_PID}."
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

collect_libvirt_networks() {
    # Print B62 libvirt network names from generated config, assignment, and
    # the current libvirt network list. Paths are globals from this script.
    python3 - "${CONFIG_KVM}" "${ASSIGNMENT_FILE}" <<'PY'
import subprocess
import sys
from pathlib import Path

import yaml


def load_yaml(path_text):
    path = Path(path_text)
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def add(values, seen, name):
    name = str(name or "").strip()
    if name and name not in seen:
        seen.add(name)
        values.append(name)


config = load_yaml(sys.argv[1])
assignment = load_yaml(sys.argv[2])
values = []
seen = set()

kvm_config = config.get("kvm") if isinstance(config.get("kvm"), dict) else {}
add(values, seen, kvm_config.get("network"))

experiment = assignment.get("experiment") if isinstance(assignment.get("experiment"), dict) else {}
kvm_assignment = assignment.get("kvm") if isinstance(assignment.get("kvm"), dict) else {}
worker_count = experiment.get("workerCount")
network_prefix = str(kvm_assignment.get("networkPrefix") or "").strip()
if network_prefix and worker_count is not None:
    add(values, seen, f"{network_prefix}-w{worker_count}")

if network_prefix:
    listed = subprocess.run(
        ["virsh", "-c", "qemu:///system", "net-list", "--all", "--name"],
        text=True,
        capture_output=True,
        check=False,
    )
    if listed.returncode == 0:
        for line in listed.stdout.splitlines():
            name = line.strip()
            if name.startswith(f"{network_prefix}-"):
                add(values, seen, name)

for value in values:
    print(value)
PY
}

network_field() {
    # Return one field from `virsh net-info` stdout.
    # $1=infoText, $2=fieldName
    awk -F: -v key="$2" 'tolower($1) == tolower(key) {gsub(/^[ \t]+|[ \t]+$/, "", $2); print tolower($2); exit}' <<<"$1"
}

destroy_libvirt_network() {
    # Destroy and undefine one libvirt network if it still exists.
    # $1=networkName
    local network="$1"
    local info
    if [ -z "${network}" ]; then
        return 0
    fi
    if ! info="$(virsh -c qemu:///system net-info "${network}" 2>/dev/null)"; then
        echo "libvirt network not found, skip: ${network}"
        return 0
    fi

    echo "Cleaning libvirt network: ${network}"
    if [ "$(network_field "${info}" "Active")" = "yes" ]; then
        virsh -c qemu:///system net-destroy "${network}" || true
    fi

    if virsh -c qemu:///system net-info "${network}" >/dev/null 2>&1; then
        virsh -c qemu:///system net-autostart --disable "${network}" >/dev/null 2>&1 || true
        virsh -c qemu:///system net-undefine "${network}" || true
    fi

    if virsh -c qemu:///system net-info "${network}" >/dev/null 2>&1; then
        echo "WARNING: libvirt network still exists after cleanup: ${network}" >&2
    else
        echo "Removed libvirt network: ${network}"
    fi
}

cleanup_libvirt_networks() {
    local network
    local found=0
    while IFS= read -r network; do
        [ -n "${network}" ] || continue
        found=1
        destroy_libvirt_network "${network}"
    done < <(collect_libvirt_networks)
    if [ "${found}" -eq 0 ]; then
        echo "No B62 libvirt networks found to clean."
    fi
}

echo "===== destroy-cluster started $(date --iso-8601=seconds) ====="
destroy_rc=0
if [ ! -f "${CONFIG_K3S}" ]; then
    echo "No configK3s file found at ${CONFIG_K3S}; nothing to destroy."
else
    run_tracked python3 "${SCRIPT_DIR}/k8sTools.py" destroy -d "${CONFIG_K3S}" --keep-temp || destroy_rc=$?
fi

cleanup_libvirt_networks
echo "===== destroy-cluster completed $(date --iso-8601=seconds) ====="
exit "${destroy_rc}"
