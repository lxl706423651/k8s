#!/usr/bin/env bash
# Build a K3s cluster from configK3s.yaml. The script installs K3s with the
# bundled Ansible playbook, creates the master registry, imports bootstrap
# images, writes kubeconfig/inventory outputs, and validates node readiness.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
HELPER="${SCRIPT_DIR}/manageK3sConfig.py"
if [ -f "${SCRIPT_DIR}/ansible/k3s-install.yml" ]; then
    PLAYBOOK_PATH="${SCRIPT_DIR}/ansible/k3s-install.yml"
else
    PLAYBOOK_PATH="${REPO_ROOT}/ansible/k3s-install.yml"
fi
CONFIG_PATH="${1:-${SCRIPT_DIR}/configK3s.yaml}"
ansibleTimeout="3600s"

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
seedEmulatorDockerDir="${HOME}/seed-emulator/docker_images/multiarch"
seedBaseSourceImage="handsonsecurity/seedemu-multiarch-base:buildx-latest"
seedRouterSourceImage="handsonsecurity/seedemu-multiarch-router:buildx-latest"
seedBaseHashImage="98a2693c996c2294358552f48373498d:latest"
seedRouterHashImage="39e016aa9e819f203ebc1809245a5818:latest"
ubuntuBuildImage="ubuntu:20.04"

usage() {
    cat <<EOF
Usage: $0 [configK3s.yaml]

Build a K3s cluster from configK3s.yaml. The YAML must contain a nodes list.
Each node should provide role, ip, and ssh.user/key; missing names are
normalized by manageK3sConfig.py.
EOF
}

requireCommand() {
    command -v "$1" >/dev/null 2>&1 || {
        echo "Missing required command: $1" >&2
        exit 1
    }
}

runWithTimeout() {
    local duration="$1"
    shift
    if command -v timeout >/dev/null 2>&1; then
        timeout "${duration}" "$@"
    else
        "$@"
    fi
}

resolveInput() {
    if [ "${CONFIG_PATH}" = "-h" ] || [ "${CONFIG_PATH}" = "--help" ]; then
        usage
        exit 0
    fi
    if [ ! -s "${CONFIG_PATH}" ]; then
        echo "configK3s.yaml not found or empty: ${CONFIG_PATH}" >&2
        exit 1
    fi
}

helper() {
    local command="$1"
    shift
    python3 "${HELPER}" --config "${CONFIG_PATH}" "${command}" "$@"
}

loadConfigVars() {
    local vars_output
    if ! vars_output="$(helper shell-vars)"; then
        return 1
    fi
    eval "${vars_output}"
    SSH_OPTS=(
        -i "${k3sSshKey}"
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

loadNodeSshContext() {
    # Args:
    #   $1: node name from configK3s.yaml.
    # Reads per-node ssh.user/key and prepares NODE_SSH_OPTS for node-specific
    # SSH/scp operations. Master-only operations keep using SSH_OPTS.
    local name="$1"
    eval "$(helper node-ssh-vars --name "${name}")"
    [ -f "${nodeSshKey}" ] || {
        echo "SSH key not found for ${name}: ${nodeSshKey}" >&2
        exit 1
    }
    NODE_SSH_OPTS=(
        -i "${nodeSshKey}"
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

printPlan() {
    echo "config=${CONFIG_PATH}"
    echo "K3s node plan:"
    helper nodes-tsv | awk -F '\t' '{printf "  %-24s role=%-6s ip=%-15s mac=%s\n", $1, $2, $3, $4}'
    echo "master=${k3sMasterName} (${k3sMasterIp})"
    echo "kubeconfig=${outputKubeconfig}"
}

verifyConnectivity() {
    echo "[1/9] Verifying SSH and sudo on all nodes"
    while IFS=$'\t' read -r name role ip mac vcpus memory_mb disk_gb; do
        echo "  ${name} ${ip}"
        loadNodeSshContext "${name}"
        runWithTimeout 12s ssh -n "${NODE_SSH_OPTS[@]}" "${nodeSshUser}@${ip}" "echo ssh-ok" >/dev/null
        runWithTimeout 12s ssh -n "${NODE_SSH_OPTS[@]}" "${nodeSshUser}@${ip}" "sudo -n true" >/dev/null
    done < <(helper nodes-tsv)
}

runAnsibleInstall() {
    echo "[2/9] Installing K3s via generated Ansible inventory"
    local inventory_tmp playbook_tmp node_count
    mkdir -p "${setupTmpDir}"
    inventory_tmp="$(mktemp "${setupTmpDir}/ansible-inventory.XXXXXX.yml")"
    playbook_tmp="$(mktemp "${setupTmpDir}/k3s-install.XXXXXX.yml")"
    helper write-ansible-inventory --output "${inventory_tmp}" >/dev/null
    node_count="$(helper nodes-tsv | wc -l | tr -d ' ')"
    sed "s/ready_nodes.stdout | int >= 3/ready_nodes.stdout | int >= ${node_count}/" \
        "${PLAYBOOK_PATH}" > "${playbook_tmp}"
    ANSIBLE_HOST_KEY_CHECKING=False \
        runWithTimeout "${ansibleTimeout}" \
        ansible-playbook \
        -i "${inventory_tmp}" \
        "${playbook_tmp}" \
        --ssh-common-args="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o BatchMode=yes -o IdentitiesOnly=yes -o IdentityAgent=none"
    rm -f "${inventory_tmp}" "${playbook_tmp}"
}

imageTarName() {
    printf '%s\n' "$1" | sed 's|[^A-Za-z0-9_.-]|_|g'
}

dockerIoMirrorRef() {
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

ensureHostDockerImage() {
    local image="$1"
    local mirror_image=""

    if docker image inspect "${image}" >/dev/null 2>&1; then
        return 0
    fi

    echo "  host docker pull ${image}" >&2
    if docker pull "${image}" >/dev/null; then
        return 0
    fi

    if mirror_image="$(dockerIoMirrorRef "${image}")"; then
        echo "  host docker pull ${mirror_image}" >&2
        docker pull "${mirror_image}" >/dev/null
        docker tag "${mirror_image}" "${image}" >/dev/null
        return 0
    fi

    echo "Failed to prepare image on host: ${image}" >&2
    echo "This script intentionally does not ask the VM to pull public images." >&2
    return 1
}

hostImageTarball() {
    local image="$1"
    mkdir -p "${HOST_IMAGE_CACHE_DIR}"
    printf '%s/%s.tar\n' "${HOST_IMAGE_CACHE_DIR}" "$(imageTarName "${image}")"
}

saveHostImageTarball() {
    local image="$1"
    local tar_path
    tar_path="$(hostImageTarball "${image}")"
    ensureHostDockerImage "${image}"
    echo "  host docker save ${image} -> ${tar_path}" >&2
    docker save -o "${tar_path}" "${image}"
    printf '%s\n' "${tar_path}"
}

loadDockerImageToMaster() {
    local image="$1"
    local tar_path remote_tar
    tar_path="$(saveHostImageTarball "${image}")"
    remote_tar="/tmp/$(basename "${tar_path}")"
    echo "  copy ${image} to ${k3sMasterName}:${remote_tar}"
    scp "${SSH_OPTS[@]}" "${tar_path}" "${k3sUser}@${k3sMasterIp}:${remote_tar}" >/dev/null
    runWithTimeout 180s ssh "${SSH_OPTS[@]}" "${k3sUser}@${k3sMasterIp}" \
        "sudo -n docker load -i '${remote_tar}' >/dev/null && rm -f '${remote_tar}'"
}

importK3sImageToNode() {
    local image="$1"
    local node_name="$2"
    local node_ip="$3"
    local tar_path remote_tar
    tar_path="$(saveHostImageTarball "${image}")"
    remote_tar="/tmp/$(basename "${tar_path}")"
    echo "  import ${image} to ${node_name}"
    loadNodeSshContext "${node_name}"
    scp "${NODE_SSH_OPTS[@]}" "${tar_path}" "${nodeSshUser}@${node_ip}:${remote_tar}" >/dev/null
    runWithTimeout 180s ssh -n "${NODE_SSH_OPTS[@]}" "${nodeSshUser}@${node_ip}" \
        "sudo -n k3s ctr -n k8s.io images import '${remote_tar}' >/dev/null && rm -f '${remote_tar}'"
}

ensureRegistry() {
    echo "[3/9] Ensuring private registry on master"
    echo "  preparing ${REGISTRY_BOOTSTRAP_IMAGE} on host and loading it into master Docker"
    loadDockerImageToMaster "${REGISTRY_BOOTSTRAP_IMAGE}"
    runWithTimeout 180s ssh "${SSH_OPTS[@]}" "${k3sUser}@${k3sMasterIp}" "
        set -euo pipefail
        if ! docker buildx version >/dev/null 2>&1; then
            sudo -n apt-get update >/dev/null
            sudo -n apt-get install -y docker-buildx >/dev/null
        fi
        sudo -n docker rm -f registry >/dev/null 2>&1 || true
        sudo -n docker image inspect '${REGISTRY_BOOTSTRAP_IMAGE}' >/dev/null
        sudo -n docker run -d --network host --restart=always --name registry \
            -e REGISTRY_HTTP_ADDR=0.0.0.0:${registryPort} '${REGISTRY_BOOTSTRAP_IMAGE}' >/dev/null
    "
}

ensureSeedemuHostBuildImages() {
    ensureHostDockerImage "${ubuntuBuildImage}"

    if ! docker image inspect "${seedBaseSourceImage}" >/dev/null 2>&1; then
        echo "  host docker build ${seedBaseSourceImage}" >&2
        DOCKER_BUILDKIT=1 docker build -t "${seedBaseSourceImage}" \
            "${seedEmulatorDockerDir}/seedemu-base" >/dev/null
    fi

    if ! docker image inspect "${seedRouterSourceImage}" >/dev/null 2>&1; then
        echo "  host docker build ${seedRouterSourceImage}" >&2
        DOCKER_BUILDKIT=1 docker build -t "${seedRouterSourceImage}" \
            "${seedEmulatorDockerDir}/seedemu-router" >/dev/null
    fi

    docker tag "${seedBaseSourceImage}" "${seedBaseHashImage}" >/dev/null
    docker tag "${seedRouterSourceImage}" "${seedRouterHashImage}" >/dev/null
}

prepareMasterWorkloadBuildImages() {
    echo "[4/9] Preparing workload build base images on master Docker"
    [ -d "${seedEmulatorDockerDir}/seedemu-base" ] || {
        echo "Missing host seedemu base image directory: ${seedEmulatorDockerDir}/seedemu-base" >&2
        exit 1
    }
    [ -d "${seedEmulatorDockerDir}/seedemu-router" ] || {
        echo "Missing host seedemu router image directory: ${seedEmulatorDockerDir}/seedemu-router" >&2
        exit 1
    }

    ensureSeedemuHostBuildImages
    loadDockerImageToMaster "${ubuntuBuildImage}"
    loadDockerImageToMaster "${seedBaseSourceImage}"
    loadDockerImageToMaster "${seedRouterSourceImage}"

    echo "  ensuring stable compiler hash tags on master Docker"
    runWithTimeout 120s ssh "${SSH_OPTS[@]}" "${k3sUser}@${k3sMasterIp}" "
        set -euo pipefail
        sudo -n docker tag '${seedBaseSourceImage}' '${seedBaseHashImage}'
        sudo -n docker tag '${seedRouterSourceImage}' '${seedRouterHashImage}'
    "

    echo "  pushing seedemu base/router images into master local registry"
    runWithTimeout 600s ssh "${SSH_OPTS[@]}" "${k3sUser}@${k3sMasterIp}" "
        set -euo pipefail
        sudo -n docker tag '${seedBaseSourceImage}' \
            '127.0.0.1:${registryPort}/${seedBaseSourceImage}'
        sudo -n docker push '127.0.0.1:${registryPort}/${seedBaseSourceImage}'
        sudo -n docker tag '${seedRouterSourceImage}' \
            '127.0.0.1:${registryPort}/${seedRouterSourceImage}'
        sudo -n docker push '127.0.0.1:${registryPort}/${seedRouterSourceImage}'
    "
}

fetchKubeconfig() {
    echo "[5/9] Fetching kubeconfig"
    mkdir -p "$(dirname "${outputKubeconfig}")"
    runWithTimeout 30s ssh "${SSH_OPTS[@]}" "${k3sUser}@${k3sMasterIp}" \
        "sudo -n cat /etc/rancher/k3s/k3s.yaml" > "${outputKubeconfig}"
    sed -i "s|127.0.0.1|${k3sMasterIp}|g" "${outputKubeconfig}"
    echo "kubeconfig=${outputKubeconfig}"
}

preloadK3sBootstrapImagesAllNodes() {
    echo "[6/9] Preloading K3s system and Multus images from host into all K3s containerd nodes"
    while IFS=$'\t' read -r name role ip mac vcpus memory_mb disk_gb; do
        for image in "${K3S_SYSTEM_BOOTSTRAP_IMAGES[@]}" "${MULTUS_BOOTSTRAP_IMAGE}"; do
            importK3sImageToNode "${image}" "${name}" "${ip}"
        done
    done < <(helper nodes-tsv)
    kubectl --kubeconfig "${outputKubeconfig}" -n kube-system delete pod -l k8s-app=kube-dns \
        --force --grace-period=0 --wait=false >/dev/null 2>&1 || true
    kubectl --kubeconfig "${outputKubeconfig}" -n kube-system delete pod -l k8s-app=metrics-server \
        --force --grace-period=0 --wait=false >/dev/null 2>&1 || true
    kubectl --kubeconfig "${outputKubeconfig}" -n kube-system delete pod -l app=local-path-provisioner \
        --force --grace-period=0 --wait=false >/dev/null 2>&1 || true
    kubectl --kubeconfig "${outputKubeconfig}" -n kube-system delete pod -l name=multus \
        --force --grace-period=0 --wait=false >/dev/null 2>&1 || true
}

applyNodeK3sTuning() {
    local name="$1"
    local ip="$2"
    echo "  tuning K3s on ${name} (${ip})"
    loadNodeSshContext "${name}"
    ssh "${NODE_SSH_OPTS[@]}" "${nodeSshUser}@${ip}" "sudo -n bash -s" -- \
        "${k3sMaxPods}" \
        "${kubeletRegistryQps}" \
        "${kubeletRegistryBurst}" \
        "${rebootAfterTuning}" <<'EOF_REMOTE'
set -euo pipefail
MAX_PODS="$1"
REGISTRY_QPS="$2"
REGISTRY_BURST="$3"
ASYNC_RESTART="$4"

if [ ! -f /etc/sysctl.d/99-seed-vm-limits.conf ]; then
    echo "warning: /etc/sysctl.d/99-seed-vm-limits.conf not found; run tuneVmLimits.sh before large-scale experiments" >&2
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

applyTuningAllNodes() {
    echo "[7/9] Applying K3s runtime tuning"
    while IFS=$'\t' read -r name role ip mac vcpus memory_mb disk_gb; do
        applyNodeK3sTuning "${name}" "${ip}"
    done < <(helper nodes-tsv)
}

verifyCluster() {
    echo "[8/9] Waiting for K3s nodes"
    kubectl --kubeconfig "${outputKubeconfig}" wait --for=condition=Ready node --all --timeout=300s
    kubectl --kubeconfig "${outputKubeconfig}" -n kube-system rollout status daemonset/kube-multus-ds --timeout=300s
    kubectl --kubeconfig "${outputKubeconfig}" get nodes -o wide
    kubectl --kubeconfig "${outputKubeconfig}" get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\tPodCIDR: "}{.spec.podCIDR}{"\n"}{end}'
    echo
}

writeOutputs() {
    echo "[9/9] Writing inventory output"
    helper write-cluster-inventory >/dev/null
    echo "inventory=${outputInventory}"
    echo "kubeconfig=${outputKubeconfig}"
    echo "registry=${registryHost}:${registryPort}"
}

main() {
    requireCommand python3
    requireCommand ansible-playbook
    requireCommand docker
    requireCommand scp
    requireCommand ssh
    requireCommand kubectl
    requireCommand sed
    requireCommand virsh
    [ -f "${PLAYBOOK_PATH}" ] || {
        echo "K3s Ansible playbook not found: ${PLAYBOOK_PATH}" >&2
        exit 1
    }

    resolveInput
    loadConfigVars

    [ -f "${k3sSshKey}" ] || {
        echo "SSH key not found: ${k3sSshKey}" >&2
        exit 1
    }

    printPlan
    verifyConnectivity
    runAnsibleInstall
    ensureRegistry
    prepareMasterWorkloadBuildImages
    fetchKubeconfig
    preloadK3sBootstrapImagesAllNodes
    applyTuningAllNodes
    verifyCluster
    writeOutputs
    echo "K3s cluster is ready."
}

main "$@"
