#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path("/home/lxl/k8s")
sys.path.insert(0, str(REPO_ROOT))

from seedemu.k8spre import K8sPre


E2E_DIR = Path("/home/lxl/k8s/origin_k8s/test/k8spre-test")
OUTPUT_DIR = Path("/home/lxl/k8s/origin_k8s/emulate/output")


def run(cmd: list[str], *, cwd: Path) -> None:
    print(f"\n[K8sPre E2E] cwd={cwd}")
    print("[K8sPre E2E] " + " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=str(cwd), check=True)


def main() -> int:
    if not (OUTPUT_DIR / "k8s.yaml").exists() or not (OUTPUT_DIR / "images.yaml").exists():
        raise SystemExit(f"Missing compile output under {OUTPUT_DIR}")

    if E2E_DIR.exists():
        raise SystemExit(
            f"{E2E_DIR} already exists. Refusing to overwrite a real KVM/K3s test directory.\n"
            "Clean the VMs recorded in setup/kvm.resolved-nodes.tsv first, then remove this directory intentionally."
        )

    k8spre = K8sPre()

    print(f"[K8sPre E2E] generating setup under {E2E_DIR / 'setup'}")
    setup_dir = k8spre.writeKvmInstallScripts(E2E_DIR, overwrite=True)

    print(f"[K8sPre E2E] generating running under {E2E_DIR / 'running'}")
    running_dir = k8spre.writeRunningScripts(E2E_DIR, output_dir=OUTPUT_DIR, overwrite=True)

    # run(["bash", "./installKvmVms.sh"], cwd=setup_dir)

    print("[K8sPre E2E] refreshing K3s build script while preserving resolved nodes")
    setup_dir = k8spre.writeK3sBuildScripts(E2E_DIR, overwrite=True)
    # run(["bash", "./buildK3sCluster.sh"], cwd=setup_dir)

    # run(["make", "preflight"], cwd=running_dir)
    # run(["make", "build"], cwd=running_dir)
    # run(["make", "up"], cwd=running_dir)

    print("\n[K8sPre E2E] completed successfully")
    print(f"[K8sPre E2E] setup_dir={setup_dir}")
    print(f"[K8sPre E2E] running_dir={running_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
