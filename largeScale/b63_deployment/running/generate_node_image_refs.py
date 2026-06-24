#!/usr/bin/env python3
"""Generate per-node image preload lists from a nodeSelector-pinned manifest."""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import yaml

WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "ReplicaSet", "ReplicationController", "Pod"}


def pod_spec(doc: dict) -> dict | None:
    kind = str(doc.get("kind", ""))
    if kind == "Pod":
        return (doc.get("spec") or {}) if isinstance(doc.get("spec"), dict) else None
    if kind in WORKLOAD_KINDS:
        spec = doc.get("spec") or {}
        tmpl = (spec.get("template") or {}) if isinstance(spec, dict) else {}
        return (tmpl.get("spec") or {}) if isinstance(tmpl, dict) else None
    return None


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: generate_node_image_refs.py <k8s.yaml> <out_dir> [node ...]", file=sys.stderr)
        return 2

    manifest_path = Path(sys.argv[1]).expanduser().resolve()
    out_dir = Path(sys.argv[2]).expanduser().resolve()
    expected_nodes = sys.argv[3:]

    if not manifest_path.is_file():
        print(f"Manifest not found: {manifest_path}", file=sys.stderr)
        return 1

    node_to_images: dict[str, set[str]] = defaultdict(set)
    missing_selector = []

    with manifest_path.open("r", encoding="utf-8") as handle:
        for index, doc in enumerate(yaml.safe_load_all(handle), start=1):
            if not isinstance(doc, dict):
                continue
            spec = pod_spec(doc)
            if not spec:
                continue
            selector = spec.get("nodeSelector") or {}
            node = selector.get("kubernetes.io/hostname") if isinstance(selector, dict) else None
            if not node:
                meta = doc.get("metadata") or {}
                missing_selector.append({
                    "doc_index": index,
                    "kind": doc.get("kind"),
                    "name": meta.get("name"),
                    "namespace": meta.get("namespace"),
                })
                continue

            for key in ("initContainers", "containers"):
                for container in spec.get(key) or []:
                    if not isinstance(container, dict):
                        continue
                    image = str(container.get("image", "")).strip()
                    if image:
                        node_to_images[node].add(image)

    out_dir.mkdir(parents=True, exist_ok=True)
    all_nodes = sorted(set(expected_nodes) | set(node_to_images.keys()))
    summary = {
        "manifest": str(manifest_path),
        "out_dir": str(out_dir),
        "nodes": {},
        "missing_selector_count": len(missing_selector),
        "missing_selector_examples": missing_selector[:20],
    }

    for node in all_nodes:
        images = sorted(node_to_images.get(node, set()))
        node_file = out_dir / f"images_{node}.txt"
        node_file.write_text("".join(f"{image}\n" for image in images), encoding="utf-8")
        summary["nodes"][node] = {
            "image_count": len(images),
            "file": str(node_file),
        }

    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
