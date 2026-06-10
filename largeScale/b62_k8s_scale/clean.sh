#!/usr/bin/env bash
# Clean the active B62 experiment namespace and related Kube-OVN resources.
#
# Inputs: optional experiment run directory; assignment.yaml is resolved by
#         lib.sh to derive KUBECONFIG and SEED_NAMESPACE.
# Outputs: clean.log and loadAverage.log in the run directory.
# Side effects: deletes resources only in SEED_NAMESPACE, deletes cluster-scoped
#               Kube-OVN IP/Subnet/Vpc objects whose names contain the namespace,
#               and clears finalizers for those matching Kube-OVN objects if
#               they remain stuck after the grace period.
# Context: run from b62_k8s_scale on the K3s/KVM controller host.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

CLEAN_NAMESPACE=true
CLEAN_CHECK_INTERVAL_SECONDS=15
CLEAN_TIMEOUT_SECONDS=36000
CLEAN_DELETE_REQUEST_TIMEOUT_SECONDS=120
CLEAN_BATCH_SIZE=100
CLEAN_EVENT_DELETE_PARALLELISM=16
CLEAN_KUBE_OVN_DELETE_PARALLELISM=12
CLEAN_KUBE_OVN_FINALIZER_PARALLELISM=16
CLEAN_FORCE_NAMESPACED_AFTER_SECONDS=60
CLEAN_RETRY_DELETE_INTERVAL_SECONDS=60
CLEAN_KUBE_OVN_FINALIZER_AFTER_SECONDS=120
CLEAN_NAMESPACE_FINALIZER_AFTER_SECONDS=300

setup_experiment_context "${1:-}"
begin_stage_logging "clean"
ensure_kubeconfig

NS="${SEED_NAMESPACE}"
WORK_DIR="${EXPERIMENT_DIR}/.cleanup-${NS}"
mkdir -p "${WORK_DIR}"

cleanup_work_dir() {
    rm -rf "${WORK_DIR}"
}
trap cleanup_work_dir EXIT

k() {
    kubectl --kubeconfig "${KUBECONFIG}" "$@"
}

count_lines() {
    local file="$1"
    if [ -f "${file}" ]; then
        wc -l < "${file}" | tr -d ' '
    else
        printf '0\n'
    fi
}

namespace_exists() {
    k get namespace "${NS}" --request-timeout="${CLEAN_DELETE_REQUEST_TIMEOUT_SECONDS}s" >/dev/null 2>&1
}

count_namespaced_kind() {
    local resource="$1"
    if ! namespace_exists; then
        printf '0\n'
        return 0
    fi
    k -n "${NS}" get "${resource}" \
        --ignore-not-found \
        --request-timeout="${CLEAN_DELETE_REQUEST_TIMEOUT_SECONDS}s" \
        --no-headers 2>/dev/null | wc -l | tr -d ' '
}

residual_resource_summary() {
    local pods deploys rsets stss dss jobs cronjobs nads events events_v1 cms secrets sas svcs roles rolebindings total
    pods="$(count_namespaced_kind pods)"
    deploys="$(count_namespaced_kind deployments.apps)"
    rsets="$(count_namespaced_kind replicasets.apps)"
    stss="$(count_namespaced_kind statefulsets.apps)"
    dss="$(count_namespaced_kind daemonsets.apps)"
    jobs="$(count_namespaced_kind jobs.batch)"
    cronjobs="$(count_namespaced_kind cronjobs.batch)"
    nads="$(count_namespaced_kind network-attachment-definitions.k8s.cni.cncf.io)"
    events="$(count_namespaced_kind events)"
    events_v1="$(count_namespaced_kind events.events.k8s.io)"
    cms="$(count_namespaced_kind configmaps)"
    secrets="$(count_namespaced_kind secrets)"
    sas="$(count_namespaced_kind serviceaccounts)"
    svcs="$(count_namespaced_kind services)"
    roles="$(count_namespaced_kind roles.rbac.authorization.k8s.io)"
    rolebindings="$(count_namespaced_kind rolebindings.rbac.authorization.k8s.io)"
    total=$((pods + deploys + rsets + stss + dss + jobs + cronjobs + nads + events + events_v1 + cms + secrets + sas + svcs + roles + rolebindings))
    printf 'total=%s pods=%s deploy=%s rs=%s sts=%s ds=%s jobs=%s cronjobs=%s nads=%s events=%s events_v1=%s cm=%s secrets=%s sa=%s svc=%s roles=%s rolebindings=%s\n' \
        "${total}" "${pods}" "${deploys}" "${rsets}" "${stss}" "${dss}" "${jobs}" "${cronjobs}" \
        "${nads}" "${events}" "${events_v1}" "${cms}" "${secrets}" "${sas}" "${svcs}" "${roles}" "${rolebindings}"
}

summary_value() {
    local summary="$1"
    local key="$2"
    awk -v k="${key}" '{for (i=1;i<=NF;i++){split($i,kv,"="); if (kv[1]==k){print kv[2]; exit}}}' <<< "${summary}"
}

list_matching_cluster_resource() {
    local resource="$1"
    local output="$2"
    k get "${resource}" -o name --request-timeout="${CLEAN_DELETE_REQUEST_TIMEOUT_SECONDS}s" 2>/dev/null \
        | grep -F -- "${NS}" > "${output}" || true
}

count_matching_cluster_resource() {
    local resource="$1"
    local file="${WORK_DIR}/count-${resource//[^A-Za-z0-9]/_}.txt"
    list_matching_cluster_resource "${resource}" "${file}"
    count_lines "${file}"
}

cluster_scoped_summary() {
    local ips subnets vpcs total
    ips="$(count_matching_cluster_resource ip.kubeovn.io)"
    subnets="$(count_matching_cluster_resource subnet.kubeovn.io)"
    vpcs="$(count_matching_cluster_resource vpc.kubeovn.io)"
    total=$((ips + subnets + vpcs))
    printf 'cluster_total=%s kubeovn_ips=%s kubeovn_subnets=%s kubeovn_vpcs=%s\n' "${total}" "${ips}" "${subnets}" "${vpcs}"
}

delete_names_from_file() {
    local file="$1"
    local namespace="$2"
    local parallelism="$3"
    local batch_size="$4"
    local count
    count="$(count_lines "${file}")"
    [ "${count}" -gt 0 ] || return 0
    if [ -n "${namespace}" ]; then
        xargs -r -a "${file}" -n "${batch_size}" -P "${parallelism}" sh -c '
            kubeconfig="$1"; namespace="$2"; timeout="$3"; shift 3
            kubectl --kubeconfig "${kubeconfig}" -n "${namespace}" delete \
                --ignore-not-found --wait=false --request-timeout="${timeout}" "$@" >/dev/null 2>&1 || true
        ' sh "${KUBECONFIG}" "${namespace}" "${CLEAN_DELETE_REQUEST_TIMEOUT_SECONDS}s"
    else
        xargs -r -a "${file}" -n "${batch_size}" -P "${parallelism}" sh -c '
            kubeconfig="$1"; timeout="$2"; shift 2
            kubectl --kubeconfig "${kubeconfig}" delete \
                --ignore-not-found --wait=false --request-timeout="${timeout}" "$@" >/dev/null 2>&1 || true
        ' sh "${KUBECONFIG}" "${CLEAN_DELETE_REQUEST_TIMEOUT_SECONDS}s"
    fi
}

patch_finalizers_from_file() {
    local file="$1"
    local parallelism="$2"
    local count
    count="$(count_lines "${file}")"
    [ "${count}" -gt 0 ] || return 0
    xargs -r -a "${file}" -n 20 -P "${parallelism}" sh -c '
        kubeconfig="$1"; timeout="$2"; patch="$3"; shift 3
        for name in "$@"; do
            kubectl --kubeconfig "${kubeconfig}" patch "${name}" \
                --type=merge -p "${patch}" --request-timeout="${timeout}" >/dev/null 2>&1 || true
        done
    ' sh "${KUBECONFIG}" "${CLEAN_DELETE_REQUEST_TIMEOUT_SECONDS}s" '{"metadata":{"finalizers":null}}'
}

delete_all_namespaced_resources() {
    local resource file count
    if ! namespace_exists; then
        return 0
    fi
    for resource in \
        deployments.apps replicasets.apps statefulsets.apps daemonsets.apps \
        jobs.batch cronjobs.batch services configmaps secrets serviceaccounts \
        roles.rbac.authorization.k8s.io rolebindings.rbac.authorization.k8s.io \
        network-attachment-definitions.k8s.cni.cncf.io pods
    do
        file="${WORK_DIR}/namespaced-${resource//[^A-Za-z0-9]/_}.txt"
        k -n "${NS}" get "${resource}" -o name \
            --ignore-not-found \
            --request-timeout="${CLEAN_DELETE_REQUEST_TIMEOUT_SECONDS}s" > "${file}" 2>/dev/null || true
        count="$(count_lines "${file}")"
        if [ "${count}" -gt 0 ]; then
            echo "Deleting ${count} ${resource} objects from ${NS}"
            delete_names_from_file "${file}" "${NS}" "${CLEAN_KUBE_OVN_DELETE_PARALLELISM}" "${CLEAN_BATCH_SIZE}"
        fi
    done
}

force_delete_namespaced_resources() {
    local resource file count
    if ! namespace_exists; then
        return 0
    fi
    for resource in pods deployments.apps replicasets.apps statefulsets.apps daemonsets.apps jobs.batch; do
        file="${WORK_DIR}/force-${resource//[^A-Za-z0-9]/_}.txt"
        k -n "${NS}" get "${resource}" -o name \
            --ignore-not-found \
            --request-timeout="${CLEAN_DELETE_REQUEST_TIMEOUT_SECONDS}s" > "${file}" 2>/dev/null || true
        count="$(count_lines "${file}")"
        if [ "${count}" -gt 0 ]; then
            echo "Force deleting ${count} ${resource} objects from ${NS}"
            xargs -r -a "${file}" -n "${CLEAN_BATCH_SIZE}" -P "${CLEAN_KUBE_OVN_DELETE_PARALLELISM}" sh -c '
                kubeconfig="$1"; namespace="$2"; timeout="$3"; shift 3
                kubectl --kubeconfig "${kubeconfig}" -n "${namespace}" delete \
                    --ignore-not-found --force --grace-period=0 --wait=false \
                    --request-timeout="${timeout}" "$@" >/dev/null 2>&1 || true
            ' sh "${KUBECONFIG}" "${NS}" "${CLEAN_DELETE_REQUEST_TIMEOUT_SECONDS}s"
        fi
    done
}

delete_namespaced_events() {
    local resource file count
    if ! namespace_exists; then
        return 0
    fi
    for resource in events.events.k8s.io events; do
        file="${WORK_DIR}/events-${resource//[^A-Za-z0-9]/_}.txt"
        k -n "${NS}" get "${resource}" -o name \
            --ignore-not-found \
            --request-timeout="${CLEAN_DELETE_REQUEST_TIMEOUT_SECONDS}s" > "${file}" 2>/dev/null || true
        count="$(count_lines "${file}")"
        if [ "${count}" -gt 0 ]; then
            echo "Deleting ${count} ${resource} objects from ${NS}"
            delete_names_from_file "${file}" "${NS}" "${CLEAN_EVENT_DELETE_PARALLELISM}" "${CLEAN_BATCH_SIZE}"
        fi
    done
}

delete_cluster_scoped_kube_ovn_resources() {
    local resource file count
    for resource in ip.kubeovn.io subnet.kubeovn.io vpc.kubeovn.io; do
        file="${WORK_DIR}/cluster-${resource//[^A-Za-z0-9]/_}.txt"
        list_matching_cluster_resource "${resource}" "${file}"
        count="$(count_lines "${file}")"
        if [ "${count}" -gt 0 ]; then
            echo "Deleting ${count} matching ${resource} objects"
            delete_names_from_file "${file}" "" "${CLEAN_KUBE_OVN_DELETE_PARALLELISM}" "${CLEAN_BATCH_SIZE}"
        fi
    done
}

clear_cluster_scoped_kube_ovn_finalizers() {
    local resource file count
    for resource in ip.kubeovn.io subnet.kubeovn.io vpc.kubeovn.io; do
        file="${WORK_DIR}/cluster-finalizer-${resource//[^A-Za-z0-9]/_}.txt"
        list_matching_cluster_resource "${resource}" "${file}"
        count="$(count_lines "${file}")"
        if [ "${count}" -gt 0 ]; then
            echo "Clearing finalizers from ${count} matching ${resource} objects"
            patch_finalizers_from_file "${file}" "${CLEAN_KUBE_OVN_FINALIZER_PARALLELISM}"
        fi
    done
}

namespace_ready_for_safe_finalize() {
    local ns_json summary total cluster_summary cluster_total
    ns_json="$(k get namespace "${NS}" -o json --request-timeout="${CLEAN_DELETE_REQUEST_TIMEOUT_SECONDS}s" 2>/dev/null || true)"
    [ -n "${ns_json}" ] || return 1
    summary="$(residual_resource_summary)"
    total="$(summary_value "${summary}" total)"
    cluster_summary="$(cluster_scoped_summary)"
    cluster_total="$(summary_value "${cluster_summary}" cluster_total)"
    [ "${total:-1}" = "0" ] || return 1
    [ "${cluster_total:-1}" = "0" ] || return 1
    printf '%s' "${ns_json}" | python3 -c '
import json
import sys
data = json.load(sys.stdin)
spec_finalizers = data.get("spec", {}).get("finalizers") or []
conditions = {item.get("type"): item.get("status") for item in data.get("status", {}).get("conditions", [])}
safe = (
    spec_finalizers == ["kubernetes"]
    and conditions.get("NamespaceContentRemaining") == "False"
    and conditions.get("NamespaceFinalizersRemaining") == "False"
)
raise SystemExit(0 if safe else 1)
'
}

force_clear_namespace_finalizers() {
    local tmp_json
    tmp_json="$(mktemp)"
    k get namespace "${NS}" -o json --request-timeout="${CLEAN_DELETE_REQUEST_TIMEOUT_SECONDS}s" > "${tmp_json}"
    python3 - "${tmp_json}" <<'PY'
import json
import sys
path = sys.argv[1]
with open(path, "r", encoding="utf-8") as fh:
    data = json.load(fh)
data.setdefault("spec", {})["finalizers"] = []
with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh)
PY
    k replace --raw "/api/v1/namespaces/${NS}/finalize" -f "${tmp_json}" >/dev/null 2>&1 || true
    rm -f "${tmp_json}"
}

echo "Cleaning experiment namespace..."
print_test_context
echo "CLEAN_CHECK_INTERVAL_SECONDS=${CLEAN_CHECK_INTERVAL_SECONDS}"
echo "CLEAN_TIMEOUT_SECONDS=${CLEAN_TIMEOUT_SECONDS}"
echo "CLEAN_EVENT_DELETE_PARALLELISM=${CLEAN_EVENT_DELETE_PARALLELISM}"
echo "CLEAN_KUBE_OVN_DELETE_PARALLELISM=${CLEAN_KUBE_OVN_DELETE_PARALLELISM}"

if [ "${CLEAN_NAMESPACE}" != "true" ]; then
    echo "Skipping cleanup because CLEAN_NAMESPACE=${CLEAN_NAMESPACE}"
    exit 0
fi

if namespace_exists; then
    echo "[1/4] Deleting namespaced workload resources"
    delete_all_namespaced_resources

    echo "[2/4] Deleting namespaced events"
    delete_namespaced_events

    echo "[3/4] Deleting namespace ${NS}"
    k delete namespace "${NS}" --wait=false --request-timeout="${CLEAN_DELETE_REQUEST_TIMEOUT_SECONDS}s" >/dev/null 2>&1 || true
else
    echo "Namespace ${NS} is already absent; checking matching cluster-scoped resources."
fi

echo "[4/4] Deleting matching Kube-OVN cluster-scoped resources"
delete_cluster_scoped_kube_ovn_resources

start_ts="$(date +%s)"
deadline=$((start_ts + CLEAN_TIMEOUT_SECONDS))
last_delete_retry_ts="${start_ts}"
last_event_retry_ts="${start_ts}"
last_cluster_retry_ts="${start_ts}"
kube_ovn_finalizers_cleared=false
namespace_finalizer_cleared=false

while true; do
    now="$(date +%s)"
    elapsed=$((now - start_ts))
    summary="$(residual_resource_summary)"
    total="$(summary_value "${summary}" total)"
    cluster_summary="$(cluster_scoped_summary)"
    cluster_total="$(summary_value "${cluster_summary}" cluster_total)"

    if ! namespace_exists; then
        if [ "${cluster_total:-0}" = "0" ]; then
            echo "[${elapsed}s] Namespace ${NS} and matching Kube-OVN resources fully deleted."
            exit 0
        fi
        echo "[${elapsed}s] Namespace deleted; waiting on ${cluster_summary}"
        if [ $((now - last_cluster_retry_ts)) -ge "${CLEAN_RETRY_DELETE_INTERVAL_SECONDS}" ]; then
            delete_cluster_scoped_kube_ovn_resources
            last_cluster_retry_ts="${now}"
        fi
        if [ "${kube_ovn_finalizers_cleared}" = "false" ] &&
           [ "${elapsed}" -ge "${CLEAN_KUBE_OVN_FINALIZER_AFTER_SECONDS}" ]; then
            clear_cluster_scoped_kube_ovn_finalizers
            kube_ovn_finalizers_cleared=true
        fi
        sleep "${CLEAN_CHECK_INTERVAL_SECONDS}"
        continue
    fi

    echo "[${elapsed}s] ${summary} ${cluster_summary}"

    if [ "${total:-0}" -gt 0 ] &&
       [ "${elapsed}" -ge "${CLEAN_FORCE_NAMESPACED_AFTER_SECONDS}" ] &&
       [ $((now - last_delete_retry_ts)) -ge "${CLEAN_RETRY_DELETE_INTERVAL_SECONDS}" ]; then
        echo "[${elapsed}s] Reissuing forced deletes for remaining namespaced resources."
        force_delete_namespaced_resources
        delete_all_namespaced_resources
        last_delete_retry_ts="${now}"
    fi

    if [ $((now - last_event_retry_ts)) -ge "${CLEAN_RETRY_DELETE_INTERVAL_SECONDS}" ]; then
        delete_namespaced_events
        last_event_retry_ts="${now}"
    fi

    if [ "${cluster_total:-0}" -gt 0 ] &&
       [ $((now - last_cluster_retry_ts)) -ge "${CLEAN_RETRY_DELETE_INTERVAL_SECONDS}" ]; then
        delete_cluster_scoped_kube_ovn_resources
        last_cluster_retry_ts="${now}"
    fi

    if [ "${kube_ovn_finalizers_cleared}" = "false" ] &&
       [ "${elapsed}" -ge "${CLEAN_KUBE_OVN_FINALIZER_AFTER_SECONDS}" ] &&
       [ "${cluster_total:-0}" -gt 0 ]; then
        clear_cluster_scoped_kube_ovn_finalizers
        kube_ovn_finalizers_cleared=true
    fi

    if [ "${namespace_finalizer_cleared}" = "false" ] &&
       [ "${elapsed}" -ge "${CLEAN_NAMESPACE_FINALIZER_AFTER_SECONDS}" ] &&
       namespace_ready_for_safe_finalize; then
        echo "[${elapsed}s] Namespace has no remaining content; clearing stuck namespace finalizer."
        force_clear_namespace_finalizers
        namespace_finalizer_cleared=true
    fi

    if [ "${now}" -ge "${deadline}" ]; then
        echo "[${elapsed}s] ERROR: Timed out waiting for namespace ${NS} deletion."
        k get namespace "${NS}" -o yaml --request-timeout="${CLEAN_DELETE_REQUEST_TIMEOUT_SECONDS}s" || true
        exit 1
    fi

    sleep "${CLEAN_CHECK_INTERVAL_SECONDS}"
done
