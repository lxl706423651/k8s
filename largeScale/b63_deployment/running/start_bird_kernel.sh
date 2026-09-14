#!/usr/bin/env bash
# Forward this stage to B62 using an explicit B63 run directory.
# Input: run directory containing assignment.yaml and its runtime cluster paths.
# Output/side effects: those of the B62 stage; never fall back to B62 defaults.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ "$#" -ne 1 ] || [ ! -f "$1/assignment.yaml" ]; then
    echo "Usage: $0 <B63-run-directory-with-assignment.yaml>" >&2
    exit 2
fi
RUN_DIR="$(cd "$1" && pwd)"
exec bash "${SCRIPT_DIR}/../../b62_k8s_scale/start_bird_kernel.sh" "${RUN_DIR}"
