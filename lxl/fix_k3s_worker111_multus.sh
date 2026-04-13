#!/usr/bin/env bash
set -Eeuo pipefail

MASTER_IP="${MASTER_IP:-192.168.122.110}"
WORKER_IP="${WORKER_IP:-192.168.122.111}"
DONOR_IP="${DONOR_IP:-192.168.122.112}"
SSH_USER="${SSH_USER:-ubuntu}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
PAUSE_IMAGE="${PAUSE_IMAGE:-docker.io/rancher/mirrored-pause:3.6}"
PAUSE_TAR_LOCAL="${PAUSE_TAR_LOCAL:-$HOME/pause-3.6.tar}"
PAUSE_TAR_REMOTE="${PAUSE_TAR_REMOTE:-/tmp/pause-3.6.tar}"
FLANNEL_LOCAL="${FLANNEL_LOCAL:-$HOME/10-flannel.conflist}"

STABLE_SECONDS="${STABLE_SECONDS:-30}"
CHECK_INTERVAL="${CHECK_INTERVAL:-5}"

SSH_OPTS=(-i "$SSH_KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null)

log() {
  echo
  echo "==== $* ===="
}

run_ssh() {
  local host="$1"
  shift
  ssh "${SSH_OPTS[@]}" "${SSH_USER}@${host}" "$@"
}

scp_from() {
  local host="$1" remote="$2" localf="$3"
  scp "${SSH_OPTS[@]}" "${SSH_USER}@${host}:${remote}" "${localf}"
}

scp_to() {
  local localf="$1" host="$2" remote="$3"
  scp "${SSH_OPTS[@]}" "${localf}" "${SSH_USER}@${host}:${remote}"
}

find_flannel_on_node() {
  local host="$1"
  run_ssh "$host" '
set +e
for f in \
  /etc/cni/net.d/10-flannel.conflist \
  /var/lib/rancher/k3s/agent/etc/cni/net.d/10-flannel.conflist
do
  if [[ -f "$f" ]]; then
    echo "$f"
    exit 0
  fi
done
find /etc/cni/net.d /var/lib/rancher/k3s/agent/etc/cni/net.d -maxdepth 2 -type f 2>/dev/null | grep flannel | head -n1
'
}

log "1. 在 master 上导出 pause 镜像"
run_ssh "$MASTER_IP" "sudo rm -f '$PAUSE_TAR_REMOTE'; sudo k3s ctr -n k8s.io images export '$PAUSE_TAR_REMOTE' '$PAUSE_IMAGE'; ls -lh '$PAUSE_TAR_REMOTE'"

log "2. 拉到宿主机，再传到 111"
rm -f "$PAUSE_TAR_LOCAL"
scp_from "$MASTER_IP" "$PAUSE_TAR_REMOTE" "$PAUSE_TAR_LOCAL"
ls -lh "$PAUSE_TAR_LOCAL"
scp_to "$PAUSE_TAR_LOCAL" "$WORKER_IP" "$PAUSE_TAR_REMOTE"

log "3. 在 111 上导入 pause 镜像"
run_ssh "$WORKER_IP" "sudo k3s ctr -n k8s.io images import '$PAUSE_TAR_REMOTE' || true; sudo k3s crictl images | egrep 'pause|mirrored-pause'"

log "4. 从健康节点提取 flannel 主 CNI 配置"
DONOR_FILE="$(find_flannel_on_node "$DONOR_IP" | tail -n1 || true)"
if [[ -z "${DONOR_FILE}" ]]; then
  DONOR_FILE="$(find_flannel_on_node "$MASTER_IP" | tail -n1 || true)"
  DONOR_USE="$MASTER_IP"
else
  DONOR_USE="$DONOR_IP"
fi

if [[ -z "${DONOR_FILE}" ]]; then
  echo "未找到 flannel 主 CNI 配置"
  exit 1
fi

echo "使用 donor 节点 ${DONOR_USE} 的文件: ${DONOR_FILE}"

rm -f "$FLANNEL_LOCAL"
scp_from "$DONOR_USE" "$DONOR_FILE" "$FLANNEL_LOCAL"
ls -lh "$FLANNEL_LOCAL"

log "5. 推送 flannel 配置到 111"
scp_to "$FLANNEL_LOCAL" "$WORKER_IP" /tmp/10-flannel.conflist
run_ssh "$WORKER_IP" '
set -Eeuo pipefail
sudo mkdir -p /etc/cni/net.d
sudo cp -f /tmp/10-flannel.conflist /etc/cni/net.d/10-flannel.conflist
sudo chown root:root /etc/cni/net.d/10-flannel.conflist
sudo chmod 644 /etc/cni/net.d/10-flannel.conflist
echo "111 当前 /etc/cni/net.d:"
sudo find /etc/cni/net.d -maxdepth 2 -type f | sort
'

log "6. 重启 111 的 k3s-agent"
run_ssh "$WORKER_IP" "sudo systemctl restart k3s-agent; sleep 5; sudo systemctl is-active k3s-agent"

log "7. 删除 111 上 multus pod，让它重建"
MULTUS_POD="$(kubectl get pods -n kube-system -o wide | awk '/kube-multus-ds/ && /'"$WORKER_IP"'/ {print $1; exit}')"
if [[ -n "${MULTUS_POD}" ]]; then
  kubectl delete pod -n kube-system "$MULTUS_POD" --wait=false
fi

log "8. 观察 multus 是否稳定"
NEEDED_OK_COUNT=$((STABLE_SECONDS / CHECK_INTERVAL))
OK_COUNT=0

for _ in $(seq 1 36); do
  sleep "$CHECK_INTERVAL"
  kubectl get pods -n kube-system -o wide | grep kube-multus-ds || true

  STATUS="$(kubectl get pods -n kube-system -o wide | awk '/kube-multus-ds/ && /'"$WORKER_IP"'/ {print $3; exit}')"
  READY="$(kubectl get pods -n kube-system -o wide | awk '/kube-multus-ds/ && /'"$WORKER_IP"'/ {print $2; exit}')"

  if [[ "${STATUS:-}" == "Running" && "${READY:-}" == "1/1" ]]; then
    OK_COUNT=$((OK_COUNT + 1))
    echo "multus 连续健康次数: ${OK_COUNT}/${NEEDED_OK_COUNT}"
  else
    echo "multus 当前未稳定: status=${STATUS:-N/A}, ready=${READY:-N/A}"
    OK_COUNT=0
  fi

  if [[ "$OK_COUNT" -ge "$NEEDED_OK_COUNT" ]]; then
    echo "111 上 multus 已稳定 Running ${STABLE_SECONDS} 秒"
    break
  fi
done

if [[ "$OK_COUNT" -lt "$NEEDED_OK_COUNT" ]]; then
  echo "111 上 multus 未能在观察窗口内保持稳定"
  FINAL_POD="$(kubectl get pods -n kube-system -o wide | awk '/kube-multus-ds/ && /'"$WORKER_IP"'/ {print $1; exit}')"
  if [[ -n "${FINAL_POD}" ]]; then
    kubectl describe pod -n kube-system "$FINAL_POD" || true
    kubectl logs -n kube-system "$FINAL_POD" -c kube-multus --tail=80 || true
  fi
  exit 1
fi

log "9. 最终检查"
kubectl get nodes -o wide
kubectl get pods -n kube-system -o wide | grep kube-multus-ds || true
FINAL_POD="$(kubectl get pods -n kube-system -o wide | awk '/kube-multus-ds/ && /'"$WORKER_IP"'/ {print $1; exit}')"
if [[ -n "${FINAL_POD}" ]]; then
  kubectl logs -n kube-system "$FINAL_POD" -c kube-multus --tail=80 || true
fi