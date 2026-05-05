#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "${SCRIPT_DIR}/env_12node.sh"
source "${SCRIPT_DIR}/01_cluster_nodes_12node.sh"
seed_load_cluster_nodes

SSH_OPTS=(
    -i "${SEED_K3S_SSH_KEY}"
    -o StrictHostKeyChecking=no
    -o UserKnownHostsFile=/dev/null
    -o BatchMode=yes
    -o ConnectTimeout=5
)

echo "NODE IP HASH_MAX FDB_COUNT VETH_COUNT"

for i in "${!SEED_NODE_IPS[@]}"; do
    node_name="${SEED_NODE_NAMES[$i]}"
    node_ip="${SEED_NODE_IPS[$i]}"

    output="$(
        ssh "${SSH_OPTS[@]}" "${SEED_K3S_USER}@${node_ip}" '
            set -Eeuo pipefail
            if ! sudo -n test -d /sys/class/net/cni0/bridge; then
                echo "CNI0_MISSING"
                exit 0
            fi
            hash_max="$(sudo -n cat /sys/class/net/cni0/bridge/hash_max 2>/dev/null || echo NA)"
            fdb_count="$(sudo -n bridge fdb show br cni0 2>/dev/null | wc -l | tr -d " ")"
            veth_count="$(sudo -n ip link show type veth 2>/dev/null | wc -l | tr -d " ")"
            printf "%s %s %s\n" "${hash_max}" "${fdb_count}" "${veth_count}"
        ' 2>/dev/null || true
    )"

    if [[ "${output}" == "CNI0_MISSING" || -z "${output}" ]]; then
        echo "${node_name} ${node_ip} CNI0_MISSING CNI0_MISSING CNI0_MISSING"
        continue
    fi

    read -r hash_max fdb_count veth_count <<< "${output}"
    echo "${node_name} ${node_ip} ${hash_max:-NA} ${fdb_count:-NA} ${veth_count:-NA}"
done
