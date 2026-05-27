#!/usr/bin/env python3
"""Generate a minimal physical-node K8sPre example that uses Kube-OVN.

The script only writes setup/running scripts into an isolated test directory.
It does not install K3s, install Kube-OVN, or deploy SeedEMU workloads.
"""

from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path("/home/lxl/k8s")
TEST_DIR = Path(__file__).resolve().parent
EXAMPLE_DIR = TEST_DIR / "k8spre-ovn-example"
CONFIG_PATH = TEST_DIR / "configK3sOvn.yaml"
OUTPUT_DIR = Path("/home/lxl/k8s/origin_k8s/emulate/output")

sys.path.insert(0, str(REPO_ROOT))

from seedemu.k8spre import K8sPre  # noqa: E402


def writeOvnExample() -> None:
    """Write setup and running scripts for the Kube-OVN physical-node flow.

    The generated setup/configK3s.yaml is derived from CONFIG_PATH. Passing
    connection="ovn" makes K8sPre select fabric.type=ovn and inject the K3s
    version currently required by the bundled Kube-OVN installer when absent.
    """
    k8spre = K8sPre()
    setup_dir = k8spre.writePhysicalNodeScripts(
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

    print("OVN example generated.")
    print(f"setup_dir={setup_dir}")
    print(f"running_dir={running_dir}")
    print("Manual next steps:")
    print(f"  cd {setup_dir}")
    print("  bash ./preparePhysicalNodes.sh ./configK3s.yaml")
    print("  bash ./buildK3sCluster.sh")
    print("    # buildK3sCluster.sh calls ovn/installKubeOvnFabric.sh automatically")
    print("    # when configK3s.yaml has fabric.type=ovn.")
    print("  bash ./ovn/validateKubeOvnFabric.sh ./configK3s.yaml")
    print(f"  cd {running_dir}")
    print("  make preflight")
    print("  make build")
    print("  make up")


if __name__ == "__main__":
    writeOvnExample()
