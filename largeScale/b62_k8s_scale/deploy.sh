#!/usr/bin/env bash
# Deploy a compiled SeedEMU/K8s manifest into the configured namespace.
#
# Inputs: an experiment directory containing output/k8s.yaml plus
# assignment.yaml-derived cluster identity from lib.sh.
# Outputs: deploy.log and output/deploy_batches/ split/batch artifacts.
# Side effects: creates the experiment namespace and Kubernetes resources in
# the active K3s cluster; optionally starts a background diagnostics monitor.
# Context: run from the controller host after build.sh has completed image
# preload for the same compiled manifest. Deploy tuning is defined in this file
# so generated assignment overrides cannot silently change deploy speed.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

setup_experiment_context "${1:-}"
DEPLOY_RESUME_REQUEST="${2:-}"
seed_load_cluster_nodes
ensure_kubeconfig
begin_stage_logging "deploy"

MANIFEST=""
DEPLOY_BATCH_SIZE=5
DEPLOY_BATCH_SLEEP_SECONDS=0
DEPLOY_SUBNET_BATCH_SIZE=250
DEPLOY_SUBNET_BATCH_SLEEP_SECONDS=0
DEPLOY_MONITOR_INTERVAL=20
DEPLOY_MONITOR_ENABLED=false
DEPLOY_VERBOSE_SNAPSHOTS=false
DEPLOY_STATIC_APPLY_MODE=batch
DEPLOY_CONTROLLER_APPLY_MODE=batch
DEPLOY_USE_APPLY=false
DEPLOY_FAIL_FAST=true
DEPLOY_CAPTURE_DESCRIBE_LIMIT=20
DEPLOY_KIND_DIR="${OUTPUT_DIR}/deploy_batches"
DEPLOY_WARMUP_BATCHES=2
DEPLOY_WARMUP_BATCH_SIZE=5
DEPLOY_POST_CONTROLLER_APPLY_SETTLE_SECONDS=5
DEPLOY_PRESSURE_CHECK_SECONDS=5
DEPLOY_STABILIZE_TIMEOUT_SECONDS=36000
DEPLOY_MAX_PENDING_PODS=8
DEPLOY_MAX_CREATING_PODS=0
DEPLOY_MAX_CREATING_PODS_PER_NODE=0
DEPLOY_MAX_NOTREADY_PODS=8
DEPLOY_MAX_FAILED_PODS=10
DEPLOY_REQUIRE_ALL_NODES_READY=true
DEPLOY_WAIT_KUBE_OVN_SUBNETS=true
DEPLOY_KUBE_OVN_SUBNET_TIMEOUT_SECONDS=36000
DEPLOY_POST_SUBNET_COOLDOWN_SECONDS=0
DEPLOY_NODE_STREAM_OBSERVE_TIMEOUT_SECONDS=120
DEPLOY_NODE_STREAM_MAX_ACTIVE_PER_NODE=1
DEPLOY_SKIP_KUBE_OVN_GATEWAY_CHECK=false
DEPLOY_RESUME_EXISTING=false

load_deploy_tuning_from_assignment() {
    # Load optional run-local deploy tuning from assignment.yaml. The script
    # defaults above remain the source of truth when a field is absent.
    local assignment_path="${B62_ASSIGNMENT_FILE:-}"
    [ -n "${assignment_path}" ] && [ -f "${assignment_path}" ] || return 0
    eval "$(
        python3 - "${assignment_path}" <<'PY'
import shlex
import sys

import yaml


path = sys.argv[1]
with open(path, "r", encoding="utf-8") as fh:
    data = yaml.safe_load(fh) or {}
deploy = data.get("deploy") if isinstance(data, dict) else {}
if not isinstance(deploy, dict):
    raise SystemExit(0)

fields = {
    "batchSize": "DEPLOY_BATCH_SIZE",
    "batchSleepSeconds": "DEPLOY_BATCH_SLEEP_SECONDS",
    "subnetBatchSize": "DEPLOY_SUBNET_BATCH_SIZE",
    "subnetBatchSleepSeconds": "DEPLOY_SUBNET_BATCH_SLEEP_SECONDS",
    "monitorInterval": "DEPLOY_MONITOR_INTERVAL",
    "monitorEnabled": "DEPLOY_MONITOR_ENABLED",
    "verboseSnapshots": "DEPLOY_VERBOSE_SNAPSHOTS",
    "staticApplyMode": "DEPLOY_STATIC_APPLY_MODE",
    "controllerApplyMode": "DEPLOY_CONTROLLER_APPLY_MODE",
    "useApply": "DEPLOY_USE_APPLY",
    "failFast": "DEPLOY_FAIL_FAST",
    "captureDescribeLimit": "DEPLOY_CAPTURE_DESCRIBE_LIMIT",
    "warmupBatches": "DEPLOY_WARMUP_BATCHES",
    "warmupBatchSize": "DEPLOY_WARMUP_BATCH_SIZE",
    "postControllerApplySettleSeconds": "DEPLOY_POST_CONTROLLER_APPLY_SETTLE_SECONDS",
    "pressureCheckSeconds": "DEPLOY_PRESSURE_CHECK_SECONDS",
    "stabilizeTimeoutSeconds": "DEPLOY_STABILIZE_TIMEOUT_SECONDS",
    "maxPendingPods": "DEPLOY_MAX_PENDING_PODS",
    "maxCreatingPods": "DEPLOY_MAX_CREATING_PODS",
    "maxCreatingPodsPerNode": "DEPLOY_MAX_CREATING_PODS_PER_NODE",
    "maxNotReadyPods": "DEPLOY_MAX_NOTREADY_PODS",
    "maxFailedPods": "DEPLOY_MAX_FAILED_PODS",
    "requireAllNodesReady": "DEPLOY_REQUIRE_ALL_NODES_READY",
    "waitKubeOvnSubnets": "DEPLOY_WAIT_KUBE_OVN_SUBNETS",
    "kubeOvnSubnetTimeoutSeconds": "DEPLOY_KUBE_OVN_SUBNET_TIMEOUT_SECONDS",
    "postSubnetCooldownSeconds": "DEPLOY_POST_SUBNET_COOLDOWN_SECONDS",
    "nodeStreamObserveTimeoutSeconds": "DEPLOY_NODE_STREAM_OBSERVE_TIMEOUT_SECONDS",
    "nodeStreamMaxActivePerNode": "DEPLOY_NODE_STREAM_MAX_ACTIVE_PER_NODE",
    "skipKubeOvnGatewayCheck": "DEPLOY_SKIP_KUBE_OVN_GATEWAY_CHECK",
}

for key, var in fields.items():
    if key not in deploy or deploy[key] is None:
        continue
    value = deploy[key]
    if isinstance(value, bool):
        value = "true" if value else "false"
    print(f"{var}={shlex.quote(str(value))}")
PY
    )"
}

case "${DEPLOY_RESUME_REQUEST}" in
    "")
        ;;
    --resume)
        DEPLOY_RESUME_EXISTING=true
        ;;
    *)
        echo "Usage: $0 <experiment_dir> [--resume]" >&2
        exit 2
        ;;
esac

if [ "${SEED_ATTACHED_CNI_TYPE:-${SEED_CNI_TYPE:-}}" = "kube-ovn" ]; then
    # Pure OVN/OVS remains sensitive to concurrent CNI ADD. Use a per-node
    # stream so each worker submits only one not-yet-Running Pod at a time,
    # while still allowing independent workers to progress in parallel.
    DEPLOY_BATCH_SIZE=5
    DEPLOY_BATCH_SLEEP_SECONDS=0
    DEPLOY_SUBNET_BATCH_SIZE=250
    DEPLOY_SUBNET_BATCH_SLEEP_SECONDS=0
    DEPLOY_CONTROLLER_APPLY_MODE=node-stream
    DEPLOY_WARMUP_BATCHES=2
    DEPLOY_WARMUP_BATCH_SIZE=5
    DEPLOY_POST_CONTROLLER_APPLY_SETTLE_SECONDS=0
    DEPLOY_MAX_PENDING_PODS=8
    DEPLOY_MAX_CREATING_PODS=0
    DEPLOY_MAX_CREATING_PODS_PER_NODE=0
    DEPLOY_MAX_NOTREADY_PODS=8
    DEPLOY_POST_SUBNET_COOLDOWN_SECONDS=60
    DEPLOY_NODE_STREAM_MAX_ACTIVE_PER_NODE=1
    DEPLOY_SKIP_KUBE_OVN_GATEWAY_CHECK=true
fi

load_deploy_tuning_from_assignment

case "${DEPLOY_NODE_STREAM_MAX_ACTIVE_PER_NODE}" in
    ""|*[!0-9]*)
        echo "DEPLOY_NODE_STREAM_MAX_ACTIVE_PER_NODE must be a positive integer, got: ${DEPLOY_NODE_STREAM_MAX_ACTIVE_PER_NODE}" >&2
        exit 2
        ;;
esac
if [ "${DEPLOY_NODE_STREAM_MAX_ACTIVE_PER_NODE}" -lt 1 ]; then
    echo "DEPLOY_NODE_STREAM_MAX_ACTIVE_PER_NODE must be >= 1, got: ${DEPLOY_NODE_STREAM_MAX_ACTIVE_PER_NODE}" >&2
    exit 2
fi

SSH_OPTS=(
  -i "${SEED_K3S_SSH_KEY}"
  -o BatchMode=yes
  -o ConnectTimeout=5
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
  -o LogLevel=ERROR
  -o IdentitiesOnly=yes
  -o IdentityAgent=none
)

vlan_cni_rows_by_node() {
    # Print required VLAN parent links from $1=runtimeManifestPath as
    # node-scoped TSV rows. Workloads without a fixed hostname selector are
    # expanded to all nodes so CNI ADD never misses a parent link.
    local manifest_path="$1"
    local helper
    helper="$(seed_manage_manifest_helper)"
    require_file "${helper}"
    run_in_seedpy310 env PYTHONPATH="${REPO_ROOT}" PYTHONNOUSERSITE=1 \
        python3 "${helper}" macvlan-vlan-interfaces-by-node \
            --manifest "${manifest_path}" \
            --cni-master-interface "${SEED_CNI_MASTER_INTERFACE}" \
            --nodes "${SEED_NODE_NAMES[@]}"
}

prepare_vlan_cni_interfaces() {
    # Create VLAN parent interfaces required by $1=runtimeManifestPath on the
    # K3s nodes that run workloads using those parents. This must happen before
    # Kubernetes starts CNI ADD for workload Pods.
    local manifest_path="$1"
    local rows row_count script_file
    rows="$(vlan_cni_rows_by_node "${manifest_path}")"
    [ -n "${rows}" ] || return 0

    row_count="$(printf '%s\n' "${rows}" | awk 'NF {count++} END {print count + 0}')"
    echo "[vlan-cni] preparing ${row_count} node-scoped VLAN-backed CNI parent entries across ${#SEED_NODE_NAMES[@]} node(s)"
    script_file="$(mktemp)"
    cat > "${script_file}" <<EOF_VLAN_SCRIPT
#!/usr/bin/env bash
set -euo pipefail
: "\${TARGET_NODE:?TARGET_NODE is required}"
if command -v modprobe >/dev/null 2>&1; then
    modprobe 8021q >/dev/null 2>&1 || true
fi
declare -A seen_base=()
while IFS=\$'\\t' read -r node iface base vlan bridge namespace name; do
    [ "\${node}" = "\${TARGET_NODE}" ] || continue
    [ -n "\${iface}" ] || continue
    [ "\${bridge}" = "-" ] && bridge=""
    if ! ip link show "\${base}" >/dev/null 2>&1; then
        echo "missing base interface \${base} for VLAN \${vlan} (\${namespace}/\${name})" >&2
        exit 1
    fi
    if [ -z "\${seen_base[\${base}]:-}" ]; then
        ip link set "\${base}" up
        seen_base["\${base}"]=1
    fi
    if ! ip link show "\${iface}" >/dev/null 2>&1; then
        ip link add link "\${base}" name "\${iface}" type vlan id "\${vlan}"
    fi
    master_path="\$(readlink "/sys/class/net/\${iface}/master" 2>/dev/null || true)"
    current_master=""
    [ -n "\${master_path}" ] && current_master="\$(basename "\${master_path}")"
    if [ -n "\${bridge}" ]; then
        if [ ! -x /opt/cni/bin/bridge ]; then
            mkdir -p /opt/cni/bin
            for candidate in /var/lib/rancher/k3s/data/cni/bridge /var/lib/rancher/k3s/data/*/bin/cni /usr/lib/cni/bridge; do
                if [ -x "\${candidate}" ]; then
                    ln -sf "\${candidate}" /opt/cni/bin/bridge
                    break
                fi
            done
        fi
        if [ ! -x /opt/cni/bin/bridge ]; then
            echo "missing bridge CNI plugin for VLAN-backed bridge network \${namespace}/\${name}" >&2
            exit 1
        fi
        if ! ip link show "\${bridge}" >/dev/null 2>&1; then
            ip link add name "\${bridge}" type bridge
        fi
        ip link set "\${bridge}" up
        if [ "\${current_master}" != "\${bridge}" ]; then
            [ -z "\${current_master}" ] || ip link set "\${iface}" nomaster
            ip link set "\${iface}" master "\${bridge}"
        fi
    elif [ -n "\${current_master}" ]; then
        ip link set "\${iface}" nomaster
    fi
    ip link set "\${iface}" up
done <<'EOF_VLANS'
${rows}
EOF_VLANS
EOF_VLAN_SCRIPT

    for i in "${!SEED_NODE_NAMES[@]}"; do
        local node_name planned_count quoted_node
        node_name="${SEED_NODE_NAMES[$i]}"
        planned_count="$(printf '%s\n' "${rows}" | awk -F '\t' -v node="${node_name}" '$1 == node {count++} END {print count + 0}')"
        echo "[vlan-cni] node=${node_name} ip=${SEED_NODE_IPS[$i]} planned=${planned_count}"
        [ "${planned_count}" -gt 0 ] || continue
        printf -v quoted_node '%q' "${node_name}"
        ssh "${SSH_OPTS[@]}" "${SEED_K3S_USER}@${SEED_NODE_IPS[$i]}" "sudo -n env TARGET_NODE=${quoted_node} bash -s" < "${script_file}"
    done
    rm -f "${script_file}"
}

echo "EXPERIMENT_DIR=${EXPERIMENT_DIR}"
echo "SEED_TOPOLOGY_SIZE=${SEED_TOPOLOGY_SIZE}"
echo "SEED_CLUSTER_INVENTORY_PATH=${SEED_CLUSTER_INVENTORY_PATH}"
echo "KUBECONFIG=${KUBECONFIG}"
echo "SEED_NAMESPACE=${SEED_NAMESPACE}"
echo "SEED_NETWORK_BACKEND=${SEED_NETWORK_BACKEND:-${SEED_CNI_TYPE:-kube-ovn}}"
echo "Batch size: ${DEPLOY_BATCH_SIZE}"
echo "Warmup batches: ${DEPLOY_WARMUP_BATCHES} x ${DEPLOY_WARMUP_BATCH_SIZE}"
echo "Batch sleep: ${DEPLOY_BATCH_SLEEP_SECONDS}s"
echo "Post-controller apply settle: ${DEPLOY_POST_CONTROLLER_APPLY_SETTLE_SECONDS}s"
echo "Subnet batch size: ${DEPLOY_SUBNET_BATCH_SIZE}"
echo "Subnet batch sleep: ${DEPLOY_SUBNET_BATCH_SLEEP_SECONDS}s"
echo "Monitor interval: ${DEPLOY_MONITOR_INTERVAL}s"
echo "Monitor enabled: ${DEPLOY_MONITOR_ENABLED}"
echo "Verbose snapshots: ${DEPLOY_VERBOSE_SNAPSHOTS}"
echo "Static apply mode: ${DEPLOY_STATIC_APPLY_MODE}"
echo "Controller apply mode: ${DEPLOY_CONTROLLER_APPLY_MODE}"
echo "Use apply: ${DEPLOY_USE_APPLY}"
echo "Resume existing namespace: ${DEPLOY_RESUME_EXISTING}"
echo "Pressure limits: pending<=${DEPLOY_MAX_PENDING_PODS}, creating<=${DEPLOY_MAX_CREATING_PODS}, creatingPerNode<=${DEPLOY_MAX_CREATING_PODS_PER_NODE}, notReady<=${DEPLOY_MAX_NOTREADY_PODS}, failed<=${DEPLOY_MAX_FAILED_PODS}"
echo "Kube-OVN subnet wait: ${DEPLOY_WAIT_KUBE_OVN_SUBNETS}, timeout=${DEPLOY_KUBE_OVN_SUBNET_TIMEOUT_SECONDS}s"
echo "Post-subnet cooldown: ${DEPLOY_POST_SUBNET_COOLDOWN_SECONDS}s"
echo "Node-stream observe timeout: ${DEPLOY_NODE_STREAM_OBSERVE_TIMEOUT_SECONDS}s"
echo "Node-stream max active per node: ${DEPLOY_NODE_STREAM_MAX_ACTIVE_PER_NODE}"
echo "Skip Kube-OVN gateway check via activation_strategy: ${DEPLOY_SKIP_KUBE_OVN_GATEWAY_CHECK}"
seed_print_cluster_nodes

MANIFEST="$(render_runtime_manifest)"
[ -f "${MANIFEST}" ] || {
    echo "Error: runtime manifest not found at ${MANIFEST}" >&2
    exit 1
}
prepare_vlan_cni_interfaces "${MANIFEST}"

patch_kube_ovn_gateway_check_skip() {
    # Add Kube-OVN activation_strategy annotations for every rendered provider
    # static-IP annotation. This makes kube-ovn-daemon set gwCheckModeDisabled
    # for attached NICs, avoiding per-interface ARP/ping gateway readiness waits
    # when Subnet.spec.disableGatewayCheck is already true.
    # Do not enable this for BIRD/FIB validation runs: attached logical ports can
    # stay activation-strategy=rarp/up=false and fail ARP, OSPF, and BGP checks.
    # Args: $1=renderedManifestPath.
    local manifest_path="$1"
    python3 - "${manifest_path}" <<'PY'
import re
import sys
from pathlib import Path

import yaml


manifest = Path(sys.argv[1])
docs = list(yaml.safe_load_all(manifest.read_text(encoding="utf-8")))
provider_re = re.compile(r"^(.+)\.kubernetes\.io/(?:ip_pool|ip_address)$")
changed = 0


def pod_annotations(doc):
    kind = doc.get("kind")
    if kind == "Pod":
        return doc.setdefault("metadata", {}).setdefault("annotations", {})
    if kind in {"Deployment", "DaemonSet", "StatefulSet", "Job"}:
        template = doc.setdefault("spec", {}).setdefault("template", {})
        return template.setdefault("metadata", {}).setdefault("annotations", {})
    return None


for doc in docs:
    if not isinstance(doc, dict):
        continue
    annotations = pod_annotations(doc)
    if not isinstance(annotations, dict):
        continue
    providers = []
    for key in list(annotations):
        match = provider_re.match(str(key))
        if match:
            providers.append(match.group(1))
    for provider in providers:
        activation_key = f"{provider}.kubernetes.io/activation_strategy"
        if annotations.get(activation_key) != "rarp":
            annotations[activation_key] = "rarp"
            changed += 1

if changed:
    manifest.write_text(yaml.safe_dump_all(docs, sort_keys=False), encoding="utf-8")
print(f"[kube-ovn-gateway-check] activation_strategy annotations added={changed}")
PY
}

if [ "${DEPLOY_SKIP_KUBE_OVN_GATEWAY_CHECK}" = "true" ] && [ "${SEED_ATTACHED_CNI_TYPE:-${SEED_CNI_TYPE:-}}" = "kube-ovn" ]; then
    patch_kube_ovn_gateway_check_skip "${MANIFEST}"
fi
echo "Runtime manifest: ${MANIFEST}"

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
    if [ "${DEPLOY_RESUME_EXISTING}" = "true" ]; then
        echo "Namespace ${SEED_NAMESPACE} already exists; resume mode is enabled."
        DEPLOY_USE_APPLY="true"
    else
    echo "Error: namespace ${SEED_NAMESPACE} already exists." >&2
    echo "Existing namespace resource summary: $(namespace_leftover_summary)" >&2
    exit 1
    fi
fi

if [ "${DEPLOY_RESUME_EXISTING}" != "true" ] && namespace_has_leftovers; then
    echo "Error: stale namespaced resources for ${SEED_NAMESPACE} still exist." >&2
    echo "Residual resource summary: $(namespace_leftover_summary)" >&2
    exit 1
fi

mkdir -p "${DEPLOY_KIND_DIR}"
if [ "${DEPLOY_RESUME_EXISTING}" = "true" ] && [ -d "${DEPLOY_KIND_DIR}/03-controllers-by-node" ] && [ -d "${DEPLOY_KIND_DIR}/01-subnets" ]; then
    echo "Resume mode: keeping existing split deploy artifacts in ${DEPLOY_KIND_DIR}"
else
    rm -rf "${DEPLOY_KIND_DIR:?}/"*
fi

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

node_creating_snapshot() {
    kubectl -n "${SEED_NAMESPACE}" get pods -o json 2>/dev/null | python3 -c '
import json
import sys

try:
    data = json.load(sys.stdin)
except Exception:
    print("max_creating_per_node=0 creating_by_node=none")
    raise SystemExit(0)

counts = {}
for item in data.get("items", []):
    status = str((item.get("status") or {}).get("phase") or "")
    reasons = []
    for key in ("initContainerStatuses", "containerStatuses"):
        for status_item in (item.get("status") or {}).get(key, []) or []:
            state = status_item.get("state") or {}
            waiting = state.get("waiting") or {}
            if waiting.get("reason"):
                reasons.append(str(waiting.get("reason")))
    creating = status in {"Pending"} and any(
        reason in {"ContainerCreating", "PodInitializing"} or reason.startswith("Init:")
        for reason in reasons
    )
    if not creating:
        continue
    node = str((item.get("spec") or {}).get("nodeName") or "unassigned")
    counts[node] = counts.get(node, 0) + 1

max_count = max(counts.values(), default=0)
detail = ",".join(f"{node}:{count}" for node, count in sorted(counts.items())) or "none"
print(f"max_creating_per_node={max_count} creating_by_node={detail}")
'
}

node_creating_count() {
    # Return the number of currently CNI-creating Pods on $1=nodeName.
    local node_name="$1"
    kubectl -n "${SEED_NAMESPACE}" get pods -o json 2>/dev/null | python3 -c '
import json
import sys

node_name = sys.argv[1]
try:
    data = json.load(sys.stdin)
except Exception:
    print(0)
    raise SystemExit(0)

count = 0
for item in data.get("items", []):
    spec = item.get("spec") or {}
    if spec.get("nodeName") != node_name:
        continue
    status = item.get("status") or {}
    phase = str(status.get("phase") or "")
    reasons = []
    for key in ("initContainerStatuses", "containerStatuses"):
        for status_item in status.get(key, []) or []:
            state = status_item.get("state") or {}
            waiting = state.get("waiting") or {}
            if waiting.get("reason"):
                reasons.append(str(waiting.get("reason")))
    if phase == "Pending" and any(
        reason in {"ContainerCreating", "PodInitializing"} or reason.startswith("Init:")
        for reason in reasons
    ):
        count += 1
print(count)
' "${node_name}"
}

node_active_count() {
    # Return the number of scheduled Pods on $1=nodeName that have not reached
    # Running/Succeeded/Failed yet. This is the node-local CNI ADD pressure
    # guard; it also counts newly observed Pending Pods before container status
    # reasons such as ContainerCreating become visible.
    local node_name="$1"
    kubectl -n "${SEED_NAMESPACE}" get pods -o json 2>/dev/null | python3 -c '
import json
import sys

node_name = sys.argv[1]
try:
    data = json.load(sys.stdin)
except Exception:
    print(0)
    raise SystemExit(0)

count = 0
for item in data.get("items", []):
    spec = item.get("spec") or {}
    if spec.get("nodeName") != node_name:
        continue
    status = item.get("status") or {}
    phase = str(status.get("phase") or "")
    if phase not in {"Running", "Succeeded", "Failed"}:
        count += 1
print(count)
' "${node_name}"
}

node_pod_count() {
    # Return the number of Pods currently scheduled on $1=nodeName.
    local node_name="$1"
    kubectl -n "${SEED_NAMESPACE}" get pods \
        --field-selector "spec.nodeName=${node_name}" \
        --no-headers 2>/dev/null | wc -l | tr -d ' '
}

node_running_count() {
    # Return the number of Pods in Running phase on $1=nodeName.
    local node_name="$1"
    kubectl -n "${SEED_NAMESPACE}" get pods \
        --field-selector "spec.nodeName=${node_name}" \
        --no-headers 2>/dev/null | awk '$3 == "Running" {count++} END {print count + 0}'
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
        local snapshot node_snapshot per_node_snapshot pending creating running_notready failed notready ready_nodes total_nodes max_creating_per_node per_node_ok
        snapshot="$(cluster_pressure_snapshot)"
        node_snapshot="$(node_readiness_snapshot)"
        per_node_snapshot="$(node_creating_snapshot)"
        pending="$(pressure_value "${snapshot}" "pending")"
        creating="$(pressure_value "${snapshot}" "creating")"
        running_notready="$(pressure_value "${snapshot}" "running_notready")"
        failed="$(pressure_value "${snapshot}" "failed")"
        notready=$((pending + creating + running_notready))
        ready_nodes="$(pressure_value "${node_snapshot}" "ready")"
        total_nodes="$(pressure_value "${node_snapshot}" "total")"
        max_creating_per_node="$(pressure_value "${per_node_snapshot}" "max_creating_per_node")"
        max_creating_per_node="${max_creating_per_node:-0}"
        per_node_ok=true
        if [ "${DEPLOY_MAX_CREATING_PODS_PER_NODE}" -ge 0 ] && [ "${max_creating_per_node}" -gt "${DEPLOY_MAX_CREATING_PODS_PER_NODE}" ]; then
            per_node_ok=false
        fi
        echo "[pressure:${stage_label}] ${snapshot} ${per_node_snapshot} nodes=${ready_nodes}/${total_nodes}"
        if [ "${failed}" -gt "${DEPLOY_MAX_FAILED_PODS}" ]; then
            echo "[pressure:${stage_label}] too many failed pods: ${failed}" >&2
            return 1
        fi
        if [ "${DEPLOY_REQUIRE_ALL_NODES_READY}" = "true" ] && [ "${ready_nodes}" -lt "${total_nodes}" ]; then
            :
        elif [ "${pending}" -le "${DEPLOY_MAX_PENDING_PODS}" ] && [ "${creating}" -le "${DEPLOY_MAX_CREATING_PODS}" ] && [ "${notready}" -le "${DEPLOY_MAX_NOTREADY_PODS}" ] && [ "${per_node_ok}" = "true" ]; then
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
    python3 - "${manifest}" "${DEPLOY_KIND_DIR}" "${split_dir}" "${SEED_NODE_NAMES[@]}" <<'PY'
import json
import re
import shutil
import sys
from pathlib import Path

manifest = Path(sys.argv[1])
kind_dir = Path(sys.argv[2])
split_dir = Path(sys.argv[3])
nodes = set(sys.argv[4:])

dirs = {
    "crds": kind_dir / "00-crds",
    "foundation": kind_dir / "01-foundation",
    "subnets": kind_dir / "01-subnets",
    "services": kind_dir / "02-services",
    "controllers": kind_dir / "03-controllers",
    "by_node": kind_dir / "03-controllers-by-node",
    "unpinned": kind_dir / "03-controllers-unpinned",
}
split_dir.mkdir(parents=True, exist_ok=True)
for path in dirs.values():
    path.mkdir(parents=True, exist_ok=True)
for node in nodes:
    (dirs["by_node"] / node).mkdir(parents=True, exist_ok=True)

kind_re = re.compile(r'^kind:\s*(\S+)', re.M)
kind_json_re = re.compile(r'"kind"\s*:\s*"([^"]+)"')
node_re = re.compile(r'nodeSelector:\s*(?:\n(?:[ \t]+.*\n)*)?[ \t]+kubernetes\.io/hostname:\s*["\']?([^"\'\n]+)')
node_json_re = re.compile(r'"nodeSelector"\s*:\s*\{[^{}]*"kubernetes\.io/hostname"\s*:\s*"([^"]+)"', re.S)


def split_docs(text: str):
    cur = []
    for line in text.splitlines():
        if line.strip() == "---":
            if cur:
                doc = "\n".join(cur).strip()
                if doc:
                    yield doc + "\n"
                cur = []
            continue
        cur.append(line)
    if cur:
        doc = "\n".join(cur).strip()
        if doc:
            yield doc + "\n"


def nested(data, keys):
    cur = data
    for key in keys:
        if not isinstance(cur, dict):
            return ""
        cur = cur.get(key)
    return cur if isinstance(cur, str) else ""


def meta(doc: str):
    kind = ""
    node = ""
    stripped = doc.lstrip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            kind = str(data.get("kind") or "")
            node = nested(data, ("spec", "template", "spec", "nodeSelector", "kubernetes.io/hostname"))
            if not node:
                node = nested(data, ("spec", "nodeSelector", "kubernetes.io/hostname"))
    if not kind:
        match = kind_re.search(doc) or kind_json_re.search(doc)
        if match:
            kind = match.group(1)
    if not node:
        match = node_re.search(doc) or node_json_re.search(doc)
        if match:
            node = match.group(1).strip()
    return kind, node


def copy_doc(src: Path, dest_dir: Path):
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest_dir / src.name)


for idx, doc in enumerate(split_docs(manifest.read_text(encoding="utf-8")), start=1):
    raw = split_dir / f"doc_{idx:05d}.yaml"
    raw.write_text(doc, encoding="utf-8")
    kind, node_selector = meta(doc)
    if kind == "CustomResourceDefinition":
        copy_doc(raw, dirs["crds"])
    elif kind in {
        "Namespace",
        "ServiceAccount",
        "ConfigMap",
        "Secret",
        "Role",
        "RoleBinding",
        "ClusterRole",
        "ClusterRoleBinding",
        "NetworkAttachmentDefinition",
        "NetworkAttachmentDefinitionList",
    }:
        copy_doc(raw, dirs["foundation"])
    elif kind == "Subnet":
        copy_doc(raw, dirs["subnets"])
    elif kind in {"Service", "Endpoints"}:
        copy_doc(raw, dirs["services"])
    elif kind in {"Deployment", "StatefulSet", "DaemonSet", "Job"}:
        copy_doc(raw, dirs["controllers"])
        if node_selector and node_selector in nodes:
            copy_doc(raw, dirs["by_node"] / node_selector)
        else:
            copy_doc(raw, dirs["unpinned"])
    else:
        copy_doc(raw, dirs["foundation"])
PY
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

apply_subnet_batches() {
    local dir="$1"
    local label="$2"
    shopt -s nullglob
    local files=( "${dir}"/*.yaml )
    shopt -u nullglob
    local total="${#files[@]}"
    [ "${total}" -gt 0 ] || { echo "[${label}] no resources"; return 0; }
    echo "[${label}] total resources: ${total}, batch_size=${DEPLOY_SUBNET_BATCH_SIZE}"
    local i=0
    local batch_no=0
    local batch_dir="${DEPLOY_KIND_DIR}/05-subnet-batches"
    mkdir -p "${batch_dir}"
    while [ "${i}" -lt "${total}" ]; do
        batch_no=$((batch_no + 1))
        local end batch_file j
        end=$((i + DEPLOY_SUBNET_BATCH_SIZE))
        [ "${end}" -le "${total}" ] || end="${total}"
        printf -v batch_file '%s/batch_%04d.yaml' "${batch_dir}" "${batch_no}"
        : > "${batch_file}"
        for ((j=i; j<end; j++)); do
            printf '%s\n' '---' >> "${batch_file}"
            cat "${files[j]}" >> "${batch_file}"
            printf '\n' >> "${batch_file}"
        done
        echo "[${label}] apply $(basename "${batch_file}") items $((i + 1))..${end}/${total} (size=$((end - i)))"
        if ! kapply "${batch_file}"; then
            echo "[${label}] failed on $(basename "${batch_file}")"
            [ "${DEPLOY_FAIL_FAST}" = "true" ] && return 1
        fi
        i="${end}"
        if [ "${i}" -lt "${total}" ] && [ "${DEPLOY_SUBNET_BATCH_SLEEP_SECONDS}" -gt 0 ]; then
            echo "[${label}] sleeping ${DEPLOY_SUBNET_BATCH_SLEEP_SECONDS}s before next subnet batch..."
            sleep "${DEPLOY_SUBNET_BATCH_SLEEP_SECONDS}"
        fi
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

post_subnet_cooldown() {
    # Wait only when the current runtime manifest actually depends on
    # Kube-OVN Subnet convergence.
    # Args: $1=expectedKubeOvnSubnetCount.
    local expected="${1:-${EXPECTED_KUBE_OVN_SUBNETS:-0}}"
    local backend
    case "${expected}" in
        ""|*[!0-9]*)
            expected=0
            ;;
    esac
    backend="$(seed_network_backend)"
    if [ "${backend}" != "kube-ovn" ] && [ "${backend}" != "ovn" ]; then
        echo "[kube-ovn-subnets] cooldown skipped for network backend=${backend}"
        return 0
    fi
    if [ "${expected}" -le 0 ]; then
        echo "[kube-ovn-subnets] cooldown skipped; no Subnet resources"
        return 0
    fi
    if [ "${DEPLOY_POST_SUBNET_COOLDOWN_SECONDS}" -le 0 ]; then
        return 0
    fi
    echo "[kube-ovn-subnets] cooldown ${DEPLOY_POST_SUBNET_COOLDOWN_SECONDS}s before creating workload pods"
    sleep "${DEPLOY_POST_SUBNET_COOLDOWN_SECONDS}"
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

existing_controller_count() {
    kubectl -n "${SEED_NAMESPACE}" get deployments.apps,statefulsets.apps,daemonsets.apps,jobs.batch --ignore-not-found --no-headers 2>/dev/null | wc -l | tr -d ' '
}

controllerBatchNoForIndex() {
    local index="$1"
    local covered=0
    local batch_no=0
    while [ "${covered}" -lt "${index}" ]; do
        batch_no=$((batch_no + 1))
        covered=$((covered + $(batch_size_for_iteration "${batch_no}")))
    done
    echo "${batch_no}"
}

apply_controllers_round_robin() {
    local label="$1"
    local batch_size="$2"
    mapfile -t files < <(controller_submission_order)
    local total="${#files[@]}"
    [ "${total}" -gt 0 ] || { echo "[${label}] no resources"; return 0; }
    echo "[${label}] total resources: ${total}, base_batch_size=${batch_size}"
    local i=0 batch_no=0
    if [ "${DEPLOY_RESUME_EXISTING}" = "true" ]; then
        i="$(existing_controller_count)"
        if [ "${i}" -gt "${total}" ]; then
            i="${total}"
        fi
        batch_no="$(controllerBatchNoForIndex "${i}")"
        echo "[${label}] resume existing controllers: ${i}/${total}; next_batch=$((batch_no + 1))"
        if [ "${i}" -ge "${total}" ]; then
            echo "[${label}] all controllers already exist."
            return 0
        fi
    fi
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
        if [ "${DEPLOY_POST_CONTROLLER_APPLY_SETTLE_SECONDS}" -gt 0 ]; then
            echo "[${label}] settling ${DEPLOY_POST_CONTROLLER_APPLY_SETTLE_SECONDS}s after apply..."
            sleep "${DEPLOY_POST_CONTROLLER_APPLY_SETTLE_SECONDS}"
        fi
        wait_for_deploy_pressure_budget "post-batch-${batch_no}"
        if [ "${end}" -lt "${total}" ] && [ "${DEPLOY_BATCH_SLEEP_SECONDS}" -gt 0 ]; then
            echo "[${label}] sleeping ${DEPLOY_BATCH_SLEEP_SECONDS}s before next batch..."
            sleep "${DEPLOY_BATCH_SLEEP_SECONDS}"
        fi
        i="${end}"
    done
}

wait_for_node_stream_slot() {
    # Wait until $1=nodeName has fewer than the configured number of active
    # not-yet-Running Pods. This bounds node-local CNI ADD pressure while still
    # allowing controlled overlap when DEPLOY_NODE_STREAM_MAX_ACTIVE_PER_NODE>1.
    local node_name="$1"
    local elapsed=0
    local timeout="${DEPLOY_STABILIZE_TIMEOUT_SECONDS}"
    while true; do
        local active creating
        active="$(node_active_count "${node_name}")"
        creating="$(node_creating_count "${node_name}")"
        echo "[node-stream:${node_name}] active=${active} creating=${creating} max_active=${DEPLOY_NODE_STREAM_MAX_ACTIVE_PER_NODE}"
        if [ "${active:-0}" -lt "${DEPLOY_NODE_STREAM_MAX_ACTIVE_PER_NODE}" ]; then
            return 0
        fi
        if [ "${elapsed}" -ge "${timeout}" ]; then
            echo "[node-stream:${node_name}] timeout after ${elapsed}s waiting for CNI slot" >&2
            return 1
        fi
        sleep "${DEPLOY_PRESSURE_CHECK_SECONDS}"
        elapsed=$((elapsed + DEPLOY_PRESSURE_CHECK_SECONDS))
    done
}

wait_for_node_pod_observed() {
    # Wait until $1=nodeName has more scheduled Pods than $2=previousCount.
    # This prevents the next node-local submit loop from racing ahead before
    # the Deployment controller and scheduler have materialized the new Pod.
    local node_name="$1"
    local previous_count="$2"
    local elapsed=0
    local timeout="${DEPLOY_NODE_STREAM_OBSERVE_TIMEOUT_SECONDS}"
    while true; do
        local current_count
        current_count="$(node_pod_count "${node_name}")"
        echo "[node-stream:${node_name}] pods=${current_count} previous=${previous_count}"
        if [ "${current_count:-0}" -gt "${previous_count}" ]; then
            return 0
        fi
        if [ "${elapsed}" -ge "${timeout}" ]; then
            echo "[node-stream:${node_name}] timeout after ${elapsed}s waiting for new Pod observation" >&2
            return 1
        fi
        sleep "${DEPLOY_PRESSURE_CHECK_SECONDS}"
        elapsed=$((elapsed + DEPLOY_PRESSURE_CHECK_SECONDS))
    done
}

wait_for_node_running_increment() {
    # Wait until $1=nodeName has more Running Pods than $2=previousCount.
    # This is the strict node-local serial gate: the next controller for a
    # node is not submitted until the previous one has completed CNI setup.
    local node_name="$1"
    local previous_count="$2"
    local elapsed=0
    local timeout="${DEPLOY_STABILIZE_TIMEOUT_SECONDS}"
    while true; do
        local running creating
        running="$(node_running_count "${node_name}")"
        creating="$(node_creating_count "${node_name}")"
        echo "[node-stream:${node_name}] running=${running} previous_running=${previous_count} creating=${creating}"
        if [ "${running:-0}" -gt "${previous_count}" ]; then
            return 0
        fi
        if [ "${elapsed}" -ge "${timeout}" ]; then
            echo "[node-stream:${node_name}] timeout after ${elapsed}s waiting for new Pod Running" >&2
            return 1
        fi
        sleep "${DEPLOY_PRESSURE_CHECK_SECONDS}"
        elapsed=$((elapsed + DEPLOY_PRESSURE_CHECK_SECONDS))
    done
}

apply_node_stream_worker() {
    # Apply one node's pinned controllers serially. $1=nodeName.
    local node_name="$1"
    local node_dir="${DEPLOY_KIND_DIR}/03-controllers-by-node/${node_name}"
    shopt -s nullglob
    local files=( "${node_dir}"/*.yaml )
    shopt -u nullglob
    local total="${#files[@]}"
    if [ "${total}" -eq 0 ]; then
        echo "[node-stream:${node_name}] no pinned controllers"
        return 0
    fi
    echo "[node-stream:${node_name}] total=${total}"
    local index=0 file
    for file in "${files[@]}"; do
        index=$((index + 1))
        wait_for_node_stream_slot "${node_name}"
        local previous_pods
        previous_pods="$(node_pod_count "${node_name}")"
        echo "[node-stream:${node_name}] apply ${index}/${total} $(basename "${file}")"
        if ! kapply "${file}"; then
            echo "[node-stream:${node_name}] failed on $(basename "${file}")" >&2
            return 1
        fi
        wait_for_node_pod_observed "${node_name}" "${previous_pods}"
        if [ "${DEPLOY_POST_CONTROLLER_APPLY_SETTLE_SECONDS}" -gt 0 ]; then
            sleep "${DEPLOY_POST_CONTROLLER_APPLY_SETTLE_SECONDS}"
        fi
    done
}

apply_controllers_node_stream() {
    # Run one serial controller submission loop per node. This keeps each
    # worker to one active CNI ADD while allowing different workers to progress
    # independently.
    local label="$1"
    local unpinned_dir="${DEPLOY_KIND_DIR}/03-controllers-unpinned"
    echo "[${label}] controller apply mode: node-stream"
    local pids=()
    local nodes=()
    local node
    for node in "${SEED_NODE_NAMES[@]}"; do
        apply_node_stream_worker "${node}" &
        pids+=( "$!" )
        nodes+=( "${node}" )
    done
    local rc=0
    local i
    for i in "${!pids[@]}"; do
        if ! wait "${pids[$i]}"; then
            echo "[${label}] node stream failed for ${nodes[$i]}" >&2
            rc=1
        fi
    done
    [ "${rc}" -eq 0 ] || return "${rc}"

    shopt -s nullglob
    local unpinned=( "${unpinned_dir}"/*.yaml )
    shopt -u nullglob
    if [ "${#unpinned[@]}" -gt 0 ]; then
        echo "[${label}] applying ${#unpinned[@]} unpinned controllers serially"
        for file in "${unpinned[@]}"; do
            wait_for_deploy_pressure_budget "unpinned-pre-$(basename "${file}")"
            echo "[${label}] apply unpinned $(basename "${file}")"
            kapply "${file}"
        done
    fi
    wait_for_deploy_pressure_budget "node-stream-final"
}

if [ "${DEPLOY_RESUME_EXISTING}" = "true" ] && kubectl get namespace "${SEED_NAMESPACE}" >/dev/null 2>&1; then
    echo "Resume mode: using existing namespace ${SEED_NAMESPACE}"
else
    echo "Creating namespace..."
    kubectl create namespace "${SEED_NAMESPACE}"
fi

if [ "${DEPLOY_MONITOR_ENABLED}" = "true" ]; then
    echo "Starting node monitor in background..."
    monitor_loop "${EXPERIMENT_DIR}" &
    MONITOR_PID=$!
    echo "Monitor PID: ${MONITOR_PID}"
else
    echo "Node monitor disabled; failure artifacts will still be captured on errors."
fi

if [ "${DEPLOY_RESUME_EXISTING}" = "true" ] && [ -d "${DEPLOY_KIND_DIR}/03-controllers-by-node" ] && [ -d "${DEPLOY_KIND_DIR}/01-subnets" ]; then
    echo "Resume mode: using existing split manifest in ${DEPLOY_KIND_DIR}"
else
    echo "Splitting manifest..."
    split_manifest "${MANIFEST}" "${DEPLOY_KIND_DIR}"
fi
EXPECTED_KUBE_OVN_SUBNETS="$(count_split_kind "${DEPLOY_KIND_DIR}/01-subnets" "Subnet")"

if [ "${DEPLOY_RESUME_EXISTING}" = "true" ]; then
    echo "Resume mode: applying CRDs idempotently."
    apply_dir_serial "${DEPLOY_KIND_DIR}/00-crds" "crds"
    echo "Resume mode: applying foundation objects idempotently."
    apply_dir_serial "${DEPLOY_KIND_DIR}/01-foundation" "foundation"
    echo "Resume mode: applying subnets idempotently in batches."
    apply_subnet_batches "${DEPLOY_KIND_DIR}/01-subnets" "subnets"
    wait_for_kube_ovn_subnets "${EXPECTED_KUBE_OVN_SUBNETS}"
    post_subnet_cooldown "${EXPECTED_KUBE_OVN_SUBNETS}"
    echo "Resume mode: applying services idempotently."
    apply_dir_serial "${DEPLOY_KIND_DIR}/02-services" "services"
else
    echo "Applying CRDs..."
    apply_dir_serial "${DEPLOY_KIND_DIR}/00-crds" "crds"
    echo "Applying foundation objects..."
    apply_dir_serial "${DEPLOY_KIND_DIR}/01-foundation" "foundation"
    echo "Applying Kube-OVN Subnets in batches..."
    apply_subnet_batches "${DEPLOY_KIND_DIR}/01-subnets" "subnets"
    wait_for_kube_ovn_subnets "${EXPECTED_KUBE_OVN_SUBNETS}"
    post_subnet_cooldown "${EXPECTED_KUBE_OVN_SUBNETS}"
    echo "Applying services..."
    apply_dir_serial "${DEPLOY_KIND_DIR}/02-services" "services"
fi
echo "Applying controllers in node-balanced batches..."
if [ "${DEPLOY_CONTROLLER_APPLY_MODE}" = "node-stream" ]; then
    apply_controllers_node_stream "controllers"
else
    apply_controllers_round_robin "controllers" "${DEPLOY_BATCH_SIZE}"
fi

echo ""
echo "Deploy submitted successfully."
echo "Next step: ${SCRIPT_DIR}/wait-ready.sh ${EXPERIMENT_DIR}"
cleanup_monitor
