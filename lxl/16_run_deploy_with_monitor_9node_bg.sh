#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

source "${SCRIPT_DIR}/env_9node.sh"
source "${SCRIPT_DIR}/01_cluster_nodes_9node.sh"
seed_load_cluster_nodes

export KUBECONFIG="${REPO_ROOT}/output/kubeconfigs/${SEED_K3S_CLUSTER_NAME}.yaml"

: "${DEPLOY_DEBUG_MONITOR_INTERVAL:=20}"
: "${DEPLOY_DEBUG_RUN_WAIT_READY:=true}"
: "${DEPLOY_DEBUG_EVENTS_TAIL:=100}"
: "${DEPLOY_DEBUG_JOURNAL_SINCE:=15 min ago}"

RUNNER="${SCRIPT_DIR}/05_deploy-batched_9node"
WAIT_READY="${SCRIPT_DIR}/wait-ready"

require_experiment_dir() {
    if [ -z "${EXPERIMENT_DIR:-}" ]; then
        echo "Error: EXPERIMENT_DIR is not set."
        echo "Example:"
        echo "  export EXPERIMENT_DIR=/home/lxl/k8s/lxl/logs/$(date +%Y%m%d_%H%M%S)_9955"
        exit 1
    fi
}

ts() { date '+%F %T %z'; }

safe_kubectl() {
    kubectl "$@" 2>&1 || true
}

namespace_phase() {
    kubectl get namespace "${SEED_NAMESPACE}" -o jsonpath='{.status.phase}' 2>/dev/null || echo "MISSING"
}

node_readiness_summary() {
    kubectl get nodes --no-headers 2>/dev/null | awk '
        BEGIN {ready=0; total=0}
        {total++; if ($2 == "Ready") ready++}
        END {printf("ready=%d total=%d\n", ready, total)}
    ' || echo "ready=0 total=0"
}

pod_summary() {
    kubectl -n "${SEED_NAMESPACE}" get pods --no-headers 2>/dev/null | awk '
        BEGIN { total=0; ready_ok=0; }
        {
            total++;
            split($2, a, "/");
            if ($3 == "Running" && a[1] == a[2] && a[1] != "") ready_ok++;
            phase[$3]++;
        }
        END {
            printf("total=%d ready=%d", total, ready_ok);
            if (phase["Pending"] != "") printf(" Pending=%d", phase["Pending"]);
            if (phase["ContainerCreating"] != "") printf(" ContainerCreating=%d", phase["ContainerCreating"]);
            if (phase["Running"] != "") printf(" Running=%d", phase["Running"]);
            if (phase["Terminating"] != "") printf(" Terminating=%d", phase["Terminating"]);
            if (phase["Completed"] != "") printf(" Completed=%d", phase["Completed"]);
            if (phase["Succeeded"] != "") printf(" Succeeded=%d", phase["Succeeded"]);
            if (phase["Unknown"] != "") printf(" Unknown=%d", phase["Unknown"]);
            if (phase["CrashLoopBackOff"] != "") printf(" CrashLoopBackOff=%d", phase["CrashLoopBackOff"]);
            print "";
        }
    ' || echo "total=0 ready=0"
}

capture_node_quick_stats() {
    local node_name="$1"
    local node_ip="$2"
    local out="$3"
    {
        echo "===== $(ts) ${node_name} ${node_ip} ====="
        ssh -i "${SEED_K3S_SSH_KEY}" \
            -o BatchMode=yes \
            -o ConnectTimeout=5 \
            -o StrictHostKeyChecking=no \
            -o UserKnownHostsFile=/dev/null \
            "${SEED_K3S_USER}@${node_ip}" '
                set -u
                echo "hostname=$(hostname 2>/dev/null || echo unknown)"
                echo "uptime=$(uptime 2>/dev/null || echo unavailable)"
                echo "loadavg=$(cat /proc/loadavg 2>/dev/null || echo unavailable)"
                echo "--- free -m ---"
                free -m 2>/dev/null | sed -n "1,2p" || true
                if systemctl list-unit-files 2>/dev/null | grep -q "^k3s-agent"; then
                    echo "k3s_agent=$(systemctl is-active k3s-agent 2>/dev/null || echo unknown)"
                else
                    echo "k3s_server=$(systemctl is-active k3s 2>/dev/null || echo unknown)"
                fi
                if sudo -n test -d /sys/class/net/cni0/bridge 2>/dev/null; then
                    echo "cni0_hash_max=$(sudo -n cat /sys/class/net/cni0/bridge/hash_max 2>/dev/null || echo NA)"
                    echo "cni0_fdb_count=$(sudo -n bridge fdb show br cni0 2>/dev/null | wc -l | tr -d \" \")"
                    echo "veth_count=$(sudo -n ip link show type veth 2>/dev/null | wc -l | tr -d \" \")"
                else
                    echo "cni0_hash_max=CNI0_MISSING"
                    echo "cni0_fdb_count=CNI0_MISSING"
                    echo "veth_count=CNI0_MISSING"
                fi
            ' 2>&1 || echo "[ssh failed ${node_name} ${node_ip}]"
        echo
    } >> "${out}"
}

capture_host_snapshot() {
    local out="$1"
    {
        echo "===== $(ts) host ====="
        echo "--- df -h / ---"
        df -h /
        echo "--- free -h ---"
        free -h
        echo "--- virsh list --all ---"
        sudo -n virsh list --all 2>&1 || true
        echo "--- virsh domstate --reason ---"
        for node_name in "${SEED_NODE_NAMES[@]}"; do
            printf "%s: " "${node_name}"
            sudo -n virsh domstate --reason "${node_name}" 2>&1 || true
        done
        echo "--- qemu rss ---"
        ps -eo pid,rss,cmd | grep qemu-system | grep -v grep || true
        echo
    } >> "${out}"
}

capture_cluster_snapshot() {
    local monitor_dir="$1"
    {
        echo "===== $(ts) cluster ====="
        echo "namespace_phase=$(namespace_phase)"
        echo "nodes=$(node_readiness_summary)"
        echo "pods=$(pod_summary)"
        echo "--- readyz ---"
        kubectl get --raw='/readyz?verbose' 2>/dev/null | tail -n 20 || echo "[readyz unavailable]"
        echo
    } >> "${monitor_dir}/cluster_summary.log"

    {
        echo "===== $(ts) nodes wide ====="
        safe_kubectl get nodes -o wide
        echo
    } >> "${monitor_dir}/nodes_wide.log"

    {
        echo "===== $(ts) namespace pods ====="
        safe_kubectl -n "${SEED_NAMESPACE}" get pods -o wide
        echo
    } >> "${monitor_dir}/pods_wide.log"

    {
        echo "===== $(ts) events tail ====="
        safe_kubectl -n "${SEED_NAMESPACE}" get events --sort-by='.lastTimestamp' | tail -n "${DEPLOY_DEBUG_EVENTS_TAIL}"
        echo
    } >> "${monitor_dir}/events_tail.log"

    {
        echo "===== $(ts) kubectl top nodes ====="
        kubectl top nodes 2>/dev/null || echo "[kubectl top nodes unavailable]"
        echo
    } >> "${monitor_dir}/top_nodes.log"

    {
        echo "===== $(ts) kubectl top pods ====="
        kubectl -n "${SEED_NAMESPACE}" top pods 2>/dev/null || echo "[kubectl top pods unavailable]"
        echo
    } >> "${monitor_dir}/top_pods.log"
}

capture_failure_bundle() {
    local outdir="$1"
    mkdir -p "${outdir}/nodes"

    safe_kubectl get nodes -o wide > "${outdir}/nodes_wide.txt"
    safe_kubectl describe nodes > "${outdir}/nodes_describe.txt"
    safe_kubectl -n "${SEED_NAMESPACE}" get all -o wide > "${outdir}/all_wide.txt"
    safe_kubectl -n "${SEED_NAMESPACE}" get pods -o wide > "${outdir}/pods_wide.txt"
    safe_kubectl -n "${SEED_NAMESPACE}" get events --sort-by='.lastTimestamp' > "${outdir}/events.txt"
    safe_kubectl -n "${SEED_NAMESPACE}" get network-attachment-definitions.k8s.cni.cncf.io -o yaml > "${outdir}/nad.yaml"
    kubectl get --raw='/readyz?verbose' 2>/dev/null > "${outdir}/readyz.txt" || echo "[readyz unavailable]" > "${outdir}/readyz.txt"

    capture_host_snapshot "${outdir}/host_snapshot.txt"
    "${SCRIPT_DIR}/check_cni0_bridge_state_9node.sh" > "${outdir}/cni0_bridge_state.txt" 2>&1 || true

    for i in "${!SEED_NODE_NAMES[@]}"; do
        local node_name="${SEED_NODE_NAMES[$i]}"
        local node_ip="${SEED_NODE_IPS[$i]}"
        capture_node_quick_stats "${node_name}" "${node_ip}" "${outdir}/nodes/${node_name}_quick.txt"
        {
            echo "===== $(ts) ${node_name} ${node_ip} failure detail ====="
            ssh -i "${SEED_K3S_SSH_KEY}" \
                -o BatchMode=yes \
                -o ConnectTimeout=5 \
                -o StrictHostKeyChecking=no \
                -o UserKnownHostsFile=/dev/null \
                "${SEED_K3S_USER}@${node_ip}" '
                    set -u
                    echo "--- uptime ---"
                    uptime || true
                    echo "--- vmstat 1 2 ---"
                    vmstat 1 2 || true
                    echo "--- top -b -n 1 ---"
                    top -b -n 1 | head -n 30 || true
                    echo "--- dmesg tail ---"
                    dmesg -T 2>/dev/null | tail -n 200 || true
                    echo "--- journal k3s ---"
                    journalctl -u k3s --since "'"${DEPLOY_DEBUG_JOURNAL_SINCE}"'" --no-pager 2>/dev/null | tail -n 200 || true
                    echo "--- journal k3s-agent ---"
                    journalctl -u k3s-agent --since "'"${DEPLOY_DEBUG_JOURNAL_SINCE}"'" --no-pager 2>/dev/null | tail -n 200 || true
                ' 2>&1 || echo "[ssh failed ${node_name} ${node_ip}]"
        } > "${outdir}/nodes/${node_name}_failure.txt"
    done
}

monitor_loop() {
    local monitor_dir="$1"
    mkdir -p "${monitor_dir}/nodes"
    while true; do
        capture_cluster_snapshot "${monitor_dir}"
        capture_host_snapshot "${monitor_dir}/host_monitor.log"
        "${SCRIPT_DIR}/check_cni0_bridge_state_9node.sh" >> "${monitor_dir}/cni0_bridge_state.log" 2>&1 || true
        for i in "${!SEED_NODE_NAMES[@]}"; do
            capture_node_quick_stats "${SEED_NODE_NAMES[$i]}" "${SEED_NODE_IPS[$i]}" "${monitor_dir}/nodes/${SEED_NODE_NAMES[$i]}.log"
        done
        sleep "${DEPLOY_DEBUG_MONITOR_INTERVAL}"
    done
}

cleanup_monitor() {
    if [ -n "${MONITOR_PID:-}" ]; then
        kill "${MONITOR_PID}" >/dev/null 2>&1 || true
        wait "${MONITOR_PID}" 2>/dev/null || true
    fi
}

run_stage() {
    local label="$1"
    local cmd="$2"
    echo "[$(ts)] starting stage: ${label}"
    set +e
    eval "${cmd}"
    local rc=$?
    set -e
    echo "[$(ts)] stage ${label} exit_code=${rc}"
    return "${rc}"
}

run_mode() {
    local monitor_dir="${EXPERIMENT_DIR}/deploy_debug_monitor"
    local runner_log="${EXPERIMENT_DIR}/deploy_debug_runner.log"
    local exit_file="${EXPERIMENT_DIR}/deploy_debug.exit"
    mkdir -p "${monitor_dir}"

    exec >> "${runner_log}" 2>&1

    echo "=== Deploy Debug Wrapper (9-node) ==="
    echo "Started at: $(ts)"
    echo "Experiment dir: ${EXPERIMENT_DIR}"
    echo "Monitor dir: ${monitor_dir}"
    echo "Run wait-ready after deploy: ${DEPLOY_DEBUG_RUN_WAIT_READY}"
    echo "Monitor interval: ${DEPLOY_DEBUG_MONITOR_INTERVAL}s"

    monitor_loop "${monitor_dir}" &
    MONITOR_PID=$!
    echo "Monitor PID: ${MONITOR_PID}"

    local final_rc=0
    local failure_stage=""

    trap 'final_rc=130; failure_stage="interrupted"; cleanup_monitor; capture_failure_bundle "${EXPERIMENT_DIR}/deploy_debug_failure_artifacts"; echo "exit_code=${final_rc}" > "${exit_file}"; exit "${final_rc}"' INT TERM

    local stage_rc=0
    run_stage "deploy" "'${RUNNER}'" || stage_rc=$?
    if [ "${stage_rc}" -ne 0 ]; then
        final_rc="${stage_rc}"
        failure_stage="deploy"
    elif [ "${DEPLOY_DEBUG_RUN_WAIT_READY}" = "true" ]; then
        stage_rc=0
        run_stage "wait-ready" "'${WAIT_READY}'" || stage_rc=$?
        if [ "${stage_rc}" -ne 0 ]; then
            final_rc="${stage_rc}"
            failure_stage="wait-ready"
        fi
    fi

    cleanup_monitor

    if [ "${final_rc}" -ne 0 ]; then
        echo "[$(ts)] failure detected in stage=${failure_stage}, collecting failure bundle..."
        capture_failure_bundle "${EXPERIMENT_DIR}/deploy_debug_failure_artifacts"
    else
        echo "[$(ts)] deploy debug wrapper completed successfully"
        capture_failure_bundle "${EXPERIMENT_DIR}/deploy_debug_success_artifacts"
    fi

    {
        echo "exit_code=${final_rc}"
        echo "failure_stage=${failure_stage:-none}"
        echo "finished_at=$(ts)"
    } > "${exit_file}"

    exit "${final_rc}"
}

launch_bg() {
    require_experiment_dir
    mkdir -p "${EXPERIMENT_DIR}"

    local main_log="${EXPERIMENT_DIR}/deploy_debug_runner.log"
    local pid_file="${EXPERIMENT_DIR}/deploy_debug.pid"
    local start_file="${EXPERIMENT_DIR}/deploy_debug.start"

    if [ -f "${pid_file}" ]; then
        local old_pid
        old_pid="$(cat "${pid_file}" 2>/dev/null || true)"
        if [ -n "${old_pid}" ] && kill -0 "${old_pid}" 2>/dev/null; then
            echo "A deploy debug run is already active."
            echo "PID=${old_pid}"
            echo "LOG=${main_log}"
            exit 1
        fi
    fi

    {
        echo "started_at=$(ts)"
        echo "experiment_dir=${EXPERIMENT_DIR}"
        echo "runner=${RUNNER}"
        echo "wait_ready=${WAIT_READY}"
        echo "run_wait_ready=${DEPLOY_DEBUG_RUN_WAIT_READY}"
        echo "monitor_interval=${DEPLOY_DEBUG_MONITOR_INTERVAL}"
    } > "${start_file}"

    nohup bash -lc "
        set -Eeuo pipefail
        cd '${SCRIPT_DIR}'
        source '${SCRIPT_DIR}/env_9node.sh' >/dev/null 2>&1
        export EXPERIMENT_DIR='${EXPERIMENT_DIR}'
        export DEPLOY_DEBUG_MONITOR_INTERVAL='${DEPLOY_DEBUG_MONITOR_INTERVAL}'
        export DEPLOY_DEBUG_RUN_WAIT_READY='${DEPLOY_DEBUG_RUN_WAIT_READY}'
        export DEPLOY_DEBUG_EVENTS_TAIL='${DEPLOY_DEBUG_EVENTS_TAIL}'
        export DEPLOY_DEBUG_JOURNAL_SINCE='${DEPLOY_DEBUG_JOURNAL_SINCE}'
        exec '${SCRIPT_DIR}/16_run_deploy_with_monitor_9node_bg.sh' --run
    " >> "${main_log}" 2>&1 &

    local bg_pid=$!
    echo "${bg_pid}" > "${pid_file}"

    echo "Started deploy debug run in background."
    echo "PID=${bg_pid}"
    echo "LOG=${main_log}"
    echo "PID_FILE=${pid_file}"
    echo "START_FILE=${start_file}"
    echo ""
    echo "Useful commands:"
    echo "  tail -f '${main_log}'"
    echo "  tail -f '${EXPERIMENT_DIR}/deploy.log'"
    echo "  tail -f '${EXPERIMENT_DIR}/wait-ready.log'"
    echo "  ps -p ${bg_pid} -o pid,ppid,tty,stat,etime,cmd"
}

case "${1:-}" in
    --run)
        require_experiment_dir
        run_mode
        ;;
    *)
        launch_bg
        ;;
esac
