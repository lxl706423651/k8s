#!/usr/bin/env bash
###############################################################################
# env_9node.sh - 9 节点环境变量配置
# 用法: source env_9node.sh
###############################################################################

# ============ 集群配置（9 节点）============
# K3s 集群节点 IP
export SEED_K3S_MASTER_IP="${SEED_K3S_MASTER_IP:-192.168.122.110}"
export SEED_K3S_WORKER1_IP="${SEED_K3S_WORKER1_IP:-192.168.122.111}"
export SEED_K3S_WORKER2_IP="${SEED_K3S_WORKER2_IP:-192.168.122.112}"
export SEED_K3S_WORKER3_IP="${SEED_K3S_WORKER3_IP:-192.168.122.113}"
export SEED_K3S_WORKER4_IP="${SEED_K3S_WORKER4_IP:-192.168.122.114}"
export SEED_K3S_WORKER5_IP="${SEED_K3S_WORKER5_IP:-192.168.122.115}"
export SEED_K3S_WORKER6_IP="${SEED_K3S_WORKER6_IP:-192.168.122.116}"
export SEED_K3S_WORKER7_IP="${SEED_K3S_WORKER7_IP:-192.168.122.117}"
export SEED_K3S_WORKER8_IP="${SEED_K3S_WORKER8_IP:-192.168.122.118}"
export SEED_K3S_ALL_NODE_IPS="${SEED_K3S_ALL_NODE_IPS:-${SEED_K3S_MASTER_IP} ${SEED_K3S_WORKER1_IP} ${SEED_K3S_WORKER2_IP} ${SEED_K3S_WORKER3_IP} ${SEED_K3S_WORKER4_IP} ${SEED_K3S_WORKER5_IP} ${SEED_K3S_WORKER6_IP} ${SEED_K3S_WORKER7_IP} ${SEED_K3S_WORKER8_IP}}"

# SSH 配置
export SEED_K3S_USER="${SEED_K3S_USER:-ubuntu}"
export SEED_K3S_SSH_KEY="${SEED_K3S_SSH_KEY:-$HOME/.ssh/id_ed25519}"

# 集群名称（用于定位 kubeconfig）
export SEED_K3S_CLUSTER_NAME="${SEED_K3S_CLUSTER_NAME:-seedemu-k3s}"
export SEED_CLUSTER_INVENTORY="${SEED_CLUSTER_INVENTORY:-seedemu-k3s-9node}"
export SEED_CLUSTER_INVENTORY_PATH="${SEED_CLUSTER_INVENTORY_PATH:-${HOME}/k8s/configs/clusters/seedemu-k3s-9node.yaml}"

# ============ 拓扑配置（必须修改）=======================
# 拓扑规模
export SEED_TOPOLOGY_SIZE="${SEED_TOPOLOGY_SIZE:-4954}"
# 拓扑文件目录
export SEED_REAL_TOPOLOGY_DIR="${SEED_REAL_TOPOLOGY_DIR:-$HOME/seed-emulator/topology}"

# ============ 可选配置（一般不需要修改）=================
# K8s namespace
export SEED_NAMESPACE="${SEED_NAMESPACE:-seedemu-k3s-real-topo}"

# 镜像仓库
export SEED_REGISTRY="${SEED_REGISTRY:-${SEED_K3S_MASTER_IP}:5000}"
export SEED_REGISTRY_HOST="${SEED_REGISTRY_HOST:-${SEED_K3S_MASTER_IP}}"
export SEED_REGISTRY_PORT="${SEED_REGISTRY_PORT:-5000}"

# CNI 配置
export SEED_CNI_TYPE="${SEED_CNI_TYPE:-macvlan}"
export SEED_CNI_MASTER_INTERFACE="${SEED_CNI_MASTER_INTERFACE:-ens2}"

# 调度配置
export SEED_SCHEDULING_STRATEGY="${SEED_SCHEDULING_STRATEGY:-by_as_hard}"
export SEED_PLACEMENT_MODE="${SEED_PLACEMENT_MODE:-by_as_hard}"

# 镜像配置
export SEED_IMAGE_PULL_POLICY="${SEED_IMAGE_PULL_POLICY:-IfNotPresent}" #IfNotPresent,Always
export SEED_IMAGE_DISTRIBUTION_MODE="${SEED_IMAGE_DISTRIBUTION_MODE:-preload}"

# 构建配置
export SEED_BUILD_PARALLELISM="${SEED_BUILD_PARALLELISM:-8}"
export SEED_BUILD_BATCH_SIZE="${SEED_BUILD_BATCH_SIZE:-50}"
export SEED_DOCKER_BUILDKIT="${SEED_DOCKER_BUILDKIT:-1}"
export SEED_COMPOSE_DOCKER_CLI_BUILD="${SEED_COMPOSE_DOCKER_CLI_BUILD:-1}"
export SEED_PRELOAD_BATCH_SIZE="${SEED_PRELOAD_BATCH_SIZE:-10}"

# 大规模部署配置（deploy-batched）
export DEPLOY_BATCH_SIZE="${DEPLOY_BATCH_SIZE:-40}"
export DEPLOY_BATCH_SLEEP_SECONDS="${DEPLOY_BATCH_SLEEP_SECONDS:-20}"
export DEPLOY_MONITOR_INTERVAL="${DEPLOY_MONITOR_INTERVAL:-20}"
export DEPLOY_WARMUP_BATCHES="${DEPLOY_WARMUP_BATCHES:-3}"
export DEPLOY_WARMUP_BATCH_SIZE="${DEPLOY_WARMUP_BATCH_SIZE:-10}"
export DEPLOY_PRESSURE_CHECK_SECONDS="${DEPLOY_PRESSURE_CHECK_SECONDS:-15}"
export DEPLOY_STABILIZE_TIMEOUT_SECONDS="${DEPLOY_STABILIZE_TIMEOUT_SECONDS:-3600}"
export DEPLOY_MAX_PENDING_PODS="${DEPLOY_MAX_PENDING_PODS:-200}"
export DEPLOY_MAX_CREATING_PODS="${DEPLOY_MAX_CREATING_PODS:-300}"
export DEPLOY_MAX_NOTREADY_PODS="${DEPLOY_MAX_NOTREADY_PODS:-600}"
export DEPLOY_MAX_FAILED_PODS="${DEPLOY_MAX_FAILED_PODS:-10}"

# KVM / 基础镜像配置
export SEED_KVM_UBUNTU_SERIES="${SEED_KVM_UBUNTU_SERIES:-jammy}"
export SEED_KVM_BASE_IMAGE_URL="${SEED_KVM_BASE_IMAGE_URL:-https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img}"
export SEED_KVM_BASE_IMAGE_PATH="${SEED_KVM_BASE_IMAGE_PATH:-${HOME}/k8s/output/kvm_lab/base/jammy-server-cloudimg-amd64.img}"

# 输出目录
export OUTPUT_DIR="${OUTPUT_DIR:-${HOME}/k8s/lxl/output}"

# 日志目录（实验批次文件夹）
export LOG_BASE_DIR="${LOG_BASE_DIR:-${HOME}/k8s/lxl/logs}"

# 部署配置
export CLEAN_NAMESPACE="${CLEAN_NAMESPACE:-true}"

# 创建实验批次目录（时间戳+topology_size）
# 如果未设置，则创建新目录；否则使用已有目录（用于续跑）
create_experiment_dir() {
    if [ -n "${EXPERIMENT_DIR:-}" ] && [ -d "${EXPERIMENT_DIR}" ]; then
        echo "Using existing experiment directory: ${EXPERIMENT_DIR}"
        export EXPERIMENT_DIR="$(cd "${EXPERIMENT_DIR}" && pwd)"
        mkdir -p "${EXPERIMENT_DIR}"
        return 0
    fi

    local timestamp
    timestamp="$(date +%Y%m%d_%H%M%S)"
    export EXPERIMENT_DIR="${LOG_BASE_DIR}/${timestamp}_${SEED_TOPOLOGY_SIZE}"
    mkdir -p "${EXPERIMENT_DIR}"
    echo "Created experiment directory: ${EXPERIMENT_DIR}"

    echo "# Experiment: ${timestamp}_${SEED_TOPOLOGY_SIZE}" > "${EXPERIMENT_DIR}/timings.txt"
    echo "# Created: $(date)" >> "${EXPERIMENT_DIR}/timings.txt"
    echo "" >> "${EXPERIMENT_DIR}/timings.txt"
}

# 记录脚本开始时间
log_start() {
    local script_name="$1"
    echo "[$(date +%H:%M:%S)] ${script_name} started" >> "${EXPERIMENT_DIR}/timings.txt"
}

# 记录脚本结束时间
log_end() {
    local script_name="$1"
    echo "[$(date +%H:%M:%S)] ${script_name} completed" >> "${EXPERIMENT_DIR}/timings.txt"
}

# 打印当前配置
echo "=== SEED Emulator Environment (9-node) ==="
echo "SEED_K3S_MASTER_IP: ${SEED_K3S_MASTER_IP}"
echo "SEED_K3S_WORKER1_IP: ${SEED_K3S_WORKER1_IP}"
echo "SEED_K3S_WORKER2_IP: ${SEED_K3S_WORKER2_IP}"
echo "SEED_K3S_WORKER3_IP: ${SEED_K3S_WORKER3_IP}"
echo "SEED_K3S_WORKER4_IP: ${SEED_K3S_WORKER4_IP}"
echo "SEED_K3S_WORKER5_IP: ${SEED_K3S_WORKER5_IP}"
echo "SEED_K3S_WORKER6_IP: ${SEED_K3S_WORKER6_IP}"
echo "SEED_K3S_WORKER7_IP: ${SEED_K3S_WORKER7_IP}"
echo "SEED_K3S_WORKER8_IP: ${SEED_K3S_WORKER8_IP}"
echo "SEED_K3S_ALL_NODE_IPS: ${SEED_K3S_ALL_NODE_IPS}"
echo "SEED_K3S_USER: ${SEED_K3S_USER}"
echo "SEED_K3S_SSH_KEY: ${SEED_K3S_SSH_KEY}"
echo "SEED_K3S_CLUSTER_NAME: ${SEED_K3S_CLUSTER_NAME}"
echo "SEED_CLUSTER_INVENTORY: ${SEED_CLUSTER_INVENTORY}"
echo "SEED_CLUSTER_INVENTORY_PATH: ${SEED_CLUSTER_INVENTORY_PATH}"
echo "SEED_TOPOLOGY_SIZE: ${SEED_TOPOLOGY_SIZE}"
echo "SEED_REAL_TOPOLOGY_DIR: ${SEED_REAL_TOPOLOGY_DIR}"
echo "SEED_NAMESPACE: ${SEED_NAMESPACE}"
echo "SEED_REGISTRY: ${SEED_REGISTRY}"
echo "OUTPUT_DIR: ${OUTPUT_DIR}"
echo "LOG_BASE_DIR: ${LOG_BASE_DIR}"
if [ -n "${EXPERIMENT_DIR:-}" ]; then
    echo "EXPERIMENT_DIR: ${EXPERIMENT_DIR} (续跑模式)"
else
    echo "EXPERIMENT_DIR: <自动创建新目录>"
fi
echo "========================================="
echo ""
if [ -d "${LOG_BASE_DIR}" ]; then
    echo "已有实验目录:"
    ls -1 "${LOG_BASE_DIR}" 2>/dev/null | sed 's/^/  /'
    echo ""
fi
echo "如需续跑之前的实验，在 source env_9node.sh 前执行:"
echo "  export EXPERIMENT_DIR=\${LOG_BASE_DIR}/<时间戳>_<topology_size>"
echo ""
