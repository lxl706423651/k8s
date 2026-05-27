#!/usr/bin/env python3
from pathlib import Path
import sys

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from seedemu.k8spre import K8sPre


OUT = Path("/home/lxl/k8s/origin_k8s/test/k8spre-generated")


def assert_file(path: Path) -> None:
    assert path.exists(), f"missing {path}"
    assert path.is_file(), f"not a file: {path}"


def main() -> None:
    k8spre = K8sPre()

    setup_dir = k8spre.writeKvmInstallScripts(
        OUT,
        overwrite=True,
        master_vcpus=16,
        worker_count=4,
    )
    assert_file(setup_dir / "kvm.yaml")
    assert_file(setup_dir / "installKvmVms.sh")
    assert not (setup_dir / ".gitignore").exists()
    assert_file(setup_dir / "createKvmVms.sh")
    assert_file(setup_dir / "prepareHostAssets.sh")
    assert_file(setup_dir / "tuneVmLimits.sh")
    assert_file(setup_dir / "manageK3sConfig.py")
    assert not (setup_dir / "buildK3sCluster.sh").exists()
    assert not (setup_dir / "applyK3sCluster.sh").exists()
    assert not (setup_dir / "kvm_template.yaml").exists()
    assert not (setup_dir / "k3s_template.yaml").exists()

    with (setup_dir / "kvm.yaml").open("r", encoding="utf-8") as handle:
        kvm_config = yaml.safe_load(handle)
    assert kvm_config["master"]["vcpus"] == 16
    assert kvm_config["workers"]["count"] == 4
    assert kvm_config["outputs"]["kubeconfig"] == str(setup_dir / "seedemu-k3s.kubeconfig.yaml")
    assert kvm_config["outputs"]["k3sConfig"] == str(setup_dir / "configK3s.yaml")
    assert kvm_config["outputs"]["kvmState"] == str(setup_dir / "kvmState.yaml")
    assert "runningConfig" not in kvm_config["outputs"]
    assert not (setup_dir / "configRunning.yaml").exists()
    assert kvm_config["kvm"]["cloudInitDir"] == str(setup_dir / "cloud-init")
    assert kvm_config["kvm"]["diskDir"].startswith("/data/")
    assert kvm_config["kvm"]["baseImagePath"].startswith("/data/")

    custom_config = OUT.parent / "k8spre-custom-kvm.yaml"
    custom_config.write_text(
        "master:\n"
        "  vcpus: 20\n"
        "workers:\n"
        "  count: 1\n",
        encoding="utf-8",
    )
    setup_dir = k8spre.writeKvmInstallScripts(
        OUT,
        config=custom_config,
        overwrite=True,
        master_vcpus=16,
        worker_count=4,
    )
    with (setup_dir / "kvm.yaml").open("r", encoding="utf-8") as handle:
        kvm_config = yaml.safe_load(handle)
    assert kvm_config["master"]["vcpus"] == 20
    assert kvm_config["master"]["memoryMb"] == 10240
    assert kvm_config["workers"]["count"] == 1
    custom_config.unlink()

    (setup_dir / "configK3s.yaml").write_text(
        "clusterName: seedemu-k3s\n"
        "nodes:\n"
        "  - name: seed-k3s-master\n"
        "    role: master\n"
        "    ip: 192.168.122.110\n"
        "    ssh:\n"
        "      user: ubuntu\n"
        "      key: /home/lxl/.ssh/id_ed25519\n",
        encoding="utf-8",
    )
    setup_dir = k8spre.writeK3sBuildScripts(OUT, overwrite=True)
    assert_file(setup_dir / "installKvmVms.sh")
    assert_file(setup_dir / "buildK3sCluster.sh")
    assert_file(setup_dir / "applyK3sCluster.sh")
    assert_file(setup_dir / "createKvmVms.sh")
    assert_file(setup_dir / "ansible" / "k3s-install.yml")
    assert_file(setup_dir / "configK3s.yaml")

    existing_vm_config = OUT.parent / "k8spre-existing-vms-k3s.yaml"
    existing_vm_config.write_text(
        "clusterName: seedemu-k3s\n"
        "nodes:\n"
        "  - role: master\n"
        "    ip: 192.168.122.122\n"
        "    ssh:\n"
        "      user: ubuntu\n"
        "      key: /home/lxl/.ssh/id_ed25519\n"
        "  - role: worker\n"
        "    ip: 192.168.122.123\n"
        "    ssh:\n"
        "      user: ubuntu\n"
        "      key: /home/lxl/.ssh/id_ed25519\n",
        encoding="utf-8",
    )
    k3s_only_dir = k8spre.writeK3sBuildScripts(OUT.parent / "k8spre-k3s-only", config=existing_vm_config, overwrite=True)
    assert_file(k3s_only_dir / "configK3s.yaml")
    assert_file(k3s_only_dir / "buildK3sCluster.sh")
    assert_file(k3s_only_dir / "applyK3sCluster.sh")
    assert not (k3s_only_dir / ".gitignore").exists()
    assert not (k3s_only_dir / "kvm.yaml").exists()
    assert not (k3s_only_dir / "installKvmVms.sh").exists()
    with (k3s_only_dir / "configK3s.yaml").open("r", encoding="utf-8") as handle:
        k3s_config = yaml.safe_load(handle)
    assert "ssh" not in k3s_config
    assert "outputs" not in k3s_config
    assert k3s_config["nodes"][0]["ssh"]["key"] == "/home/lxl/.ssh/id_ed25519"
    existing_vm_config.unlink()

    output_dir = Path("/home/lxl/k8s/origin_k8s/emulate/output")
    running_dir = k8spre.writeRunningScripts(
        OUT,
        overwrite=True,
        output_dir=output_dir,
        image_registry_prefix="seedemu",
    )
    assert_file(running_dir / "Makefile")
    assert_file(running_dir / "configRunning.yaml")
    assert_file(running_dir / "manageK8sManifest.py")
    assert_file(running_dir / "buildRegistryImages.sh")
    assert_file(running_dir / "validateClusterPreflight.sh")
    with (running_dir / "configRunning.yaml").open("r", encoding="utf-8") as handle:
        running_config = yaml.safe_load(handle)
    assert running_config["outputDir"] == str(output_dir)
    assert running_config["setupConfig"] == str(setup_dir / "configK3s.yaml")
    assert running_config["imageRegistryPrefix"] == "seedemu"
    assert not (setup_dir / "configRunning.yaml").exists()

    print(f"setup_dir={setup_dir}")
    print(f"running_dir={running_dir}")
    print("k8spre smoke test passed")


if __name__ == "__main__":
    main()
