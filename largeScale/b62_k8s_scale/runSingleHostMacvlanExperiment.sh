#!/usr/bin/env bash
# Run the complete B62 workflow on one hypervisor using the configured VM count.
#
# Inputs: configSingleHostKvmMacvlan.yaml and an optional
#         first positional run-directory path.
# Outputs: scale+timestamp logs plus stable single-host cluster output files.
# Side effects: creates the dedicated libvirt networks and VMs, installs K3s,
#               builds/deploys the workload, and runs routing/dataplane checks.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_ARGS=()
if [ "$#" -gt 0 ] && [[ "$1" != --* ]]; then
    RUN_ARGS=(--run-dir "$1")
    shift
fi

exec python3 "${SCRIPT_DIR}/runFullExperiment.py" \
    --config "${SCRIPT_DIR}/configSingleHostKvmMacvlan.yaml" \
    --config-k3s "${SCRIPT_DIR}/configK3sSingleHostKvmMacvlan.yaml" \
    --kubeconfig "${SCRIPT_DIR}/kubeconfigSingleHostKvmMacvlan.yaml" \
    --inventory "${SCRIPT_DIR}/clusterSingleHostKvmMacvlan.inventory.yaml" \
    "${RUN_ARGS[@]}" \
    "$@"
