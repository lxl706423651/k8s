#!/usr/bin/env bash
set -euo pipefail

OUTPUT_DIR="${OUTPUT_DIR:?}"
MANIFEST="${MANIFEST:?}"
SCALE_MANIFEST="${SCALE_MANIFEST:?}"
IMAGES_YAML="${IMAGES_YAML:?}"
KUSTOMIZATION="${KUSTOMIZATION:?}"
NODE_IMAGE_DIR="${NODE_IMAGE_DIR:?}"
INVENTORY="${INVENTORY:?}"
NODE_ROLE_FILTER="${NODE_ROLE_FILTER:-all}"
KUBECONFIG="${KUBECONFIG:?}"
REGISTRY_PREFIX="${REGISTRY_PREFIX:?}"
IMAGE_REGISTRY_PREFIX="${IMAGE_REGISTRY_PREFIX:-seedemu}"
SSH_USER="${SSH_USER:-ubuntu}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
HELPER="${HELPER:?}"
OPTIMIZER="${OPTIMIZER:?}"

ssh_args=(-n -i "${SSH_KEY}" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o BatchMode=yes -o ConnectTimeout=8)

fail() {
    echo "[scalepreflight][ERROR] $*" >&2
    exit 1
}

require_file() {
    [ -f "$1" ] || fail "missing file: $1"
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || fail "missing command: $1"
}

registry_ref="${REGISTRY_PREFIX#http://}"
registry_ref="${registry_ref#https://}"
registry_ref="${registry_ref%%/*}"
registry_host="${registry_ref%%:*}"
registry_url="http://${registry_ref}"

echo "=== native-k8s largeScale preflight ==="
echo "output_dir=${OUTPUT_DIR}"
echo "manifest=${MANIFEST}"
echo "scale_manifest=${SCALE_MANIFEST}"
echo "images_yaml=${IMAGES_YAML}"
echo "inventory=${INVENTORY}"
echo "kubeconfig=${KUBECONFIG}"
echo "registry_prefix=${REGISTRY_PREFIX}"
echo "node_role_filter=${NODE_ROLE_FILTER}"

echo "[1/8] Local tools and input files"
require_cmd python3
require_cmd kubectl
require_cmd curl
require_cmd ssh
require_file "${MANIFEST}"
require_file "${IMAGES_YAML}"
require_file "${INVENTORY}"
require_file "${KUBECONFIG}"
require_file "${HELPER}"
require_file "${OPTIMIZER}"
require_file "${SSH_KEY}"

echo "[2/8] Optimizing manifest with hard same-AS placement"
python3 "${OPTIMIZER}" "${OUTPUT_DIR}" \
    --src "${MANIFEST}" \
    --dst "${SCALE_MANIFEST}" \
    --mode hard \
    --inventory "${INVENTORY}" \
    --node-role-filter "${NODE_ROLE_FILTER}" \
    --plan "${OUTPUT_DIR}/scale_placement_plan.json"
require_file "${SCALE_MANIFEST}"

namespace="$(python3 "${HELPER}" namespace --manifest "${SCALE_MANIFEST}")"
echo "namespace=${namespace}"

echo "[3/8] Rendering kustomization for scale manifest"
python3 "${HELPER}" kustomization \
    --images-yaml "${IMAGES_YAML}" \
    --image-registry-prefix "${IMAGE_REGISTRY_PREFIX}" \
    --registry-prefix "${REGISTRY_PREFIX}" \
    --resource "${SCALE_MANIFEST}" \
    --output "${KUSTOMIZATION}"
require_file "${KUSTOMIZATION}"

echo "[4/8] Generating per-node image refs"
rm -rf "${NODE_IMAGE_DIR}"
python3 "${HELPER}" node-image-refs \
    --manifest "${SCALE_MANIFEST}" \
    --images-yaml "${IMAGES_YAML}" \
    --inventory "${INVENTORY}" \
    --node-role-filter "${NODE_ROLE_FILTER}" \
    --image-registry-prefix "${IMAGE_REGISTRY_PREFIX}" \
    --registry-prefix "${REGISTRY_PREFIX}" \
    --out-dir "${NODE_IMAGE_DIR}" \
    --require-complete

echo "[5/8] Kube API and node readiness"
kubectl --kubeconfig "${KUBECONFIG}" get nodes -o wide
not_ready="$(
    kubectl --kubeconfig "${KUBECONFIG}" get nodes \
        -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{range .status.conditions[?(@.type=="Ready")]}{.status}{end}{"\n"}{end}' \
    | awk '$2 != "True" {print $1}'
)"
[ -z "${not_ready}" ] || fail "not-ready nodes: ${not_ready//$'\n'/ }"

echo "[6/8] kube-system baseline"
kubectl --kubeconfig "${KUBECONFIG}" -n kube-system get pods -o wide
bad_system_pods="$(
    kubectl --kubeconfig "${KUBECONFIG}" -n kube-system get pods --no-headers 2>/dev/null \
    | awk '$3 !~ /^(Running|Completed)$/ {print $1 ":" $3}'
)"
[ -z "${bad_system_pods}" ] || fail "kube-system has unhealthy pods: ${bad_system_pods//$'\n'/ }"

echo "[7/8] Namespace baseline"
if kubectl --kubeconfig "${KUBECONFIG}" get namespace "${namespace}" >/dev/null 2>&1; then
    fail "namespace ${namespace} already exists; run make scaleclean first"
fi
echo "namespace ${namespace} is absent"

echo "[8/8] Registry, remote buildx, and node registry reachability"
code="$(curl -s -o /dev/null -w '%{http_code}' "${registry_url}/v2/" || true)"
case "${code}" in
    200|401) echo "local registry_http=${code}" ;;
    *) fail "registry ${registry_url}/v2/ is not healthy from local host, http=${code}" ;;
esac

ssh_target="${SSH_USER}@${registry_host}"
ssh "${ssh_args[@]}" "${ssh_target}" "curl -s -o /dev/null -w '%{http_code}' '${registry_url}/v2/'" \
    | awk '{print "registry-host registry_http="$0; if ($0 != "200" && $0 != "401") exit 1}' \
    || fail "registry ${registry_url}/v2/ is not healthy from ${ssh_target}"
ssh "${ssh_args[@]}" "${ssh_target}" "sudo -n docker version >/dev/null && sudo -n docker buildx version >/dev/null" \
    || fail "docker/buildx is not ready on ${ssh_target}"
echo "registry-host docker/buildx ok"

while IFS=$'\t' read -r node_name node_ip node_role; do
    [ -n "${node_name}" ] || continue
    kubectl --kubeconfig "${KUBECONFIG}" get node "${node_name}" >/dev/null 2>&1 \
        || fail "inventory node ${node_name} is not present in the kubeconfig cluster"
    code="$(ssh "${ssh_args[@]}" "${SSH_USER}@${node_ip}" "curl -s -o /dev/null -w '%{http_code}' '${registry_url}/v2/' || true")"
    printf '%s\t%s\t%s\tregistry_http=%s\n' "${node_name}" "${node_ip}" "${node_role}" "${code}"
    case "${code}" in
        200|401) ;;
        *) fail "registry ${registry_url}/v2/ is not reachable from ${node_name} (${node_ip}), http=${code}" ;;
    esac
done < <(python3 "${HELPER}" inventory-nodes --inventory "${INVENTORY}" --node-role-filter "${NODE_ROLE_FILTER}")

echo "LargeScale preflight completed"
