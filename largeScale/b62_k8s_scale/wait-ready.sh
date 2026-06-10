#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

setup_experiment_context "${1:-}"
ensure_kubeconfig
begin_stage_logging "wait-ready"

CHECK_INTERVAL=30
TIMEOUT_SECONDS=36000

echo "EXPERIMENT_DIR=${EXPERIMENT_DIR}"
echo "SEED_TOPOLOGY_SIZE=${SEED_TOPOLOGY_SIZE}"
echo "KUBECONFIG=${KUBECONFIG}"
echo "SEED_NAMESPACE=${SEED_NAMESPACE}"
echo "Check interval: ${CHECK_INTERVAL}s"
echo "Timeout: ${TIMEOUT_SECONDS}s"

if ! kubectl get namespace "${SEED_NAMESPACE}" >/dev/null 2>&1; then
    echo "Error: namespace ${SEED_NAMESPACE} not found" >&2
    exit 1
fi

start_ts="$(date +%s)"
deadline=$((start_ts + TIMEOUT_SECONDS))

tmp_pods="$(mktemp)"
cleanup() { rm -f "${tmp_pods}"; }
trap cleanup EXIT

while true; do
    now="$(date +%s)"
    elapsed=$((now - start_ts))

    if [ "${now}" -ge "${deadline}" ]; then
        echo ""
        echo "ERROR: wait-ready timed out after ${TIMEOUT_SECONDS} seconds"
        echo ""
        echo "Current namespace status:"
        kubectl get namespace "${SEED_NAMESPACE}" -o wide || true
        echo ""
        echo "Remaining pods:"
        kubectl get pods -n "${SEED_NAMESPACE}" -o wide 2>/dev/null || true
        exit 1
    fi

    if ! kubectl get pods -n "${SEED_NAMESPACE}" --request-timeout=300s --no-headers \
        -L seedemu.io/workload,seedemu.io/role > "${tmp_pods}" 2>/dev/null; then
        echo "[${elapsed}s] Warning: failed to fetch pod list, retrying in ${CHECK_INTERVAL}s..."
        echo ""
        sleep "${CHECK_INTERVAL}"
        continue
    fi

    stats="$(
      awk '
        BEGIN {
          total=0; running=0; ready=0;
          seed_total=0; seed_running=0; seed_ready=0;
          router_total=0; router_running=0; router_ready=0;
        }
        NF >= 7 {
          total++;
          split($2, r, "/");
          ready_ok=(r[1] == r[2] && r[1] != "");
          if ($3 == "Running") running++;
          if (ready_ok) ready++;
          workload=$(NF-1);
          role=$NF;
          is_seed=(workload == "seedemu");
          is_router=(role == "r" || role == "brd" || role == "rs");
          if (is_seed) {
            seed_total++;
            if ($3 == "Running") seed_running++;
            if (ready_ok) seed_ready++;
          }
          if (is_seed && is_router) {
            router_total++;
            if ($3 == "Running") router_running++;
            if (ready_ok) router_ready++;
          }
        }
        END {
          print total;
          print running;
          print ready;
          print seed_total;
          print seed_running;
          print seed_ready;
          print router_total;
          print router_running;
          print router_ready;
        }
      ' "${tmp_pods}"
    )"

    readarray -t lines <<< "${stats}"
    total_pods="${lines[0]:-0}"
    running_pods="${lines[1]:-0}"
    ready_pods="${lines[2]:-0}"
    seedemu_total="${lines[3]:-0}"
    seedemu_running="${lines[4]:-0}"
    seedemu_ready="${lines[5]:-0}"
    router_total="${lines[6]:-0}"
    router_running="${lines[7]:-0}"
    router_ready="${lines[8]:-0}"

    echo "[${elapsed}s] all pods: total=${total_pods}, running=${running_pods}, ready=${ready_pods}"
    echo "[${elapsed}s] seedemu pods: total=${seedemu_total}, running=${seedemu_running}, ready=${seedemu_ready}"
    echo "[${elapsed}s] router-like pods(r/brd/rs): total=${router_total}, running=${router_running}, ready=${router_ready}"

    if [ "${total_pods}" -gt 0 ] && [ "${total_pods}" = "${running_pods}" ] && [ "${total_pods}" = "${ready_pods}" ]; then
        echo ""
        echo "All pods in namespace ${SEED_NAMESPACE} are Running and Ready."
        exit 0
    fi

    echo "Not ready yet. Checking again in ${CHECK_INTERVAL}s..."
    echo ""
    sleep "${CHECK_INTERVAL}"
done
