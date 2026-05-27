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
    "kvm",
    "manageK3sConfig.py",
]
K3S_SETUP_RESOURCE_ITEMS = [
    "README.md",
    "applyK3sCluster.sh",
    "manageK3sConfig.py",
    "ansible",
]
PHYSICAL_SETUP_RESOURCE_ITEMS = [
    "README.md",
    "preparePhysicalNodes.sh",
    "destroyPhysicalCluster.sh",
    "manageK3sConfig.py",
]
FABRIC_RESOURCE_DIRS = ("vxlan", "ovn")
DEFAULT_SEED_NAMESPACE = "seedemu-k3s-real-topo"
SETUP_README_CONTEXT_START = "<!-- K8SPRE_GENERATED_CONTEXT_START -->"
SETUP_README_CONTEXT_END = "<!-- K8SPRE_GENERATED_CONTEXT_END -->"


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
        connection: str | None = None,
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
            connection: Optional SeedEMU secondary network backend for the
                K3s stage. Use "ovn" for KVM + Kube-OVN; omit it to keep the
                historical KVM + macvlan-on-ens2 flow.
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
        _applyKvmConnectionDefault(kvm_config, connection)
        writeYaml(setup_dir / "kvm.yaml", kvm_config)
        writeExecutableScript(setup_dir / KVM_INSTALL_ENTRYPOINT, _installKvmVmsEntrypoint())
        writeExecutableScript(setup_dir / "destroyKvmVms.sh", _destroyKvmVmsEntrypoint())
        _writeSetupReadmeContext(setup_dir)
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
        k3s_config = None

        if config is not None:
            config_data = loadYaml(config)
            if "nodes" not in config_data:
                raise ValueError(
                    "writeK3sBuildScripts(config=...) expects configK3s.yaml with a nodes list. "
                    "For the KVM flow, run installKvmVms.sh first so it generates configK3s.yaml."
                )
            k3s_config = makeK3sConfig(config=config, setup_dir=setup_dir)
            writeYaml(setup_dir / "configK3s.yaml", k3s_config)
        elif (setup_dir / "configK3s.yaml").exists():
            k3s_config = loadYaml(setup_dir / "configK3s.yaml")
        elif (setup_dir / "kvm.yaml").exists():
            # KVM creation writes configK3s.yaml later, but kvm.yaml already
            # carries fabric.type. Copy the matching fabric resources now so a
            # generated K3s build directory is complete before installKvmVms.sh
            # runs.
            k3s_config = loadYaml(setup_dir / "kvm.yaml")

        if k3s_config is not None:
            _copySelectedFabricResources(setup_dir, k3s_config, overwrite=overwrite)

        writeExecutableScript(setup_dir / K3S_BUILD_ENTRYPOINT, _buildK3sClusterEntrypoint())
        _writeSetupReadmeContext(setup_dir)
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

    def writePhysicalNodeScripts(
        self,
        path: str | Path,
        *,
        config: str | Path | None = None,
        connection: str | None = None,
        overwrite: bool = False,
    ) -> Path:
        """Write physical-node preparation and fabric scripts.

        Args:
            path: Output root. Scripts are written to path/setup.
            config: Optional configK3s.yaml source for existing physical
                servers. It should contain nodes[].{role,ip,ssh} and, when a
                synthetic L2 fabric is needed, fabric.type=linux-vxlan.
            connection: Physical fabric backend. Supported values are "vxlan"
                and "ovn". When omitted, an existing config fabric.type is
                preserved; otherwise vxlan is used as the default.
            overwrite: Replace physical-node scripts with bundled versions.

        Returns:
            The generated setup directory path.
        """
        base_dir = Path(path).expanduser()
        setup_dir = base_dir / "setup"
        setup_dir = copyResourceItems("setup", PHYSICAL_SETUP_RESOURCE_ITEMS, setup_dir, overwrite=overwrite)
        k3s_config = None
        if config is not None:
            config_data = loadYaml(config)
            if "nodes" not in config_data:
                raise ValueError("writePhysicalNodeScripts(config=...) expects configK3s.yaml with a nodes list")
            k3s_config = makeK3sConfig(config=config, setup_dir=setup_dir)
            _applyPhysicalConnectionDefault(k3s_config, connection)
            writeYaml(setup_dir / "configK3s.yaml", k3s_config)
        elif (setup_dir / "configK3s.yaml").exists():
            k3s_config = loadYaml(setup_dir / "configK3s.yaml")
            _applyPhysicalConnectionDefault(k3s_config, connection)
            writeYaml(setup_dir / "configK3s.yaml", k3s_config)
        else:
            raise FileNotFoundError(f"{setup_dir / 'configK3s.yaml'} does not exist; pass config=... for physical nodes")
        _copySelectedFabricResources(setup_dir, k3s_config, overwrite=overwrite)
        _writeSetupReadmeContext(setup_dir)
        _removeGeneratedGitignore(setup_dir)
        chmodScripts(setup_dir)
        return setup_dir

    def preparePhysicalNodes(
        self,
        *,
        path: str | Path | None = None,
        config: str | Path | None = None,
        connection: str | None = None,
        overwrite: bool = True,
    ) -> list[subprocess.CompletedProcess]:
        """Generate physical-node scripts and validate/configure the L2 fabric.

        Args:
            path: Output root. A temporary root is used when omitted.
            config: configK3s.yaml source for existing physical servers.
            connection: Optional fabric backend selector. Supported values are
                "vxlan" and "ovn". OVN fabric is installed during K3s build.
            overwrite: Replace generated physical-node scripts before running.

        Returns:
            CompletedProcess objects for preflight, fabric setup, and fabric
            validation, in that order.
        """
        base_dir = Path(path).expanduser() if path is not None else Path(
            tempfile.mkdtemp(prefix="seedemu-k8spre-")
        )
        setup_dir = self.writePhysicalNodeScripts(base_dir, config=config, connection=connection, overwrite=overwrite)
        k3s_config = loadYaml(setup_dir / "configK3s.yaml")
        scripts = ["preparePhysicalNodes.sh"]
        if _selectedFabricResourceDirs(k3s_config) == ["vxlan"]:
            scripts.extend(
                [
                    "vxlan/configureLinuxVxlanFabric.sh",
                    "vxlan/validateLinuxVxlanFabric.sh",
                ]
            )
        results = []
        for script_name in scripts:
            results.append(
                subprocess.run(
                    [str(setup_dir / script_name), str(setup_dir / "configK3s.yaml")],
                    cwd=str(setup_dir),
                    check=True,
                )
            )
        return results

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
        bash ./kvm/prepareHostAssets.sh ./kvm.yaml

        echo "[K8sPre] Creating KVM virtual machines..."
        bash ./kvm/createKvmVms.sh ./kvm.yaml

        echo "[K8sPre] Unlocking VM limits..."
        bash ./kvm/tuneVmLimits.sh ./configK3s.yaml

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


def _destroyKvmVmsEntrypoint() -> str:
    return textwrap.dedent(
        """\
        #!/usr/bin/env bash
        # Destroy KVM VMs recorded in kvmState.yaml. This root-level wrapper
        # keeps the generated setup interface stable while KVM internals live
        # under setup/kvm/.
        set -euo pipefail

        SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
        cd "$SCRIPT_DIR"

        bash ./kvm/destroyKvmVms.sh "${1:-./kvmState.yaml}"
        """
    )


def _applyPhysicalConnectionDefault(config: dict[str, Any], connection: str | None) -> None:
    """Set or override the physical fabric backend selected by the caller.

    Args:
        config: Parsed configK3s.yaml mapping that will be written to setup/.
        connection: Optional user-facing backend selector. "vxlan" keeps the
            existing Linux VXLAN bridge path; "ovn" enables Kube-OVN
            non-primary CNI. When omitted, an existing fabric.type is kept and
            missing fabric.type defaults to vxlan.
    """
    fabric = config.get("fabric")
    if fabric is None:
        fabric = {}
        config["fabric"] = fabric
    if not isinstance(fabric, dict):
        raise ValueError("configK3s.yaml field fabric must be a mapping when present")
    if connection is None and (fabric.get("type") or fabric.get("backend")):
        return

    normalized = (connection or "vxlan").strip().lower()
    if normalized in {"vxlan", "linux-vxlan", "linux_vxlan"}:
        fabric["type"] = "linux-vxlan"
    elif normalized in {"ovn", "kube-ovn", "kube_ovn"}:
        fabric["type"] = "ovn"
        k3s = config.get("k3s")
        if k3s is None:
            k3s = {}
            config["k3s"] = k3s
        if not isinstance(k3s, dict):
            raise ValueError("configK3s.yaml field k3s must be a mapping when present")
        k3s.setdefault("version", "v1.29.15+k3s1")
    else:
        raise ValueError(f"Unsupported physical connection backend: {connection}")


def _applyKvmConnectionDefault(config: dict[str, Any], connection: str | None) -> None:
    """Set the K3s secondary-network backend for generated KVM clusters.

    Args:
        config: Parsed kvm.yaml mapping that will later be converted into
            configK3s.yaml by setup/kvm/manageKvmConfig.py.
        connection: Optional backend selector. "ovn" enables Kube-OVN for
            SeedEMU secondary interfaces. "macvlan", "none", or None keep the
            historical KVM path where compiler NADs use macvlan on ens2.
    """
    if connection is None:
        return

    normalized = connection.strip().lower()
    if normalized in {"macvlan", "none", ""}:
        return
    if normalized in {"ovn", "kube-ovn", "kube_ovn"}:
        fabric = config.get("fabric")
        if fabric is None:
            fabric = {}
            config["fabric"] = fabric
        if not isinstance(fabric, dict):
            raise ValueError("kvm.yaml field fabric must be a mapping when present")
        fabric["type"] = "ovn"

        k3s = config.get("k3s")
        if k3s is None:
            k3s = {}
            config["k3s"] = k3s
        if not isinstance(k3s, dict):
            raise ValueError("kvm.yaml field k3s must be a mapping when present")
        k3s.setdefault("version", "v1.29.15+k3s1")
        return
    raise ValueError(f"Unsupported KVM connection backend: {connection}")


def _selectedFabricResourceDirs(config: dict[str, Any]) -> list[str]:
    """Return the setup resource subdirectory required by config.fabric.type.

    Args:
        config: Parsed configK3s.yaml mapping after optional connection
            defaults have been applied.
    """
    fabric = config.get("fabric") or {}
    if not isinstance(fabric, dict):
        raise ValueError("configK3s.yaml field fabric must be a mapping when present")
    fabric_type = str(fabric.get("type") or fabric.get("backend") or "none").strip().lower()
    if fabric_type in {"none", "", "null"}:
        return []
    if fabric_type in {"vxlan", "linux-vxlan", "linux_vxlan"}:
        return ["vxlan"]
    if fabric_type in {"ovn", "kube-ovn", "kube_ovn"}:
        return ["ovn"]
    raise ValueError(f"Unsupported physical fabric.type: {fabric_type}")


def _copySelectedFabricResources(setup_dir: Path, config: dict[str, Any], *, overwrite: bool) -> None:
    """Copy only the fabric resource directory required by configK3s.yaml.

    Args:
        setup_dir: Generated setup directory.
        config: Parsed configK3s.yaml mapping.
        overwrite: When true, remove stale fabric directories from previous
            generations so OVN outputs do not contain vxlan/ and vice versa.
    """
    selected = _selectedFabricResourceDirs(config)
    if selected:
        copyResourceItems("setup", selected, setup_dir, overwrite=overwrite)
    if not overwrite:
        return
    for item_name in FABRIC_RESOURCE_DIRS:
        if item_name in selected:
            continue
        stale_item = setup_dir / item_name
        if stale_item.exists():
            shutil.rmtree(stale_item)


def _removeGeneratedGitignore(setup_dir: Path) -> None:
    """Remove generated .gitignore from user-facing setup outputs.

    Args:
        setup_dir: Generated setup directory.
    """
    gitignore = setup_dir / ".gitignore"
    if gitignore.exists():
        gitignore.unlink()


def _writeSetupReadmeContext(setup_dir: Path) -> None:
    """Write concrete kubeconfig and namespace hints into setup/README.md.

    Args:
        setup_dir: Generated setup directory containing README.md and optional
            kvm.yaml/configK3s.yaml. The helper reads those YAML files only to
            resolve clusterName and outputs.kubeconfig.
    """
    readme = setup_dir / "README.md"
    if not readme.exists():
        return

    cluster_name = _readSetupClusterName(setup_dir)
    kubeconfig = _readSetupKubeconfigPath(setup_dir, cluster_name)
    namespace = DEFAULT_SEED_NAMESPACE
    context = textwrap.dedent(
        f"""\
        {SETUP_README_CONTEXT_START}
        ## 当前生成目录上下文

        这个区块由 `seedemu.k8spre.K8sPre` 生成，用来把本次输出目录中的具体路径固定下来：

        - Kubeconfig: `{kubeconfig}`
        - 默认实验 namespace: `{namespace}`
        - K3s 配置: `{(setup_dir / "configK3s.yaml").resolve()}`

        `buildK3sCluster.sh` 成功后，可以直接使用：

        ```bash
        export KUBECONFIG="{kubeconfig}"
        export SEED_NAMESPACE="{namespace}"
        kubectl get nodes -o wide
        kubectl -n "${{SEED_NAMESPACE}}" get pods -o wide
        ```

        进入一个 Pod：

        ```bash
        POD="$(kubectl -n "${{SEED_NAMESPACE}}" get pods -o jsonpath='{{.items[0].metadata.name}}')"
        kubectl -n "${{SEED_NAMESPACE}}" exec -it "${{POD}}" -- bash
        ```

        如果镜像里没有 `bash`，把最后一行的 `bash` 改成 `sh`。
        {SETUP_README_CONTEXT_END}
        """
    )

    text = readme.read_text(encoding="utf-8")
    start = text.find(SETUP_README_CONTEXT_START)
    end = text.find(SETUP_README_CONTEXT_END)
    if start != -1 and end != -1 and end >= start:
        end += len(SETUP_README_CONTEXT_END)
        updated = text[:start].rstrip() + "\n\n" + context.rstrip() + "\n\n" + text[end:].lstrip()
    else:
        updated = text.rstrip() + "\n\n" + context
    readme.write_text(updated, encoding="utf-8")


def _readSetupClusterName(setup_dir: Path) -> str:
    """Return clusterName from generated setup YAML, falling back to default.

    Args:
        setup_dir: Generated setup directory to inspect.
    """
    for name in ("configK3s.yaml", "kvm.yaml"):
        data = _loadSetupYamlIfPresent(setup_dir / name)
        cluster_name = data.get("clusterName") or data.get("cluster_name")
        if cluster_name:
            return str(cluster_name)
    return "seedemu-k3s"


def _readSetupKubeconfigPath(setup_dir: Path, cluster_name: str) -> Path:
    """Resolve the kubeconfig path that setup scripts are expected to write.

    Args:
        setup_dir: Generated setup directory to inspect.
        cluster_name: Cluster name used for the default kubeconfig filename.
    """
    for name in ("configK3s.yaml", "kvm.yaml"):
        data = _loadSetupYamlIfPresent(setup_dir / name)
        outputs = data.get("outputs")
        if isinstance(outputs, dict):
            path = outputs.get("kubeconfig")
            if path:
                candidate = Path(str(path)).expanduser()
                if not candidate.is_absolute():
                    candidate = setup_dir / candidate
                return candidate.resolve()
    return (setup_dir / f"{cluster_name}.kubeconfig.yaml").resolve()


def _loadSetupYamlIfPresent(path: Path) -> dict[str, Any]:
    """Load a setup YAML mapping if it exists; otherwise return an empty map.

    Args:
        path: YAML file path under the generated setup directory.
    """
    if not path.exists():
        return {}
    try:
        return loadYaml(path)
    except Exception:
        return {}


# Backward-compatible aliases for the first prototype API.
K8sPre.kvminstall_script = K8sPre.writeKvmInstallScripts
K8sPre.kvminstall = K8sPre.installKvmVms
K8sPre.k8sbuild_script = K8sPre.writeK3sBuildScripts
K8sPre.k8sbuild = K8sPre.buildK3sCluster
K8sPre.running_scripts = K8sPre.writeRunningScripts
K8sPre.physical_node_scripts = K8sPre.writePhysicalNodeScripts
K8sPre.physical_nodes = K8sPre.preparePhysicalNodes
