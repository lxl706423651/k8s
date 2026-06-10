#!/usr/bin/env bash
# Run one complete B62 assignment experiment.
#
# Inputs: assignment.yaml.
# Outputs: runs/<timestamp>_<topology>_w<workers>/ with per-stage logs,
#          summary.json, and BIRD summaries.
# Side effects: may destroy an existing recorded cluster, creates a new KVM/K3s
#               cluster, deploys the workload namespace, starts/verifies BIRD,
#               and destroys the VM cluster after full pass.
# Context: run from this B62 directory on the KVM host.
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "${SCRIPT_DIR}/runFullExperiment.py" "$@"
