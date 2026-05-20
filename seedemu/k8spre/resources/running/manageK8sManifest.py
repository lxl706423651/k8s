#!/usr/bin/env python3
"""Resolve running-stage configuration and render deploy helper artifacts.

Inputs:
- configRunning.yaml, which points to configK3s.yaml and compile output.
- configK3s.yaml, whose master node provides the default registry/SSH target.

Outputs:
- scalar values consumed by Makefile,
- kustomization.yaml image mappings,
- manifest-derived namespace and deployment names.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml


def load_yaml(path: str) -> dict[str, Any]:
    """Load a YAML mapping from path."""
    data = yaml.safe_load(Path(path).expanduser().read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"Invalid YAML root in {path}: expected mapping")
    return data


def get_nested(data: dict[str, Any], path: str, default: Any = None) -> Any:
    """Read a dotted path from YAML, accepting camelCase and snake_case keys."""
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict):
            return default
        candidates = [part, snake_case(part), camel_case(part)]
        found = False
        for candidate in candidates:
            if candidate in cur:
                cur = cur[candidate]
                found = True
                break
        if not found:
            return default
    return cur


def normalizeRole(role: Any) -> str:
    """Normalize a configK3s.yaml node role for master-node detection.

    Args:
        role: Raw YAML role value from one node item.
    """
    return str(role or "").strip().lower()


def getSetupNodes(setup: dict[str, Any]) -> list[dict[str, Any]]:
    """Return node mappings from configK3s.yaml.

    Args:
        setup: Parsed configK3s.yaml mapping.
    """
    nodes = setup.get("nodes") or []
    if not isinstance(nodes, list):
        raise SystemExit("configK3s.yaml field nodes must be a list")
    invalid = [node for node in nodes if not isinstance(node, dict)]
    if invalid:
        raise SystemExit(f"configK3s.yaml node item must be a mapping: {invalid[0]}")
    return nodes


def findMasterNode(setup: dict[str, Any]) -> dict[str, Any] | None:
    """Find the single master node used as the registry and build SSH target.

    Args:
        setup: Parsed configK3s.yaml mapping.
    """
    masters = [
        node
        for node in getSetupNodes(setup)
        if normalizeRole(node.get("role")) in {"master", "server", "control-plane", "control_plane"}
    ]
    if len(masters) > 1:
        names = ", ".join(str(node.get("name") or node.get("ip") or "<unnamed>") for node in masters)
        raise SystemExit(f"configK3s.yaml must contain exactly one master node, got {len(masters)}: {names}")
    return masters[0] if masters else None


def resolveRegistryHost(setup: dict[str, Any], master_node: dict[str, Any] | None) -> str:
    """Resolve registry host from explicit registry.host or master node IP.

    Args:
        setup: Parsed configK3s.yaml mapping.
        master_node: Master node mapping returned by findMasterNode().
    """
    explicit_host = get_nested(setup, "registry.host")
    if explicit_host:
        return str(explicit_host)
    if master_node and master_node.get("ip"):
        return str(master_node["ip"])
    raise SystemExit("Cannot resolve registry host: set registry.host or provide one role=master node with ip")


def resolveSshUser(setup: dict[str, Any], master_node: dict[str, Any] | None) -> str:
    """Resolve SSH user for the registry/build host.

    Args:
        setup: Parsed configK3s.yaml mapping.
        master_node: Master node mapping returned by findMasterNode().
    """
    explicit_user = get_nested(setup, "ssh.user")
    if explicit_user:
        return str(explicit_user)
    master_user = get_nested(master_node or {}, "ssh.user")
    if master_user:
        return str(master_user)
    return "ubuntu"


def resolveSshKey(setup: dict[str, Any], master_node: dict[str, Any] | None) -> str:
    """Resolve SSH private key path for the registry/build host.

    Args:
        setup: Parsed configK3s.yaml mapping.
        master_node: Master node mapping returned by findMasterNode().
    """
    explicit_key = get_nested(setup, "ssh.key")
    if explicit_key:
        return str(Path(str(explicit_key)).expanduser())
    master_key = get_nested(master_node or {}, "ssh.key")
    if master_key:
        return str(Path(str(master_key)).expanduser())
    return str(Path("~/.ssh/id_ed25519").expanduser())


def running_context(config_path: str) -> dict[str, str]:
    """Resolve all Makefile-facing values from configRunning.yaml."""
    running_config_path = Path(config_path).expanduser().resolve()
    running = load_yaml(str(running_config_path))
    setup_config_path = Path(str(running.get("setupConfig") or running_config_path.parent / "../setup/configK3s.yaml")).expanduser()
    if not setup_config_path.is_absolute():
        setup_config_path = (running_config_path.parent / setup_config_path).resolve()
    setup = load_yaml(str(setup_config_path)) if setup_config_path.exists() else {}
    output_dir = Path(str(running.get("outputDir") or running_config_path.parent / "../emulate/output")).expanduser()
    if not output_dir.is_absolute():
        output_dir = (running_config_path.parent / output_dir).resolve()
    master_node = findMasterNode(setup) if setup else None
    registry_host = resolveRegistryHost(setup, master_node)
    registry_port = str(get_nested(setup, "registry.port", "5000"))
    return {
        "setupConfig": str(setup_config_path),
        "outputDir": str(output_dir),
        "manifest": str(output_dir / "k8s.yaml"),
        "imagesYaml": str(output_dir / "images.yaml"),
        "kustomization": str(output_dir / "kustomization.yaml"),
        "imageRegistryPrefix": str(running.get("imageRegistryPrefix") or "seedemu"),
        "registryPrefix": f"{registry_host}:{registry_port}",
        "kubeconfig": str(Path(str(get_nested(setup, "outputs.kubeconfig", setup_config_path.parent / "seedemu-k3s.kubeconfig.yaml"))).expanduser()),
        "sshUser": resolveSshUser(setup, master_node),
        "sshKey": resolveSshKey(setup, master_node),
        "rolloutTimeoutSeconds": str(running.get("rolloutTimeoutSeconds") or "1800"),
    }


def config_value(args: argparse.Namespace) -> None:
    """Print one resolved value from configRunning.yaml."""
    values = running_context(args.config)
    if args.key not in values:
        raise SystemExit(f"Unknown config key: {args.key}")
    print(values[args.key])


def split_repo_tag(image: str) -> tuple[str, str]:
    tail = image.rsplit("/", 1)[-1]
    if ":" in tail:
        return image.rsplit(":", 1)
    return image, "latest"


def strip_prefix(image: str, prefix: str) -> str:
    prefix = prefix.rstrip("/")
    if image.startswith(prefix + "/"):
        return image[len(prefix) + 1 :]
    return image.split("/", 1)[-1]


def load_images(path: str) -> list[dict[str, str]]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return data.get("images", [])


def mapped_images(args: argparse.Namespace) -> None:
    registry = args.registry_prefix.rstrip("/")
    logical_prefix = args.image_registry_prefix.rstrip("/")
    for item in load_images(args.images_yaml):
        logical = item["name"].strip()
        context = item["context"].strip()
        print(f"{registry}/{strip_prefix(logical, logical_prefix)}\t{context}")


def render_kustomization(args: argparse.Namespace) -> None:
    registry = args.registry_prefix.rstrip("/")
    logical_prefix = args.image_registry_prefix.rstrip("/")
    images = []
    for item in load_images(args.images_yaml):
        logical = item["name"].strip()
        repo, tag = split_repo_tag(strip_prefix(logical, logical_prefix))
        logical_repo, _ = split_repo_tag(logical)
        images.append({"name": logical_repo, "newName": f"{registry}/{repo}", "newTag": tag})
    payload = {"resources": ["k8s.yaml"], "images": images}
    Path(args.output).write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def deployment_names(args: argparse.Namespace) -> None:
    with open(args.manifest, "r", encoding="utf-8") as fh:
        for doc in yaml.safe_load_all(fh):
            if isinstance(doc, dict) and doc.get("kind") == "Deployment":
                name = (doc.get("metadata") or {}).get("name")
                if name:
                    print(name)


def namespace(args: argparse.Namespace) -> None:
    with open(args.manifest, "r", encoding="utf-8") as fh:
        for doc in yaml.safe_load_all(fh):
            if isinstance(doc, dict) and doc.get("kind") == "Namespace":
                name = (doc.get("metadata") or {}).get("name")
                if name:
                    print(name)
                    return
    raise SystemExit(f"No Namespace object found in {args.manifest}")


def validate_manifest(args: argparse.Namespace) -> None:
    seen = {}
    duplicate_errors = []
    with open(args.manifest, "r", encoding="utf-8") as fh:
        for index, doc in enumerate(yaml.safe_load_all(fh), 1):
            if not isinstance(doc, dict):
                continue
            kind = doc.get("kind")
            metadata = doc.get("metadata") or {}
            name = metadata.get("name")
            namespace_name = metadata.get("namespace") or ""
            if not kind or not name:
                continue
            key = (kind, namespace_name, name)
            if key in seen:
                duplicate_errors.append(
                    f"{kind}/{name} namespace={namespace_name or '<cluster>'} "
                    f"appears in docs {seen[key]} and {index}"
                )
            else:
                seen[key] = index

    if duplicate_errors:
        print(f"Duplicate Kubernetes resources in {args.manifest}:", flush=True)
        for error in duplicate_errors[:30]:
            print(f"  {error}", flush=True)
        if len(duplicate_errors) > 30:
            print(f"  ... {len(duplicate_errors) - 30} more", flush=True)
        raise SystemExit(1)


def snake_case(value: str) -> str:
    out = []
    for char in value:
        if char.isupper():
            out.append("_")
            out.append(char.lower())
        else:
            out.append(char)
    return "".join(out).lstrip("_")


def camel_case(value: str) -> str:
    parts = value.split("_")
    return parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:])


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    config = subparsers.add_parser("config-value")
    config.add_argument("--config", required=True)
    config.add_argument("--key", required=True)
    config.set_defaults(func=config_value)

    mapped = subparsers.add_parser("mapped-images")
    mapped.add_argument("--images-yaml", required=True)
    mapped.add_argument("--image-registry-prefix", required=True)
    mapped.add_argument("--registry-prefix", required=True)
    mapped.set_defaults(func=mapped_images)

    kustomization = subparsers.add_parser("kustomization")
    kustomization.add_argument("--images-yaml", required=True)
    kustomization.add_argument("--image-registry-prefix", required=True)
    kustomization.add_argument("--registry-prefix", required=True)
    kustomization.add_argument("--output", required=True)
    kustomization.set_defaults(func=render_kustomization)

    deployments = subparsers.add_parser("deployment-names")
    deployments.add_argument("--manifest", required=True)
    deployments.set_defaults(func=deployment_names)

    ns = subparsers.add_parser("namespace")
    ns.add_argument("--manifest", required=True)
    ns.set_defaults(func=namespace)

    validate = subparsers.add_parser("validate-manifest")
    validate.add_argument("--manifest", required=True)
    validate.set_defaults(func=validate_manifest)

    args = parser.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
