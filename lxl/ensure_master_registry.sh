#!/usr/bin/env bash
# 只确保 master 上的 Docker registry 可用。
# 默认不清理 /var/lib/docker、/var/lib/containerd，也不删除已有镜像。
# 如 registry 已可用，脚本直接退出成功。

set -euo pipefail

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() {
    echo -e "${GREEN}[INFO] $1${NC}"
}

warn() {
    echo -e "${YELLOW}[WARN] $1${NC}"
}

err() {
    echo -e "${RED}[ERROR] $1${NC}"
}

MASTER_IP="${MASTER_IP:-192.168.122.110}"
SSH_USER="${SSH_USER:-ubuntu}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
REGISTRY_PORT="${REGISTRY_PORT:-5000}"
REGISTRY_NAME="${REGISTRY_NAME:-registry}"
REGISTRY_DATA_DIR="${REGISTRY_DATA_DIR:-/var/lib/registry}"
REGISTRY_IMAGE="${REGISTRY_IMAGE:-registry:2}"

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

registry_healthy() {
    remote "curl -m 5 -fsS http://127.0.0.1:${REGISTRY_PORT}/v2/ >/dev/null"
}

log "Checking registry health on ${MASTER_IP}:${REGISTRY_PORT} ..."
if registry_healthy; then
    log "Registry is already healthy. Nothing to do."
    exit 0
fi

warn "Registry is not healthy. Repairing registry only, without clearing Docker data."

log "Ensuring Docker service is running..."
remote "sudo systemctl start docker"

log "Ensuring /etc/docker/daemon.json contains insecure registry settings..."
remote "sudo mkdir -p /etc/docker"
remote "sudo python3 - <<'PY'
import json
from pathlib import Path

path = Path('/etc/docker/daemon.json')
data = {}
if path.exists():
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        data = {}

insecure = list(data.get('insecure-registries', []) or [])
for value in ('127.0.0.1:${REGISTRY_PORT}', '${MASTER_IP}:${REGISTRY_PORT}'):
    if value not in insecure:
        insecure.append(value)
data['insecure-registries'] = insecure

path.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
PY"

log "Restarting Docker to apply registry settings..."
remote "sudo systemctl restart docker"

log "Ensuring registry data directory exists..."
remote "sudo mkdir -p '${REGISTRY_DATA_DIR}'"

log "Removing stale registry container if present..."
remote "sudo docker rm -f '${REGISTRY_NAME}' >/dev/null 2>&1 || true"

log "Starting registry container..."
remote "sudo docker run -d \
  --restart unless-stopped \
  -p ${REGISTRY_PORT}:5000 \
  --name '${REGISTRY_NAME}' \
  -v '${REGISTRY_DATA_DIR}:/var/lib/registry' \
  '${REGISTRY_IMAGE}' >/dev/null"

log "Waiting for registry to become healthy..."
for _ in $(seq 1 15); do
    if registry_healthy; then
        log "Registry is healthy."
        remote "curl -m 5 -fsS http://127.0.0.1:${REGISTRY_PORT}/v2/_catalog || true"
        exit 0
    fi
    sleep 1
done

err "Registry failed to become healthy."
exit 1
