#!/usr/bin/env bash
# Build a compiled SeedEMU/K8s experiment on the registry/master node, then
# preload the resulting images onto the target K3s nodes.
#
# Inputs: an experiment directory containing output/k8s.yaml,
# output/build_images.sh, plus explicit cluster/build parameters.
# Outputs: build.log, build_remote.log, image_refs.txt, node_image_refs/, and
# preload_<node>.log files in the experiment directory.
# Side effects: uploads compile artifacts to the master, builds Docker images on
# the master, and pulls those images into remote K3s containerd stores.
# Context: run from the controller host after placement.sh has produced hard
# nodeSelector placement; Kube-OVN runs use output/k8s.kube-ovn.yaml.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

STAGE_ARGS=("$@")
setup_experiment_context "$@"
load_build_config
seed_load_cluster_nodes
ensure_kubeconfig
begin_stage_logging "build"

OUTPUT_DIR="${EXPERIMENT_DIR}/output"
REMOTE_WORK_DIR="/tmp/seedemu-build"
REGISTRY_ENSURE_SCRIPT="${TEST_DIR}/ensure_master_registry.sh"
NODE_IMAGE_DIR="${EXPERIMENT_DIR}/node_image_refs"
IMAGE_REFS_FILE="${EXPERIMENT_DIR}/image_refs.txt"
K8S_MANIFEST=""
REMOTE_BUILD_DISTRIBUTION_MODE="registry"
NODE_IMAGE_VALIDATE_DIR="${EXPERIMENT_DIR}/.node_image_refs_validate"

SSH_OPTS=(
  -i "${SEED_K3S_SSH_KEY}"
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
  -o LogLevel=ERROR
  -o BatchMode=yes
  -o ConnectTimeout=10
)

echo "EXPERIMENT_DIR=${EXPERIMENT_DIR}"
echo "SEED_TOPOLOGY_SIZE=${SEED_TOPOLOGY_SIZE}"
echo "SEED_CLUSTER_INVENTORY_PATH=${SEED_CLUSTER_INVENTORY_PATH}"
echo "KUBECONFIG=${KUBECONFIG}"
echo "SEED_NAMESPACE=${SEED_NAMESPACE}"
echo "SEED_BUILD_PARALLELISM=${SEED_BUILD_PARALLELISM}"
echo "SEED_DOCKER_BUILDKIT=${SEED_DOCKER_BUILDKIT}"
echo "SEED_DOCKER_BUILDX=${SEED_DOCKER_BUILDX}"
echo "SEED_BUILD_SKIP_EXISTING=${SEED_BUILD_SKIP_EXISTING}"
echo "SEED_PRELOAD_NODE_CONCURRENCY=${SEED_PRELOAD_NODE_CONCURRENCY}"
echo "SEED_PRELOAD_IMAGE_CONCURRENCY=${SEED_PRELOAD_IMAGE_CONCURRENCY}"
echo "SEED_NETWORK_BACKEND=${SEED_NETWORK_BACKEND}"
echo "SEED_ATTACHED_CNI_TYPE=${SEED_ATTACHED_CNI_TYPE}"
echo "Workflow distribution mode: preload"
echo "Remote build publish mode: ${REMOTE_BUILD_DISTRIBUTION_MODE}"
seed_print_cluster_nodes

[ -d "${OUTPUT_DIR}" ] || {
    echo "Error: missing output directory ${OUTPUT_DIR}. Run test/compile.sh first." >&2
    exit 1
}
require_file "${OUTPUT_DIR}/build_images.sh"
K8S_MANIFEST="$(render_runtime_manifest)"
require_file "${K8S_MANIFEST}"
echo "Runtime manifest: ${K8S_MANIFEST}"

rm -rf "${NODE_IMAGE_DIR}" "${NODE_IMAGE_VALIDATE_DIR}"
python3 "${TEST_DIR}/generate_node_image_refs.py" \
    "${K8S_MANIFEST}" \
    "${NODE_IMAGE_VALIDATE_DIR}" \
    "${SEED_NODE_NAMES[@]}"

VALIDATION_SUMMARY="${NODE_IMAGE_VALIDATE_DIR}/summary.json"
MISSING_SELECTOR_COUNT="$(
    python3 - "${VALIDATION_SUMMARY}" <<'PY'
import json
import sys
with open(sys.argv[1], "r", encoding="utf-8") as fh:
    data = json.load(fh)
print(data.get("missing_selector_count", 0))
PY
)"

if [ "${MISSING_SELECTOR_COUNT}" != "0" ]; then
    echo "Error: ${K8S_MANIFEST} is missing hard node placement for ${MISSING_SELECTOR_COUNT} workloads." >&2
    echo "This build flow requires nodeSelector-based placement so per-node preload can be generated." >&2
        echo "Run ./compile.sh and ./placement.sh again for this experiment directory before running ./build.sh." >&2
    echo "Validation details: ${VALIDATION_SUMMARY}" >&2
    exit 1
fi

echo "Checking registry availability on master..."
if ! ssh "${SSH_OPTS[@]}" "${SEED_K3S_USER}@${SEED_MASTER_NODE_IP}" \
    "curl -m 5 -fsS http://${SEED_REGISTRY_HOST}:${SEED_REGISTRY_PORT}/v2/ >/dev/null" 2>&1; then
    echo "Registry is not reachable at http://${SEED_REGISTRY_HOST}:${SEED_REGISTRY_PORT}/v2/"
    echo "Trying to repair registry using ${REGISTRY_ENSURE_SCRIPT} ..."
    "${REGISTRY_ENSURE_SCRIPT}" "${STAGE_ARGS[@]}"
    if ! ssh "${SSH_OPTS[@]}" "${SEED_K3S_USER}@${SEED_MASTER_NODE_IP}" \
        "curl -m 5 -fsS http://${SEED_REGISTRY_HOST}:${SEED_REGISTRY_PORT}/v2/ >/dev/null" 2>&1; then
        echo "Registry is still not reachable after repair." >&2
        exit 1
    fi
fi

tar -C "${OUTPUT_DIR}" -czf /tmp/compiled.tar.gz .

echo "Uploading to master..."
ssh "${SSH_OPTS[@]}" "${SEED_K3S_USER}@${SEED_MASTER_NODE_IP}" \
    "sudo -n rm -rf '${REMOTE_WORK_DIR}' && mkdir -p '${REMOTE_WORK_DIR}' && sudo -n chown '${SEED_K3S_USER}:${SEED_K3S_USER}' '${REMOTE_WORK_DIR}'"
scp -q "${SSH_OPTS[@]}" /tmp/compiled.tar.gz "${SEED_K3S_USER}@${SEED_MASTER_NODE_IP}:${REMOTE_WORK_DIR}/"

REMOTE_SCRIPT="${REMOTE_WORK_DIR}/build_remote.sh"
cat > /tmp/build_remote.sh <<'REMOTE_EOF'
#!/bin/bash
set -euo pipefail
REMOTE_WORK_DIR="$1"
BUILD_PARALLEL_JOBS="$2"
BUILD_BATCH_SIZE="$3"
DOCKER_BUILDKIT="$4"
COMPOSE_DOCKER_CLI_BUILD="$5"
SEED_IMAGE_DISTRIBUTION_MODE="$6"
SEED_REGISTRY="$7"
SEED_REGISTRY_LOCAL_ENDPOINT="$8"
SEED_REGISTRY_PUSH_RETRIES="$9"
SEED_REGISTRY_PUSH_BACKOFF_SECONDS="${10}"
SEED_REGISTRY_PUSH_TIMEOUT_SECONDS="${11}"
SEED_DOCKER_BUILDX="${12}"
SEED_BUILD_SKIP_EXISTING="${13}"
cd "${REMOTE_WORK_DIR}"
tar -xzf compiled.tar.gz
export DOCKER_BUILDKIT="${DOCKER_BUILDKIT}"
export COMPOSE_DOCKER_CLI_BUILD="${COMPOSE_DOCKER_CLI_BUILD}"
export SEED_DOCKER_BUILDKIT="${DOCKER_BUILDKIT}"
export SEED_DOCKER_BUILDX="${SEED_DOCKER_BUILDX}"
export SEED_BUILD_PARALLELISM="${BUILD_PARALLEL_JOBS}"
export SEED_IMAGE_DISTRIBUTION_MODE="${SEED_IMAGE_DISTRIBUTION_MODE}"
export SEED_REGISTRY="${SEED_REGISTRY}"
export SEED_REGISTRY_LOCAL_ENDPOINT="${SEED_REGISTRY_LOCAL_ENDPOINT}"
export SEED_REGISTRY_PUSH_RETRIES="${SEED_REGISTRY_PUSH_RETRIES}"
export SEED_REGISTRY_PUSH_BACKOFF_SECONDS="${SEED_REGISTRY_PUSH_BACKOFF_SECONDS}"
export SEED_REGISTRY_PUSH_TIMEOUT_SECONDS="${SEED_REGISTRY_PUSH_TIMEOUT_SECONDS}"
export SEED_BUILD_SKIP_EXISTING="${SEED_BUILD_SKIP_EXISTING}"
if command -v curl >/dev/null 2>&1; then
  REGISTRY_CHECK_ENDPOINT="${SEED_REGISTRY_LOCAL_ENDPOINT:-${SEED_REGISTRY}}"
  curl -m 5 -fsS "http://${REGISTRY_CHECK_ENDPOINT}/v2/" >/dev/null
fi

install_buildx_wrapper() {
  if [[ "${SEED_DOCKER_BUILDX}" != "1" ]]; then
    echo "[build] docker buildx wrapper disabled"
    return 0
  fi
  local docker_bin
  docker_bin="$(command -v docker || true)"
  if [[ -z "${docker_bin}" ]]; then
    echo "[build] docker command not found" >&2
    return 1
  fi
  if ! "${docker_bin}" buildx version >/dev/null 2>&1; then
    echo "[build] docker buildx unavailable; falling back to docker build" >&2
    return 0
  fi
  local wrapper_dir="${REMOTE_WORK_DIR}/.docker-wrapper"
  mkdir -p "${wrapper_dir}"
  cat > "${wrapper_dir}/docker" <<WRAPPER_EOF
#!/usr/bin/env bash
set -euo pipefail
real_docker=${docker_bin@Q}
if [[ "\${1:-}" == "build" ]]; then
  shift
  tag=""
  args=("\$@")
  for ((i = 0; i < \${#args[@]}; i++)); do
    case "\${args[\$i]}" in
      -t|--tag)
        if (( i + 1 < \${#args[@]} )); then
          tag="\${args[\$((i + 1))]}"
        fi
        ;;
      --tag=*)
        tag="\${args[\$i]#--tag=}"
        ;;
    esac
  done
  if [[ "\${SEED_BUILD_SKIP_EXISTING:-0}" == "1" && -n "\${tag}" ]]; then
    if "\${real_docker}" image inspect "\${tag}" >/dev/null 2>&1; then
      echo "[build] skip existing image: \${tag}" >&2
      exit 0
    fi
  fi
  echo "+ DOCKER_BUILDKIT=1 docker buildx build --load --provenance=false \$*" >&2
  exec "\${real_docker}" buildx build --load --provenance=false "\$@"
fi
exec "\${real_docker}" "\$@"
WRAPPER_EOF
  chmod +x "${wrapper_dir}/docker"
  export PATH="${wrapper_dir}:${PATH}"
  echo "[build] docker build commands will use buildx --load --provenance=false"
}

install_buildx_wrapper
if [ -x "./build_images.sh" ]; then
  ./build_images.sh 2>&1 | tee "${REMOTE_WORK_DIR}/build.log"
else
  echo "Missing build_images.sh" >&2
  exit 1
fi
REMOTE_EOF

scp -q "${SSH_OPTS[@]}" /tmp/build_remote.sh "${SEED_K3S_USER}@${SEED_MASTER_NODE_IP}:${REMOTE_SCRIPT}"
printf -v REMOTE_CMD 'sudo -n bash %q %q %q %q %q %q %q %q %q %q %q %q %q %q' \
    "${REMOTE_SCRIPT}" \
    "${REMOTE_WORK_DIR}" \
    "${SEED_BUILD_PARALLELISM}" \
    "${SEED_BUILD_BATCH_SIZE}" \
    "${SEED_DOCKER_BUILDKIT}" \
    "${SEED_COMPOSE_DOCKER_CLI_BUILD}" \
    "${REMOTE_BUILD_DISTRIBUTION_MODE}" \
    "${SEED_REGISTRY}" \
    "${SEED_REGISTRY_LOCAL_ENDPOINT:-}" \
    "${SEED_REGISTRY_PUSH_RETRIES}" \
    "${SEED_REGISTRY_PUSH_BACKOFF_SECONDS}" \
    "${SEED_REGISTRY_PUSH_TIMEOUT_SECONDS}" \
    "${SEED_DOCKER_BUILDX}" \
    "${SEED_BUILD_SKIP_EXISTING}"
ssh "${SSH_OPTS[@]}" "${SEED_K3S_USER}@${SEED_MASTER_NODE_IP}" "${REMOTE_CMD}"
echo "[build] remote build stage finished"
rm -f /tmp/build_remote.sh

ssh "${SSH_OPTS[@]}" "${SEED_K3S_USER}@${SEED_MASTER_NODE_IP}" \
    "cat ${REMOTE_WORK_DIR}/build.log" > "${EXPERIMENT_DIR}/build_remote.log" 2>/dev/null || true

if [ -s "${OUTPUT_DIR}/images.txt" ]; then
    cp "${OUTPUT_DIR}/images.txt" "${IMAGE_REFS_FILE}"
else
    awk '/^seedemu_build_and_push / {print $2}' "${OUTPUT_DIR}/build_images.sh" > "${IMAGE_REFS_FILE}"
fi

mv "${NODE_IMAGE_VALIDATE_DIR}" "${NODE_IMAGE_DIR}"

PRELOAD_BATCH_SIZE="${SEED_PRELOAD_BATCH_SIZE:-10}"
PRELOAD_RETRIES="${SEED_PRELOAD_RETRIES:-3}"
PRELOAD_BACKOFF_SECONDS="${SEED_PRELOAD_BACKOFF_SECONDS:-5}"
PRELOAD_NODE_CONCURRENCY="${SEED_PRELOAD_NODE_CONCURRENCY:-3}"
PRELOAD_IMAGE_CONCURRENCY="${SEED_PRELOAD_IMAGE_CONCURRENCY:-1}"

preload_images_on_node() {
    local node_ip="$1"
    local node_label="$2"
    local log_file="${EXPERIMENT_DIR}/preload_${node_label}.log"
    local node_image_file="${NODE_IMAGE_DIR}/images_${node_label}.txt"
    local remote_list="/tmp/seedemu-image-refs-$$-${RANDOM}.txt"

    if [ ! -s "${node_image_file}" ]; then
        : > "${log_file}"
        echo "[preload] skip: no images assigned to ${node_label}" | tee -a "${log_file}"
        return 0
    fi

    : > "${log_file}"
    scp -q "${SSH_OPTS[@]}" "${node_image_file}" "${SEED_K3S_USER}@${node_ip}:${remote_list}" >> "${log_file}" 2>&1
    ssh "${SSH_OPTS[@]}" "${SEED_K3S_USER}@${node_ip}" "sudo -n bash -s" > "${log_file}" 2>&1 <<EOF
set -euo pipefail
remote_list="${remote_list}"
image_concurrency="${PRELOAD_IMAGE_CONCURRENCY}"
trap 'rm -f "\$remote_list"' EXIT

pull_image_with_retry() {
  local image="\$1"
  local attempt=1
  while true; do
    if sudo -n k3s ctr images pull --plain-http "\$image"; then
      return 0
    fi
    if [ "\$attempt" -ge "${PRELOAD_RETRIES}" ]; then
      echo "preload failed after ${PRELOAD_RETRIES} attempts: \$image" >&2
      return 1
    fi
    sleep_seconds=\$(( ${PRELOAD_BACKOFF_SECONDS} * \$attempt ))
    echo "retry preload in \${sleep_seconds}s: \$image (\$attempt/${PRELOAD_RETRIES})" >&2
    sleep "\${sleep_seconds}"
    attempt=\$((\$attempt + 1))
  done
}

wait_preload_batch() {
  local rc=0
  local pid
  for pid in "\${pending_pids[@]}"; do
    if ! wait "\$pid"; then
      rc=1
    fi
  done
  pending_pids=()
  return "\$rc"
}

pending_pids=()
if ! [[ "\$image_concurrency" =~ ^[0-9]+$ ]] || [ "\$image_concurrency" -lt 1 ]; then
  image_concurrency=1
fi

echo "[preload] image_concurrency=\$image_concurrency"
while IFS= read -r image; do
  [ -n "\$image" ] || continue
  pull_image_with_retry "\$image" &
  pending_pids+=("\$!")
  if [ "\${#pending_pids[@]}" -ge "\$image_concurrency" ]; then
    wait_preload_batch
  fi
done < "\$remote_list"
if [ "\${#pending_pids[@]}" -gt 0 ]; then
  wait_preload_batch
fi
EOF
    echo "✅ ${node_label} preload completed" | tee -a "${log_file}"
}

pending_pids=()
pending_labels=()

wait_pending_batch() {
    local rc=0
    local idx
    for idx in "${!pending_pids[@]}"; do
        if ! wait "${pending_pids[$idx]}"; then
            echo "preload failed on ${pending_labels[$idx]}" >&2
            rc=1
        fi
    done
    pending_pids=()
    pending_labels=()
    return "${rc}"
}

echo "Preloading images to all cluster nodes from registry..."
for i in "${!SEED_NODE_NAMES[@]}"; do
    preload_images_on_node "${SEED_NODE_IPS[$i]}" "${SEED_NODE_NAMES[$i]}" &
    pending_pids+=("$!")
    pending_labels+=("${SEED_NODE_NAMES[$i]}")
    if [ "${#pending_pids[@]}" -ge "${PRELOAD_NODE_CONCURRENCY}" ]; then
        wait_pending_batch
    fi
done
if [ "${#pending_pids[@]}" -gt 0 ]; then
    wait_pending_batch
fi

echo "Build completed. Log: $(stage_log_file build)"
