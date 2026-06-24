#!/usr/bin/env bash
# Run b63 placement optimization on the compiled manifest.
#
# Inputs: experiment directory containing output/k8s.raw.yaml, b63 inventory,
#         kubeconfig/configK3s paths, and explicit placement parameters.
# Outputs: output/k8s.source.yaml, output/k8s.yaml, optional
#          output/k8s.kube-ovn.yaml, output/network_weights.yaml,
#          output/placement_report.json, and placement.log.
# Side effects: writes only experiment artifacts; it does not deploy resources.
# Context: run after running/compile.sh and before running/build.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
B63_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
source "${SCRIPT_DIR}/lib.sh"

setup_experiment_context "$@"
ensure_kubeconfig
begin_stage_logging "placement"

RAW_MANIFEST="${OUTPUT_DIR}/k8s.raw.yaml"
SOURCE_MANIFEST="${OUTPUT_DIR}/k8s.source.yaml"
DEPLOY_MANIFEST="${OUTPUT_DIR}/k8s.yaml"
NETWORK_WEIGHTS="${OUTPUT_DIR}/network_weights.yaml"
PLACEMENT_REPORT="${OUTPUT_DIR}/placement_report.json"

require_file "${RAW_MANIFEST}"
require_file "${SEED_CLUSTER_INVENTORY_PATH}"

ALGORITHM="${SEED_PLACEMENT_ALGORITHM:-optimized}"
NETWORK_COST_MODE="${SEED_NETWORK_COST_MODE:-ratio}"
ALPHA="${SEED_PLACEMENT_ALPHA:-1.0}"
BETA="${SEED_PLACEMENT_BETA:-1.0}"
IMPROVEMENT_PASSES="${SEED_PLACEMENT_IMPROVEMENT_PASSES:-0}"
PINNING_MODE="${SEED_PLACEMENT_PINNING_MODE:-node-selector}"
SKIP_RESOURCE_REQUEST_INJECTION="${SEED_SKIP_RESOURCE_REQUEST_INJECTION:-false}"

echo "Running placement optimization"
print_test_context
echo "ALGORITHM=${ALGORITHM}"
echo "NETWORK_COST_MODE=${NETWORK_COST_MODE}"
echo "PINNING_MODE=${PINNING_MODE}"
echo "SKIP_RESOURCE_REQUEST_INJECTION=${SKIP_RESOURCE_REQUEST_INJECTION}"
echo "NETWORK_BACKEND=${SEED_NETWORK_BACKEND}"
echo "ATTACHED_CNI_TYPE=${SEED_ATTACHED_CNI_TYPE}"

PLACEMENT_EXTRA_ARGS=()
if [ "${SKIP_RESOURCE_REQUEST_INJECTION}" = "true" ] || [ "${SKIP_RESOURCE_REQUEST_INJECTION}" = "1" ]; then
    PLACEMENT_EXTRA_ARGS+=(--skip-resource-request-injection)
fi

python3 "${B63_DIR}/runPlacementExperiment.py" \
    --output-dir "${OUTPUT_DIR}" \
    --input-manifest "${RAW_MANIFEST}" \
    --source-manifest "${SOURCE_MANIFEST}" \
    --deploy-manifest "${DEPLOY_MANIFEST}" \
    --network-weights "${NETWORK_WEIGHTS}" \
    --report "${PLACEMENT_REPORT}" \
    --config-k3s "${SEED_CONFIG_K3S_PATH}" \
    --kubeconfig "${SEED_KUBECONFIG_PATH}" \
    --inventory "${SEED_CLUSTER_INVENTORY_PATH}" \
    --kvm-config "${SEED_KVM_CONFIG_PATH}" \
    --algorithm "${ALGORITHM}" \
    --network-cost-mode "${NETWORK_COST_MODE}" \
    --alpha "${ALPHA}" \
    --beta "${BETA}" \
    --improvement-passes "${IMPROVEMENT_PASSES}" \
    --fast-greedy-score \
    --pinning-mode "${PINNING_MODE}" \
    --include-master \
    "${PLACEMENT_EXTRA_ARGS[@]}"

RUNTIME_MANIFEST="$(render_runtime_manifest)"

echo "Placement completed. Source deploy manifest: ${DEPLOY_MANIFEST}"
echo "Runtime manifest: ${RUNTIME_MANIFEST}"
