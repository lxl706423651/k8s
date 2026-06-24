#!/usr/bin/env bash
# Compile a real-topology SeedEMU experiment into Kubernetes artifacts.
#
# Inputs: experiment directory, real_topology_<scale>.txt, assignment.pkl,
#         repository source tree, and explicit compile parameters.
# Outputs: output/k8s.yaml, output/k8s.raw.yaml, output/k8s.kube-ovn.yaml when
#          using Kube-OVN, images/build artifacts, and compile.log.
# Side effects: writes only experiment artifacts; it does not contact the
#               Kubernetes cluster.
# Context: run from the b63 orchestrator after the target cluster config exists.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

setup_experiment_context "$@"
ensure_kubeconfig
begin_stage_logging "compile"

TOPOLOGY_FILE="${SEED_REAL_TOPOLOGY_DIR}/real_topology_${SEED_TOPOLOGY_SIZE}.txt"
ASSIGNMENT_FILE="${SEED_REAL_TOPOLOGY_DIR}/assignment.pkl"
mkdir -p "${OUTPUT_DIR}"

require_file "${TOPOLOGY_FILE}"
require_file "${ASSIGNMENT_FILE}"

echo "[1/1] Running b63 Kubernetes compile"
print_test_context
echo "TOPOLOGY_FILE=${TOPOLOGY_FILE}"
echo "ASSIGNMENT_FILE=${ASSIGNMENT_FILE}"
echo "OUTPUT_DIR=${OUTPUT_DIR}"

cd "${REPO_ROOT}"

env \
    PYTHONPATH="${REPO_ROOT}" \
    PYTHONNOUSERSITE=1 \
    SEED_NAMESPACE="${SEED_NAMESPACE}" \
    SEED_REGISTRY="${SEED_REGISTRY}" \
    SEED_CNI_TYPE="${SEED_CNI_TYPE}" \
    SEED_LOCAL_LINK_CNI_TYPE="${SEED_LOCAL_LINK_CNI_TYPE:-}" \
    SEED_CNI_MASTER_INTERFACE="${SEED_CNI_MASTER_INTERFACE}" \
    SEED_SCHEDULING_STRATEGY="none" \
    SEED_PLACEMENT_MODE="b63-placement" \
    SEED_IMAGE_PULL_POLICY="${SEED_IMAGE_PULL_POLICY}" \
    SEED_OUTPUT_DIR="${OUTPUT_DIR}" \
    SEED_REAL_TOPOLOGY_DIR="${SEED_REAL_TOPOLOGY_DIR}" \
    SEED_TOPOLOGY_SIZE="${SEED_TOPOLOGY_SIZE}" \
    SEED_TOPOLOGY_FILE="${TOPOLOGY_FILE}" \
    SEED_ASSIGNMENT_FILE="${ASSIGNMENT_FILE}" \
    python3 examples/kubernetes/real_topology_k3s_compile.py

cp "${OUTPUT_DIR}/k8s.yaml" "${OUTPUT_DIR}/k8s.raw.yaml"
RUNTIME_MANIFEST="$(render_runtime_manifest)"
echo "Compile completed. Raw manifest: ${OUTPUT_DIR}/k8s.raw.yaml"
echo "Compile completed. Runtime manifest: ${RUNTIME_MANIFEST}"
