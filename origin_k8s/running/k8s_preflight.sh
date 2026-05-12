#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

OUTPUT_DIR="${OUTPUT_DIR:-${SCRIPT_DIR}/../emulate/output}"
KUBECONFIG="${KUBECONFIG:-/home/lxl/k8s/output/kubeconfigs/seedemu-k3s.yaml}"
REGISTRY_PREFIX="${REGISTRY_PREFIX:-192.168.122.110:5000}"
IMAGE_REGISTRY_PREFIX="${IMAGE_REGISTRY_PREFIX:-seedemu}"
SSH_USER="${SSH_USER:-ubuntu}"
SSH_KEY="${SSH_KEY:-/home/lxl/.ssh/id_ed25519}"
HELPER="${HELPER:-${SCRIPT_DIR}/k8s_make_helper.py}"
MANIFEST="${MANIFEST:-${OUTPUT_DIR}/k8s.yaml}"
IMAGES_YAML="${IMAGES_YAML:-${OUTPUT_DIR}/images.yaml}"

ssh_args=(-n -i "${SSH_KEY}" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=8)

fail() {
    echo "[preflight][ERROR] $*" >&2
    exit 1
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || fail "missing command: $1"
}

require_file() {
    [ -f "$1" ] || fail "missing file: $1"
}

registry_ref="${REGISTRY_PREFIX#http://}"
registry_ref="${registry_ref#https://}"
registry_ref="${registry_ref%%/*}"
registry_host="${registry_ref%%:*}"
registry_url="http://${registry_ref}"

echo "=== native-k8s preflight ==="
echo "output_dir=${OUTPUT_DIR}"
echo "manifest=${MANIFEST}"
echo "images_yaml=${IMAGES_YAML}"
echo "kubeconfig=${KUBECONFIG}"
echo "registry_prefix=${REGISTRY_PREFIX}"
echo "image_registry_prefix=${IMAGE_REGISTRY_PREFIX}"

echo "[1/7] Local tools and compile output"
require_cmd python3
require_cmd kubectl
require_cmd curl
require_cmd ssh
require_file "${HELPER}"
require_file "${MANIFEST}"
require_file "${IMAGES_YAML}"
require_file "${KUBECONFIG}"
[ -d "${OUTPUT_DIR}" ] || fail "missing output directory: ${OUTPUT_DIR}"
[ -s "${MANIFEST}" ] || fail "empty manifest: ${MANIFEST}"
[ -s "${IMAGES_YAML}" ] || fail "empty images yaml: ${IMAGES_YAML}"

namespace="$(python3 "${HELPER}" namespace --manifest "${MANIFEST}")"
[ -n "${namespace}" ] || fail "cannot determine namespace from ${MANIFEST}"
image_count="$(python3 "${HELPER}" mapped-images --images-yaml "${IMAGES_YAML}" --image-registry-prefix "${IMAGE_REGISTRY_PREFIX}" --registry-prefix "${REGISTRY_PREFIX}" | wc -l)"
[ "${image_count}" -gt 0 ] || fail "no images found in ${IMAGES_YAML}"
echo "namespace=${namespace}"
echo "image_count=${image_count}"

echo "[2/7] Kubeconfig and API server"
kubectl --kubeconfig "${KUBECONFIG}" version --client=true >/dev/null
kubectl --kubeconfig "${KUBECONFIG}" get nodes -o wide

echo "[3/7] Node readiness"
not_ready="$(
    kubectl --kubeconfig "${KUBECONFIG}" get nodes \
        -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{range .status.conditions[?(@.type=="Ready")]}{.status}{end}{"\n"}{end}' \
    | awk '$2 != "True" {print $1}'
)"
[ -z "${not_ready}" ] || fail "not-ready nodes: ${not_ready//$'\n'/ }"

echo "[4/7] kube-system baseline"
kubectl --kubeconfig "${KUBECONFIG}" -n kube-system get pods -o wide
bad_system_pods="$(
    kubectl --kubeconfig "${KUBECONFIG}" -n kube-system get pods --no-headers 2>/dev/null \
    | awk '$3 !~ /^(Running|Completed)$/ {print $1 ":" $3}'
)"
[ -z "${bad_system_pods}" ] || fail "kube-system has unhealthy pods: ${bad_system_pods//$'\n'/ }"

echo "[5/7] Namespace baseline"
if kubectl --kubeconfig "${KUBECONFIG}" get namespace "${namespace}" >/dev/null 2>&1; then
    fail "namespace ${namespace} already exists; run make clean or wait for previous cleanup before make up"
fi
echo "namespace ${namespace} is absent"

echo "[6/7] Registry health"
code="$(curl -s -o /dev/null -w '%{http_code}' "${registry_url}/v2/" || true)"
case "${code}" in
    200|401) echo "local registry_http=${code}" ;;
    *) fail "registry ${registry_url}/v2/ is not healthy from local host, http=${code}" ;;
esac

require_file "${SSH_KEY}"
ssh_target="${SSH_USER}@${registry_host}"
ssh "${ssh_args[@]}" "${ssh_target}" "curl -s -o /dev/null -w '%{http_code}' '${registry_url}/v2/'" \
    | awk '{print "registry-host registry_http="$0; if ($0 != "200" && $0 != "401") exit 1}' \
    || fail "registry ${registry_url}/v2/ is not healthy from ${ssh_target}"

echo "[7/7] Remote build prerequisites and node registry reachability"
ssh "${ssh_args[@]}" "${ssh_target}" "sudo -n docker version >/dev/null && sudo -n docker buildx version >/dev/null" \
    || fail "docker/buildx is not ready on ${ssh_target}"
echo "registry-host docker/buildx ok"

while IFS=$'\t' read -r node_name node_ip; do
    [ -n "${node_name}" ] || continue
    [ -n "${node_ip}" ] || fail "cannot determine InternalIP for node ${node_name}"
    node_target="${SSH_USER}@${node_ip}"
    code="$(ssh "${ssh_args[@]}" "${node_target}" "curl -s -o /dev/null -w '%{http_code}' '${registry_url}/v2/' || true")"
    printf '%s\t%s\tregistry_http=%s\n' "${node_name}" "${node_ip}" "${code}"
    case "${code}" in
        200|401) ;;
        *) fail "registry ${registry_url}/v2/ is not reachable from ${node_name} (${node_ip}), http=${code}" ;;
    esac
done < <(
    kubectl --kubeconfig "${KUBECONFIG}" get nodes \
        -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.addresses[?(@.type=="InternalIP")].address}{"\n"}{end}'
)

echo "Preflight completed"
