#!/usr/bin/env bash
set -euo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLACEMENT_PLANNER="${SEED_PLACEMENT_PLANNER:-${TEST_DIR}/seed_k8s_plan_real_topology_by_as.py}"

SEEDPY310_CONDA_SH="${SEEDPY310_CONDA_SH:-$HOME/anaconda3/etc/profile.d/conda.sh}"
SEEDPY310_ENV_NAME="${SEEDPY310_ENV_NAME:-seedpy310}"

resolve_assignment_file() {
    # Resolve the assignment source for a stage. $1 is the absolute run
    # directory. Priority: explicit B62_ASSIGNMENT_FILE, run-local copy,
    # repository default.
    local run_dir="$1"
    if [ -n "${B62_ASSIGNMENT_FILE:-}" ]; then
        printf '%s\n' "${B62_ASSIGNMENT_FILE}"
        return 0
    fi
    if [ -f "${run_dir}/assignment.yaml" ]; then
        printf '%s\n' "${run_dir}/assignment.yaml"
        return 0
    fi
    printf '%s\n' "${TEST_DIR}/assignment.yaml"
}

load_assignment_runtime() {
    # Export common runtime values derived directly from assignment.yaml.
    # $1=assignmentPath, $2=runDir.
    local assignment_path="$1"
    local run_dir="$2"
    eval "$(
        python3 - "${assignment_path}" "${run_dir}" "${TEST_DIR}" <<'PY'
import os
import shlex
import sys
from pathlib import Path

import yaml


def get(data, dotted, default=None):
    cur = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def resolve(value, base):
    path = Path(os.path.expandvars(os.path.expanduser(str(value))))
    if path.is_absolute():
        return path.resolve()
    return (base / path).resolve()


def find_repo_root(start):
    for candidate in (start, *start.parents):
        if (candidate / "setup.py").is_file() and (candidate / "seedemu").is_dir():
            return candidate
    raise RuntimeError(f"Cannot find SeedEMU repository root above {start}")


assignment_path = resolve(sys.argv[1], Path.cwd())
run_dir = resolve(sys.argv[2], Path.cwd())
script_dir = resolve(sys.argv[3], Path.cwd())
assignment = yaml.safe_load(assignment_path.read_text(encoding="utf-8")) or {}

topology_size = int(get(assignment, "experiment.topologySize"))
worker_count = int(get(assignment, "experiment.workerCount"))
namespace = str(get(assignment, "experiment.namespace", "") or f"seedemu-b62-{topology_size}")
experiment_name = str(get(assignment, "experiment.name", "") or f"b62-scale-{topology_size}")
source_root = resolve(get(assignment, "experiment.sourceRoot", find_repo_root(script_dir)), script_dir)
topology_dir = resolve(get(assignment, "experiment.topologyDir", Path.home() / "seed-emulator" / "topology"), script_dir)
cluster_name = f"{get(assignment, 'kvm.clusterNamePrefix', 'seedemu-b62')}-w{worker_count}"
ip_prefix = str(get(assignment, "kvm.ipPrefix"))
master_ip = f"{ip_prefix}.{int(get(assignment, 'kvm.master.ipStart'))}"
registry_port = int(get(assignment, "registry.port", 5000))
network_backend = str(get(assignment, "networking.backend", "kube-ovn")).strip().lower() or "kube-ovn"
cni_type = str(get(assignment, "networking.cniType", "kube-ovn")).strip().lower() or "kube-ovn"
local_link_cni_type = str(get(assignment, "networking.localLinkCniType", cni_type) or cni_type).strip().lower()
attached_cni_type = str(get(assignment, "networking.attachedCniType", network_backend) or network_backend).strip().lower()

values = {
    "B62_ASSIGNMENT_FILE": str(assignment_path),
    "B62_WORKER_COUNT": str(worker_count),
    "B62_TOPOLOGY_SIZE": str(topology_size),
    "B62_EXPERIMENT_NAME": experiment_name,
    "B62_CONFIG_KVM_OVN_PATH": str(script_dir / "configKvmOvn.yaml"),
    "B62_CONFIG_K3S_PATH": str(script_dir / "configK3s.yaml"),
    "B62_KUBECONFIG_PATH": str(script_dir / "kubeconfig.yaml"),
    "B62_INVENTORY_PATH": str(script_dir / "cluster.inventory.yaml"),
    "B62_RESOURCE_PLAN_YAML": str(script_dir / "resourcePlan.yaml"),
    "B62_RESOURCE_PLAN_JSON": str(script_dir / "resourcePlan.json"),
    "SEED_SOURCE_ROOT": str(source_root),
    "SEED_TOPOLOGY_SIZE": str(topology_size),
    "SEED_K3S_CLUSTER_NAME": cluster_name,
    "SEED_CLUSTER_INVENTORY": cluster_name,
    "SEED_CLUSTER_INVENTORY_PATH": str(script_dir / "cluster.inventory.yaml"),
    "SEED_KUBECONFIG_PATH": str(script_dir / "kubeconfig.yaml"),
    "KUBECONFIG": str(script_dir / "kubeconfig.yaml"),
    "SEED_K3S_USER": str(get(assignment, "kvm.ssh.user", "ubuntu")),
    "SEED_K3S_SSH_KEY": str(resolve(get(assignment, "kvm.ssh.key", "~/.ssh/id_ed25519"), script_dir)),
    "SEED_NAMESPACE": namespace,
    "SEED_REAL_TOPOLOGY_DIR": str(topology_dir),
    "SEED_K3S_MASTER_IP": master_ip,
    "SEED_REGISTRY_HOST": master_ip,
    "SEED_REGISTRY_PORT": str(registry_port),
    "SEED_REGISTRY": f"{master_ip}:{registry_port}",
    "SEED_NETWORK_BACKEND": network_backend,
    "SEED_CNI_TYPE": cni_type,
    "SEED_LOCAL_LINK_CNI_TYPE": local_link_cni_type,
    "SEED_ATTACHED_CNI_TYPE": attached_cni_type,
    "SEED_CNI_MASTER_INTERFACE": str(get(assignment, "networking.cniMasterInterface", "ens2")),
}

for key, value in values.items():
    print(f"export {key}={shlex.quote(value)}")
PY
    )"
    export REPO_ROOT="${SEED_SOURCE_ROOT}"
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

    load_assignment_runtime "$(resolve_assignment_file "${EXPERIMENT_DIR}")" "${EXPERIMENT_DIR}"
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
    # Append one host resource sample for $1=stageName into
    # EXPERIMENT_DIR/loadAverage.log. This intentionally samples the controller
    # host running the experiment script, matching cpu_monitor.sh's local-host
    # model.
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
    # Start a low-frequency background host monitor for $1=stageName. The child
    # exits when this shell exits, so it does not depend on EXIT traps that
    # individual stage scripts may override.
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
    run_in_seedpy310 python3 "${PLACEMENT_PLANNER}" \
        "${topology_file}" \
        "${assignment_file}" \
        "${nodes_json_file}" \
        "${mapping_file}" \
        "${plan_file}"
}

seed_network_backend() {
    local backend="${SEED_NETWORK_BACKEND:-${SEED_CNI_TYPE:-kube-ovn}}"
    printf '%s\n' "${backend}" | tr '[:upper:]' '[:lower:]'
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
        printf '%s\n' "${target_manifest}"
        return 0
    fi

    require_file "${helper}"
    if [ ! -f "${target_manifest}" ] || [ "${source_manifest}" -nt "${target_manifest}" ] || [ "${helper}" -nt "${target_manifest}" ]; then
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
    fi
    printf '%s\n' "${target_manifest}"
}

print_test_context() {
    echo "EXPERIMENT_DIR=${EXPERIMENT_DIR}"
    echo "SEED_TOPOLOGY_SIZE=${SEED_TOPOLOGY_SIZE}"
    echo "SEED_CLUSTER_INVENTORY_PATH=${SEED_CLUSTER_INVENTORY_PATH}"
    echo "KUBECONFIG=${KUBECONFIG}"
    echo "SEED_NAMESPACE=${SEED_NAMESPACE}"
}
