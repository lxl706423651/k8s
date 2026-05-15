#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_PATH="${1:-${SCRIPT_DIR}/kvm_template.yaml}"
HELPER="${SCRIPT_DIR}/cluster_config.py"

HOST_IMAGE_CACHE_DIR="${SCRIPT_DIR}/image-cache"
HOST_DOCKER_IO_MIRROR="${HOST_DOCKER_IO_MIRROR:-docker.m.daocloud.io}"
PREPARE_FORCE="${PREPARE_FORCE:-false}"
REGISTRY_BOOTSTRAP_IMAGE="registry:2"
MULTUS_BOOTSTRAP_IMAGE="ghcr.io/k8snetworkplumbingwg/multus-cni:snapshot"
SEED_EMULATOR_DOCKER_DIR="${SEED_EMULATOR_DOCKER_DIR:-/home/lxl/seed-emulator/docker_images/multiarch}"
SEED_BASE_SOURCE_IMAGE="handsonsecurity/seedemu-multiarch-base:buildx-latest"
SEED_ROUTER_SOURCE_IMAGE="handsonsecurity/seedemu-multiarch-router:buildx-latest"
SEED_BASE_HASH_IMAGE="98a2693c996c2294358552f48373498d:latest"
SEED_ROUTER_HASH_IMAGE="39e016aa9e819f203ebc1809245a5818:latest"
SEED_UBUNTU_BUILD_IMAGE="ubuntu:20.04"

usage() {
    cat <<EOF
Usage: $0 [kvm.yaml]

Prepare setup assets under ${SCRIPT_DIR} without creating VMs or installing K3s.

It prepares:
  - Ubuntu cloud image at SEED_KVM_BASE_IMAGE_PATH
  - Docker image tar cache under image-cache/

Set PREPARE_FORCE=true to overwrite existing image-cache tar files.

The default config is:
  ${SCRIPT_DIR}/kvm_template.yaml
EOF
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "Missing required command: $1" >&2
        exit 1
    }
}

image_tar_name() {
    printf '%s\n' "$1" | sed 's|[^A-Za-z0-9_.-]|_|g'
}

docker_io_mirror_ref() {
    local image="$1"

    if [[ "${image}" == */*/* ]]; then
        return 1
    fi

    if [[ "${image}" == */* ]]; then
        printf '%s/%s\n' "${HOST_DOCKER_IO_MIRROR}" "${image}"
    else
        printf '%s/library/%s\n' "${HOST_DOCKER_IO_MIRROR}" "${image}"
    fi
}

ensure_host_docker_image() {
    local image="$1"
    local mirror_image=""

    if docker image inspect "${image}" >/dev/null 2>&1; then
        return 0
    fi

    echo "  docker pull ${image}"
    if docker pull "${image}" >/dev/null; then
        return 0
    fi

    if mirror_image="$(docker_io_mirror_ref "${image}")"; then
        echo "  docker pull ${mirror_image}"
        docker pull "${mirror_image}" >/dev/null
        docker tag "${mirror_image}" "${image}" >/dev/null
        return 0
    fi

    echo "Failed to prepare Docker image on host: ${image}" >&2
    return 1
}

host_image_tarball() {
    local image="$1"
    mkdir -p "${HOST_IMAGE_CACHE_DIR}"
    printf '%s/%s.tar\n' "${HOST_IMAGE_CACHE_DIR}" "$(image_tar_name "${image}")"
}

save_host_image_tarball() {
    local image="$1"
    local tar_path
    tar_path="$(host_image_tarball "${image}")"
    if [ "${PREPARE_FORCE}" != "true" ] && [ -s "${tar_path}" ]; then
        echo "  cache exists: ${tar_path}"
        return
    fi
    ensure_host_docker_image "${image}"
    echo "  docker save ${image} -> ${tar_path}"
    docker save -o "${tar_path}" "${image}"
}

prepare_base_image() {
    mkdir -p "$(dirname "${SEED_KVM_BASE_IMAGE_PATH}")"

    if [ -e "${SEED_KVM_BASE_IMAGE_PATH}" ]; then
        echo "Base image already exists: ${SEED_KVM_BASE_IMAGE_PATH}"
        return
    fi

    if [ -n "${SEED_KVM_LEGACY_BASE_IMAGE_PATH:-}" ] && [ -f "${SEED_KVM_LEGACY_BASE_IMAGE_PATH}" ]; then
        echo "Reusing existing base image from ${SEED_KVM_LEGACY_BASE_IMAGE_PATH}"
        ln -s "${SEED_KVM_LEGACY_BASE_IMAGE_PATH}" "${SEED_KVM_BASE_IMAGE_PATH}"
        return
    fi

    local output_image=""
    output_image="$(find /home/lxl/k8s/output -type f -name "$(basename "${SEED_KVM_BASE_IMAGE_PATH}")" -print -quit 2>/dev/null || true)"
    if [ -n "${output_image}" ]; then
        echo "Reusing existing base image from ${output_image}"
        ln -s "${output_image}" "${SEED_KVM_BASE_IMAGE_PATH}"
        return
    fi

    echo "Downloading Ubuntu cloud image:"
    echo "  url=${SEED_KVM_BASE_IMAGE_URL}"
    echo "  output=${SEED_KVM_BASE_IMAGE_PATH}"
    curl -fL "${SEED_KVM_BASE_IMAGE_URL}" -o "${SEED_KVM_BASE_IMAGE_PATH}"
}

prepare_seedemu_build_images() {
    ensure_host_docker_image "${SEED_UBUNTU_BUILD_IMAGE}"

    if ! docker image inspect "${SEED_BASE_SOURCE_IMAGE}" >/dev/null 2>&1; then
        if [ -d "${SEED_EMULATOR_DOCKER_DIR}/seedemu-base" ]; then
            echo "  docker build ${SEED_BASE_SOURCE_IMAGE}"
            DOCKER_BUILDKIT=1 docker build -t "${SEED_BASE_SOURCE_IMAGE}" \
                "${SEED_EMULATOR_DOCKER_DIR}/seedemu-base" >/dev/null
        else
            ensure_host_docker_image "${SEED_BASE_SOURCE_IMAGE}"
        fi
    fi

    if ! docker image inspect "${SEED_ROUTER_SOURCE_IMAGE}" >/dev/null 2>&1; then
        if [ -d "${SEED_EMULATOR_DOCKER_DIR}/seedemu-router" ]; then
            echo "  docker build ${SEED_ROUTER_SOURCE_IMAGE}"
            DOCKER_BUILDKIT=1 docker build -t "${SEED_ROUTER_SOURCE_IMAGE}" \
                "${SEED_EMULATOR_DOCKER_DIR}/seedemu-router" >/dev/null
        else
            ensure_host_docker_image "${SEED_ROUTER_SOURCE_IMAGE}"
        fi
    fi

    docker tag "${SEED_BASE_SOURCE_IMAGE}" "${SEED_BASE_HASH_IMAGE}" >/dev/null
    docker tag "${SEED_ROUTER_SOURCE_IMAGE}" "${SEED_ROUTER_HASH_IMAGE}" >/dev/null
}

prepare_image_cache() {
    mkdir -p "${HOST_IMAGE_CACHE_DIR}"
    prepare_seedemu_build_images

    save_host_image_tarball "${REGISTRY_BOOTSTRAP_IMAGE}"
    save_host_image_tarball "${MULTUS_BOOTSTRAP_IMAGE}"
    save_host_image_tarball "${SEED_UBUNTU_BUILD_IMAGE}"
    save_host_image_tarball "${SEED_BASE_SOURCE_IMAGE}"
    save_host_image_tarball "${SEED_ROUTER_SOURCE_IMAGE}"

    echo "  hash tags prepared locally, not saved into image-cache:"
    echo "    ${SEED_BASE_HASH_IMAGE}"
    echo "    ${SEED_ROUTER_HASH_IMAGE}"
}

main() {
    if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
        usage
        exit 0
    fi

    require_cmd python3
    require_cmd curl
    require_cmd docker
    require_cmd find

    eval "$(python3 "${HELPER}" "${CONFIG_PATH}" kvm-env)"

    echo "Preparing setup assets using config: ${CONFIG_PATH}"
    echo "base_image_path=${SEED_KVM_BASE_IMAGE_PATH}"
    echo "image_cache_dir=${HOST_IMAGE_CACHE_DIR}"
    prepare_base_image
    prepare_image_cache
    echo "Setup assets are ready."
}

main "$@"
