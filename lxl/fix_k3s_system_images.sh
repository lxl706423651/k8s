#!/usr/bin/env bash
set -euo pipefail

###############################################################################
# Host-side script:
# - Pull/save K3s system images on host
# - Recover SSH on worker VMs if needed
# - Copy and import images into all 3 K3s nodes
# - Restart k3s / k3s-agent
# - Verify kube-system on master
###############################################################################

SSH_USER="${SSH_USER:-ubuntu}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"

MASTER_IP="${MASTER_IP:-192.168.122.110}"
WORKER1_IP="${WORKER1_IP:-192.168.122.111}"
WORKER2_IP="${WORKER2_IP:-192.168.122.112}"

MASTER_VM="${MASTER_VM:-seed-k3s-master}"
WORKER1_VM="${WORKER1_VM:-seed-k3s-worker1}"
WORKER2_VM="${WORKER2_VM:-seed-k3s-worker2}"

WORKDIR="${WORKDIR:-/tmp/k3s-system-images}"
SSH_WAIT_SECONDS="${SSH_WAIT_SECONDS:-300}"

SSH_OPTS=(
  -i "${SSH_KEY}"
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
  -o LogLevel=ERROR
  -o ConnectTimeout=10
)

IMAGES=(
  "rancher/mirrored-pause:3.6"
  "rancher/mirrored-coredns-coredns:1.14.2"
  "rancher/local-path-provisioner:v0.0.35"
  "rancher/mirrored-metrics-server:v0.8.1"
)

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

ssh_ok() {
  local ip="$1"
  rssh "$ip" "echo ok" >/dev/null 2>&1
}

wait_ssh() {
  local ip="$1"
  local deadline=$(( $(date +%s) + SSH_WAIT_SECONDS ))
  while true; do
    if ssh_ok "$ip"; then
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

recover_node_ssh() {
  local vm="$1"
  local ip="$2"

  if ssh_ok "$ip"; then
    echo "SSH already OK on ${vm} (${ip})"
    return 0
  fi

  echo "SSH not reachable on ${vm} (${ip}), trying virsh reboot..."
  virsh reboot "$vm" >/dev/null 2>&1 || true
  sleep 5
  if wait_ssh "$ip"; then
    return 0
  fi

  echo "virsh reboot did not recover ${vm}, trying hard restart..."
  virsh destroy "$vm" >/dev/null 2>&1 || true
  sleep 2
  virsh start "$vm" >/dev/null
  wait_ssh "$ip"
}

sanitize_name() {
  local image="$1"
  echo "$image" | sed 's#/#__#g; s#:#__#g'
}

say "0) Prepare local image tarballs on host"
mkdir -p "${WORKDIR}"

for image in "${IMAGES[@]}"; do
  tar_name="$(sanitize_name "$image").tar"
  tar_path="${WORKDIR}/${tar_name}"

  echo "[pull] $image"
  docker pull "$image"

  echo "[save] $image -> $tar_path"
  docker save "$image" -o "$tar_path"
done

say "1) Recover SSH reachability where needed"
recover_node_ssh "${MASTER_VM}"  "${MASTER_IP}"
recover_node_ssh "${WORKER1_VM}" "${WORKER1_IP}"
recover_node_ssh "${WORKER2_VM}" "${WORKER2_IP}"

say "2) Copy tarballs to all nodes"
for ip in "${MASTER_IP}" "${WORKER1_IP}" "${WORKER2_IP}"; do
  rssh "$ip" "mkdir -p /tmp/k3s-system-images"
  for image in "${IMAGES[@]}"; do
    tar_name="$(sanitize_name "$image").tar"
    tar_path="${WORKDIR}/${tar_name}"
    echo "[copy] ${tar_name} -> ${ip}"
    rscp "$tar_path" "$ip" "/tmp/k3s-system-images/${tar_name}"
  done
done

say "3) Import images into containerd on all nodes"
import_remote='
set -euo pipefail
for f in /tmp/k3s-system-images/*.tar; do
  echo "[import] $f"
  sudo ctr -n k8s.io images import "$f"
done
sudo ctr -n k8s.io images ls | egrep "pause|coredns|local-path-provisioner|metrics-server" || true
'
rssh "${MASTER_IP}"  "bash -lc '$import_remote'"
rssh "${WORKER1_IP}" "bash -lc '$import_remote'"
rssh "${WORKER2_IP}" "bash -lc '$import_remote'"

say "4) Restart K3s services"
rssh "${MASTER_IP}"  "sudo systemctl restart k3s"
rssh "${WORKER1_IP}" "sudo systemctl restart k3s-agent"
rssh "${WORKER2_IP}" "sudo systemctl restart k3s-agent"

say "5) Wait a bit for kube-system recovery"
sleep 20

say "6) Verify kube-system on master"
rssh "${MASTER_IP}" "sudo k3s kubectl -n kube-system get pods -o wide"
echo
rssh "${MASTER_IP}" "sudo k3s kubectl get events -A --sort-by='.lastTimestamp' | tail -n 80"

say "Done"
echo "If kube-system is still not healthy, run this to watch progress:"
echo "  ssh -i ${SSH_KEY} -o StrictHostKeyChecking=no ${SSH_USER}@${MASTER_IP} 'sudo k3s kubectl -n kube-system get pods -w'"
