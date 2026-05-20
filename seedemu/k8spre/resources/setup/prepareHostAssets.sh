#!/usr/bin/env bash
# Prepare host-side assets required before KVM creation:
# Ubuntu cloud image, registry/K3s bootstrap image tarballs, and SeedEMU base
# image tags. All user-configurable paths come from kvm.yaml.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_PATH="${1:-${SCRIPT_DIR}/kvm.yaml}"
HELPER="${SCRIPT_DIR}/manageKvmConfig.py"

HOST_IMAGE_CACHE_DIR="${SCRIPT_DIR}/image-cache"
hostDockerIoMirror="docker.m.daocloud.io"
prepareForce="false"
kvmBaseImageReuseMode="copy"
REGISTRY_BOOTSTRAP_IMAGE="registry:2"
MULTUS_BOOTSTRAP_IMAGE="ghcr.io/k8snetworkplumbingwg/multus-cni:snapshot"
K3S_SYSTEM_BOOTSTRAP_IMAGES=(
    "rancher/mirrored-coredns-coredns:1.10.1"
    "rancher/mirrored-metrics-server:v0.6.3"
    "rancher/local-path-provisioner:v0.0.24"
)
seedEmulatorDockerDir="${HOME}/seed-emulator/docker_images/multiarch"
kvmBaseImageSearchDirs="${HOME}/k8s/output"
seedBaseSourceImage="handsonsecurity/seedemu-multiarch-base:buildx-latest"
seedRouterSourceImage="handsonsecurity/seedemu-multiarch-router:buildx-latest"
seedBaseHashImage="98a2693c996c2294358552f48373498d:latest"
seedRouterHashImage="39e016aa9e819f203ebc1809245a5818:latest"
ubuntuBuildImage="ubuntu:20.04"

usage() {
    cat <<EOF
Usage: $0 [kvm.yaml]

Prepare setup assets under ${SCRIPT_DIR} without creating VMs or installing K3s.

It prepares:
  - Ubuntu cloud image at kvmBaseImagePath
  - Docker image tar cache under image-cache/

The default config is:
  ${SCRIPT_DIR}/kvm.yaml
EOF
}

requireCommand() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "Missing required command: $1" >&2
        exit 1
    }
}

imageTarName() {
    printf '%s\n' "$1" | sed 's|[^A-Za-z0-9_.-]|_|g'
}

dockerIoMirrorRef() {
    local image="$1"

    if [[ "${image}" == */*/* ]]; then
        return 1
    fi

    if [[ "${image}" == */* ]]; then
        printf '%s/%s\n' "${hostDockerIoMirror}" "${image}"
    else
        printf '%s/library/%s\n' "${hostDockerIoMirror}" "${image}"
    fi
}

ensureHostDockerImage() {
    local image="$1"
    local mirror_image=""

    if docker image inspect "${image}" >/dev/null 2>&1; then
        return 0
    fi

    echo "  docker pull ${image}"
    if docker pull "${image}" >/dev/null; then
        return 0
    fi

    if mirror_image="$(dockerIoMirrorRef "${image}")"; then
        echo "  docker pull ${mirror_image}"
        docker pull "${mirror_image}" >/dev/null
        docker tag "${mirror_image}" "${image}" >/dev/null
        return 0
    fi

    echo "Failed to prepare Docker image on host: ${image}" >&2
    return 1
}

hostImageTarball() {
    local image="$1"
    mkdir -p "${HOST_IMAGE_CACHE_DIR}"
    printf '%s/%s.tar\n' "${HOST_IMAGE_CACHE_DIR}" "$(imageTarName "${image}")"
}

saveHostImageTarball() {
    local image="$1"
    local tar_path
    tar_path="$(hostImageTarball "${image}")"
    if [ "${prepareForce}" != "true" ] && [ -s "${tar_path}" ]; then
        echo "  cache exists: ${tar_path}"
        return
    fi
    ensureHostDockerImage "${image}"
    echo "  docker save ${image} -> ${tar_path}"
    docker save -o "${tar_path}" "${image}"
}

prepareBaseImage() {
    mkdir -p "$(dirname "${kvmBaseImagePath}")"

    if [ -e "${kvmBaseImagePath}" ]; then
        echo "Base image already exists: ${kvmBaseImagePath}"
        return
    fi

    if [ -n "${kvmLegacyBaseImagePath:-}" ] && [ -f "${kvmLegacyBaseImagePath}" ]; then
        echo "Reusing existing base image from ${kvmLegacyBaseImagePath}"
        if [ "${kvmBaseImageReuseMode}" = "symlink" ]; then
            ln -s "${kvmLegacyBaseImagePath}" "${kvmBaseImagePath}"
        else
            cp --reflink=auto "${kvmLegacyBaseImagePath}" "${kvmBaseImagePath}"
        fi
        return
    fi

    local output_image=""
    local search_dir=""
    for search_dir in ${kvmBaseImageSearchDirs}; do
        [ -d "${search_dir}" ] || continue
        output_image="$(find "${search_dir}" -type f -name "$(basename "${kvmBaseImagePath}")" -print -quit 2>/dev/null || true)"
        if [ -n "${output_image}" ]; then
            echo "Reusing existing base image from ${output_image}"
            if [ "${kvmBaseImageReuseMode}" = "symlink" ]; then
                ln -s "${output_image}" "${kvmBaseImagePath}"
            else
                cp --reflink=auto "${output_image}" "${kvmBaseImagePath}"
            fi
            return
        fi
    done

    echo "Downloading Ubuntu cloud image:"
    echo "  url=${kvmBaseImageUrl}"
    echo "  output=${kvmBaseImagePath}"
    curl -fL "${kvmBaseImageUrl}" -o "${kvmBaseImagePath}"
}

prepareSeedemuBuildImages() {
    ensureHostDockerImage "${ubuntuBuildImage}"

    if ! docker image inspect "${seedBaseSourceImage}" >/dev/null 2>&1; then
        if [ -d "${seedEmulatorDockerDir}/seedemu-base" ]; then
            echo "  docker build ${seedBaseSourceImage}"
            DOCKER_BUILDKIT=1 docker build -t "${seedBaseSourceImage}" \
                "${seedEmulatorDockerDir}/seedemu-base" >/dev/null
        else
            ensureHostDockerImage "${seedBaseSourceImage}"
        fi
    fi

    if ! docker image inspect "${seedRouterSourceImage}" >/dev/null 2>&1; then
        if [ -d "${seedEmulatorDockerDir}/seedemu-router" ]; then
            echo "  docker build ${seedRouterSourceImage}"
            DOCKER_BUILDKIT=1 docker build -t "${seedRouterSourceImage}" \
                "${seedEmulatorDockerDir}/seedemu-router" >/dev/null
        else
            ensureHostDockerImage "${seedRouterSourceImage}"
        fi
    fi

    docker tag "${seedBaseSourceImage}" "${seedBaseHashImage}" >/dev/null
    docker tag "${seedRouterSourceImage}" "${seedRouterHashImage}" >/dev/null
}

prepareImageCache() {
    mkdir -p "${HOST_IMAGE_CACHE_DIR}"
    prepareSeedemuBuildImages

    saveHostImageTarball "${REGISTRY_BOOTSTRAP_IMAGE}"
    saveHostImageTarball "${MULTUS_BOOTSTRAP_IMAGE}"
    for image in "${K3S_SYSTEM_BOOTSTRAP_IMAGES[@]}"; do
        saveHostImageTarball "${image}"
    done
    saveHostImageTarball "${ubuntuBuildImage}"
    saveHostImageTarball "${seedBaseSourceImage}"
    saveHostImageTarball "${seedRouterSourceImage}"

    echo "  hash tags prepared locally, not saved into image-cache:"
    echo "    ${seedBaseHashImage}"
    echo "    ${seedRouterHashImage}"
}

main() {
    if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
        usage
        exit 0
    fi

    requireCommand python3
    requireCommand curl
    requireCommand docker
    requireCommand find

    eval "$(python3 "${HELPER}" "${CONFIG_PATH}" kvm-vars)"

    echo "Preparing setup assets using config: ${CONFIG_PATH}"
    echo "base_image_path=${kvmBaseImagePath}"
    echo "image_cache_dir=${HOST_IMAGE_CACHE_DIR}"
    prepareBaseImage
    prepareImageCache
    echo "Setup assets are ready."
}

main "$@"
