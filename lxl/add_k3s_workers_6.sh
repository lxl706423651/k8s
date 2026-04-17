#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
source "${REPO_ROOT}/scripts/env_seedemu.sh"

SEED_KVM_NETWORK="${SEED_KVM_NETWORK:-default}"
SEED_KVM_STORAGE_DIR="${SEED_KVM_STORAGE_DIR:-${REPO_ROOT}/output/kvm_lab}"
SEED_KVM_UBUNTU_SERIES="${SEED_KVM_UBUNTU_SERIES:-jammy}"
SEED_KVM_BOOT_TIMEOUT_SECONDS="${SEED_KVM_BOOT_TIMEOUT_SECONDS:-300}"
SEED_K3S_USER="${SEED_K3S_USER:-ubuntu}"
SEED_K3S_SSH_KEY="${SEED_K3S_SSH_KEY:-$HOME/.ssh/id_ed25519}"
SEED_K3S_VERSION="${SEED_K3S_VERSION:-v1.28.5+k3s1}"
SEED_K3S_ARTIFACT_URL="${SEED_K3S_ARTIFACT_URL:-https://rancher-mirror.rancher.cn/k3s}"
if [ -z "${SEED_K3S_INSTALL_VERSION:-}" ]; then
    if [[ "${SEED_K3S_ARTIFACT_URL}" == *"rancher-mirror.rancher.cn/k3s"* ]]; then
        SEED_K3S_INSTALL_VERSION="${SEED_K3S_VERSION//+/-}"
    else
        SEED_K3S_INSTALL_VERSION="${SEED_K3S_VERSION}"
    fi
fi
SEED_REGISTRY_HOST="${SEED_REGISTRY_HOST:-192.168.122.110}"
SEED_REGISTRY_PORT="${SEED_REGISTRY_PORT:-5000}"
SEED_DOCKER_IO_MIRROR_ENDPOINT="${SEED_DOCKER_IO_MIRROR_ENDPOINT:-https://docker.m.daocloud.io}"
SEED_CNI_MASTER_INTERFACE="${SEED_CNI_MASTER_INTERFACE:-ens2}"
SEED_K3S_MAX_PODS="${SEED_K3S_MAX_PODS:-4000}"
SEED_CNI0_HASH_MAX="${SEED_CNI0_HASH_MAX:-16384}"
SEED_USER_MAX_NET_NAMESPACES="${SEED_USER_MAX_NET_NAMESPACES:-65536}"
SEED_NEIGH_GC_THRESH1="${SEED_NEIGH_GC_THRESH1:-1048576}"
SEED_NEIGH_GC_THRESH2="${SEED_NEIGH_GC_THRESH2:-4194304}"
SEED_NEIGH_GC_THRESH3="${SEED_NEIGH_GC_THRESH3:-8388608}"
SEED_NETDEV_MAX_BACKLOG="${SEED_NETDEV_MAX_BACKLOG:-1000000}"
SEED_OPTMEM_MAX="${SEED_OPTMEM_MAX:-25165824}"
SEED_KUBELET_MAX_PARALLEL_IMAGE_PULLS="${SEED_KUBELET_MAX_PARALLEL_IMAGE_PULLS:-3}"
SEED_KUBELET_REGISTRY_QPS="${SEED_KUBELET_REGISTRY_QPS:-100}"
SEED_KUBELET_REGISTRY_BURST="${SEED_KUBELET_REGISTRY_BURST:-20}"
SEED_K3S_MASTER_NAME="${SEED_K3S_MASTER_NAME:-seed-k3s-master}"
SEED_K3S_MASTER_IP="${SEED_K3S_MASTER_IP:-192.168.122.110}"

SEED_EXTRA_WORKER_START_INDEX="${SEED_EXTRA_WORKER_START_INDEX:-3}"
SEED_EXTRA_WORKER_COUNT="${SEED_EXTRA_WORKER_COUNT:-6}"
SEED_EXTRA_WORKER_VCPUS="${SEED_EXTRA_WORKER_VCPUS:-32}"
SEED_EXTRA_WORKER_MEMORY_MB="${SEED_EXTRA_WORKER_MEMORY_MB:-61440}"
SEED_EXTRA_WORKER_DISK_GB="${SEED_EXTRA_WORKER_DISK_GB:-200}"
SEED_EXTRA_WORKER_NAME_PREFIX="${SEED_EXTRA_WORKER_NAME_PREFIX:-seed-k3s-worker}"
SEED_EXTRA_WORKER_IP_PREFIX="${SEED_EXTRA_WORKER_IP_PREFIX:-192.168.122}"
SEED_EXTRA_WORKER_MAC_PREFIX="${SEED_EXTRA_WORKER_MAC_PREFIX:-52:54:00:64:10}"
SEED_EXTRA_WORKER_INVENTORY_PATH="${SEED_EXTRA_WORKER_INVENTORY_PATH:-${REPO_ROOT}/configs/clusters/seedemu-k3s-9node.yaml}"
SEED_EXTRA_WORKER_ENV_FILE="${SEED_EXTRA_WORKER_ENV_FILE:-${SEED_KVM_STORAGE_DIR}/k3s_vm_env_9node.sh}"

case "${SEED_KVM_UBUNTU_SERIES}" in
    jammy)
        DEFAULT_KVM_BASE_IMAGE_URL="https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img"
        DEFAULT_KVM_BASE_IMAGE_PATH="${SEED_KVM_STORAGE_DIR}/base/jammy-server-cloudimg-amd64.img"
        ;;
    noble)
        DEFAULT_KVM_BASE_IMAGE_URL="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
        DEFAULT_KVM_BASE_IMAGE_PATH="${SEED_KVM_STORAGE_DIR}/base/noble-server-cloudimg-amd64.img"
        ;;
    *)
        echo "Unsupported SEED_KVM_UBUNTU_SERIES: ${SEED_KVM_UBUNTU_SERIES}" >&2
        exit 1
        ;;
esac

SEED_KVM_BASE_IMAGE_URL="${SEED_KVM_BASE_IMAGE_URL:-${DEFAULT_KVM_BASE_IMAGE_URL}}"
SEED_KVM_BASE_IMAGE_PATH="${SEED_KVM_BASE_IMAGE_PATH:-${DEFAULT_KVM_BASE_IMAGE_PATH}}"

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

declare -a WORKER_NAMES=()
declare -a WORKER_IPS=()
declare -a WORKER_MACS=()

usage() {
    cat <<'EOF'
Usage: ./add_k3s_workers_6.sh

This script:
  1. Creates worker3 ... worker8 as KVM guests
  2. Joins them to the existing K3s cluster as agents
  3. Writes a 9-node inventory file
  4. Writes an extended VM env file

Default worker profile:
  32 vCPU, 60 GiB RAM, 200 GiB disk

Default worker addresses:
  worker3 -> 192.168.122.113
  worker4 -> 192.168.122.114
  worker5 -> 192.168.122.115
  worker6 -> 192.168.122.116
  worker7 -> 192.168.122.117
  worker8 -> 192.168.122.118
EOF
}

require_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "Missing required command: $1" >&2
        exit 1
    fi
}

domain_exists() {
    virsh dominfo "$1" >/dev/null 2>&1
}

domain_running() {
    virsh domstate "$1" 2>/dev/null | grep -qi "running"
}

prepare_dirs() {
    mkdir -p "${SEED_KVM_STORAGE_DIR}/base"
    mkdir -p "${SEED_KVM_STORAGE_DIR}/disks"
    mkdir -p "${SEED_KVM_STORAGE_DIR}/cloud-init"
}

ensure_network() {
    if ! virsh net-info "${SEED_KVM_NETWORK}" >/dev/null 2>&1; then
        echo "Libvirt network not found: ${SEED_KVM_NETWORK}" >&2
        exit 1
    fi
    virsh net-start "${SEED_KVM_NETWORK}" >/dev/null 2>&1 || true
    virsh net-autostart "${SEED_KVM_NETWORK}" >/dev/null 2>&1 || true
}

download_base_image() {
    if [ -f "${SEED_KVM_BASE_IMAGE_PATH}" ]; then
        return
    fi
    curl -fL "${SEED_KVM_BASE_IMAGE_URL}" -o "${SEED_KVM_BASE_IMAGE_PATH}"
}

load_ssh_pubkey() {
    if [ -f "${SEED_K3S_SSH_KEY}.pub" ]; then
        tr -d '\n' < "${SEED_K3S_SSH_KEY}.pub"
        return 0
    fi

    if [ -f "${SEED_K3S_SSH_KEY}" ]; then
        ssh-keygen -y -f "${SEED_K3S_SSH_KEY}" 2>/dev/null | tr -d '\n'
        return 0
    fi

    echo "Cannot read SSH key for ${SEED_K3S_SSH_KEY}" >&2
    exit 1
}

wait_for_ssh() {
    local vm_name="$1"
    local vm_ip="$2"
    local elapsed=0

    while [ "${elapsed}" -lt "${SEED_KVM_BOOT_TIMEOUT_SECONDS}" ]; do
        if ssh "${SSH_OPTS[@]}" "${SEED_K3S_USER}@${vm_ip}" "echo ok" >/dev/null 2>&1; then
            echo "SSH ready: ${vm_name} (${vm_ip})"
            return 0
        fi
        sleep 5
        elapsed=$((elapsed + 5))
    done

    echo "Timeout waiting for SSH on ${vm_name} (${vm_ip})" >&2
    return 1
}

update_dhcp_host() {
    local vm_name="$1"
    local vm_ip="$2"
    local vm_mac="$3"
    local host_xml
    host_xml="<host mac='${vm_mac}' name='${vm_name}' ip='${vm_ip}'/>"

    virsh net-update "${SEED_KVM_NETWORK}" delete ip-dhcp-host "${host_xml}" --live --config >/dev/null 2>&1 || true
    virsh net-update "${SEED_KVM_NETWORK}" add ip-dhcp-host "${host_xml}" --live --config >/dev/null
}

create_vm_cloud_init() {
    local vm_name="$1"
    local pubkey="$2"
    local vm_dir="${SEED_KVM_STORAGE_DIR}/cloud-init/${vm_name}"
    local user_data="${vm_dir}/user-data.yaml"
    local meta_data="${vm_dir}/meta-data.yaml"

    mkdir -p "${vm_dir}"

    cat > "${user_data}" <<EOF
#cloud-config
users:
  - default
  - name: ${SEED_K3S_USER}
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    groups: [sudo]
    ssh_authorized_keys:
      - ${pubkey}
package_update: true
packages:
  - qemu-guest-agent
runcmd:
  - [ systemctl, enable, --now, qemu-guest-agent ]
EOF

    cat > "${meta_data}" <<EOF
instance-id: ${vm_name}
local-hostname: ${vm_name}
EOF
}

create_vm_disk() {
    local vm_name="$1"
    local vm_disk="${SEED_KVM_STORAGE_DIR}/disks/${vm_name}.qcow2"

    if [ -f "${vm_disk}" ]; then
        return
    fi

    qemu-img create -f qcow2 -F qcow2 -b "${SEED_KVM_BASE_IMAGE_PATH}" "${vm_disk}" "${SEED_EXTRA_WORKER_DISK_GB}G" >/dev/null
}

create_vm() {
    local vm_name="$1"
    local vm_ip="$2"
    local vm_mac="$3"
    local vm_disk="${SEED_KVM_STORAGE_DIR}/disks/${vm_name}.qcow2"
    local vm_cloud_dir="${SEED_KVM_STORAGE_DIR}/cloud-init/${vm_name}"

    if domain_exists "${vm_name}"; then
        echo "VM exists: ${vm_name}"
        if ! domain_running "${vm_name}"; then
            virsh start "${vm_name}" >/dev/null
        fi
        return
    fi

    echo "Creating VM: ${vm_name} (${vm_ip})"
    virt-install \
        --name "${vm_name}" \
        --memory "${SEED_EXTRA_WORKER_MEMORY_MB}" \
        --vcpus "${SEED_EXTRA_WORKER_VCPUS}" \
        --cpu host-passthrough \
        --import \
        --os-variant generic \
        --network "network=${SEED_KVM_NETWORK},model=virtio,mac=${vm_mac}" \
        --disk "path=${vm_disk},format=qcow2,bus=virtio" \
        --graphics none \
        --noautoconsole \
        --cloud-init "user-data=${vm_cloud_dir}/user-data.yaml,meta-data=${vm_cloud_dir}/meta-data.yaml" >/dev/null
}

build_workers() {
    local idx ip_octet mac_suffix

    for ((idx=SEED_EXTRA_WORKER_START_INDEX; idx<SEED_EXTRA_WORKER_START_INDEX+SEED_EXTRA_WORKER_COUNT; idx++)); do
        WORKER_NAMES+=("${SEED_EXTRA_WORKER_NAME_PREFIX}${idx}")
        ip_octet=$((110 + idx))
        WORKER_IPS+=("${SEED_EXTRA_WORKER_IP_PREFIX}.${ip_octet}")
        printf -v mac_suffix '%02x' $((16 + idx))
        WORKER_MACS+=("${SEED_EXTRA_WORKER_MAC_PREFIX}:${mac_suffix}")
    done
}

detect_master_token() {
    ssh "${SSH_OPTS[@]}" "${SEED_K3S_USER}@${SEED_K3S_MASTER_IP}" "sudo -n cat /var/lib/rancher/k3s/server/node-token"
}

detect_cni_interface() {
    if [ -n "${SEED_CNI_MASTER_INTERFACE}" ]; then
        printf '%s\n' "${SEED_CNI_MASTER_INTERFACE}"
        return 0
    fi
    ssh "${SSH_OPTS[@]}" "${SEED_K3S_USER}@${SEED_K3S_MASTER_IP}" \
        "ip -o -4 route show to default | sed -n '1{s/.* dev \\([^ ]*\\).*/\\1/p}'"
}

join_worker() {
    local vm_name="$1"
    local vm_ip="$2"
    local token="$3"
    local cni_iface="$4"

    echo "Joining worker to K3s: ${vm_name} (${vm_ip})"
    ssh "${SSH_OPTS[@]}" "${SEED_K3S_USER}@${vm_ip}" "
        set -euo pipefail
        sudo -n mkdir -p /etc/rancher/k3s
        cat <<'EOF' | sudo -n tee /etc/rancher/k3s/config.yaml >/dev/null
kubelet-arg:
  - \"max-pods=${SEED_K3S_MAX_PODS}\"
  - \"max-parallel-image-pulls=${SEED_KUBELET_MAX_PARALLEL_IMAGE_PULLS}\"
  - \"registry-qps=${SEED_KUBELET_REGISTRY_QPS}\"
  - \"registry-burst=${SEED_KUBELET_REGISTRY_BURST}\"
EOF
        cat <<'EOF' | sudo -n tee /etc/rancher/k3s/registries.yaml >/dev/null
mirrors:
  \"${SEED_REGISTRY_HOST}:${SEED_REGISTRY_PORT}\":
    endpoint:
      - \"http://${SEED_REGISTRY_HOST}:${SEED_REGISTRY_PORT}\"
  \"docker.io\":
    endpoint:
      - \"${SEED_DOCKER_IO_MIRROR_ENDPOINT}\"
  \"registry-1.docker.io\":
    endpoint:
      - \"${SEED_DOCKER_IO_MIRROR_ENDPOINT}\"
EOF

        if [ ! -d /var/lib/rancher/k3s/agent ]; then
            curl -sfL https://get.k3s.io | \
              INSTALL_K3S_VERSION='${SEED_K3S_INSTALL_VERSION}' \
              INSTALL_K3S_ARTIFACT_URL='${SEED_K3S_ARTIFACT_URL}' \
              K3S_URL='https://${SEED_K3S_MASTER_IP}:6443' \
              K3S_TOKEN='${token}' \
              sh -
        else
            sudo -n systemctl restart k3s-agent
        fi

        sudo -n apt-get update -y >/dev/null
        sudo -n apt-get install -y containernetworking-plugins >/dev/null
        sudo -n mkdir -p /opt/cni/bin /etc/cni/net.d
        for bin in macvlan ipvlan static; do
          sudo -n ln -sf \"/usr/lib/cni/\${bin}\" \"/opt/cni/bin/\${bin}\"
        done
        sudo -n rm -rf /etc/cni/net.d/multus.d
        sudo -n ln -s /var/lib/rancher/k3s/agent/etc/cni/net.d/multus.d /etc/cni/net.d/multus.d
        sudo -n systemctl enable --now k3s-agent >/dev/null
        ip -o -4 route show to default | grep -q \" dev ${cni_iface}\\b\" || true
    "
}

apply_ultimate_tuning() {
    local vm_name="$1"
    local vm_ip="$2"

    echo "Applying resource uncap tuning: ${vm_name} (${vm_ip})"
    ssh "${SSH_OPTS[@]}" "${SEED_K3S_USER}@${vm_ip}" "
        set -euo pipefail

        LIMITS_FILE=/etc/security/limits.conf
        append_if_missing() {
            local pattern=\"\$1\"
            local line=\"\$2\"
            if ! sudo -n grep -qF \"\$pattern\" \"${LIMITS_FILE}\"; then
                printf '%s\n' \"\$line\" | sudo -n tee -a \"${LIMITS_FILE}\" >/dev/null
            fi
        }

        append_if_missing '* soft nofile' '* soft    nofile          10485760'
        append_if_missing '* hard nofile' '* hard    nofile          10485760'
        append_if_missing '* soft nproc'  '* soft    nproc           4194304'
        append_if_missing '* hard nproc'  '* hard    nproc           4194304'
        append_if_missing 'root soft nofile' 'root            soft    nofile          10485760'
        append_if_missing 'root hard nofile' 'root            hard    nofile          10485760'

        cat <<'EOF_SYSCTL' | sudo -n tee /etc/sysctl.d/99-k8s-ultimate.conf >/dev/null
user.max_net_namespaces = ${SEED_USER_MAX_NET_NAMESPACES}
net.ipv4.neigh.default.gc_thresh1 = ${SEED_NEIGH_GC_THRESH1}
net.ipv4.neigh.default.gc_thresh2 = ${SEED_NEIGH_GC_THRESH2}
net.ipv4.neigh.default.gc_thresh3 = ${SEED_NEIGH_GC_THRESH3}
fs.inotify.max_user_watches = 52428800
fs.inotify.max_user_instances = 5242880
kernel.pid_max = 4194304
kernel.threads-max = 4194304
net.core.somaxconn = 65535
net.core.netdev_max_backlog = ${SEED_NETDEV_MAX_BACKLOG}
net.core.optmem_max = ${SEED_OPTMEM_MAX}
net.core.rmem_max = 134217728
net.core.wmem_max = 134217728
net.ipv4.tcp_rmem = 4096 87380 134217728
net.ipv4.tcp_wmem = 4096 65536 134217728
net.ipv4.ip_forward = 1
net.netfilter.nf_conntrack_max = 33554432
net.netfilter.nf_conntrack_buckets = 8388608
net.ipv4.ipfrag_high_thresh = 268435456
net.ipv4.ipfrag_low_thresh = 134217728
EOF_SYSCTL

        sudo -n modprobe nf_conntrack || true
        sudo -n sysctl --system >/dev/null 2>&1 || true
        sudo -n ip -s -s neigh flush all >/dev/null 2>&1 || true

        for service in k3s k3s-agent containerd; do
            if systemctl list-unit-files | grep -q \"^\${service}.service\"; then
                sudo -n mkdir -p \"/etc/systemd/system/\${service}.service.d\"
                cat <<'EOF_SYSTEMD' | sudo -n tee \"/etc/systemd/system/\${service}.service.d/override.conf\" >/dev/null
[Service]
LimitNOFILE=10485760
LimitNPROC=4194304
TasksMax=infinity
EOF_SYSTEMD
            fi
        done
        sudo -n systemctl daemon-reload

        cat <<'EOF_TUNE' | sudo -n tee /usr/local/sbin/seed-k8s-network-tune.sh >/dev/null
#!/usr/bin/env bash
set -euo pipefail

TARGET_HASH_MAX='${SEED_CNI0_HASH_MAX}'
for _ in \$(seq 1 60); do
    if [ -w /sys/class/net/cni0/bridge/hash_max ]; then
        printf '%s\n' \"\${TARGET_HASH_MAX}\" | tee /sys/class/net/cni0/bridge/hash_max >/dev/null
        exit 0
    fi
    sleep 2
done
exit 0
EOF_TUNE
        sudo -n chmod +x /usr/local/sbin/seed-k8s-network-tune.sh

        cat <<'EOF_UNIT' | sudo -n tee /etc/systemd/system/seed-k8s-network-tune.service >/dev/null
[Unit]
Description=Apply Seed K8s bridge/network tuning after K3s startup
After=network-online.target k3s.service k3s-agent.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/seed-k8s-network-tune.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF_UNIT
        sudo -n systemctl daemon-reload
        sudo -n systemctl enable seed-k8s-network-tune.service >/dev/null 2>&1 || true

        sudo -n mkdir -p /etc/rancher/k3s
        sudo -n touch /etc/rancher/k3s/config.yaml
        sudo -n sed -i '/kubelet-arg:/,\$d' /etc/rancher/k3s/config.yaml 2>/dev/null || true
        cat <<'EOF_K3S' | sudo -n tee -a /etc/rancher/k3s/config.yaml >/dev/null
kubelet-arg:
  - \"max-pods=${SEED_K3S_MAX_PODS}\"
  - \"kube-api-qps=50\"
  - \"kube-api-burst=100\"
  - \"max-parallel-image-pulls=${SEED_KUBELET_MAX_PARALLEL_IMAGE_PULLS}\"
  - \"registry-qps=${SEED_KUBELET_REGISTRY_QPS}\"
  - \"registry-burst=${SEED_KUBELET_REGISTRY_BURST}\"
EOF_K3S

        sudo -n systemctl restart k3s-agent >/dev/null 2>&1 || true
        sudo -n systemctl restart containerd >/dev/null 2>&1 || true
        sudo -n systemctl restart seed-k8s-network-tune.service >/dev/null 2>&1 || true
    "
}

wait_for_nodes_ready() {
    local node_name

    export KUBECONFIG="${REPO_ROOT}/output/kubeconfigs/seedemu-k3s.yaml"
    for node_name in "${WORKER_NAMES[@]}"; do
        echo "Waiting for node Ready: ${node_name}"
        kubectl wait --for=condition=Ready "node/${node_name}" --timeout=300s
        kubectl label node "${node_name}" seedemu.io/as-group="extra-worker" --overwrite >/dev/null
    done
}

write_extended_env_file() {
    local idx env_name

    mkdir -p "$(dirname "${SEED_EXTRA_WORKER_ENV_FILE}")"
    cat > "${SEED_EXTRA_WORKER_ENV_FILE}" <<EOF
#!/usr/bin/env bash
export SEED_K3S_CLUSTER_NAME="seedemu-k3s"
export SEED_K3S_MASTER_IP="${SEED_K3S_MASTER_IP}"
export SEED_K3S_USER="${SEED_K3S_USER}"
export SEED_K3S_SSH_KEY="${SEED_K3S_SSH_KEY}"
export SEED_REGISTRY_HOST="${SEED_REGISTRY_HOST}"
export SEED_REGISTRY_PORT="${SEED_REGISTRY_PORT}"
export SEED_CLUSTER_INVENTORY_PATH="${SEED_EXTRA_WORKER_INVENTORY_PATH}"
export SEED_CLUSTER_INVENTORY="seedemu-k3s-9node"
EOF

    for idx in "${!WORKER_NAMES[@]}"; do
        env_name="$(printf '%s' "${WORKER_NAMES[$idx]}" | tr '[:lower:]-' '[:upper:]_')"
        cat >> "${SEED_EXTRA_WORKER_ENV_FILE}" <<EOF
export ${env_name}_IP="${WORKER_IPS[$idx]}"
EOF
    done

    chmod +x "${SEED_EXTRA_WORKER_ENV_FILE}"
}

write_inventory() {
    local idx

    mkdir -p "$(dirname "${SEED_EXTRA_WORKER_INVENTORY_PATH}")"
    cat > "${SEED_EXTRA_WORKER_INVENTORY_PATH}" <<EOF
cluster_name: seedemu-k3s
reference_cluster: false
runtime: k3s
max_validated_topology_size: 5000
k3s:
  cluster_cidr: 10.42.0.0/16
  service_cidr: 10.43.0.0/16
  node_cidr_mask_size_ipv4: 20
  max_pods: ${SEED_K3S_MAX_PODS}
network_tuning:
  cni0_hash_max: ${SEED_CNI0_HASH_MAX}
  user_max_net_namespaces: ${SEED_USER_MAX_NET_NAMESPACES}
  neigh_gc_thresh1: ${SEED_NEIGH_GC_THRESH1}
  neigh_gc_thresh2: ${SEED_NEIGH_GC_THRESH2}
  neigh_gc_thresh3: ${SEED_NEIGH_GC_THRESH3}
  netdev_max_backlog: ${SEED_NETDEV_MAX_BACKLOG}
  optmem_max: ${SEED_OPTMEM_MAX}
ssh:
  user: ${SEED_K3S_USER}
  key_path_env: SEED_K3S_SSH_KEY
  default_key_path: ~/.ssh/id_ed25519
registry:
  host: ${SEED_REGISTRY_HOST}
  port: ${SEED_REGISTRY_PORT}
cni:
  default_master_interface: ${SEED_CNI_MASTER_INTERFACE}
nodes:
  - name: ${SEED_K3S_MASTER_NAME}
    role: master
    management_ip: ${SEED_K3S_MASTER_IP}
    runtime: k3s
    labels:
      kubernetes.io/hostname: ${SEED_K3S_MASTER_NAME}
  - name: seed-k3s-worker1
    role: worker
    management_ip: 192.168.122.111
    runtime: k3s
    labels:
      kubernetes.io/hostname: seed-k3s-worker1
  - name: seed-k3s-worker2
    role: worker
    management_ip: 192.168.122.112
    runtime: k3s
    labels:
      kubernetes.io/hostname: seed-k3s-worker2
EOF

    for idx in "${!WORKER_NAMES[@]}"; do
        cat >> "${SEED_EXTRA_WORKER_INVENTORY_PATH}" <<EOF
  - name: ${WORKER_NAMES[$idx]}
    role: worker
    management_ip: ${WORKER_IPS[$idx]}
    runtime: k3s
    labels:
      kubernetes.io/hostname: ${WORKER_NAMES[$idx]}
EOF
    done
}

main() {
    local pubkey token cni_iface idx

    require_cmd virsh
    require_cmd virt-install
    require_cmd qemu-img
    require_cmd curl
    require_cmd ssh
    require_cmd kubectl
    require_cmd ssh-keygen

    if [ ! -f "${SEED_K3S_SSH_KEY}" ]; then
        echo "SSH key not found: ${SEED_K3S_SSH_KEY}" >&2
        exit 1
    fi

    build_workers
    prepare_dirs
    ensure_network
    download_base_image
    pubkey="$(load_ssh_pubkey)"
    token="$(detect_master_token)"
    cni_iface="$(detect_cni_interface)"
    SEED_CNI_MASTER_INTERFACE="${cni_iface}"

    for idx in "${!WORKER_NAMES[@]}"; do
        update_dhcp_host "${WORKER_NAMES[$idx]}" "${WORKER_IPS[$idx]}" "${WORKER_MACS[$idx]}"
        create_vm_cloud_init "${WORKER_NAMES[$idx]}" "${pubkey}"
        create_vm_disk "${WORKER_NAMES[$idx]}"
        create_vm "${WORKER_NAMES[$idx]}" "${WORKER_IPS[$idx]}" "${WORKER_MACS[$idx]}"
    done

    for idx in "${!WORKER_NAMES[@]}"; do
        wait_for_ssh "${WORKER_NAMES[$idx]}" "${WORKER_IPS[$idx]}"
        join_worker "${WORKER_NAMES[$idx]}" "${WORKER_IPS[$idx]}" "${token}" "${cni_iface}"
        apply_ultimate_tuning "${WORKER_NAMES[$idx]}" "${WORKER_IPS[$idx]}"
    done

    wait_for_nodes_ready
    write_inventory
    write_extended_env_file

    echo ""
    echo "Added ${SEED_EXTRA_WORKER_COUNT} workers successfully."
    echo "Inventory: ${SEED_EXTRA_WORKER_INVENTORY_PATH}"
    echo "Extended env: ${SEED_EXTRA_WORKER_ENV_FILE}"
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    usage
    exit 0
fi

main "$@"
