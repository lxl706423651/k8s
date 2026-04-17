#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/env_9node.sh"
source "${SCRIPT_DIR}/01_cluster_nodes_9node.sh"

SEED_CNI0_HASH_MAX="${SEED_CNI0_HASH_MAX:-16384}"
SEED_USER_MAX_NET_NAMESPACES="${SEED_USER_MAX_NET_NAMESPACES:-65536}"
SEED_OPTMEM_MAX="${SEED_OPTMEM_MAX:-25165824}"
SEED_KUBELET_MAX_PARALLEL_IMAGE_PULLS="${SEED_KUBELET_MAX_PARALLEL_IMAGE_PULLS:-4}"
SEED_OPTIMIZE_RESTART_K3S="${SEED_OPTIMIZE_RESTART_K3S:-true}"

SSH_OPTS=(
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
  -o LogLevel=ERROR
  -o BatchMode=yes
  -o IdentitiesOnly=yes
  -o IdentityAgent=none
  -o ConnectTimeout=10
  -o ServerAliveInterval=30
  -o ServerAliveCountMax=3
  -i "${SEED_K3S_SSH_KEY}"
)

apply_node_tuning() {
  local node_name="$1"
  local node_ip="$2"

  echo "Applying network tuning on ${node_name} (${node_ip})..."

  # 巧妙利用 bash -s 传参，将本地变量安全传入远端，彻底避开令人崩溃的各种反斜杠转义！
  ssh "${SSH_OPTS[@]}" "${SEED_K3S_USER}@${node_ip}" "sudo -n bash -s" \
    "${SEED_USER_MAX_NET_NAMESPACES}" \
    "${SEED_OPTMEM_MAX}" \
    "${SEED_CNI0_HASH_MAX}" \
    "${SEED_KUBELET_MAX_PARALLEL_IMAGE_PULLS}" \
    "${SEED_OPTIMIZE_RESTART_K3S}" << 'EOF_SSH'
set -euo pipefail

# 接收本地传过来的纯净变量
MAX_NET_NS="$1"
MAX_OPTMEM="$2"
HASH_MAX="$3"
MAX_PULLS="$4"
DO_RESTART="$5"

# 1. 写入 Sysctl (由于 EOF_SYSCTL 没有引号，远端 bash 会自动替换 ${MAX_NET_NS})
cat <<-EOF_SYSCTL > /etc/sysctl.d/99-seed-k8s-network.conf
user.max_net_namespaces = ${MAX_NET_NS}
net.core.optmem_max = ${MAX_OPTMEM}
EOF_SYSCTL

sysctl --system >/dev/null 2>&1 || true

# 2. 写入调整 hash_max 的后台脚本 (EOF_TUNE 有单引号，保持原样写入文件)
cat <<-'EOF_TUNE' > /usr/local/sbin/seed-k8s-network-tune.sh
#!/usr/bin/env bash
set -euo pipefail
TARGET_HASH_MAX="$1"  # 接收 systemd service 传来的参数
for _ in $(seq 1 60); do
    if [ -w /sys/class/net/cni0/bridge/hash_max ]; then
        printf "%s\n" "${TARGET_HASH_MAX}" > /sys/class/net/cni0/bridge/hash_max
        exit 0
    fi
    sleep 2
done
exit 0
EOF_TUNE
chmod +x /usr/local/sbin/seed-k8s-network-tune.sh

# 3. 配置 Systemd 开机服务
cat <<-EOF_UNIT > /etc/systemd/system/seed-k8s-network-tune.service
[Unit]
Description=Apply Seed K8s bridge/network tuning after K3s startup
After=network-online.target k3s.service k3s-agent.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/seed-k8s-network-tune.sh ${HASH_MAX}
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF_UNIT

# 4. 配置 k3s config.yaml
mkdir -p /etc/rancher/k3s
touch /etc/rancher/k3s/config.yaml

cfg=/etc/rancher/k3s/config.yaml
tmp=$(mktemp)
found=0
in_kubelet=0

while IFS= read -r line || [ -n "$line" ]; do
  if [ "${in_kubelet}" -eq 1 ]; then
    if [[ "$line" =~ ^[^[:space:]] ]]; then
      printf "%s\n" "  - \"max-parallel-image-pulls=${MAX_PULLS}\"" >> "${tmp}"
      in_kubelet=0
      printf "%s\n" "$line" >> "${tmp}"
      continue
    fi
    if [[ "$line" == *max-parallel-image-pulls=* ]]; then
      continue
    fi
    printf "%s\n" "$line" >> "${tmp}"
    continue
  fi

  if [[ "$line" == "kubelet-arg:" ]]; then
    found=1
    in_kubelet=1
    printf "%s\n" "$line" >> "${tmp}"
    continue
  fi

  printf "%s\n" "$line" >> "${tmp}"
done < "${cfg}"

if [ "${in_kubelet}" -eq 1 ]; then
  printf "%s\n" "  - \"max-parallel-image-pulls=${MAX_PULLS}\"" >> "${tmp}"
fi

if [ "${found}" -eq 0 ]; then
  printf "%s\n" "kubelet-arg:" >> "${tmp}"
  printf "%s\n" "  - \"max-parallel-image-pulls=${MAX_PULLS}\"" >> "${tmp}"
fi

mv "${tmp}" "${cfg}"

systemctl daemon-reload
systemctl enable seed-k8s-network-tune.service >/dev/null 2>&1 || true
systemctl restart seed-k8s-network-tune.service >/dev/null 2>&1 || true

# 5. 核心修复：100% 防卡死的异步重启大法！
if [ "${DO_RESTART}" = "true" ]; then
  echo "  --> Scheduling async restart of K3s components..."
  # 利用 systemd-run 向系统托管一个 2 秒后执行的临时守护任务，当前 SSH 会立刻成功断开并返回，再也不受断网影响！
  systemd-run --on-active=2 /bin/bash -c "systemctl restart k3s 2>/dev/null || systemctl restart k3s-agent 2>/dev/null || true; systemctl restart containerd 2>/dev/null || true; systemctl restart seed-k8s-network-tune.service 2>/dev/null || true" >/dev/null 2>&1
fi

hash_val=$(cat /sys/class/net/cni0/bridge/hash_max 2>/dev/null || echo CNI0_MISSING)
echo "  --> OK! Current hash_max=${hash_val} on $(hostname)"
EOF_SSH
}

main() {
  seed_load_cluster_nodes
  local idx
  for idx in "${!SEED_NODE_NAMES[@]}"; do
    apply_node_tuning "${SEED_NODE_NAMES[$idx]}" "${SEED_NODE_IPS[$idx]}"
  done
  echo "Completed network tuning on ${#SEED_NODE_NAMES[@]} nodes."
}

main "$@"