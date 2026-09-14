#!/usr/bin/env bash
# Common b63 running-stage library.
#
# Inputs: explicit --experiment-dir/--kubeconfig/--inventory/--config-k3s and
#         stage-specific knobs from runB63LargeExperiment.py.
# Outputs: exported SEED_* and B62_* compatibility variables consumed by the
#          copied b62 running scripts, plus per-stage log helpers.
# Side effects: creates the experiment/output directories and may later run
#               kubectl or SSH commands through helper functions.
# Context: sourced by scripts under largeScale/b63_deployment/running.
set -euo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
B63_DIR="$(cd "${TEST_DIR}/.." && pwd)"
PLACEMENT_PLANNER="${SEED_PLACEMENT_PLANNER:-${TEST_DIR}/seed_k8s_plan_real_topology_by_as.py}"

SEEDPY310_CONDA_SH="${SEEDPY310_CONDA_SH:-$HOME/anaconda3/etc/profile.d/conda.sh}"
SEEDPY310_ENV_NAME="${SEEDPY310_ENV_NAME:-seedpy310}"

find_repo_root() {
    local start="$1"
    local candidate
    candidate="$(cd "${start}" && pwd)"
    while true; do
        if [ -f "${candidate}/setup.py" ] && [ -d "${candidate}/seedemu" ]; then
            printf '%s\n' "${candidate}"
            return 0
        fi
        [ "${candidate}" != "/" ] || break
        candidate="$(dirname "${candidate}")"
    done
    echo "Cannot find SeedEMU repository root above ${start}" >&2
    return 1
}

resolve_path() {
    local value="$1"
    local base="${2:-${B63_DIR}}"
    python3 - "$value" "$base" <<'PY'
import os
import sys
from pathlib import Path

value = Path(os.path.expandvars(os.path.expanduser(sys.argv[1])))
base = Path(sys.argv[2]).expanduser().resolve()
print(value.resolve() if value.is_absolute() else (base / value).resolve())
PY
}

normalize_network_value() {
    printf '%s\n' "$1" | tr '[:upper:]' '[:lower:]' | sed 's/_/-/g'
}

truthy() {
    case "$(printf '%s\n' "$1" | tr '[:upper:]' '[:lower:]')" in
        1|true|yes|y|on) return 0 ;;
        *) return 1 ;;
    esac
}

setup_experiment_context() {
    # Parse b63 explicit stage args and export the compatibility contract used
    # by copied b62 scripts. Positional <experiment_dir> remains accepted for
    # manual debugging, but the documented b63 path should use named args.
    local experiment_dir=""
    local source_root=""
    local topology_size=""
    local topology_dir=""
    local namespace=""
    local kubeconfig=""
    local inventory=""
    local config_k3s=""
    local kvm_config=""
    local registry_host=""
    local registry_port=""
    local registry=""
    local k3s_user="ubuntu"
    local k3s_ssh_key="${HOME}/.ssh/id_ed25519"
    local cni_type="macvlan"
    local cni_master_interface="ens2"
    local network_backend="macvlan"
    local attached_cni_type="macvlan"
    local image_pull_policy="IfNotPresent"

    while [ "$#" -gt 0 ]; do
        case "$1" in
            --experiment-dir) experiment_dir="$2"; shift 2 ;;
            --source-root) source_root="$2"; shift 2 ;;
            --topology-size) topology_size="$2"; shift 2 ;;
            --topology-dir) topology_dir="$2"; shift 2 ;;
            --namespace) namespace="$2"; shift 2 ;;
            --kubeconfig) kubeconfig="$2"; shift 2 ;;
            --inventory) inventory="$2"; shift 2 ;;
            --config-k3s) config_k3s="$2"; shift 2 ;;
            --kvm-config) kvm_config="$2"; shift 2 ;;
            --registry-host) registry_host="$2"; shift 2 ;;
            --registry-port) registry_port="$2"; shift 2 ;;
            --registry) registry="$2"; shift 2 ;;
            --k3s-user) k3s_user="$2"; shift 2 ;;
            --k3s-ssh-key) k3s_ssh_key="$2"; shift 2 ;;
            --cni-type) cni_type="$2"; shift 2 ;;
            --cni-master-interface) cni_master_interface="$2"; shift 2 ;;
            --network-backend) network_backend="$2"; shift 2 ;;
            --attached-cni-type) attached_cni_type="$2"; shift 2 ;;
            --image-pull-policy) image_pull_policy="$2"; shift 2 ;;

            --clean-namespace) CLEAN_NAMESPACE="$2"; shift 2 ;;
            --clean-check-interval-seconds) CLEAN_CHECK_INTERVAL_SECONDS="$2"; shift 2 ;;
            --clean-timeout-seconds) CLEAN_TIMEOUT_SECONDS="$2"; shift 2 ;;
            --clean-force-finalizer-cleanup) CLEAN_FORCE_FINALIZER_CLEANUP="$2"; shift 2 ;;
            --clean-auto-finalize-stuck-namespace) CLEAN_AUTO_FINALIZE_STUCK_NAMESPACE="$2"; shift 2 ;;
            --clean-auto-finalize-after-seconds) CLEAN_AUTO_FINALIZE_AFTER_SECONDS="$2"; shift 2 ;;

            --placement-algorithm) B63_PLACEMENT_ALGORITHM="$2"; shift 2 ;;
            --network-cost-mode) B63_NETWORK_COST_MODE="$2"; shift 2 ;;
            --placement-alpha) B63_PLACEMENT_ALPHA="$2"; shift 2 ;;
            --placement-beta) B63_PLACEMENT_BETA="$2"; shift 2 ;;
            --placement-pod-balance-weight) B63_PLACEMENT_POD_BALANCE_WEIGHT="$2"; shift 2 ;;
            --placement-vlan-parent-balance-weight) B63_PLACEMENT_VLAN_PARENT_BALANCE_WEIGHT="$2"; shift 2 ;;
            --placement-pod-cap-slack) B63_PLACEMENT_POD_CAP_SLACK="$2"; shift 2 ;;
            --placement-vlan-parent-cap-slack) B63_PLACEMENT_VLAN_PARENT_CAP_SLACK="$2"; shift 2 ;;
            --placement-capacity-balance-ratio) B63_PLACEMENT_CAPACITY_BALANCE_RATIO="$2"; shift 2 ;;
            --placement-resource-overload-penalty) B63_PLACEMENT_RESOURCE_OVERLOAD_PENALTY="$2"; shift 2 ;;
            --placement-capacity-hard-cap-penalty) B63_PLACEMENT_CAPACITY_HARD_CAP_PENALTY="$2"; shift 2 ;;
            --placement-improvement-passes) B63_PLACEMENT_IMPROVEMENT_PASSES="$2"; shift 2 ;;
            --placement-fast-greedy-score) B63_PLACEMENT_FAST_GREEDY_SCORE="$2"; shift 2 ;;
            --placement-pinning-mode) B63_PLACEMENT_PINNING_MODE="$2"; shift 2 ;;
            --placement-include-master) B63_PLACEMENT_INCLUDE_MASTER="$2"; shift 2 ;;
            --placement-selected-only) B63_PLACEMENT_SELECTED_ONLY="$2"; shift 2 ;;
            --inject-resource-requests)
                if [ "$#" -gt 1 ] && [[ "$2" != --* ]]; then
                    B63_INJECT_RESOURCE_REQUESTS="$2"
                    shift 2
                else
                    B63_INJECT_RESOURCE_REQUESTS="true"
                    shift
                fi
                ;;
            --skip-resource-request-injection)
                B63_INJECT_RESOURCE_REQUESTS="false"
                if [ "$#" -gt 1 ] && [[ "$2" != --* ]]; then
                    shift 2
                else
                    shift
                fi
                ;;

            --build-parallelism) SEED_BUILD_PARALLELISM="$2"; shift 2 ;;
            --build-batch-size) SEED_BUILD_BATCH_SIZE="$2"; shift 2 ;;
            --docker-buildkit) SEED_DOCKER_BUILDKIT="$2"; shift 2 ;;
            --docker-buildx) SEED_DOCKER_BUILDX="$2"; shift 2 ;;
            --build-skip-existing) SEED_BUILD_SKIP_EXISTING="$2"; shift 2 ;;
            --preload-batch-size) SEED_PRELOAD_BATCH_SIZE="$2"; shift 2 ;;
            --preload-node-concurrency) SEED_PRELOAD_NODE_CONCURRENCY="$2"; shift 2 ;;
            --preload-image-concurrency) SEED_PRELOAD_IMAGE_CONCURRENCY="$2"; shift 2 ;;
            --registry-push-retries) SEED_REGISTRY_PUSH_RETRIES="$2"; shift 2 ;;
            --registry-push-backoff-seconds) SEED_REGISTRY_PUSH_BACKOFF_SECONDS="$2"; shift 2 ;;
            --registry-push-timeout-seconds) SEED_REGISTRY_PUSH_TIMEOUT_SECONDS="$2"; shift 2 ;;

            --deploy-batch-size) DEPLOY_BATCH_SIZE="$2"; shift 2 ;;
            --deploy-batch-sleep-seconds) DEPLOY_BATCH_SLEEP_SECONDS="$2"; shift 2 ;;
            --deploy-monitor-enabled) DEPLOY_MONITOR_ENABLED="$2"; shift 2 ;;
            --deploy-monitor-interval) DEPLOY_MONITOR_INTERVAL="$2"; shift 2 ;;
            --deploy-warmup-batches) DEPLOY_WARMUP_BATCHES="$2"; shift 2 ;;
            --deploy-warmup-batch-size) DEPLOY_WARMUP_BATCH_SIZE="$2"; shift 2 ;;
            --deploy-pressure-check-seconds) DEPLOY_PRESSURE_CHECK_SECONDS="$2"; shift 2 ;;
            --deploy-stabilize-timeout-seconds) DEPLOY_STABILIZE_TIMEOUT_SECONDS="$2"; shift 2 ;;
            --deploy-max-pending-pods) DEPLOY_MAX_PENDING_PODS="$2"; shift 2 ;;
            --deploy-max-creating-pods) DEPLOY_MAX_CREATING_PODS="$2"; shift 2 ;;
            --deploy-max-creating-pods-per-node) DEPLOY_MAX_CREATING_PODS_PER_NODE="$2"; shift 2 ;;
            --deploy-max-notready-pods) DEPLOY_MAX_NOTREADY_PODS="$2"; shift 2 ;;
            --deploy-max-failed-pods) DEPLOY_MAX_FAILED_PODS="$2"; shift 2 ;;
            --deploy-wait-kube-ovn-subnets) DEPLOY_WAIT_KUBE_OVN_SUBNETS="$2"; shift 2 ;;
            --deploy-kube-ovn-subnet-timeout-seconds) DEPLOY_KUBE_OVN_SUBNET_TIMEOUT_SECONDS="$2"; shift 2 ;;
            --deploy-kube-ovn-subnet-settle-seconds) DEPLOY_POST_SUBNET_COOLDOWN_SECONDS="$2"; shift 2 ;;
            --deploy-restart-kube-ovn-controller-after-subnets) DEPLOY_RESTART_KUBE_OVN_CONTROLLER_AFTER_SUBNETS="$2"; shift 2 ;;
            --deploy-kube-ovn-controller-restart-timeout-seconds) DEPLOY_KUBE_OVN_CONTROLLER_RESTART_TIMEOUT_SECONDS="$2"; shift 2 ;;
            --deploy-kube-ovn-controller-post-restart-settle-seconds) DEPLOY_KUBE_OVN_CONTROLLER_POST_RESTART_SETTLE_SECONDS="$2"; shift 2 ;;
            --wait-ready-interval-seconds) CHECK_INTERVAL="$2"; shift 2 ;;
            --wait-ready-timeout-seconds) TIMEOUT_SECONDS="$2"; shift 2 ;;
            --resume) DEPLOY_RESUME_REQUEST="--resume"; shift ;;
            --)
                shift
                break
                ;;
            -*)
                echo "Unsupported b63 running argument: $1" >&2
                exit 2
                ;;
            *)
                if [ -z "${experiment_dir}" ]; then
                    experiment_dir="$1"
                    shift
                else
                    echo "Unexpected positional argument: $1" >&2
                    exit 2
                fi
                ;;
        esac
    done

    if [ -z "${experiment_dir}" ]; then
        echo "Usage: $0 --experiment-dir <path> [common b63 options]" >&2
        exit 2
    fi

    mkdir -p "${experiment_dir}"
    export EXPERIMENT_DIR
    EXPERIMENT_DIR="$(cd "${experiment_dir}" && pwd)"
    export LOG_BASE_DIR
    LOG_BASE_DIR="$(dirname "${EXPERIMENT_DIR}")"
    export OUTPUT_DIR="${EXPERIMENT_DIR}/output"
    mkdir -p "${OUTPUT_DIR}"

    source_root="${source_root:-$(find_repo_root "${B63_DIR}")}"
    topology_dir="${topology_dir:-${HOME}/seed-emulator/topology}"
    config_k3s="${config_k3s:-${B63_DIR}/configK3s-b63.yaml}"
    kubeconfig="${kubeconfig:-${B63_DIR}/kubeconfig-b63.yaml}"
    inventory="${inventory:-${B63_DIR}/inventory-b63.yaml}"
    kvm_config="${kvm_config:-${B63_DIR}/configkvm_b63.yaml}"
    if [ -z "${topology_size}" ] && [ -f "${EXPERIMENT_DIR}/topology_size" ]; then
        topology_size="$(tr -d '[:space:]' < "${EXPERIMENT_DIR}/topology_size")"
    fi
    topology_size="${topology_size:-1897}"
    namespace="${namespace:-seedemu-b63-${topology_size}}"
    registry_port="${registry_port:-5000}"
    registry_host="${registry_host:-127.0.0.1}"
    registry="${registry:-${registry_host}:${registry_port}}"

    cni_type="$(normalize_network_value "${cni_type}")"
    attached_cni_type="$(normalize_network_value "${attached_cni_type}")"
    network_backend="$(normalize_network_value "${network_backend}")"
    if [ "${network_backend}" = "ovn" ]; then
        network_backend="kube-ovn"
    fi

    export REPO_ROOT
    REPO_ROOT="$(resolve_path "${source_root}" "${B63_DIR}")"
    export SEED_SOURCE_ROOT="${REPO_ROOT}"
    export SEED_TOPOLOGY_SIZE="${topology_size}"
    export SEED_REAL_TOPOLOGY_DIR
    SEED_REAL_TOPOLOGY_DIR="$(resolve_path "${topology_dir}" "${B63_DIR}")"
    export SEED_NAMESPACE="${namespace}"
    export SEED_KUBECONFIG_PATH
    SEED_KUBECONFIG_PATH="$(resolve_path "${kubeconfig}" "${B63_DIR}")"
    export KUBECONFIG="${SEED_KUBECONFIG_PATH}"
    export B63_CONFIG_K3S_PATH
    B63_CONFIG_K3S_PATH="$(resolve_path "${config_k3s}" "${B63_DIR}")"
    export B63_KUBECONFIG_PATH="${SEED_KUBECONFIG_PATH}"
    export B63_INVENTORY_PATH
    B63_INVENTORY_PATH="$(resolve_path "${inventory}" "${B63_DIR}")"
    export B63_KVM_CONFIG_PATH
    B63_KVM_CONFIG_PATH="$(resolve_path "${kvm_config}" "${B63_DIR}")"

    export B62_CONFIG_K3S_PATH="${B63_CONFIG_K3S_PATH}"
    export B62_KUBECONFIG_PATH="${B63_KUBECONFIG_PATH}"
    export B62_INVENTORY_PATH="${B63_INVENTORY_PATH}"
    export B62_CONFIG_KVM_OVN_PATH="${B63_KVM_CONFIG_PATH}"
    export SEED_CLUSTER_INVENTORY_PATH="${B63_INVENTORY_PATH}"
    export SEED_CLUSTER_INVENTORY="${B63_INVENTORY_PATH}"

    export SEED_K3S_USER="${k3s_user}"
    export SEED_K3S_SSH_KEY
    SEED_K3S_SSH_KEY="$(resolve_path "${k3s_ssh_key}" "${B63_DIR}")"
    export SEED_REGISTRY_HOST="${registry_host}"
    export SEED_REGISTRY_PORT="${registry_port}"
    export SEED_REGISTRY="${registry}"
    export SEED_K3S_MASTER_IP="${registry_host}"
    export SEED_NETWORK_BACKEND="${network_backend}"
    export SEED_CNI_TYPE="${cni_type}"
    export SEED_LOCAL_LINK_CNI_TYPE="${SEED_LOCAL_LINK_CNI_TYPE:-${cni_type}}"
    export SEED_ATTACHED_CNI_TYPE="${attached_cni_type}"
    export SEED_CNI_MASTER_INTERFACE="${cni_master_interface}"
    export SEED_IMAGE_PULL_POLICY="${image_pull_policy}"

    if [ -z "${SEED_MACVLAN_VLAN_MODE:-}" ]; then
        if [ "${cni_type}" = "macvlan" ] && { [ "${network_backend}" = "macvlan" ] || [ "${network_backend}" = "macvlan-vlan" ]; }; then
            export SEED_MACVLAN_VLAN_MODE=1
        else
            export SEED_MACVLAN_VLAN_MODE=0
        fi
    fi
    export SEED_MACVLAN_VLAN_START="${SEED_MACVLAN_VLAN_START:-100}"
    export SEED_MACVLAN_VLAN_TRUNKS_JSON="${SEED_MACVLAN_VLAN_TRUNKS_JSON:-[]}"

    export CLEAN_NAMESPACE="${CLEAN_NAMESPACE:-true}"
    export CLEAN_CHECK_INTERVAL_SECONDS="${CLEAN_CHECK_INTERVAL_SECONDS:-20}"
    export CLEAN_TIMEOUT_SECONDS="${CLEAN_TIMEOUT_SECONDS:-3600}"
    export CLEAN_FORCE_FINALIZER_CLEANUP="${CLEAN_FORCE_FINALIZER_CLEANUP:-false}"
    export CLEAN_AUTO_FINALIZE_STUCK_NAMESPACE="${CLEAN_AUTO_FINALIZE_STUCK_NAMESPACE:-true}"
    export CLEAN_AUTO_FINALIZE_AFTER_SECONDS="${CLEAN_AUTO_FINALIZE_AFTER_SECONDS:-120}"
    export CLEAN_NAMESPACE_FINALIZER_AFTER_SECONDS="${CLEAN_NAMESPACE_FINALIZER_AFTER_SECONDS:-${CLEAN_AUTO_FINALIZE_AFTER_SECONDS}}"

    export SEED_BUILD_PARALLELISM="${SEED_BUILD_PARALLELISM:-12}"
    export SEED_BUILD_BATCH_SIZE="${SEED_BUILD_BATCH_SIZE:-50}"
    export SEED_DOCKER_BUILDKIT="${SEED_DOCKER_BUILDKIT:-1}"
    export SEED_DOCKER_BUILDX="${SEED_DOCKER_BUILDX:-1}"
    export SEED_BUILD_SKIP_EXISTING="${SEED_BUILD_SKIP_EXISTING:-0}"
    export SEED_PRELOAD_BATCH_SIZE="${SEED_PRELOAD_BATCH_SIZE:-10}"
    export SEED_PRELOAD_NODE_CONCURRENCY="${SEED_PRELOAD_NODE_CONCURRENCY:-4}"
    export SEED_PRELOAD_IMAGE_CONCURRENCY="${SEED_PRELOAD_IMAGE_CONCURRENCY:-2}"
    export SEED_PRELOAD_RETRIES="${SEED_PRELOAD_RETRIES:-3}"
    export SEED_PRELOAD_BACKOFF_SECONDS="${SEED_PRELOAD_BACKOFF_SECONDS:-5}"
    export SEED_REGISTRY_PUSH_RETRIES="${SEED_REGISTRY_PUSH_RETRIES:-5}"
    export SEED_REGISTRY_PUSH_BACKOFF_SECONDS="${SEED_REGISTRY_PUSH_BACKOFF_SECONDS:-5}"
    export SEED_REGISTRY_PUSH_TIMEOUT_SECONDS="${SEED_REGISTRY_PUSH_TIMEOUT_SECONDS:-180}"

    if [ -z "${DEPLOY_WAIT_KUBE_OVN_SUBNETS:-}" ]; then
        if [ "${network_backend}" = "kube-ovn" ]; then
            DEPLOY_WAIT_KUBE_OVN_SUBNETS=true
        else
            DEPLOY_WAIT_KUBE_OVN_SUBNETS=false
        fi
    fi
    export DEPLOY_WAIT_KUBE_OVN_SUBNETS
    export CHECK_INTERVAL="${CHECK_INTERVAL:-30}"
    export TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-7200}"
}

ts() {
    date +"%F %T"
}

stage_log_file() {
    local stage="$1"
    printf '%s/%s.log\n' "${EXPERIMENT_DIR}" "${stage}"
}

stage_load_monitor_file() {
    printf '%s/loadAverage.log\n' "${EXPERIMENT_DIR}"
}

append_load_monitor_sample() {
    # Append one controller-host resource sample for $1=stageName.
    local stage="$1"
    local log_file timestamp load_1 load_5 load_15 cpu_raw cpu_user cpu_system cpu_idle mem_values
    log_file="$(stage_load_monitor_file)"
    timestamp="$(date '+%F %T')"
    read -r load_1 load_5 load_15 _ < /proc/loadavg
    cpu_raw="$(LC_ALL=C top -bn1 | awk '/Cpu\(s\)|%Cpu/ {print; exit}')"
    cpu_user="$(printf '%s\n' "${cpu_raw}" | awk -F'us,' '{print $1}' | awk '{print $NF}')"
    cpu_system="$(printf '%s\n' "${cpu_raw}" | awk -F'sy,' '{print $1}' | awk '{print $NF}')"
    cpu_idle="$(printf '%s\n' "${cpu_raw}" | awk -F'id,' '{print $1}' | awk '{print $NF}')"
    cpu_user="${cpu_user:-na}"
    cpu_system="${cpu_system:-na}"
    cpu_idle="${cpu_idle:-na}"
    mem_values="$(awk '
        /MemTotal:/ {total=$2}
        /MemAvailable:/ {available=$2}
        END {
            used = total - available
            used_pct = total > 0 ? used * 100 / total : 0
            printf "%.0f,%.0f,%.0f,%.2f", total / 1024, available / 1024, used / 1024, used_pct
        }
    ' /proc/meminfo)"
    if [ ! -s "${log_file}" ]; then
        printf 'Timestamp,Stage,ParentPID,Load_1,Load_5,Load_15,CPU_User,CPU_System,CPU_Idle,Mem_Total_MiB,Mem_Available_MiB,Mem_Used_MiB,Mem_Used_Pct\n' > "${log_file}"
    fi
    printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
        "${timestamp}" "${stage}" "$$" "${load_1}" "${load_5}" "${load_15}" \
        "${cpu_user}" "${cpu_system}" "${cpu_idle}" "${mem_values}" >> "${log_file}"
}

start_stage_load_monitor() {
    # Start a low-frequency background resource monitor for $1=stageName.
    local stage="$1"
    local interval=30
    local parent_pid="$$"
    local log_file
    if [ "${SEED_STAGE_LOAD_MONITOR_DISABLED:-0}" = "1" ]; then
        return 0
    fi
    log_file="$(stage_load_monitor_file)"
    (
        exec >/dev/null 2>&1
        while kill -0 "${parent_pid}" >/dev/null 2>&1; do
            append_load_monitor_sample "${stage}" || true
            for ((i = 0; i < interval; i++)); do
                sleep 1
                kill -0 "${parent_pid}" >/dev/null 2>&1 || exit 0
            done
        done
    ) &
    export SEED_STAGE_LOAD_MONITOR_PID="$!"
    echo "Load monitor: ${log_file} interval=${interval}s pid=${SEED_STAGE_LOAD_MONITOR_PID}"
}

begin_stage_logging() {
    local stage="$1"
    local log_file
    log_file="$(stage_log_file "${stage}")"
    exec > >(tee -a "${log_file}") 2>&1
    echo "=== ${stage} ==="
    echo "Log file: ${log_file}"
    start_stage_load_monitor "${stage}"
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

cluster_nodes_tsv() {
    python3 - "${SEED_CLUSTER_INVENTORY_PATH}" <<'PY'
import sys
import yaml

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as fh:
    data = yaml.safe_load(fh) or {}
for node in data.get("nodes", []):
    management_ip = node.get("managementIp") or node.get("management_ip") or node.get("ip")
    if not management_ip:
        raise SystemExit(f"missing management IP for node {node.get('name', '<unknown>')}")
    print(f"{node['name']}\t{management_ip}\t{node.get('role', '')}")
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
        -o UserKnownHostsFile=/dev/null \
        -o LogLevel=ERROR \
        -o IdentitiesOnly=yes \
        -o IdentityAgent=none \
        "${SEED_K3S_USER}@${ip}" "$@"
}

run_in_seedpy310() {
    local env_dir="${SEEDPY310_ENV_DIR:-$HOME/anaconda3/envs/${SEEDPY310_ENV_NAME}}"
    if [ -x "${env_dir}/bin/python3" ]; then
        PATH="${env_dir}/bin:${PATH}" CONDA_PREFIX="${env_dir}" "$@"
        return
    fi

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
    run_in_seedpy310 python3 "${PLACEMENT_PLANNER}" \
        "${topology_file}" \
        "${assignment_file}" \
        "${nodes_json_file}" \
        "${mapping_file}" \
        "${plan_file}"
}

normalizeNetworkValue() {
    # Normalize one network mode string read from config or compiler output.
    printf '%s\n' "$1" | tr '[:upper:]' '[:lower:]' | sed 's/_/-/g'
}

seedCompiledNetworkBackend() {
    # Return the backend declared by OUTPUT_DIR/networking.yaml when available.
    local metadata="${OUTPUT_DIR}/networking.yaml"
    [ -f "${metadata}" ] || return 1
    python3 - "${metadata}" <<'PY'
import sys
import yaml

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    data = yaml.safe_load(handle) or {}
if not isinstance(data, dict):
    raise SystemExit(1)

cni = str(data.get("cniType") or "").strip().lower().replace("_", "-")
backend = str(data.get("networkBackend") or "").strip().lower().replace("_", "-")
value = cni or backend
if value in {"kube-ovn", "ovn"}:
    print("kube-ovn")
elif value:
    print(value)
else:
    raise SystemExit(1)
PY
}

seed_network_backend() {
    local compiled cni backend
    compiled="$(seedCompiledNetworkBackend 2>/dev/null || true)"
    if [ -n "${compiled}" ]; then
        printf '%s\n' "${compiled}"
        return 0
    fi

    cni="$(normalizeNetworkValue "${SEED_CNI_TYPE:-}")"
    case "${cni}" in
        kube-ovn|ovn)
            printf 'kube-ovn\n'
            return 0
            ;;
        macvlan|macvlan-vlan|ipvlan|host-local|bridge|bridge-vlan)
            printf '%s\n' "${cni%%-vlan}"
            return 0
            ;;
    esac

    backend="$(normalizeNetworkValue "${SEED_NETWORK_BACKEND:-macvlan}")"
    if [ "${backend}" = "ovn" ]; then
        backend="kube-ovn"
    fi
    printf '%s\n' "${backend%%-vlan}"
}

seed_manage_manifest_helper() {
    printf '%s\n' "${REPO_ROOT}/seedemu/k8sTools/resources/running/manageK8sManifest.py"
}

seed_runtime_manifest_path() {
    local backend
    backend="$(seed_network_backend)"
    if [ "${backend}" = "kube-ovn" ] || [ "${backend}" = "kube_ovn" ] || [ "${backend}" = "ovn" ]; then
        printf '%s/k8s.kube-ovn.yaml\n' "${OUTPUT_DIR}"
    else
        printf '%s/k8s.yaml\n' "${OUTPUT_DIR}"
    fi
}

render_runtime_manifest() {
    local source_manifest="${OUTPUT_DIR}/k8s.yaml"
    local target_manifest
    local helper
    target_manifest="$(seed_runtime_manifest_path)"
    helper="$(seed_manage_manifest_helper)"
    require_file "${source_manifest}"

    if [ "${target_manifest}" = "${source_manifest}" ]; then
        rm -f "${OUTPUT_DIR}/k8s.kube-ovn.yaml"
        printf '%s\n' "${target_manifest}"
        return 0
    fi

    require_file "${helper}"
    echo "Rendering Kube-OVN manifest: ${target_manifest}" >&2
    run_in_seedpy310 env PYTHONPATH="${REPO_ROOT}" PYTHONNOUSERSITE=1 \
        python3 - \
        "${helper}" \
        "${source_manifest}" \
        "${target_manifest}" \
        "${SEED_ATTACHED_CNI_TYPE:-${SEED_NETWORK_BACKEND:-kube-ovn}}" \
        "${SEED_CNI_MASTER_INTERFACE}" <<'PY'
import importlib.util
import sys
from pathlib import Path

helper = Path(sys.argv[1])
source = sys.argv[2]
target = Path(sys.argv[3])
attached_cni_type = sys.argv[4]
cni_master_interface = sys.argv[5]
spec = importlib.util.spec_from_file_location("seedemu_manage_k8s_manifest", helper)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)
module.renderKubeOvnManifest(
    source,
    target,
    attached_cni_type=attached_cni_type,
    cni_master_interface=cni_master_interface,
)
PY
    printf '%s\n' "${target_manifest}"
}

print_test_context() {
    echo "EXPERIMENT_DIR=${EXPERIMENT_DIR}"
    echo "SEED_TOPOLOGY_SIZE=${SEED_TOPOLOGY_SIZE}"
    echo "SEED_CLUSTER_INVENTORY_PATH=${SEED_CLUSTER_INVENTORY_PATH}"
    echo "KUBECONFIG=${KUBECONFIG}"
    echo "SEED_NAMESPACE=${SEED_NAMESPACE}"
    echo "SEED_NETWORK_BACKEND=${SEED_NETWORK_BACKEND}"
    echo "SEED_CNI_TYPE=${SEED_CNI_TYPE}"
    echo "SEED_ATTACHED_CNI_TYPE=${SEED_ATTACHED_CNI_TYPE}"
}
