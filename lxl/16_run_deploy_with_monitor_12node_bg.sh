#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_SCRIPT="${SCRIPT_DIR}/16_run_deploy_with_monitor_9node_bg.sh"
TMP_SCRIPT="$(mktemp "${SCRIPT_DIR}/tmp.deploymon12.XXXXXX")"
trap 'rm -f "${TMP_SCRIPT}"' EXIT

sed \
  -e 's/env_9node\.sh/env_12node.sh/g' \
  -e 's/01_cluster_nodes_9node\.sh/01_cluster_nodes_12node.sh/g' \
  -e 's/Deploy Debug Wrapper (9-node)/Deploy Debug Wrapper (12-node)/g' \
  -e 's/05_deploy-batched_9node/05_deploy-batched_12node/g' \
  -e 's|\${SCRIPT_DIR}/wait-ready|\${SCRIPT_DIR}/wait-ready_12node|g' \
  -e 's|"wait-ready"|"wait-ready_12node"|g' \
  -e 's/check_cni0_bridge_state_9node\.sh/check_cni0_bridge_state_12node.sh/g' \
  "${BASE_SCRIPT}" > "${TMP_SCRIPT}"

chmod +x "${TMP_SCRIPT}"
exec "${TMP_SCRIPT}" "$@"
