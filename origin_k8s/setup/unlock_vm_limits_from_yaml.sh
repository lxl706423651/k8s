#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_PATH="${1:-${SCRIPT_DIR}/cluster_config.example.yaml}"
HELPER="${SCRIPT_DIR}/cluster_config.py"
RESOLVED_NODES_TSV="${CONFIG_PATH%.*}.resolved-nodes.tsv"

eval "$(python3 "${HELPER}" "${CONFIG_PATH}" kvm-env)"

# Keep these defaults in the script, not in kvm.yaml. The YAML describes VMs;
# this script describes the current high-density OS limit policy.
VM_LIMIT_NOFILE="${VM_LIMIT_NOFILE:-10485760}"
VM_LIMIT_NPROC="${VM_LIMIT_NPROC:-4194304}"
VM_LIMIT_MAX_NET_NAMESPACES="${VM_LIMIT_MAX_NET_NAMESPACES:-65536}"
VM_LIMIT_NEIGH_GC_THRESH1="${VM_LIMIT_NEIGH_GC_THRESH1:-1048576}"
VM_LIMIT_NEIGH_GC_THRESH2="${VM_LIMIT_NEIGH_GC_THRESH2:-4194304}"
VM_LIMIT_NEIGH_GC_THRESH3="${VM_LIMIT_NEIGH_GC_THRESH3:-8388608}"
VM_LIMIT_NETDEV_MAX_BACKLOG="${VM_LIMIT_NETDEV_MAX_BACKLOG:-1000000}"
VM_LIMIT_OPTMEM_MAX="${VM_LIMIT_OPTMEM_MAX:-25165824}"
VM_LIMIT_CNI0_HASH_MAX="${VM_LIMIT_CNI0_HASH_MAX:-16384}"
VM_LIMIT_REBOOT="${VM_LIMIT_REBOOT:-false}"

SSH_OPTS=(
    -i "${SEED_SSH_KEY}"
    -o StrictHostKeyChecking=no
    -o UserKnownHostsFile=/dev/null
    -o LogLevel=ERROR
    -o BatchMode=yes
    -o IdentitiesOnly=yes
    -o IdentityAgent=none
    -o ConnectTimeout=10
    -o ServerAliveInterval=30
    -o ServerAliveCountMax=3
)

usage() {
    cat <<EOF
Usage: $0 <kvm.yaml>

Open OS-level limits on every VM listed in the YAML.
Only SSH access is required, so the implementation is VM-provider neutral.
The current setup flow only creates KVM guests, but this script can also tune
other VM types if the YAML lists reachable IPs and the SSH user/key is valid.

Optional environment overrides:
  VM_LIMIT_NOFILE=${VM_LIMIT_NOFILE}
  VM_LIMIT_NPROC=${VM_LIMIT_NPROC}
  VM_LIMIT_MAX_NET_NAMESPACES=${VM_LIMIT_MAX_NET_NAMESPACES}
  VM_LIMIT_CNI0_HASH_MAX=${VM_LIMIT_CNI0_HASH_MAX}
  VM_LIMIT_REBOOT=${VM_LIMIT_REBOOT}
EOF
}

nodes_input() {
    if [ -f "${RESOLVED_NODES_TSV}" ]; then
        cat "${RESOLVED_NODES_TSV}"
    else
        python3 "${HELPER}" "${CONFIG_PATH}" nodes-tsv
    fi
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "Missing required command: $1" >&2
        exit 1
    }
}

wait_for_ssh() {
    local name="$1"
    local ip="$2"
    if ssh -n "${SSH_OPTS[@]}" "${SEED_SSH_USER}@${ip}" "echo ok" >/dev/null 2>&1; then
        return 0
    fi
    echo "SSH failed for ${name} (${ip})" >&2
    return 1
}

unlock_one_vm() {
    local name="$1"
    local ip="$2"
    echo "Unlocking VM limits: ${name} (${ip})"
    ssh "${SSH_OPTS[@]}" "${SEED_SSH_USER}@${ip}" "sudo -n bash -s" -- \
        "${VM_LIMIT_NOFILE}" \
        "${VM_LIMIT_NPROC}" \
        "${VM_LIMIT_MAX_NET_NAMESPACES}" \
        "${VM_LIMIT_NEIGH_GC_THRESH1}" \
        "${VM_LIMIT_NEIGH_GC_THRESH2}" \
        "${VM_LIMIT_NEIGH_GC_THRESH3}" \
        "${VM_LIMIT_NETDEV_MAX_BACKLOG}" \
        "${VM_LIMIT_OPTMEM_MAX}" \
        "${VM_LIMIT_CNI0_HASH_MAX}" \
        "${VM_LIMIT_REBOOT}" <<'EOF_REMOTE'
set -euo pipefail

LIMIT_NOFILE="$1"
LIMIT_NPROC="$2"
MAX_NET_NS="$3"
NEIGH1="$4"
NEIGH2="$5"
NEIGH3="$6"
NETDEV_BACKLOG="$7"
OPTMEM_MAX="$8"
CNI0_HASH_MAX="$9"
DO_REBOOT="${10}"

append_if_missing() {
    local file="$1"
    local pattern="$2"
    local line="$3"
    touch "${file}"
    if ! grep -qF "${pattern}" "${file}"; then
        printf '%s\n' "${line}" >> "${file}"
    fi
}

echo ">> limits.conf"
append_if_missing /etc/security/limits.conf "* soft nofile" "* soft    nofile          ${LIMIT_NOFILE}"
append_if_missing /etc/security/limits.conf "* hard nofile" "* hard    nofile          ${LIMIT_NOFILE}"
append_if_missing /etc/security/limits.conf "* soft nproc" "* soft    nproc           ${LIMIT_NPROC}"
append_if_missing /etc/security/limits.conf "* hard nproc" "* hard    nproc           ${LIMIT_NPROC}"
append_if_missing /etc/security/limits.conf "root soft nofile" "root            soft    nofile          ${LIMIT_NOFILE}"
append_if_missing /etc/security/limits.conf "root hard nofile" "root            hard    nofile          ${LIMIT_NOFILE}"

echo ">> sysctl"
cat > /etc/sysctl.d/99-seed-vm-limits.conf <<EOF_SYSCTL
user.max_net_namespaces = ${MAX_NET_NS}
net.ipv4.neigh.default.gc_thresh1 = ${NEIGH1}
net.ipv4.neigh.default.gc_thresh2 = ${NEIGH2}
net.ipv4.neigh.default.gc_thresh3 = ${NEIGH3}
fs.inotify.max_user_watches = 52428800
fs.inotify.max_user_instances = 5242880
kernel.pid_max = 4194304
kernel.threads-max = 4194304
net.core.somaxconn = 65535
net.core.netdev_max_backlog = ${NETDEV_BACKLOG}
net.core.optmem_max = ${OPTMEM_MAX}
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

modprobe nf_conntrack || true
sysctl --system >/dev/null 2>&1 || true
ip -s -s neigh flush all >/dev/null 2>&1 || true

echo ">> systemd default limits"
mkdir -p /etc/systemd/system.conf.d /etc/systemd/user.conf.d
cat > /etc/systemd/system.conf.d/99-seed-vm-limits.conf <<EOF_SYSTEMD_DEFAULTS
[Manager]
DefaultLimitNOFILE=${LIMIT_NOFILE}
DefaultLimitNPROC=${LIMIT_NPROC}
DefaultTasksMax=infinity
EOF_SYSTEMD_DEFAULTS
cat > /etc/systemd/user.conf.d/99-seed-vm-limits.conf <<EOF_SYSTEMD_USER
[Manager]
DefaultLimitNOFILE=${LIMIT_NOFILE}
DefaultLimitNPROC=${LIMIT_NPROC}
DefaultTasksMax=infinity
EOF_SYSTEMD_USER

echo ">> known service drop-ins if present"
for service in k3s k3s-agent containerd docker; do
    if systemctl list-unit-files | grep -q "^${service}.service"; then
        mkdir -p "/etc/systemd/system/${service}.service.d"
        cat > "/etc/systemd/system/${service}.service.d/99-seed-vm-limits.conf" <<EOF_SERVICE
[Service]
LimitNOFILE=${LIMIT_NOFILE}
LimitNPROC=${LIMIT_NPROC}
TasksMax=infinity
EOF_SERVICE
    fi
done
systemctl daemon-reload

echo ">> cni0 hash_max service for future K3s/flannel bridge"
cat > /usr/local/sbin/seed-vm-cni0-hashmax.sh <<'EOF_TUNE'
#!/usr/bin/env bash
set -euo pipefail
target="${1:-16384}"
for _ in $(seq 1 60); do
    if [ -w /sys/class/net/cni0/bridge/hash_max ]; then
        printf '%s\n' "${target}" > /sys/class/net/cni0/bridge/hash_max
        exit 0
    fi
    sleep 2
done
exit 0
EOF_TUNE
chmod +x /usr/local/sbin/seed-vm-cni0-hashmax.sh

cat > /etc/systemd/system/seed-vm-cni0-hashmax.service <<EOF_UNIT
[Unit]
Description=Apply SEED cni0 bridge hash_max when cni0 exists
After=network-online.target k3s.service k3s-agent.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/seed-vm-cni0-hashmax.sh ${CNI0_HASH_MAX}
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF_UNIT
systemctl daemon-reload
systemctl enable seed-vm-cni0-hashmax.service >/dev/null 2>&1 || true
if [ -w /sys/class/net/cni0/bridge/hash_max ]; then
    /usr/local/sbin/seed-vm-cni0-hashmax.sh "${CNI0_HASH_MAX}" >/dev/null 2>&1 || true
fi

if [ "${DO_REBOOT}" = "true" ]; then
    systemd-run --on-active=2 /bin/bash -c "reboot" >/dev/null 2>&1 || true
fi

echo "VM limit unlock completed on $(hostname)"
EOF_REMOTE
}

main() {
    if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
        usage
        exit 0
    fi
    require_cmd python3
    require_cmd ssh
    [ -f "${SEED_SSH_KEY}" ] || {
        echo "SSH key not found: ${SEED_SSH_KEY}" >&2
        exit 1
    }

    if [ -f "${RESOLVED_NODES_TSV}" ]; then
        echo "Using resolved node plan: ${RESOLVED_NODES_TSV}"
    else
        echo "Resolved node plan not found; using direct YAML expansion."
    fi

    while IFS=$'\t' read -r name role ip mac vcpus memory_mb disk_gb; do
        wait_for_ssh "${name}" "${ip}"
    done < <(nodes_input)

    while IFS=$'\t' read -r name role ip mac vcpus memory_mb disk_gb; do
        unlock_one_vm "${name}" "${ip}"
    done < <(nodes_input)

    echo "VM limit unlock completed for all nodes in ${CONFIG_PATH}"
}

main "$@"
