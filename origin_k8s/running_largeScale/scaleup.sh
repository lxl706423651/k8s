#!/usr/bin/env bash
set -Eeuo pipefail

OUTPUT_DIR="${OUTPUT_DIR:?}"
SCALE_MANIFEST="${SCALE_MANIFEST:?}"
KUSTOMIZATION="${KUSTOMIZATION:?}"
RENDERED_MANIFEST="${RENDERED_MANIFEST:?}"
DEPLOY_BATCH_DIR="${DEPLOY_BATCH_DIR:?}"
INVENTORY="${INVENTORY:?}"
NODE_ROLE_FILTER="${NODE_ROLE_FILTER:-all}"
KUBECONFIG="${KUBECONFIG:?}"
HELPER="${HELPER:?}"
DEPLOY_BATCH_SIZE="${DEPLOY_BATCH_SIZE:-20}"
DEPLOY_BATCH_SLEEP_SECONDS="${DEPLOY_BATCH_SLEEP_SECONDS:-20}"
DEPLOY_WARMUP_BATCHES="${DEPLOY_WARMUP_BATCHES:-3}"
DEPLOY_WARMUP_BATCH_SIZE="${DEPLOY_WARMUP_BATCH_SIZE:-5}"
DEPLOY_PRESSURE_CHECK_SECONDS="${DEPLOY_PRESSURE_CHECK_SECONDS:-10}"
DEPLOY_STABILIZE_TIMEOUT_SECONDS="${DEPLOY_STABILIZE_TIMEOUT_SECONDS:-3600}"
DEPLOY_MAX_PENDING_PODS="${DEPLOY_MAX_PENDING_PODS:-100}"
DEPLOY_MAX_CREATING_PODS="${DEPLOY_MAX_CREATING_PODS:-150}"
DEPLOY_MAX_NOTREADY_PODS="${DEPLOY_MAX_NOTREADY_PODS:-250}"
DEPLOY_MAX_FAILED_PODS="${DEPLOY_MAX_FAILED_PODS:-5}"

namespace="$(python3 "${HELPER}" namespace --manifest "${SCALE_MANIFEST}")"

fail() {
    echo "[scaleup][ERROR] $*" >&2
    exit 1
}

pressure_value() {
    local snapshot="$1"
    local key="$2"
    awk -v k="${key}" '{for (i=1;i<=NF;i++){split($i,kv,"="); if (kv[1]==k){print kv[2]; exit}}}' <<< "${snapshot}"
}

cluster_pressure_snapshot() {
    kubectl --kubeconfig "${KUBECONFIG}" -n "${namespace}" get pods --no-headers 2>/dev/null | awk '
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

node_readiness_snapshot() {
    kubectl --kubeconfig "${KUBECONFIG}" get nodes --no-headers 2>/dev/null | awk 'BEGIN {ready=0; total=0} {total++; if ($2=="Ready") ready++} END {printf("ready=%d total=%d\n", ready, total)}'
}

wait_for_deploy_pressure_budget() {
    local stage_label="$1"
    local elapsed=0
    while true; do
        local snapshot node_snapshot pending creating running_notready failed notready ready_nodes total_nodes
        snapshot="$(cluster_pressure_snapshot)"
        node_snapshot="$(node_readiness_snapshot)"
        pending="$(pressure_value "${snapshot}" "pending")"
        creating="$(pressure_value "${snapshot}" "creating")"
        running_notready="$(pressure_value "${snapshot}" "running_notready")"
        failed="$(pressure_value "${snapshot}" "failed")"
        ready_nodes="$(pressure_value "${node_snapshot}" "ready")"
        total_nodes="$(pressure_value "${node_snapshot}" "total")"
        notready=$((pending + creating + running_notready))
        echo "[pressure:${stage_label}] ${snapshot} nodes=${ready_nodes}/${total_nodes}"
        if [ "${failed}" -gt "${DEPLOY_MAX_FAILED_PODS}" ]; then
            echo "[pressure:${stage_label}] too many failed pods: ${failed}" >&2
            return 1
        fi
        if [ "${ready_nodes}" -eq "${total_nodes}" ] &&
           [ "${pending}" -le "${DEPLOY_MAX_PENDING_PODS}" ] &&
           [ "${creating}" -le "${DEPLOY_MAX_CREATING_PODS}" ] &&
           [ "${notready}" -le "${DEPLOY_MAX_NOTREADY_PODS}" ]; then
            return 0
        fi
        if [ "${elapsed}" -ge "${DEPLOY_STABILIZE_TIMEOUT_SECONDS}" ]; then
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

split_manifest() {
    local manifest="$1"
    local split_dir="${DEPLOY_BATCH_DIR}/raw_docs"
    rm -rf "${DEPLOY_BATCH_DIR}"
    mkdir -p "${split_dir}" \
        "${DEPLOY_BATCH_DIR}/00-crds" \
        "${DEPLOY_BATCH_DIR}/01-foundation" \
        "${DEPLOY_BATCH_DIR}/02-services" \
        "${DEPLOY_BATCH_DIR}/03-controllers" \
        "${DEPLOY_BATCH_DIR}/03-controllers-by-node" \
        "${DEPLOY_BATCH_DIR}/03-controllers-unpinned"

    while IFS=$'\t' read -r node_name node_ip node_role; do
        [ -n "${node_name}" ] || continue
        mkdir -p "${DEPLOY_BATCH_DIR}/03-controllers-by-node/${node_name}"
    done < <(python3 "${HELPER}" inventory-nodes --inventory "${INVENTORY}" --node-role-filter "${NODE_ROLE_FILTER}")

    awk '
        BEGIN {n=0; out=""}
        /^---[[:space:]]*$/ { if (out != "") close(out); n++; out=sprintf("'"${split_dir}"'/doc_%05d.yaml", n); next }
        { if (out == "") { n++; out=sprintf("'"${split_dir}"'/doc_%05d.yaml", n) } print >> out }
        END { if (out != "") close(out) }
    ' "${manifest}"

    shopt -s nullglob
    for f in "${split_dir}"/*.yaml; do
        [ -s "${f}" ] || continue
        mapfile -t meta < <(python3 "${HELPER}" doc-meta "${f}" 2>/dev/null || true)
        kind="${meta[0]:-}"
        node_selector="${meta[1]:-}"
        case "${kind}" in
            CustomResourceDefinition) cp "${f}" "${DEPLOY_BATCH_DIR}/00-crds/" ;;
            Namespace|ServiceAccount|ConfigMap|Secret|Role|RoleBinding|ClusterRole|ClusterRoleBinding|NetworkAttachmentDefinition|NetworkAttachmentDefinitionList) cp "${f}" "${DEPLOY_BATCH_DIR}/01-foundation/" ;;
            Service|Endpoints) cp "${f}" "${DEPLOY_BATCH_DIR}/02-services/" ;;
            Deployment|StatefulSet|DaemonSet|Job|Pod)
                cp "${f}" "${DEPLOY_BATCH_DIR}/03-controllers/"
                if [ -n "${node_selector}" ] && [ -d "${DEPLOY_BATCH_DIR}/03-controllers-by-node/${node_selector}" ]; then
                    cp "${f}" "${DEPLOY_BATCH_DIR}/03-controllers-by-node/${node_selector}/"
                else
                    cp "${f}" "${DEPLOY_BATCH_DIR}/03-controllers-unpinned/"
                fi
                ;;
            *) cp "${f}" "${DEPLOY_BATCH_DIR}/01-foundation/" ;;
        esac
    done
    shopt -u nullglob
}

kapply() {
    local file="$1"
    kubectl --kubeconfig "${KUBECONFIG}" apply -f "${file}"
}

apply_dir_serial() {
    local dir="$1"
    local label="$2"
    shopt -s nullglob
    local files=( "${dir}"/*.yaml )
    shopt -u nullglob
    [ "${#files[@]}" -gt 0 ] || return 0
    for f in "${files[@]}"; do
        echo "[${label}] $(basename "${f}")"
        kapply "${f}"
    done
}

controller_submission_order() {
    local buckets_dir="${DEPLOY_BATCH_DIR}/03-controllers-by-node"
    local unpinned_dir="${DEPLOY_BATCH_DIR}/03-controllers-unpinned"
    mapfile -t nodes < <(python3 "${HELPER}" inventory-nodes --inventory "${INVENTORY}" --node-role-filter "${NODE_ROLE_FILTER}" | awk -F '\t' '{print $1}')
    python3 - "${buckets_dir}" "${unpinned_dir}" "${nodes[@]}" <<'PY'
import os, sys
buckets_dir = sys.argv[1]
unpinned_dir = sys.argv[2]
nodes = sys.argv[3:]
queues = {}
for node in nodes:
    node_dir = os.path.join(buckets_dir, node)
    queues[node] = sorted(os.path.join(node_dir, name) for name in os.listdir(node_dir) if name.endswith(".yaml")) if os.path.isdir(node_dir) else []
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
    ordered.extend(sorted(os.path.join(unpinned_dir, name) for name in os.listdir(unpinned_dir) if name.endswith(".yaml")))
for path in ordered:
    print(path)
PY
}

apply_controllers_round_robin() {
    mapfile -t files < <(controller_submission_order)
    local total="${#files[@]}"
    [ "${total}" -gt 0 ] || { echo "[controllers] no resources"; return 0; }
    echo "[controllers] total resources: ${total}"
    local i=0 batch_no=0
    while [ "${i}" -lt "${total}" ]; do
        batch_no=$((batch_no + 1))
        local current_batch_size end
        current_batch_size="$(batch_size_for_iteration "${batch_no}")"
        end=$((i + current_batch_size))
        [ "${end}" -le "${total}" ] || end="${total}"
        wait_for_deploy_pressure_budget "pre-batch-${batch_no}"
        echo "[controllers] batch ${batch_no}: items $((i + 1))..${end}/${total} size=${current_batch_size}"
        for ((j=i; j<end; j++)); do
            echo "[controllers] apply $(basename "${files[j]}")"
            kapply "${files[j]}"
        done
        kubectl --kubeconfig "${KUBECONFIG}" -n "${namespace}" get pods -o wide 2>/dev/null | tail -n 30 || true
        wait_for_deploy_pressure_budget "post-batch-${batch_no}"
        if [ "${end}" -lt "${total}" ]; then
            sleep "${DEPLOY_BATCH_SLEEP_SECONDS}"
        fi
        i="${end}"
    done
}

echo "=== native-k8s largeScale deploy ==="
echo "namespace=${namespace}"
echo "scale_manifest=${SCALE_MANIFEST}"
echo "kustomization=${KUSTOMIZATION}"
echo "rendered_manifest=${RENDERED_MANIFEST}"
echo "batch_size=${DEPLOY_BATCH_SIZE}"

if kubectl --kubeconfig "${KUBECONFIG}" get namespace "${namespace}" >/dev/null 2>&1; then
    fail "namespace ${namespace} already exists; run make scaleclean first"
fi

kubectl --kubeconfig "${KUBECONFIG}" kustomize "${OUTPUT_DIR}" > "${RENDERED_MANIFEST}"
split_manifest "${RENDERED_MANIFEST}"

echo "[deploy] applying CRDs"
apply_dir_serial "${DEPLOY_BATCH_DIR}/00-crds" "crds"
echo "[deploy] applying foundation"
apply_dir_serial "${DEPLOY_BATCH_DIR}/01-foundation" "foundation"
echo "[deploy] applying services"
apply_dir_serial "${DEPLOY_BATCH_DIR}/02-services" "services"
echo "[deploy] applying controllers in node-balanced batches"
apply_controllers_round_robin

echo "LargeScale deploy submitted successfully"
