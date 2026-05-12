#!/usr/bin/env bash
set -euo pipefail

OUTPUT_DIR="${OUTPUT_DIR:?}"
SCALE_MANIFEST="${SCALE_MANIFEST:-}"
MANIFEST="${MANIFEST:-}"
KUSTOMIZATION="${KUSTOMIZATION:-${OUTPUT_DIR}/kustomization.yaml}"
RENDERED_MANIFEST="${RENDERED_MANIFEST:-${OUTPUT_DIR}/k8s_scale_rendered.yaml}"
DEPLOY_BATCH_DIR="${DEPLOY_BATCH_DIR:-${OUTPUT_DIR}/scale_deploy_batches}"
KUBECONFIG="${KUBECONFIG:?}"
HELPER="${HELPER:?}"
CHECK_INTERVAL="${CHECK_INTERVAL:-5}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-600}"

manifest_for_ns="${SCALE_MANIFEST}"
if [ ! -f "${manifest_for_ns}" ]; then
    manifest_for_ns="${MANIFEST}"
fi
[ -f "${manifest_for_ns}" ] || {
    echo "[scaleclean][ERROR] cannot determine namespace; missing ${SCALE_MANIFEST} and ${MANIFEST}" >&2
    exit 1
}

namespace="$(python3 "${HELPER}" namespace --manifest "${manifest_for_ns}")"

namespace_exists() {
    kubectl --kubeconfig "${KUBECONFIG}" get namespace "${namespace}" >/dev/null 2>&1
}

count_kind() {
    local resource="$1"
    kubectl --kubeconfig "${KUBECONFIG}" -n "${namespace}" get "${resource}" --ignore-not-found --no-headers 2>/dev/null | wc -l | tr -d ' '
}

summary() {
    local pods deploys stss dss jobs nads cms secrets sas svcs total
    pods="$(count_kind pods)"
    deploys="$(count_kind deployments.apps)"
    stss="$(count_kind statefulsets.apps)"
    dss="$(count_kind daemonsets.apps)"
    jobs="$(count_kind jobs.batch)"
    nads="$(count_kind network-attachment-definitions.k8s.cni.cncf.io)"
    cms="$(count_kind configmaps)"
    secrets="$(count_kind secrets)"
    sas="$(count_kind serviceaccounts)"
    svcs="$(count_kind services)"
    total=$((pods + deploys + stss + dss + jobs + nads + cms + secrets + sas + svcs))
    printf 'total=%s pods=%s deploy=%s sts=%s ds=%s jobs=%s nads=%s cm=%s secrets=%s sa=%s svc=%s\n' \
        "${total}" "${pods}" "${deploys}" "${stss}" "${dss}" "${jobs}" "${nads}" "${cms}" "${secrets}" "${sas}" "${svcs}"
}

delete_all_namespaced_resources() {
    local resource
    for resource in deployments.apps statefulsets.apps daemonsets.apps jobs.batch pods services configmaps secrets network-attachment-definitions.k8s.cni.cncf.io; do
        kubectl --kubeconfig "${KUBECONFIG}" -n "${namespace}" delete "${resource}" --all --ignore-not-found --wait=false >/dev/null 2>&1 || true
    done
    while IFS= read -r resource; do
        [ -n "${resource}" ] || continue
        kubectl --kubeconfig "${KUBECONFIG}" -n "${namespace}" delete "${resource}" --all --ignore-not-found --wait=false >/dev/null 2>&1 || true
    done < <(kubectl --kubeconfig "${KUBECONFIG}" api-resources --verbs=list --namespaced -o name 2>/dev/null | sort -u)
}

echo "=== native-k8s largeScale clean ==="
echo "namespace=${namespace}"

if ! namespace_exists; then
    echo "[clean] namespace ${namespace} does not exist"
    rm -rf "${DEPLOY_BATCH_DIR}"
    rm -f "${KUSTOMIZATION}" "${RENDERED_MANIFEST}"
    exit 0
fi

echo "[1/3] deleting namespaced resources"
delete_all_namespaced_resources

echo "[2/3] deleting namespace"
kubectl --kubeconfig "${KUBECONFIG}" delete namespace "${namespace}" --wait=false >/dev/null 2>&1 || true

echo "[3/3] waiting for namespace removal"
start_ts="$(date +%s)"
while true; do
    now="$(date +%s)"
    elapsed=$((now - start_ts))
    if ! namespace_exists; then
        echo "[${elapsed}s] namespace ${namespace} fully deleted"
        rm -rf "${DEPLOY_BATCH_DIR}"
        rm -f "${KUSTOMIZATION}" "${RENDERED_MANIFEST}"
        exit 0
    fi
    if [ "${elapsed}" -ge "${TIMEOUT_SECONDS}" ]; then
        echo "[${elapsed}s] ERROR: timed out waiting for namespace deletion" >&2
        kubectl --kubeconfig "${KUBECONFIG}" get namespace "${namespace}" -o yaml || true
        exit 1
    fi
    echo "[${elapsed}s] $(summary)"
    sleep "${CHECK_INTERVAL}"
done
