from __future__ import annotations

import copy
import getpass
import hashlib
import os
from pathlib import Path
from typing import Any

import yaml


def loadYaml(path: str | Path) -> dict[str, Any]:
    """Load a YAML mapping from path.

    Args:
        path: YAML file path supplied by the caller.

    Returns:
        Parsed YAML mapping. Empty files are treated as an empty mapping.
    """
    with Path(path).expanduser().open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Invalid YAML root in {path}: expected a mapping")
    return data


def writeYaml(path: str | Path, data: dict[str, Any]) -> None:
    """Write a YAML mapping to path, creating parent directories as needed.

    Args:
        path: Destination YAML file path.
        data: Mapping to serialize.
    """
    output = Path(path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle, default_flow_style=False, sort_keys=False)


def makeKvmConfig(
    *,
    config: str | Path | None = None,
    setup_dir: str | Path | None = None,
    cluster_name: str = "seedemu-k3s",
    ssh_user: str = "ubuntu",
    ssh_key: str = "~/.ssh/id_ed25519",
    registry_port: int = 5000,
    disk_dir: str | Path | None = None,
    base_image_path: str | Path | None = None,
    cloud_init_dir: str | Path | None = None,
    tmp_dir: str | Path | None = None,
    kubeconfig_path: str | Path | None = None,
    inventory_path: str | Path | None = None,
    k3s_config_path: str | Path | None = None,
    master: bool = True,
    workers: bool = True,
    master_vcpus: int = 12,
    master_memory_mb: int = 10240,
    master_disk_gb: int = 80,
    worker_count: int = 2,
    worker_vcpus: int = 6,
    worker_memory_mb: int = 10240,
    worker_disk_gb: int = 80,
) -> dict[str, Any]:
    """Create the KVM-stage YAML config consumed by setup scripts.

    Args:
        config: Optional user YAML. Existing fields have priority.
        setup_dir: Generated setup directory; used to derive output paths.
        cluster_name: Default K3s cluster name.
        ssh_user: Default SSH user for generated cloud-init users.
        ssh_key: Default SSH private key path used for VM access.
        registry_port: Default registry port on the K3s master.
        disk_dir: Optional KVM qcow2 disk directory override.
        base_image_path: Optional Ubuntu cloud image path override.
        cloud_init_dir: Optional cloud-init artifact directory override.
        tmp_dir: Optional setup temporary directory override.
        kubeconfig_path: Optional generated kubeconfig output path.
        inventory_path: Optional generated cluster inventory output path.
        k3s_config_path: Optional generated configK3s.yaml output path.
        master: Whether to request a generated master VM. Current KVM scripts
            require one master.
        workers: Whether to request generated worker VMs.
        master_vcpus: Default master vCPU count.
        master_memory_mb: Default master memory size in MiB.
        master_disk_gb: Default master disk size in GiB.
        worker_count: Default number of generated worker VMs.
        worker_vcpus: Default worker vCPU count.
        worker_memory_mb: Default worker memory size in MiB.
        worker_disk_gb: Default worker disk size in GiB.
    """
    if not master:
        raise ValueError("master=False is not supported by current KVM scripts")

    setup_path = Path(setup_dir).expanduser().resolve() if setup_dir is not None else None
    data = copy.deepcopy(loadYaml(config)) if config is not None else {}
    effective_cluster_name = str(data.get("clusterName") or data.get("cluster_name") or cluster_name)
    data["clusterName"] = effective_cluster_name
    data.pop("cluster_name", None)

    master_cfg = _mapping(data, "master")
    workers_cfg = _mapping(data, "workers")
    _normalizeLegacyResourceKeys(master_cfg)
    _normalizeLegacyResourceKeys(workers_cfg)
    _fillMissing(master_cfg, {"vcpus": master_vcpus, "memoryMb": master_memory_mb, "diskGb": master_disk_gb})
    _fillMissing(workers_cfg, {"count": worker_count, "vcpus": worker_vcpus, "memoryMb": worker_memory_mb, "diskGb": worker_disk_gb})

    if not workers:
        workers_cfg["count"] = 0

    ssh_cfg = _mapping(data, "ssh")
    _fillMissing(ssh_cfg, {"user": ssh_user, "key": ssh_key})

    registry_cfg = _mapping(data, "registry")
    _fillMissing(registry_cfg, {"port": registry_port})

    if setup_path is not None:
        kvm_data_dir = _defaultKvmDataDir(setup_path)
        kvm_defaults = {
            "storageDir": _portablePath(setup_path, setup_path),
            "diskDir": str(Path(disk_dir).expanduser().resolve()) if disk_dir is not None else str(kvm_data_dir / "disks"),
            "cloudInitDir": (
                str(Path(cloud_init_dir).expanduser().resolve())
                if cloud_init_dir is not None
                else _portablePath(setup_path / "cloud-init", setup_path)
            ),
            "baseImagePath": str(Path(base_image_path).expanduser().resolve()) if base_image_path is not None else str(kvm_data_dir / "base" / "jammy-server-cloudimg-amd64.img"),
        }
        output_defaults = {
            "tmpDir": str(Path(tmp_dir).expanduser().resolve()) if tmp_dir is not None else _portablePath(setup_path / "tmp", setup_path),
            "kubeconfig": (
                str(Path(kubeconfig_path).expanduser().resolve())
                if kubeconfig_path is not None
                else _portablePath(setup_path / f"{effective_cluster_name}.kubeconfig.yaml", setup_path)
            ),
            "inventory": (
                str(Path(inventory_path).expanduser().resolve())
                if inventory_path is not None
                else _portablePath(setup_path / f"{effective_cluster_name}.inventory.yaml", setup_path)
            ),
            "k3sConfig": (
                str(Path(k3s_config_path).expanduser().resolve())
                if k3s_config_path is not None
                else _portablePath(setup_path / "configK3s.yaml", setup_path)
            ),
            "kvmState": _portablePath(setup_path / "kvmState.yaml", setup_path),
        }
        kvm_cfg = _mapping(data, "kvm")
        outputs_cfg = _mapping(data, "outputs")
        _normalizeLegacyKvmKeys(kvm_cfg)
        _normalizeLegacyOutputKeys(outputs_cfg)
        outputs_cfg.pop("runningConfig", None)
        outputs_cfg.pop("running_config", None)
        _fillMissing(kvm_cfg, kvm_defaults)
        _fillMissing(outputs_cfg, output_defaults)
    else:
        _normalizeLegacyKvmKeys(_mapping(data, "kvm"))
        outputs_cfg = _mapping(data, "outputs")
        _normalizeLegacyOutputKeys(outputs_cfg)
        outputs_cfg.pop("runningConfig", None)
        outputs_cfg.pop("running_config", None)
    return data


def makeRunningConfig(
    *,
    setup_dir: str | Path,
    running_dir: str | Path,
    output_dir: str | Path | None = None,
    image_registry_prefix: str = "seedemu",
    rollout_timeout_seconds: int = 1800,
) -> dict[str, Any]:
    """Create running-stage YAML config consumed by the generated Makefile.

    Args:
        setup_dir: Setup directory containing configK3s.yaml.
        running_dir: Running scripts directory.
        output_dir: SeedEMU compile output directory. Defaults to
            ../emulate/output relative to the generated root.
        image_registry_prefix: Logical image prefix used in k8s.yaml.
        rollout_timeout_seconds: Rollout/wait timeout used by make up/wait.
    """
    setup_path = Path(setup_dir).expanduser().resolve()
    running_path = Path(running_dir).expanduser().resolve()
    if output_dir is None:
        output_path = (running_path.parent / "emulate" / "output").resolve()
    else:
        output_path = Path(output_dir).expanduser().resolve()
    return {
        "setupConfig": _portablePath(setup_path / "configK3s.yaml", running_path),
        "outputDir": _portablePath(output_path, running_path),
        "imageRegistryPrefix": image_registry_prefix,
        "rolloutTimeoutSeconds": rollout_timeout_seconds,
    }


def makeK3sConfig(
    *,
    config: str | Path | None = None,
    setup_dir: str | Path | None = None,
    cluster_name: str = "seedemu-k3s",
    ssh_user: str = "ubuntu",
    ssh_key: str = "~/.ssh/id_ed25519",
    registry_port: int = 5000,
) -> dict[str, Any]:
    """Create configK3s.yaml for existing or newly created VMs.

    Args:
        config: Optional YAML containing at least a nodes list. A minimal node
            item can be {"ip": "192.168.122.10"}; name/role are inferred later.
        setup_dir: Generated setup directory; used to derive output paths.
        cluster_name: Default K3s cluster name.
        ssh_user: Default SSH user for all nodes.
        ssh_key: Default SSH private key path.
        registry_port: Default registry port on the master.
    """
    setup_path = Path(setup_dir).expanduser().resolve() if setup_dir is not None else None
    data = copy.deepcopy(loadYaml(config)) if config is not None else {}
    effective_cluster_name = str(data.get("clusterName") or data.get("cluster_name") or cluster_name)
    data["clusterName"] = effective_cluster_name
    data.pop("cluster_name", None)

    if "nodes" not in data:
        data["nodes"] = []
    if not isinstance(data["nodes"], list):
        raise ValueError("configK3s.yaml field 'nodes' must be a list")

    default_ssh = data.get("ssh") if isinstance(data.get("ssh"), dict) else {}
    default_user = default_ssh.get("user") or ssh_user
    default_key = default_ssh.get("key") or ssh_key
    for node in data["nodes"]:
        if not isinstance(node, dict):
            raise ValueError(f"configK3s.yaml node item must be a mapping: {node}")
        ssh_cfg = _mapping(node, "ssh")
        _fillMissing(ssh_cfg, {"user": default_user, "key": default_key})
        # Passwords are only useful for a one-time manual SSH key bootstrap.
        # Generated K3s configs should not replicate them because the build
        # scripts require key-based SSH and sudo -n.
        ssh_cfg.pop("password", None)
        ssh_cfg.pop("passwd", None)
    data.pop("ssh", None)
    if "registry" in data:
        registry_cfg = _mapping(data, "registry")
        _fillMissing(registry_cfg, {"port": registry_port})
        if registry_cfg == {"port": registry_port}:
            data.pop("registry", None)
    data.pop("outputs", None)
    return data


def _fillMissing(target: dict[str, Any], defaults: dict[str, Any]) -> None:
    for key, value in defaults.items():
        if key not in target or target[key] is None:
            target[key] = value


def _mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if value is None:
        value = {}
        data[key] = value
    if not isinstance(value, dict):
        raise ValueError(f"Invalid config: '{key}' must be a mapping")
    return value


def _defaultKvmDataDir(setup_path: Path) -> Path:
    # KVM disks are placed under /data so libvirt does not depend on access to
    # a user's home directory.
    root = Path(f"/data/{getpass.getuser()}/k8spre").expanduser()
    base = setup_path.parent
    digest = hashlib.sha1(str(base).encode("utf-8")).hexdigest()[:8]
    return root / f"{base.name}-{digest}"


def _portablePath(path: Path, base_dir: Path) -> str:
    """Return a readable path for generated YAML.

    Args:
        path: Absolute path to write.
        base_dir: Directory that will contain the generated YAML.

    Paths inside the same nearby source tree are written relative to the YAML
    file's directory. Unrelated paths, such as a repo output consumed from a
    temporary directory, stay absolute so copied generated directories continue
    to resolve correctly.
    """
    path = path.expanduser().resolve()
    base_dir = base_dir.expanduser().resolve()
    common = Path(os.path.commonpath([str(path), str(base_dir)]))
    if common == Path(path.anchor):
        return str(path)
    relative = Path(os.path.relpath(path, start=base_dir))
    if len(relative.parts) <= 8:
        return relative.as_posix()
    return str(path)


def _normalizeLegacyResourceKeys(data: dict[str, Any]) -> None:
    aliases = {"memory_mb": "memoryMb", "disk_gb": "diskGb", "name_prefix": "namePrefix"}
    for old, new in aliases.items():
        if old in data and new not in data:
            data[new] = data.pop(old)


def _normalizeLegacyKvmKeys(data: dict[str, Any]) -> None:
    aliases = {
        "storage_dir": "storageDir",
        "disk_dir": "diskDir",
        "cloud_init_dir": "cloudInitDir",
        "base_image_path": "baseImagePath",
        "legacy_base_image_path": "legacyBaseImagePath",
        "base_image_url": "baseImageUrl",
        "ubuntu_series": "ubuntuSeries",
        "boot_timeout_seconds": "bootTimeoutSeconds",
        "allow_existing": "allowExisting",
    }
    for old, new in aliases.items():
        if old in data and new not in data:
            data[new] = data.pop(old)


def _normalizeLegacyOutputKeys(data: dict[str, Any]) -> None:
    aliases = {"tmp_dir": "tmpDir", "kvm_state": "kvmState"}
    for old, new in aliases.items():
        if old in data and new not in data:
            data[new] = data.pop(old)


# Backward-compatible aliases for callers that still use the first prototype.
load_yaml = loadYaml
write_yaml = writeYaml
make_kvm_config = makeKvmConfig
make_k3s_config = makeK3sConfig
make_running_config = makeRunningConfig
