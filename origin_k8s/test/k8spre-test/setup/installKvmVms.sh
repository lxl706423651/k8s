#!/usr/bin/env bash
# Create KVM VMs from kvm.yaml, generate configK3s.yaml/kvmState.yaml,
# and tune VM OS limits.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "[K8sPre] Preparing setup assets..."
bash ./kvm/prepareHostAssets.sh ./kvm.yaml

echo "[K8sPre] Creating KVM virtual machines..."
bash ./kvm/createKvmVms.sh ./kvm.yaml

echo "[K8sPre] Unlocking VM limits..."
bash ./kvm/tuneVmLimits.sh ./configK3s.yaml

echo "[K8sPre] KVM installation finished."
