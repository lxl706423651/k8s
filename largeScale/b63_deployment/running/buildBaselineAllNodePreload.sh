#!/usr/bin/env bash
# Build a default-scheduler baseline with fast all-node image preload.
#
# Inputs: the same experiment arguments accepted by build.sh, after compile.sh
# and placement.sh have produced a manifest without hard nodeSelector placement.
# Outputs: the standard build.sh artifacts plus all-node node_image_refs files.
# Side effects: builds fast image archives on the master and imports every
# workload image archive into every K3s node before deployment.
# Context: run from the controller host for kubernetes-default-scheduler dynamic
# baselines where registry-pull build/deploy latency would dominate the run.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export SEED_DEFAULT_SCHEDULER_BUILD_MODE="all-node-preload"
export SEED_FAST_IMAGE_ARCHIVE="1"

exec "${SCRIPT_DIR}/build.sh" "$@"
