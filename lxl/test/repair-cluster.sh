#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

export KUBECONFIG="${REPO_ROOT}/output/kubeconfigs/${SEED_K3S_CLUSTER_NAME}.yaml"
seed_load_cluster_nodes
ensure_kubeconfig

MASTER_REPAIR_MODE="auto"
WAIT_IP_SECONDS="${WAIT_IP_SECONDS:-180}"
WAIT_SSH_SECONDS="${WAIT_SSH_SECONDS:-180}"
WAIT_READY_SECONDS="${WAIT_READY_SECONDS:-300}"
WITH_BASE_IMAGES=0
NAMESPACE_WAIT_SECONDS="${NAMESPACE_WAIT_SECONDS:-30}"
NAMESPACE_ONLY=0
CLEANUP_NAMESPACE=""
REPAIR_STATE_DB=0
STATE_DB_REPAIR_SCRIPT_LOCAL="${LXL_DIR}/repair_k3s_state_db_seedemu_namespace.py"
STATE_DB_REPAIR_SCRIPT_REMOTE="/tmp/repair_k3s_state_db_seedemu_namespace.py"

usage() {
    cat <<EOF
Usage:
  $(basename "$0") [node_name ...]
  $(basename "$0") --all-workers
  $(basename "$0") --master-mode auto|registry-only|full-init [node_name ...]
  $(basename "$0") --with-base-images [node_name ...]
  $(basename "$0") --cleanup-namespace [namespace] --namespace-only
  $(basename "$0") --cleanup-namespace [namespace] --repair-state-db --namespace-only

Behavior:
  1. Repairs master Docker/registry via 14_repair_master_docker_registry.sh.
  2. Repairs workers selected by arguments.
  3. If no worker names are provided, only NotReady workers are repaired.
  4. Optionally cleans a stale experiment namespace and its residual resources.
  5. Optionally purges stale namespace rows from master state.db/kine.

Examples:
  $(basename "$0")
  $(basename "$0") seed-k3s-worker2 seed-k3s-worker4
  $(basename "$0") --all-workers
  $(basename "$0") --cleanup-namespace --namespace-only
  $(basename "$0") --cleanup-namespace --repair-state-db --namespace-only
EOF
}

TARGET_NAMES=()
REPAIR_ALL_WORKERS=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --all-workers)
            REPAIR_ALL_WORKERS=1
            shift
            ;;
        --master-mode)
            MASTER_REPAIR_MODE="${2:-}"
            shift 2
            ;;
        --with-base-images)
            WITH_BASE_IMAGES=1
            shift
            ;;
        --cleanup-namespace)
            if [ "$#" -ge 2 ] && [[ ! "${2}" =~ ^-- ]]; then
                CLEANUP_NAMESPACE="$2"
                shift 2
            else
                CLEANUP_NAMESPACE="${SEED_NAMESPACE}"
                shift
            fi
            ;;
        --namespace-only)
            NAMESPACE_ONLY=1
            shift
            ;;
        --repair-state-db)
            REPAIR_STATE_DB=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            TARGET_NAMES+=("$1")
            shift
            ;;
    esac
done

case "${MASTER_REPAIR_MODE}" in
    auto|registry-only|full-init) ;;
    *)
        echo "Invalid --master-mode: ${MASTER_REPAIR_MODE}" >&2
        exit 2
        ;;
esac

ts() { date +"%F %T"; }
log() { printf '[repair] %s %s\n' "$(ts)" "$*"; }
warn() { printf '[repair][warn] %s %s\n' "$(ts)" "$*" >&2; }

SSH_OPTS=(
  -i "${SEED_K3S_SSH_KEY}"
  -o BatchMode=yes
  -o ConnectTimeout=10
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
  -o LogLevel=ERROR
)

remote_master() {
    ssh "${SSH_OPTS[@]}" "${SEED_K3S_USER}@${SEED_MASTER_NODE_IP}" "$@"
}

copy_to_master() {
    local local_path="$1"
    local remote_path="$2"
    scp -q "${SSH_OPTS[@]}" "${local_path}" "${SEED_K3S_USER}@${SEED_MASTER_NODE_IP}:${remote_path}"
}

node_ip_for_name() {
    local target="$1"
    local i
    for i in "${!SEED_NODE_NAMES[@]}"; do
        if [ "${SEED_NODE_NAMES[$i]}" = "${target}" ]; then
            printf '%s\n' "${SEED_NODE_IPS[$i]}"
            return 0
        fi
    done
    return 1
}

ssh_ready() {
    local ip="$1"
    ssh_node "${ip}" "true" >/dev/null 2>&1
}

wait_for_vm_ip() {
    local vm_name="$1"
    local deadline ip_line
    deadline=$(( $(date +%s) + WAIT_IP_SECONDS ))
    while true; do
        ip_line="$(sudo -n virsh domifaddr "${vm_name}" 2>/dev/null | awk '/ipv4/ {print $4; exit}')"
        if [ -n "${ip_line}" ]; then
            return 0
        fi
        if [ "$(date +%s)" -ge "${deadline}" ]; then
            return 1
        fi
        sleep 5
    done
}

wait_for_ssh() {
    local ip="$1"
    local deadline
    deadline=$(( $(date +%s) + WAIT_SSH_SECONDS ))
    while true; do
        if ssh_ready "${ip}"; then
            return 0
        fi
        if [ "$(date +%s)" -ge "${deadline}" ]; then
            return 1
        fi
        sleep 5
    done
}

restart_worker_services() {
    local node_name="$1"
    local node_ip="$2"
    log "${node_name}: restarting k3s-agent"
    ssh_node "${node_ip}" "sudo -n systemctl restart k3s-agent && systemctl is-active k3s-agent && sudo -n journalctl -u k3s-agent -n 15 --no-pager" || return 1
}

recover_worker() {
    local node_name="$1"
    local node_ip="$2"

    log "${node_name}: begin recovery (ip=${node_ip})"

    if ! ssh_ready "${node_ip}"; then
        warn "${node_name}: SSH unavailable, rebooting VM"
        sudo -n virsh reboot "${node_name}" >/dev/null 2>&1 || true
        if ! wait_for_vm_ip "${node_name}"; then
            warn "${node_name}: no IP after reboot, forcing destroy/start"
            sudo -n virsh destroy "${node_name}" >/dev/null 2>&1 || true
            sleep 2
            sudo -n virsh start "${node_name}" >/dev/null
            wait_for_vm_ip "${node_name}" || {
                warn "${node_name}: still no IP after destroy/start"
                return 1
            }
        fi
        wait_for_ssh "${node_ip}" || {
            warn "${node_name}: SSH did not recover after VM restart"
            return 1
        }
    fi

    restart_worker_services "${node_name}" "${node_ip}" || {
        warn "${node_name}: failed to restart/verify k3s-agent"
        return 1
    }

    log "${node_name}: recovery completed"
}

notready_workers() {
    kubectl get nodes --no-headers 2>/dev/null | awk '$2 != "Ready" && $1 != "seed-k3s-master" {print $1}'
}

wait_cluster_ready() {
    local deadline
    deadline=$(( $(date +%s) + WAIT_READY_SECONDS ))
    while true; do
        if kubectl get nodes --no-headers >/tmp/test_repair_nodes.out 2>/tmp/test_repair_nodes.err; then
            local not_ready
            not_ready="$(awk '$2 != "Ready" {c++} END{print c+0}' /tmp/test_repair_nodes.out)"
            if [ "${not_ready}" -eq 0 ]; then
                cat /tmp/test_repair_nodes.out
                rm -f /tmp/test_repair_nodes.out /tmp/test_repair_nodes.err
                return 0
            fi
        fi
        if [ "$(date +%s)" -ge "${deadline}" ]; then
            warn "cluster did not become fully Ready within ${WAIT_READY_SECONDS}s"
            [ -f /tmp/test_repair_nodes.out ] && cat /tmp/test_repair_nodes.out || true
            [ -f /tmp/test_repair_nodes.err ] && cat /tmp/test_repair_nodes.err >&2 || true
            rm -f /tmp/test_repair_nodes.out /tmp/test_repair_nodes.err
            return 1
        fi
        sleep 5
    done
}

namespace_exists() {
    local ns="$1"
    kubectl get namespace "${ns}" >/dev/null 2>&1
}

namespaced_kind_count_by_namespace() {
    local kind="$1"
    local ns="$2"
    kubectl get "${kind}" -A -o json 2>/dev/null \
        | python3 -c 'import json,sys; ns=sys.argv[1]; data=json.load(sys.stdin); print(sum(1 for item in data.get("items", []) if item.get("metadata", {}).get("namespace")==ns))' "${ns}"
}

namespace_resource_summary() {
    local ns="$1"
    local total=0
    local kinds=(
        pods
        deployments.apps
        statefulsets.apps
        daemonsets.apps
        jobs.batch
        services
        serviceaccounts
        configmaps
        secrets
        roles.rbac.authorization.k8s.io
        rolebindings.rbac.authorization.k8s.io
        network-attachment-definitions.k8s.cni.cncf.io
    )
    local kind count
    for kind in "${kinds[@]}"; do
        count="$(namespaced_kind_count_by_namespace "${kind}" "${ns}")"
        total=$((total + count))
    done
    printf '%s\n' "${total}"
}

wait_namespace_gone() {
    local ns="$1"
    local deadline
    deadline=$(( $(date +%s) + NAMESPACE_WAIT_SECONDS ))
    while namespace_exists "${ns}"; do
        if [ "$(date +%s)" -ge "${deadline}" ]; then
            return 1
        fi
        sleep 5
    done
    return 0
}

clear_namespace_finalizers() {
    local ns="$1"
    kubectl get namespace "${ns}" -o json 2>/dev/null \
        | python3 -c 'import json,sys; data=json.load(sys.stdin); data.get("metadata", {}).pop("managedFields", None); data.setdefault("spec", {})["finalizers"]=[]; print(json.dumps(data))' \
        | kubectl replace --raw "/api/v1/namespaces/${ns}/finalize" -f - >/dev/null 2>&1 || true
}

cleanup_namespace_resources() {
    local ns="$1"
    kubectl -n "${ns}" delete network-attachment-definitions.k8s.cni.cncf.io --all --ignore-not-found --timeout=60s >/dev/null 2>&1 || true
    kubectl -n "${ns}" delete deploy,statefulset,daemonset,job,pod,svc,sa,cm,secret,role,rolebinding --all --ignore-not-found --timeout=60s >/dev/null 2>&1 || true
}

delete_kind_by_namespace() {
    local kind="$1"
    local ns="$2"
    kubectl get "${kind}" -A -o json 2>/dev/null \
        | python3 -c 'import json,sys; ns=sys.argv[1]; data=json.load(sys.stdin); [print(item.get("metadata", {}).get("name")) for item in data.get("items", []) if item.get("metadata", {}).get("namespace")==ns and item.get("metadata", {}).get("name")]' "${ns}" \
        | xargs -r -n 100 kubectl -n "${ns}" delete "${kind}" --ignore-not-found --timeout=30s >/dev/null 2>&1 || true
}

cleanup_orphan_namespace_resources() {
    local ns="$1"
    local kinds=(
        network-attachment-definitions.k8s.cni.cncf.io
        deployments.apps
        statefulsets.apps
        daemonsets.apps
        jobs.batch
        pods
        services
        serviceaccounts
        configmaps
        secrets
        roles.rbac.authorization.k8s.io
        rolebindings.rbac.authorization.k8s.io
    )
    local kind
    for kind in "${kinds[@]}"; do
        delete_kind_by_namespace "${kind}" "${ns}"
    done
}

state_db_namespace_rows() {
    local ns="$1"
    remote_master "sudo -n python3 - <<'PY'
import sqlite3
ns = ${ns@Q}
pat = f'/registry/%{ns}%'
conn = sqlite3.connect('/var/lib/rancher/k3s/server/db/state.db')
cur = conn.cursor()
print(cur.execute('select count(*) from kine where name like ?', (pat,)).fetchone()[0])
conn.close()
PY"
}

repair_namespace_state_db() {
    local ns="$1"
    if [ ! -f "${STATE_DB_REPAIR_SCRIPT_LOCAL}" ]; then
        warn "missing state db repair script: ${STATE_DB_REPAIR_SCRIPT_LOCAL}"
        return 1
    fi
    log "Purging stale namespace rows for ${ns} from master state.db"
    copy_to_master "${STATE_DB_REPAIR_SCRIPT_LOCAL}" "${STATE_DB_REPAIR_SCRIPT_REMOTE}"
    remote_master "sudo -n systemctl stop k3s"
    remote_master "sudo -n SEED_NAMESPACE=${ns@Q} python3 ${STATE_DB_REPAIR_SCRIPT_REMOTE}"
    remote_master "sudo -n systemctl start k3s"
    sleep 10
    wait_cluster_ready
}

cleanup_namespace() {
    local ns="$1"
    if [ -z "${ns}" ]; then
        return 0
    fi
    if namespace_exists "${ns}"; then
        log "Cleaning namespace ${ns} (resources=$(namespace_resource_summary "${ns}"))"
        kubectl delete namespace "${ns}" --wait=false >/dev/null 2>&1 || true
        if ! wait_namespace_gone "${ns}"; then
            warn "namespace ${ns} still present after initial delete; cleaning residual namespaced resources"
            cleanup_namespace_resources "${ns}"
            kubectl patch namespace "${ns}" --type=merge -p '{"metadata":{"finalizers":[]}}' >/dev/null 2>&1 || true
            clear_namespace_finalizers "${ns}"
            if ! wait_namespace_gone "${ns}"; then
                warn "namespace ${ns} still exists after force cleanup"
                kubectl get namespace "${ns}" -o wide || true
            fi
        fi
    fi

    if [ "$(namespace_resource_summary "${ns}")" -gt 0 ]; then
        warn "orphan namespaced resources still exist for ${ns}; sweeping them explicitly"
        cleanup_orphan_namespace_resources "${ns}"
    fi

    if [ "${REPAIR_STATE_DB}" -eq 1 ]; then
        local ns_rows
        ns_rows="$(state_db_namespace_rows "${ns}" | tail -n 1 | tr -d '[:space:]')"
        if [ "${ns_rows:-0}" -gt 0 ]; then
            repair_namespace_state_db "${ns}"
        fi
    fi

    if namespace_exists "${ns}" || [ "$(namespace_resource_summary "${ns}")" -gt 0 ]; then
        warn "namespace cleanup incomplete for ${ns}"
        kubectl get namespace "${ns}" -o wide 2>/dev/null || true
        kubectl get network-attachment-definitions.k8s.cni.cncf.io -A 2>/dev/null | awk '$1=="'"${ns}"'" {print}' | sed -n '1,40p' || true
        return 1
    fi

    log "namespace ${ns} and residual resources are gone"
    return 0
}

if [ -n "${CLEANUP_NAMESPACE}" ]; then
    log "Step 0: cleanup namespace ${CLEANUP_NAMESPACE}"
    cleanup_namespace "${CLEANUP_NAMESPACE}"
fi

if [ "${NAMESPACE_ONLY}" -eq 1 ]; then
    log "Namespace cleanup requested only; skipping master/worker repair."
    kubectl get ns
    exit 0
fi

if [ "${REPAIR_ALL_WORKERS}" -eq 1 ]; then
    for name in "${SEED_NODE_NAMES[@]}"; do
        [ "${name}" = "${SEED_MASTER_NODE_NAME}" ] && continue
        TARGET_NAMES+=("${name}")
    done
elif [ "${#TARGET_NAMES[@]}" -eq 0 ]; then
    mapfile -t TARGET_NAMES < <(notready_workers)
fi

log "Step 1/3: repair master Docker/registry via 14_repair_master_docker_registry.sh"
MASTER_REPAIR_CMD=(bash "${LXL_DIR}/14_repair_master_docker_registry.sh" --mode "${MASTER_REPAIR_MODE}" --skip-worker-probe)
if [ "${WITH_BASE_IMAGES}" -ne 1 ]; then
    MASTER_REPAIR_CMD+=(--without-base-images)
fi
"${MASTER_REPAIR_CMD[@]}"

if [ "${#TARGET_NAMES[@]}" -eq 0 ]; then
    log "No NotReady workers detected. Master repair completed."
    kubectl get nodes -o wide
    exit 0
fi

log "Step 2/3: repair target workers: ${TARGET_NAMES[*]}"
failures=0
for node_name in "${TARGET_NAMES[@]}"; do
    node_ip="$(node_ip_for_name "${node_name}")" || {
        warn "Unknown node name: ${node_name}"
        failures=$((failures + 1))
        continue
    }
    if ! recover_worker "${node_name}" "${node_ip}"; then
        failures=$((failures + 1))
    fi
done

log "Step 3/3: wait for the cluster to return to Ready"
if ! wait_cluster_ready; then
    exit 1
fi

if [ "${failures}" -gt 0 ]; then
    warn "Completed with ${failures} worker recovery failure(s)"
    exit 1
fi

log "Cluster repair completed successfully."
