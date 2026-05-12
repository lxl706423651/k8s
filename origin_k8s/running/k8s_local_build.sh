#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_DIR="${OUTPUT_DIR:-${SCRIPT_DIR}/../emulate/output}"
REGISTRY_PREFIX="${REGISTRY_PREFIX:-192.168.122.110:5000}"
IMAGE_REGISTRY_PREFIX="${IMAGE_REGISTRY_PREFIX:-seedemu}"
IMAGES_YAML="${IMAGES_YAML:-${OUTPUT_DIR}/images.yaml}"
HELPER="${HELPER:-${SCRIPT_DIR}/k8s_make_helper.py}"

OUTPUT_DIR="$(cd "${OUTPUT_DIR}" && pwd)"
cd "${OUTPUT_DIR}"

buildx_build_load() {
    local image="$1"
    local context="$2"
    echo "+ DOCKER_BUILDKIT=1 docker buildx build --load -t ${image} ${context}"
    DOCKER_BUILDKIT=1 docker buildx build --load -t "${image}" "${context}"
}

if [ -d "base_images" ]; then
    while IFS= read -r dockerfile; do
        base_tag="$(basename "$(dirname "${dockerfile}")")"
        buildx_build_load "${base_tag}" "$(dirname "${dockerfile}")"
    done < <(find base_images -mindepth 2 -maxdepth 2 -name Dockerfile | sort)
fi

python3 "${HELPER}" mapped-images \
    --images-yaml "${IMAGES_YAML}" \
    --image-registry-prefix "${IMAGE_REGISTRY_PREFIX}" \
    --registry-prefix "${REGISTRY_PREFIX}" |
while IFS=$'\t' read -r image context; do
    [ -n "${image}" ] || continue
    [ -n "${context}" ] || { echo "Missing context for ${image}" >&2; exit 1; }
    buildx_build_load "${image}" "${context}"
    echo "+ docker push ${image}"
    docker push "${image}"
done
