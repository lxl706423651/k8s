from __future__ import annotations

import subprocess
import shutil
import tempfile
import textwrap
from pathlib import Path
from typing import Any

from .config import loadYaml, makeK3sConfig, makeKvmConfig, makeRunningConfig, writeYaml
from .utils import chmodScripts, copyResourceItems, copyTree, writeExecutableScript


KVM_INSTALL_ENTRYPOINT = "installKvmVms.sh"
K3S_BUILD_ENTRYPOINT = "buildK3sCluster.sh"
KVM_SETUP_RESOURCE_ITEMS = [
    "README.md",
    "prepareHostAssets.sh",
    "createKvmVms.sh",
    "tuneVmLimits.sh",
    "destroyKvmVms.sh",
    "manageKvmConfig.py",
    "manageK3sConfig.py",
]
K3S_SETUP_RESOURCE_ITEMS = [
    "README.md",
    "applyK3sCluster.sh",
    "manageK3sConfig.py",
    "ansible",
]


class K8sPre:
    """Generate and optionally execute lightweight SeedEMU/K3s helper scripts."""

    def writeKvmInstallScripts(
        self,
        path: str | Path,
        *,
        config: str | Path | None = None,
        master: bool = True,
        workers: bool = True,
        master_vcpus: int = 12,
        master_memory_mb: int = 10240,
        master_disk_gb: int = 80,
        worker_count: int = 2,
        worker_vcpus: int = 6,
        worker_memory_mb: int = 10240,
        worker_disk_gb: int = 80,
        cluster_name: str = "seedemu-k3s",
        ssh_user: str = "ubuntu",
        ssh_key: str = "~/.ssh/id_ed25519",
        registry_port: int = 5000,
        disk_dir: str | Path | None = None,
        overwrite: bool = False,
    ) -> Path:
        """Write setup scripts and kvm.yaml for the KVM creation stage.

        Args:
            path: Output root. Scripts are written to path/setup.
            config: Optional user kvm YAML. Existing YAML fields win over
                function defaults.
            master: Whether to create a master VM. Current scripts require it.
            workers: Whether to create worker VMs.
            master_vcpus: Default master vCPU count.
            master_memory_mb: Default master memory in MiB.
            master_disk_gb: Default master disk in GiB.
            worker_count: Default worker VM count.
            worker_vcpus: Default worker vCPU count.
            worker_memory_mb: Default worker memory in MiB.
            worker_disk_gb: Default worker disk in GiB.
            cluster_name: Default cluster name written to generated YAML.
            ssh_user: Default VM SSH user.
            ssh_key: Default SSH private key path.
            registry_port: Default registry port on the master.
            disk_dir: Optional KVM disk directory override.
            overwrite: Replace an existing setup directory if true.
        """
        base_dir = Path(path).expanduser()
        setup_dir = base_dir / "setup"
        if setup_dir.exists() and overwrite:
            shutil.rmtree(setup_dir)
        if setup_dir.exists() and not overwrite and (setup_dir / "kvm.yaml").exists():
            raise FileExistsError(f"{setup_dir / 'kvm.yaml'} already exists; use overwrite=True to replace it")
        setup_dir = copyResourceItems("setup", KVM_SETUP_RESOURCE_ITEMS, setup_dir, overwrite=overwrite)
        kvm_config = makeKvmConfig(
            config=config,
            setup_dir=setup_dir,
            cluster_name=cluster_name,
            ssh_user=ssh_user,
            ssh_key=ssh_key,
            registry_port=registry_port,
            disk_dir=disk_dir,
            master=master,
            workers=workers,
            master_vcpus=master_vcpus,
            master_memory_mb=master_memory_mb,
            master_disk_gb=master_disk_gb,
            worker_count=worker_count,
            worker_vcpus=worker_vcpus,
            worker_memory_mb=worker_memory_mb,
            worker_disk_gb=worker_disk_gb,
        )
        writeYaml(setup_dir / "kvm.yaml", kvm_config)
        writeExecutableScript(setup_dir / KVM_INSTALL_ENTRYPOINT, _installKvmVmsEntrypoint())
        _removeGeneratedGitignore(setup_dir)
        chmodScripts(setup_dir)
        return setup_dir

    def installKvmVms(
        self,
        *,
        path: str | Path | None = None,
        config: str | Path | None = None,
        overwrite: bool = True,
        **kwargs: Any,
    ) -> subprocess.CompletedProcess:
        """Generate setup scripts and run the KVM creation entrypoint.

        Args:
            path: Output root. A temporary root is used when omitted.
            config: Optional user kvm YAML.
            overwrite: Replace generated scripts before execution.
            **kwargs: Forwarded to writeKvmInstallScripts.
        """
        base_dir = Path(path).expanduser() if path is not None else Path(
            tempfile.mkdtemp(prefix="seedemu-k8spre-")
        )
        setup_dir = self.writeKvmInstallScripts(
            base_dir,
            config=config,
            overwrite=overwrite,
            **kwargs,
        )
        return subprocess.run(
            [str(setup_dir / KVM_INSTALL_ENTRYPOINT)],
            cwd=str(setup_dir),
            check=True,
        )

    def writeK3sBuildScripts(
        self,
        path: str | Path,
        *,
        config: str | Path | None = None,
        overwrite: bool = False,
    ) -> Path:
        """Write only the K3s-build stage scripts.

        Args:
            path: Output root. Scripts are written to path/setup.
            config: Optional configK3s.yaml source. It must contain a nodes
                list. KVM-stage YAML is intentionally not accepted here.
            overwrite: Replace K3s-stage scripts while preserving KVM-stage
                files such as kvm.yaml, configK3s.yaml, and kvmState.yaml.
        """
        base_dir = Path(path).expanduser()
        setup_dir = base_dir / "setup"
        setup_dir = copyResourceItems("setup", K3S_SETUP_RESOURCE_ITEMS, setup_dir, overwrite=overwrite)

        if config is not None:
            config_data = loadYaml(config)
            if "nodes" not in config_data:
                raise ValueError(
                    "writeK3sBuildScripts(config=...) expects configK3s.yaml with a nodes list. "
                    "For the KVM flow, run installKvmVms.sh first so it generates configK3s.yaml."
                )
            writeYaml(setup_dir / "configK3s.yaml", makeK3sConfig(config=config, setup_dir=setup_dir))

        writeExecutableScript(setup_dir / K3S_BUILD_ENTRYPOINT, _buildK3sClusterEntrypoint())
        _removeGeneratedGitignore(setup_dir)
        chmodScripts(setup_dir)
        return setup_dir

    def buildK3sCluster(
        self,
        *,
        path: str | Path | None = None,
        config: str | Path | None = None,
        overwrite: bool = True,
    ) -> subprocess.CompletedProcess:
        """Generate setup scripts and run the K3s build entrypoint.

        Args:
            path: Output root. A temporary root is used when omitted.
            config: Optional kvm.yaml source.
            overwrite: Replace generated scripts before execution.
        """
        base_dir = Path(path).expanduser() if path is not None else Path(
            tempfile.mkdtemp(prefix="seedemu-k8spre-")
        )
        setup_dir = self.writeK3sBuildScripts(base_dir, config=config, overwrite=overwrite)
        return subprocess.run(
            [str(setup_dir / K3S_BUILD_ENTRYPOINT)],
            cwd=str(setup_dir),
            check=True,
        )

    def writeRunningScripts(
        self,
        path: str | Path,
        *,
        output_dir: str | Path | None = None,
        image_registry_prefix: str = "seedemu",
        rollout_timeout_seconds: int = 1800,
        overwrite: bool = False,
    ) -> Path:
        """Write running scripts and configRunning.yaml.

        Args:
            path: Output root. Scripts are written to path/running.
            output_dir: SeedEMU compile output directory containing k8s.yaml and
                images.yaml. Defaults to path/emulate/output.
            image_registry_prefix: Logical image registry prefix used by the
                compiler output before kustomize rewrites it.
            rollout_timeout_seconds: Timeout used by make up/wait.
            overwrite: Replace an existing running directory if true.
        """
        base_dir = Path(path).expanduser()
        running_dir = copyTree("running", base_dir / "running", overwrite=overwrite)
        setup_dir = base_dir / "setup"
        running_config = makeRunningConfig(
            setup_dir=setup_dir,
            running_dir=running_dir,
            output_dir=output_dir,
            image_registry_prefix=image_registry_prefix,
            rollout_timeout_seconds=rollout_timeout_seconds,
        )
        writeYaml(running_dir / "configRunning.yaml", running_config)
        chmodScripts(running_dir)
        return running_dir


def _installKvmVmsEntrypoint() -> str:
    return textwrap.dedent(
        """\
        #!/usr/bin/env bash
        # Create KVM VMs from kvm.yaml, generate configK3s.yaml/kvmState.yaml,
        # and tune VM OS limits.
        set -euo pipefail

        SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
        cd "$SCRIPT_DIR"

        echo "[K8sPre] Preparing setup assets..."
        bash ./prepareHostAssets.sh ./kvm.yaml

        echo "[K8sPre] Creating KVM virtual machines..."
        bash ./createKvmVms.sh ./kvm.yaml

        echo "[K8sPre] Unlocking VM limits..."
        bash ./tuneVmLimits.sh ./configK3s.yaml

        echo "[K8sPre] KVM installation finished."
        """
    )


def _buildK3sClusterEntrypoint() -> str:
    return textwrap.dedent(
        """\
        #!/usr/bin/env bash
        # Build a K3s cluster from configK3s.yaml. This intentionally refuses
        # to infer cluster membership from ambient environment variables.
        set -euo pipefail

        SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
        cd "$SCRIPT_DIR"

        if [ ! -s ./configK3s.yaml ]; then
            echo "[K8sPre] Missing configK3s.yaml." >&2
            echo "[K8sPre] Run bash ./installKvmVms.sh first, or provide configK3s.yaml for existing VMs." >&2
            exit 1
        fi

        echo "[K8sPre] Building Kubernetes/K3s cluster from configK3s.yaml..."
        bash ./applyK3sCluster.sh ./configK3s.yaml

        echo "[K8sPre] Kubernetes/K3s build finished."
        """
    )


def _removeGeneratedGitignore(setup_dir: Path) -> None:
    """Remove generated .gitignore from user-facing setup outputs.

    Args:
        setup_dir: Generated setup directory.
    """
    gitignore = setup_dir / ".gitignore"
    if gitignore.exists():
        gitignore.unlink()


# Backward-compatible aliases for the first prototype API.
K8sPre.kvminstall_script = K8sPre.writeKvmInstallScripts
K8sPre.kvminstall = K8sPre.installKvmVms
K8sPre.k8sbuild_script = K8sPre.writeK3sBuildScripts
K8sPre.k8sbuild = K8sPre.buildK3sCluster
K8sPre.running_scripts = K8sPre.writeRunningScripts
