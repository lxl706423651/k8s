#!/usr/bin/env bash
# Build SeedEMU workload images with BuildKit/buildx and push them to the
# registry selected by command-line arguments or configRunning.yaml.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_PATH="${SCRIPT_DIR}/configRunning.yaml"
OUTPUT_DIR=""
REGISTRY_PREFIX=""
IMAGE_REGISTRY_PREFIX=""
HELPER="${SCRIPT_DIR}/manageK8sManifest.py"

usage() {
    cat <<EOF
Usage: $0 [--config configRunning.yaml] [--output-dir output] [--registry-prefix host:port] [--image-registry-prefix seedemu]

Build images listed in images.yaml and push them to the configured registry.
The remote large-scale build path passes explicit --output-dir and
--registry-prefix so it does not depend on host-local config paths.
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --config)
            CONFIG_PATH="$2"
            shift 2
            ;;
        --output-dir)
            OUTPUT_DIR="$2"
            shift 2
            ;;
        --registry-prefix)
            REGISTRY_PREFIX="$2"
            shift 2
            ;;
        --image-registry-prefix)
            IMAGE_REGISTRY_PREFIX="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

if [ -z "${OUTPUT_DIR}" ]; then
    OUTPUT_DIR="$(python3 "${HELPER}" config-value --config "${CONFIG_PATH}" --key outputDir)"
fi
if [ -z "${REGISTRY_PREFIX}" ]; then
    REGISTRY_PREFIX="$(python3 "${HELPER}" config-value --config "${CONFIG_PATH}" --key registryPrefix)"
fi
if [ -z "${IMAGE_REGISTRY_PREFIX}" ]; then
    IMAGE_REGISTRY_PREFIX="$(python3 "${HELPER}" config-value --config "${CONFIG_PATH}" --key imageRegistryPrefix)"
fi

IMAGES_YAML="${OUTPUT_DIR}/images.yaml"
OUTPUT_DIR="$(cd "${OUTPUT_DIR}" && pwd)"
cd "${OUTPUT_DIR}"

buildImageWithBuildx() {
    # Args:
    #   $1: full destination image reference
    #   $2: Docker build context directory
    local image="$1"
    local context="$2"
    local log_file
    log_file="$(mktemp)"
    echo "+ DOCKER_BUILDKIT=1 docker buildx build --load --provenance=false -t ${image} ${context}"
    if DOCKER_BUILDKIT=1 docker buildx build --load --provenance=false -t "${image}" "${context}" 2>&1 | tee "${log_file}"; then
        rm -f "${log_file}"
        return 0
    fi

    if grep -Eq 'parent snapshot .* does not exist|failed to prepare extraction snapshot|failed to solve' "${log_file}"; then
        echo "[k8s_build] buildx export/cache failure while building ${image}; pruning buildx cache and retrying once" >&2
        docker buildx prune -af >/dev/null 2>&1 || true
        rm -f "${log_file}"
        DOCKER_BUILDKIT=1 docker buildx build --load --provenance=false -t "${image}" "${context}"
        return $?
    fi

    rm -f "${log_file}"
    return 1
}

if [ -d "base_images" ]; then
    while IFS= read -r dockerfile; do
        base_tag="$(basename "$(dirname "${dockerfile}")")"
        buildImageWithBuildx "${base_tag}" "$(dirname "${dockerfile}")"
    done < <(find base_images -mindepth 2 -maxdepth 2 -name Dockerfile | sort)
fi

python3 "${HELPER}" mapped-images \
    --images-yaml "${IMAGES_YAML}" \
    --image-registry-prefix "${IMAGE_REGISTRY_PREFIX}" \
    --registry-prefix "${REGISTRY_PREFIX}" |
while IFS=$'\t' read -r image context; do
    [ -n "${image}" ] || continue
    [ -n "${context}" ] || { echo "Missing context for ${image}" >&2; exit 1; }
    buildImageWithBuildx "${image}" "${context}"
    echo "+ docker push ${image}"
    docker push "${image}"
done
