#!/usr/bin/env bash
###############################################################################
# env.sh - 环境变量配置
# 用法: source env.sh
###############################################################################

# ============ 集群配置（必须修改为你实际的配置）============
# K3s 集群节点 IP
export SEED_K3S_MASTER_IP="${SEED_K3S_MASTER_IP:-192.168.122.110}"
export SEED_K3S_WORKER1_IP="${SEED_K3S_WORKER1_IP:-192.168.122.111}"
export SEED_K3S_WORKER2_IP="${SEED_K3S_WORKER2_IP:-192.168.122.112}"

# SSH 配置
export SEED_K3S_USER="${SEED_K3S_USER:-ubuntu}"
export SEED_K3S_SSH_KEY="${SEED_K3S_SSH_KEY:-$HOME/.ssh/id_ed25519}"

# 集群名称（用于定位 kubeconfig）
export SEED_K3S_CLUSTER_NAME="${SEED_K3S_CLUSTER_NAME:-seedemu-k3s}"

# ============ 拓扑配置（必须修改）=======================
# 拓扑规模
export SEED_TOPOLOGY_SIZE="${SEED_TOPOLOGY_SIZE:-1897}"
# 拓扑文件目录
export SEED_REAL_TOPOLOGY_DIR="${SEED_REAL_TOPOLOGY_DIR:-$HOME/lxl_topology/autocoder_test}"

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
export SEED_IMAGE_PULL_POLICY="${SEED_IMAGE_PULL_POLICY:-IfNotPresent}"
export SEED_IMAGE_DISTRIBUTION_MODE="${SEED_IMAGE_DISTRIBUTION_MODE:-preload}"

# 构建配置 (优化版)
export SEED_BUILD_PARALLELISM="${SEED_BUILD_PARALLELISM:-4}"      # 并行构建任务数
export SEED_BUILD_BATCH_SIZE="${SEED_BUILD_BATCH_SIZE:-50}"         # 每批构建的服务数
export SEED_DOCKER_BUILDKIT="${SEED_DOCKER_BUILDKIT:-1}"           # 启用 BuildKit
export SEED_COMPOSE_DOCKER_CLI_BUILD="${SEED_COMPOSE_DOCKER_CLI_BUILD:-1}"  # Docker Compose CLI 构建模式

# 输出目录
export OUTPUT_DIR="${OUTPUT_DIR:-${HOME}/seed-emulator-k8s/output/k8s}"

# 日志目录（实验批次文件夹）
export LOG_BASE_DIR="${LOG_BASE_DIR:-${HOME}/seed-emulator-k8s/lxl/logs}"

# 部署配置
export CLEAN_NAMESPACE="${CLEAN_NAMESPACE:-true}"

# 创建实验批次目录（时间戳+topology_size）
# 如果未设置，则创建新目录；否则使用已有目录（用于续跑）
create_experiment_dir() {
    if [ -n "${EXPERIMENT_DIR:-}" ] && [ -d "${EXPERIMENT_DIR}" ]; then
        echo "Using existing experiment directory: ${EXPERIMENT_DIR}"
        # 转换为绝对路径
        export EXPERIMENT_DIR="$(cd "${EXPERIMENT_DIR}" && pwd)"
        mkdir -p "${EXPERIMENT_DIR}"
        return 0
    fi
    
    local timestamp
    timestamp="$(date +%Y%m%d_%H%M%S)"
    export EXPERIMENT_DIR="${LOG_BASE_DIR}/${timestamp}_${SEED_TOPOLOGY_SIZE}"
    mkdir -p "${EXPERIMENT_DIR}"
    echo "Created experiment directory: ${EXPERIMENT_DIR}"
    
    # 创建 timings.txt 记录脚本执行时间
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
echo "=== SEED Emulator Environment ==="
echo "SEED_K3S_MASTER_IP: ${SEED_K3S_MASTER_IP}"
echo "SEED_K3S_WORKER1_IP: ${SEED_K3S_WORKER1_IP}"
echo "SEED_K3S_WORKER2_IP: ${SEED_K3S_WORKER2_IP}"
echo "SEED_K3S_USER: ${SEED_K3S_USER}"
echo "SEED_K3S_SSH_KEY: ${SEED_K3S_SSH_KEY}"
echo "SEED_K3S_CLUSTER_NAME: ${SEED_K3S_CLUSTER_NAME}"
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
echo "================================"
echo ""
if [ -d "${LOG_BASE_DIR}" ]; then
    echo "已有实验目录:"
    ls -1 "${LOG_BASE_DIR}" 2>/dev/null | sed 's/^/  /'
    echo ""
fi
echo "如需续跑之前的实验，在 source env.sh 前执行:"
echo "  export EXPERIMENT_DIR=\${LOG_BASE_DIR}/<时间戳>_<topology_size>"
echo ""