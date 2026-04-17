#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "${SCRIPT_DIR}/env_9node.sh"
source "${SCRIPT_DIR}/01_cluster_nodes_9node.sh"
seed_load_cluster_nodes

SSH_OPTS=(
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
  -o LogLevel=ERROR
  -o BatchMode=yes
  -o ConnectTimeout=10
)

echo -e "NODE\tIP\tIMAGE_COUNT"
for i in "${!SEED_NODE_NAMES[@]}"; do
    node_name="${SEED_NODE_NAMES[$i]}"
    node_ip="${SEED_NODE_IPS[$i]}"
    count="$(
        ssh "${SSH_OPTS[@]}" "${SEED_K3S_USER}@${node_ip}" \
            "sudo -n k3s ctr images list 2>/dev/null | tail -n +2 | wc -l" 2>/dev/null || echo "ERROR"
    )"
    echo -e "${node_name}\t${node_ip}\t${count}"
done
