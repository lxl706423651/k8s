#!/usr/bin/env python3
"""Run the complete b63 large-scale placement experiment.

The workflow builds a b63-owned KVM/K3s/Kube-OVN cluster from
configkvm_b63.yaml, compiles a real topology, runs b63 placement optimization,
deploys the workload through local running/ stages, starts BIRD, enables kernel
export, and leaves the post-start BGP test disabled in the main flow.

Artifacts are stored under experiments/<scale>_<timestamp>/ and every stage is
timed in summary.json. This script does not depend on another example's
generated cluster files.
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


def findRepoRoot(start: Path) -> Path:
    """Return the SeedEMU repository root above start."""
    for candidate in (start, *start.parents):
        if (candidate / "setup.py").is_file() and (candidate / "seedemu").is_dir():
            return candidate
    raise RuntimeError(f"Cannot find SeedEMU repository root above {start}")


REPO_ROOT = findRepoRoot(SCRIPT_DIR)
DEFAULT_RESULTS_ROOT = SCRIPT_DIR / "experiments"
DEFAULT_TOPOLOGY_DIR = Path("~/seed-emulator/topology")
DEFAULT_KVM_CONFIG = SCRIPT_DIR / "configkvm_b63.yaml"
DEFAULT_CONFIG_K3S = SCRIPT_DIR / "configK3s-b63.yaml"
DEFAULT_KUBECONFIG = SCRIPT_DIR / "kubeconfig-b63.yaml"
DEFAULT_INVENTORY = SCRIPT_DIR / "inventory-b63.yaml"


def parseArgs() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topology-size", type=int, default=1897)
    parser.add_argument("--namespace", default="")
    parser.add_argument("--topology-dir", type=Path, default=DEFAULT_TOPOLOGY_DIR)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--results-root", type=Path, default=DEFAULT_RESULTS_ROOT)
    parser.add_argument("--kvm-config", type=Path, default=DEFAULT_KVM_CONFIG)
    parser.add_argument("--config-k3s", type=Path, default=DEFAULT_CONFIG_K3S)
    parser.add_argument("--kubeconfig", type=Path, default=DEFAULT_KUBECONFIG)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument(
        "--algorithm",
        default="optimized",
        choices=(
            "optimized",
            "network-only",
            "pod-count",
            "pod-count-balanced",
            "resource-only",
            "hypergraph",
            "kubernetes-default-scheduler",
        ),
    )
    parser.add_argument("--network-cost-mode", choices=("ratio", "pair", "endpoint-exposure"), default="ratio")
    parser.add_argument("--alpha", type=float, default=1.0)
    parser.add_argument("--beta", type=float, default=1.0)
    parser.add_argument("--improvement-passes", type=int, default=0)
    parser.add_argument("--pinning-mode", choices=("node-selector", "node-name"), default="node-selector")
    parser.add_argument(
        "--skip-resource-request-injection",
        action="store_true",
        help="keep estimated resources in placement_report.json but do not write requests into the deploy manifest",
    )
    parser.add_argument("--cni-type", default="kube-ovn")
    parser.add_argument("--cni-master-interface", default="ens2")
    parser.add_argument(
        "--network-backend",
        default="kube-ovn",
        choices=("kube-ovn", "ovn", "macvlan"),
        help="runtime manifest backend; kube-ovn renders output/k8s.kube-ovn.yaml for deploy/build",
    )
    parser.add_argument(
        "--attached-cni-type",
        default="kube-ovn",
        choices=("kube-ovn", "ovn", "macvlan"),
        help="secondary CNI rendered into k8s.kube-ovn.yaml; kube-ovn gives cross-node overlay links",
    )
    parser.add_argument("--image-pull-policy", default="IfNotPresent")
    parser.add_argument("--sample-interval-seconds", type=float, default=15.0)
    parser.add_argument("--post-deploy-seconds", type=float, default=600.0)
    parser.add_argument("--build-parallelism", type=int, default=12)
    parser.add_argument("--build-batch-size", type=int, default=50)
    parser.add_argument("--docker-buildkit", default="1")
    parser.add_argument("--docker-buildx", default="1")
    parser.add_argument("--build-skip-existing", default="0")
    parser.add_argument("--preload-batch-size", type=int, default=10)
    parser.add_argument("--preload-node-concurrency", type=int, default=4)
    parser.add_argument("--preload-image-concurrency", type=int, default=2)
    parser.add_argument("--registry-push-retries", type=int, default=5)
    parser.add_argument("--registry-push-backoff-seconds", type=int, default=5)
    parser.add_argument("--registry-push-timeout-seconds", type=int, default=180)
    parser.add_argument("--deploy-batch-size", type=int, default=10)
    parser.add_argument("--deploy-batch-sleep-seconds", type=int, default=3)
    parser.add_argument("--deploy-monitor-enabled", default="false")
    parser.add_argument("--deploy-monitor-interval", type=int, default=60)
    parser.add_argument("--deploy-warmup-batches", type=int, default=3)
    parser.add_argument("--deploy-warmup-batch-size", type=int, default=5)
    parser.add_argument("--deploy-pressure-check-seconds", type=int, default=5)
    parser.add_argument("--deploy-stabilize-timeout-seconds", type=int, default=7200)
    parser.add_argument("--deploy-max-pending-pods", type=int, default=60)
    parser.add_argument("--deploy-max-creating-pods", type=int, default=80)
    parser.add_argument("--deploy-max-notready-pods", type=int, default=120)
    parser.add_argument("--deploy-max-failed-pods", type=int, default=5)
    parser.add_argument("--deploy-wait-kube-ovn-subnets", default="true")
    parser.add_argument("--deploy-kube-ovn-subnet-timeout-seconds", type=int, default=7200)
    parser.add_argument(
        "--deploy-kube-ovn-subnet-settle-seconds",
        type=int,
        default=0,
        help="extra wait after all Kube-OVN Subnets report Ready, allowing controller IPAM to catch up",
    )
    parser.add_argument(
        "--deploy-restart-kube-ovn-controller-after-subnets",
        default="false",
        choices=["true", "false"],
        help="restart kube-ovn-controller after all generated Subnets are Ready and before creating workload Pods",
    )
    parser.add_argument("--deploy-kube-ovn-controller-restart-timeout-seconds", type=int, default=600)
    parser.add_argument("--deploy-kube-ovn-controller-post-restart-settle-seconds", type=int, default=30)
    parser.add_argument("--wait-ready-interval-seconds", type=int, default=30)
    parser.add_argument("--wait-ready-timeout-seconds", type=int, default=7200)
    parser.add_argument("--clean-namespace", default="true")
    parser.add_argument("--clean-check-interval-seconds", type=int, default=20)
    parser.add_argument("--clean-timeout-seconds", type=int, default=3600)
    parser.add_argument("--clean-force-finalizer-cleanup", default="false")
    parser.add_argument("--clean-auto-finalize-stuck-namespace", default="true")
    parser.add_argument("--clean-auto-finalize-after-seconds", type=int, default=120)
    parser.add_argument("--bgp-test-exec-timeout-seconds", type=int, default=45)
    parser.add_argument("--bgp-test-timeout-seconds", type=int, default=1800)
    parser.add_argument("--bgp-test-retry-interval-seconds", type=int, default=10)
    parser.add_argument("--skip-cluster-build", action="store_true")
    parser.add_argument("--destroy-existing-cluster", action="store_true")
    parser.add_argument("--destroy-after-pass", action="store_true", help="compatibility flag; after-pass destroy is currently disabled")
    parser.add_argument("--skip-clean", action="store_true")
    parser.add_argument("--skip-preflight", action="store_true")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--skip-deploy", action="store_true")
    parser.add_argument("--skip-wait-ready", action="store_true")
    parser.add_argument("--skip-start-bird", action="store_true")
    parser.add_argument("--skip-start-kernel", action="store_true")
    parser.add_argument("--skip-bgp-test", action="store_true", help="compatibility flag; test-bgp is currently disabled")
    parser.add_argument("--skip-dynamic", action="store_true")
    parser.add_argument("--keep-temp", action="store_true")
    return parser.parse_args()


def main() -> int:
    """Run the full b63 experiment."""
    args = parseArgs()
    normalizeArgs(args)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    run_dir = resolveRunDir(args, timestamp)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "topology_size").write_text(f"{args.topology_size}\n", encoding="utf-8")

    summary_path = run_dir / "summary.json"
    summary = createSummary(args, run_dir)
    writeJson(summary_path, summary)
    shutil.copy2(args.kvm_config, run_dir / "configkvm_b63.yaml")

    try:
        if args.destroy_existing_cluster:
            destroyExistingCluster(args, run_dir, summary, summary_path)
        if not args.skip_cluster_build:
            buildCluster(args, run_dir, summary, summary_path)

        if not args.skip_clean:
            runRunningStage("clean", "clean.sh", args, run_dir, summary, summary_path)
        runRunningStage("compile", "compile.sh", args, run_dir, summary, summary_path)
        runRunningStage("placement", "placement.sh", args, run_dir, summary, summary_path)
        if not args.skip_preflight:
            runRunningStage("preflight", "preflight.sh", args, run_dir, summary, summary_path)
        if not args.skip_build:
            runRunningStage("build", "build.sh", args, run_dir, summary, summary_path)
        if not args.skip_deploy:
            runDeployAndDynamic(args, run_dir, summary, summary_path)
        if not args.skip_start_bird:
            runRunningStage("start-bird", "start_bird.sh", args, run_dir, summary, summary_path)
        if not args.skip_start_kernel:
            runRunningStage("start-kernel", "start_bird_kernel.sh", args, run_dir, summary, summary_path)
        # test-bgp is intentionally disabled in the current manual flow.
        # if not args.skip_bgp_test:
        #     runBgpTest(args, run_dir, summary, summary_path)
        # destroy-cluster-after-pass is intentionally disabled in the current manual flow.
        # if args.destroy_after_pass:
        #     destroyClusterAfterPass(args, run_dir, summary, summary_path)

        summary["status"] = "PASS"
        summary["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        writeJson(summary_path, summary)
        print(f"Complete b63 experiment passed: {run_dir}")
        return 0
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - preserve summary for failed long runs.
        summary["status"] = "FAIL"
        summary["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        summary["failure"] = str(exc)
        writeJson(summary_path, summary)
        raise


def normalizeArgs(args: argparse.Namespace) -> None:
    """Resolve paths and fill derived defaults."""
    args.topology_dir = resolvePath(args.topology_dir)
    args.results_root = resolvePath(args.results_root)
    args.kvm_config = resolvePath(args.kvm_config)
    args.config_k3s = resolvePath(args.config_k3s)
    args.kubeconfig = resolvePath(args.kubeconfig)
    args.inventory = resolvePath(args.inventory)
    if not args.namespace:
        args.namespace = f"seedemu-b63-{args.topology_size}"
    requireFile(args.kvm_config)


def resolvePath(path: Path) -> Path:
    """Resolve a CLI path relative to the b63 directory."""
    expanded = Path(os.path.expandvars(os.path.expanduser(str(path))))
    if expanded.is_absolute():
        return expanded.resolve()
    return (SCRIPT_DIR / expanded).resolve()


def resolveRunDir(args: argparse.Namespace, timestamp: str) -> Path:
    """Return experiments/<scale>_<timestamp> unless explicitly provided."""
    if args.run_dir:
        return resolvePath(args.run_dir)
    return args.results_root / f"{args.topology_size}_{timestamp}"


def createSummary(args: argparse.Namespace, run_dir: Path) -> dict[str, Any]:
    """Create an initial summary.json payload."""
    return {
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "finishedAt": None,
        "status": "RUNNING",
        "scale": args.topology_size,
        "namespace": args.namespace,
        "runDir": str(run_dir),
        "topologyDir": str(args.topology_dir),
        "kvmConfig": str(args.kvm_config),
        "configK3s": str(args.config_k3s),
        "kubeconfig": str(args.kubeconfig),
        "inventory": str(args.inventory),
        "algorithm": args.algorithm,
        "networkCostMode": args.network_cost_mode,
        "networkBackend": args.network_backend,
        "attachedCniType": args.attached_cni_type,
        "skipResourceRequestInjection": args.skip_resource_request_injection,
        "stageOrder": [],
        "stages": {},
        "bgpTest": {
            "pass": None,
            "failedBrdnodes": [],
            "failedProtocols": [],
        },
        "placementReport": str(run_dir / "output" / "placement_report.json"),
    }


def buildRuntimeEnv(args: argparse.Namespace) -> dict[str, str]:
    """Return only process environment needed to import local Python code."""
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT),
            "PYTHONNOUSERSITE": "1",
        }
    )
    return env


def commonStageArgs(args: argparse.Namespace, run_dir: Path) -> list[str]:
    """Return explicit arguments shared by b63 running scripts."""
    registry_host, registry_port = resolveRegistry(args.inventory, args.kvm_config)
    ssh_user, ssh_key = resolveSsh(args.inventory, args.kvm_config)
    return [
        "--experiment-dir",
        str(run_dir),
        "--source-root",
        str(REPO_ROOT),
        "--topology-size",
        str(args.topology_size),
        "--topology-dir",
        str(args.topology_dir),
        "--namespace",
        args.namespace,
        "--kubeconfig",
        str(args.kubeconfig),
        "--inventory",
        str(args.inventory),
        "--config-k3s",
        str(args.config_k3s),
        "--kvm-config",
        str(args.kvm_config),
        "--registry-host",
        registry_host,
        "--registry-port",
        registry_port,
        "--registry",
        f"{registry_host}:{registry_port}",
        "--k3s-user",
        ssh_user,
        "--k3s-ssh-key",
        ssh_key,
        "--cni-type",
        args.cni_type,
        "--cni-master-interface",
        args.cni_master_interface,
        "--network-backend",
        args.network_backend,
        "--attached-cni-type",
        args.attached_cni_type,
        "--image-pull-policy",
        args.image_pull_policy,
    ]


def stageArgs(name: str, args: argparse.Namespace, run_dir: Path) -> list[str]:
    """Return explicit per-stage arguments, keeping commands readable in logs."""
    values = commonStageArgs(args, run_dir)
    if name == "clean":
        values.extend(
            [
                "--clean-namespace",
                args.clean_namespace,
                "--clean-check-interval-seconds",
                str(args.clean_check_interval_seconds),
                "--clean-timeout-seconds",
                str(args.clean_timeout_seconds),
                "--clean-force-finalizer-cleanup",
                args.clean_force_finalizer_cleanup,
                "--clean-auto-finalize-stuck-namespace",
                args.clean_auto_finalize_stuck_namespace,
                "--clean-auto-finalize-after-seconds",
                str(args.clean_auto_finalize_after_seconds),
            ]
        )
    elif name == "placement":
        values.extend(
            [
                "--placement-algorithm",
                args.algorithm,
                "--network-cost-mode",
                args.network_cost_mode,
                "--placement-alpha",
                str(args.alpha),
                "--placement-beta",
                str(args.beta),
                "--placement-improvement-passes",
                str(args.improvement_passes),
                "--placement-pinning-mode",
                args.pinning_mode,
                "--skip-resource-request-injection",
                str(args.skip_resource_request_injection).lower(),
            ]
        )
    elif name == "build":
        values.extend(
            [
                "--build-parallelism",
                str(args.build_parallelism),
                "--build-batch-size",
                str(args.build_batch_size),
                "--docker-buildkit",
                args.docker_buildkit,
                "--docker-buildx",
                args.docker_buildx,
                "--build-skip-existing",
                args.build_skip_existing,
                "--preload-batch-size",
                str(args.preload_batch_size),
                "--preload-node-concurrency",
                str(args.preload_node_concurrency),
                "--preload-image-concurrency",
                str(args.preload_image_concurrency),
                "--registry-push-retries",
                str(args.registry_push_retries),
                "--registry-push-backoff-seconds",
                str(args.registry_push_backoff_seconds),
                "--registry-push-timeout-seconds",
                str(args.registry_push_timeout_seconds),
            ]
        )
    elif name in {"deploy", "wait-ready"}:
        values.extend(
            [
                "--deploy-batch-size",
                str(args.deploy_batch_size),
                "--deploy-batch-sleep-seconds",
                str(args.deploy_batch_sleep_seconds),
                "--deploy-monitor-enabled",
                args.deploy_monitor_enabled,
                "--deploy-monitor-interval",
                str(args.deploy_monitor_interval),
                "--deploy-warmup-batches",
                str(args.deploy_warmup_batches),
                "--deploy-warmup-batch-size",
                str(args.deploy_warmup_batch_size),
                "--deploy-pressure-check-seconds",
                str(args.deploy_pressure_check_seconds),
                "--deploy-stabilize-timeout-seconds",
                str(args.deploy_stabilize_timeout_seconds),
                "--deploy-max-pending-pods",
                str(args.deploy_max_pending_pods),
                "--deploy-max-creating-pods",
                str(args.deploy_max_creating_pods),
                "--deploy-max-notready-pods",
                str(args.deploy_max_notready_pods),
                "--deploy-max-failed-pods",
                str(args.deploy_max_failed_pods),
                "--deploy-wait-kube-ovn-subnets",
                args.deploy_wait_kube_ovn_subnets,
                "--deploy-kube-ovn-subnet-timeout-seconds",
                str(args.deploy_kube_ovn_subnet_timeout_seconds),
                "--deploy-kube-ovn-subnet-settle-seconds",
                str(args.deploy_kube_ovn_subnet_settle_seconds),
                "--deploy-restart-kube-ovn-controller-after-subnets",
                args.deploy_restart_kube_ovn_controller_after_subnets,
                "--deploy-kube-ovn-controller-restart-timeout-seconds",
                str(args.deploy_kube_ovn_controller_restart_timeout_seconds),
                "--deploy-kube-ovn-controller-post-restart-settle-seconds",
                str(args.deploy_kube_ovn_controller_post_restart_settle_seconds),
                "--wait-ready-interval-seconds",
                str(args.wait_ready_interval_seconds),
                "--wait-ready-timeout-seconds",
                str(args.wait_ready_timeout_seconds),
            ]
        )
    return values


def resolveRegistry(inventory: Path, kvm_config: Path) -> tuple[str, str]:
    """Resolve registry host and port from inventory, then kvm config."""
    if inventory.exists():
        data = loadYaml(inventory)
        registry = data.get("registry") or {}
        host = str(registry.get("host") or "").strip()
        port = str(registry.get("port") or "").strip()
        if host and port:
            return host, port
    data = loadYaml(kvm_config)
    registry = data.get("registry") or {}
    port = str(registry.get("port") or "5000")
    for node in data.get("nodes") or []:
        if str(node.get("role") or "") in {"master", "control-plane"}:
            return str(node.get("ip") or "127.0.0.1"), port
    return "127.0.0.1", port


def resolveSsh(inventory: Path, kvm_config: Path) -> tuple[str, str]:
    """Resolve SSH user and key used by image preload and dynamic sampling."""
    if inventory.exists():
        data = loadYaml(inventory)
        default_ssh = data.get("ssh") or {}
        for node in data.get("nodes") or []:
            ssh = node.get("ssh") or default_ssh
            user = str(ssh.get("user") or default_ssh.get("user") or "")
            key = str(ssh.get("key") or default_ssh.get("key") or default_ssh.get("default_key_path") or "")
            if user and key:
                return user, str(Path(key).expanduser())
    data = loadYaml(kvm_config)
    ssh = data.get("ssh") or {}
    return str(ssh.get("user") or "ubuntu"), str(Path(str(ssh.get("key") or "~/.ssh/id_ed25519")).expanduser())


def buildCluster(args: argparse.Namespace, run_dir: Path, summary: dict[str, Any], summary_path: Path) -> None:
    """Run k8sTools.py build for the b63 cluster."""
    cmd = [
        "python3",
        "./k8sTools.py",
        "build",
        "--input",
        str(args.kvm_config),
        "--config-k3s",
        str(args.config_k3s),
        "--kubeconfig",
        str(args.kubeconfig),
        "--inventory",
        str(args.inventory),
    ]
    if args.keep_temp:
        cmd.append("--keep-temp")
    runRequiredStage("build-cluster", cmd, SCRIPT_DIR, run_dir / "build-cluster.log", summary, summary_path, buildRuntimeEnv(args))


def destroyExistingCluster(args: argparse.Namespace, run_dir: Path, summary: dict[str, Any], summary_path: Path) -> None:
    """Destroy the currently recorded b63 cluster before a fresh build."""
    if not args.config_k3s.exists():
        recordSkippedStage("destroy-existing-cluster", "configK3s file does not exist", summary, summary_path)
        return
    cmd = ["python3", "./k8sTools.py", "destroy", "-d", str(args.config_k3s)]
    if args.keep_temp:
        cmd.append("--keep-temp")
    runRequiredStage(
        "destroy-existing-cluster",
        cmd,
        SCRIPT_DIR,
        run_dir / "destroy-existing-cluster.log",
        summary,
        summary_path,
        buildRuntimeEnv(args),
    )


def destroyClusterAfterPass(args: argparse.Namespace, run_dir: Path, summary: dict[str, Any], summary_path: Path) -> None:
    """Optionally destroy the b63 cluster after every validation stage passes."""
    cmd = ["python3", "./k8sTools.py", "destroy", "-d", str(args.config_k3s)]
    if args.keep_temp:
        cmd.append("--keep-temp")
    runRequiredStage(
        "destroy-cluster-after-pass",
        cmd,
        SCRIPT_DIR,
        run_dir / "destroy-cluster-after-pass.log",
        summary,
        summary_path,
        buildRuntimeEnv(args),
    )


def runRunningStage(
    name: str,
    script_name: str,
    args: argparse.Namespace,
    run_dir: Path,
    summary: dict[str, Any],
    summary_path: Path,
) -> None:
    """Run one executable from running/ with the experiment directory."""
    script = RUNNING_DIR / script_name
    requireFile(script)
    runRequiredStage(
        name,
        [str(script), *stageArgs(name, args, run_dir)],
        RUNNING_DIR,
        run_dir / f"{name}.orchestrator.log",
        summary,
        summary_path,
        buildRuntimeEnv(args),
    )


def runDeployAndDynamic(args: argparse.Namespace, run_dir: Path, summary: dict[str, Any], summary_path: Path) -> None:
    """Run deploy/wait-ready while collecting dynamic metrics."""
    runtime_nodes = placement.loadNodesFromInventory(args.inventory) if args.inventory.exists() else []
    if not runtime_nodes and args.config_k3s.exists():
        runtime_nodes = placement.loadNodesFromConfigK3s(args.config_k3s)
    sampler = placement.DynamicSampler(runtime_nodes, args.sample_interval_seconds)
    deploy_error = None
    deployment_start = time.time()
    ready_at = None
    if not args.skip_dynamic:
        sampler.startSampler()
    try:
        runRunningStage("deploy", "deploy.sh", args, run_dir, summary, summary_path)
        if not args.skip_wait_ready:
            runRunningStage("wait-ready", "wait-ready.sh", args, run_dir, summary, summary_path)
            ready_at = time.time()
        if not args.skip_dynamic and args.post_deploy_seconds > 0:
            time.sleep(args.post_deploy_seconds)
    except BaseException as exc:  # noqa: BLE001 - record failed deploys, including SystemExit, in placement report.
        deploy_error = str(exc)
        raise
    finally:
        if not args.skip_dynamic:
            sampler.stopSampler()
        dynamic = sampler.summarizeSamples() if not args.skip_dynamic else {"sampleCount": 0, "skipped": True}
        dynamic["deploymentStart"] = deployment_start
        dynamic["readyAt"] = ready_at
        dynamic["deploymentEnd"] = time.time()
        dynamic["deployError"] = deploy_error
        dynamic["postDeploySampleSeconds"] = 0.0 if args.skip_dynamic else args.post_deploy_seconds
        dynamic["workloadStateFinal"] = placement.collectKubernetesState(args.kubeconfig, args.namespace)
        appendDynamicReport(run_dir, dynamic, summary)
        writeJson(summary_path, summary)


def runBgpTest(args: argparse.Namespace, run_dir: Path, summary: dict[str, Any], summary_path: Path) -> None:
    """Run the post-start BGP protocol test and copy its compact result into summary.json."""
    rc = runStage(
        "test-bgp",
        [
            "python3",
            str(RUNNING_DIR / "test.py"),
            "--namespace",
            args.namespace,
            "--artifact-dir",
            str(run_dir),
            "--kubeconfig",
            str(args.kubeconfig),
            "--exec-timeout-seconds",
            str(args.bgp_test_exec_timeout_seconds),
            "--timeout-seconds",
            str(args.bgp_test_timeout_seconds),
            "--retry-interval-seconds",
            str(args.bgp_test_retry_interval_seconds),
        ],
        RUNNING_DIR,
        run_dir / "test-bgp.log",
        summary,
        summary_path,
        buildRuntimeEnv(args),
    )
    test_summary_path = run_dir / "bgp_test_summary.json"
    bgp_pass = rc == 0
    if test_summary_path.exists():
        test_summary = json.loads(test_summary_path.read_text(encoding="utf-8"))
        bgp_pass = bool(test_summary.get("pass", test_summary.get("status") == "PASS"))
        summary["bgpTest"] = {
            "pass": bgp_pass,
            "status": test_summary.get("status"),
            "targets": test_summary.get("targets"),
            "verified": test_summary.get("verified"),
            "failedBrdnodes": test_summary.get("failedBrdnodes", []),
            "failedProtocols": test_summary.get("failedProtocols", []),
            "summaryPath": str(test_summary_path),
        }
        writeJson(summary_path, summary)
    if rc != 0 or not bgp_pass:
        summary["status"] = "FAIL"
        summary["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        writeJson(summary_path, summary)
        raise SystemExit(rc or 1)


def runRequiredStage(
    name: str,
    cmd: list[str],
    cwd: Path,
    log_path: Path,
    summary: dict[str, Any],
    summary_path: Path,
    env: dict[str, str],
) -> None:
    """Run one required stage and raise SystemExit on failure."""
    rc = runStage(name, cmd, cwd, log_path, summary, summary_path, env)
    if rc != 0:
        summary["status"] = "FAIL"
        summary["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        writeJson(summary_path, summary)
        raise SystemExit(rc)


def runStage(
    name: str,
    cmd: list[str],
    cwd: Path,
    log_path: Path,
    summary: dict[str, Any],
    summary_path: Path,
    env: dict[str, str],
) -> int:
    """Run one command, stream output to console/log, and record timing."""
    started = time.time()
    record = {
        "name": name,
        "command": " ".join(cmd),
        "cwd": str(cwd),
        "log": str(log_path),
        "start": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(started)),
        "end": None,
        "durationSeconds": None,
        "exitCode": None,
        "status": "RUNNING",
    }
    summary["stageOrder"].append(name)
    summary["stages"][name] = record
    writeJson(summary_path, summary)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8", errors="replace") as log_fh:
        log_fh.write(f"$ {' '.join(cmd)}\n")
        log_fh.flush()
        process = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            sys.stdout.write(line)
            log_fh.write(line)
        rc = process.wait()
    ended = time.time()
    record["end"] = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ended))
    record["durationSeconds"] = round(ended - started, 2)
    record["exitCode"] = rc
    record["status"] = "PASS" if rc == 0 else "FAIL"
    writeJson(summary_path, summary)
    return rc


def recordSkippedStage(name: str, reason: str, summary: dict[str, Any], summary_path: Path) -> None:
    """Record a skipped stage in summary.json."""
    summary["stageOrder"].append(name)
    summary["stages"][name] = {
        "name": name,
        "status": "SKIPPED",
        "reason": reason,
        "durationSeconds": 0.0,
        "exitCode": 0,
    }
    writeJson(summary_path, summary)


def appendDynamicReport(run_dir: Path, dynamic: dict[str, Any], summary: dict[str, Any]) -> None:
    """Append dynamic validation and workflow paths to placement_report.json."""
    report_path = run_dir / "output" / "placement_report.json"
    if not report_path.exists():
        return
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["dynamicValidation"] = dynamic
    report["b63Workflow"] = {
        "summaryPath": str(run_dir / "summary.json"),
        "runDir": str(run_dir),
        "stageOrder": summary.get("stageOrder", []),
        "stages": summary.get("stages", {}),
    }
    placement.writeJson(report_path, report)


def loadYaml(path: Path) -> dict[str, Any]:
    """Load a YAML mapping from path, returning an empty mapping for absent files."""
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"Invalid YAML mapping: {path}")
    return data


def writeJson(path: Path, payload: dict[str, Any]) -> None:
    """Write formatted JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def requireFile(path: Path) -> None:
    """Raise if a required input file is missing."""
    if not path.exists():
        raise SystemExit(f"Required file not found: {path}")


if __name__ == "__main__":
    raise SystemExit(main())
