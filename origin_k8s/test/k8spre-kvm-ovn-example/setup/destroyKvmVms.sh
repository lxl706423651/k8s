#!/usr/bin/env bash
# Destroy KVM VMs recorded in kvmState.yaml. This root-level wrapper
# keeps the generated setup interface stable while KVM internals live
# under setup/kvm/.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

bash ./kvm/destroyKvmVms.sh "${1:-./kvmState.yaml}"
