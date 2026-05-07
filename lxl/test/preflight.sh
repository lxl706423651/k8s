#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

setup_experiment_context "${1:-}"
ensure_kubeconfig

LOG_FILE="${EXPERIMENT_DIR}/preflight.log"
exec > >(tee "${LOG_FILE}") 2>&1

echo "=== test/preflight ==="
print_test_context

echo "[1/5] Cluster nodes"
kubectl get nodes -o wide

echo "[2/5] Registry health from master and workers"
while IFS=$'\t' read -r node_name node_ip node_role; do
    [ -n "${node_name}" ] || continue
    code="$(ssh_node "${node_ip}" "curl -s -o /dev/null -w '%{http_code}' http://${SEED_REGISTRY}/v2/ || true")"
    printf '%s\t%s\tregistry_http=%s\n' "${node_name}" "${node_ip}" "${code}"
    [ "${code}" = "200" ] || {
        echo "Registry unhealthy from ${node_name} (${node_ip})" >&2
        exit 1
    }
done < <(cluster_nodes_tsv)

echo "[3/5] K3s and kube-system health"
kubectl -n kube-system get pods -o wide

echo "[4/5] Topology inputs"
require_file "${SEED_REAL_TOPOLOGY_DIR}/real_topology_${SEED_TOPOLOGY_SIZE}.txt"
require_file "${SEED_REAL_TOPOLOGY_DIR}/assignment.pkl"
ls -lh "${SEED_REAL_TOPOLOGY_DIR}/real_topology_${SEED_TOPOLOGY_SIZE}.txt" "${SEED_REAL_TOPOLOGY_DIR}/assignment.pkl"

[ -d "${OUTPUT_DIR}" ] || mkdir -p "${OUTPUT_DIR}"
NODES_JSON="${EXPERIMENT_DIR}/nodes.ready.json"
PLACEMENT_MAPPING_FILE="$(placement_mapping_file)"
PLACEMENT_PLAN_FILE="$(placement_plan_file)"

echo "[4.1/5] Generating by-AS placement mapping"
kubectl get nodes -o json > "${NODES_JSON}"
generate_as_placement_plan \
    "${SEED_REAL_TOPOLOGY_DIR}/real_topology_${SEED_TOPOLOGY_SIZE}.txt" \
    "${SEED_REAL_TOPOLOGY_DIR}/assignment.pkl" \
    "${NODES_JSON}" \
    "${PLACEMENT_MAPPING_FILE}" \
    "${PLACEMENT_PLAN_FILE}"
ls -lh "${PLACEMENT_MAPPING_FILE}" "${PLACEMENT_PLAN_FILE}"

echo "[5/5] Namespace baseline"
kubectl get ns "${SEED_NAMESPACE}" >/dev/null 2>&1 && {
    echo "Namespace ${SEED_NAMESPACE} already exists; clean it before running deploy" >&2
    exit 1
} || true

echo "Preflight completed"
