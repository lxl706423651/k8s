#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import yaml


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


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

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

    args = parser.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
