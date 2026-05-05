#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [ -f "${SCRIPT_DIR}/env_9node.sh" ]; then
    # shellcheck disable=SC1091
    source "${SCRIPT_DIR}/env_9node.sh" >/dev/null 2>&1 || true
fi

export SEED_KVM_STORAGE_DIR="${SEED_KVM_STORAGE_DIR:-/data/lxl/k8s/output/kvm_lab}"
export SEED_KVM_BASE_IMAGE_PATH="${SEED_KVM_BASE_IMAGE_PATH:-${REPO_ROOT}/output/kvm_lab/base/jammy-server-cloudimg-amd64.img}"
export SEED_EXTRA_WORKER_START_INDEX="${SEED_EXTRA_WORKER_START_INDEX:-9}"
export SEED_EXTRA_WORKER_COUNT="${SEED_EXTRA_WORKER_COUNT:-3}"
export SEED_EXTRA_WORKER_VCPUS="${SEED_EXTRA_WORKER_VCPUS:-24}"
export SEED_EXTRA_WORKER_MEMORY_MB="${SEED_EXTRA_WORKER_MEMORY_MB:-49152}"
export SEED_EXTRA_WORKER_DISK_GB="${SEED_EXTRA_WORKER_DISK_GB:-200}"
export SEED_EXTRA_WORKER_INVENTORY_PATH="${SEED_EXTRA_WORKER_INVENTORY_PATH:-${REPO_ROOT}/configs/clusters/seedemu-k3s-12node.yaml}"
export SEED_EXTRA_WORKER_ENV_FILE="${SEED_EXTRA_WORKER_ENV_FILE:-${REPO_ROOT}/output/kvm_lab/k3s_vm_env_12node.sh}"

export SEED_K3S_WORKER9_IP="${SEED_K3S_WORKER9_IP:-192.168.122.119}"
export SEED_K3S_WORKER10_IP="${SEED_K3S_WORKER10_IP:-192.168.122.120}"
export SEED_K3S_WORKER11_IP="${SEED_K3S_WORKER11_IP:-192.168.122.121}"

for idx in $(seq 1 8); do
    ip_var="SEED_K3S_WORKER${idx}_IP"
    if [ -z "${!ip_var:-}" ]; then
        printf -v "${ip_var}" '%s' "192.168.122.$((110 + idx))"
        export "${ip_var}"
    fi
done

write_12node_inventory() {
    mkdir -p "$(dirname "${SEED_EXTRA_WORKER_INVENTORY_PATH}")"
    cat > "${SEED_EXTRA_WORKER_INVENTORY_PATH}" <<EOF
cluster_name: seedemu-k3s
reference_cluster: false
runtime: k3s
max_validated_topology_size: 12000
k3s:
  cluster_cidr: 10.42.0.0/16
  service_cidr: 10.43.0.0/16
  node_cidr_mask_size_ipv4: 20
  max_pods: ${SEED_K3S_MAX_PODS:-4000}
network_tuning:
  cni0_hash_max: ${SEED_CNI0_HASH_MAX:-16384}
  user_max_net_namespaces: ${SEED_USER_MAX_NET_NAMESPACES:-65536}
  neigh_gc_thresh1: ${SEED_NEIGH_GC_THRESH1:-1048576}
  neigh_gc_thresh2: ${SEED_NEIGH_GC_THRESH2:-4194304}
  neigh_gc_thresh3: ${SEED_NEIGH_GC_THRESH3:-8388608}
  netdev_max_backlog: ${SEED_NETDEV_MAX_BACKLOG:-1000000}
  optmem_max: ${SEED_OPTMEM_MAX:-25165824}
ssh:
  user: ${SEED_K3S_USER:-ubuntu}
  key_path_env: SEED_K3S_SSH_KEY
  default_key_path: ~/.ssh/id_ed25519
registry:
  host: ${SEED_REGISTRY_HOST:-192.168.122.110}
  port: ${SEED_REGISTRY_PORT:-5000}
cni:
  default_master_interface: ${SEED_CNI_MASTER_INTERFACE:-ens2}
nodes:
  - name: seed-k3s-master
    role: master
    management_ip: ${SEED_K3S_MASTER_IP:-192.168.122.110}
    runtime: k3s
    labels:
      kubernetes.io/hostname: seed-k3s-master
EOF

    local idx ip
    for idx in $(seq 1 11); do
        ip_var="SEED_K3S_WORKER${idx}_IP"
        ip="${!ip_var}"
        cat >> "${SEED_EXTRA_WORKER_INVENTORY_PATH}" <<EOF
  - name: seed-k3s-worker${idx}
    role: worker
    management_ip: ${ip}
    runtime: k3s
    labels:
      kubernetes.io/hostname: seed-k3s-worker${idx}
EOF
    done
}

write_12node_env_file() {
    mkdir -p "$(dirname "${SEED_EXTRA_WORKER_ENV_FILE}")"
    cat > "${SEED_EXTRA_WORKER_ENV_FILE}" <<EOF
#!/usr/bin/env bash
export SEED_K3S_CLUSTER_NAME="seedemu-k3s"
export SEED_CLUSTER_INVENTORY="seedemu-k3s-12node"
export SEED_CLUSTER_INVENTORY_PATH="${SEED_EXTRA_WORKER_INVENTORY_PATH}"
export SEED_K3S_MASTER_IP="${SEED_K3S_MASTER_IP:-192.168.122.110}"
export SEED_K3S_USER="${SEED_K3S_USER:-ubuntu}"
export SEED_K3S_SSH_KEY="${SEED_K3S_SSH_KEY:-$HOME/.ssh/id_ed25519}"
export SEED_REGISTRY_HOST="${SEED_REGISTRY_HOST:-192.168.122.110}"
export SEED_REGISTRY_PORT="${SEED_REGISTRY_PORT:-5000}"
EOF
    local idx ip
    for idx in $(seq 1 11); do
        ip_var="SEED_K3S_WORKER${idx}_IP"
        ip="${!ip_var}"
        cat >> "${SEED_EXTRA_WORKER_ENV_FILE}" <<EOF
export SEED_K3S_WORKER${idx}_IP="${ip}"
EOF
    done
    chmod +x "${SEED_EXTRA_WORKER_ENV_FILE}"
}

main() {
    if [ "${1:-}" = "--rewrite-only" ]; then
        write_12node_inventory
        write_12node_env_file
        echo "12-node inventory refreshed: ${SEED_EXTRA_WORKER_INVENTORY_PATH}"
        echo "12-node env file refreshed: ${SEED_EXTRA_WORKER_ENV_FILE}"
        return 0
    fi

    "${SCRIPT_DIR}/add_k3s_workers_6.sh" "$@"
    write_12node_inventory
    write_12node_env_file
    echo "12-node inventory refreshed: ${SEED_EXTRA_WORKER_INVENTORY_PATH}"
    echo "12-node env file refreshed: ${SEED_EXTRA_WORKER_ENV_FILE}"
}

main "$@"
