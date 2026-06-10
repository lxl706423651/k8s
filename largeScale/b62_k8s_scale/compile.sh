#!/usr/bin/env bash
# Compile the selected real topology into Kubernetes manifests for the current
# B62 run directory.
#
# Inputs: run directory, assignment.yaml, real_topology_<size>.txt, assignment.pkl,
#         and the current cluster inventory/kubeconfig.
# Outputs: output/k8s.yaml, output/k8s.kube-ovn.yaml when using Kube-OVN,
#          plus placement_expected.json and placement_plan.json.
# Side effects: reads cluster node readiness through kubectl; does not deploy
#               Kubernetes workload resources.
# Context: run from this B62 directory after the K3s cluster is built.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

setup_experiment_context "${1:-}"
SEED_SCHEDULING_STRATEGY=by_as_hard
SEED_PLACEMENT_MODE=by_as_hard
SEED_IMAGE_PULL_POLICY=IfNotPresent
export SEED_SCHEDULING_STRATEGY SEED_PLACEMENT_MODE SEED_IMAGE_PULL_POLICY

ensure_kubeconfig
begin_stage_logging "compile"

TOPOLOGY_FILE="${SEED_REAL_TOPOLOGY_DIR}/real_topology_${SEED_TOPOLOGY_SIZE}.txt"
ASSIGNMENT_FILE="${SEED_REAL_TOPOLOGY_DIR}/assignment.pkl"
NODES_JSON="${EXPERIMENT_DIR}/nodes.ready.json"
PLACEMENT_MAPPING_FILE="$(placement_mapping_file)"
PLACEMENT_PLAN_FILE="$(placement_plan_file)"
mkdir -p "${OUTPUT_DIR}"

require_file "${TOPOLOGY_FILE}"
require_file "${ASSIGNMENT_FILE}"

case "${SEED_SCHEDULING_STRATEGY}" in
    by_as_hard|custom)
        ;;
    *)
        echo "Unsupported SEED_SCHEDULING_STRATEGY=${SEED_SCHEDULING_STRATEGY} for standalone test flow." >&2
        echo "This flow requires hard node placement so preload and batched deploy can work per node." >&2
        echo "Use by_as_hard (recommended) or custom." >&2
        exit 1
        ;;
esac

echo "[1/3] Capturing current Ready node set"
kubectl get nodes -o json > "${NODES_JSON}"
ls -lh "${NODES_JSON}"

echo "[2/3] Generating by-AS hard placement mapping"
generate_as_placement_plan \
    "${TOPOLOGY_FILE}" \
    "${ASSIGNMENT_FILE}" \
    "${NODES_JSON}" \
    "${PLACEMENT_MAPPING_FILE}" \
    "${PLACEMENT_PLAN_FILE}"
ls -lh "${PLACEMENT_MAPPING_FILE}" "${PLACEMENT_PLAN_FILE}"

require_file "${PLACEMENT_MAPPING_FILE}"
SEED_NODE_LABELS_JSON="$(cat "${PLACEMENT_MAPPING_FILE}")"

echo "[3/3] Running Kubernetes compile"
echo "EXPERIMENT_DIR=${EXPERIMENT_DIR}"
echo "SEED_TOPOLOGY_SIZE=${SEED_TOPOLOGY_SIZE}"
echo "SEED_CLUSTER_INVENTORY_PATH=${SEED_CLUSTER_INVENTORY_PATH}"
echo "KUBECONFIG=${KUBECONFIG}"
echo "SEED_NAMESPACE=${SEED_NAMESPACE}"
echo "Current Dir: ${REPO_ROOT}"
echo "Placement mapping: ${PLACEMENT_MAPPING_FILE}"

cd "${REPO_ROOT}"

run_in_seedpy310 env \
    PYTHONPATH="${REPO_ROOT}" \
    PYTHONNOUSERSITE=1 \
    SEED_NAMESPACE="${SEED_NAMESPACE}" \
    SEED_REGISTRY="${SEED_REGISTRY}" \
    SEED_CNI_TYPE="${SEED_CNI_TYPE}" \
    SEED_LOCAL_LINK_CNI_TYPE="${SEED_LOCAL_LINK_CNI_TYPE:-}" \
    SEED_CNI_MASTER_INTERFACE="${SEED_CNI_MASTER_INTERFACE}" \
    SEED_SCHEDULING_STRATEGY="${SEED_SCHEDULING_STRATEGY}" \
    SEED_PLACEMENT_MODE="${SEED_PLACEMENT_MODE}" \
    SEED_IMAGE_PULL_POLICY="${SEED_IMAGE_PULL_POLICY}" \
    SEED_NODE_LABELS_JSON="${SEED_NODE_LABELS_JSON}" \
    SEED_OUTPUT_DIR="${OUTPUT_DIR}" \
    SEED_REAL_TOPOLOGY_DIR="${SEED_REAL_TOPOLOGY_DIR}" \
    SEED_TOPOLOGY_SIZE="${SEED_TOPOLOGY_SIZE}" \
    SEED_TOPOLOGY_FILE="${TOPOLOGY_FILE}" \
    SEED_ASSIGNMENT_FILE="${ASSIGNMENT_FILE}" \
    python3 examples/kubernetes/real_topology_k3s_compile.py

RUNTIME_MANIFEST="$(render_runtime_manifest)"
echo "Compile completed. Source manifest: ${OUTPUT_DIR}/k8s.yaml"
echo "Compile completed. Runtime manifest: ${RUNTIME_MANIFEST}"
