#!/usr/bin/env bash
# Run b63 placement optimization on the raw compiled manifest.
#
# Inputs: output/k8s.raw.yaml from compile.sh, b63 cluster inventory/config, and
#         placement knobs from runB63LargeExperiment.py.
# Outputs: output/k8s.yaml with selected placement, output/network_weights.yaml,
#          and output/placement_report.json with static baseline evaluation.
# Side effects: writes files only under the experiment output directory.
# Context: run after compile.sh and before build.sh/deploy.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

setup_experiment_context "$@"
begin_stage_logging "placement"

RAW_MANIFEST="${OUTPUT_DIR}/k8s.raw.yaml"
SOURCE_MANIFEST="${OUTPUT_DIR}/k8s.source.yaml"
DEPLOY_MANIFEST="${OUTPUT_DIR}/k8s.yaml"
NETWORK_WEIGHTS="${OUTPUT_DIR}/network_weights.yaml"
PLACEMENT_REPORT="${OUTPUT_DIR}/placement_report.json"

require_file "${RAW_MANIFEST}"
require_file "${B63_KVM_CONFIG_PATH}"

PLACEMENT_ALGORITHM="${B63_PLACEMENT_ALGORITHM:-optimized}"
NETWORK_COST_MODE="${B63_NETWORK_COST_MODE:-ratio}"
PLACEMENT_ALPHA="${B63_PLACEMENT_ALPHA:-1.0}"
PLACEMENT_BETA="${B63_PLACEMENT_BETA:-1.0}"
PLACEMENT_POD_BALANCE_WEIGHT="${B63_PLACEMENT_POD_BALANCE_WEIGHT:-8.0}"
PLACEMENT_VLAN_PARENT_BALANCE_WEIGHT="${B63_PLACEMENT_VLAN_PARENT_BALANCE_WEIGHT:-4.0}"
PLACEMENT_POD_CAP_SLACK="${B63_PLACEMENT_POD_CAP_SLACK:-1.0}"
PLACEMENT_VLAN_PARENT_CAP_SLACK="${B63_PLACEMENT_VLAN_PARENT_CAP_SLACK:-1.50}"
PLACEMENT_CAPACITY_BALANCE_RATIO="${B63_PLACEMENT_CAPACITY_BALANCE_RATIO:-1.0}"
PLACEMENT_RESOURCE_OVERLOAD_PENALTY="${B63_PLACEMENT_RESOURCE_OVERLOAD_PENALTY:-0.0}"
PLACEMENT_CAPACITY_HARD_CAP_PENALTY="${B63_PLACEMENT_CAPACITY_HARD_CAP_PENALTY:-100000.0}"
PLACEMENT_IMPROVEMENT_PASSES="${B63_PLACEMENT_IMPROVEMENT_PASSES:-0}"
PLACEMENT_FAST_GREEDY_SCORE="${B63_PLACEMENT_FAST_GREEDY_SCORE:-false}"
PLACEMENT_PINNING_MODE="${B63_PLACEMENT_PINNING_MODE:-node-selector}"
PLACEMENT_INCLUDE_MASTER="${B63_PLACEMENT_INCLUDE_MASTER:-true}"
PLACEMENT_SELECTED_ONLY="${B63_PLACEMENT_SELECTED_ONLY:-false}"
INJECT_RESOURCE_REQUESTS="${B63_INJECT_RESOURCE_REQUESTS:-false}"

echo "EXPERIMENT_DIR=${EXPERIMENT_DIR}"
echo "RAW_MANIFEST=${RAW_MANIFEST}"
echo "DEPLOY_MANIFEST=${DEPLOY_MANIFEST}"
echo "PLACEMENT_REPORT=${PLACEMENT_REPORT}"
echo "PLACEMENT_ALGORITHM=${PLACEMENT_ALGORITHM}"
echo "NETWORK_COST_MODE=${NETWORK_COST_MODE}"
echo "PLACEMENT_PINNING_MODE=${PLACEMENT_PINNING_MODE}"
echo "PLACEMENT_POD_BALANCE_WEIGHT=${PLACEMENT_POD_BALANCE_WEIGHT}"
echo "PLACEMENT_VLAN_PARENT_BALANCE_WEIGHT=${PLACEMENT_VLAN_PARENT_BALANCE_WEIGHT}"
echo "PLACEMENT_RESOURCE_OVERLOAD_PENALTY=${PLACEMENT_RESOURCE_OVERLOAD_PENALTY}"
echo "PLACEMENT_CAPACITY_HARD_CAP_PENALTY=${PLACEMENT_CAPACITY_HARD_CAP_PENALTY}"
echo "PLACEMENT_FAST_GREEDY_SCORE=${PLACEMENT_FAST_GREEDY_SCORE}"
echo "PLACEMENT_INCLUDE_MASTER=${PLACEMENT_INCLUDE_MASTER}"
echo "PLACEMENT_SELECTED_ONLY=${PLACEMENT_SELECTED_ONLY}"
echo "INJECT_RESOURCE_REQUESTS=${INJECT_RESOURCE_REQUESTS}"

cmd=(
    python3 "${B63_DIR}/runPlacementExperiment.py"
    --output-dir "${OUTPUT_DIR}"
    --input-manifest "${RAW_MANIFEST}"
    --source-manifest "${SOURCE_MANIFEST}"
    --deploy-manifest "${DEPLOY_MANIFEST}"
    --network-weights "${NETWORK_WEIGHTS}"
    --report "${PLACEMENT_REPORT}"
    --kvm-config "${B63_KVM_CONFIG_PATH}"
    --config-k3s "${B63_CONFIG_K3S_PATH}"
    --kubeconfig "${B63_KUBECONFIG_PATH}"
    --inventory "${B63_INVENTORY_PATH}"
    --algorithm "${PLACEMENT_ALGORITHM}"
    --network-cost-mode "${NETWORK_COST_MODE}"
    --alpha "${PLACEMENT_ALPHA}"
    --beta "${PLACEMENT_BETA}"
    --pod-balance-weight "${PLACEMENT_POD_BALANCE_WEIGHT}"
    --vlan-parent-balance-weight "${PLACEMENT_VLAN_PARENT_BALANCE_WEIGHT}"
    --pod-cap-slack "${PLACEMENT_POD_CAP_SLACK}"
    --vlan-parent-cap-slack "${PLACEMENT_VLAN_PARENT_CAP_SLACK}"
    --capacity-balance-ratio "${PLACEMENT_CAPACITY_BALANCE_RATIO}"
    --resource-overload-penalty "${PLACEMENT_RESOURCE_OVERLOAD_PENALTY}"
    --capacity-hard-cap-penalty "${PLACEMENT_CAPACITY_HARD_CAP_PENALTY}"
    --improvement-passes "${PLACEMENT_IMPROVEMENT_PASSES}"
    --pinning-mode "${PLACEMENT_PINNING_MODE}"
)

if truthy "${INJECT_RESOURCE_REQUESTS}"; then
    cmd+=(--inject-resource-requests)
fi

if truthy "${PLACEMENT_INCLUDE_MASTER}"; then
    cmd+=(--include-master)
fi

if truthy "${PLACEMENT_FAST_GREEDY_SCORE}"; then
    cmd+=(--fast-greedy-score)
fi

if truthy "${PLACEMENT_SELECTED_ONLY}"; then
    cmd+=(--selected-only)
fi

"${cmd[@]}"

if [ "${PLACEMENT_ALGORITHM}" = "kubernetes-default-scheduler" ]; then
    echo "Warning: kubernetes-default-scheduler writes no hard placement; use buildBaselineAllNodePreload.sh for all-node preload dynamic baselines." >&2
fi
