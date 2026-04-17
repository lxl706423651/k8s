#!/usr/bin/env bash
set -euo pipefail

SSH_USER="${SSH_USER:-ubuntu}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
REBOOT_NODES="${REBOOT_NODES:-true}"

if [ -n "${TARGET_NODES:-}" ]; then
    IFS=' ' read -r -a NODES <<< "${TARGET_NODES}"
else
    NODES=("192.168.122.110" "192.168.122.111" "192.168.122.112")
fi

echo "====================================================================="
echo "🛠️ 正在生成 K8s 终极调优脚本 (Payload)..."
echo "====================================================================="

# ==================== 开始生成内部调优脚本 ====================
cat << 'EOF_TUNE' > seed_k8s_ultimate_tune.sh
#!/usr/bin/env bash
set -e

echo ">> 1. Uncapping OS-level limits (/etc/security/limits.conf)..."
LIMITS_FILE="/etc/security/limits.conf"
append_if_missing() {
    local pattern="$1"; local line="$2"
    if ! grep -qF "$pattern" "$LIMITS_FILE"; then echo "$line" | tee -a "$LIMITS_FILE" >/dev/null; fi
}
append_if_missing "* soft nofile"    "* soft    nofile          10485760"
append_if_missing "* hard nofile"    "* hard    nofile          10485760"
append_if_missing "* soft nproc"     "* soft    nproc           4194304"
append_if_missing "* hard nproc"     "* hard    nproc           4194304"
append_if_missing "root soft nofile" "root            soft    nofile          10485760"
append_if_missing "root hard nofile" "root            hard    nofile          10485760"

echo ">> 2. Uncapping Linux Kernel & Network capabilities..."
cat <<'EOF_SYSCTL' | tee /etc/sysctl.d/99-k8s-ultimate.conf > /dev/null
net.ipv4.neigh.default.gc_thresh1 = 1048576
net.ipv4.neigh.default.gc_thresh2 = 4194304
net.ipv4.neigh.default.gc_thresh3 = 8388608
fs.inotify.max_user_watches = 52428800
fs.inotify.max_user_instances = 5242880
kernel.pid_max = 4194304
kernel.threads-max = 4194304
net.core.somaxconn = 65535
net.core.netdev_max_backlog = 1000000
net.core.rmem_max = 134217728
net.core.wmem_max = 134217728
net.ipv4.tcp_rmem = 4096 87380 134217728
net.ipv4.tcp_wmem = 4096 65536 134217728
net.ipv4.ip_forward = 1
net.netfilter.nf_conntrack_max = 33554432
net.netfilter.nf_conntrack_buckets = 8388608
net.ipv4.ipfrag_high_thresh = 268435456
net.ipv4.ipfrag_low_thresh  = 134217728
EOF_SYSCTL

modprobe nf_conntrack || true
sysctl --system >/dev/null 2>&1 || true
ip -s -s neigh flush all >/dev/null 2>&1 || true

echo ">> 3. Uncapping Systemd limits for K3s & Containerd..."
for SERVICE in k3s k3s-agent containerd; do
    if systemctl list-unit-files | grep -q "^${SERVICE}.service"; then
        mkdir -p /etc/systemd/system/${SERVICE}.service.d
        cat <<EOF_SYSTEMD | tee /etc/systemd/system/${SERVICE}.service.d/override.conf >/dev/null
[Service]
LimitNOFILE=10485760
LimitNPROC=4194304
TasksMax=infinity
EOF_SYSTEMD
    fi
done
systemctl daemon-reload

echo ">> 4. Injecting K3s configuration (Smart Master/Worker Detection)..."
mkdir -p /etc/rancher/k3s
touch /etc/rancher/k3s/config.yaml

sed -i '/kubelet-arg:/,$d' /etc/rancher/k3s/config.yaml 2>/dev/null || true
sed -i '/kube-apiserver-arg:/,$d' /etc/rancher/k3s/config.yaml 2>/dev/null || true

cat <<EOF_K3S >> /etc/rancher/k3s/config.yaml
kubelet-arg:
  - "max-pods=4000"
  - "kube-api-qps=50"
  - "kube-api-burst=100"
  - "registry-qps=100"
EOF_K3S

if systemctl list-unit-files | grep -q "^k3s.service"; then
    echo "   [!] Master Node Detected. Injecting kube-apiserver-arg..."
    cat <<EOF_K3S_MASTER >> /etc/rancher/k3s/config.yaml
kube-apiserver-arg:
  - "max-requests-inflight=1000"
  - "max-mutating-requests-inflight=500"
EOF_K3S_MASTER
else
    echo "   [!] Worker Node Detected. Skipping Master-only arguments."
fi

echo ">> Node tuning completed successfully!"
EOF_TUNE
# ==================== 内部调优脚本生成完毕 ====================


chmod +x seed_k8s_ultimate_tune.sh

echo "✅ Payload 脚本生成成功！准备分发..."
echo ""

# ---------------------------------------------------------
# 自动分发与执行环节
# ---------------------------------------------------------
SSH_OPTS=(-i "${SSH_KEY}" -o StrictHostKeyChecking=no -o BatchMode=yes -o ConnectTimeout=10)

for ip in "${NODES[@]}"; do
    echo "====================================================================="
    echo "🚀 正在连接 Node: $ip ..."
    
    # 1. 传输脚本
    echo "   [1/3] 上传调优脚本..."
    scp "${SSH_OPTS[@]}" seed_k8s_ultimate_tune.sh "${SSH_USER}@${ip}:/tmp/"
    
    # 2. 远程执行调优脚本
    echo "   [2/3] 以 Root 权限执行深度调优..."
    ssh "${SSH_OPTS[@]}" "${SSH_USER}@${ip}" "sudo -n bash /tmp/seed_k8s_ultimate_tune.sh"
    
    # 3. 可选重启
    if [ "${REBOOT_NODES}" = "true" ]; then
        echo "   [3/3] 调优完毕，正在下发重启指令..."
        ssh "${SSH_OPTS[@]}" "${SSH_USER}@${ip}" "sudo -n reboot" || true
    else
        echo "   [3/3] 已完成调优，按 REBOOT_NODES=false 跳过重启。"
    fi
    
    echo "✅ Node $ip 处理完毕！"
done

echo "====================================================================="
if [ "${REBOOT_NODES}" = "true" ]; then
    echo "🎉 所有节点调优脚本分发完毕并已下发重启指令！"
else
    echo "🎉 所有节点调优脚本分发完毕，且未重启节点！"
fi
echo "====================================================================="

# 顺手清理一下宿主机的临时文件
rm -f seed_k8s_ultimate_tune.sh
