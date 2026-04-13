#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# Rebuild K3s cluster on 3 existing VMs from the host machine
# - 1 master: 192.168.122.110
# - 2 workers: 192.168.122.111, 192.168.122.112
#
# What it does:
# 1) Fix /etc/hosts on all nodes
# 2) Stop/uninstall old K3s
# 3) Clean old CNI/containerd/K3s state
# 4) Reinstall K3s with:
#      - cluster-cidr: 10.42.0.0/16
#      - service-cidr: 10.43.0.0/16
#      - node-cidr-mask-size-ipv4=20   (critical)
#      - max-pods=4000
#      - flannel-backend=host-gw
# 5) Rejoin workers
# 6) Verify:
#      - nodes Ready
#      - podCIDR is /20 on all nodes
#      - allocatable.pods is 4k
#      - kube-system pod status
#
# WARNING:
# - This REBUILDS the K3s cluster on the three VMs.
# - It does NOT recreate the VMs themselves.
# - It wipes old K3s state / workloads on the VMs.
###############################################################################

# ---------- User-adjustable ----------
SSH_USER="${SSH_USER:-ubuntu}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"

MASTER_IP="${MASTER_IP:-192.168.122.110}"
WORKER1_IP="${WORKER1_IP:-192.168.122.111}"
WORKER2_IP="${WORKER2_IP:-192.168.122.112}"

MASTER_HOSTNAME="${MASTER_HOSTNAME:-seed-k3s-master}"
WORKER1_HOSTNAME="${WORKER1_HOSTNAME:-seed-k3s-worker1}"
WORKER2_HOSTNAME="${WORKER2_HOSTNAME:-seed-k3s-worker2}"

K3S_VERSION="${K3S_VERSION:-}"   # optional, e.g. v1.32.2+k3s1 ; empty = latest install script default
K3S_CLUSTER_CIDR="${K3S_CLUSTER_CIDR:-10.42.0.0/16}"
K3S_SERVICE_CIDR="${K3S_SERVICE_CIDR:-10.43.0.0/16}"
K3S_NODE_CIDR_MASK_V4="${K3S_NODE_CIDR_MASK_V4:-20}"
K3S_MAX_PODS="${K3S_MAX_PODS:-4000}"
K3S_FLANNEL_BACKEND="${K3S_FLANNEL_BACKEND:-host-gw}"

# If you do not want Traefik / servicelb, set true
DISABLE_TRAEFIK="${DISABLE_TRAEFIK:-true}"
DISABLE_SERVICELB="${DISABLE_SERVICELB:-true}"

# Polling
SSH_WAIT_SECONDS="${SSH_WAIT_SECONDS:-300}"
NODE_READY_WAIT_SECONDS="${NODE_READY_WAIT_SECONDS:-300}"
# ------------------------------------

SSH_OPTS=(
  -i "${SSH_KEY}"
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
  -o LogLevel=ERROR
  -o ConnectTimeout=10
)

K3S_TOKEN="seedemu-$(date +%s)-$RANDOM-$RANDOM"

say() {
  echo
  echo "====================================================================="
  echo "$*"
  echo "====================================================================="
}

rssh() {
  local ip="$1"
  shift
  ssh "${SSH_OPTS[@]}" "${SSH_USER}@${ip}" "$@"
}

rscp() {
  local src="$1"
  local ip="$2"
  local dst="$3"
  scp "${SSH_OPTS[@]}" "$src" "${SSH_USER}@${ip}:$dst"
}

wait_ssh() {
  local ip="$1"
  local deadline=$(( $(date +%s) + SSH_WAIT_SECONDS ))
  while true; do
    if rssh "$ip" "echo ok" >/dev/null 2>&1; then
      echo "SSH OK: $ip"
      return 0
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
      echo "ERROR: SSH did not come back on $ip within ${SSH_WAIT_SECONDS}s"
      return 1
    fi
    sleep 5
  done
}

# 1 = hostname, 2 = node ip
prepare_node_script() {
  cat <<'EOS'
#!/usr/bin/env bash
set -euo pipefail

NODE_HOSTNAME="$1"
NODE_IP="$2"
MASTER_IP="$3"
WORKER1_IP="$4"
WORKER2_IP="$5"
MASTER_HOSTNAME="$6"
WORKER1_HOSTNAME="$7"
WORKER2_HOSTNAME="$8"
K3S_CLUSTER_CIDR="$9"
K3S_SERVICE_CIDR="${10}"
K3S_NODE_CIDR_MASK_V4="${11}"
K3S_MAX_PODS="${12}"
K3S_FLANNEL_BACKEND="${13}"
NODE_ROLE="${14}"
K3S_TOKEN="${15}"
K3S_VERSION="${16}"
DISABLE_TRAEFIK="${17}"
DISABLE_SERVICELB="${18}"

export DEBIAN_FRONTEND=noninteractive

echo "[prepare] hostname=${NODE_HOSTNAME} ip=${NODE_IP} role=${NODE_ROLE}"

sudo hostnamectl set-hostname "${NODE_HOSTNAME}" || true

# Rebuild /etc/hosts deterministically
sudo cp /etc/hosts /etc/hosts.bak.$(date +%s) || true
cat <<EOF_HOSTS | sudo tee /etc/hosts >/dev/null
127.0.0.1 localhost
127.0.1.1 ${NODE_HOSTNAME}

${MASTER_IP} ${MASTER_HOSTNAME}
${WORKER1_IP} ${WORKER1_HOSTNAME}
${WORKER2_IP} ${WORKER2_HOSTNAME}

# IPv6 defaults
::1 localhost ip6-localhost ip6-loopback
ff02::1 ip6-allnodes
ff02::2 ip6-allrouters
EOF_HOSTS

# Try to reduce DiskPressure before reinstall
sudo systemctl stop k3s 2>/dev/null || true
sudo systemctl stop k3s-agent 2>/dev/null || true

# Run uninstall scripts if present
sudo /usr/local/bin/k3s-uninstall.sh 2>/dev/null || true
sudo /usr/local/bin/k3s-agent-uninstall.sh 2>/dev/null || true

# Kill leftovers
sudo pkill -9 -f '/usr/local/bin/k3s' 2>/dev/null || true
sudo pkill -9 -f containerd 2>/dev/null || true
sudo pkill -9 -f kubelet 2>/dev/null || true

# Clean CNI / containerd / k3s state
sudo rm -rf /etc/cni/net.d/* || true
sudo rm -rf /var/lib/cni/* || true
sudo rm -rf /var/lib/rancher/k3s/* || true
sudo rm -rf /etc/rancher/k3s/* || true
sudo rm -rf /run/k3s/* || true
sudo rm -rf /var/lib/kubelet/* || true
sudo rm -rf /var/lib/containerd/* || true
sudo rm -rf /run/containerd/* || true

# Helpful for disk pressure
sudo journalctl --vacuum-time=2d >/dev/null 2>&1 || true
sudo rm -rf /var/log/pods/* /var/log/containers/* 2>/dev/null || true

sudo mkdir -p /etc/rancher/k3s

if [ "${NODE_ROLE}" = "server" ]; then
  {
    echo "cluster-cidr: ${K3S_CLUSTER_CIDR}"
    echo "service-cidr: ${K3S_SERVICE_CIDR}"
    echo "token: ${K3S_TOKEN}"
    echo "node-ip: ${NODE_IP}"
    echo "flannel-backend: ${K3S_FLANNEL_BACKEND}"
    echo "write-kubeconfig-mode: \"0644\""
    echo "kubelet-arg:"
    echo "  - \"max-pods=${K3S_MAX_PODS}\""
    echo "  - \"kube-api-qps=50\""
    echo "  - \"kube-api-burst=100\""
    echo "  - \"registry-qps=100\""
    echo "kube-controller-manager-arg:"
    echo "  - \"node-cidr-mask-size-ipv4=${K3S_NODE_CIDR_MASK_V4}\""
    echo "kube-apiserver-arg:"
    echo "  - \"max-requests-inflight=1000\""
    echo "  - \"max-mutating-requests-inflight=500\""
    if [ "${DISABLE_TRAEFIK}" = "true" ]; then
      echo "disable:"
      echo "  - traefik"
      if [ "${DISABLE_SERVICELB}" = "true" ]; then
        echo "  - servicelb"
      fi
    elif [ "${DISABLE_SERVICELB}" = "true" ]; then
      echo "disable:"
      echo "  - servicelb"
    fi
  } | sudo tee /etc/rancher/k3s/config.yaml >/dev/null
else
  {
    echo "server: https://${MASTER_IP}:6443"
    echo "token: ${K3S_TOKEN}"
    echo "node-ip: ${NODE_IP}"
    echo "kubelet-arg:"
    echo "  - \"max-pods=${K3S_MAX_PODS}\""
    echo "  - \"kube-api-qps=50\""
    echo "  - \"kube-api-burst=100\""
    echo "  - \"registry-qps=100\""
  } | sudo tee /etc/rancher/k3s/config.yaml >/dev/null
fi

echo "[prepare] /etc/rancher/k3s/config.yaml written:"
sudo cat /etc/rancher/k3s/config.yaml

# Install or reinstall K3s
INSTALL_ENV=""
if [ -n "${K3S_VERSION}" ]; then
  INSTALL_ENV="INSTALL_K3S_VERSION=${K3S_VERSION}"
fi

if [ "${NODE_ROLE}" = "server" ]; then
  echo "[install] installing k3s server..."
  if [ -n "${INSTALL_ENV}" ]; then
    sudo env ${INSTALL_ENV} sh -c 'curl -sfL https://get.k3s.io | sh -'
  else
    sudo sh -c 'curl -sfL https://get.k3s.io | sh -'
  fi
else
  echo "[install] installing k3s agent..."
  if [ -n "${INSTALL_ENV}" ]; then
    sudo env ${INSTALL_ENV} K3S_URL="https://${MASTER_IP}:6443" K3S_TOKEN="${K3S_TOKEN}" sh -c 'curl -sfL https://get.k3s.io | sh -'
  else
    sudo env K3S_URL="https://${MASTER_IP}:6443" K3S_TOKEN="${K3S_TOKEN}" sh -c 'curl -sfL https://get.k3s.io | sh -'
  fi
fi

sleep 2
sudo systemctl enable k3s 2>/dev/null || true
sudo systemctl enable k3s-agent 2>/dev/null || true
sudo systemctl restart k3s 2>/dev/null || true
sudo systemctl restart k3s-agent 2>/dev/null || true

echo "[prepare] done on ${NODE_HOSTNAME}"
EOS
}

say "0) Preflight: verify SSH reachability"
for ip in "${MASTER_IP}" "${WORKER1_IP}" "${WORKER2_IP}"; do
  wait_ssh "$ip"
done

say "1) Upload per-node rebuild script"
tmp_prepare="$(mktemp)"
prepare_node_script > "${tmp_prepare}"
chmod +x "${tmp_prepare}"

for ip in "${MASTER_IP}" "${WORKER1_IP}" "${WORKER2_IP}"; do
  rscp "${tmp_prepare}" "$ip" /tmp/rebuild_k3s_node.sh
done
rm -f "${tmp_prepare}"

say "2) Rebuild master first"
rssh "${MASTER_IP}" \
  "bash /tmp/rebuild_k3s_node.sh '${MASTER_HOSTNAME}' '${MASTER_IP}' '${MASTER_IP}' '${WORKER1_IP}' '${WORKER2_IP}' '${MASTER_HOSTNAME}' '${WORKER1_HOSTNAME}' '${WORKER2_HOSTNAME}' '${K3S_CLUSTER_CIDR}' '${K3S_SERVICE_CIDR}' '${K3S_NODE_CIDR_MASK_V4}' '${K3S_MAX_PODS}' '${K3S_FLANNEL_BACKEND}' 'server' '${K3S_TOKEN}' '${K3S_VERSION}' '${DISABLE_TRAEFIK}' '${DISABLE_SERVICELB}'"

say "3) Wait until master K3s API is up"
deadline=$(( $(date +%s) + NODE_READY_WAIT_SECONDS ))
while true; do
  if rssh "${MASTER_IP}" "sudo k3s kubectl get node >/dev/null 2>&1"; then
    echo "Master API is up"
    break
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "ERROR: master API did not come up in ${NODE_READY_WAIT_SECONDS}s"
    exit 1
  fi
  sleep 5
done

say "4) Rebuild workers"
rssh "${WORKER1_IP}" \
  "bash /tmp/rebuild_k3s_node.sh '${WORKER1_HOSTNAME}' '${WORKER1_IP}' '${MASTER_IP}' '${WORKER1_IP}' '${WORKER2_IP}' '${MASTER_HOSTNAME}' '${WORKER1_HOSTNAME}' '${WORKER2_HOSTNAME}' '${K3S_CLUSTER_CIDR}' '${K3S_SERVICE_CIDR}' '${K3S_NODE_CIDR_MASK_V4}' '${K3S_MAX_PODS}' '${K3S_FLANNEL_BACKEND}' 'agent' '${K3S_TOKEN}' '${K3S_VERSION}' '${DISABLE_TRAEFIK}' '${DISABLE_SERVICELB}'"

rssh "${WORKER2_IP}" \
  "bash /tmp/rebuild_k3s_node.sh '${WORKER2_HOSTNAME}' '${WORKER2_IP}' '${MASTER_IP}' '${WORKER1_IP}' '${WORKER2_IP}' '${MASTER_HOSTNAME}' '${WORKER1_HOSTNAME}' '${WORKER2_HOSTNAME}' '${K3S_CLUSTER_CIDR}' '${K3S_SERVICE_CIDR}' '${K3S_NODE_CIDR_MASK_V4}' '${K3S_MAX_PODS}' '${K3S_FLANNEL_BACKEND}' 'agent' '${K3S_TOKEN}' '${K3S_VERSION}' '${DISABLE_TRAEFIK}' '${DISABLE_SERVICELB}'"

say "5) Wait for all 3 nodes to become Ready"
deadline=$(( $(date +%s) + NODE_READY_WAIT_SECONDS ))
while true; do
  ready_count="$(
    rssh "${MASTER_IP}" "sudo k3s kubectl get nodes --no-headers 2>/dev/null | awk '\$2 ~ /Ready/ {count++} END {print count+0}'" \
    | tr -d '\r'
  )"
  echo "Ready nodes: ${ready_count}/3"
  if [ "${ready_count}" = "3" ]; then
    break
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "ERROR: not all nodes became Ready within ${NODE_READY_WAIT_SECONDS}s"
    rssh "${MASTER_IP}" "sudo k3s kubectl get nodes -o wide || true"
    exit 1
  fi
  sleep 5
done

say "6) Verification: nodes, podCIDR, allocatable.pods, kube-system"
rssh "${MASTER_IP}" "sudo k3s kubectl get nodes -o wide"
echo
rssh "${MASTER_IP}" "sudo k3s kubectl get node -o jsonpath='{range .items[*]}{.metadata.name}{\"  podCIDR=\"}{.spec.podCIDR}{\"  allocatable.pods=\"}{.status.allocatable.pods}{\"\\n\"}{end}'"
echo
rssh "${MASTER_IP}" "sudo k3s kubectl -n kube-system get pods -o wide"

say "7) Strong check: require podCIDR mask == /20 on all nodes"
mask_check="$(
  rssh "${MASTER_IP}" "sudo k3s kubectl get node -o jsonpath='{range .items[*]}{.spec.podCIDR}{\"\\n\"}{end}' | grep -c '/20$' || true" \
  | tr -d '\r'
)"
if [ "${mask_check}" != "3" ]; then
  echo "ERROR: expected 3 nodes with /20 podCIDR, got ${mask_check}"
  exit 1
fi

say "SUCCESS"
echo "K3s cluster was rebuilt on the three VMs."
echo "Verified:"
echo " - all 3 nodes Ready"
echo " - podCIDR is /20 on all nodes"
echo " - allocatable.pods should remain high (target 4k)"
echo
echo "Next recommended checks:"
echo "  ssh ${SSH_USER}@${MASTER_IP} 'sudo k3s kubectl get node -o jsonpath=\"{range .items[*]}{.metadata.name}{\"  podCIDR=\"}{.spec.podCIDR}{\"\\n\"}{end}\"'"
echo "  ssh ${SSH_USER}@${MASTER_IP} 'sudo k3s kubectl -n kube-system get pods -o wide'"
