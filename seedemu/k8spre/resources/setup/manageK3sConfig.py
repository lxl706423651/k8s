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


def expandPath(value: str) -> str:
    """Expand ~ and return an absolute path string."""
    return str(Path(os.path.expanduser(value)).resolve())


def getNested(data: dict[str, Any], path: str, default: Any = None) -> Any:
    """Read a dotted YAML path, accepting both camelCase and snake_case keys."""
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict):
            return default
        candidates = [part, _snakeCase(part), _camelCase(part)]
        found = False
        for candidate in candidates:
            if candidate in cur:
                cur = cur[candidate]
                found = True
                break
        if not found:
            return default
    return cur


def loadYaml(path: str) -> dict[str, Any]:
    """Load configK3s.yaml and validate that it is a mapping."""
    with open(path, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"Invalid YAML root in {path}: expected mapping")
    return data


def normalizeRole(name: str, role: str | None, index: int) -> str:
    """Normalize user node roles; role must be explicitly provided."""
    if not role:
        raise SystemExit(f"Node {name or index} requires role: master or worker")
    if role in {"master", "control-plane", "server"}:
        return "master"
    if role in {"worker", "agent"}:
        return "worker"
    raise SystemExit(f"Unsupported role for node {name or index}: {role}")


def yamlNodes(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Return normalized nodes from configK3s.yaml.

    Each node requires an IP address and should carry its own ssh.user/key.
    Missing names become seed-k3s-master/seed-k3s-workerN. A top-level ssh
    block is still accepted as a compatibility fallback, but generated configs
    write SSH settings per node.
    """
    raw_nodes = data.get("nodes") or []
    if not isinstance(raw_nodes, list):
        raise SystemExit("configK3s.yaml field nodes must be a list")
    out: list[dict[str, Any]] = []
    for index, item in enumerate(raw_nodes):
        if not isinstance(item, dict):
            raise SystemExit(f"Invalid node item: {item}")
        ip = str(item.get("ip") or item.get("managementIp") or item.get("management_ip") or "")
        if not ip:
            raise SystemExit(f"Each k3s node requires an ip: {item}")
        role = normalizeRole(str(item.get("name") or ""), str(item.get("role") or ""), index)
        default_name = "seed-k3s-master" if role == "master" else f"seed-k3s-worker{sum(1 for node in out if node['role'] == 'worker') + 1}"
        name = str(item.get("name") or default_name)
        ssh_user, ssh_key = nodeSshSettings(data, item, name)
        out.append(
            {
                "name": name,
                "role": role,
                "ip": ip,
                "mac": str(item.get("mac") or ""),
                "vcpus": int(item.get("vcpus") or 0),
                "memoryMb": int(item.get("memoryMb") or item.get("memory_mb") or 0),
                "diskGb": int(item.get("diskGb") or item.get("disk_gb") or 0),
                "sshUser": ssh_user,
                "sshKey": expandPath(ssh_key),
            }
        )
    validateNodes(out)
    return out


def nodeSshSettings(data: dict[str, Any], item: dict[str, Any], node_name: str) -> tuple[str, str]:
    """Return SSH user/key for one node.

    Args:
        data: Full configK3s.yaml mapping.
        item: One raw node mapping.
        node_name: Normalized node name used for error messages.
    """
    ssh = item.get("ssh") if isinstance(item.get("ssh"), dict) else {}
    user = ssh.get("user") or getNested(data, "ssh.user")
    key = ssh.get("key") or getNested(data, "ssh.key")
    if not user or not key:
        raise SystemExit(
            f"Node {node_name} requires ssh.user and ssh.key. "
            "Use nodes[].ssh.{user,key}; top-level ssh is accepted only as a fallback."
        )
    return str(user), str(key)


def validateNodes(nodes: list[dict[str, Any]]) -> None:
    """Validate that the selected node set can form one K3s cluster."""
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


def installVersion(data: dict[str, Any]) -> str:
    """Return the K3s install version expected by the configured artifact URL."""
    version = str(getNested(data, "k3s.version", "v1.28.5+k3s1"))
    artifact = str(getNested(data, "k3s.artifactUrl", "https://rancher-mirror.rancher.cn/k3s"))
    configured = getNested(data, "k3s.installVersion")
    if configured:
        return str(configured)
    if "rancher-mirror.rancher.cn/k3s" in artifact:
        return version.replace("+", "-")
    return version


def masterNode(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the unique master node."""
    return [node for node in nodes if node["role"] == "master"][0]


def configValues(data: dict[str, Any], nodes: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the local shell variable values consumed by applyK3sCluster.sh."""
    cluster_name = str(data.get("clusterName") or data.get("cluster_name") or "seedemu-k3s")
    master = masterNode(nodes)
    registry_host = getNested(data, "registry.host", master["ip"])
    return {
        "clusterName": cluster_name,
        "setupTmpDir": expandPath(str(getNested(data, "outputs.tmpDir", SETUP_DIR / "tmp"))),
        "k3sUser": master["sshUser"],
        "k3sSshKey": master["sshKey"],
        "k3sMasterName": master["name"],
        "k3sMasterIp": master["ip"],
        "k3sVersion": getNested(data, "k3s.version", "v1.28.5+k3s1"),
        "k3sInstallVersion": installVersion(data),
        "k3sArtifactUrl": getNested(data, "k3s.artifactUrl", "https://rancher-mirror.rancher.cn/k3s"),
        "k3sForceReinstall": str(getNested(data, "k3s.forceReinstall", True)).lower(),
        "k3sClusterCidr": getNested(data, "k3s.clusterCidr", "10.42.0.0/16"),
        "k3sServiceCidr": getNested(data, "k3s.serviceCidr", "10.43.0.0/16"),
        "k3sNodeCidrMaskSizeIpv4": getNested(data, "k3s.nodeCidrMaskSizeIpv4", 20),
        "k3sMaxPods": getNested(data, "k3s.maxPods", 4000),
        "kubeletRegistryQps": getNested(data, "k3s.kubeletRegistryQps", 100),
        "kubeletRegistryBurst": getNested(data, "k3s.kubeletRegistryBurst", 20),
        "registryHost": registry_host,
        "registryPort": getNested(data, "registry.port", 5000),
        "dockerIoMirrorEndpoint": getNested(data, "registry.dockerIoMirrorEndpoint", "https://docker.m.daocloud.io"),
        "cniMasterInterface": getNested(data, "cni.defaultMasterInterface", "ens2"),
        "cni0HashMax": getNested(data, "tuning.cni0HashMax", 16384),
        "userMaxNetNamespaces": getNested(data, "tuning.userMaxNetNamespaces", 65536),
        "neighGcThresh1": getNested(data, "tuning.neighGcThresh1", 1048576),
        "neighGcThresh2": getNested(data, "tuning.neighGcThresh2", 4194304),
        "neighGcThresh3": getNested(data, "tuning.neighGcThresh3", 8388608),
        "netdevMaxBacklog": getNested(data, "tuning.netdevMaxBacklog", 1000000),
        "optmemMax": getNested(data, "tuning.optmemMax", 25165824),
        "rebootAfterTuning": str(getNested(data, "tuning.rebootAfterTuning", False)).lower(),
        "outputKubeconfig": expandPath(str(getNested(data, "outputs.kubeconfig", SETUP_DIR / f"{cluster_name}.kubeconfig.yaml"))),
        "outputInventory": expandPath(str(getNested(data, "outputs.inventory", SETUP_DIR / f"{cluster_name}.inventory.yaml"))),
    }


def commandShellVars(args: argparse.Namespace) -> None:
    """Print local shell assignments derived from configK3s.yaml."""
    data = loadYaml(args.config)
    nodes = yamlNodes(data)
    for key, value in configValues(data, nodes).items():
        print(f"{key}={shlex.quote(str(value))}")


def commandNodesTsv(args: argparse.Namespace) -> None:
    """Print normalized node rows as TSV."""
    for node in yamlNodes(loadYaml(args.config)):
        print(
            "\t".join(
                str(node[key])
                for key in ("name", "role", "ip", "mac", "vcpus", "memoryMb", "diskGb")
            )
        )


def commandNodeSshVars(args: argparse.Namespace) -> None:
    """Print shell assignments for one node's SSH settings."""
    nodes = yamlNodes(loadYaml(args.config))
    selected = next((node for node in nodes if node["name"] == args.name), None)
    if selected is None:
        raise SystemExit(f"Node not found in configK3s.yaml: {args.name}")
    print(f"nodeSshUser={shlex.quote(str(selected['sshUser']))}")
    print(f"nodeSshKey={shlex.quote(str(selected['sshKey']))}")


def commandWriteAnsibleInventory(args: argparse.Namespace) -> None:
    """Write the temporary Ansible inventory used by applyK3sCluster.sh."""
    data = loadYaml(args.config)
    nodes = yamlNodes(data)
    vals = configValues(data, nodes)
    master = masterNode(nodes)
    workers = [node for node in nodes if node["role"] == "worker"]
    payload = {
        "all": {
            "vars": {
                "k3s_version": vals["k3sVersion"],
                "k3s_install_version": vals["k3sInstallVersion"],
                "seed_registry_host": vals["registryHost"],
                "seed_registry_port": int(vals["registryPort"]),
                "seed_docker_io_mirror_endpoint": vals["dockerIoMirrorEndpoint"],
                "seed_k3s_artifact_url": vals["k3sArtifactUrl"],
                "seed_cni_master_interface": vals["cniMasterInterface"],
                "seed_k3s_cluster_cidr": vals["k3sClusterCidr"],
                "seed_k3s_service_cidr": vals["k3sServiceCidr"],
                "seed_k3s_node_cidr_mask_size_ipv4": int(vals["k3sNodeCidrMaskSizeIpv4"]),
                "seed_k3s_max_pods": int(vals["k3sMaxPods"]),
                "seed_k3s_force_reinstall": str(vals["k3sForceReinstall"]).lower() == "true",
            },
            "children": {
                "master": {
                    "hosts": {
                        master["name"]: {
                            "ansible_host": master["ip"],
                            "ansible_user": master["sshUser"],
                            "ansible_ssh_private_key_file": master["sshKey"],
                            "k3s_role": "server",
                            "seedemu_as_group": "master",
                        }
                    }
                },
                "workers": {
                    "hosts": {
                        node["name"]: {
                            "ansible_host": node["ip"],
                            "ansible_user": node["sshUser"],
                            "ansible_ssh_private_key_file": node["sshKey"],
                            "k3s_role": "agent",
                            "seedemu_as_group": f"worker-{index}",
                        }
                        for index, node in enumerate(workers, start=1)
                    }
                },
            },
        }
    }
    _writeYaml(args.output, payload)
    print(args.output)


def commandWriteClusterInventory(args: argparse.Namespace) -> None:
    """Write a persistent human-readable cluster inventory YAML."""
    data = loadYaml(args.config)
    nodes = yamlNodes(data)
    vals = configValues(data, nodes)
    payload = {
        "clusterName": vals["clusterName"],
        "runtime": "k3s",
        "k3s": {
            "clusterCidr": vals["k3sClusterCidr"],
            "serviceCidr": vals["k3sServiceCidr"],
            "nodeCidrMaskSizeIpv4": int(vals["k3sNodeCidrMaskSizeIpv4"]),
            "maxPods": int(vals["k3sMaxPods"]),
        },
        "registry": {"host": vals["registryHost"], "port": int(vals["registryPort"])},
        "nodes": [
            {
                "name": node["name"],
                "role": node["role"],
                "managementIp": node["ip"],
                "ssh": {"user": node["sshUser"], "key": node["sshKey"]},
                "resources": {
                    "vcpus": node["vcpus"],
                    "memoryMb": node["memoryMb"],
                    "diskGb": node["diskGb"],
                },
                "labels": {"kubernetes.io/hostname": node["name"]},
            }
            for node in nodes
        ],
    }
    _writeYaml(vals["outputInventory"], payload)
    print(vals["outputInventory"])


def commandWriteRunningConfig(args: argparse.Namespace) -> None:
    """Write legacy configRunning.yaml for running/Makefile.

    Args:
        args.config: Source configK3s.yaml path.
        args.output_dir: Optional compile output directory.
        args.image_registry_prefix: Logical image prefix in k8s.yaml.
        args.rollout_timeout_seconds: Rollout wait timeout for make up/wait.
    """
    data = loadYaml(args.config)
    output_dir = args.output_dir or str(SETUP_DIR.parent / "emulate" / "output")
    output_path = expandPath(str(getNested(data, "outputs.runningConfig", SETUP_DIR / "configRunning.yaml")))
    payload = {
        "setupConfig": str(Path(args.config).expanduser().resolve()),
        "outputDir": expandPath(output_dir),
        "imageRegistryPrefix": args.image_registry_prefix,
        "rolloutTimeoutSeconds": int(args.rollout_timeout_seconds),
    }
    _writeYaml(output_path, payload)
    print(output_path)


def _writeYaml(path: str, payload: dict[str, Any]) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _snakeCase(value: str) -> str:
    return re.sub(r"(?<!^)([A-Z])", r"_\1", value).lower()


def _camelCase(value: str) -> str:
    parts = value.split("_")
    return parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="configK3s.yaml path")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("shell-vars").set_defaults(func=commandShellVars)
    sub.add_parser("nodes-tsv").set_defaults(func=commandNodesTsv)
    node_ssh = sub.add_parser("node-ssh-vars")
    node_ssh.add_argument("--name", required=True)
    node_ssh.set_defaults(func=commandNodeSshVars)

    ansible = sub.add_parser("write-ansible-inventory")
    ansible.add_argument("--output", required=True)
    ansible.set_defaults(func=commandWriteAnsibleInventory)

    cluster = sub.add_parser("write-cluster-inventory")
    cluster.set_defaults(func=commandWriteClusterInventory)

    running = sub.add_parser("write-running-config")
    running.add_argument("--output-dir")
    running.add_argument("--image-registry-prefix", default="seedemu")
    running.add_argument("--rollout-timeout-seconds", default="1800")
    running.set_defaults(func=commandWriteRunningConfig)

    args = parser.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
