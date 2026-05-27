#!/usr/bin/env python3
"""Generate a minimal physical-node K8sPre example that uses Linux VXLAN.

The script only writes setup/running scripts into an isolated test directory.
It does not create interfaces, install K3s, or deploy SeedEMU workloads.
"""

from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path("/home/lxl/k8s")
TEST_DIR = Path(__file__).resolve().parent
EXAMPLE_DIR = TEST_DIR / "k8spre-vxlan-example"
CONFIG_PATH = TEST_DIR / "configK3sVxlan.yaml"
OUTPUT_DIR = Path("/home/lxl/k8s/origin_k8s/emulate/output")

sys.path.insert(0, str(REPO_ROOT))

from seedemu.k8spre import K8sPre  # noqa: E402


def writeVxlanExample() -> None:
    """Write setup and running scripts for the Linux VXLAN physical-node flow.

    The generated setup/configK3s.yaml keeps the physical machine list in
    CONFIG_PATH, while connection="vxlan" makes K8sPre preserve the Linux
    VXLAN bridge fabric. Running scripts point at OUTPUT_DIR so the user can
    reuse the existing SeedEMU compile output.
    """
    k8spre = K8sPre()
    setup_dir = k8spre.writePhysicalNodeScripts(
        EXAMPLE_DIR,
        config=CONFIG_PATH,
        connection="vxlan",
        overwrite=True,
    )
    setup_dir = k8spre.writeK3sBuildScripts(EXAMPLE_DIR, overwrite=True)
    running_dir = k8spre.writeRunningScripts(
        EXAMPLE_DIR,
        output_dir=OUTPUT_DIR,
        overwrite=True,
    )

    print("VXLAN example generated.")
    print(f"setup_dir={setup_dir}")
    print(f"running_dir={running_dir}")
    print("Manual next steps:")
    print(f"  cd {setup_dir}")
    print("  bash ./preparePhysicalNodes.sh ./configK3s.yaml")
    print("  bash ./vxlan/configureLinuxVxlanFabric.sh ./configK3s.yaml")
    print("  bash ./vxlan/validateLinuxVxlanFabric.sh ./configK3s.yaml")
    print("  bash ./buildK3sCluster.sh")
    print(f"  cd {running_dir}")
    print("  make preflight")
    print("  make build")
    print("  make up")


if __name__ == "__main__":
    writeVxlanExample()
