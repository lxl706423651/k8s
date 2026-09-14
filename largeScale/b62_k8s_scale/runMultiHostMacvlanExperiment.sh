#!/usr/bin/env bash
# Run the complete B62 4954-node workflow on the multi-host macvlan+VLAN
# cluster described by configMultiHostKvmMacvlan.yaml.
#
# Inputs: configMultiHostKvmMacvlan.yaml, assignment.yaml, and an optional
#         first positional run-directory path.
# Outputs: scale+timestamp run logs/artifacts plus stable generated
#          configK3s, kubeconfig, and inventory files for this cluster.
# Side effects: destroys the previously recorded B62 multi-host cluster,
#               creates VMs and VXLAN-backed VLAN trunks on all configured
#               hypervisors, installs K3s, builds/preloads images, deploys
#               4954 Pods, and runs BIRD/FIB/reconvergence verification.
# Context: run from any directory on the configured control hypervisor.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
RUN_DIR="${1:-${SCRIPT_DIR}/runs/4954_${TIMESTAMP}_multihost_macvlan_vlan}"
if [ "$#" -gt 0 ]; then
    shift
fi

exec python3 "${SCRIPT_DIR}/runFullExperiment.py" \
    --assignment "${SCRIPT_DIR}/assignment.yaml" \
    --cluster-config "${SCRIPT_DIR}/configMultiHostKvmMacvlan.yaml" \
    --config-k3s "${SCRIPT_DIR}/configK3sMultiHostKvmMacvlan.yaml" \
    --kubeconfig "${SCRIPT_DIR}/kubeconfigMultiHostKvmMacvlan.yaml" \
    --inventory "${SCRIPT_DIR}/clusterMultiHostKvmMacvlan.inventory.yaml" \
    --run-dir "${RUN_DIR}" \
    "$@"
