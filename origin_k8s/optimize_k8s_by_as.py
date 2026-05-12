#!/usr/bin/env python3
from __future__ import annotations

import sys
import argparse
import json
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List

import yaml


DEFAULT_OUTPUT_DIR = Path("/home/lxl/k8s/origin_k8s/emulate/output")


def ensure_affinity(doc: Dict[str, Any]) -> bool:
    if doc.get("kind") != "Deployment":
        return False

    meta = doc.get("metadata") or {}
    labels = meta.get("labels") or {}
    asn = str(labels.get("seedemu.io/asn", "") or "").strip()
    if not asn:
        return False

    pod_spec = (
        doc.setdefault("spec", {})
        .setdefault("template", {})
        .setdefault("spec", {})
    )
    affinity = pod_spec.setdefault("affinity", {})
    pod_affinity = affinity.setdefault("podAffinity", {})
    required = pod_affinity.setdefault("requiredDuringSchedulingIgnoredDuringExecution", [])

    target_term = {
        "labelSelector": {
            "matchExpressions": [
                {
                    "key": "seedemu.io/asn",
                    "operator": "In",
                    "values": [asn],
                }
            ]
        },
        "topologyKey": "kubernetes.io/hostname",
    }

    for term in required:
        if (
            isinstance(term, dict)
            and term.get("topologyKey") == target_term["topologyKey"]
            and (term.get("labelSelector") or {}).get("matchExpressions") == target_term["labelSelector"]["matchExpressions"]
        ):
            return False

    required.append(target_term)
    return True


def optimize_manifest(src: Path, dst: Path) -> int:
    with src.open("r", encoding="utf-8") as handle:
        docs = list(yaml.safe_load_all(handle))

    changed = 0
    out_docs: List[Any] = []
    for doc in docs:
        if isinstance(doc, dict):
            work = deepcopy(doc)
            if ensure_affinity(work):
                changed += 1
            out_docs.append(work)
        else:
            out_docs.append(doc)

    with dst.open("w", encoding="utf-8") as handle:
        yaml.safe_dump_all(out_docs, handle, sort_keys=False)

    return changed


def load_inventory_nodes(path: Path, role_filter: str) -> list[str]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}

    nodes = []
    for node in data.get("nodes", []):
        role = str(node.get("role", "") or "")
        if role_filter == "workers" and role in {"master", "control-plane"}:
            continue
        name = str(node.get("name", "") or "").strip()
        if name:
            nodes.append(name)

    if not nodes:
        raise SystemExit(f"No usable nodes found in inventory: {path}")
    return nodes


def deployment_weight(doc: Dict[str, Any]) -> int:
    spec = doc.get("spec") or {}
    replicas = spec.get("replicas", 1)
    try:
        return max(1, int(replicas))
    except Exception:
        return 1


def deployment_asn(doc: Dict[str, Any]) -> str:
    labels = (doc.get("metadata") or {}).get("labels") or {}
    asn = str(labels.get("seedemu.io/asn", "") or "").strip()
    if asn:
        return asn
    name = str((doc.get("metadata") or {}).get("name", "") or "").strip()
    return f"__unlabeled__:{name}"


def ensure_node_selector(doc: Dict[str, Any], node: str) -> None:
    pod_spec = (
        doc.setdefault("spec", {})
        .setdefault("template", {})
        .setdefault("spec", {})
    )
    selector = pod_spec.setdefault("nodeSelector", {})
    selector["kubernetes.io/hostname"] = node


def optimize_manifest_hard_by_as(
    src: Path,
    dst: Path,
    nodes: list[str],
    plan_path: Path | None = None,
) -> int:
    with src.open("r", encoding="utf-8") as handle:
        docs = list(yaml.safe_load_all(handle))

    asn_weights: dict[str, int] = defaultdict(int)
    for doc in docs:
        if isinstance(doc, dict) and doc.get("kind") == "Deployment":
            asn_weights[deployment_asn(doc)] += deployment_weight(doc)

    node_loads = {node: 0 for node in nodes}
    asn_to_node: dict[str, str] = {}
    for asn, weight in sorted(asn_weights.items(), key=lambda item: (-item[1], item[0])):
        node = min(nodes, key=lambda item: (node_loads[item], item))
        asn_to_node[asn] = node
        node_loads[node] += weight

    changed = 0
    out_docs: List[Any] = []
    node_deployments: dict[str, list[str]] = {node: [] for node in nodes}
    for doc in docs:
        if isinstance(doc, dict):
            work = deepcopy(doc)
            if work.get("kind") == "Deployment":
                asn = deployment_asn(work)
                node = asn_to_node[asn]
                ensure_node_selector(work, node)
                changed += 1
                name = str((work.get("metadata") or {}).get("name", "") or "")
                if name:
                    node_deployments[node].append(name)
            out_docs.append(work)
        else:
            out_docs.append(doc)

    with dst.open("w", encoding="utf-8") as handle:
        yaml.safe_dump_all(out_docs, handle, sort_keys=False)

    if plan_path is not None:
        plan = {
            "source": str(src),
            "output": str(dst),
            "mode": "hard_by_as",
            "nodes": [
                {
                    "name": node,
                    "assigned_weight": node_loads[node],
                    "deployment_count": len(node_deployments[node]),
                    "deployments": sorted(node_deployments[node]),
                }
                for node in nodes
            ],
            "asn_to_node": asn_to_node,
        }
        plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True), encoding="utf-8")

    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output_dir", nargs="?", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--src")
    parser.add_argument("--dst")
    parser.add_argument("--mode", choices=["affinity", "hard"], default="affinity")
    parser.add_argument("--inventory")
    parser.add_argument("--node-role-filter", choices=["all", "workers"], default="all")
    parser.add_argument("--plan")
    args = parser.parse_args()

    output_dir = Path(args.output_dir).resolve()
    src = Path(args.src).resolve() if args.src else output_dir / "k8s.yaml"
    dst = Path(args.dst).resolve() if args.dst else output_dir / ("k8s_scale.yaml" if args.mode == "hard" else "k8s_opt.yaml")

    if not src.exists():
        print(f"Missing source manifest: {src}", file=sys.stderr)
        return 1

    if args.mode == "hard":
        if not args.inventory:
            print("--inventory is required for --mode hard", file=sys.stderr)
            return 2
        nodes = load_inventory_nodes(Path(args.inventory).expanduser().resolve(), args.node_role_filter)
        changed = optimize_manifest_hard_by_as(
            src,
            dst,
            nodes,
            Path(args.plan).expanduser().resolve() if args.plan else output_dir / "scale_placement_plan.json",
        )
    else:
        changed = optimize_manifest(src, dst)

    print(f"Optimized manifest written to: {dst}")
    if args.mode == "hard":
        print(f"Deployments updated with hard same-AS nodeSelector: {changed}")
    else:
        print(f"Deployments updated with same-AS podAffinity: {changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
