#!/usr/bin/env bash
set -euo pipefail

OUTPUT_DIR="${OUTPUT_DIR:?}"
SCALE_MANIFEST="${SCALE_MANIFEST:?}"
IMAGES_YAML="${IMAGES_YAML:?}"
NODE_IMAGE_DIR="${NODE_IMAGE_DIR:?}"
INVENTORY="${INVENTORY:?}"
NODE_ROLE_FILTER="${NODE_ROLE_FILTER:-all}"
REGISTRY_PREFIX="${REGISTRY_PREFIX:?}"
IMAGE_REGISTRY_PREFIX="${IMAGE_REGISTRY_PREFIX:-seedemu}"
SSH_USER="${SSH_USER:-ubuntu}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
HELPER="${HELPER:?}"
NATIVE_RUNNING_DIR="${NATIVE_RUNNING_DIR:?}"
PRELOAD_NODE_CONCURRENCY="${PRELOAD_NODE_CONCURRENCY:-3}"
PRELOAD_BATCH_SIZE="${PRELOAD_BATCH_SIZE:-10}"
PRELOAD_RETRIES="${PRELOAD_RETRIES:-3}"
PRELOAD_BACKOFF_SECONDS="${PRELOAD_BACKOFF_SECONDS:-5}"

ssh_args=(-i "${SSH_KEY}" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o BatchMode=yes -o ConnectTimeout=10)

registry_ref="${REGISTRY_PREFIX#http://}"
registry_ref="${registry_ref#https://}"
registry_ref="${registry_ref%%/*}"
registry_host="${registry_ref%%:*}"
ssh_target="${SSH_USER}@${registry_host}"
remote_dir="/tmp/seedemu-native-scale-build-$(date +%Y%m%d_%H%M%S)-$$"

echo "=== native-k8s largeScale build ==="
echo "output_dir=${OUTPUT_DIR}"
echo "scale_manifest=${SCALE_MANIFEST}"
echo "registry_prefix=${REGISTRY_PREFIX}"
echo "preload_node_concurrency=${PRELOAD_NODE_CONCURRENCY}"

python3 "${HELPER}" node-image-refs \
    --manifest "${SCALE_MANIFEST}" \
    --images-yaml "${IMAGES_YAML}" \
    --inventory "${INVENTORY}" \
    --node-role-filter "${NODE_ROLE_FILTER}" \
    --image-registry-prefix "${IMAGE_REGISTRY_PREFIX}" \
    --registry-prefix "${REGISTRY_PREFIX}" \
    --out-dir "${NODE_IMAGE_DIR}" \
    --require-complete

echo "[scalebuild] uploading compile output to ${ssh_target}:${remote_dir}/output"
ssh "${ssh_args[@]}" "${ssh_target}" "rm -rf '${remote_dir}' && mkdir -p '${remote_dir}/output' '${remote_dir}/running'"
tar -C "${OUTPUT_DIR}" -czf - . | ssh "${ssh_args[@]}" "${ssh_target}" "tar -C '${remote_dir}/output' -xzf -"
tar -C "${NATIVE_RUNNING_DIR}" -czf - . | ssh "${ssh_args[@]}" "${ssh_target}" "tar -C '${remote_dir}/running' -xzf -"

echo "[scalebuild] running buildx build/push on ${ssh_target}"
ssh "${ssh_args[@]}" "${ssh_target}" \
    "cd '${remote_dir}/running' && sudo -n env OUTPUT_DIR='${remote_dir}/output' REGISTRY_PREFIX='${REGISTRY_PREFIX}' IMAGE_REGISTRY_PREFIX='${IMAGE_REGISTRY_PREFIX}' bash ./k8s_local_build.sh"

preload_images_on_node() {
    local node_name="$1"
    local node_ip="$2"
    local node_image_file="${NODE_IMAGE_DIR}/images_${node_name}.txt"
    local log_file="${OUTPUT_DIR}/preload_${node_name}.log"
    local remote_list="/tmp/seedemu-scale-image-refs-$$-${RANDOM}.txt"

    if [ ! -s "${node_image_file}" ]; then
        : > "${log_file}"
        echo "[preload] skip: no images assigned to ${node_name}" | tee -a "${log_file}"
        return 0
    fi

    : > "${log_file}"
    scp -q "${ssh_args[@]}" "${node_image_file}" "${SSH_USER}@${node_ip}:${remote_list}" >> "${log_file}" 2>&1
    ssh "${ssh_args[@]}" "${SSH_USER}@${node_ip}" "sudo -n bash -s" > "${log_file}" 2>&1 <<EOF
set -euo pipefail
remote_list="${remote_list}"
batch_size="${PRELOAD_BATCH_SIZE}"
trap 'rm -f "\$remote_list" /tmp/seedemu-scale-preload-batch-*' EXIT
split -l "\$batch_size" -d -a 4 "\$remote_list" /tmp/seedemu-scale-preload-batch-
for batch in /tmp/seedemu-scale-preload-batch-*; do
  [ -s "\$batch" ] || continue
  while IFS= read -r image; do
    [ -n "\$image" ] || continue
    attempt=1
    while true; do
      if sudo -n k3s ctr images pull --plain-http "\$image"; then
        break
      fi
      if [ "\$attempt" -ge "${PRELOAD_RETRIES}" ]; then
        echo "preload failed after ${PRELOAD_RETRIES} attempts: \$image" >&2
        exit 1
      fi
      sleep_seconds=\$(( ${PRELOAD_BACKOFF_SECONDS} * attempt ))
      echo "retry preload in \${sleep_seconds}s: \$image (\$attempt/${PRELOAD_RETRIES})" >&2
      sleep "\${sleep_seconds}"
      attempt=\$((attempt + 1))
    done
  done < "\$batch"
done
EOF
    echo "[preload] ${node_name} completed" | tee -a "${log_file}"
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

echo "[scalebuild] preloading images to assigned nodes"
while IFS=$'\t' read -r node_name node_ip node_role; do
    [ -n "${node_name}" ] || continue
    preload_images_on_node "${node_name}" "${node_ip}" &
    pending_pids+=("$!")
    pending_labels+=("${node_name}")
    if [ "${#pending_pids[@]}" -ge "${PRELOAD_NODE_CONCURRENCY}" ]; then
        wait_pending_batch
    fi
done < <(python3 "${HELPER}" inventory-nodes --inventory "${INVENTORY}" --node-role-filter "${NODE_ROLE_FILTER}")

if [ "${#pending_pids[@]}" -gt 0 ]; then
    wait_pending_batch
fi

echo "LargeScale build and preload completed"
