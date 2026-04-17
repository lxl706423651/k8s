#!/bin/bash
set -euo pipefail

SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
SSH_USER="${SSH_USER:-ubuntu}"
SSH_OPTS="${SSH_OPTS:--o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null}"

MASTER_IP="${MASTER_IP:-192.168.122.110}"
MASTER_NAME="${MASTER_NAME:-seed-k3s-master}"
TARGET_MASK="${TARGET_MASK:-20}"
TARGET_SUFFIX="/${TARGET_MASK}"
SKIP_MASTER="${SKIP_MASTER:-false}"
SKIP_MASTER_CONFIG="${SKIP_MASTER_CONFIG:-false}"
SKIP_MASTER_REBUILD="${SKIP_MASTER_REBUILD:-false}"

WORKERS=()

append_worker() {
  local value="$1"
  [[ -n "${value}" ]] || return 0
  WORKERS+=("${value}")
}

append_worker "${WORKER1-192.168.122.111:seed-k3s-worker1}"
append_worker "${WORKER2-192.168.122.112:seed-k3s-worker2}"
append_worker "${WORKER3-192.168.122.113:seed-k3s-worker3}"
append_worker "${WORKER4-192.168.122.114:seed-k3s-worker4}"
append_worker "${WORKER5-192.168.122.115:seed-k3s-worker5}"
append_worker "${WORKER6-192.168.122.116:seed-k3s-worker6}"
append_worker "${WORKER7-192.168.122.117:seed-k3s-worker7}"
append_worker "${WORKER8-192.168.122.118:seed-k3s-worker8}"

run_ssh() {
  local ip="$1"
  local cmd="$2"
  ssh -i "$SSH_KEY" $SSH_OPTS "${SSH_USER}@${ip}" "$cmd"
}

wait_for_node_mask() {
  local node_name="$1"
  local expected_suffix="$2"

  echo "  - 等待节点 ${node_name} 重新 Ready，并拿到 ${expected_suffix} ..."
  local appeared="false"
  local attempt
  for attempt in $(seq 1 36); do
    if run_ssh "$MASTER_IP" "sudo k3s kubectl get node ${node_name} >/dev/null 2>&1"; then
      appeared="true"
      break
    fi
    sleep 5
  done

  if [[ "${appeared}" != "true" ]]; then
    echo "节点 ${node_name} 在等待时间内未重新出现在集群中" >&2
    return 1
  fi

  run_ssh "$MASTER_IP" "sudo k3s kubectl wait --for=condition=Ready node/${node_name} --timeout=180s >/dev/null"

  local current_cidr=""
  for attempt in $(seq 1 36); do
    current_cidr="$(run_ssh "$MASTER_IP" "sudo k3s kubectl get node ${node_name} -o jsonpath='{.spec.podCIDR}'" || true)"
    if [[ -n "${current_cidr}" && "${current_cidr}" == *"${expected_suffix}" ]]; then
      echo "    ${node_name}: ${current_cidr}"
      return 0
    fi
    sleep 5
  done

  echo "节点 ${node_name} 的 podCIDR 未收敛到 ${expected_suffix}，当前值: ${current_cidr:-<empty>}" >&2
  return 1
}

normalize_master_config() {
  echo ">>> [步骤 1/5] 规范化 Master K3s 配置，强制 node-cidr-mask-size-ipv4=${TARGET_MASK} ..."
  run_ssh "$MASTER_IP" "sudo bash -s -- '${TARGET_MASK}'" <<'EOF'
set -euo pipefail
target_mask="$1"
cfg="/etc/rancher/k3s/config.yaml"
tmp="$(mktemp)"

sudo mkdir -p /etc/rancher/k3s
sudo touch "${cfg}"
sudo cp "${cfg}" "${cfg}.bak.$(date +%Y%m%d%H%M%S)"

sudo awk '
  {
    if (skip) {
      if ($0 ~ /^[^[:space:]]/) {
        skip=0
      } else {
        next
      }
    }
    if ($0 ~ /^kube-controller-manager-arg:/) {
      skip=1
      next
    }
    print
  }
' "${cfg}" > "${tmp}"

cat >> "${tmp}" <<BLOCK
kube-controller-manager-arg:
  - "node-cidr-mask-size-ipv4=${target_mask}"
BLOCK

sudo mv "${tmp}" "${cfg}"
echo "[master config]"
sudo cat "${cfg}"
EOF
}

rebuild_master() {
  echo ">>> [步骤 2/5] 重建 Master 节点: ${MASTER_NAME} (${MASTER_IP}) ..."
  echo "  - 驱逐 Master 上的可驱逐 Pod ..."
  run_ssh "$MASTER_IP" "sudo k3s kubectl drain ${MASTER_NAME} --ignore-daemonsets --delete-emptydir-data --force --timeout=60s || true"

  echo "  - 删除 Master Node 对象 ..."
  run_ssh "$MASTER_IP" "sudo k3s kubectl delete node ${MASTER_NAME} --ignore-not-found"

  echo "  - 停止 k3s ..."
  run_ssh "$MASTER_IP" "sudo systemctl stop k3s"

  echo "  - 清理 Master 的旧 CNI 状态 ..."
  run_ssh "$MASTER_IP" "sudo rm -rf /var/lib/cni /etc/cni/net.d && sudo ip link delete cni0 2>/dev/null || true && sudo ip link delete flannel.1 2>/dev/null || true"

  echo "  - 启动 k3s ..."
  run_ssh "$MASTER_IP" "sudo systemctl start k3s"

  wait_for_node_mask "${MASTER_NAME}" "${TARGET_SUFFIX}"
}

rebuild_worker() {
  local worker_ip="$1"
  local worker_name="$2"

  echo "--------------------------------------------------"
  echo ">>> [步骤 3/5] 重建 Worker: ${worker_name} (${worker_ip}) ..."

  echo "  - 驱逐节点上的 Pod ..."
  run_ssh "$MASTER_IP" "sudo k3s kubectl drain ${worker_name} --ignore-daemonsets --delete-emptydir-data --force --timeout=60s || true"

  echo "  - 停止 k3s-agent ..."
  run_ssh "$worker_ip" "sudo systemctl stop k3s-agent"

  echo "  - 删除 Worker Node 对象 ..."
  run_ssh "$MASTER_IP" "sudo k3s kubectl delete node ${worker_name} --ignore-not-found"

  echo "  - 清理 Worker 的旧 CNI 状态 ..."
  run_ssh "$worker_ip" "sudo rm -rf /var/lib/cni /etc/cni/net.d && sudo ip link delete cni0 2>/dev/null || true && sudo ip link delete flannel.1 2>/dev/null || true"

  echo "  - 启动 k3s-agent ..."
  run_ssh "$worker_ip" "sudo systemctl start k3s-agent"

  wait_for_node_mask "${worker_name}" "${TARGET_SUFFIX}"
}

print_result() {
  echo "--------------------------------------------------"
  echo ">>> [步骤 4/5] 当前集群 Node 状态及 podCIDR:"
  run_ssh "$MASTER_IP" "sudo k3s kubectl get node -o jsonpath='{range .items[*]}{.metadata.name}{\"  podCIDR=\"}{.spec.podCIDR}{\"  Status=\"}{range .status.conditions[?(@.type==\"Ready\")]}{.status}{\"\n\"}{end}{end}'"
}

main() {
  if [[ "${SKIP_MASTER}" != "true" ]]; then
    if [[ "${SKIP_MASTER_CONFIG}" != "true" ]]; then
      normalize_master_config
    else
      echo ">>> 跳过 Master 配置更新 ..."
    fi

    if [[ "${SKIP_MASTER_REBUILD}" != "true" ]]; then
      rebuild_master
    else
      echo ">>> 跳过 Master 重建 ..."
    fi
  else
    echo ">>> 跳过 Master 配置和重建，仅处理指定 Worker ..."
  fi

  local worker
  for worker in "${WORKERS[@]}"; do
    [[ -n "${worker}" ]] || continue
    rebuild_worker "${worker%%:*}" "${worker##*:}"
  done

  echo ">>> [步骤 5/5] 校验所有节点都已收敛到 ${TARGET_SUFFIX} ..."
  print_result
  echo "=================================================="
  echo "操作完成。目标掩码: ${TARGET_SUFFIX}"
}

main "$@"
