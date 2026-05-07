#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

setup_experiment_context "${1:-}"
load_deploy_config
ensure_kubeconfig
begin_stage_logging "wait-ready"

CHECK_INTERVAL="${SEED_WAIT_READY_INTERVAL_SECONDS}"
TIMEOUT_SECONDS="${SEED_WAIT_READY_TIMEOUT_SECONDS}"

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

tmp_json="$(mktemp)"
cleanup() { rm -f "${tmp_json}"; }
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

    if ! kubectl get pods -n "${SEED_NAMESPACE}" -o json > "${tmp_json}" 2>/dev/null; then
        echo "[${elapsed}s] Warning: failed to fetch pod JSON, retrying in ${CHECK_INTERVAL}s..."
        echo ""
        sleep "${CHECK_INTERVAL}"
        continue
    fi

    stats="$(
      python3 - "${tmp_json}" <<'PY'
import json, sys
path = sys.argv[1]
try:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
except Exception:
    data = {"items": []}

items = data.get("items", []) or []
roles = {"r", "brd", "rs"}
total_pods = len(items)
running_pods = 0
ready_pods = 0
seedemu_total = 0
seedemu_running = 0
seedemu_ready = 0
router_total = 0
router_running = 0
router_ready = 0

for item in items:
    meta = item.get("metadata", {}) or {}
    labels = meta.get("labels", {}) or {}
    status = item.get("status", {}) or {}
    phase = str(status.get("phase", ""))
    container_statuses = status.get("containerStatuses", []) or []
    is_ready = bool(container_statuses) and all(c.get("ready", False) for c in container_statuses)
    if phase == "Running":
        running_pods += 1
    if is_ready:
        ready_pods += 1
    is_seedemu = labels.get("seedemu.io/workload") == "seedemu"
    role = str(labels.get("seedemu.io/role", ""))
    if is_seedemu:
        seedemu_total += 1
        if phase == "Running":
            seedemu_running += 1
        if is_ready:
            seedemu_ready += 1
    if is_seedemu and role in roles:
        router_total += 1
        if phase == "Running":
            router_running += 1
        if is_ready:
            router_ready += 1

print(total_pods)
print(running_pods)
print(ready_pods)
print(seedemu_total)
print(seedemu_running)
print(seedemu_ready)
print(router_total)
print(router_running)
print(router_ready)
PY
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
