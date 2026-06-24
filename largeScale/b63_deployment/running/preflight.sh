#!/usr/bin/env bash
# Validate b63 cluster readiness and experiment inputs before build/deploy.
#
# Inputs: experiment directory, generated b63 kubeconfig/inventory, topology
#         files, and explicit cluster/preflight parameters.
# Outputs: preflight.log under the experiment directory.
# Side effects: verifies cluster state and, for Kube-OVN secondary-link runs,
# rewrites stale node Multus default delegates back to flannel.
# Context: run after placement and before image build/deploy.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

setup_experiment_context "$@"
ensure_kubeconfig

LOG_FILE="${EXPERIMENT_DIR}/preflight.log"
exec > >(tee "${LOG_FILE}") 2>&1

echo "=== preflight ==="
print_test_context

echo "[1/6] Cluster nodes"
kubectl get nodes -o wide

echo "[2/6] Registry health from master and workers"
while IFS=$'\t' read -r node_name node_ip node_role; do
    [ -n "${node_name}" ] || continue
    code="$(ssh_node "${node_ip}" "curl -s -o /dev/null -w '%{http_code}' http://${SEED_REGISTRY}/v2/ || true")"
    printf '%s\t%s\tregistry_http=%s\n' "${node_name}" "${node_ip}" "${code}"
    [ "${code}" = "200" ] || {
        echo "Registry unhealthy from ${node_name} (${node_ip})" >&2
        exit 1
    }
done < <(cluster_nodes_tsv)

echo "[3/6] K3s and kube-system health"
kubectl -n kube-system get pods -o wide

echo "[4/6] Multus default delegate"
ensure_multus_flannel_default_delegate

echo "[5/6] Topology inputs"
require_file "${SEED_REAL_TOPOLOGY_DIR}/real_topology_${SEED_TOPOLOGY_SIZE}.txt"
require_file "${SEED_REAL_TOPOLOGY_DIR}/assignment.pkl"
ls -lh "${SEED_REAL_TOPOLOGY_DIR}/real_topology_${SEED_TOPOLOGY_SIZE}.txt" "${SEED_REAL_TOPOLOGY_DIR}/assignment.pkl"

echo "[6/6] Namespace baseline"
kubectl get ns "${SEED_NAMESPACE}" >/dev/null 2>&1 && {
    echo "Namespace ${SEED_NAMESPACE} already exists; clean it before running deploy" >&2
    exit 1
} || true

echo "Preflight completed"
