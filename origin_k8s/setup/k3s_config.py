#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import shlex
from pathlib import Path
from typing import Any

import yaml


SETUP_DIR = Path(__file__).resolve().parent


def expand_path(value: str) -> str:
    return str(Path(os.path.expanduser(os.path.expandvars(value))).resolve())


def get_nested(data: dict[str, Any], path: str, default: Any = None) -> Any:
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def load_yaml(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"Invalid YAML root in {path}: expected mapping")
    return data


def role_from_name(name: str) -> str:
    lowered = name.lower()
    if "master" in lowered or "control-plane" in lowered:
        return "master"
    return "worker"


def normalize_role(name: str, role: str | None) -> str:
    if role in {"master", "control-plane", "server"}:
        return "master"
    if role in {"worker", "agent"}:
        return "worker"
    return role_from_name(name)


def read_nodes_tsv(path: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as handle:
        for raw in handle:
            raw = raw.strip()
            if not raw or raw.startswith("#"):
                continue
            parts = raw.split("\t")
            while len(parts) < 7:
                parts.append("")
            name, role, ip, mac, vcpus, memory_mb, disk_gb = parts[:7]
            if not name or not ip:
                raise SystemExit(f"Invalid node TSV row, name and ip are required: {raw}")
            out.append(
                {
                    "name": name,
                    "role": normalize_role(name, role),
                    "ip": ip,
                    "mac": mac,
                    "vcpus": int(vcpus or 0),
                    "memory_mb": int(memory_mb or 0),
                    "disk_gb": int(disk_gb or 0),
                }
            )
    return out


def yaml_nodes(data: dict[str, Any]) -> list[dict[str, Any]]:
    raw_nodes = data.get("nodes") or []
    if not isinstance(raw_nodes, list):
        raise SystemExit("k3s YAML field nodes must be a list")
    out: list[dict[str, Any]] = []
    for item in raw_nodes:
        if not isinstance(item, dict):
            raise SystemExit(f"Invalid node item: {item}")
        name = str(item.get("name") or "")
        ip = str(item.get("ip") or item.get("management_ip") or "")
        if not name or not ip:
            raise SystemExit(f"Each k3s node requires name and ip: {item}")
        out.append(
            {
                "name": name,
                "role": normalize_role(name, str(item.get("role") or "")),
                "ip": ip,
                "mac": str(item.get("mac") or ""),
                "vcpus": int(item.get("vcpus") or 0),
                "memory_mb": int(item.get("memory_mb") or 0),
                "disk_gb": int(item.get("disk_gb") or 0),
            }
        )
    return out


def validate_nodes(nodes: list[dict[str, Any]]) -> None:
    if not nodes:
        raise SystemExit("No nodes selected for K3s cluster")
    masters = [node for node in nodes if node["role"] == "master"]
    if len(masters) != 1:
        names = ", ".join(node["name"] for node in masters) or "none"
        raise SystemExit(f"Expected exactly one master node, got {len(masters)}: {names}")
    seen_names: set[str] = set()
    seen_ips: set[str] = set()
    for node in nodes:
        if node["name"] in seen_names:
            raise SystemExit(f"Duplicate node name: {node['name']}")
        if node["ip"] in seen_ips:
            raise SystemExit(f"Duplicate node ip: {node['ip']}")
        seen_names.add(node["name"])
        seen_ips.add(node["ip"])


def install_version(data: dict[str, Any]) -> str:
    version = str(get_nested(data, "k3s.version", "v1.28.5+k3s1"))
    artifact = str(get_nested(data, "k3s.artifact_url", "https://rancher-mirror.rancher.cn/k3s"))
    configured = get_nested(data, "k3s.install_version")
    if configured:
        return str(configured)
    if "rancher-mirror.rancher.cn/k3s" in artifact:
        return version.replace("+", "-")
    return version


def context(args: argparse.Namespace) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    data = load_yaml(args.config)
    if args.nodes_tsv:
        nodes = read_nodes_tsv(args.nodes_tsv)
    else:
        nodes = yaml_nodes(data)
    validate_nodes(nodes)
    return data, nodes


def master(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    return [node for node in nodes if node["role"] == "master"][0]


def values(data: dict[str, Any], nodes: list[dict[str, Any]]) -> dict[str, Any]:
    cluster_name = str(data.get("cluster_name", "seedemu-k3s"))
    master_node = master(nodes)
    registry_host = get_nested(data, "registry.host", master_node["ip"])
    return {
        "SEED_CLUSTER_NAME": cluster_name,
        "SEED_SETUP_TMP_DIR": expand_path(str(get_nested(data, "outputs.tmp_dir", SETUP_DIR / "tmp"))),
        "SEED_K3S_USER": get_nested(data, "ssh.user", "ubuntu"),
        "SEED_K3S_SSH_KEY": expand_path(str(get_nested(data, "ssh.key", "~/.ssh/id_ed25519"))),
        "SEED_K3S_MASTER_NAME": master_node["name"],
        "SEED_K3S_MASTER_IP": master_node["ip"],
        "SEED_K3S_VERSION": get_nested(data, "k3s.version", "v1.28.5+k3s1"),
        "SEED_K3S_INSTALL_VERSION": install_version(data),
        "SEED_K3S_ARTIFACT_URL": get_nested(data, "k3s.artifact_url", "https://rancher-mirror.rancher.cn/k3s"),
        "SEED_K3S_FORCE_REINSTALL": str(get_nested(data, "k3s.force_reinstall", True)).lower(),
        "SEED_K3S_CLUSTER_CIDR": get_nested(data, "k3s.cluster_cidr", "10.42.0.0/16"),
        "SEED_K3S_SERVICE_CIDR": get_nested(data, "k3s.service_cidr", "10.43.0.0/16"),
        "SEED_K3S_NODE_CIDR_MASK_SIZE_IPV4": get_nested(data, "k3s.node_cidr_mask_size_ipv4", 20),
        "SEED_K3S_MAX_PODS": get_nested(data, "k3s.max_pods", 4000),
        "SEED_KUBELET_REGISTRY_QPS": get_nested(data, "k3s.kubelet_registry_qps", 100),
        "SEED_KUBELET_REGISTRY_BURST": get_nested(data, "k3s.kubelet_registry_burst", 20),
        "SEED_REGISTRY_HOST": registry_host,
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


def cmd_env(args: argparse.Namespace) -> None:
    data, nodes = context(args)
    for key, value in values(data, nodes).items():
        print(f"{key}={shlex.quote(str(value))}")


def cmd_nodes_tsv(args: argparse.Namespace) -> None:
    _data, nodes = context(args)
    for node in nodes:
        print(
            "\t".join(
                str(node[key])
                for key in ("name", "role", "ip", "mac", "vcpus", "memory_mb", "disk_gb")
            )
        )


def cmd_write_inventory(args: argparse.Namespace) -> None:
    data, nodes = context(args)
    vals = values(data, nodes)
    master_node = master(nodes)
    workers = [node for node in nodes if node["role"] == "worker"]
    payload = {
        "all": {
            "vars": {
                "ansible_user": vals["SEED_K3S_USER"],
                "ansible_ssh_private_key_file": vals["SEED_K3S_SSH_KEY"],
                "k3s_version": vals["SEED_K3S_VERSION"],
                "k3s_install_version": vals["SEED_K3S_INSTALL_VERSION"],
                "seed_registry_host": vals["SEED_REGISTRY_HOST"],
                "seed_registry_port": int(vals["SEED_REGISTRY_PORT"]),
                "seed_docker_io_mirror_endpoint": vals["SEED_DOCKER_IO_MIRROR_ENDPOINT"],
                "seed_k3s_artifact_url": vals["SEED_K3S_ARTIFACT_URL"],
                "seed_cni_master_interface": vals["SEED_CNI_MASTER_INTERFACE"],
                "seed_k3s_cluster_cidr": vals["SEED_K3S_CLUSTER_CIDR"],
                "seed_k3s_service_cidr": vals["SEED_K3S_SERVICE_CIDR"],
                "seed_k3s_node_cidr_mask_size_ipv4": int(vals["SEED_K3S_NODE_CIDR_MASK_SIZE_IPV4"]),
                "seed_k3s_max_pods": int(vals["SEED_K3S_MAX_PODS"]),
                "seed_k3s_force_reinstall": str(vals["SEED_K3S_FORCE_REINSTALL"]).lower() == "true",
            },
            "children": {
                "master": {
                    "hosts": {
                        master_node["name"]: {
                            "ansible_host": master_node["ip"],
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


def cmd_write_cluster_inventory(args: argparse.Namespace) -> None:
    data, nodes = context(args)
    vals = values(data, nodes)
    payload = {
        "cluster_name": vals["SEED_CLUSTER_NAME"],
        "runtime": "k3s",
        "k3s": {
            "cluster_cidr": vals["SEED_K3S_CLUSTER_CIDR"],
            "service_cidr": vals["SEED_K3S_SERVICE_CIDR"],
            "node_cidr_mask_size_ipv4": int(vals["SEED_K3S_NODE_CIDR_MASK_SIZE_IPV4"]),
            "max_pods": int(vals["SEED_K3S_MAX_PODS"]),
        },
        "registry": {
            "host": vals["SEED_REGISTRY_HOST"],
            "port": int(vals["SEED_REGISTRY_PORT"]),
        },
        "ssh": {
            "user": vals["SEED_K3S_USER"],
            "default_key_path": vals["SEED_K3S_SSH_KEY"],
        },
        "nodes": [
            {
                "name": node["name"],
                "role": node["role"],
                "management_ip": node["ip"],
                "resources": {
                    "vcpus": node["vcpus"],
                    "memory_mb": node["memory_mb"],
                    "disk_gb": node["disk_gb"],
                },
                "labels": {"kubernetes.io/hostname": node["name"]},
            }
            for node in nodes
        ],
    }
    output = Path(vals["SEED_OUTPUT_INVENTORY"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    print(output)


def cmd_write_env_file(args: argparse.Namespace) -> None:
    data, nodes = context(args)
    vals = values(data, nodes)
    output = Path(vals["SEED_OUTPUT_ENV_FILE"])
    lines = [
        "#!/usr/bin/env bash",
        f"export SEED_K3S_CLUSTER_NAME={shlex.quote(str(vals['SEED_CLUSTER_NAME']))}",
        f"export SEED_K3S_MASTER_IP={shlex.quote(str(vals['SEED_K3S_MASTER_IP']))}",
        f"export SEED_K3S_USER={shlex.quote(str(vals['SEED_K3S_USER']))}",
        f"export SEED_K3S_SSH_KEY={shlex.quote(str(vals['SEED_K3S_SSH_KEY']))}",
        f"export SEED_REGISTRY_HOST={shlex.quote(str(vals['SEED_REGISTRY_HOST']))}",
        f"export SEED_REGISTRY_PORT={shlex.quote(str(vals['SEED_REGISTRY_PORT']))}",
        f"export SEED_OUTPUT_KUBECONFIG={shlex.quote(str(vals['SEED_OUTPUT_KUBECONFIG']))}",
    ]
    for node in nodes:
        env_name = re.sub(r"[^A-Za-z0-9_]", "_", node["name"]).upper()
        lines.append(f"export {env_name}_IP={shlex.quote(str(node['ip']))}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    output.chmod(0o755)
    print(output)


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config")
    parser.add_argument("--nodes-tsv")


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    for name, func in (
        ("env", cmd_env),
        ("nodes-tsv", cmd_nodes_tsv),
        ("write-ansible-inventory", cmd_write_inventory),
        ("write-cluster-inventory", cmd_write_cluster_inventory),
        ("write-env-file", cmd_write_env_file),
    ):
        item = sub.add_parser(name)
        add_common(item)
        if name == "write-ansible-inventory":
            item.add_argument("--output", required=True)
        item.set_defaults(func=func)
    args = parser.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
