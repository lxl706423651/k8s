#!/usr/bin/env bash
# Ensure all K3s nodes, including the master, can accept workload pods.
#
# Inputs: one experiment run directory resolved by lib.sh.
# Outputs: ensure_all_nodes_schedulable.log under the run directory.
# Side effects: removes common master/control-plane NoSchedule taints and
#               uncordons all nodes in the current K3s cluster.
# Context: run after k8sTools.py build and before compile/deploy.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

setup_experiment_context "${1:-}"
begin_stage_logging "ensure_all_nodes_schedulable"
ensure_kubeconfig

echo "Ensuring all nodes are schedulable for workload placement..."
print_test_context

kubectl --kubeconfig "${KUBECONFIG}" get nodes -o wide
kubectl --kubeconfig "${KUBECONFIG}" taint nodes --all node-role.kubernetes.io/control-plane- >/dev/null 2>&1 || true
kubectl --kubeconfig "${KUBECONFIG}" taint nodes --all node-role.kubernetes.io/master- >/dev/null 2>&1 || true

while read -r node_name; do
    [ -n "${node_name}" ] || continue
    kubectl --kubeconfig "${KUBECONFIG}" uncordon "${node_name}" >/dev/null 2>&1 || true
done < <(kubectl --kubeconfig "${KUBECONFIG}" get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')

kubectl --kubeconfig "${KUBECONFIG}" get nodes -o custom-columns=NAME:.metadata.name,READY:.status.conditions[-1].status,UNSCHEDULABLE:.spec.unschedulable,TAINTS:.spec.taints
echo "All nodes, including master, are eligible for placement."
