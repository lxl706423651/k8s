#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "${SCRIPT_DIR}/env_9node.sh"
source "${SCRIPT_DIR}/01_cluster_nodes_9node.sh"
seed_load_cluster_nodes

for ip in "${SEED_NODE_IPS[@]}"; do
    echo "===== ${ip} ====="
    ssh -i "${SEED_K3S_SSH_KEY}" \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        "${SEED_K3S_USER}@${ip}" '
            sudo -n mkdir -p /etc/cni/net.d &&
            sudo -n rm -rf /etc/cni/net.d/multus.d &&
            sudo -n ln -s /var/lib/rancher/k3s/agent/etc/cni/net.d/multus.d /etc/cni/net.d/multus.d &&
            sudo -n ls -ld /etc/cni/net.d/multus.d &&
            sudo -n test -f /etc/cni/net.d/multus.d/multus.kubeconfig
        '
    echo
done
