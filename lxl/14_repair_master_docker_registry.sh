#!/usr/bin/env bash
set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log() { echo -e "${GREEN}[INFO] $*${NC}"; }
warn() { echo -e "${YELLOW}[WARN] $*${NC}"; }
err() { echo -e "${RED}[ERROR] $*${NC}" >&2; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -f "${SCRIPT_DIR}/env_9node.sh" ]; then
    # shellcheck disable=SC1091
    source "${SCRIPT_DIR}/env_9node.sh" >/dev/null 2>&1 || true
fi

if [ -f "${SCRIPT_DIR}/01_cluster_nodes_9node.sh" ]; then
    # shellcheck disable=SC1091
    source "${SCRIPT_DIR}/01_cluster_nodes_9node.sh"
fi

MASTER_IP="${MASTER_IP:-${SEED_K3S_MASTER_IP:-192.168.122.110}}"
SSH_USER="${SSH_USER:-${SEED_K3S_USER:-ubuntu}}"
SSH_KEY="${SSH_KEY:-${SEED_K3S_SSH_KEY:-$HOME/.ssh/id_ed25519}}"
REGISTRY_PORT="${REGISTRY_PORT:-5000}"
REGISTRY_NAME="${REGISTRY_NAME:-registry}"
REGISTRY_DATA_DIR="${REGISTRY_DATA_DIR:-/var/lib/registry}"
REGISTRY_IMAGE="${REGISTRY_IMAGE:-registry:2}"
SEED_EMULATOR_DIR="${SEED_EMULATOR_DIR:-$HOME/seed-emulator}"

MODE="auto"
WITH_BASE_IMAGES=1
SKIP_WORKER_PROBE=0
SKIP_K8S_PROBE=0

usage() {
    cat <<EOF
Usage:
  $(basename "$0") [--mode auto|registry-only|full-init] [--without-base-images] [--skip-worker-probe] [--skip-k8s-probe]

Modes:
  auto
    1. Check current registry health.
    2. If unhealthy, do a lightweight registry-only repair.
    3. If still unhealthy, stop and tell you to rerun with --mode full-init.

  registry-only
    Only do the lightweight registry repair.
    This preserves /var/lib/docker and /var/lib/containerd.

  full-init
    Reinitialize master Docker like init_master_docker.sh:
    - stop docker/containerd
    - wipe Docker/containerd data and config
    - recreate daemon.json
    - restart Docker
    - recreate registry
    - optionally rebuild/load/push seedemu base/router images

Notes:
  - full-init is destructive for Docker state on the master VM.
  - base/router sync uses the host's local Docker and the seed-emulator repo.
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --mode)
            MODE="${2:-}"
            shift 2
            ;;
        --without-base-images)
            WITH_BASE_IMAGES=0
            shift
            ;;
        --skip-worker-probe)
            SKIP_WORKER_PROBE=1
            shift
            ;;
        --skip-k8s-probe)
            SKIP_K8S_PROBE=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            err "Unknown argument: $1"
            usage
            exit 1
            ;;
    esac
done

case "${MODE}" in
    auto|registry-only|full-init) ;;
    *)
        err "Invalid mode: ${MODE}"
        usage
        exit 1
        ;;
esac

SSH_OPTS=(
  -i "${SSH_KEY}"
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
  -o LogLevel=ERROR
  -o ConnectTimeout=10
)

remote() {
    ssh "${SSH_OPTS[@]}" "${SSH_USER}@${MASTER_IP}" "$@"
}

ensure_node_inventory() {
    if declare -F seed_load_cluster_nodes >/dev/null 2>&1; then
        seed_load_cluster_nodes
        return 0
    fi

    SEED_NODE_NAMES=("seed-k3s-master")
    SEED_NODE_IPS=("${MASTER_IP}")
    SEED_WORKER_NODE_NAMES=()
    SEED_WORKER_NODE_IPS=()
    local idx name_var ip_var ip
    for idx in 1 2 3 4 5 6 7 8; do
        name_var="SEED_K3S_WORKER${idx}_NAME"
        ip_var="SEED_K3S_WORKER${idx}_IP"
        ip="${!ip_var:-}"
        [ -n "${ip}" ] || continue
        SEED_WORKER_NODE_NAMES+=("${!name_var:-seed-k3s-worker${idx}}")
        SEED_WORKER_NODE_IPS+=("${ip}")
        SEED_NODE_NAMES+=("${!name_var:-seed-k3s-worker${idx}}")
        SEED_NODE_IPS+=("${ip}")
    done
}

master_registry_healthy() {
    remote "curl -m 5 -fsS http://127.0.0.1:${REGISTRY_PORT}/v2/ >/dev/null"
}

worker_registry_probe() {
    ensure_node_inventory
    local i failures=0
    for i in "${!SEED_WORKER_NODE_NAMES[@]}"; do
        local name="${SEED_WORKER_NODE_NAMES[$i]}"
        local ip="${SEED_WORKER_NODE_IPS[$i]}"
        if ssh "${SSH_OPTS[@]}" "${SSH_USER}@${ip}" \
            "curl -m 5 -fsS http://${MASTER_IP}:${REGISTRY_PORT}/v2/ >/dev/null"; then
            log "${name} -> registry OK"
        else
            warn "${name} -> registry FAILED"
            failures=$((failures + 1))
        fi
    done
    return "${failures}"
}

k8s_probe() {
    if [ "${SKIP_K8S_PROBE}" -eq 1 ]; then
        return 0
    fi
    if [ -f "$HOME/k8s/output/kubeconfigs/seedemu-k3s.yaml" ]; then
        KUBECONFIG="$HOME/k8s/output/kubeconfigs/seedemu-k3s.yaml" kubectl get nodes -o wide
    else
        warn "Kubeconfig not found at $HOME/k8s/output/kubeconfigs/seedemu-k3s.yaml; skipping cluster probe."
    fi
}

write_daemon_json() {
    remote "sudo mkdir -p /etc/docker"
    remote "sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{
  \"registry-mirrors\": [
    \"https://docker.1ms.run\",
    \"https://docker.anyhub.us.kg\",
    \"https://dockerhub.icu\",
    \"https://docker.1panel.live\",
    \"https://docker.m.daocloud.io\"
  ],
  \"insecure-registries\": [
    \"127.0.0.1:${REGISTRY_PORT}\",
    \"${MASTER_IP}:${REGISTRY_PORT}\",
    \"localhost:${REGISTRY_PORT}\"
  ],
  \"max-concurrent-uploads\": 1
}
EOF"
}

start_registry_container() {
    remote "sudo mkdir -p '${REGISTRY_DATA_DIR}'"
    remote "sudo docker rm -f '${REGISTRY_NAME}' >/dev/null 2>&1 || true"
    remote "sudo docker run -d \
      --restart unless-stopped \
      -p ${REGISTRY_PORT}:5000 \
      --name '${REGISTRY_NAME}' \
      -v '${REGISTRY_DATA_DIR}:/var/lib/registry' \
      '${REGISTRY_IMAGE}' >/dev/null"
}

wait_for_registry() {
    local i
    for i in $(seq 1 20); do
        if master_registry_healthy; then
            return 0
        fi
        sleep 1
    done
    return 1
}

registry_only_repair() {
    warn "Doing lightweight registry-only repair. Docker data will be preserved."
    remote "sudo systemctl start docker"
    write_daemon_json
    remote "sudo systemctl restart docker"
    start_registry_container
    if ! wait_for_registry; then
        err "Registry did not become healthy after lightweight repair."
        return 1
    fi
    log "Registry is healthy after lightweight repair."
    return 0
}

build_and_push_seed_images() {
    if [ "${WITH_BASE_IMAGES}" -ne 1 ]; then
        warn "Skipping base/router image rebuild and push (--without-base-images)."
        return 0
    fi

    if [ ! -d "${SEED_EMULATOR_DIR}/docker_images/multiarch/seedemu-base" ] || \
       [ ! -d "${SEED_EMULATOR_DIR}/docker_images/multiarch/seedemu-router" ]; then
        err "seed-emulator docker image directories not found under ${SEED_EMULATOR_DIR}"
        return 1
    fi

    local tar_path="/tmp/seedemu-base-router.tar"
    log "Building seedemu base image on host..."
    (
        cd "${SEED_EMULATOR_DIR}/docker_images/multiarch/seedemu-base"
        docker build -t handsonsecurity/seedemu-multiarch-base:buildx-latest .
    )

    log "Building seedemu router image on host..."
    (
        cd "${SEED_EMULATOR_DIR}/docker_images/multiarch/seedemu-router"
        docker build -t handsonsecurity/seedemu-multiarch-router:buildx-latest .
    )

    log "Exporting seedemu base/router images to ${tar_path} ..."
    docker save -o "${tar_path}" \
        handsonsecurity/seedemu-multiarch-base:buildx-latest \
        handsonsecurity/seedemu-multiarch-router:buildx-latest

    log "Copying base/router tarball to master..."
    scp "${SSH_OPTS[@]}" "${tar_path}" "${SSH_USER}@${MASTER_IP}:${tar_path}"

    log "Loading images on master..."
    remote "sudo docker load -i '${tar_path}'"

    log "Pushing base/router images into local registry..."
    remote "sudo docker tag handsonsecurity/seedemu-multiarch-base:buildx-latest 127.0.0.1:${REGISTRY_PORT}/handsonsecurity/seedemu-multiarch-base:buildx-latest"
    remote "sudo docker push 127.0.0.1:${REGISTRY_PORT}/handsonsecurity/seedemu-multiarch-base:buildx-latest"
    remote "sudo docker tag handsonsecurity/seedemu-multiarch-router:buildx-latest 127.0.0.1:${REGISTRY_PORT}/handsonsecurity/seedemu-multiarch-router:buildx-latest"
    remote "sudo docker push 127.0.0.1:${REGISTRY_PORT}/handsonsecurity/seedemu-multiarch-router:buildx-latest"

    rm -f "${tar_path}"
    remote "rm -f '${tar_path}'"
}

full_init() {
    warn "Doing full Docker initialization on master. This clears Docker and containerd state on the master VM."
    remote "sudo systemctl stop docker.socket docker containerd 2>/dev/null || true"
    remote "sudo rm -rf /var/lib/docker /var/lib/containerd /etc/docker /var/run/docker /var/run/containerd 2>/dev/null || true"
    write_daemon_json
    remote "sudo systemctl daemon-reload && sudo systemctl start containerd && sudo systemctl start docker"
    start_registry_container
    if ! wait_for_registry; then
        err "Registry did not become healthy after full initialization."
        return 1
    fi
    build_and_push_seed_images
    log "Full Docker initialization completed."
}

verify_everything() {
    log "Verifying master docker and registry..."
    remote "systemctl is-active docker"
    remote "sudo docker ps -a --format '{{.Names}} {{.Status}} {{.Ports}}' | grep '^${REGISTRY_NAME} '"
    remote "sudo ss -lntp | grep ':${REGISTRY_PORT}'"
    remote "curl -m 5 -fsS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:${REGISTRY_PORT}/v2/"
    remote "curl -m 5 -fsS -o /dev/null -w '%{http_code}\n' http://${MASTER_IP}:${REGISTRY_PORT}/v2/"

    if [ "${SKIP_WORKER_PROBE}" -ne 1 ]; then
        log "Verifying worker -> registry connectivity..."
        worker_registry_probe
    fi

    log "Verifying k3s nodes..."
    k8s_probe
}

main() {
    log "Mode: ${MODE}"

    if master_registry_healthy; then
        log "Registry is already healthy."
        verify_everything
        exit 0
    fi

    case "${MODE}" in
        auto)
            if registry_only_repair; then
                verify_everything
                exit 0
            fi
            err "Lightweight repair failed. Rerun with --mode full-init if you want a destructive full reset."
            exit 1
            ;;
        registry-only)
            registry_only_repair
            verify_everything
            ;;
        full-init)
            full_init
            verify_everything
            ;;
    esac
}

main "$@"
