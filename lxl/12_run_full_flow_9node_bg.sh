#!/usr/bin/env bash
#set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/env_9node.sh"

RUNNER="${SCRIPT_DIR}/11_run_full_flow_9node.sh"

if [ -z "${EXPERIMENT_DIR:-}" ]; then
  echo "Error: EXPERIMENT_DIR is not set."
  echo "Example:"
  echo "  export EXPERIMENT_DIR=/home/lxl/k8s/lxl/logs/20260417_xxxxxx_214"
  exit 1
fi

mkdir -p "${EXPERIMENT_DIR}"

MAIN_LOG="${EXPERIMENT_DIR}/full_flow.log"
PID_FILE="${EXPERIMENT_DIR}/full_flow.pid"
START_FILE="${EXPERIMENT_DIR}/full_flow.start"

if [ -f "${PID_FILE}" ]; then
  old_pid="$(cat "${PID_FILE}" 2>/dev/null || true)"
  if [ -n "${old_pid}" ] && kill -0 "${old_pid}" 2>/dev/null; then
    echo "A full-flow run is already active."
    echo "PID=${old_pid}"
    echo "LOG=${MAIN_LOG}"
    exit 1
  fi
fi

{
  echo "started_at=$(date '+%F %T %z')"
  echo "experiment_dir=${EXPERIMENT_DIR}"
  echo "runner=${RUNNER}"
} > "${START_FILE}"

nohup bash -lc "
  set -euo pipefail
  cd '${SCRIPT_DIR}'
  source '${SCRIPT_DIR}/env_9node.sh' >/dev/null 2>&1
  export EXPERIMENT_DIR='${EXPERIMENT_DIR}'
  exec '${RUNNER}'
" >> "${MAIN_LOG}" 2>&1 &

bg_pid=$!
echo "${bg_pid}" > "${PID_FILE}"

echo "Started full flow in background."
echo "PID=${bg_pid}"
echo "LOG=${MAIN_LOG}"
echo "PID_FILE=${PID_FILE}"
echo ""
echo "Useful commands:"
echo "  tail -f '${MAIN_LOG}'"
echo "  ps -p ${bg_pid} -o pid,ppid,tty,stat,etime,cmd"
