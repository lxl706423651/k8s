#!/usr/bin/env bash
set -Eeuo pipefail

SSH_USER="${SSH_USER:-ubuntu}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
NODE_IPS=("${@:-192.168.122.110 192.168.122.111 192.168.122.112}")

CHECK_INTERVAL="${CHECK_INTERVAL:-5}"     # 每次检查间隔秒数
STABLE_SECONDS="${STABLE_SECONDS:-30}"    # 连续观察窗口
SSH_OPTS=(-i "$SSH_KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null)

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "缺少命令: $1"
    exit 1
  }
}

need_cmd ssh
need_cmd kubectl

section() {
  echo
  echo "################################################################"
  echo "# $*"
  echo "################################################################"
}

run_ssh() {
  local host="$1"
  shift
  ssh "${SSH_OPTS[@]}" "${SSH_USER}@${host}" "$@"
}

get_multus_pod_by_ip() {
  local ip="$1"
  kubectl get pods -n kube-system -o wide | awk '/kube-multus-ds/ && /'"$ip"'/ {print $1; exit}'
}

get_node_name_by_ip() {
  local ip="$1"
  kubectl get nodes -o wide | awk 'NR>1 && $6=="'"$ip"'" {print $1; exit}'
}

print_quick_advice() {
  local ip="$1"
  local pod="$2"

  echo
  echo "---- 自动建议 (${ip}) ----"

  if [[ -n "$pod" ]]; then
    local logs
    logs="$(kubectl logs -n kube-system "$pod" -c kube-multus --tail=80 2>/dev/null || true)"

    if echo "$logs" | grep -q 'cannot find valid master CNI config'; then
      echo "发现: Multus 找不到主 CNI 配置。"
      echo "建议:"
      echo "  1. 检查 /etc/cni/net.d 是否存在 10-flannel.conflist"
      echo "  2. 检查 /var/lib/rancher/k3s/agent/etc/cni/net.d/10-flannel.conflist 是否存在"
      echo "  3. 若只存在 K3s 目录，复制到 /etc/cni/net.d/"
      echo "  4. 删除该节点上的 multus pod 让其重建"
      return
    fi

    if kubectl describe pod -n kube-system "$pod" 2>/dev/null | grep -q 'failed to get sandbox image "rancher/mirrored-pause:3.6"'; then
      echo "发现: pause 镜像缺失或 docker.io 拉取失败。"
      echo "建议:"
      echo "  1. 检查该节点 sudo k3s crictl images | grep pause"
      echo "  2. 从 master 导出 pause 镜像导入该节点"
      echo "  3. 后续给 docker.io 配私有镜像或 mirror"
      return
    fi
  fi

  echo "未识别到固定模式，请重点查看："
  echo "  1. kubectl describe pod -n kube-system <multus-pod>"
  echo "  2. kubectl logs -n kube-system <multus-pod> -c kube-multus --previous"
  echo "  3. 节点上的 /etc/cni/net.d 和 /var/lib/rancher/k3s/agent/etc/cni/net.d"
}

section "1. 集群概览"
kubectl get nodes -o wide
echo
kubectl get pods -n kube-system -o wide | grep kube-multus-ds || true

section "2. 连续观察 multus 是否稳定"
NEEDED_OK_COUNT=$((STABLE_SECONDS / CHECK_INTERVAL))

for ip in "${NODE_IPS[@]}"; do
  echo
  echo "================ Node ${ip} ================"
  node_name="$(get_node_name_by_ip "$ip")"
  pod="$(get_multus_pod_by_ip "$ip")"

  if [[ -z "$node_name" ]]; then
    echo "未在集群中找到 IP=${ip} 对应节点"
    continue
  fi

  echo "节点名: ${node_name}"
  echo "当前 multus pod: ${pod:-<none>}"

  OK_COUNT=0
  for _ in $(seq 1 36); do
    sleep "$CHECK_INTERVAL"
    pod="$(get_multus_pod_by_ip "$ip")"

    if [[ -z "$pod" ]]; then
      echo "multus pod 尚未出现"
      OK_COUNT=0
      continue
    fi

    READY="$(kubectl get pod -n kube-system "$pod" -o jsonpath='{.status.containerStatuses[0].ready}' 2>/dev/null || echo false)"
    PHASE="$(kubectl get pod -n kube-system "$pod" -o jsonpath='{.status.phase}' 2>/dev/null || echo Unknown)"
    RESTARTS="$(kubectl get pod -n kube-system "$pod" -o jsonpath='{.status.containerStatuses[0].restartCount}' 2>/dev/null || echo 0)"

    if [[ "$PHASE" == "Running" && "$READY" == "true" ]]; then
      OK_COUNT=$((OK_COUNT + 1))
      echo "稳定计数 ${OK_COUNT}/${NEEDED_OK_COUNT} | phase=${PHASE} ready=${READY} restarts=${RESTARTS}"
    else
      echo "未稳定 | phase=${PHASE} ready=${READY} restarts=${RESTARTS}"
      OK_COUNT=0
    fi

    if [[ "$OK_COUNT" -ge "$NEEDED_OK_COUNT" ]]; then
      echo "判定: ${ip} 上 multus 稳定正常 (${STABLE_SECONDS}s)"
      break
    fi
  done

  if [[ "$OK_COUNT" -lt "$NEEDED_OK_COUNT" ]]; then
    echo "判定: ${ip} 上 multus 不稳定或异常"
  fi
done

section "3. 集群侧详细信息"
for ip in "${NODE_IPS[@]}"; do
  echo
  echo "================ 集群视角 ${ip} ================"
  node_name="$(get_node_name_by_ip "$ip")"
  pod="$(get_multus_pod_by_ip "$ip")"

  echo "节点名: ${node_name:-<unknown>}"
  echo "multus pod: ${pod:-<none>}"

  if [[ -n "$node_name" ]]; then
    echo
    echo "[Node Conditions]"
    kubectl describe node "$node_name" | sed -n '/Conditions:/,/Addresses:/p' || true
  fi

  if [[ -n "$pod" ]]; then
    echo
    echo "[Pod Summary]"
    kubectl get pod -n kube-system "$pod" -o wide || true

    echo
    echo "[Describe Pod]"
    kubectl describe pod -n kube-system "$pod" | sed -n '1,220p' || true

    echo
    echo "[Current Logs]"
    kubectl logs -n kube-system "$pod" -c kube-multus --tail=80 || true

    echo
    echo "[Previous Logs]"
    kubectl logs -n kube-system "$pod" -c kube-multus --previous --tail=80 || true
  fi
done

section "4. 节点侧 SSH 检查"
for ip in "${NODE_IPS[@]}"; do
  echo
  echo "================ SSH ${ip} ================"
  run_ssh "$ip" '
set +e
echo "[hostname]"
hostname

echo
echo "[k3s-agent]"
sudo systemctl is-active k3s-agent
sudo systemctl is-enabled k3s-agent

echo
echo "[pause image]"
sudo k3s crictl images | egrep "pause|mirrored-pause" || true

echo
echo "[/etc/cni/net.d]"
sudo find /etc/cni/net.d -maxdepth 2 -type f 2>/dev/null | sort || true

echo
echo "[/var/lib/rancher/k3s/agent/etc/cni/net.d]"
sudo find /var/lib/rancher/k3s/agent/etc/cni/net.d -maxdepth 2 -type f 2>/dev/null | sort || true

echo
echo "[cni bin dirs]"
sudo ls -ld /opt/cni/bin /var/lib/rancher/k3s/data/current/bin 2>/dev/null || true

echo
echo "[recent k3s-agent log keywords]"
sudo journalctl -u k3s-agent -n 200 --no-pager 2>/dev/null | egrep -i "pause|sandbox image|multus|cni|flannel|failed to get sandbox image|cannot find valid master CNI config" || true

echo
echo "[quick flags]"
HAS_PAUSE=0
HAS_STD_FLANNEL=0
HAS_K3S_FLANNEL=0
sudo k3s crictl images | egrep -q "pause|mirrored-pause" && HAS_PAUSE=1
test -f /etc/cni/net.d/10-flannel.conflist && HAS_STD_FLANNEL=1
test -f /var/lib/rancher/k3s/agent/etc/cni/net.d/10-flannel.conflist && HAS_K3S_FLANNEL=1
echo "HAS_PAUSE=$HAS_PAUSE"
echo "HAS_STD_FLANNEL=$HAS_STD_FLANNEL"
echo "HAS_K3S_FLANNEL=$HAS_K3S_FLANNEL"

if [[ "$HAS_PAUSE" -eq 0 ]]; then
  echo "WARN: 缺少 pause 镜像"
fi
if [[ "$HAS_K3S_FLANNEL" -eq 1 && "$HAS_STD_FLANNEL" -eq 0 ]]; then
  echo "WARN: 只有 K3s CNI 目录有 flannel，Multus 若挂 /etc/cni/net.d 可能失败"
fi
'
done

section "5. 自动建议"
for ip in "${NODE_IPS[@]}"; do
  pod="$(get_multus_pod_by_ip "$ip")"
  print_quick_advice "$ip" "$pod"
done

section "完成"
