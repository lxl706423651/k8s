#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

setup_experiment_context "${1:-}"
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

if [ ! -f "${PLACEMENT_MAPPING_FILE}" ]; then
    echo "Placement mapping missing; generating ${PLACEMENT_MAPPING_FILE}"
    kubectl get nodes -o json > "${NODES_JSON}"
    generate_as_placement_plan \
        "${TOPOLOGY_FILE}" \
        "${ASSIGNMENT_FILE}" \
        "${NODES_JSON}" \
        "${PLACEMENT_MAPPING_FILE}" \
        "${PLACEMENT_PLAN_FILE}"
fi

require_file "${PLACEMENT_MAPPING_FILE}"
SEED_NODE_LABELS_JSON="$(cat "${PLACEMENT_MAPPING_FILE}")"

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

echo "Compile completed. Output: ${OUTPUT_DIR}/k8s.yaml"
