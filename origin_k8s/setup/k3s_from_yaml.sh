#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
HELPER="${SCRIPT_DIR}/k3s_config.py"
if [ -f "${SCRIPT_DIR}/ansible/k3s-install.yml" ]; then
    PLAYBOOK_PATH="${SCRIPT_DIR}/ansible/k3s-install.yml"
else
    PLAYBOOK_PATH="${REPO_ROOT}/ansible/k3s-install.yml"
fi
INPUT_PATH="${1:-}"
SOURCE_KIND=""
NODES_TSV=""
CONFIG_PATH=""
AUTO_NODES_TSV=""

# Public bootstrap images are prepared on the host and then copied into VMs.
# Do not make fresh VMs pull these images from the public internet during setup.
HOST_IMAGE_CACHE_DIR="${SCRIPT_DIR}/image-cache"
HOST_DOCKER_IO_MIRROR="docker.m.daocloud.io"
REGISTRY_BOOTSTRAP_IMAGE="registry:2"
MULTUS_BOOTSTRAP_IMAGE="ghcr.io/k8snetworkplumbingwg/multus-cni:snapshot"
K3S_SYSTEM_BOOTSTRAP_IMAGES=(
    "rancher/mirrored-coredns-coredns:1.10.1"
    "rancher/mirrored-metrics-server:v0.6.3"
    "rancher/local-path-provisioner:v0.0.24"
)
SEED_EMULATOR_DOCKER_DIR="/home/lxl/seed-emulator/docker_images/multiarch"
SEED_BASE_SOURCE_IMAGE="handsonsecurity/seedemu-multiarch-base:buildx-latest"
SEED_ROUTER_SOURCE_IMAGE="handsonsecurity/seedemu-multiarch-router:buildx-latest"
SEED_BASE_HASH_IMAGE="98a2693c996c2294358552f48373498d:latest"
SEED_ROUTER_HASH_IMAGE="39e016aa9e819f203ebc1809245a5818:latest"
SEED_UBUNTU_BUILD_IMAGE="ubuntu:20.04"

usage() {
    cat <<EOF
Usage:
  $0
      Auto-discover all existing libvirt VMs from the default network leases.

  $0 <resolved-nodes.tsv>
      Build a K3s cluster from the nodes recorded in a KVM resolved plan.

  $0 <k3s.yaml>
      Build a K3s cluster from an explicit K3s/Vagrant-style YAML.

The selected node set must contain exactly one master. Roles are read from TSV/YAML
when present; otherwise names containing "master" become master and all others
become workers.
EOF
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "Missing required command: $1" >&2
        exit 1
    }
}

run_with_timeout() {
    local duration="$1"
    shift
    if command -v timeout >/dev/null 2>&1; then
        timeout "${duration}" "$@"
    else
        "$@"
    fi
}

cleanup_tmp() {
    [ -n "${AUTO_NODES_TSV}" ] && rm -f "${AUTO_NODES_TSV}" || true
}

resolve_input() {
    if [ -z "${INPUT_PATH}" ]; then
        SOURCE_KIND="auto"
        AUTO_NODES_TSV="$(mktemp "${SCRIPT_DIR}/k3s-auto-nodes.XXXXXX.tsv")"
        collect_all_existing_vms "${AUTO_NODES_TSV}"
        NODES_TSV="${AUTO_NODES_TSV}"
        return
    fi

    if [ "${INPUT_PATH}" = "-h" ] || [ "${INPUT_PATH}" = "--help" ]; then
        usage
        exit 0
    fi

    if [[ "${INPUT_PATH}" == *.tsv ]]; then
        SOURCE_KIND="tsv"
        NODES_TSV="${INPUT_PATH}"
        return
    fi

    SOURCE_KIND="yaml"
    CONFIG_PATH="${INPUT_PATH}"
}

collect_all_existing_vms() {
    local output="$1"
    local domains leases
    domains="$(mktemp "${SCRIPT_DIR}/k3s-domains.XXXXXX")"
    leases="$(mktemp "${SCRIPT_DIR}/k3s-leases.XXXXXX")"
    virsh list --all --name | awk 'NF {print $1}' > "${domains}"
    virsh net-dhcp-leases default 2>/dev/null | awk '
        NR <= 2 {next}
        NF >= 6 {
          ip=$5
          sub("/.*", "", ip)
          print $6 "\t" ip "\t" tolower($3)
        }
    ' > "${leases}" || true
    awk -F '\t' '
        NR == FNR {domain[$1]=1; next}
        $1 in domain {
          role = (tolower($1) ~ /master|control-plane/) ? "master" : "worker"
          print $1 "\t" role "\t" $2 "\t" $3 "\t0\t0\t0"
          seen[$1]=1
        }
        END {
          for (name in domain) {
            if (!(name in seen)) {
              print "Missing DHCP lease for VM: " name > "/dev/stderr"
              missing=1
            }
          }
          if (missing) exit 1
        }
    ' "${domains}" "${leases}" > "${output}"
    rm -f "${domains}" "${leases}"
}

helper() {
    local command="$1"
    shift
    local args=()
    if [ -n "${NODES_TSV}" ]; then
        args=(--nodes-tsv "${NODES_TSV}")
    else
        args=(--config "${CONFIG_PATH}")
    fi
    python3 "${HELPER}" "${command}" "${args[@]}" "$@"
}

load_env() {
    local env_output
    if ! env_output="$(helper env)"; then
        return 1
    fi
    eval "${env_output}"
    SSH_OPTS=(
        -i "${SEED_K3S_SSH_KEY}"
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
}

print_plan() {
    echo "K3s input source: ${SOURCE_KIND}"
    if [ -n "${NODES_TSV}" ]; then
        echo "nodes_tsv=${NODES_TSV}"
    else
        echo "config=${CONFIG_PATH}"
    fi
    echo "K3s node plan:"
    helper nodes-tsv | awk -F '\t' '{printf "  %-24s role=%-6s ip=%-15s mac=%s\n", $1, $2, $3, $4}'
    echo "master=${SEED_K3S_MASTER_NAME} (${SEED_K3S_MASTER_IP})"
    echo "kubeconfig=${SEED_OUTPUT_KUBECONFIG}"
}

verify_connectivity() {
    echo "[1/9] Verifying SSH and sudo on all nodes"
    while IFS=$'\t' read -r name role ip mac vcpus memory_mb disk_gb; do
        echo "  ${name} ${ip}"
        run_with_timeout 12s ssh -n "${SSH_OPTS[@]}" "${SEED_K3S_USER}@${ip}" "echo ssh-ok" >/dev/null
        run_with_timeout 12s ssh -n "${SSH_OPTS[@]}" "${SEED_K3S_USER}@${ip}" "sudo -n true" >/dev/null
    done < <(helper nodes-tsv)
}

run_ansible_install() {
    echo "[2/9] Installing K3s via generated Ansible inventory"
    local inventory_tmp playbook_tmp node_count
    mkdir -p "${SEED_SETUP_TMP_DIR}"
    inventory_tmp="$(mktemp "${SEED_SETUP_TMP_DIR}/ansible-inventory.XXXXXX.yml")"
    playbook_tmp="$(mktemp "${SEED_SETUP_TMP_DIR}/k3s-install.XXXXXX.yml")"
    helper write-ansible-inventory --output "${inventory_tmp}" >/dev/null
    node_count="$(helper nodes-tsv | wc -l | tr -d ' ')"
    sed "s/ready_nodes.stdout | int >= 3/ready_nodes.stdout | int >= ${node_count}/" \
        "${PLAYBOOK_PATH}" > "${playbook_tmp}"
    ANSIBLE_HOST_KEY_CHECKING=False \
        run_with_timeout "${SEED_ANSIBLE_TIMEOUT:-3600s}" \
        ansible-playbook \
        -i "${inventory_tmp}" \
        "${playbook_tmp}" \
        --ssh-common-args="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o BatchMode=yes -o IdentitiesOnly=yes -o IdentityAgent=none"
    rm -f "${inventory_tmp}" "${playbook_tmp}"
}

image_tar_name() {
    printf '%s\n' "$1" | sed 's|[^A-Za-z0-9_.-]|_|g'
}

docker_io_mirror_ref() {
    local image="$1"
    local mirror_ref="${HOST_DOCKER_IO_MIRROR}"

    if [[ "${image}" == */*/* ]]; then
        return 1
    fi

    if [[ "${image}" == */* ]]; then
        printf '%s/%s\n' "${mirror_ref}" "${image}"
    else
        printf '%s/library/%s\n' "${mirror_ref}" "${image}"
    fi
}

ensure_host_docker_image() {
    local image="$1"
    local mirror_image=""

    if docker image inspect "${image}" >/dev/null 2>&1; then
        return 0
    fi

    echo "  host docker pull ${image}" >&2
    if docker pull "${image}" >/dev/null; then
        return 0
    fi

    if mirror_image="$(docker_io_mirror_ref "${image}")"; then
        echo "  host docker pull ${mirror_image}" >&2
        docker pull "${mirror_image}" >/dev/null
        docker tag "${mirror_image}" "${image}" >/dev/null
        return 0
    fi

    echo "Failed to prepare image on host: ${image}" >&2
    echo "This script intentionally does not ask the VM to pull public images." >&2
    return 1
}

host_image_tarball() {
    local image="$1"
    mkdir -p "${HOST_IMAGE_CACHE_DIR}"
    printf '%s/%s.tar\n' "${HOST_IMAGE_CACHE_DIR}" "$(image_tar_name "${image}")"
}

save_host_image_tarball() {
    local image="$1"
    local tar_path
    tar_path="$(host_image_tarball "${image}")"
    ensure_host_docker_image "${image}"
    echo "  host docker save ${image} -> ${tar_path}" >&2
    docker save -o "${tar_path}" "${image}"
    printf '%s\n' "${tar_path}"
}

load_docker_image_to_master() {
    local image="$1"
    local tar_path remote_tar
    tar_path="$(save_host_image_tarball "${image}")"
    remote_tar="/tmp/$(basename "${tar_path}")"
    echo "  copy ${image} to ${SEED_K3S_MASTER_NAME}:${remote_tar}"
    scp "${SSH_OPTS[@]}" "${tar_path}" "${SEED_K3S_USER}@${SEED_K3S_MASTER_IP}:${remote_tar}" >/dev/null
    run_with_timeout 180s ssh "${SSH_OPTS[@]}" "${SEED_K3S_USER}@${SEED_K3S_MASTER_IP}" \
        "sudo -n docker load -i '${remote_tar}' >/dev/null && rm -f '${remote_tar}'"
}

import_k3s_image_to_node() {
    local image="$1"
    local node_name="$2"
    local node_ip="$3"
    local tar_path remote_tar
    tar_path="$(save_host_image_tarball "${image}")"
    remote_tar="/tmp/$(basename "${tar_path}")"
    echo "  import ${image} to ${node_name}"
    scp "${SSH_OPTS[@]}" "${tar_path}" "${SEED_K3S_USER}@${node_ip}:${remote_tar}" >/dev/null
    run_with_timeout 180s ssh -n "${SSH_OPTS[@]}" "${SEED_K3S_USER}@${node_ip}" \
        "sudo -n k3s ctr -n k8s.io images import '${remote_tar}' >/dev/null && rm -f '${remote_tar}'"
}

ensure_registry() {
    echo "[3/9] Ensuring private registry on master"
    echo "  preparing ${REGISTRY_BOOTSTRAP_IMAGE} on host and loading it into master Docker"
    load_docker_image_to_master "${REGISTRY_BOOTSTRAP_IMAGE}"
    run_with_timeout 180s ssh "${SSH_OPTS[@]}" "${SEED_K3S_USER}@${SEED_K3S_MASTER_IP}" "
        set -euo pipefail
        if ! docker buildx version >/dev/null 2>&1; then
            sudo -n apt-get update >/dev/null
            sudo -n apt-get install -y docker-buildx >/dev/null
        fi
        sudo -n docker rm -f registry >/dev/null 2>&1 || true
        sudo -n docker image inspect '${REGISTRY_BOOTSTRAP_IMAGE}' >/dev/null
        sudo -n docker run -d --network host --restart=always --name registry \
            -e REGISTRY_HTTP_ADDR=0.0.0.0:${SEED_REGISTRY_PORT} '${REGISTRY_BOOTSTRAP_IMAGE}' >/dev/null
    "
}

ensure_seedemu_host_build_images() {
    ensure_host_docker_image "${SEED_UBUNTU_BUILD_IMAGE}"

    if ! docker image inspect "${SEED_BASE_SOURCE_IMAGE}" >/dev/null 2>&1; then
        echo "  host docker build ${SEED_BASE_SOURCE_IMAGE}" >&2
        DOCKER_BUILDKIT=1 docker build -t "${SEED_BASE_SOURCE_IMAGE}" \
            "${SEED_EMULATOR_DOCKER_DIR}/seedemu-base" >/dev/null
    fi

    if ! docker image inspect "${SEED_ROUTER_SOURCE_IMAGE}" >/dev/null 2>&1; then
        echo "  host docker build ${SEED_ROUTER_SOURCE_IMAGE}" >&2
        DOCKER_BUILDKIT=1 docker build -t "${SEED_ROUTER_SOURCE_IMAGE}" \
            "${SEED_EMULATOR_DOCKER_DIR}/seedemu-router" >/dev/null
    fi

    docker tag "${SEED_BASE_SOURCE_IMAGE}" "${SEED_BASE_HASH_IMAGE}" >/dev/null
    docker tag "${SEED_ROUTER_SOURCE_IMAGE}" "${SEED_ROUTER_HASH_IMAGE}" >/dev/null
}

prepare_master_workload_build_images() {
    echo "[4/9] Preparing workload build base images on master Docker"
    [ -d "${SEED_EMULATOR_DOCKER_DIR}/seedemu-base" ] || {
        echo "Missing host seedemu base image directory: ${SEED_EMULATOR_DOCKER_DIR}/seedemu-base" >&2
        exit 1
    }
    [ -d "${SEED_EMULATOR_DOCKER_DIR}/seedemu-router" ] || {
        echo "Missing host seedemu router image directory: ${SEED_EMULATOR_DOCKER_DIR}/seedemu-router" >&2
        exit 1
    }

    ensure_seedemu_host_build_images
    load_docker_image_to_master "${SEED_UBUNTU_BUILD_IMAGE}"
    load_docker_image_to_master "${SEED_BASE_SOURCE_IMAGE}"
    load_docker_image_to_master "${SEED_ROUTER_SOURCE_IMAGE}"

    echo "  ensuring stable compiler hash tags on master Docker"
    run_with_timeout 120s ssh "${SSH_OPTS[@]}" "${SEED_K3S_USER}@${SEED_K3S_MASTER_IP}" "
        set -euo pipefail
        sudo -n docker tag '${SEED_BASE_SOURCE_IMAGE}' '${SEED_BASE_HASH_IMAGE}'
        sudo -n docker tag '${SEED_ROUTER_SOURCE_IMAGE}' '${SEED_ROUTER_HASH_IMAGE}'
    "

    echo "  pushing seedemu base/router images into master local registry"
    run_with_timeout 600s ssh "${SSH_OPTS[@]}" "${SEED_K3S_USER}@${SEED_K3S_MASTER_IP}" "
        set -euo pipefail
        sudo -n docker tag '${SEED_BASE_SOURCE_IMAGE}' \
            '127.0.0.1:${SEED_REGISTRY_PORT}/${SEED_BASE_SOURCE_IMAGE}'
        sudo -n docker push '127.0.0.1:${SEED_REGISTRY_PORT}/${SEED_BASE_SOURCE_IMAGE}'
        sudo -n docker tag '${SEED_ROUTER_SOURCE_IMAGE}' \
            '127.0.0.1:${SEED_REGISTRY_PORT}/${SEED_ROUTER_SOURCE_IMAGE}'
        sudo -n docker push '127.0.0.1:${SEED_REGISTRY_PORT}/${SEED_ROUTER_SOURCE_IMAGE}'
    "
}

fetch_kubeconfig() {
    echo "[5/9] Fetching kubeconfig"
    mkdir -p "$(dirname "${SEED_OUTPUT_KUBECONFIG}")"
    run_with_timeout 30s ssh "${SSH_OPTS[@]}" "${SEED_K3S_USER}@${SEED_K3S_MASTER_IP}" \
        "sudo -n cat /etc/rancher/k3s/k3s.yaml" > "${SEED_OUTPUT_KUBECONFIG}"
    sed -i "s|127.0.0.1|${SEED_K3S_MASTER_IP}|g" "${SEED_OUTPUT_KUBECONFIG}"
    echo "kubeconfig=${SEED_OUTPUT_KUBECONFIG}"
}

preload_k3s_bootstrap_images_all_nodes() {
    echo "[6/9] Preloading K3s system and Multus images from host into all K3s containerd nodes"
    while IFS=$'\t' read -r name role ip mac vcpus memory_mb disk_gb; do
        for image in "${K3S_SYSTEM_BOOTSTRAP_IMAGES[@]}" "${MULTUS_BOOTSTRAP_IMAGE}"; do
            import_k3s_image_to_node "${image}" "${name}" "${ip}"
        done
    done < <(helper nodes-tsv)
    kubectl --kubeconfig "${SEED_OUTPUT_KUBECONFIG}" -n kube-system delete pod -l k8s-app=kube-dns \
        --force --grace-period=0 --wait=false >/dev/null 2>&1 || true
    kubectl --kubeconfig "${SEED_OUTPUT_KUBECONFIG}" -n kube-system delete pod -l k8s-app=metrics-server \
        --force --grace-period=0 --wait=false >/dev/null 2>&1 || true
    kubectl --kubeconfig "${SEED_OUTPUT_KUBECONFIG}" -n kube-system delete pod -l app=local-path-provisioner \
        --force --grace-period=0 --wait=false >/dev/null 2>&1 || true
    kubectl --kubeconfig "${SEED_OUTPUT_KUBECONFIG}" -n kube-system delete pod -l name=multus \
        --force --grace-period=0 --wait=false >/dev/null 2>&1 || true
}

apply_node_k3s_tuning() {
    local name="$1"
    local ip="$2"
    echo "  tuning K3s on ${name} (${ip})"
    ssh "${SSH_OPTS[@]}" "${SEED_K3S_USER}@${ip}" "sudo -n bash -s" -- \
        "${SEED_K3S_MAX_PODS}" \
        "${SEED_KUBELET_REGISTRY_QPS}" \
        "${SEED_KUBELET_REGISTRY_BURST}" \
        "${SEED_REBOOT_AFTER_TUNING}" <<'EOF_REMOTE'
set -euo pipefail
MAX_PODS="$1"
REGISTRY_QPS="$2"
REGISTRY_BURST="$3"
ASYNC_RESTART="$4"

if [ ! -f /etc/sysctl.d/99-seed-vm-limits.conf ]; then
    echo "warning: /etc/sysctl.d/99-seed-vm-limits.conf not found; run unlock_vm_limits_from_yaml.sh before large-scale experiments" >&2
fi

mkdir -p /etc/rancher/k3s
touch /etc/rancher/k3s/config.yaml
cfg=/etc/rancher/k3s/config.yaml
tmp=$(mktemp)
awk '
  /^[^[:space:]].*:/ {
    if ($0 ~ /^(kubelet-arg|kube-apiserver-arg):/) {skip=1; next}
    skip=0
  }
  skip == 0 {print}
' "${cfg}" > "${tmp}"
cat >> "${tmp}" <<EOF_K3S
kubelet-arg:
  - "max-pods=${MAX_PODS}"
  - "kube-api-qps=50"
  - "kube-api-burst=100"
  - "registry-qps=${REGISTRY_QPS}"
  - "registry-burst=${REGISTRY_BURST}"
EOF_K3S
if systemctl list-unit-files | grep -q "^k3s.service"; then
    cat >> "${tmp}" <<'EOF_MASTER'
kube-apiserver-arg:
  - "max-requests-inflight=1000"
  - "max-mutating-requests-inflight=500"
EOF_MASTER
fi
cat "${tmp}" > "${cfg}"
rm -f "${tmp}"

if [ "${ASYNC_RESTART}" = "true" ]; then
    systemd-run --on-active=2 /bin/bash -c "systemctl restart k3s 2>/dev/null || systemctl restart k3s-agent 2>/dev/null || true; systemctl restart containerd 2>/dev/null || true" >/dev/null 2>&1 || true
else
    systemctl restart k3s 2>/dev/null || systemctl restart k3s-agent 2>/dev/null || true
    systemctl restart containerd 2>/dev/null || true
fi
EOF_REMOTE
}

apply_tuning_all_nodes() {
    echo "[7/9] Applying K3s runtime tuning"
    while IFS=$'\t' read -r name role ip mac vcpus memory_mb disk_gb; do
        apply_node_k3s_tuning "${name}" "${ip}"
    done < <(helper nodes-tsv)
}

verify_cluster() {
    echo "[8/9] Waiting for K3s nodes"
    kubectl --kubeconfig "${SEED_OUTPUT_KUBECONFIG}" wait --for=condition=Ready node --all --timeout=300s
    kubectl --kubeconfig "${SEED_OUTPUT_KUBECONFIG}" -n kube-system rollout status daemonset/kube-multus-ds --timeout=300s
    kubectl --kubeconfig "${SEED_OUTPUT_KUBECONFIG}" get nodes -o wide
    kubectl --kubeconfig "${SEED_OUTPUT_KUBECONFIG}" get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\tPodCIDR: "}{.spec.podCIDR}{"\n"}{end}'
    echo
}

write_outputs() {
    echo "[9/9] Writing inventory/env outputs"
    helper write-cluster-inventory >/dev/null
    helper write-env-file >/dev/null
    echo "inventory=${SEED_OUTPUT_INVENTORY}"
    echo "env_file=${SEED_OUTPUT_ENV_FILE}"
    echo "kubeconfig=${SEED_OUTPUT_KUBECONFIG}"
}

main() {
    require_cmd python3
    require_cmd ansible-playbook
    require_cmd docker
    require_cmd scp
    require_cmd ssh
    require_cmd kubectl
    require_cmd sed
    require_cmd virsh
    [ -f "${PLAYBOOK_PATH}" ] || {
        echo "K3s Ansible playbook not found: ${PLAYBOOK_PATH}" >&2
        exit 1
    }

    trap cleanup_tmp EXIT
    resolve_input
    load_env

    [ -f "${SEED_K3S_SSH_KEY}" ] || {
        echo "SSH key not found: ${SEED_K3S_SSH_KEY}" >&2
        exit 1
    }

    print_plan
    verify_connectivity
    run_ansible_install
    ensure_registry
    prepare_master_workload_build_images
    fetch_kubeconfig
    preload_k3s_bootstrap_images_all_nodes
    apply_tuning_all_nodes
    verify_cluster
    write_outputs
    echo "K3s cluster is ready."
}

main "$@"
