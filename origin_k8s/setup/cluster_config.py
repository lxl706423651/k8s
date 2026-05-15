#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import shlex
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path("/home/lxl/k8s")
SETUP_DIR = Path(__file__).resolve().parent
SETUP_DATA_DIR = Path("/data/lxl/k8s/origin_k8s/setup")


def expand_path(value: str) -> str:
    return str(Path(os.path.expandvars(os.path.expanduser(value))).resolve())


def load_config(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"Invalid config root in {path}: expected mapping")
    return data


def get_nested(data: dict[str, Any], path: str, default: Any = None) -> Any:
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def ubuntu_image_defaults(data: dict[str, Any]) -> tuple[str, str]:
    series = str(get_nested(data, "kvm.ubuntu_series", "jammy"))
    if series == "jammy":
        return (
            "https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img",
            str(SETUP_DIR / "base/jammy-server-cloudimg-amd64.img"),
        )
    if series == "noble":
        return (
            "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img",
            str(SETUP_DIR / "base/noble-server-cloudimg-amd64.img"),
        )
    raise SystemExit(f"Unsupported kvm.ubuntu_series: {series}")


def normalize_node(node: dict[str, Any], defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    defaults = defaults or {}
    out = dict(defaults)
    out.update(node)
    required = ["name", "role", "ip", "mac", "vcpus", "memory_mb", "disk_gb"]
    missing = [key for key in required if key not in out or out[key] in ("", None)]
    if missing:
        raise SystemExit(f"Node is missing required fields {missing}: {out}")
    out["role"] = "master" if str(out["role"]) in {"master", "control-plane"} else "worker"
    out["vcpus"] = int(out["vcpus"])
    out["memory_mb"] = int(out["memory_mb"])
    out["disk_gb"] = int(out["disk_gb"])
    return out


def read_existing_nodes(path: str | None) -> list[dict[str, str]]:
    if not path:
        return []
    existing_path = Path(path)
    if not existing_path.exists():
        return []
    out: list[dict[str, str]] = []
    with existing_path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            raw = raw.strip()
            if not raw or raw.startswith("#"):
                continue
            parts = raw.split("\t")
            while len(parts) < 3:
                parts.append("")
            name, ip, mac = [part.strip() for part in parts[:3]]
            if name or ip or mac:
                out.append({"name": name, "ip": ip, "mac": mac.lower()})
    return out


def split_ipv4(ip: str) -> tuple[str, int] | None:
    match = re.fullmatch(r"(\d+\.\d+\.\d+)\.(\d+)", ip)
    if not match:
        return None
    octet = int(match.group(2))
    if octet < 1 or octet > 254:
        return None
    return match.group(1), octet


def mac_suffix(mac: str, prefix: str) -> int | None:
    mac = mac.lower()
    prefix = prefix.lower()
    if not mac.startswith(prefix + ":"):
        return None
    suffix = mac.rsplit(":", 1)[-1]
    try:
        value = int(suffix, 16)
    except ValueError:
        return None
    if value < 0 or value > 255:
        return None
    return value


def next_vm_name(base: str, used_names: set[str], first_number: int | None = None) -> str:
    if first_number is None and base not in used_names:
        used_names.add(base)
        return base
    number = first_number or 2
    while True:
        candidate = f"{base}{number}"
        if candidate not in used_names:
            used_names.add(candidate)
            return candidate
        number += 1


def next_worker_number(name_prefix: str, used_names: set[str]) -> int:
    pattern = re.compile(rf"^{re.escape(name_prefix)}(\d+)$")
    highest = 0
    for name in used_names:
        match = pattern.fullmatch(name)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def next_ip(ip_prefix: str, start: int, used_ips: set[str]) -> str:
    highest = start - 1
    for ip in used_ips:
        parsed = split_ipv4(ip)
        if parsed and parsed[0] == ip_prefix and parsed[1] >= start:
            highest = max(highest, parsed[1])
    candidate = highest + 1
    while candidate <= 254:
        ip = f"{ip_prefix}.{candidate}"
        if ip not in used_ips:
            used_ips.add(ip)
            return ip
        candidate += 1
    raise SystemExit(f"No available IPv4 address left in {ip_prefix}.0/24 starting at {start}")


def next_mac(mac_prefix: str, start: int, used_macs: set[str]) -> str:
    highest = start - 1
    for mac in used_macs:
        suffix = mac_suffix(mac, mac_prefix)
        if suffix is not None and suffix >= start:
            highest = max(highest, suffix)
    candidate = highest + 1
    while candidate <= 255:
        mac = f"{mac_prefix.lower()}:{candidate:02x}"
        if mac not in used_macs:
            used_macs.add(mac)
            return mac
        candidate += 1
    raise SystemExit(f"No available MAC suffix left for prefix {mac_prefix} starting at {start:02x}")


def auto_node(
    role: str,
    cfg: dict[str, Any],
    used_names: set[str],
    used_ips: set[str],
    used_macs: set[str],
    *,
    default_name: str | None = None,
    default_name_prefix: str | None = None,
    worker_number: int | None = None,
    default_ip_prefix: str,
    default_ip_start: int,
    default_mac_prefix: str,
    default_mac_start: int,
) -> dict[str, Any]:
    required = ["vcpus", "memory_mb", "disk_gb"]
    missing = [key for key in required if cfg.get(key) in ("", None)]
    if missing:
        raise SystemExit(f"{role} config is missing required fields {missing}: {cfg}")

    if cfg.get("name"):
        name = str(cfg["name"])
        if name in used_names:
            raise SystemExit(f"Configured {role} name conflicts with existing/planned VM: {name}")
        used_names.add(name)
    elif role == "master":
        name = next_vm_name(str(default_name or "seed-k3s-master"), used_names)
    else:
        prefix = str(default_name_prefix or "seed-k3s-worker")
        number = worker_number or next_worker_number(prefix, used_names)
        name = next_vm_name(prefix, used_names, first_number=number)

    ip = str(cfg["ip"]) if cfg.get("ip") else next_ip(
        str(cfg.get("ip_prefix", default_ip_prefix)),
        int(cfg.get("ip_start", default_ip_start)),
        used_ips,
    )
    if cfg.get("ip") and ip in used_ips:
        raise SystemExit(f"Configured {role} IP conflicts with existing/planned VM: {ip}")
    used_ips.add(ip)

    mac = str(cfg["mac"]).lower() if cfg.get("mac") else next_mac(
        str(cfg.get("mac_prefix", default_mac_prefix)),
        int(cfg.get("mac_start", default_mac_start)),
        used_macs,
    )
    if cfg.get("mac") and mac in used_macs:
        raise SystemExit(f"Configured {role} MAC conflicts with existing/planned VM: {mac}")
    used_macs.add(mac)

    return normalize_node(
        {
            "name": name,
            "role": role,
            "ip": ip,
            "mac": mac,
            "vcpus": cfg["vcpus"],
            "memory_mb": cfg["memory_mb"],
            "disk_gb": cfg["disk_gb"],
        }
    )


def nodes(data: dict[str, Any], existing: list[dict[str, str]] | None = None) -> list[dict[str, Any]]:
    existing = existing or []
    used_names = {item["name"] for item in existing if item.get("name")}
    used_ips = {item["ip"] for item in existing if item.get("ip")}
    used_macs = {item["mac"].lower() for item in existing if item.get("mac")}
    explicit = data.get("nodes")
    if explicit:
        out = [normalize_node(item) for item in explicit]
    else:
        out = []
        master_cfg = data.get("master")
        if "master" in data and master_cfg is not None:
            out.append(
                auto_node(
                    "master",
                    master_cfg or {},
                    used_names,
                    used_ips,
                    used_macs,
                    default_name=str(get_nested(data, "defaults.master_name", "seed-k3s-master")),
                    default_ip_prefix=str(get_nested(data, "defaults.ip_prefix", "192.168.122")),
                    default_ip_start=int(get_nested(data, "defaults.master_ip_start", 110)),
                    default_mac_prefix=str(get_nested(data, "defaults.mac_prefix", "52:54:00:64:10")),
                    default_mac_start=int(get_nested(data, "defaults.master_mac_start", 0x10)),
                )
            )
        workers_cfg = data.get("workers") or {}
        if "workers" in data and data.get("workers") is not None and "count" not in workers_cfg:
            raise SystemExit("workers.count is required when workers is configured")
        count = int(workers_cfg.get("count", 0))
        if count < 0:
            raise SystemExit("workers.count must be >= 0")
        name_prefix = str(workers_cfg.get("name_prefix", get_nested(data, "defaults.worker_name_prefix", "seed-k3s-worker")))
        next_number = next_worker_number(name_prefix, used_names)
        for idx in range(1, count + 1):
            out.append(
                auto_node(
                    "worker",
                    workers_cfg,
                    used_names,
                    used_ips,
                    used_macs,
                    default_name_prefix=name_prefix,
                    worker_number=next_number + idx - 1,
                    default_ip_prefix=str(workers_cfg.get("ip_prefix", get_nested(data, "defaults.ip_prefix", "192.168.122"))),
                    default_ip_start=int(workers_cfg.get("ip_start", get_nested(data, "defaults.worker_ip_start", 111))),
                    default_mac_prefix=str(workers_cfg.get("mac_prefix", get_nested(data, "defaults.mac_prefix", "52:54:00:64:10"))),
                    default_mac_start=int(workers_cfg.get("mac_start", get_nested(data, "defaults.worker_mac_start", 0x11))),
                )
            )
        if not out:
            raise SystemExit("No VMs requested: provide master and/or workers.count > 0")

    masters = [node for node in out if node["role"] == "master"]
    if len(masters) > 1:
        raise SystemExit(f"Expected at most one master node, got {len(masters)}")
    names = [node["name"] for node in out]
    ips = [node["ip"] for node in out]
    macs = [node["mac"] for node in out]
    for label, values in (("name", names), ("ip", ips), ("mac", macs)):
        dup = sorted({value for value in values if values.count(value) > 1})
        if dup:
            raise SystemExit(f"Duplicate node {label}: {dup}")
    return out


def master_node(data: dict[str, Any]) -> dict[str, Any]:
    return [node for node in nodes(data) if node["role"] == "master"][0]


def optional_master_node(data: dict[str, Any], existing: list[dict[str, str]] | None = None) -> dict[str, Any] | None:
    masters = [node for node in nodes(data, existing) if node["role"] == "master"]
    return masters[0] if masters else None


def install_version(data: dict[str, Any]) -> str:
    version = str(get_nested(data, "k3s.version", "v1.28.5+k3s1"))
    artifact = str(get_nested(data, "k3s.artifact_url", "https://rancher-mirror.rancher.cn/k3s"))
    configured = get_nested(data, "k3s.install_version")
    if configured:
        return str(configured)
    if "rancher-mirror.rancher.cn/k3s" in artifact:
        return version.replace("+", "-")
    return version


def shell_env(args: argparse.Namespace) -> None:
    data = load_config(args.config)
    base_url, base_path = ubuntu_image_defaults(data)
    master = master_node(data)
    cluster_name = str(data.get("cluster_name", "seedemu-k3s"))
    storage_dir = expand_path(str(get_nested(data, "kvm.storage_dir", SETUP_DIR)))
    disk_dir = expand_path(str(get_nested(data, "kvm.disk_dir", SETUP_DATA_DIR / "disks")))
    cloud_init_dir = expand_path(str(get_nested(data, "kvm.cloud_init_dir", SETUP_DIR / "cloud-init")))
    values = {
        "SEED_CLUSTER_NAME": cluster_name,
        "SEED_KVM_NETWORK": get_nested(data, "kvm.network", "default"),
        "SEED_KVM_STORAGE_DIR": storage_dir,
        "SEED_KVM_DISK_DIR": disk_dir,
        "SEED_KVM_CLOUD_INIT_DIR": cloud_init_dir,
        "SEED_SETUP_TMP_DIR": expand_path(str(get_nested(data, "outputs.tmp_dir", SETUP_DIR / "tmp"))),
        "SEED_KVM_UBUNTU_SERIES": get_nested(data, "kvm.ubuntu_series", "jammy"),
        "SEED_KVM_BASE_IMAGE_URL": get_nested(data, "kvm.base_image_url", base_url),
        "SEED_KVM_BASE_IMAGE_PATH": expand_path(str(get_nested(data, "kvm.base_image_path", base_path))),
        "SEED_KVM_LEGACY_BASE_IMAGE_PATH": expand_path(str(get_nested(data, "kvm.legacy_base_image_path", REPO_ROOT / f"output/kvm_lab/base/{Path(base_path).name}"))),
        "SEED_KVM_BOOT_TIMEOUT_SECONDS": get_nested(data, "kvm.boot_timeout_seconds", 300),
        "SEED_KVM_ALLOW_EXISTING": str(get_nested(data, "kvm.allow_existing", False)).lower(),
        "SEED_K3S_USER": get_nested(data, "ssh.user", "ubuntu"),
        "SEED_K3S_SSH_KEY": expand_path(str(get_nested(data, "ssh.key", "~/.ssh/id_ed25519"))),
        "SEED_K3S_MASTER_NAME": master["name"],
        "SEED_K3S_MASTER_IP": master["ip"],
        "SEED_K3S_VERSION": get_nested(data, "k3s.version", "v1.28.5+k3s1"),
        "SEED_K3S_INSTALL_VERSION": install_version(data),
        "SEED_K3S_ARTIFACT_URL": get_nested(data, "k3s.artifact_url", "https://rancher-mirror.rancher.cn/k3s"),
        "SEED_K3S_FORCE_REINSTALL": str(get_nested(data, "k3s.force_reinstall", False)).lower(),
        "SEED_K3S_CLUSTER_CIDR": get_nested(data, "k3s.cluster_cidr", "10.42.0.0/16"),
        "SEED_K3S_SERVICE_CIDR": get_nested(data, "k3s.service_cidr", "10.43.0.0/16"),
        "SEED_K3S_NODE_CIDR_MASK_SIZE_IPV4": get_nested(data, "k3s.node_cidr_mask_size_ipv4", 20),
        "SEED_K3S_MAX_PODS": get_nested(data, "k3s.max_pods", 4000),
        "SEED_KUBELET_REGISTRY_QPS": get_nested(data, "k3s.kubelet_registry_qps", 100),
        "SEED_KUBELET_REGISTRY_BURST": get_nested(data, "k3s.kubelet_registry_burst", 20),
        "SEED_REGISTRY_HOST": get_nested(data, "registry.host", master["ip"]),
        "SEED_REGISTRY_PORT": get_nested(data, "registry.port", 5000),
        "SEED_DOCKER_IO_MIRROR_ENDPOINT": get_nested(data, "registry.docker_io_mirror_endpoint", "https://docker.m.daocloud.io"),
        "SEED_CNI_MASTER_INTERFACE": get_nested(data, "cni.default_master_interface", "ens2"),
        "SEED_CNI0_HASH_MAX": get_nested(data, "tuning.cni0_hash_max", 16384),
        "SEED_USER_MAX_NET_NAMESPACES": get_nested(data, "tuning.user_max_net_namespaces", 65536),
        "SEED_NEIGH_GC_THRESH1": get_nested(data, "tuning.neigh_gc_thresh1", 1048576),
        "SEED_NEIGH_GC_THRESH2": get_nested(data, "tuning.neigh_gc_thresh2", 4194304),
        "SEED_NEIGH_GC_THRESH3": get_nested(data, "tuning.neigh_gc_thresh3", 8388608),
        "SEED_NETDEV_MAX_BACKLOG": get_nested(data, "tuning.netdev_max_backlog", 1000000),
        "SEED_OPTMEM_MAX": get_nested(data, "tuning.optmem_max", 25165824),
        "SEED_REBOOT_AFTER_TUNING": str(get_nested(data, "tuning.reboot_after_tuning", False)).lower(),
        "SEED_OUTPUT_KUBECONFIG": expand_path(str(get_nested(data, "outputs.kubeconfig", SETUP_DIR / f"{cluster_name}.kubeconfig.yaml"))),
        "SEED_OUTPUT_INVENTORY": expand_path(str(get_nested(data, "outputs.inventory", SETUP_DIR / f"{cluster_name}.inventory.yaml"))),
        "SEED_OUTPUT_ENV_FILE": expand_path(str(get_nested(data, "outputs.env_file", SETUP_DIR / f"{cluster_name}.env.sh"))),
    }
    for key, value in values.items():
        print(f"{key}={shlex.quote(str(value))}")


def kvm_env(args: argparse.Namespace) -> None:
    data = load_config(args.config)
    base_url, base_path = ubuntu_image_defaults(data)
    existing = read_existing_nodes(args.existing_tsv)
    master = optional_master_node(data, existing)
    cluster_name = str(data.get("cluster_name", "seedemu-k3s"))
    storage_dir = expand_path(str(get_nested(data, "kvm.storage_dir", SETUP_DIR)))
    disk_dir = expand_path(str(get_nested(data, "kvm.disk_dir", SETUP_DATA_DIR / "disks")))
    cloud_init_dir = expand_path(str(get_nested(data, "kvm.cloud_init_dir", SETUP_DIR / "cloud-init")))
    values = {
        "SEED_CLUSTER_NAME": cluster_name,
        "SEED_KVM_NETWORK": get_nested(data, "kvm.network", "default"),
        "SEED_KVM_STORAGE_DIR": storage_dir,
        "SEED_KVM_DISK_DIR": disk_dir,
        "SEED_KVM_CLOUD_INIT_DIR": cloud_init_dir,
        "SEED_KVM_UBUNTU_SERIES": get_nested(data, "kvm.ubuntu_series", "jammy"),
        "SEED_KVM_BASE_IMAGE_URL": get_nested(data, "kvm.base_image_url", base_url),
        "SEED_KVM_BASE_IMAGE_PATH": expand_path(str(get_nested(data, "kvm.base_image_path", base_path))),
        "SEED_KVM_LEGACY_BASE_IMAGE_PATH": expand_path(
            str(get_nested(data, "kvm.legacy_base_image_path", REPO_ROOT / f"output/kvm_lab/base/{Path(base_path).name}"))
        ),
        "SEED_KVM_BOOT_TIMEOUT_SECONDS": get_nested(data, "kvm.boot_timeout_seconds", 300),
        "SEED_KVM_ALLOW_EXISTING": str(get_nested(data, "kvm.allow_existing", False)).lower(),
        "SEED_SSH_USER": get_nested(data, "ssh.user", "ubuntu"),
        "SEED_SSH_KEY": expand_path(str(get_nested(data, "ssh.key", "~/.ssh/id_ed25519"))),
        "SEED_MASTER_NAME": master["name"] if master else "",
        "SEED_MASTER_IP": master["ip"] if master else "",
    }
    for key, value in values.items():
        print(f"{key}={shlex.quote(str(value))}")


def nodes_tsv(args: argparse.Namespace) -> None:
    for node in nodes(load_config(args.config), read_existing_nodes(args.existing_tsv)):
        print(
            "\t".join(
                str(node[key])
                for key in ("name", "role", "ip", "mac", "vcpus", "memory_mb", "disk_gb")
            )
        )


def write_inventory(args: argparse.Namespace) -> None:
    data = load_config(args.config)
    cluster_name = str(data.get("cluster_name", "seedemu-k3s"))
    master = master_node(data)
    output = Path(args.output or expand_path(str(get_nested(data, "outputs.inventory", SETUP_DIR / f"{cluster_name}.inventory.yaml"))))
    payload = {
        "cluster_name": cluster_name,
        "reference_cluster": False,
        "runtime": "k3s",
        "max_validated_topology_size": int(get_nested(data, "max_validated_topology_size", 12000)),
        "k3s": {
            "cluster_cidr": get_nested(data, "k3s.cluster_cidr", "10.42.0.0/16"),
            "service_cidr": get_nested(data, "k3s.service_cidr", "10.43.0.0/16"),
            "node_cidr_mask_size_ipv4": int(get_nested(data, "k3s.node_cidr_mask_size_ipv4", 20)),
            "max_pods": int(get_nested(data, "k3s.max_pods", 4000)),
        },
        "network_tuning": {
            "cni0_hash_max": int(get_nested(data, "tuning.cni0_hash_max", 16384)),
            "user_max_net_namespaces": int(get_nested(data, "tuning.user_max_net_namespaces", 65536)),
            "neigh_gc_thresh1": int(get_nested(data, "tuning.neigh_gc_thresh1", 1048576)),
            "neigh_gc_thresh2": int(get_nested(data, "tuning.neigh_gc_thresh2", 4194304)),
            "neigh_gc_thresh3": int(get_nested(data, "tuning.neigh_gc_thresh3", 8388608)),
            "netdev_max_backlog": int(get_nested(data, "tuning.netdev_max_backlog", 1000000)),
            "optmem_max": int(get_nested(data, "tuning.optmem_max", 25165824)),
        },
        "ssh": {
            "user": get_nested(data, "ssh.user", "ubuntu"),
            "key_path_env": "SEED_K3S_SSH_KEY",
            "default_key_path": get_nested(data, "ssh.key", "~/.ssh/id_ed25519"),
        },
        "registry": {
            "host": get_nested(data, "registry.host", master["ip"]),
            "port": int(get_nested(data, "registry.port", 5000)),
        },
        "cni": {"default_master_interface": get_nested(data, "cni.default_master_interface", "ens2")},
        "nodes": [
            {
                "name": node["name"],
                "role": node["role"],
                "management_ip": node["ip"],
                "runtime": "k3s",
                "resources": {
                    "vcpus": node["vcpus"],
                    "memory_mb": node["memory_mb"],
                    "disk_gb": node["disk_gb"],
                },
                "labels": {"kubernetes.io/hostname": node["name"]},
            }
            for node in nodes(data)
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    print(output)


def write_env_file(args: argparse.Namespace) -> None:
    data = load_config(args.config)
    cluster_name = str(data.get("cluster_name", "seedemu-k3s"))
    output = Path(args.output or expand_path(str(get_nested(data, "outputs.env_file", SETUP_DIR / f"{cluster_name}.env.sh"))))
    inventory_path = expand_path(str(get_nested(data, "outputs.inventory", SETUP_DIR / f"{cluster_name}.inventory.yaml")))
    master = master_node(data)
    lines = [
        "#!/usr/bin/env bash",
        f"export SEED_K3S_CLUSTER_NAME={shlex.quote(cluster_name)}",
        f"export SEED_CLUSTER_INVENTORY={shlex.quote(cluster_name + '-custom')}",
        f"export SEED_CLUSTER_INVENTORY_PATH={shlex.quote(inventory_path)}",
        f"export SEED_K3S_MASTER_IP={shlex.quote(str(master['ip']))}",
        f"export SEED_K3S_USER={shlex.quote(str(get_nested(data, 'ssh.user', 'ubuntu')))}",
        f"export SEED_K3S_SSH_KEY={shlex.quote(expand_path(str(get_nested(data, 'ssh.key', '~/.ssh/id_ed25519'))))}",
        f"export SEED_REGISTRY_HOST={shlex.quote(str(get_nested(data, 'registry.host', master['ip'])))}",
        f"export SEED_REGISTRY_PORT={shlex.quote(str(get_nested(data, 'registry.port', 5000)))}",
    ]
    for node in nodes(data):
        env_name = node["name"].upper().replace("-", "_")
        lines.append(f"export {env_name}_IP={shlex.quote(str(node['ip']))}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    output.chmod(0o755)
    print(output)


def write_ansible_inventory(args: argparse.Namespace) -> None:
    data = load_config(args.config)
    all_nodes = nodes(data)
    master = [node for node in all_nodes if node["role"] == "master"][0]
    workers = [node for node in all_nodes if node["role"] == "worker"]
    payload = {
        "all": {
            "vars": {
                "ansible_user": get_nested(data, "ssh.user", "ubuntu"),
                "ansible_ssh_private_key_file": expand_path(str(get_nested(data, "ssh.key", "~/.ssh/id_ed25519"))),
                "k3s_version": get_nested(data, "k3s.version", "v1.28.5+k3s1"),
                "k3s_install_version": install_version(data),
                "seed_registry_host": get_nested(data, "registry.host", master["ip"]),
                "seed_registry_port": get_nested(data, "registry.port", 5000),
                "seed_docker_io_mirror_endpoint": get_nested(data, "registry.docker_io_mirror_endpoint", "https://docker.m.daocloud.io"),
                "seed_k3s_artifact_url": get_nested(data, "k3s.artifact_url", "https://rancher-mirror.rancher.cn/k3s"),
                "seed_cni_master_interface": get_nested(data, "cni.default_master_interface", "ens2"),
                "seed_k3s_cluster_cidr": get_nested(data, "k3s.cluster_cidr", "10.42.0.0/16"),
                "seed_k3s_service_cidr": get_nested(data, "k3s.service_cidr", "10.43.0.0/16"),
                "seed_k3s_node_cidr_mask_size_ipv4": get_nested(data, "k3s.node_cidr_mask_size_ipv4", 20),
                "seed_k3s_max_pods": get_nested(data, "k3s.max_pods", 4000),
                "seed_k3s_force_reinstall": bool(get_nested(data, "k3s.force_reinstall", False)),
            },
            "children": {
                "master": {
                    "hosts": {
                        master["name"]: {
                            "ansible_host": master["ip"],
                            "k3s_role": "server",
                            "seedemu_as_group": "master",
                        }
                    }
                },
                "workers": {
                    "hosts": {
                        node["name"]: {
                            "ansible_host": node["ip"],
                            "k3s_role": "agent",
                            "seedemu_as_group": f"worker-{idx}",
                        }
                        for idx, node in enumerate(workers, start=1)
                    }
                },
            },
        }
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    print(output)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("config")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("shell-env").set_defaults(func=shell_env)
    kvm_env_parser = sub.add_parser("kvm-env")
    kvm_env_parser.add_argument("--existing-tsv")
    kvm_env_parser.set_defaults(func=kvm_env)
    nodes_parser = sub.add_parser("nodes-tsv")
    nodes_parser.add_argument("--existing-tsv")
    nodes_parser.set_defaults(func=nodes_tsv)
    inv = sub.add_parser("write-inventory")
    inv.add_argument("--output")
    inv.set_defaults(func=write_inventory)
    env = sub.add_parser("write-env")
    env.add_argument("--output")
    env.set_defaults(func=write_env_file)
    ansible = sub.add_parser("write-ansible-inventory")
    ansible.add_argument("--output", required=True)
    ansible.set_defaults(func=write_ansible_inventory)
    args = parser.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
