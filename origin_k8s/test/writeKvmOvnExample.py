#!/usr/bin/env python3
"""Generate a KVM-backed K8sPre example that uses Kube-OVN.

The script writes an isolated setup/running directory. It does not create VMs,
install K3s, build images, or deploy workloads by itself; the printed commands
are the reproducible manual execution flow.
"""

from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path("/home/lxl/k8s")
TEST_DIR = Path(__file__).resolve().parent
EXAMPLE_DIR = TEST_DIR / "k8spre-kvm-ovn-example"
CONFIG_PATH = TEST_DIR / "configKvmOvn.yaml"
OUTPUT_DIR = Path("/home/lxl/k8s/origin_k8s/emulate/output")

sys.path.insert(0, str(REPO_ROOT))

from seedemu.k8spre import K8sPre  # noqa: E402


def writeKvmOvnExample() -> None:
    """Write setup and running scripts for the KVM + Kube-OVN flow."""
    k8spre = K8sPre()
    setup_dir = k8spre.writeKvmInstallScripts(
        EXAMPLE_DIR,
        config=CONFIG_PATH,
        connection="ovn",
        overwrite=True,
    )
    setup_dir = k8spre.writeK3sBuildScripts(EXAMPLE_DIR, overwrite=True)
    running_dir = k8spre.writeRunningScripts(
        EXAMPLE_DIR,
        output_dir=OUTPUT_DIR,
        overwrite=True,
    )

    print("KVM + OVN example generated.")
    print(f"setup_dir={setup_dir}")
    print(f"running_dir={running_dir}")
    print("Manual next steps:")
    print(f"  cd {setup_dir}")
    print("  bash ./installKvmVms.sh")
    print("  bash ./buildK3sCluster.sh")
    print("    # buildK3sCluster.sh calls ovn/installKubeOvnFabric.sh automatically")
    print("    # because generated kvm.yaml -> configK3s.yaml preserves fabric.type=ovn.")
    print("  bash ./ovn/validateKubeOvnFabric.sh ./configK3s.yaml")
    print(f"  cd {running_dir}")
    print("  make preflight")
    print("  make build")
    print("  make up")


if __name__ == "__main__":
    writeKvmOvnExample()
