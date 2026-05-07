#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

setup_experiment_context "${1:-}"
begin_stage_logging "clean"
source "${SCRIPT_DIR}/config/runtime.sh"
ensure_kubeconfig

NS="${SEED_NAMESPACE}"
CHECK_INTERVAL="${SEED_CLEAN_CHECK_INTERVAL_SECONDS}"
TIMEOUT_SECONDS="${SEED_CLEAN_TIMEOUT_SECONDS}"
FORCE_FINALIZER_CLEANUP="${SEED_CLEAN_FORCE_FINALIZER_CLEANUP}"
AUTO_FINALIZE_STUCK_NAMESPACE="${SEED_CLEAN_AUTO_FINALIZE_STUCK_NAMESPACE}"
AUTO_FINALIZE_AFTER_SECONDS="${SEED_CLEAN_AUTO_FINALIZE_AFTER_SECONDS}"

echo "Cleaning experiment namespace..."
print_test_context
echo "CHECK_INTERVAL=${CHECK_INTERVAL}"
echo "TIMEOUT_SECONDS=${TIMEOUT_SECONDS}"
echo "FORCE_FINALIZER_CLEANUP=${FORCE_FINALIZER_CLEANUP}"

if [ "${CLEAN_NAMESPACE}" != "true" ]; then
    echo "Skipping cleanup because CLEAN_NAMESPACE=${CLEAN_NAMESPACE}"
    exit 0
fi

count_namespaced_kind() {
    local resource="$1"
    kubectl -n "${NS}" get "${resource}" --ignore-not-found --no-headers 2>/dev/null | wc -l | tr -d ' '
}

residual_resource_summary() {
    local pods deploys stss dss jobs nads cms secrets sas svcs roles rolebindings total
    pods="$(count_namespaced_kind pods)"
    deploys="$(count_namespaced_kind deployments.apps)"
    stss="$(count_namespaced_kind statefulsets.apps)"
    dss="$(count_namespaced_kind daemonsets.apps)"
    jobs="$(count_namespaced_kind jobs.batch)"
    nads="$(count_namespaced_kind network-attachment-definitions.k8s.cni.cncf.io)"
    cms="$(count_namespaced_kind configmaps)"
    secrets="$(count_namespaced_kind secrets)"
    sas="$(count_namespaced_kind serviceaccounts)"
    svcs="$(count_namespaced_kind services)"
    roles="$(count_namespaced_kind roles.rbac.authorization.k8s.io)"
    rolebindings="$(count_namespaced_kind rolebindings.rbac.authorization.k8s.io)"
    total=$((pods + deploys + stss + dss + jobs + nads + cms + secrets + sas + svcs + roles + rolebindings))
    printf 'total=%s pods=%s deploy=%s sts=%s ds=%s jobs=%s nads=%s cm=%s secrets=%s sa=%s svc=%s roles=%s rolebindings=%s\n' \
        "${total}" "${pods}" "${deploys}" "${stss}" "${dss}" "${jobs}" "${nads}" "${cms}" "${secrets}" "${sas}" "${svcs}" "${roles}" "${rolebindings}"
}

residual_summary_value() {
    local summary="$1"
    local key="$2"
    awk -v k="${key}" '{for (i=1;i<=NF;i++){split($i,kv,"="); if (kv[1]==k){print kv[2]; exit}}}' <<< "${summary}"
}

namespaced_api_resources() {
    kubectl api-resources --verbs=list --namespaced -o name 2>/dev/null | sort -u
}

namespace_exists() {
    kubectl get namespace "${NS}" >/dev/null 2>&1
}

delete_all_namespaced_resources() {
    local resource
    for resource in deployments.apps statefulsets.apps daemonsets.apps jobs.batch pods services configmaps network-attachment-definitions.k8s.cni.cncf.io; do
        kubectl -n "${NS}" delete "${resource}" --all --ignore-not-found --wait=false >/dev/null 2>&1 || true
    done
    while IFS= read -r resource; do
        [ -n "${resource}" ] || continue
        kubectl -n "${NS}" delete "${resource}" --all --ignore-not-found --wait=false >/dev/null 2>&1 || true
    done < <(namespaced_api_resources)
}

ensure_namespace_exists_if_residuals_present() {
    local summary total
    summary="$(residual_resource_summary)"
    total="$(residual_summary_value "${summary}" total)"
    if namespace_exists; then
        return 0
    fi
    if [ "${total:-0}" -gt 0 ]; then
        echo "Namespace object is absent but residual resources still exist: ${summary}"
        echo "Recreating namespace ${NS} temporarily for cleanup..."
        kubectl create namespace "${NS}" >/dev/null 2>&1 || true
        return 0
    fi
    return 1
}

force_clear_namespace_finalizers() {
    local tmp_json
    tmp_json="$(mktemp)"
    kubectl get namespace "${NS}" -o json > "${tmp_json}"
    python3 - <<'PY' "${tmp_json}"
import json, sys
path = sys.argv[1]
with open(path, "r", encoding="utf-8") as fh:
    data = json.load(fh)
data.setdefault("spec", {})["finalizers"] = []
with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh)
PY
    kubectl replace --raw "/api/v1/namespaces/${NS}/finalize" -f "${tmp_json}" || true
    rm -f "${tmp_json}"
}

namespace_ready_for_safe_finalize() {
    local ns_json summary total tmp_py
    ns_json="$(kubectl get namespace "${NS}" -o json 2>/dev/null || true)"
    [ -n "${ns_json}" ] || return 1

    tmp_py="$(mktemp)"
    cat > "${tmp_py}" <<'PY'
import json
import sys

data = json.load(sys.stdin)
spec_finalizers = data.get("spec", {}).get("finalizers") or []
if spec_finalizers != ["kubernetes"]:
    raise SystemExit(1)
conditions = {item.get("type"): item.get("status") for item in data.get("status", {}).get("conditions", [])}
safe = (
    conditions.get("NamespaceContentRemaining") == "False"
    and conditions.get("NamespaceFinalizersRemaining") == "False"
    and conditions.get("NamespaceDeletionContentFailure") == "True"
)
raise SystemExit(0 if safe else 1)
PY
    if ! printf '%s' "${ns_json}" | python3 "${tmp_py}"; then
        rm -f "${tmp_py}"
        return 1
    fi
    rm -f "${tmp_py}"

    summary="$(residual_resource_summary)"
    total="$(residual_summary_value "${summary}" total)"
    [ "${total:-1}" = "0" ]
}

if ! namespace_exists; then
    if ! ensure_namespace_exists_if_residuals_present; then
        echo "Namespace ${NS} does not exist and no residual namespaced resources were detected."
        exit 0
    fi
fi

echo "[1/3] Deleting namespaced resources"
delete_all_namespaced_resources

echo "[2/3] Deleting namespace"
kubectl delete namespace "${NS}" --wait=false >/dev/null 2>&1 || true

echo "[3/3] Waiting for namespace removal"
start_ts="$(date +%s)"
deadline=$((start_ts + TIMEOUT_SECONDS))
auto_finalize_attempted=false

while true; do
    now="$(date +%s)"
    elapsed=$((now - start_ts))

    if ! namespace_exists; then
        echo "[${elapsed}s] Namespace ${NS} fully deleted."
        exit 0
    fi

    if [ "${AUTO_FINALIZE_STUCK_NAMESPACE}" = "true" ] &&
       [ "${auto_finalize_attempted}" = "false" ] &&
       [ "${elapsed}" -ge "${AUTO_FINALIZE_AFTER_SECONDS}" ] &&
       namespace_ready_for_safe_finalize; then
        echo "[${elapsed}s] Namespace content empty but finalizer is stuck; clearing finalizers."
        force_clear_namespace_finalizers
        auto_finalize_attempted=true
        sleep 5
        continue
    fi

    if [ "${now}" -ge "${deadline}" ]; then
        echo "[${elapsed}s] ERROR: Timed out waiting for namespace ${NS} deletion."
        kubectl get namespace "${NS}" -o yaml || true
        if [ "${FORCE_FINALIZER_CLEANUP}" = "true" ]; then
            echo "Attempting force finalizer cleanup."
            force_clear_namespace_finalizers
        fi
        exit 1
    fi

    echo "[${elapsed}s] $(residual_resource_summary)"
    sleep "${CHECK_INTERVAL}"
done
