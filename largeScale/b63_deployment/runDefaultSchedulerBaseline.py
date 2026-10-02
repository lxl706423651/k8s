#!/usr/bin/env python3
"""Prepare and optionally run the b63 Kubernetes default-scheduler baseline.

Inputs:
  - an existing b63 compile output directory containing k8s.raw.yaml or k8s.yaml
    plus images.yaml/images.txt and Docker build contexts.
  - b63 configK3s/kubeconfig/inventory files for the active cluster.

Outputs:
  - experiments/<scale>_<timestamp>_default_scheduler/output/k8s.yaml with
    node placement and Kubernetes resource requests removed.
  - default_scheduler_baseline.json, summary.json, and optional dynamic/routing
    validation artifacts in the baseline experiment directory.

Side effects:
  - Without --run-up, only writes files under the baseline experiment directory.
  - With --run-up, runs k8sTools.py up, which builds/pushes workload images,
    creates Kubernetes resources, and waits for Pods to become Ready.
  - With --run-routing, additionally starts BIRD and verifies BIRD/FIB state.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml

import runPlacementExperiment as placement


SCRIPT_DIR = Path(__file__).resolve().parent
RUNNING_DIR = SCRIPT_DIR / "running"


def parseArgs() -> argparse.Namespace:
    """Parse command-line arguments for default-scheduler baseline runs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topology-size", type=int, required=True)
    parser.add_argument(
        "--source-output",
        type=Path,
        default=Path("output"),
        help="compile output to copy; prefer an output directory with k8s.raw.yaml",
    )
    parser.add_argument("--experiment-dir", type=Path)
    parser.add_argument("--namespace")
    parser.add_argument("--k8s-tools", type=Path, default=SCRIPT_DIR / "k8sTools.py")
    parser.add_argument("--config-k3s", type=Path, default=SCRIPT_DIR / "configK3s-b63.yaml")
    parser.add_argument("--kubeconfig", type=Path, default=SCRIPT_DIR / "kubeconfig-b63.yaml")
    parser.add_argument("--inventory", type=Path, default=SCRIPT_DIR / "inventory-b63.yaml")
    parser.add_argument("--kvm-config", type=Path, default=SCRIPT_DIR / "configkvm_b63.yaml")
    parser.add_argument("--source-root", type=Path, default=SCRIPT_DIR.parents[1])
    parser.add_argument("--topology-dir", type=Path, default=Path("~/seed-emulator/topology"))
    parser.add_argument("--registry-host", default="192.168.122.230")
    parser.add_argument("--registry-port", default="5063")
    parser.add_argument("--registry")
    parser.add_argument("--k3s-user", default="ubuntu")
    parser.add_argument("--k3s-ssh-key", type=Path, default=Path("~/.ssh/id_ed25519"))
    parser.add_argument("--cni-type", default="macvlan")
    parser.add_argument("--cni-master-interface", default="ens2")
    parser.add_argument("--network-backend", default="macvlan")
    parser.add_argument("--attached-cni-type", default="macvlan")
    parser.add_argument("--sample-interval-seconds", type=float, default=15.0)
    parser.add_argument("--post-deploy-seconds", type=float, default=60.0)
    parser.add_argument("--deploy-timeout-seconds", type=float, default=7200.0)
    parser.add_argument("--run-up", action="store_true", help="run k8sTools.py up after preparing output")
    parser.add_argument("--clean-first", action="store_true", help="run k8sTools.py down on the baseline output before up")
    parser.add_argument("--run-routing", action="store_true", help="run start-bird/start-kernel and verification after up")
    parser.add_argument("--keep-temp", action="store_true")
    return parser.parse_args()


def resolvePath(path: Path) -> Path:
    """Resolve a possibly user-relative path."""
    return Path(os.path.expandvars(os.path.expanduser(str(path)))).resolve()


def defaultRunDir(topology_size: int) -> Path:
    """Return a scale+timestamp baseline experiment directory path."""
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return SCRIPT_DIR / "experiments" / f"{topology_size}_{stamp}_default_scheduler"


def readYamlDocs(path: Path) -> list[dict[str, Any]]:
    """Read a Kubernetes YAML stream, preserving only mapping documents."""
    with path.open("r", encoding="utf-8") as handle:
        return [doc for doc in yaml.safe_load_all(handle) if isinstance(doc, dict)]


def writeYamlDocs(path: Path, docs: list[dict[str, Any]]) -> None:
    """Write a Kubernetes YAML stream."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump_all(docs, handle, sort_keys=False)


def findManifestNamespace(docs: list[dict[str, Any]], fallback: str) -> str:
    """Return the first explicit manifest namespace or fallback."""
    for doc in docs:
        metadata = doc.get("metadata") or {}
        if doc.get("kind") == "Namespace" and metadata.get("name"):
            return str(metadata["name"])
        if metadata.get("namespace"):
            return str(metadata["namespace"])
    return fallback


def podSpec(doc: dict[str, Any]) -> dict[str, Any] | None:
    """Return a workload Pod spec for placement/resource scrubbing."""
    kind = doc.get("kind")
    if kind == "Pod":
        spec = doc.get("spec")
    elif kind in {"Deployment", "ReplicaSet", "StatefulSet", "DaemonSet", "Job"}:
        spec = ((doc.get("spec") or {}).get("template") or {}).get("spec")
    elif kind == "CronJob":
        spec = (
            (((doc.get("spec") or {}).get("jobTemplate") or {}).get("spec") or {})
            .get("template", {})
            .get("spec")
        )
    else:
        spec = None
    return spec if isinstance(spec, dict) else None


def metadataForPodTemplate(doc: dict[str, Any]) -> dict[str, Any] | None:
    """Return Pod template metadata for placement annotation scrubbing."""
    kind = doc.get("kind")
    if kind == "Pod":
        metadata = doc.get("metadata")
    elif kind in {"Deployment", "ReplicaSet", "StatefulSet", "DaemonSet", "Job"}:
        metadata = ((doc.get("spec") or {}).get("template") or {}).get("metadata")
    elif kind == "CronJob":
        metadata = (
            (((doc.get("spec") or {}).get("jobTemplate") or {}).get("spec") or {})
            .get("template", {})
            .get("metadata")
        )
    else:
        metadata = None
    return metadata if isinstance(metadata, dict) else None


def scrubManifestForDefaultScheduler(
    docs: list[dict[str, Any]],
    namespace: str,
    *, remove_requests: bool = True,
) -> dict[str, Any]:
    """Scrub docs in namespace; optionally remove requests for legacy callers."""
    removed_node_names = 0
    removed_node_selectors = 0
    removed_requests = 0
    removed_annotations = 0
    controller_count = 0

    for doc in docs:
        metadata = doc.setdefault("metadata", {})
        kind = doc.get("kind")
        if kind == "Namespace":
            metadata["name"] = namespace
        elif kind not in {"ClusterRole", "ClusterRoleBinding", "CustomResourceDefinition", "StorageClass"}:
            metadata["namespace"] = namespace

        subjects = (doc.get("subjects") or []) if kind in {"RoleBinding", "ClusterRoleBinding"} else []
        for subject in subjects:
            if isinstance(subject, dict) and subject.get("kind") == "ServiceAccount":
                subject["namespace"] = namespace

        spec = podSpec(doc)
        if spec is not None:
            controller_count += 1
            if "nodeName" in spec:
                spec.pop("nodeName", None)
                removed_node_names += 1
            selector = spec.get("nodeSelector")
            if isinstance(selector, dict) and "kubernetes.io/hostname" in selector:
                selector.pop("kubernetes.io/hostname", None)
                removed_node_selectors += 1
                if not selector:
                    spec.pop("nodeSelector", None)
            for container in spec.get("containers") or []:
                if not isinstance(container, dict):
                    continue
                resources = container.get("resources")
                requests = resources.get("requests") if isinstance(resources, dict) else None
                if remove_requests and isinstance(requests, dict) and requests:
                    resources.pop("requests", None)
                    removed_requests += 1
                    if not resources:
                        container.pop("resources", None)

        for item_metadata in (metadata, metadataForPodTemplate(doc)):
            if not isinstance(item_metadata, dict):
                continue
            annotations = item_metadata.get("annotations")
            if not isinstance(annotations, dict):
                continue
            for key in list(annotations):
                if str(key).startswith("seedemu.k8s.placement/"):
                    annotations.pop(key, None)
                    removed_annotations += 1
            if not annotations:
                item_metadata.pop("annotations", None)

    return {
        "namespace": namespace,
        "controllerCount": controller_count,
        "removedNodeNames": removed_node_names,
        "removedHostnameNodeSelectors": removed_node_selectors,
        "removedResourceRequests": removed_requests,
        "removedPlacementAnnotations": removed_annotations,
    }


def prepareSchedulerManifest(docs: list[dict], namespace: str,
                             eligible_nodes: list[str] | None = None) -> None:
    """Unpin docs for real scheduling, preserving resource/network semantics.

    eligible_nodes restricts the scheduler's node pool, never a per-Pod target.
    Existing non-hostname constraints remain part of the source workload.
    """
    scrubManifestForDefaultScheduler(docs, namespace, remove_requests=False)
    for doc in docs:
        spec = podSpec(doc)
        if spec is None:
            continue
        spec['schedulerName'] = 'default-scheduler'
        if eligible_nodes is not None:
            affinity = spec.setdefault('affinity', {}).setdefault('nodeAffinity', {})
            required = affinity.setdefault('requiredDuringSchedulingIgnoredDuringExecution', {})
            terms = required.setdefault('nodeSelectorTerms', [{}])
            for term in terms:
                term.setdefault('matchExpressions', []).append({
                    'key': 'kubernetes.io/hostname', 'operator': 'In',
                    'values': list(eligible_nodes),
                })


def copyOutputTree(source_output: Path, baseline_output: Path) -> None:
    """Copy compile output into a clean baseline output directory."""
    if baseline_output.exists():
        shutil.rmtree(baseline_output)
    ignore = shutil.ignore_patterns("deploy_batches", "kustomization.yaml", "placement_report.json")
    shutil.copytree(source_output, baseline_output, ignore=ignore)


def prepareDefaultOutput(args: argparse.Namespace, run_dir: Path) -> dict[str, Any]:
    """Create a k8sTools-compatible output directory for default scheduling."""
    source_output = resolvePath(args.source_output)
    baseline_output = run_dir / "output"
    if not source_output.is_dir():
        raise SystemExit(f"Missing source output directory: {source_output}")
    copyOutputTree(source_output, baseline_output)

    source_manifest = source_output / "k8s.raw.yaml"
    if not source_manifest.exists():
        source_manifest = source_output / "k8s.source.yaml"
    if not source_manifest.exists():
        source_manifest = source_output / "k8s.yaml"
    if not source_manifest.exists():
        raise SystemExit(f"No source manifest found in {source_output}")

    docs = readYamlDocs(source_manifest)
    original_namespace = findManifestNamespace(docs, f"seedemu-b63-{args.topology_size}")
    namespace = args.namespace or f"{original_namespace}-default-scheduler"
    scrub_summary = scrubManifestForDefaultScheduler(docs, namespace)
    deploy_manifest = baseline_output / "k8s.yaml"
    writeYamlDocs(deploy_manifest, docs)
    shutil.copy2(source_manifest, baseline_output / "k8s.default-source.yaml")

    baseline = {
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "topologySize": args.topology_size,
        "baseline": "kubernetes-default-scheduler",
        "sourceOutput": str(source_output),
        "sourceManifest": str(source_manifest),
        "outputDir": str(baseline_output),
        "deployManifest": str(deploy_manifest),
        "originalNamespace": original_namespace,
        "namespace": namespace,
        "resourceRequestsInjected": False,
        "hardPlacementInjected": False,
        "manifestScrub": scrub_summary,
    }
    writeJson(run_dir / "default_scheduler_baseline.json", baseline)
    return baseline


def writeJson(path: Path, payload: dict[str, Any]) -> None:
    """Write compact, deterministic JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def runLoggedStage(
    name: str,
    command: list[str],
    cwd: Path,
    log_path: Path,
    summary: dict[str, Any],
    timeout: float | None = None,
) -> None:
    """Run one stage, teeing output to a log and recording status."""
    start = time.time()
    summary.setdefault("stages", {})[name] = {
        "status": "RUNNING",
        "command": " ".join(command),
        "log": str(log_path),
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    writeJson(Path(summary["summaryPath"]), summary)
    with log_path.open("w", encoding="utf-8") as log:
        log.write("$ " + " ".join(command) + "\n")
        log.flush()
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    item = summary["stages"][name]
    item["durationSeconds"] = round(time.time() - start, 2)
    item["exitCode"] = completed.returncode
    item["status"] = "PASS" if completed.returncode == 0 else "FAIL"
    writeJson(Path(summary["summaryPath"]), summary)
    if completed.returncode != 0:
        raise SystemExit(f"{name} failed with exit code {completed.returncode}; see {log_path}")


def commonRunningArgs(args: argparse.Namespace, run_dir: Path, namespace: str) -> list[str]:
    """Return explicit b63 running-script arguments for routing validation."""
    registry = args.registry or f"{args.registry_host}:{args.registry_port}"
    return [
        "--experiment-dir",
        str(run_dir),
        "--source-root",
        str(resolvePath(args.source_root)),
        "--topology-size",
        str(args.topology_size),
        "--topology-dir",
        str(resolvePath(args.topology_dir)),
        "--namespace",
        namespace,
        "--kubeconfig",
        str(resolvePath(args.kubeconfig)),
        "--inventory",
        str(resolvePath(args.inventory)),
        "--config-k3s",
        str(resolvePath(args.config_k3s)),
        "--kvm-config",
        str(resolvePath(args.kvm_config)),
        "--registry-host",
        args.registry_host,
        "--registry-port",
        str(args.registry_port),
        "--registry",
        registry,
        "--k3s-user",
        args.k3s_user,
        "--k3s-ssh-key",
        str(resolvePath(args.k3s_ssh_key)),
        "--cni-type",
        args.cni_type,
        "--cni-master-interface",
        args.cni_master_interface,
        "--network-backend",
        args.network_backend,
        "--attached-cni-type",
        args.attached_cni_type,
        "--image-pull-policy",
        "IfNotPresent",
    ]


def runK8sToolsUp(args: argparse.Namespace, run_dir: Path, baseline: dict[str, Any], summary: dict[str, Any]) -> None:
    """Run k8sTools.py up while collecting dynamic metrics."""
    kubeconfig = resolvePath(args.kubeconfig)
    config_k3s = resolvePath(args.config_k3s)
    inventory = resolvePath(args.inventory)
    runtime_nodes = placement.loadNodesFromInventory(inventory) if inventory.exists() else []
    if not runtime_nodes and config_k3s.exists():
        runtime_nodes = placement.loadNodesFromConfigK3s(config_k3s)

    sampler = placement.DynamicSampler(runtime_nodes, args.sample_interval_seconds)
    dynamic: dict[str, Any] = {
        "deploymentStart": time.time(),
        "readyAt": None,
        "deployError": None,
        "postDeploySampleSeconds": args.post_deploy_seconds,
    }
    sampler.startSampler()
    try:
        keep_temp = ["--keep-temp"] if args.keep_temp else []
        if args.clean_first:
            runLoggedStage(
                "k8sTools-down",
                [
                    "python3",
                    str(resolvePath(args.k8s_tools)),
                    "down",
                    "-f",
                    str(baseline["outputDir"]),
                    "-k",
                    str(kubeconfig),
                    *keep_temp,
                ],
                SCRIPT_DIR,
                run_dir / "k8sTools-down.log",
                summary,
                timeout=args.deploy_timeout_seconds,
            )
        runLoggedStage(
            "k8sTools-up",
            [
                "python3",
                str(resolvePath(args.k8s_tools)),
                "up",
                "-f",
                str(baseline["outputDir"]),
                "-k",
                str(kubeconfig),
                "-d",
                str(config_k3s),
                *keep_temp,
            ],
            SCRIPT_DIR,
            run_dir / "k8sTools-up.log",
            summary,
            timeout=args.deploy_timeout_seconds,
        )
        dynamic["readyAt"] = time.time()
        if args.post_deploy_seconds > 0:
            time.sleep(args.post_deploy_seconds)
    except BaseException as exc:  # noqa: BLE001 - preserve failure in summary.
        dynamic["deployError"] = str(exc)
        raise
    finally:
        sampler.stopSampler()
        dynamic.update(sampler.summarizeSamples())
        dynamic["deploymentEnd"] = time.time()
        dynamic["workloadStateFinal"] = placement.collectKubernetesState(kubeconfig, str(baseline["namespace"]))
        summary["dynamicValidation"] = dynamic
        writeJson(Path(summary["summaryPath"]), summary)


def runRoutingStages(args: argparse.Namespace, run_dir: Path, namespace: str, summary: dict[str, Any]) -> None:
    """Run BIRD/FIB validation scripts against the baseline namespace."""
    common = commonRunningArgs(args, run_dir, namespace)
    stages = [
        ("start-bird", "start_bird.sh"),
        ("verify-after-start-bird", "verifyStartBird.sh"),
        ("start-kernel", "start_bird_kernel.sh"),
        ("verify-after-fib-write", "verifyFibRoutes.sh"),
    ]
    for name, script in stages:
        runLoggedStage(
            name,
            [str(RUNNING_DIR / script), *common],
            SCRIPT_DIR,
            run_dir / f"{name}.orchestrator.log",
            summary,
            timeout=36000,
        )


def main() -> int:
    """Prepare and optionally run the default-scheduler baseline."""
    args = parseArgs()
    run_dir = resolvePath(args.experiment_dir) if args.experiment_dir else defaultRunDir(args.topology_size)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "topology_size").write_text(f"{args.topology_size}\n", encoding="utf-8")

    summary = {
        "status": "RUNNING",
        "summaryPath": str(run_dir / "summary.json"),
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "topologySize": args.topology_size,
        "baseline": "kubernetes-default-scheduler",
        "runDir": str(run_dir),
        "stages": {},
    }
    writeJson(Path(summary["summaryPath"]), summary)
    try:
        baseline = prepareDefaultOutput(args, run_dir)
        summary["defaultSchedulerBaseline"] = baseline
        writeJson(Path(summary["summaryPath"]), summary)
        if args.run_up:
            runK8sToolsUp(args, run_dir, baseline, summary)
        if args.run_routing:
            if not args.run_up:
                summary.setdefault("notes", []).append("run-routing was requested without run-up; using existing namespace state")
            runRoutingStages(args, run_dir, str(baseline["namespace"]), summary)
        summary["status"] = "PASS"
        summary["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        writeJson(Path(summary["summaryPath"]), summary)
        print(f"default scheduler baseline ready: {run_dir}")
        return 0
    except BaseException as exc:  # noqa: BLE001 - keep summary for failed long runs.
        summary["status"] = "FAIL"
        summary["failure"] = str(exc)
        summary["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        writeJson(Path(summary["summaryPath"]), summary)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
