#!/usr/bin/env bash
# Shared helpers for the b63 large-scale running stages.
#
# Inputs: explicit CLI parameters passed by runB63LargeExperiment.py, generated
#         kubeconfig/inventory files, and real-topology input files.
# Outputs: stage logs and helper artifacts under the experiment directory.
# Side effects: helper functions may call kubectl or SSH when used by stages.
# Context: sourced by scripts in largeScale/b63_deployment/running.
set -euo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
B63_DIR="$(cd "${TEST_DIR}/.." && pwd)"

find_repo_root() {
    local dir="${B63_DIR}"
    while [ "${dir}" != "/" ]; do
        if [ -f "${dir}/setup.py" ] && [ -d "${dir}/seedemu" ]; then
            printf '%s\n' "${dir}"
            return 0
        fi
        dir="$(dirname "${dir}")"
    done
    return 1
}

REPO_ROOT="$(find_repo_root)"
SEEDPY310_CONDA_SH="$HOME/anaconda3/etc/profile.d/conda.sh"
SEEDPY310_ENV_NAME="seedpy310"

derive_topology_size_from_experiment_dir() {
    local expdir_basename="$1"
    if [[ "${expdir_basename}" =~ ^([0-9]+)_ ]]; then
        printf '%s\n' "${BASH_REMATCH[1]}"
        return 0
    fi
    if [[ "${expdir_basename}" =~ _([0-9]+)$ ]]; then
        printf '%s\n' "${BASH_REMATCH[1]}"
        return 0
    fi
    return 1
}

setup_experiment_context() {
    local expdir_input=""
    parse_stage_args "$@"
    if [ -z "${expdir_input}" ]; then
        echo "Usage: $0 --experiment-dir <dir> [explicit b63 stage parameters]" >&2
        exit 2
    fi

    mkdir -p "${expdir_input}"
    export EXPERIMENT_DIR
    EXPERIMENT_DIR="$(cd "${expdir_input}" && pwd)"
    export LOG_BASE_DIR
    LOG_BASE_DIR="$(dirname "${EXPERIMENT_DIR}")"
    export OUTPUT_DIR="${EXPERIMENT_DIR}/output"

    if [ -z "${SEED_TOPOLOGY_SIZE}" ] && [ -f "${EXPERIMENT_DIR}/topology_size" ]; then
        SEED_TOPOLOGY_SIZE="$(tr -d ' \n\r' < "${EXPERIMENT_DIR}/topology_size")"
    elif [ -z "${SEED_TOPOLOGY_SIZE}" ]; then
        SEED_TOPOLOGY_SIZE="$(derive_topology_size_from_experiment_dir "$(basename "${EXPERIMENT_DIR}")")" || {
            echo "Cannot derive topology size from experiment dir name: ${EXPERIMENT_DIR}" >&2
            echo "Expected prefix like 1897_<timestamp> or create ${EXPERIMENT_DIR}/topology_size" >&2
            exit 1
        }
    fi

    apply_stage_defaults
    export_stage_values
}

parse_stage_args() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --experiment-dir) expdir_input="$2"; shift 2 ;;
            --source-root) REPO_ROOT="$2"; shift 2 ;;
            --topology-size) SEED_TOPOLOGY_SIZE="$2"; shift 2 ;;
            --topology-dir) SEED_REAL_TOPOLOGY_DIR="$2"; shift 2 ;;
            --namespace) SEED_NAMESPACE="$2"; shift 2 ;;
            --kubeconfig) SEED_KUBECONFIG_PATH="$2"; shift 2 ;;
            --inventory) SEED_CLUSTER_INVENTORY_PATH="$2"; shift 2 ;;
            --config-k3s) SEED_CONFIG_K3S_PATH="$2"; shift 2 ;;
            --kvm-config) SEED_KVM_CONFIG_PATH="$2"; shift 2 ;;
            --registry) SEED_REGISTRY="$2"; shift 2 ;;
            --registry-host) SEED_REGISTRY_HOST="$2"; shift 2 ;;
            --registry-port) SEED_REGISTRY_PORT="$2"; shift 2 ;;
            --k3s-user) SEED_K3S_USER="$2"; shift 2 ;;
            --k3s-ssh-key) SEED_K3S_SSH_KEY="$2"; shift 2 ;;
            --cni-type) SEED_CNI_TYPE="$2"; shift 2 ;;
            --cni-master-interface) SEED_CNI_MASTER_INTERFACE="$2"; shift 2 ;;
            --network-backend) SEED_NETWORK_BACKEND="$2"; shift 2 ;;
            --attached-cni-type) SEED_ATTACHED_CNI_TYPE="$2"; shift 2 ;;
            --image-pull-policy) SEED_IMAGE_PULL_POLICY="$2"; shift 2 ;;
            --placement-algorithm) SEED_PLACEMENT_ALGORITHM="$2"; shift 2 ;;
            --network-cost-mode) SEED_NETWORK_COST_MODE="$2"; shift 2 ;;
            --placement-alpha) SEED_PLACEMENT_ALPHA="$2"; shift 2 ;;
            --placement-beta) SEED_PLACEMENT_BETA="$2"; shift 2 ;;
            --placement-improvement-passes) SEED_PLACEMENT_IMPROVEMENT_PASSES="$2"; shift 2 ;;
            --placement-pinning-mode) SEED_PLACEMENT_PINNING_MODE="$2"; shift 2 ;;
            --skip-resource-request-injection) SEED_SKIP_RESOURCE_REQUEST_INJECTION="$2"; shift 2 ;;
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
            --deploy-max-notready-pods) DEPLOY_MAX_NOTREADY_PODS="$2"; shift 2 ;;
            --deploy-max-failed-pods) DEPLOY_MAX_FAILED_PODS="$2"; shift 2 ;;
            --deploy-wait-kube-ovn-subnets) DEPLOY_WAIT_KUBE_OVN_SUBNETS="$2"; shift 2 ;;
            --deploy-kube-ovn-subnet-timeout-seconds) DEPLOY_KUBE_OVN_SUBNET_TIMEOUT_SECONDS="$2"; shift 2 ;;
            --deploy-kube-ovn-subnet-settle-seconds) DEPLOY_KUBE_OVN_SUBNET_SETTLE_SECONDS="$2"; shift 2 ;;
            --deploy-restart-kube-ovn-controller-after-subnets) DEPLOY_RESTART_KUBE_OVN_CONTROLLER_AFTER_SUBNETS="$2"; shift 2 ;;
            --deploy-kube-ovn-controller-restart-timeout-seconds) DEPLOY_KUBE_OVN_CONTROLLER_RESTART_TIMEOUT_SECONDS="$2"; shift 2 ;;
            --deploy-kube-ovn-controller-post-restart-settle-seconds) DEPLOY_KUBE_OVN_CONTROLLER_POST_RESTART_SETTLE_SECONDS="$2"; shift 2 ;;
            --wait-ready-interval-seconds) SEED_WAIT_READY_INTERVAL_SECONDS="$2"; shift 2 ;;
            --wait-ready-timeout-seconds) SEED_WAIT_READY_TIMEOUT_SECONDS="$2"; shift 2 ;;
            --clean-namespace) CLEAN_NAMESPACE="$2"; shift 2 ;;
            --clean-check-interval-seconds) SEED_CLEAN_CHECK_INTERVAL_SECONDS="$2"; shift 2 ;;
            --clean-timeout-seconds) SEED_CLEAN_TIMEOUT_SECONDS="$2"; shift 2 ;;
            --clean-force-finalizer-cleanup) SEED_CLEAN_FORCE_FINALIZER_CLEANUP="$2"; shift 2 ;;
            --clean-auto-finalize-stuck-namespace) SEED_CLEAN_AUTO_FINALIZE_STUCK_NAMESPACE="$2"; shift 2 ;;
            --clean-auto-finalize-after-seconds) SEED_CLEAN_AUTO_FINALIZE_AFTER_SECONDS="$2"; shift 2 ;;
            --)
                shift
                break
                ;;
            -*)
                echo "Unknown b63 stage parameter: $1" >&2
                exit 2
                ;;
            *)
                if [ -z "${expdir_input}" ]; then
                    expdir_input="$1"
                    shift
                else
                    echo "Unexpected positional parameter: $1" >&2
                    exit 2
                fi
                ;;
        esac
    done
}

apply_stage_defaults() {
    SEED_SOURCE_ROOT="${REPO_ROOT}"
    SEED_CONFIG_K3S_PATH="${SEED_CONFIG_K3S_PATH:-${B63_DIR}/configK3s-b63.yaml}"
    SEED_CLUSTER_INVENTORY_PATH="${SEED_CLUSTER_INVENTORY_PATH:-${B63_DIR}/inventory-b63.yaml}"
    SEED_KUBECONFIG_PATH="${SEED_KUBECONFIG_PATH:-${B63_DIR}/kubeconfig-b63.yaml}"
    SEED_KVM_CONFIG_PATH="${SEED_KVM_CONFIG_PATH:-${B63_DIR}/configkvm_b63.yaml}"
    SEED_NAMESPACE="${SEED_NAMESPACE:-seedemu-b63-${SEED_TOPOLOGY_SIZE}}"
    SEED_REAL_TOPOLOGY_DIR="${SEED_REAL_TOPOLOGY_DIR:-$HOME/seed-emulator/topology}"
    SEED_REGISTRY_HOST="${SEED_REGISTRY_HOST:-192.168.122.230}"
    SEED_REGISTRY_PORT="${SEED_REGISTRY_PORT:-5063}"
    SEED_REGISTRY="${SEED_REGISTRY:-${SEED_REGISTRY_HOST}:${SEED_REGISTRY_PORT}}"
    SEED_K3S_MASTER_IP="${SEED_K3S_MASTER_IP:-${SEED_REGISTRY_HOST}}"
    SEED_K3S_USER="${SEED_K3S_USER:-ubuntu}"
    SEED_K3S_SSH_KEY="${SEED_K3S_SSH_KEY:-$HOME/.ssh/id_ed25519}"
    SEED_CNI_TYPE="${SEED_CNI_TYPE:-kube-ovn}"
    SEED_LOCAL_LINK_CNI_TYPE="${SEED_LOCAL_LINK_CNI_TYPE:-${SEED_CNI_TYPE}}"
    SEED_CNI_MASTER_INTERFACE="${SEED_CNI_MASTER_INTERFACE:-ens2}"
    SEED_NETWORK_BACKEND="${SEED_NETWORK_BACKEND:-kube-ovn}"
    SEED_ATTACHED_CNI_TYPE="${SEED_ATTACHED_CNI_TYPE:-${SEED_NETWORK_BACKEND}}"
    SEED_IMAGE_PULL_POLICY="${SEED_IMAGE_PULL_POLICY:-IfNotPresent}"
    SEED_SCHEDULING_STRATEGY="none"
    SEED_PLACEMENT_MODE="b63-placement"
    SEED_IMAGE_DISTRIBUTION_MODE="preload"
    SEED_PLACEMENT_ALGORITHM="${SEED_PLACEMENT_ALGORITHM:-optimized}"
    SEED_NETWORK_COST_MODE="${SEED_NETWORK_COST_MODE:-ratio}"
    SEED_PLACEMENT_ALPHA="${SEED_PLACEMENT_ALPHA:-1.0}"
    SEED_PLACEMENT_BETA="${SEED_PLACEMENT_BETA:-1.0}"
    SEED_PLACEMENT_IMPROVEMENT_PASSES="${SEED_PLACEMENT_IMPROVEMENT_PASSES:-0}"
    SEED_PLACEMENT_PINNING_MODE="${SEED_PLACEMENT_PINNING_MODE:-node-selector}"
    SEED_SKIP_RESOURCE_REQUEST_INJECTION="${SEED_SKIP_RESOURCE_REQUEST_INJECTION:-false}"

    SEED_BUILD_PARALLELISM="${SEED_BUILD_PARALLELISM:-12}"
    SEED_BUILD_BATCH_SIZE="${SEED_BUILD_BATCH_SIZE:-50}"
    SEED_DOCKER_BUILDKIT="${SEED_DOCKER_BUILDKIT:-1}"
    SEED_DOCKER_BUILDX="${SEED_DOCKER_BUILDX:-1}"
    SEED_BUILD_SKIP_EXISTING="${SEED_BUILD_SKIP_EXISTING:-0}"
    SEED_COMPOSE_DOCKER_CLI_BUILD="${SEED_COMPOSE_DOCKER_CLI_BUILD:-1}"
    SEED_PRELOAD_BATCH_SIZE="${SEED_PRELOAD_BATCH_SIZE:-10}"
    SEED_PRELOAD_NODE_CONCURRENCY="${SEED_PRELOAD_NODE_CONCURRENCY:-4}"
    SEED_PRELOAD_IMAGE_CONCURRENCY="${SEED_PRELOAD_IMAGE_CONCURRENCY:-2}"
    SEED_REGISTRY_PUSH_RETRIES="${SEED_REGISTRY_PUSH_RETRIES:-5}"
    SEED_REGISTRY_PUSH_BACKOFF_SECONDS="${SEED_REGISTRY_PUSH_BACKOFF_SECONDS:-5}"
    SEED_REGISTRY_PUSH_TIMEOUT_SECONDS="${SEED_REGISTRY_PUSH_TIMEOUT_SECONDS:-180}"

    DEPLOY_BATCH_SIZE="${DEPLOY_BATCH_SIZE:-10}"
    DEPLOY_BATCH_SLEEP_SECONDS="${DEPLOY_BATCH_SLEEP_SECONDS:-3}"
    DEPLOY_MONITOR_INTERVAL="${DEPLOY_MONITOR_INTERVAL:-60}"
    DEPLOY_MONITOR_ENABLED="${DEPLOY_MONITOR_ENABLED:-false}"
    DEPLOY_VERBOSE_SNAPSHOTS="${DEPLOY_VERBOSE_SNAPSHOTS:-false}"
    DEPLOY_STATIC_APPLY_MODE="${DEPLOY_STATIC_APPLY_MODE:-batch}"
    DEPLOY_CONTROLLER_APPLY_MODE="${DEPLOY_CONTROLLER_APPLY_MODE:-batch}"
    DEPLOY_WARMUP_BATCHES="${DEPLOY_WARMUP_BATCHES:-3}"
    DEPLOY_WARMUP_BATCH_SIZE="${DEPLOY_WARMUP_BATCH_SIZE:-5}"
    DEPLOY_PRESSURE_CHECK_SECONDS="${DEPLOY_PRESSURE_CHECK_SECONDS:-5}"
    DEPLOY_STABILIZE_TIMEOUT_SECONDS="${DEPLOY_STABILIZE_TIMEOUT_SECONDS:-7200}"
    DEPLOY_MAX_PENDING_PODS="${DEPLOY_MAX_PENDING_PODS:-60}"
    DEPLOY_MAX_CREATING_PODS="${DEPLOY_MAX_CREATING_PODS:-80}"
    DEPLOY_MAX_NOTREADY_PODS="${DEPLOY_MAX_NOTREADY_PODS:-120}"
    DEPLOY_MAX_FAILED_PODS="${DEPLOY_MAX_FAILED_PODS:-5}"
    DEPLOY_WAIT_KUBE_OVN_SUBNETS="${DEPLOY_WAIT_KUBE_OVN_SUBNETS:-true}"
    DEPLOY_KUBE_OVN_SUBNET_TIMEOUT_SECONDS="${DEPLOY_KUBE_OVN_SUBNET_TIMEOUT_SECONDS:-7200}"
    DEPLOY_KUBE_OVN_SUBNET_SETTLE_SECONDS="${DEPLOY_KUBE_OVN_SUBNET_SETTLE_SECONDS:-0}"
    DEPLOY_RESTART_KUBE_OVN_CONTROLLER_AFTER_SUBNETS="${DEPLOY_RESTART_KUBE_OVN_CONTROLLER_AFTER_SUBNETS:-false}"
    DEPLOY_KUBE_OVN_CONTROLLER_RESTART_TIMEOUT_SECONDS="${DEPLOY_KUBE_OVN_CONTROLLER_RESTART_TIMEOUT_SECONDS:-600}"
    DEPLOY_KUBE_OVN_CONTROLLER_POST_RESTART_SETTLE_SECONDS="${DEPLOY_KUBE_OVN_CONTROLLER_POST_RESTART_SETTLE_SECONDS:-30}"
    SEED_WAIT_READY_INTERVAL_SECONDS="${SEED_WAIT_READY_INTERVAL_SECONDS:-30}"
    SEED_WAIT_READY_TIMEOUT_SECONDS="${SEED_WAIT_READY_TIMEOUT_SECONDS:-7200}"

    CLEAN_NAMESPACE="${CLEAN_NAMESPACE:-true}"
    SEED_CLEAN_CHECK_INTERVAL_SECONDS="${SEED_CLEAN_CHECK_INTERVAL_SECONDS:-20}"
    SEED_CLEAN_TIMEOUT_SECONDS="${SEED_CLEAN_TIMEOUT_SECONDS:-3600}"
    SEED_CLEAN_FORCE_FINALIZER_CLEANUP="${SEED_CLEAN_FORCE_FINALIZER_CLEANUP:-false}"
    SEED_CLEAN_AUTO_FINALIZE_STUCK_NAMESPACE="${SEED_CLEAN_AUTO_FINALIZE_STUCK_NAMESPACE:-true}"
    SEED_CLEAN_AUTO_FINALIZE_AFTER_SECONDS="${SEED_CLEAN_AUTO_FINALIZE_AFTER_SECONDS:-120}"

    KUBECONFIG="${SEED_KUBECONFIG_PATH}"
}

export_stage_values() {
    export REPO_ROOT SEED_SOURCE_ROOT EXPERIMENT_DIR LOG_BASE_DIR OUTPUT_DIR
    export SEED_TOPOLOGY_SIZE SEED_CONFIG_K3S_PATH SEED_CLUSTER_INVENTORY_PATH SEED_KUBECONFIG_PATH SEED_KVM_CONFIG_PATH KUBECONFIG
    export SEED_NAMESPACE SEED_REAL_TOPOLOGY_DIR SEED_REGISTRY_HOST SEED_REGISTRY_PORT SEED_REGISTRY SEED_K3S_MASTER_IP
    export SEED_K3S_USER SEED_K3S_SSH_KEY SEED_CNI_TYPE SEED_LOCAL_LINK_CNI_TYPE SEED_CNI_MASTER_INTERFACE SEED_NETWORK_BACKEND SEED_ATTACHED_CNI_TYPE SEED_IMAGE_PULL_POLICY
    export SEED_SCHEDULING_STRATEGY SEED_PLACEMENT_MODE SEED_IMAGE_DISTRIBUTION_MODE
    export SEED_PLACEMENT_ALGORITHM SEED_NETWORK_COST_MODE SEED_PLACEMENT_ALPHA SEED_PLACEMENT_BETA SEED_PLACEMENT_IMPROVEMENT_PASSES SEED_PLACEMENT_PINNING_MODE
    export SEED_SKIP_RESOURCE_REQUEST_INJECTION
    export SEED_BUILD_PARALLELISM SEED_BUILD_BATCH_SIZE SEED_DOCKER_BUILDKIT SEED_DOCKER_BUILDX SEED_BUILD_SKIP_EXISTING
    export SEED_COMPOSE_DOCKER_CLI_BUILD SEED_PRELOAD_BATCH_SIZE SEED_PRELOAD_NODE_CONCURRENCY SEED_PRELOAD_IMAGE_CONCURRENCY
    export SEED_REGISTRY_PUSH_RETRIES SEED_REGISTRY_PUSH_BACKOFF_SECONDS SEED_REGISTRY_PUSH_TIMEOUT_SECONDS
    export DEPLOY_BATCH_SIZE DEPLOY_BATCH_SLEEP_SECONDS DEPLOY_MONITOR_INTERVAL DEPLOY_MONITOR_ENABLED DEPLOY_VERBOSE_SNAPSHOTS
    export DEPLOY_STATIC_APPLY_MODE DEPLOY_CONTROLLER_APPLY_MODE DEPLOY_WARMUP_BATCHES DEPLOY_WARMUP_BATCH_SIZE
    export DEPLOY_PRESSURE_CHECK_SECONDS DEPLOY_STABILIZE_TIMEOUT_SECONDS DEPLOY_MAX_PENDING_PODS DEPLOY_MAX_CREATING_PODS
    export DEPLOY_MAX_NOTREADY_PODS DEPLOY_MAX_FAILED_PODS DEPLOY_WAIT_KUBE_OVN_SUBNETS DEPLOY_KUBE_OVN_SUBNET_TIMEOUT_SECONDS
    export DEPLOY_KUBE_OVN_SUBNET_SETTLE_SECONDS DEPLOY_RESTART_KUBE_OVN_CONTROLLER_AFTER_SUBNETS
    export DEPLOY_KUBE_OVN_CONTROLLER_RESTART_TIMEOUT_SECONDS DEPLOY_KUBE_OVN_CONTROLLER_POST_RESTART_SETTLE_SECONDS
    export SEED_WAIT_READY_INTERVAL_SECONDS SEED_WAIT_READY_TIMEOUT_SECONDS
    export CLEAN_NAMESPACE SEED_CLEAN_CHECK_INTERVAL_SECONDS SEED_CLEAN_TIMEOUT_SECONDS SEED_CLEAN_FORCE_FINALIZER_CLEANUP
    export SEED_CLEAN_AUTO_FINALIZE_STUCK_NAMESPACE SEED_CLEAN_AUTO_FINALIZE_AFTER_SECONDS
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
    :
}

load_deploy_config() {
    :
}

normalizeNetworkValue() {
    # Normalize one network mode string read from env or output/networking.yaml.
    # Args: $1=rawValue.
    printf '%s\n' "$1" | tr '[:upper:]' '[:lower:]' | sed 's/_/-/g'
}

seedCompiledNetworkBackend() {
    # Return the backend declared by the compiler output, if available.
    # Reads OUTPUT_DIR/networking.yaml written by KubernetesCompiler.
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
        macvlan|ipvlan|host-local|bridge)
            printf '%s\n' "${cni}"
            return 0
            ;;
    esac

    backend="$(normalizeNetworkValue "${SEED_NETWORK_BACKEND:-kube-ovn}")"
    if [ "${backend}" = "ovn" ]; then
        backend="kube-ovn"
    fi
    printf '%s\n' "${backend}"
}

seed_manage_manifest_helper() {
    printf '%s\n' "${REPO_ROOT}/seedemu/k8sTools/resources/running/manageK8sManifest.py"
}

seed_runtime_manifest_path() {
    local backend
    backend="$(seed_network_backend)"
    if [ "${backend}" = "kube-ovn" ] || [ "${backend}" = "ovn" ]; then
        printf '%s/k8s.kube-ovn.yaml\n' "${OUTPUT_DIR}"
    else
        printf '%s/k8s.yaml\n' "${OUTPUT_DIR}"
    fi
}

# Normalize the b63 Kube-OVN runtime manifest for large secondary networks.
# Args:
#   $1=manifest path generated by manageK8sManifest.py.
# Dependencies:
#   Reads SEED_NAMESPACE and writes the manifest in place. The generated
#   secondary Subnets only need Kube-OVN logical switches and provider IPAM;
#   leaving them in a dedicated VPC forces one logical-router port per network,
#   which does not converge reliably at 1k+ Subnets in this workflow.
normalize_b63_kube_ovn_manifest() {
    local manifest="$1"
    local backend attached
    backend="$(seed_network_backend)"
    attached="$(printf '%s\n' "${SEED_ATTACHED_CNI_TYPE:-}" | tr '[:upper:]_' '[:lower:]-')"
    if { [ "${backend}" != "kube-ovn" ] && [ "${backend}" != "ovn" ]; } || \
       { [ "${attached}" != "kube-ovn" ] && [ "${attached}" != "ovn" ]; }; then
        return 0
    fi

    python3 - "${manifest}" "${SEED_NAMESPACE}" <<'PY'
from pathlib import Path
import sys
import yaml

manifest = Path(sys.argv[1])
namespace = sys.argv[2]
docs = [
    doc
    for doc in yaml.safe_load_all(manifest.read_text(encoding="utf-8"))
    if isinstance(doc, dict)
]

changed = 0
for doc in docs:
    if doc.get("kind") != "Subnet":
        continue
    spec = doc.get("spec")
    if not isinstance(spec, dict):
        continue
    provider = str(spec.get("provider") or "")
    if f".{namespace}.ovn" not in provider:
        continue
    if spec.get("vpc") and spec.get("vpc") != "ovn-cluster":
        spec.pop("vpc", None)
        changed += 1
    if spec.get("gatewayType") == "distributed":
        spec.pop("gatewayType", None)
        changed += 1

if changed:
    manifest.write_text(yaml.safe_dump_all(docs, sort_keys=False), encoding="utf-8")
print(f"[kube-ovn-manifest] normalized {changed} b63 secondary Subnet fields in {manifest}", file=sys.stderr)
PY
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
    if [ ! -f "${target_manifest}" ] || [ "${source_manifest}" -nt "${target_manifest}" ] || [ "${helper}" -nt "${target_manifest}" ]; then
        echo "Rendering Kube-OVN manifest: ${target_manifest}" >&2
        PYTHONPATH="${REPO_ROOT}" PYTHONNOUSERSITE=1 python3 - \
            "${helper}" \
            "${source_manifest}" \
            "${target_manifest}" \
            "${SEED_ATTACHED_CNI_TYPE}" \
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
    fi
    normalize_b63_kube_ovn_manifest "${target_manifest}"
    printf '%s\n' "${target_manifest}"
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
    ip = node.get("managementIp") or node.get("management_ip") or node.get("ip") or node.get("ansible_host") or ""
    print(f"{node['name']}\t{ip}\t{node.get('role', '')}")
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

multus_flannel_conflist() {
    cat <<'EOF'
{
  "cniVersion": "1.0.0",
  "name": "multus-cni-network",
  "plugins": [
    {
      "type": "multus",
      "capabilities": {
        "bandwidth": true,
        "portMappings": true
      },
      "cniConf": "/host/etc/cni/multus/net.d",
      "kubeconfig": "/etc/cni/net.d/multus.d/multus.kubeconfig",
      "delegates": [
        {
          "cniVersion": "1.0.0",
          "name": "cbr0",
          "plugins": [
            {
              "type": "flannel",
              "delegate": {
                "forceAddress": true,
                "hairpinMode": true,
                "isDefaultGateway": true
              }
            },
            {
              "type": "portmap",
              "capabilities": {
                "portMappings": true
              }
            },
            {
              "type": "bandwidth",
              "capabilities": {
                "bandwidth": true
              }
            }
          ]
        }
      ]
    }
  ]
}
EOF
}

ensure_multus_flannel_default_delegate() {
    local backend attached payload_b64 node_name node_ip node_role
    backend="$(seed_network_backend)"
    attached="$(printf '%s\n' "${SEED_ATTACHED_CNI_TYPE:-}" | tr '[:upper:]_' '[:lower:]-')"
    if { [ "${backend}" != "kube-ovn" ] && [ "${backend}" != "ovn" ]; } || \
       { [ "${attached}" != "kube-ovn" ] && [ "${attached}" != "ovn" ]; }; then
        return 0
    fi

    payload_b64="$(multus_flannel_conflist | base64 | tr -d '\n')"
    echo "[cni] ensuring Multus default delegate uses flannel, while Kube-OVN remains secondary"
    while IFS=$'\t' read -r node_name node_ip node_role; do
        [ -n "${node_name}" ] || continue
        echo "[cni] repairing ${node_name} (${node_ip})"
        ssh_node "${node_ip}" "
            set -eu
            CNI_DIR=/var/lib/rancher/k3s/agent/etc/cni/net.d
            CNI_BIN_DIR=/var/lib/rancher/k3s/data/cni
            K3S_CNI_BIN=/var/lib/rancher/k3s/data/current/bin/cni
            TMP=\$(mktemp)
            printf '%s' '${payload_b64}' | base64 -d > \"\${TMP}\"
            sudo mkdir -p \"\${CNI_BIN_DIR}\"
            sudo test -x \"\${K3S_CNI_BIN}\"
            for plugin in flannel bridge host-local bandwidth firewall; do
                sudo ln -sf \"\${K3S_CNI_BIN}\" \"\${CNI_BIN_DIR}/\${plugin}\"
            done
            sudo install -m 600 \"\${TMP}\" \"\${CNI_DIR}/00-multus.conflist\"
            sudo rm -f \"\${CNI_DIR}/00-multus.conf\"
            rm -f \"\${TMP}\"
            sudo test -x \"\${CNI_BIN_DIR}/flannel\"
            sudo test -x \"\${CNI_BIN_DIR}/bridge\"
            sudo test -x \"\${CNI_BIN_DIR}/host-local\"
            sudo test -x \"\${CNI_BIN_DIR}/bandwidth\"
            sudo test -f \"\${CNI_DIR}/10-flannel.conflist\"
            sudo test -f \"\${CNI_DIR}/01-kube-ovn.conflist\"
            ! sudo test -f \"\${CNI_DIR}/00-multus.conf\"
            sudo grep -q '\"type\": \"flannel\"' \"\${CNI_DIR}/00-multus.conflist\"
        "
    done < <(cluster_nodes_tsv)
}

run_in_seedpy310() {
    require_file "${SEEDPY310_CONDA_SH}"
    # shellcheck disable=SC1090
    source "${SEEDPY310_CONDA_SH}"
    conda activate "${SEEDPY310_ENV_NAME}"
    "$@"
}

print_test_context() {
    echo "experimentDir=${EXPERIMENT_DIR}"
    echo "topologySize=${SEED_TOPOLOGY_SIZE}"
    echo "inventory=${SEED_CLUSTER_INVENTORY_PATH}"
    echo "kubeconfig=${KUBECONFIG}"
    echo "namespace=${SEED_NAMESPACE}"
    echo "networkBackend=${SEED_NETWORK_BACKEND}"
    echo "attachedCniType=${SEED_ATTACHED_CNI_TYPE}"
}
