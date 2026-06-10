#!/usr/bin/env python3
"""Render B62 KVM/K3s input files from assignment.yaml.

Inputs:
- assignment.yaml with experiment size, master resources, worker count, and
  cluster addressing.

Outputs:
- configKvmOvn.yaml for k8sTools.py build.
- resourcePlan.yaml/json with the computed VM resource distribution.

Side effects:
- Creates the requested run directory. It does not create VMs or Kubernetes
  resources.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
from typing import Any

import yaml


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_ASSIGNMENT = SCRIPT_DIR / "assignment.yaml"


def loadYaml(path: Path) -> dict[str, Any]:
    """Load one YAML mapping from path."""
    data = yaml.safe_load(path.expanduser().read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"Invalid YAML root in {path}: expected mapping")
    return data


def getNested(data: dict[str, Any], dotted: str, default: Any = None) -> Any:
    """Return a dotted-path YAML value or default when absent."""
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def resolvePath(value: str | Path, base: Path = SCRIPT_DIR) -> Path:
    """Resolve a user path relative to base after expanding ~ and variables."""
    expanded = Path(os.path.expandvars(os.path.expanduser(str(value))))
    if expanded.is_absolute():
        return expanded.resolve()
    return (base / expanded).resolve()


def memoryMib(value: Any, *, default: int | None = None) -> int:
    """Convert a YAML memory value to MiB."""
    if value is None:
        if default is None:
            raise SystemExit("missing memory value")
        return default
    if isinstance(value, str):
        raw = value.strip().lower()
        if raw.endswith("gib") or raw.endswith("gb") or raw.endswith("g"):
            return int(float(raw.rstrip("gib").rstrip("gb").rstrip("g")) * 1024)
        if raw.endswith("mib") or raw.endswith("mb") or raw.endswith("m"):
            return int(float(raw.rstrip("mib").rstrip("mb").rstrip("m")))
        return int(raw)
    return int(value)


def boolValue(value: Any, *, default: bool = False) -> bool:
    """Convert common YAML/string booleans to bool."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "on"}
    return bool(value)


def giBToMiB(value: Any) -> int:
    """Convert a GiB YAML number to MiB."""
    if value is None:
        raise SystemExit("missing GiB memory value")
    return int(float(value) * 1024)


def distributeTotal(total: int, count: int) -> list[int]:
    """Split total into count integer buckets with at most one unit difference."""
    if count <= 0:
        raise SystemExit("workerCount must be positive")
    base = total // count
    remainder = total % count
    return [base + (1 if index < remainder else 0) for index in range(count)]


def resolveDataRoot(worker_count: int) -> Path:
    """Return the root used for KVM disk/cloud-init data."""
    user = getpass.getuser()
    data_user = Path("/data") / user
    if data_user.is_dir() or Path("/data").is_dir():
        return (data_user / "k8sTools" / "b62" / f"w{worker_count}").resolve()
    return (SCRIPT_DIR / "runtime" / "kvm" / f"w{worker_count}").resolve()


def createResourcePlan(assignment: dict[str, Any]) -> dict[str, Any]:
    """Create the fixed-total resource plan from assignment.yaml."""
    worker_count = int(getNested(assignment, "experiment.workerCount"))
    total_vcpus = int(getNested(assignment, "resources.totalVcpus", 300))
    total_memory_mib = getNested(assignment, "resources.totalMemoryMiB", None)
    if total_memory_mib is not None:
        total_memory = memoryMib(total_memory_mib)
    else:
        total_memory = giBToMiB(getNested(assignment, "resources.totalMemoryGiB", 600))
    master_vcpus = int(getNested(assignment, "resources.master.vcpus"))
    master_memory_mib = getNested(assignment, "resources.master.memoryMiB", None)
    if master_memory_mib is not None:
        master_memory = memoryMib(master_memory_mib)
    else:
        master_memory = giBToMiB(getNested(assignment, "resources.master.memoryGiB", None))
    default_disk = int(getNested(assignment, "resources.diskGb", 80))
    master_disk = int(getNested(assignment, "resources.master.diskGb", default_disk))
    worker_disk = int(getNested(assignment, "resources.workers.diskGb", default_disk))

    worker_vcpus_total = total_vcpus - master_vcpus
    worker_memory_total = total_memory - master_memory
    if worker_vcpus_total <= 0 or worker_memory_total <= 0:
        raise SystemExit("Total CPU/memory budget must exceed master CPU/memory")

    worker_vcpus = distributeTotal(worker_vcpus_total, worker_count)
    worker_memory = distributeTotal(worker_memory_total, worker_count)
    return {
        "total": {"vcpus": total_vcpus, "memoryMb": total_memory, "diskGb": default_disk},
        "master": {"vcpus": master_vcpus, "memoryMb": master_memory, "diskGb": master_disk},
        "workers": [
            {"index": index + 1, "vcpus": worker_vcpus[index], "memoryMb": worker_memory[index], "diskGb": worker_disk}
            for index in range(worker_count)
        ],
    }


def createKvmNodes(assignment: dict[str, Any], plan: dict[str, Any], worker_count: int) -> list[dict[str, Any]]:
    """Create explicit k8sTools node entries from master config and worker count."""
    ip_prefix = str(getNested(assignment, "kvm.ipPrefix"))
    master_name = f"{getNested(assignment, 'kvm.master.namePrefix')}-w{worker_count}"
    master_ip_start = int(getNested(assignment, "kvm.master.ipStart"))
    master_mac_prefix = str(getNested(assignment, "kvm.master.macPrefix")).lower()
    master_mac_start = int(getNested(assignment, "kvm.master.macStart"))

    nodes = [
        {
            "name": master_name,
            "role": "master",
            "ip": f"{ip_prefix}.{master_ip_start}",
            "mac": f"{master_mac_prefix}:{master_mac_start:02x}",
            "vcpus": plan["master"]["vcpus"],
            "memory_mb": plan["master"]["memoryMb"],
            "disk_gb": plan["master"]["diskGb"],
            "memoryMb": plan["master"]["memoryMb"],
            "diskGb": plan["master"]["diskGb"],
        }
    ]

    worker_prefix = str(getNested(assignment, "kvm.workers.namePrefix"))
    worker_ip_start = int(getNested(assignment, "kvm.workers.ipStart"))
    worker_mac_prefix = str(getNested(assignment, "kvm.workers.macPrefix")).lower()
    worker_mac_start = int(getNested(assignment, "kvm.workers.macStart"))
    for item in plan["workers"]:
        index = int(item["index"])
        nodes.append(
            {
                "name": f"{worker_prefix}{index}-w{worker_count}",
                "role": "worker",
                "ip": f"{ip_prefix}.{worker_ip_start + index - 1}",
                "mac": f"{worker_mac_prefix}:{worker_mac_start + index - 1:02x}",
                "vcpus": item["vcpus"],
                "memory_mb": item["memoryMb"],
                "disk_gb": item["diskGb"],
                "memoryMb": item["memoryMb"],
                "diskGb": item["diskGb"],
            }
        )
    return nodes


def createGeneratedPaths(assignment: dict[str, Any], run_dir: Path) -> dict[str, Path]:
    """Return generated file paths for this assignment."""
    return {
        "config_kvm_ovn": SCRIPT_DIR / "configKvmOvn.yaml",
        "config_k3s": SCRIPT_DIR / "configK3s.yaml",
        "kubeconfig": SCRIPT_DIR / "kubeconfig.yaml",
        "inventory": SCRIPT_DIR / "cluster.inventory.yaml",
        "resource_plan_yaml": SCRIPT_DIR / "resourcePlan.yaml",
        "resource_plan_json": SCRIPT_DIR / "resourcePlan.json",
        "run_dir": run_dir,
    }


def createKvmConfig(assignment: dict[str, Any], run_dir: Path, paths: dict[str, Path], plan: dict[str, Any]) -> dict[str, Any]:
    """Create the kvmOvn YAML consumed by k8sTools.py build."""
    worker_count = int(getNested(assignment, "experiment.workerCount"))
    cluster_name = f"{getNested(assignment, 'kvm.clusterNamePrefix')}-w{worker_count}"
    registry_port = int(getNested(assignment, "registry.port", 5000))
    local_link_cni_type = str(getNested(assignment, "networking.localLinkCniType", "kube-ovn"))
    cni_master_interface = str(getNested(assignment, "networking.cniMasterInterface", "ens2"))
    legacy_base_image = str(getNested(assignment, "kvm.legacyBaseImagePath", "") or "")
    base_cache_dir = SCRIPT_DIR / "base_image"
    configured_base_image = str(
        getNested(
            assignment,
            "kvm.baseImagePath",
            legacy_base_image or base_cache_dir / "jammy-server-cloudimg-amd64.img",
        )
        or ""
    )
    persistent_base_image = resolvePath(configured_base_image or base_cache_dir / "jammy-server-cloudimg-amd64.img")
    persistent_image_cache = resolvePath(
        getNested(assignment, "seedemu.hostImageCacheDir", base_cache_dir / "image-cache")
    )
    persistent_helm_cache = resolvePath(
        getNested(assignment, "ovn.helmCacheDir", base_cache_dir / "helm")
    )
    seedemu = dict(assignment.get("seedemu") if isinstance(assignment.get("seedemu"), dict) else {})
    seedemu["hostImageCacheDir"] = str(persistent_image_cache)
    raw_cache_dirs = seedemu.get("imageCacheDirs", [])
    if isinstance(raw_cache_dirs, str):
        cache_dirs = [item for item in raw_cache_dirs.split() if item.strip()]
    elif isinstance(raw_cache_dirs, list):
        cache_dirs = [str(item) for item in raw_cache_dirs if str(item).strip()]
    else:
        cache_dirs = []
    resolved_cache_dirs = [str(persistent_image_cache)]
    for item in cache_dirs:
        resolved = str(resolvePath(item))
        if resolved not in resolved_cache_dirs:
            resolved_cache_dirs.append(resolved)
    seedemu["imageCacheDirs"] = resolved_cache_dirs
    seedemu["offline"] = boolValue(seedemu.get("offline", False))
    ovn = dict(assignment.get("ovn") if isinstance(assignment.get("ovn"), dict) else {})
    ovn["helmCacheDir"] = str(persistent_helm_cache)
    config = {
        "kind": "kvmOvn",
        "clusterName": cluster_name,
        "nodes": createKvmNodes(assignment, plan, worker_count),
        "kvm": {
            "network": f"{getNested(assignment, 'kvm.networkPrefix')}-w{worker_count}",
            "networkBridge": f"{getNested(assignment, 'kvm.bridgePrefix')}w{worker_count}",
            "networkCidr": str(getNested(assignment, "kvm.networkCidr")),
            "networkGateway": str(getNested(assignment, "kvm.networkGateway")),
            "dhcpStart": str(getNested(assignment, "kvm.dhcpStart")),
            "dhcpEnd": str(getNested(assignment, "kvm.dhcpEnd")),
            "baseImagePath": str(persistent_base_image),
            "legacyBaseImagePath": str(resolvePath(legacy_base_image)) if legacy_base_image else str(persistent_base_image),
            "diskDir": str(resolveDataRoot(worker_count) / "disks"),
            "cloudInitDir": str(run_dir / "cloud-init"),
        },
        "ssh": {
            "user": str(getNested(assignment, "kvm.ssh.user")),
            "key": str(getNested(assignment, "kvm.ssh.key")),
        },
        "fabric": {"type": "ovn"},
        "cni": {
            "localLinkCniType": local_link_cni_type,
            "defaultMasterInterface": cni_master_interface,
        },
        "registry": {"port": registry_port},
        "k3s": {
            "version": str(getNested(assignment, "k3s.version")),
            "clusterCidr": str(getNested(assignment, "k3s.clusterCidr")),
            "serviceCidr": str(getNested(assignment, "k3s.serviceCidr")),
            "flannelBackend": str(getNested(assignment, "k3s.flannelBackend")),
            "nodeCidrMaskSizeIpv4": int(getNested(assignment, "k3s.nodeCidrMaskSizeIpv4")),
            "maxPods": int(getNested(assignment, "k3s.maxPods")),
            "kubeletRegistryQps": int(getNested(assignment, "k3s.kubeletRegistryQps")),
            "kubeletRegistryBurst": int(getNested(assignment, "k3s.kubeletRegistryBurst")),
        },
        "outputs": {
            "kubeconfig": str(paths["kubeconfig"]),
            "tmpDir": str(run_dir / "setup-tmp"),
        },
    }
    config["seedemu"] = seedemu
    config["ovn"] = ovn
    return config


def writeYaml(path: Path, data: dict[str, Any]) -> None:
    """Write YAML to path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def writeJson(path: Path, data: dict[str, Any]) -> None:
    """Write JSON to path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def prepareConfig(args: argparse.Namespace) -> None:
    """Render generated files from assignment.yaml."""
    assignment_path = resolvePath(args.assignment)
    assignment = loadYaml(assignment_path)
    topology_size = int(getNested(assignment, "experiment.topologySize"))
    worker_count = int(getNested(assignment, "experiment.workerCount"))
    run_dir = resolvePath(args.run_dir) if args.run_dir else SCRIPT_DIR / "runs" / f"{args.timestamp or 'manual'}_{topology_size}_w{worker_count}"
    paths = createGeneratedPaths(assignment, run_dir)
    plan = createResourcePlan(assignment)
    kvm_config = createKvmConfig(assignment, run_dir, paths, plan)

    run_dir.mkdir(parents=True, exist_ok=True)
    writeYaml(paths["config_kvm_ovn"], kvm_config)
    writeYaml(paths["resource_plan_yaml"], plan)
    writeJson(paths["resource_plan_json"], plan)
    print(f"rendered {paths['config_kvm_ovn']}")
    print(f"resource_plan={paths['resource_plan_yaml']}")
    print(f"run_dir={run_dir}")


def printPlan(args: argparse.Namespace) -> None:
    """Print the computed resource plan."""
    assignment = loadYaml(resolvePath(args.assignment))
    print(yaml.safe_dump(createResourcePlan(assignment), sort_keys=False), end="")


def parseArgs() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assignment", default=str(DEFAULT_ASSIGNMENT))
    parser.add_argument("--run-dir")
    parser.add_argument("--timestamp")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("prepare")
    sub.add_parser("plan")
    return parser.parse_args()


def main() -> int:
    """CLI entrypoint."""
    args = parseArgs()
    if args.command == "prepare":
        prepareConfig(args)
    elif args.command == "plan":
        printPlan(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
