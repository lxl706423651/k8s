#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

source "${SCRIPT_DIR}/env_9node.sh"
source "${SCRIPT_DIR}/01_cluster_nodes_9node.sh"
seed_load_cluster_nodes

SSH_OPTS=(
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
  -o LogLevel=ERROR
  -o BatchMode=yes
  -o ConnectTimeout=10
)

MASTER_IP="${SEED_MASTER_NODE_IP}"
MASTER_USER="${SEED_K3S_USER}"
EXPDIR="${EXPERIMENT_DIR:-}"

echo "=== Registry/Preload Monitor ==="
echo "Master: ${MASTER_IP}"
if [ -n "${EXPDIR}" ]; then
  echo "Experiment: ${EXPDIR}"
fi
echo

ssh "${SSH_OPTS[@]}" "${MASTER_USER}@${MASTER_IP}" '
echo "=== uptime ==="
uptime
echo
echo "=== docker stats registry ==="
sudo -n docker stats --no-stream registry || true
echo
echo "=== established connections to :5000 ==="
ss -tn state established "( sport = :5000 or dport = :5000 )" | wc -l
echo
echo "=== free -h ==="
free -h
echo
echo "=== vmstat 1 3 ==="
vmstat 1 3
echo
echo "=== registry log tail ==="
sudo -n docker logs --tail 20 registry 2>&1
'

echo
echo "=== Image Counts ==="
"${SCRIPT_DIR}/check_preload_image_counts.sh"

if [ -n "${EXPDIR}" ] && [ -d "${EXPDIR}" ]; then
  echo
  echo "=== Preload Done Markers ==="
  rg -n "preload done" "${EXPDIR}"/preload_seed-k3s-*.log 2>/dev/null || true
fi

cat <<'EOF'

=== Suggested Concurrency Heuristic ===
- If registry CPU stays below ~35%, master load stays below ~2, and active :5000 connections stay below ~4: use 3 worker nodes in parallel.
- If registry CPU rises into ~35%-60% or connections rise toward ~5-8: use 2 worker nodes in parallel.
- If registry CPU exceeds ~60%, load rises sharply, or errors appear in registry logs: fall back to serial.
EOF
