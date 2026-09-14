#!/usr/bin/env python3
"""Generate b63 KVM configs for the VM-count placement matrix.

Inputs: fixed VM counts and total resource budgets from the b63 experiment
design. Outputs: vm_matrix_configs/configkvm_b63_<N>vm.yaml files.
Side effects: writes only local YAML config files; it does not create VMs,
modify Kubernetes, or deploy workloads.
Context: run from largeScale/b63_deployment before matrix experiments.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml


DEFAULT_VM_COUNTS = (12, 18, 24, 36, 48)
TOTAL_VCPUS = 300
TOTAL_MEMORY_MB = 600 * 1024
MASTER_MIN_MEMORY_MB = 48 * 1024
DISK_GB = 80
K3S_NODE_CIDR_MASK_SIZE_IPV4 = 22
BASE_IP_PREFIX = "192.168.122"
BASE_IP_LAST_OCTET = 150
BASE_MAC_PREFIX = "52:54:00:63"
EXTRA_TRUNK_NETWORKS = (
    ("seedemu-b63-trunk1", "virbr63t1", "ens3"),
    ("seedemu-b63-trunk2", "virbr63t2", "ens4"),
    ("seedemu-b63-trunk3", "virbr63t3", "ens5"),
)


def parseArgs() -> argparse.Namespace:
    """Parse CLI arguments for config generation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("vm_matrix_configs"))
    parser.add_argument("--vm-counts", default=",".join(str(v) for v in DEFAULT_VM_COUNTS))
    return parser.parse_args()


def splitBudget(total: int, count: int) -> list[int]:
    """Split an integer budget across count VMs with at most one-unit skew."""
    base = total // count
    remainder = total % count
    return [base + 1 if index < remainder else base for index in range(count)]


def splitMemoryBudget(total: int, count: int) -> list[int]:
    """Split memory while reserving at least 48GiB for the K3s master."""
    even_split = splitBudget(total, count)
    if count == 1 or even_split[0] >= MASTER_MIN_MEMORY_MB:
        return even_split
    worker_total = total - MASTER_MIN_MEMORY_MB
    if worker_total <= 0:
        raise ValueError("total memory budget must exceed the master minimum")
    return [MASTER_MIN_MEMORY_MB, *splitBudget(worker_total, count - 1)]


def buildNodes(vm_count: int) -> list[dict[str, Any]]:
    """Build master-plus-worker node entries for one VM count."""
    vcpus = splitBudget(TOTAL_VCPUS, vm_count)
    memory = splitMemoryBudget(TOTAL_MEMORY_MB, vm_count)
    nodes = []
    for index in range(vm_count):
        role = "master" if index == 0 else "worker"
        name = "seedemu-b63-placement-master" if index == 0 else f"seedemu-b63-placement-worker{index}"
        ip = f"{BASE_IP_PREFIX}.{BASE_IP_LAST_OCTET + index}"
        mac_suffix = f"{index + 1:02x}"
        nodes.append(
            {
                "name": name,
                "role": role,
                "ip": ip,
                "mac": f"{BASE_MAC_PREFIX}:{vm_count:02x}:{mac_suffix}",
                "vcpus": vcpus[index],
                "memory_mb": memory[index],
                "disk_gb": DISK_GB,
            }
        )
    return nodes


def buildConfig(vm_count: int) -> dict[str, Any]:
    """Return a complete b63 KVM config for one VM count."""
    return {
        "kind": "kvmOvn",
        "clusterName": "seedemu-b63-placement-kvm",
        "nodes": buildNodes(vm_count),
        "kvm": {
            "legacyBaseImagePath": "",
            "allowExisting": False,
            "extraNetworks": [
                {
                    "name": name,
                    "bridge": bridge,
                    "model": "virtio",
                    "trunk": f"trunk{index}",
                    "masterInterface": master_interface,
                }
                for index, (name, bridge, master_interface) in enumerate(EXTRA_TRUNK_NETWORKS, start=1)
            ],
        },
        "ssh": {
            "user": "ubuntu",
            "key": "~/.ssh/id_ed25519",
        },
        "fabric": {
            "type": "macvlan-vlan",
        },
        "cni": {
            "defaultMasterInterface": "ens2",
        },
        "registry": {
            "port": 5063,
        },
        "k3s": {
            "maxPods": 4000,
            "nodeCidrMaskSizeIpv4": K3S_NODE_CIDR_MASK_SIZE_IPV4,
        },
        "tuning": {
            "userMaxNetNamespaces": 65536,
        },
    }


def main() -> int:
    """Generate all requested matrix configs."""
    args = parseArgs()
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    vm_counts = [int(value) for value in args.vm_counts.split(",") if value.strip()]
    for vm_count in vm_counts:
        data = buildConfig(vm_count)
        path = output_dir / f"configkvm_b63_{vm_count}vm.yaml"
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        vcpu_total = sum(node["vcpus"] for node in data["nodes"])
        memory_total = sum(node["memory_mb"] for node in data["nodes"])
        print(f"{path}: nodes={vm_count} vcpus={vcpu_total} memory_mb={memory_total} disk_gb={DISK_GB}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
