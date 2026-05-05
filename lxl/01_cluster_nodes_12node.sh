#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -f "${SCRIPT_DIR}/env_12node.sh" ]; then
    # shellcheck disable=SC1091
    source "${SCRIPT_DIR}/env_12node.sh"
fi

seed_load_nodes_from_inventory() {
    local inventory_path="${SEED_CLUSTER_INVENTORY_PATH:-}"
    if [ -z "${inventory_path}" ] || [ ! -f "${inventory_path}" ]; then
        return 1
    fi

    local parsed
    parsed="$(python3 - "${inventory_path}" <<'PY'
import sys
import yaml

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as fh:
    data = yaml.safe_load(fh) or {}

nodes = data.get("nodes", []) or []
for node in nodes:
    if not isinstance(node, dict):
        continue
    name = str(node.get("name", "")).strip()
    role = str(node.get("role", "")).strip()
    ip = str(node.get("management_ip", "")).strip()
    if name and ip:
        print(f"{name}\t{role}\t{ip}")
PY
)"

    [ -n "${parsed}" ] || return 1

    SEED_NODE_NAMES=()
    SEED_NODE_IPS=()
    SEED_WORKER_NODE_NAMES=()
    SEED_WORKER_NODE_IPS=()
    SEED_MASTER_NODE_NAME=""
    SEED_MASTER_NODE_IP=""

    while IFS=$'\t' read -r name role ip; do
        [ -n "${name}" ] || continue
        SEED_NODE_NAMES+=("${name}")
        SEED_NODE_IPS+=("${ip}")
        if [ "${role}" = "master" ]; then
            SEED_MASTER_NODE_NAME="${name}"
            SEED_MASTER_NODE_IP="${ip}"
        else
            SEED_WORKER_NODE_NAMES+=("${name}")
            SEED_WORKER_NODE_IPS+=("${ip}")
        fi
    done <<< "${parsed}"
}

seed_load_nodes_from_env_fallback() {
    SEED_NODE_NAMES=("seed-k3s-master")
    SEED_NODE_IPS=("${SEED_K3S_MASTER_IP}")
    SEED_WORKER_NODE_NAMES=()
    SEED_WORKER_NODE_IPS=()
    SEED_MASTER_NODE_NAME="seed-k3s-master"
    SEED_MASTER_NODE_IP="${SEED_K3S_MASTER_IP}"

    local idx ip_var ip
    for idx in $(seq 1 11); do
        ip_var="SEED_K3S_WORKER${idx}_IP"
        ip="${!ip_var:-}"
        [ -n "${ip}" ] || continue
        SEED_WORKER_NODE_NAMES+=("seed-k3s-worker${idx}")
        SEED_WORKER_NODE_IPS+=("${ip}")
    done

    SEED_NODE_NAMES+=("${SEED_WORKER_NODE_NAMES[@]}")
    SEED_NODE_IPS+=("${SEED_WORKER_NODE_IPS[@]}")
}

seed_load_cluster_nodes() {
    if ! seed_load_nodes_from_inventory; then
        seed_load_nodes_from_env_fallback
    fi
}

seed_print_cluster_nodes() {
    local i
    echo "Cluster nodes:"
    for i in "${!SEED_NODE_NAMES[@]}"; do
        echo "  ${SEED_NODE_NAMES[$i]} -> ${SEED_NODE_IPS[$i]}"
    done
}
