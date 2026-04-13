#!/bin/bash
# Docker 初始化脚本 - 用于重置 Master 节点的 Docker 环境
# 使用方法: 在宿主机上运行此脚本

set -e

GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

log() {
    echo -e "${GREEN}[INFO] $1${NC}"
}

err() {
    echo -e "${RED}[ERROR] $1${NC}"
}

# Master 节点 IP
MASTER_IP="192.168.122.110"
SSH_USER="ubuntu"

log "1. 停止 Docker 相关服务..."
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10 ${SSH_USER}@${MASTER_IP} "sudo systemctl stop docker.socket docker containerd 2>/dev/null || true"

log "2. 删除 Docker 数据和配置..."
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10 ${SSH_USER}@${MASTER_IP} "sudo rm -rf /var/lib/docker /var/lib/containerd /etc/docker /var/run/docker /var/run/containerd 2>/dev/null || true"

log "3. 重新创建配置目录并写入 daemon.json..."
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10 ${SSH_USER}@${MASTER_IP} "sudo mkdir -p /etc/docker && sudo tee /etc/docker/daemon.json > /dev/null <<'EOF'
{
  \"registry-mirrors\": [
    \"https://docker.1ms.run\",
    \"https://docker.anyhub.us.kg\",
    \"https://dockerhub.icu\",
    \"https://docker.1panel.live\"
  ],
  \"insecure-registries\": [\"192.168.122.110:5000\", \"127.0.0.1:5000\"]
}
EOF"

log "4. 重载配置并重启 Docker..."
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=30 ${SSH_USER}@${MASTER_IP} "sudo systemctl daemon-reload && sudo systemctl start containerd && sudo systemctl start docker && sleep 5"

log "5. 启动 Registry 容器..."
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10 ${SSH_USER}@${MASTER_IP} "sudo docker rm -f registry 2>/dev/null || true; sudo docker run -d -p 5000:5000 --name registry -v /var/lib/registry:/var/lib/registry registry:2"

log "6. 从宿主机构建并传输 base/router 镜像..."

# 获取脚本所在目录的父目录
SEED_EMULATOR_DIR="${HOME}/seed-emulator"

if [ ! -d "${SEED_EMULATOR_DIR}/docker_images/multiarch/seedemu-base" ]; then
    err "seed-emulator 目录未找到，请检查路径"
    exit 1
fi

log "6.1 在宿主机构建 base 镜像..."
cd "${SEED_EMULATOR_DIR}/docker_images/multiarch/seedemu-base"
docker build -t handsonsecurity/seedemu-multiarch-base:buildx-latest .

log "6.2 在宿主机构建 router 镜像..."
cd "${SEED_EMULATOR_DIR}/docker_images/multiarch/seedemu-router"
docker build -t handsonsecurity/seedemu-multiarch-router:buildx-latest .

log "6.3 导出镜像到 tar 文件..."
docker save -o /tmp/seedemu-base.tar handsonsecurity/seedemu-multiarch-base:buildx-latest handsonsecurity/seedemu-multiarch-router:buildx-latest

log "6.4 传输到 Master 节点..."
scp -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR /tmp/seedemu-base.tar ${SSH_USER}@${MASTER_IP}:/tmp/seedemu-base.tar

log "6.5 在 Master 节点加载镜像..."
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=30 ${SSH_USER}@${MASTER_IP} "sudo docker load -i /tmp/seedemu-base.tar"

log "6.6 Tag 基础镜像 (39e016aa9e819f203ebc1809245a5818)..."
# 注意: 39e016aa9e819f203ebc1809245a5818 是 seedemu-multiarch-router 的 digest，不是 base!
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10 ${SSH_USER}@${MASTER_IP} "sudo docker tag handsonsecurity/seedemu-multiarch-router:buildx-latest 39e016aa9e819f203ebc1809245a5818:latest"

log "6.7 推送到本地 Registry..."
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=30 ${SSH_USER}@${MASTER_IP} "
    sudo docker tag handsonsecurity/seedemu-multiarch-base:buildx-latest 127.0.0.1:5000/handsonsecurity/seedemu-multiarch-base:buildx-latest
    sudo docker push 127.0.0.1:5000/handsonsecurity/seedemu-multiarch-base:buildx-latest
    sudo docker tag handsonsecurity/seedemu-multiarch-router:buildx-latest 127.0.0.1:5000/handsonsecurity/seedemu-multiarch-router:buildx-latest
    sudo docker push 127.0.0.1:5000/handsonsecurity/seedemu-multiarch-router:buildx-latest
"

log "7. 清理临时文件..."
rm -f /tmp/seedemu-base.tar
ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10 ${SSH_USER}@${MASTER_IP} "rm -f /tmp/seedemu-base.tar"

log "完成! Master Docker 已初始化，base/router 镜像已就绪。"