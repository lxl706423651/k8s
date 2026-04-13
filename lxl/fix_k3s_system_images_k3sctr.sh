#!/usr/bin/env bash
set -euo pipefail

SSH_USER="${SSH_USER:-ubuntu}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"

MASTER_IP="${MASTER_IP:-192.168.122.110}"
WORKER1_IP="${WORKER1_IP:-192.168.122.111}"
WORKER2_IP="${WORKER2_IP:-192.168.122.112}"

WORKDIR="${WORKDIR:-/tmp/k3s-system-images}"

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

sanitize_name() {
  local image="$1"
  echo "$image" | sed 's#/#__#g; s#:#__#g'
}

say "0) Check tarballs exist locally"
mkdir -p "${WORKDIR}"
for image in "${IMAGES[@]}"; do
  tar_name="$(sanitize_name "$image").tar"
  tar_path="${WORKDIR}/${tar_name}"
  if [ ! -f "${tar_path}" ]; then
    echo "Missing tarball: ${tar_path}"
    echo "Please create it first with:"
    echo "  docker pull ${image}"
    echo "  docker save ${image} -o ${tar_path}"
    exit 1
  fi
done

say "1) Copy tarballs to all nodes"
for ip in "${MASTER_IP}" "${WORKER1_IP}" "${WORKER2_IP}"; do
  rssh "$ip" "mkdir -p /tmp/k3s-system-images"
  for image in "${IMAGES[@]}"; do
    tar_name="$(sanitize_name "$image").tar"
    tar_path="${WORKDIR}/${tar_name}"
    echo "[copy] ${tar_name} -> ${ip}"
    rscp "${tar_path}" "$ip" "/tmp/k3s-system-images/${tar_name}"
  done
done

say "2) Import images into the CORRECT K3s runtime"
remote_import='
set -euo pipefail
for f in /tmp/k3s-system-images/*.tar; do
  echo "[import into k3s runtime] $f"
  sudo k3s ctr images import "$f"
done

echo
echo "[verify in k3s runtime]"
sudo k3s ctr images list | egrep "pause|coredns|local-path-provisioner|metrics-server" || true
'
rssh "${MASTER_IP}"  "bash -lc '$remote_import'"
rssh "${WORKER1_IP}" "bash -lc '$remote_import'"
rssh "${WORKER2_IP}" "bash -lc '$remote_import'"

say "3) Restart K3s services"
rssh "${MASTER_IP}"  "sudo systemctl restart k3s"
rssh "${WORKER1_IP}" "sudo systemctl restart k3s-agent"
rssh "${WORKER2_IP}" "sudo systemctl restart k3s-agent"

say "4) Wait a bit"
sleep 20

say "5) Verify kube-system on master"
rssh "${MASTER_IP}" "sudo k3s kubectl -n kube-system get pods -o wide"
echo
rssh "${MASTER_IP}" "sudo k3s kubectl get events -A --sort-by='.lastTimestamp' | tail -n 80"

say "Done"
echo "Watch kube-system live with:"
echo "  ssh -i ${SSH_KEY} -o StrictHostKeyChecking=no ${SSH_USER}@${MASTER_IP} 'sudo k3s kubectl -n kube-system get pods -w'"
