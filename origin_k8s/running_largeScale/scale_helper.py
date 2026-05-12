#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml


WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "ReplicaSet", "ReplicationController", "Pod"}


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


def mapped_image_dict(images_yaml: str, image_registry_prefix: str, registry_prefix: str) -> dict[str, str]:
    registry = registry_prefix.rstrip("/")
    logical_prefix = image_registry_prefix.rstrip("/")
    mapping: dict[str, str] = {}
    for item in load_images(images_yaml):
        logical = item["name"].strip()
        mapped = f"{registry}/{strip_prefix(logical, logical_prefix)}"
        mapping[logical] = mapped
        logical_repo, _ = split_repo_tag(logical)
        mapped_repo, _ = split_repo_tag(mapped)
        mapping[logical_repo] = mapped_repo
    return mapping


def pod_spec(doc: dict[str, Any]) -> dict[str, Any] | None:
    kind = str(doc.get("kind", ""))
    if kind == "Pod":
        spec = doc.get("spec")
        return spec if isinstance(spec, dict) else None
    if kind in WORKLOAD_KINDS:
        tmpl = ((doc.get("spec") or {}).get("template") or {})
        spec = tmpl.get("spec")
        return spec if isinstance(spec, dict) else None
    return None


def manifest_namespace(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        for doc in yaml.safe_load_all(handle):
            if isinstance(doc, dict) and doc.get("kind") == "Namespace":
                name = (doc.get("metadata") or {}).get("name")
                if name:
                    return str(name)
    raise SystemExit(f"No Namespace object found in {path}")


def inventory_nodes(path: str, role_filter: str) -> list[dict[str, str]]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    out = []
    for node in data.get("nodes", []):
        role = str(node.get("role", "") or "")
        if role_filter == "workers" and role in {"master", "control-plane"}:
            continue
        name = str(node.get("name", "") or "")
        ip = str(node.get("management_ip", "") or "")
        if name and ip:
            out.append({"name": name, "ip": ip, "role": role})
    if not out:
        raise SystemExit(f"No usable nodes found in inventory: {path}")
    return out


def cmd_inventory_nodes(args: argparse.Namespace) -> None:
    for node in inventory_nodes(args.inventory, args.node_role_filter):
        print(f"{node['name']}\t{node['ip']}\t{node['role']}")


def cmd_namespace(args: argparse.Namespace) -> None:
    print(manifest_namespace(args.manifest))


def cmd_deployment_names(args: argparse.Namespace) -> None:
    with open(args.manifest, "r", encoding="utf-8") as handle:
        for doc in yaml.safe_load_all(handle):
            if isinstance(doc, dict) and doc.get("kind") == "Deployment":
                name = (doc.get("metadata") or {}).get("name")
                if name:
                    print(name)


def cmd_kustomization(args: argparse.Namespace) -> None:
    registry = args.registry_prefix.rstrip("/")
    logical_prefix = args.image_registry_prefix.rstrip("/")
    images = []
    for item in load_images(args.images_yaml):
        logical = item["name"].strip()
        repo, tag = split_repo_tag(strip_prefix(logical, logical_prefix))
        logical_repo, _ = split_repo_tag(logical)
        images.append({"name": logical_repo, "newName": f"{registry}/{repo}", "newTag": tag})
    payload = {"resources": [Path(args.resource).name], "images": images}
    Path(args.output).write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def cmd_mapped_images(args: argparse.Namespace) -> None:
    mapping = mapped_image_dict(args.images_yaml, args.image_registry_prefix, args.registry_prefix)
    for item in load_images(args.images_yaml):
        logical = item["name"].strip()
        context = item["context"].strip()
        print(f"{mapping[logical]}\t{context}")


def cmd_node_image_refs(args: argparse.Namespace) -> None:
    nodes = [item["name"] for item in inventory_nodes(args.inventory, args.node_role_filter)]
    known_nodes = set(nodes)
    image_mapping = mapped_image_dict(args.images_yaml, args.image_registry_prefix, args.registry_prefix)
    node_to_images: dict[str, set[str]] = defaultdict(set)
    missing_selector = []
    unknown_images = []

    with open(args.manifest, "r", encoding="utf-8") as handle:
        for index, doc in enumerate(yaml.safe_load_all(handle), start=1):
            if not isinstance(doc, dict):
                continue
            spec = pod_spec(doc)
            if not spec:
                continue
            meta = doc.get("metadata") or {}
            selector = spec.get("nodeSelector") or {}
            node = selector.get("kubernetes.io/hostname") if isinstance(selector, dict) else None
            if not node:
                missing_selector.append({"doc_index": index, "kind": doc.get("kind"), "name": meta.get("name")})
                continue
            if node not in known_nodes:
                missing_selector.append({"doc_index": index, "kind": doc.get("kind"), "name": meta.get("name"), "unknown_node": node})
                continue
            for key in ("initContainers", "containers"):
                for container in spec.get(key) or []:
                    if not isinstance(container, dict):
                        continue
                    image = str(container.get("image", "") or "").strip()
                    if not image:
                        continue
                    mapped = image_mapping.get(image)
                    if mapped is None:
                        repo, _ = split_repo_tag(image)
                        mapped = image_mapping.get(repo)
                    if mapped is None:
                        unknown_images.append({"doc_index": index, "name": meta.get("name"), "image": image})
                        continue
                    node_to_images[node].add(mapped)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "manifest": str(Path(args.manifest).resolve()),
        "images_yaml": str(Path(args.images_yaml).resolve()),
        "nodes": {},
        "missing_selector_count": len(missing_selector),
        "unknown_image_count": len(unknown_images),
        "missing_selector_examples": missing_selector[:20],
        "unknown_image_examples": unknown_images[:20],
    }
    for node in nodes:
        images = sorted(node_to_images.get(node, set()))
        node_file = out_dir / f"images_{node}.txt"
        node_file.write_text("".join(f"{image}\n" for image in images), encoding="utf-8")
        summary["nodes"][node] = {"image_count": len(images), "file": str(node_file)}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")

    if args.require_complete and (missing_selector or unknown_images):
        raise SystemExit(
            f"node image refs incomplete: missing_selector={len(missing_selector)} unknown_images={len(unknown_images)}; "
            f"see {out_dir / 'summary.json'}"
        )


def cmd_doc_meta(args: argparse.Namespace) -> None:
    content = Path(args.file).read_text(encoding="utf-8")
    doc = yaml.safe_load(content) or {}
    kind = doc.get("kind", "") if isinstance(doc, dict) else ""
    node = ""
    if isinstance(doc, dict):
        spec = pod_spec(doc)
        selector = (spec or {}).get("nodeSelector") or {}
        if isinstance(selector, dict):
            node = str(selector.get("kubernetes.io/hostname", "") or "")
    print(kind)
    print(node)


def pressure_value(text: str, key: str) -> str:
    match = re.search(rf"(?:^|\s){re.escape(key)}=([0-9]+)", text)
    return match.group(1) if match else "0"


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    inv = sub.add_parser("inventory-nodes")
    inv.add_argument("--inventory", required=True)
    inv.add_argument("--node-role-filter", choices=["all", "workers"], default="all")
    inv.set_defaults(func=cmd_inventory_nodes)

    ns = sub.add_parser("namespace")
    ns.add_argument("--manifest", required=True)
    ns.set_defaults(func=cmd_namespace)

    deps = sub.add_parser("deployment-names")
    deps.add_argument("--manifest", required=True)
    deps.set_defaults(func=cmd_deployment_names)

    kust = sub.add_parser("kustomization")
    kust.add_argument("--images-yaml", required=True)
    kust.add_argument("--image-registry-prefix", required=True)
    kust.add_argument("--registry-prefix", required=True)
    kust.add_argument("--resource", required=True)
    kust.add_argument("--output", required=True)
    kust.set_defaults(func=cmd_kustomization)

    mapped = sub.add_parser("mapped-images")
    mapped.add_argument("--images-yaml", required=True)
    mapped.add_argument("--image-registry-prefix", required=True)
    mapped.add_argument("--registry-prefix", required=True)
    mapped.set_defaults(func=cmd_mapped_images)

    refs = sub.add_parser("node-image-refs")
    refs.add_argument("--manifest", required=True)
    refs.add_argument("--images-yaml", required=True)
    refs.add_argument("--inventory", required=True)
    refs.add_argument("--node-role-filter", choices=["all", "workers"], default="all")
    refs.add_argument("--image-registry-prefix", required=True)
    refs.add_argument("--registry-prefix", required=True)
    refs.add_argument("--out-dir", required=True)
    refs.add_argument("--require-complete", action="store_true")
    refs.set_defaults(func=cmd_node_image_refs)

    meta = sub.add_parser("doc-meta")
    meta.add_argument("file")
    meta.set_defaults(func=cmd_doc_meta)

    args = parser.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
