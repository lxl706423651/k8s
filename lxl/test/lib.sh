#!/usr/bin/env bash
set -euo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LXL_DIR="$(cd "${TEST_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${LXL_DIR}/.." && pwd)"

source "${TEST_DIR}/config/cluster.sh"

SEEDPY310_CONDA_SH="${SEEDPY310_CONDA_SH:-$HOME/anaconda3/etc/profile.d/conda.sh}"
SEEDPY310_ENV_NAME="${SEEDPY310_ENV_NAME:-seedpy310}"

derive_topology_size_from_experiment_dir() {
    local expdir_basename="$1"
    if [[ "${expdir_basename}" =~ _([0-9]+)$ ]]; then
        printf '%s\n' "${BASH_REMATCH[1]}"
        return 0
    fi
    return 1
}

setup_experiment_context() {
    local expdir_input="${1:-}"
    if [ -z "${expdir_input}" ]; then
        echo "Usage: $0 <experiment_dir>" >&2
        exit 2
    fi

    mkdir -p "${expdir_input}"
    export EXPERIMENT_DIR
    EXPERIMENT_DIR="$(cd "${expdir_input}" && pwd)"
    export LOG_BASE_DIR
    LOG_BASE_DIR="$(dirname "${EXPERIMENT_DIR}")"
    export OUTPUT_DIR="${EXPERIMENT_DIR}/output"

    if [ -f "${EXPERIMENT_DIR}/topology_size" ]; then
        export SEED_TOPOLOGY_SIZE
        SEED_TOPOLOGY_SIZE="$(tr -d ' \n\r' < "${EXPERIMENT_DIR}/topology_size")"
    else
        export SEED_TOPOLOGY_SIZE
        SEED_TOPOLOGY_SIZE="$(derive_topology_size_from_experiment_dir "$(basename "${EXPERIMENT_DIR}")")" || {
            echo "Cannot derive topology size from experiment dir name: ${EXPERIMENT_DIR}" >&2
            echo "Expected suffix like *_1078 or create ${EXPERIMENT_DIR}/topology_size" >&2
            exit 1
        }
    fi

    export KUBECONFIG="${REPO_ROOT}/output/kubeconfigs/${SEED_K3S_CLUSTER_NAME}.yaml"
}

ts() {
    date +"%F %T"
}

stage_log_file() {
    local stage="$1"
    printf '%s/%s.log\n' "${EXPERIMENT_DIR}" "${stage}"
}

begin_stage_logging() {
    local stage="$1"
    local log_file
    log_file="$(stage_log_file "${stage}")"
    exec > >(tee -a "${log_file}") 2>&1
    echo "=== ${stage} ==="
    echo "Log file: ${log_file}"
}

load_build_config() {
    source "${TEST_DIR}/config/build.sh"
}

load_deploy_config() {
    source "${TEST_DIR}/config/deploy.sh"
}

require_file() {
    local path="$1"
    [ -f "${path}" ] || {
        echo "Required file not found: ${path}" >&2
        exit 1
    }
}

ensure_kubeconfig() {
    require_file "${KUBECONFIG}"
}

placement_mapping_file() {
    printf '%s/%s\n' "${EXPERIMENT_DIR}" "placement_expected.json"
}

placement_plan_file() {
    printf '%s/%s\n' "${EXPERIMENT_DIR}" "placement_plan.json"
}

cluster_nodes_tsv() {
    python3 - <<'PY' "${SEED_CLUSTER_INVENTORY_PATH}"
import sys, yaml
path = sys.argv[1]
with open(path, "r", encoding="utf-8") as fh:
    data = yaml.safe_load(fh)
for node in data.get("nodes", []):
    print(f"{node['name']}\t{node['management_ip']}\t{node.get('role', '')}")
PY
}

seed_load_cluster_nodes() {
    SEED_NODE_NAMES=()
    SEED_NODE_IPS=()
    SEED_NODE_ROLES=()
    SEED_MASTER_NODE_NAME=""
    SEED_MASTER_NODE_IP=""

    while IFS=$'\t' read -r node_name node_ip node_role; do
        [ -n "${node_name}" ] || continue
        SEED_NODE_NAMES+=("${node_name}")
        SEED_NODE_IPS+=("${node_ip}")
        SEED_NODE_ROLES+=("${node_role}")
        if [ "${node_role}" = "master" ] || [ "${node_role}" = "control-plane" ]; then
            SEED_MASTER_NODE_NAME="${node_name}"
            SEED_MASTER_NODE_IP="${node_ip}"
        fi
    done < <(cluster_nodes_tsv)

    if [ -z "${SEED_MASTER_NODE_IP}" ] && [ "${#SEED_NODE_IPS[@]}" -gt 0 ]; then
        SEED_MASTER_NODE_NAME="${SEED_NODE_NAMES[0]}"
        SEED_MASTER_NODE_IP="${SEED_NODE_IPS[0]}"
    fi
}

seed_print_cluster_nodes() {
    echo "Cluster nodes:"
    for i in "${!SEED_NODE_NAMES[@]}"; do
        printf '  %s -> %s\n' "${SEED_NODE_NAMES[$i]}" "${SEED_NODE_IPS[$i]}"
    done
}

ssh_node() {
    local ip="$1"
    shift
    ssh -i "${SEED_K3S_SSH_KEY}" \
        -n \
        -o BatchMode=yes \
        -o ConnectTimeout=8 \
        -o StrictHostKeyChecking=no \
        "${SEED_K3S_USER}@${ip}" "$@"
}

run_in_seedpy310() {
    require_file "${SEEDPY310_CONDA_SH}"
    # shellcheck disable=SC1090
    source "${SEEDPY310_CONDA_SH}"
    conda activate "${SEEDPY310_ENV_NAME}"
    "$@"
}

generate_as_placement_plan() {
    local topology_file="$1"
    local assignment_file="$2"
    local nodes_json_file="$3"
    local mapping_file="$4"
    local plan_file="$5"
    run_in_seedpy310 python3 "${LXL_DIR}/seed_k8s_plan_real_topology_by_as.py" \
        "${topology_file}" \
        "${assignment_file}" \
        "${nodes_json_file}" \
        "${mapping_file}" \
        "${plan_file}"
}

print_test_context() {
    echo "EXPERIMENT_DIR=${EXPERIMENT_DIR}"
    echo "SEED_TOPOLOGY_SIZE=${SEED_TOPOLOGY_SIZE}"
    echo "SEED_CLUSTER_INVENTORY_PATH=${SEED_CLUSTER_INVENTORY_PATH}"
    echo "KUBECONFIG=${KUBECONFIG}"
    echo "SEED_NAMESPACE=${SEED_NAMESPACE}"
}
