#!/bin/bash

# ================= 配置区 =================
SSH_KEY="~/.ssh/id_ed25519"
SSH_USER="ubuntu"
SSH_OPTS="-o StrictHostKeyChecking=no"

MASTER_IP="192.168.122.110"
MASTER_NAME="seed-k3s-master"

# 格式: "IP:节点名称"
WORKERS=(
  "192.168.122.111:seed-k3s-worker1"
  "192.168.122.112:seed-k3s-worker2"
)
# ==========================================

# 封装 SSH 执行命令的函数
run_ssh() {
  local ip=$1
  local cmd=$2
  ssh -i $SSH_KEY $SSH_OPTS ${SSH_USER}@${ip} "$cmd"
}

echo ">>> [步骤 1/4] 更新 Master 节点 (${MASTER_IP}) 的 K3s 配置，设置 node-cidr-mask-size=20..."
run_ssh $MASTER_IP "sudo mkdir -p /etc/rancher/k3s && grep -q 'node-cidr-mask-size=20' /etc/rancher/k3s/config.yaml || echo -e 'kube-controller-manager-arg:\n  - \"node-cidr-mask-size=20\"' | sudo tee -a /etc/rancher/k3s/config.yaml"


echo ">>> [步骤 2/4] 开始重建 Worker 节点..."
for W in "${WORKERS[@]}"; do
  W_IP="${W%%:*}"
  W_NAME="${W##*:}"
  
  echo "--------------------------------------------------"
  echo "正在处理 Worker: $W_NAME ($W_IP)"
  
  echo "  - 驱逐节点上的 Pod (超时 60s)..."
  run_ssh $MASTER_IP "sudo k3s kubectl drain $W_NAME --ignore-daemonsets --delete-emptydir-data --force --timeout=60s || true"
  
  echo "  - 停止 Worker 上的 k3s-agent 服务..."
  run_ssh $W_IP "sudo systemctl stop k3s-agent"
  
  echo "  - 在 Master 节点上删除 Node 对象..."
  run_ssh $MASTER_IP "sudo k3s kubectl delete node $W_NAME"
  
  echo "  - 清理 Worker 的旧 CNI 目录和虚拟网卡..."
  run_ssh $W_IP "sudo rm -rf /var/lib/cni/ /etc/cni/net.d/ && sudo ip link delete cni0 2>/dev/null || true && sudo ip link delete flannel.1 2>/dev/null || true"
  
  echo "  - 重启 Worker 上的 k3s-agent 服务并重新注册..."
  run_ssh $W_IP "sudo systemctl start k3s-agent"
  
  echo "  - 等待 10 秒让节点注册..."
  sleep 10
done


echo "--------------------------------------------------"
echo ">>> [步骤 3/4] 开始重建 Master 节点: $MASTER_NAME ($MASTER_IP)..."

echo "  - 驱逐 Master 节点上的 Pod (超时 60s)..."
run_ssh $MASTER_IP "sudo k3s kubectl drain $MASTER_NAME --ignore-daemonsets --delete-emptydir-data --force --timeout=60s || true"

echo "  - 删除 Master 的 Node 对象..."
run_ssh $MASTER_IP "sudo k3s kubectl delete node $MASTER_NAME"

echo "  - 停止 Master 上的 K3s 服务..."
run_ssh $MASTER_IP "sudo systemctl stop k3s"

echo "  - 清理 Master 的旧 CNI 目录和虚拟网卡..."
run_ssh $MASTER_IP "sudo rm -rf /var/lib/cni/ /etc/cni/net.d/ && sudo ip link delete cni0 2>/dev/null || true && sudo ip link delete flannel.1 2>/dev/null || true"

echo "  - 重启 K3s 服务，应用新的 CIDR 掩码..."
run_ssh $MASTER_IP "sudo systemctl start k3s"


echo ">>> [步骤 4/4] 正在等待所有节点恢复并验证结果 (20秒)..."
sleep 20

echo ">>> 当前集群的 Node 状态及 podCIDR:"
run_ssh $MASTER_IP "sudo k3s kubectl get node -o jsonpath='{range .items[*]}{.metadata.name}{\"  podCIDR=\"}{.spec.podCIDR}{\"  Status=\"}{range .status.conditions[?(@.type==\"Ready\")]}{.status}{\"\n\"}{end}{end}'"

echo "=================================================="
echo "操作完成！如果不全为 Ready，请稍等片刻后手动在 master 节点执行 kubectl get node 检查状态。"
