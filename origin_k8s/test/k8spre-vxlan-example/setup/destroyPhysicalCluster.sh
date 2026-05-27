#!/usr/bin/env bash
# Remove a physical SeedEMU K3s cluster and its optional fabric backend.
#
# Args:
#   $1: Optional configK3s.yaml path. Defaults to ./configK3s.yaml.
#
# Side effects:
#   Runs K3s uninstall scripts on configured nodes, removes the local registry
#   container on the master, deletes generated kubeconfig/inventory files, and
#   removes the configured linux-vxlan or Kube-OVN fabric if present.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_PATH="${1:-./configK3s.yaml}"
HELPER="${SCRIPT_DIR}/manageK3sConfig.py"

if [ ! -s "$CONFIG_PATH" ]; then
    echo "Missing config file: $CONFIG_PATH" >&2
    exit 1
fi

eval "$(python3 "$HELPER" --config "$CONFIG_PATH" shell-vars)"

sshOptions() {
    local ssh_key="$1"
    printf '%s\n' \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        -o LogLevel=ERROR \
        -o ConnectTimeout=10 \
        -i "$ssh_key"
}

runNodeRootScript() {
    local name="$1"
    local ip="$2"
    local connection="$3"
    local ssh_user="$4"
    local ssh_key="$5"
    local script="$6"

    echo "[cluster-clean] ${name} (${ip})"
    if [ "$connection" = "local" ]; then
        sudo -n bash -s <<<"$script"
        return
    fi

    # shellcheck disable=SC2046
    ssh $(sshOptions "$ssh_key") "${ssh_user}@${ip}" "sudo -n bash -s" <<<"$script"
}

uninstall_node='
set -euo pipefail
if [ -x /usr/local/bin/k3s-agent-uninstall.sh ]; then
    /usr/local/bin/k3s-agent-uninstall.sh || true
fi
if [ -x /usr/local/bin/k3s-uninstall.sh ]; then
    /usr/local/bin/k3s-uninstall.sh || true
fi
'

remove_registry='
set -euo pipefail
if command -v docker >/dev/null 2>&1; then
    docker rm -f registry >/dev/null 2>&1 || true
fi
'

echo "Cleaning physical K3s cluster from: $CONFIG_PATH"

if [ "${fabricType}" = "ovn" ] || [ "${fabricType}" = "kube-ovn" ]; then
    if [ -x "${SCRIPT_DIR}/ovn/cleanKubeOvnFabric.sh" ]; then
        bash "${SCRIPT_DIR}/ovn/cleanKubeOvnFabric.sh" "$CONFIG_PATH" || true
    else
        echo "warning: OVN cleanup script is not present in this generated setup directory" >&2
    fi
fi

while IFS=$'\t' read -r name role ip mac vcpus memory_mb disk_gb; do
    eval "$(python3 "$HELPER" --config "$CONFIG_PATH" node-ssh-vars --name "$name")"
    if [ "$role" = "master" ]; then
        runNodeRootScript "$name" "$ip" "$nodeConnection" "$nodeSshUser" "$nodeSshKey" "$remove_registry"
    fi
    runNodeRootScript "$name" "$ip" "$nodeConnection" "$nodeSshUser" "$nodeSshKey" "$uninstall_node"
done < <(python3 "$HELPER" --config "$CONFIG_PATH" nodes-tsv)

if [ "${fabricType}" = "linux-vxlan" ]; then
    if [ -x "${SCRIPT_DIR}/vxlan/cleanLinuxVxlanFabric.sh" ]; then
        bash "${SCRIPT_DIR}/vxlan/cleanLinuxVxlanFabric.sh" "$CONFIG_PATH" || true
    else
        echo "warning: VXLAN cleanup script is not present in this generated setup directory" >&2
    fi
fi

rm -f "$outputKubeconfig" "$outputInventory"
echo "Removed generated kubeconfig/inventory:"
echo "  $outputKubeconfig"
echo "  $outputInventory"
echo "Physical K3s cluster cleanup finished."
