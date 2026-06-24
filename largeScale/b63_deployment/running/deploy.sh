#!/usr/bin/env bash
# Deploy a compiled SeedEMU/K8s manifest into the configured namespace.
#
# Inputs: an experiment directory containing output/k8s.yaml and optional
# output/k8s.kube-ovn.yaml, plus explicit cluster and deploy parameters.
# Outputs: deploy.log and output/deploy_batches/ split/batch artifacts.
# Side effects: creates the experiment namespace and Kubernetes resources in
# the active K3s cluster; optionally starts a background diagnostics monitor.
# Context: run from the controller host after build.sh has completed image
# preload for the same runtime manifest.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

setup_experiment_context "$@"
load_deploy_config
seed_load_cluster_nodes
ensure_kubeconfig
begin_stage_logging "deploy"
ensure_multus_flannel_default_delegate

MANIFEST="$(render_runtime_manifest)"
DEPLOY_KIND_DIR="${DEPLOY_KIND_DIR:-${OUTPUT_DIR}/deploy_batches}"
DEPLOY_USE_APPLY="${DEPLOY_USE_APPLY:-false}"
DEPLOY_FAIL_FAST="${DEPLOY_FAIL_FAST:-true}"
DEPLOY_CAPTURE_DESCRIBE_LIMIT="${DEPLOY_CAPTURE_DESCRIBE_LIMIT:-20}"
DEPLOY_REQUIRE_ALL_NODES_READY="${DEPLOY_REQUIRE_ALL_NODES_READY:-true}"

SSH_OPTS=(
  -i "${SEED_K3S_SSH_KEY}"
  -o BatchMode=yes
  -o ConnectTimeout=5
  -o StrictHostKeyChecking=no
)

echo "EXPERIMENT_DIR=${EXPERIMENT_DIR}"
echo "SEED_TOPOLOGY_SIZE=${SEED_TOPOLOGY_SIZE}"
echo "SEED_CLUSTER_INVENTORY_PATH=${SEED_CLUSTER_INVENTORY_PATH}"
echo "KUBECONFIG=${KUBECONFIG}"
echo "SEED_NAMESPACE=${SEED_NAMESPACE}"
echo "Batch size: ${DEPLOY_BATCH_SIZE}"
echo "Warmup batches: ${DEPLOY_WARMUP_BATCHES} x ${DEPLOY_WARMUP_BATCH_SIZE}"
echo "Batch sleep: ${DEPLOY_BATCH_SLEEP_SECONDS}s"
echo "Monitor interval: ${DEPLOY_MONITOR_INTERVAL}s"
echo "Monitor enabled: ${DEPLOY_MONITOR_ENABLED}"
echo "Verbose snapshots: ${DEPLOY_VERBOSE_SNAPSHOTS}"
echo "Static apply mode: ${DEPLOY_STATIC_APPLY_MODE}"
echo "Controller apply mode: ${DEPLOY_CONTROLLER_APPLY_MODE}"
echo "Use apply: ${DEPLOY_USE_APPLY}"
echo "Network backend: ${SEED_NETWORK_BACKEND}"
echo "Attached CNI type: ${SEED_ATTACHED_CNI_TYPE}"
echo "Runtime manifest: ${MANIFEST}"
echo "Wait Kube-OVN Subnets: ${DEPLOY_WAIT_KUBE_OVN_SUBNETS}, timeout=${DEPLOY_KUBE_OVN_SUBNET_TIMEOUT_SECONDS}s, settle=${DEPLOY_KUBE_OVN_SUBNET_SETTLE_SECONDS}s"
echo "Restart Kube-OVN controller after Subnets: ${DEPLOY_RESTART_KUBE_OVN_CONTROLLER_AFTER_SUBNETS}, timeout=${DEPLOY_KUBE_OVN_CONTROLLER_RESTART_TIMEOUT_SECONDS}s, post-settle=${DEPLOY_KUBE_OVN_CONTROLLER_POST_RESTART_SETTLE_SECONDS}s"
echo "Pressure limits: pending<=${DEPLOY_MAX_PENDING_PODS}, creating<=${DEPLOY_MAX_CREATING_PODS}, notReady<=${DEPLOY_MAX_NOTREADY_PODS}, failed<=${DEPLOY_MAX_FAILED_PODS}"
seed_print_cluster_nodes

[ -f "${MANIFEST}" ] || {
    echo "Error: runtime manifest not found at ${MANIFEST}" >&2
    exit 1
}

if ! kubectl version --request-timeout=10s >/dev/null 2>&1; then
    echo "Error: kubectl cannot reach the cluster." >&2
    exit 1
fi

count_existing_namespaced_kind() {
    local kind="$1"
    kubectl -n "${SEED_NAMESPACE}" get "${kind}" --ignore-not-found --no-headers 2>/dev/null | wc -l | tr -d ' '
}

namespace_leftover_summary() {
    local pods deploys stss dss jobs nads configmaps secrets sas svcs roles rolebindings total
    pods="$(count_existing_namespaced_kind pods)"
    deploys="$(count_existing_namespaced_kind deployments.apps)"
    stss="$(count_existing_namespaced_kind statefulsets.apps)"
    dss="$(count_existing_namespaced_kind daemonsets.apps)"
    jobs="$(count_existing_namespaced_kind jobs.batch)"
    nads="$(count_existing_namespaced_kind network-attachment-definitions.k8s.cni.cncf.io)"
    configmaps="$(count_existing_namespaced_kind configmaps)"
    secrets="$(count_existing_namespaced_kind secrets)"
    sas="$(count_existing_namespaced_kind serviceaccounts)"
    svcs="$(count_existing_namespaced_kind services)"
    roles="$(count_existing_namespaced_kind roles.rbac.authorization.k8s.io)"
    rolebindings="$(count_existing_namespaced_kind rolebindings.rbac.authorization.k8s.io)"
    total=$((pods + deploys + stss + dss + jobs + nads + configmaps + secrets + sas + svcs + roles + rolebindings))
    printf 'total=%s pods=%s deploy=%s sts=%s ds=%s jobs=%s nads=%s cm=%s secrets=%s sa=%s svc=%s roles=%s rolebindings=%s\n' \
        "${total}" "${pods}" "${deploys}" "${stss}" "${dss}" "${jobs}" "${nads}" "${configmaps}" "${secrets}" "${sas}" "${svcs}" "${roles}" "${rolebindings}"
}

namespace_has_leftovers() {
    local summary total
    summary="$(namespace_leftover_summary)"
    total="$(awk '{for (i=1;i<=NF;i++){split($i,kv,"="); if (kv[1]=="total"){print kv[2]; exit}}}' <<< "${summary}")"
    [ "${total:-0}" -gt 0 ]
}

if kubectl get namespace "${SEED_NAMESPACE}" >/dev/null 2>&1; then
    echo "Error: namespace ${SEED_NAMESPACE} already exists." >&2
    echo "Existing namespace resource summary: $(namespace_leftover_summary)" >&2
    exit 1
fi

if namespace_has_leftovers; then
    echo "Error: stale namespaced resources for ${SEED_NAMESPACE} still exist." >&2
    echo "Residual resource summary: $(namespace_leftover_summary)" >&2
    exit 1
fi

mkdir -p "${DEPLOY_KIND_DIR}"
rm -rf "${DEPLOY_KIND_DIR:?}/"*

kapply() {
    local file="$1"
    if [ "${DEPLOY_USE_APPLY}" = "true" ]; then
        kubectl -n "${SEED_NAMESPACE}" apply -f "${file}"
    else
        kubectl -n "${SEED_NAMESPACE}" create -f "${file}"
    fi
}

safe_top_nodes() { kubectl top nodes 2>/dev/null || echo "[top nodes unavailable]"; }
safe_top_pods() { kubectl -n "${SEED_NAMESPACE}" top pods 2>/dev/null || true; }
safe_events() { kubectl -n "${SEED_NAMESPACE}" get events --sort-by='.lastTimestamp' 2>/dev/null || true; }
safe_get_pods_wide() { kubectl -n "${SEED_NAMESPACE}" get pods -o wide 2>/dev/null || true; }
safe_get_all() { kubectl -n "${SEED_NAMESPACE}" get all -o wide 2>/dev/null || true; }

cluster_pressure_snapshot() {
    kubectl -n "${SEED_NAMESPACE}" get pods --no-headers 2>/dev/null | awk '
        BEGIN {total=0; pending=0; creating=0; running_notready=0; failed=0; succeeded=0}
        {
            total++; ready=$2; status=$3; split(ready, a, "/"); ready_ok=(a[1]==a[2] && a[1] != "")
            if (status == "Pending") pending++
            else if (status == "ContainerCreating" || status == "PodInitializing" || status ~ /^Init:/) creating++
            else if (status == "Running" && !ready_ok) running_notready++
            else if (status == "Completed" || status == "Succeeded") succeeded++
            else if (status ~ /Error|CrashLoopBackOff|ImagePullBackOff|ErrImagePull|CreateContainerConfigError|CreateContainerError|RunContainerError|Evicted|OOMKilled/) failed++
        }
        END {printf("total=%d pending=%d creating=%d running_notready=%d failed=%d succeeded=%d\n", total, pending, creating, running_notready, failed, succeeded)}
    '
}

pressure_value() {
    local snapshot="$1"
    local key="$2"
    awk -v k="${key}" '{for (i=1;i<=NF;i++){split($i,kv,"="); if (kv[1]==k){print kv[2]; exit}}}' <<< "${snapshot}"
}

node_readiness_snapshot() {
    kubectl get nodes --no-headers 2>/dev/null | awk 'BEGIN {ready=0; total=0} {total++; if ($2=="Ready") ready++} END {printf("ready=%d total=%d\n", ready, total)}'
}

wait_for_deploy_pressure_budget() {
    local stage_label="$1"
    local timeout="${DEPLOY_STABILIZE_TIMEOUT_SECONDS}"
    local elapsed=0
    while true; do
        local snapshot node_snapshot pending creating running_notready failed notready ready_nodes total_nodes
        snapshot="$(cluster_pressure_snapshot)"
        node_snapshot="$(node_readiness_snapshot)"
        pending="$(pressure_value "${snapshot}" "pending")"
        creating="$(pressure_value "${snapshot}" "creating")"
        running_notready="$(pressure_value "${snapshot}" "running_notready")"
        failed="$(pressure_value "${snapshot}" "failed")"
        notready=$((pending + creating + running_notready))
        ready_nodes="$(pressure_value "${node_snapshot}" "ready")"
        total_nodes="$(pressure_value "${node_snapshot}" "total")"
        echo "[pressure:${stage_label}] ${snapshot} nodes=${ready_nodes}/${total_nodes}"
        if [ "${failed}" -gt "${DEPLOY_MAX_FAILED_PODS}" ]; then
            echo "[pressure:${stage_label}] too many failed pods: ${failed}" >&2
            return 1
        fi
        if [ "${DEPLOY_REQUIRE_ALL_NODES_READY}" = "true" ] && [ "${ready_nodes}" -lt "${total_nodes}" ]; then
            :
        elif [ "${pending}" -le "${DEPLOY_MAX_PENDING_PODS}" ] && [ "${creating}" -le "${DEPLOY_MAX_CREATING_PODS}" ] && [ "${notready}" -le "${DEPLOY_MAX_NOTREADY_PODS}" ]; then
            return 0
        fi
        if [ "${elapsed}" -ge "${timeout}" ]; then
            echo "[pressure:${stage_label}] timeout after ${elapsed}s" >&2
            return 1
        fi
        sleep "${DEPLOY_PRESSURE_CHECK_SECONDS}"
        elapsed=$((elapsed + DEPLOY_PRESSURE_CHECK_SECONDS))
    done
}

batch_size_for_iteration() {
    local batch_no="$1"
    if [ "${batch_no}" -le "${DEPLOY_WARMUP_BATCHES}" ]; then
        echo "${DEPLOY_WARMUP_BATCH_SIZE}"
    else
        echo "${DEPLOY_BATCH_SIZE}"
    fi
}

capture_node_ssh_stats() {
    local node_ip="$1"
    local out="$2"
    {
        echo "===== $(ts) node=${node_ip} ====="
        ssh "${SSH_OPTS[@]}" "${SEED_K3S_USER}@${node_ip}" '
            echo "--- uptime ---"
            uptime || true
            echo "--- /proc/loadavg ---"
            cat /proc/loadavg || true
            echo "--- free -m ---"
            free -m || true
            echo "--- vmstat 1 2 ---"
            vmstat 1 2 || true
            echo "--- top (batch) ---"
            top -b -n 1 | head -n 20 || true
        ' 2>&1 || echo "[ssh failed: ${node_ip}]"
        echo
    } >> "${out}"
}

monitor_loop() {
    local outdir="$1"
    local summary="${outdir}/node_monitor.log"
    local kube_top="${outdir}/kubectl_top_nodes.log"
    local pod_top="${outdir}/kubectl_top_pods.log"
    local events_log="${outdir}/events_during_deploy.log"
    while true; do
        { echo "===== $(ts) kubectl top nodes ====="; safe_top_nodes; echo; } >> "${kube_top}"
        { echo "===== $(ts) kubectl top pods ====="; safe_top_pods; echo; } >> "${pod_top}"
        { echo "===== $(ts) namespace events ====="; safe_events | tail -n 100; echo; } >> "${events_log}"
        for i in "${!SEED_NODE_NAMES[@]}"; do
            capture_node_ssh_stats "${SEED_NODE_IPS[$i]}" "${summary}"
        done
        sleep "${DEPLOY_MONITOR_INTERVAL}"
    done
}

cleanup_monitor() {
    if [ -n "${MONITOR_PID:-}" ]; then
        kill "${MONITOR_PID}" >/dev/null 2>&1 || true
        wait "${MONITOR_PID}" 2>/dev/null || true
    fi
}

capture_failure_artifacts() {
    local outdir="${EXPERIMENT_DIR}/deploy_failure_artifacts"
    mkdir -p "${outdir}"
    echo "[failure] collecting cluster artifacts into ${outdir}"
    kubectl get nodes -o wide > "${outdir}/nodes_wide.txt" 2>&1 || true
    kubectl describe nodes > "${outdir}/nodes_describe.txt" 2>&1 || true
    safe_get_all > "${outdir}/all_wide.txt"
    safe_get_pods_wide > "${outdir}/pods_wide.txt"
    safe_events > "${outdir}/events.txt"
    kubectl -n "${SEED_NAMESPACE}" get deploy,sts,ds,job -o wide > "${outdir}/controllers_wide.txt" 2>&1 || true
    kubectl -n "${SEED_NAMESPACE}" get network-attachment-definitions.k8s.cni.cncf.io -o yaml > "${outdir}/nad.yaml" 2>&1 || true
    safe_top_nodes > "${outdir}/top_nodes.txt"
    safe_top_pods > "${outdir}/top_pods.txt"
}

on_error() {
    local exit_code=$?
    echo ""
    echo "[ERROR] deploy failed at $(ts), exit_code=${exit_code}"
    cleanup_monitor
    capture_failure_artifacts
    exit "${exit_code}"
}

trap cleanup_monitor EXIT
trap on_error ERR INT TERM

manifest_kind_and_node() {
    local file="$1"
    python3 - "${file}" <<'PY'
import re, sys
path = sys.argv[1]
content = open(path, "r", encoding="utf-8").read()
kind = ""
node = ""
m = re.search(r'^kind:\s*(\S+)', content, re.M)
if not m:
    m = re.search(r'"kind"\s*:\s*"([^"]+)"', content)
if m:
    kind = m.group(1)
m = re.search(r'nodeSelector:\s*(?:\n(?:[ \t]+.*\n)*)?[ \t]+kubernetes\.io/hostname:\s*["\']?([^"\'\n]+)', content)
if not m:
    m = re.search(r'"nodeSelector"\s*:\s*\{[^{}]*"kubernetes\.io/hostname"\s*:\s*"([^"]+)"', content, re.S)
if m:
    node = m.group(1).strip()
print(kind)
print(node)
PY
}

split_manifest() {
    local manifest="$1"
    local split_dir="$2/raw_docs"
    mkdir -p "${split_dir}"
    awk '
        BEGIN {n=0; out=""}
        /^---[[:space:]]*$/ { if (out != "") close(out); n++; out=sprintf("'"${split_dir}"'/doc_%05d.yaml", n); next }
        { if (out == "") { n++; out=sprintf("'"${split_dir}"'/doc_%05d.yaml", n) } print >> out }
        END { if (out != "") close(out) }
    ' "${manifest}"

    mkdir -p "${DEPLOY_KIND_DIR}/00-crds" "${DEPLOY_KIND_DIR}/01-foundation" "${DEPLOY_KIND_DIR}/02-services" "${DEPLOY_KIND_DIR}/03-controllers" "${DEPLOY_KIND_DIR}/03-controllers-by-node" "${DEPLOY_KIND_DIR}/03-controllers-unpinned"
    for node in "${SEED_NODE_NAMES[@]}"; do
        mkdir -p "${DEPLOY_KIND_DIR}/03-controllers-by-node/${node}"
    done

    shopt -s nullglob
    for f in "${split_dir}"/*.yaml; do
        mapfile -t meta < <(manifest_kind_and_node "${f}" 2>/dev/null || true)
        kind="${meta[0]:-}"
        node_selector="${meta[1]:-}"
        case "${kind}" in
            CustomResourceDefinition) cp "${f}" "${DEPLOY_KIND_DIR}/00-crds/" ;;
            Namespace|ServiceAccount|ConfigMap|Secret|Role|RoleBinding|ClusterRole|ClusterRoleBinding|NetworkAttachmentDefinition|NetworkAttachmentDefinitionList) cp "${f}" "${DEPLOY_KIND_DIR}/01-foundation/" ;;
            Service|Endpoints) cp "${f}" "${DEPLOY_KIND_DIR}/02-services/" ;;
            Deployment|StatefulSet|DaemonSet|Job)
                cp "${f}" "${DEPLOY_KIND_DIR}/03-controllers/"
                if [ -n "${node_selector}" ] && [ -d "${DEPLOY_KIND_DIR}/03-controllers-by-node/${node_selector}" ]; then
                    cp "${f}" "${DEPLOY_KIND_DIR}/03-controllers-by-node/${node_selector}/"
                else
                    cp "${f}" "${DEPLOY_KIND_DIR}/03-controllers-unpinned/"
                fi
                ;;
            *) cp "${f}" "${DEPLOY_KIND_DIR}/01-foundation/" ;;
        esac
    done
    shopt -u nullglob
}

apply_dir_serial() {
    local dir="$1"
    local label="$2"
    shopt -s nullglob
    local files=( "${dir}"/*.yaml )
    shopt -u nullglob
    [ "${#files[@]}" -gt 0 ] || return 0
    if [ "${DEPLOY_STATIC_APPLY_MODE}" = "batch" ]; then
        local batch_dir batch_file f
        batch_dir="${DEPLOY_KIND_DIR}/04-static-batches"
        mkdir -p "${batch_dir}"
        batch_file="${batch_dir}/${label}.yaml"
        : > "${batch_file}"
        for f in "${files[@]}"; do
            printf '%s\n' '---' >> "${batch_file}"
            cat "${f}" >> "${batch_file}"
            printf '\n' >> "${batch_file}"
        done
        echo "[${label}] applying ${#files[@]} objects as one batch"
        kapply "${batch_file}"
        return 0
    fi
    for f in "${files[@]}"; do
        echo "[${label}] $(basename "${f}")"
        kapply "${f}"
    done
}

count_split_kind() {
    local dir="$1"
    local kind="$2"
    python3 - "${dir}" "${kind}" <<'PY'
from pathlib import Path
import sys
import yaml

root = Path(sys.argv[1])
kind = sys.argv[2]
count = 0
if root.is_dir():
    for path in root.rglob("*.yaml"):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(doc, dict) and doc.get("kind") == kind:
            count += 1
print(count)
PY
}

kube_ovn_subnet_snapshot() {
    python3 - "${SEED_NAMESPACE}" <<'PY'
import json
import subprocess
import sys

namespace = sys.argv[1]
try:
    raw = subprocess.check_output(
        ["kubectl", "get", "subnet.kubeovn.io", "-o", "json"],
        text=True,
        stderr=subprocess.DEVNULL,
    )
except subprocess.CalledProcessError:
    print("total=0 processed=0 ready=0 error=0")
    raise SystemExit(0)

data = json.loads(raw)
total = processed = ready = error = 0
for item in data.get("items", []):
    spec = item.get("spec") or {}
    provider = str(spec.get("provider") or "")
    if f".{namespace}" not in provider:
        continue
    total += 1
    metadata = item.get("metadata") or {}
    status = item.get("status") or {}
    if metadata.get("finalizers") and status:
        processed += 1
    conditions = status.get("conditions") or []
    if any(cond.get("type") == "Ready" and cond.get("status") == "True" for cond in conditions):
        ready += 1
    if any(cond.get("type") == "Error" and cond.get("status") == "True" for cond in conditions):
        error += 1
print(f"total={total} processed={processed} ready={ready} error={error}")
PY
}

wait_for_kube_ovn_subnets() {
    local expected="$1"
    local elapsed=0
    local timeout="${DEPLOY_KUBE_OVN_SUBNET_TIMEOUT_SECONDS}"
    [ "${expected}" -gt 0 ] || return 0
    if [ "${DEPLOY_WAIT_KUBE_OVN_SUBNETS}" != "true" ]; then
        echo "[kube-ovn-subnets] wait disabled; expected=${expected}"
        return 0
    fi

    echo "[kube-ovn-subnets] waiting for ${expected} Subnets to be processed by kube-ovn-controller"
    while true; do
        local snapshot total processed ready error
        snapshot="$(kube_ovn_subnet_snapshot)"
        total="$(pressure_value "${snapshot}" "total")"
        processed="$(pressure_value "${snapshot}" "processed")"
        ready="$(pressure_value "${snapshot}" "ready")"
        error="$(pressure_value "${snapshot}" "error")"
        echo "[kube-ovn-subnets] ${snapshot} expected=${expected}"
        if [ "${error:-0}" -gt 0 ]; then
            echo "[kube-ovn-subnets] kube-ovn reported ${error} subnet errors" >&2
            return 1
        fi
        if [ "${total:-0}" -ge "${expected}" ] && [ "${processed:-0}" -ge "${expected}" ] && [ "${ready:-0}" -ge "${expected}" ]; then
            return 0
        fi
        if [ "${elapsed}" -ge "${timeout}" ]; then
            echo "[kube-ovn-subnets] timeout after ${elapsed}s" >&2
            return 1
        fi
        sleep "${DEPLOY_PRESSURE_CHECK_SECONDS}"
        elapsed=$((elapsed + DEPLOY_PRESSURE_CHECK_SECONDS))
    done
}

settle_after_kube_ovn_subnets() {
    local seconds="${DEPLOY_KUBE_OVN_SUBNET_SETTLE_SECONDS}"
    [ "${seconds}" -gt 0 ] || return 0
    echo "[kube-ovn-subnets] all Subnets Ready; waiting ${seconds}s for kube-ovn-controller IPAM to settle"
    sleep "${seconds}"
}

restart_kube_ovn_controller_for_subnet_ipam() {
    local phase="$1"
    [ "${SEED_NETWORK_BACKEND}" = "kube-ovn" ] || return 0
    [ "${DEPLOY_RESTART_KUBE_OVN_CONTROLLER_AFTER_SUBNETS}" = "true" ] || return 0
    if ! kubectl -n kube-system get deployment kube-ovn-controller >/dev/null 2>&1; then
        echo "[kube-ovn-controller] deployment not found; skipping restart"
        return 0
    fi

    echo "[kube-ovn-controller] restarting controller ${phase}"
    kubectl -n kube-system rollout restart deployment/kube-ovn-controller
    kubectl -n kube-system rollout status deployment/kube-ovn-controller --timeout="${DEPLOY_KUBE_OVN_CONTROLLER_RESTART_TIMEOUT_SECONDS}s"
    if [ "${DEPLOY_KUBE_OVN_CONTROLLER_POST_RESTART_SETTLE_SECONDS}" -gt 0 ]; then
        echo "[kube-ovn-controller] waiting ${DEPLOY_KUBE_OVN_CONTROLLER_POST_RESTART_SETTLE_SECONDS}s after restart"
        sleep "${DEPLOY_KUBE_OVN_CONTROLLER_POST_RESTART_SETTLE_SECONDS}"
    fi
}

controller_submission_order() {
    local buckets_dir="${DEPLOY_KIND_DIR}/03-controllers-by-node"
    local unpinned_dir="${DEPLOY_KIND_DIR}/03-controllers-unpinned"
    python3 - "${buckets_dir}" "${unpinned_dir}" "${SEED_NODE_NAMES[@]}" <<'PY'
import os, sys
buckets_dir = sys.argv[1]
unpinned_dir = sys.argv[2]
nodes = sys.argv[3:]
queues = {}
for node in nodes:
    node_dir = os.path.join(buckets_dir, node)
    files = []
    if os.path.isdir(node_dir):
        files = sorted(os.path.join(node_dir, name) for name in os.listdir(node_dir) if name.endswith(".yaml"))
    queues[node] = files
ordered = []
while True:
    progressed = False
    for node in nodes:
        if queues[node]:
            ordered.append(queues[node].pop(0))
            progressed = True
    if not progressed:
        break
if os.path.isdir(unpinned_dir):
    for name in sorted(os.listdir(unpinned_dir)):
        if name.endswith(".yaml"):
            ordered.append(os.path.join(unpinned_dir, name))
for path in ordered:
    print(path)
PY
}

apply_controllers_round_robin() {
    local label="$1"
    local batch_size="$2"
    mapfile -t files < <(controller_submission_order)
    local total="${#files[@]}"
    [ "${total}" -gt 0 ] || { echo "[${label}] no resources"; return 0; }
    echo "[${label}] total resources: ${total}, base_batch_size=${batch_size}"
    local i=0 batch_no=0
    while [ "${i}" -lt "${total}" ]; do
        batch_no=$((batch_no + 1))
        local current_batch_size end
        current_batch_size="$(batch_size_for_iteration "${batch_no}")"
        end=$((i + current_batch_size))
        [ "${end}" -le "${total}" ] || end="${total}"
        wait_for_deploy_pressure_budget "pre-batch-${batch_no}"
        echo ""
        echo "[${label}] batch ${batch_no}: items $((i + 1))..${end}/${total} (size=${current_batch_size})"
        if [ "${DEPLOY_CONTROLLER_APPLY_MODE}" = "batch" ]; then
            local batch_dir batch_file
            batch_dir="${DEPLOY_KIND_DIR}/03-controller-batches"
            mkdir -p "${batch_dir}"
            printf -v batch_file '%s/batch_%04d.yaml' "${batch_dir}" "${batch_no}"
            : > "${batch_file}"
            for ((j=i; j<end; j++)); do
                printf '%s\n' '---' >> "${batch_file}"
                cat "${files[j]}" >> "${batch_file}"
                printf '\n' >> "${batch_file}"
            done
            echo "[${label}] apply $(basename "${batch_file}")"
            if ! kapply "${batch_file}"; then
                echo "[${label}] failed on $(basename "${batch_file}")"
                [ "${DEPLOY_FAIL_FAST}" = "true" ] && return 1
            fi
        else
            for ((j=i; j<end; j++)); do
                echo "[${label}] apply $(basename "${files[j]}")"
                if ! kapply "${files[j]}"; then
                    echo "[${label}] failed on $(basename "${files[j]}")"
                    [ "${DEPLOY_FAIL_FAST}" = "true" ] && return 1
                fi
            done
        fi
        if [ "${DEPLOY_VERBOSE_SNAPSHOTS}" = "true" ]; then
            echo "[${label}] post-batch quick snapshot:"
            kubectl get nodes -o wide || true
            kubectl -n "${SEED_NAMESPACE}" get pods -o wide | tail -n 30 || true
            safe_top_nodes || true
        fi
        wait_for_deploy_pressure_budget "post-batch-${batch_no}"
        if [ "${end}" -lt "${total}" ] && [ "${DEPLOY_BATCH_SLEEP_SECONDS}" -gt 0 ]; then
            echo "[${label}] sleeping ${DEPLOY_BATCH_SLEEP_SECONDS}s before next batch..."
            sleep "${DEPLOY_BATCH_SLEEP_SECONDS}"
        fi
        i="${end}"
    done
}

echo "Creating namespace..."
kubectl create namespace "${SEED_NAMESPACE}"

if [ "${DEPLOY_MONITOR_ENABLED}" = "true" ]; then
    echo "Starting node monitor in background..."
    monitor_loop "${EXPERIMENT_DIR}" &
    MONITOR_PID=$!
    echo "Monitor PID: ${MONITOR_PID}"
else
    echo "Node monitor disabled; failure artifacts will still be captured on errors."
fi

echo "Splitting manifest..."
split_manifest "${MANIFEST}" "${DEPLOY_KIND_DIR}"
EXPECTED_KUBE_OVN_SUBNETS="$(count_split_kind "${DEPLOY_KIND_DIR}/01-foundation" "Subnet")"

echo "Applying CRDs..."
apply_dir_serial "${DEPLOY_KIND_DIR}/00-crds" "crds"
echo "Applying foundation objects..."
apply_dir_serial "${DEPLOY_KIND_DIR}/01-foundation" "foundation"
restart_kube_ovn_controller_for_subnet_ipam "after foundation Subnets are created"
wait_for_kube_ovn_subnets "${EXPECTED_KUBE_OVN_SUBNETS}"
settle_after_kube_ovn_subnets
restart_kube_ovn_controller_for_subnet_ipam "after secondary Subnets are Ready"
echo "Applying services..."
apply_dir_serial "${DEPLOY_KIND_DIR}/02-services" "services"
echo "Applying controllers in node-balanced batches..."
apply_controllers_round_robin "controllers" "${DEPLOY_BATCH_SIZE}"

echo ""
echo "Deploy submitted successfully."
echo "Next step: ${SCRIPT_DIR}/wait-ready.sh ${EXPERIMENT_DIR}"
cleanup_monitor
