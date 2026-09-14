#!/usr/bin/env python3
"""Run b63 full-flow placement experiments across VM counts and topology sizes."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_RESULTS_ROOT = SCRIPT_DIR / "experiments"
DEFAULT_VM_CONFIG_DIR = SCRIPT_DIR / "vm_matrix_configs"

DEFAULT_ENV = {
    "BIRD_LOAD_THRESHOLD": "200",
    "BIRD_LOAD_MAX_WAIT_SECONDS": "30",
    "BIRD_NODE_CONCURRENCY": "3",
    "BIRD_LOAD_CHECK_INTERVAL_SECONDS": "10",
    "VERIFY_START_BIRD_LOAD_THRESHOLD": "250",
    "VERIFY_START_BIRD_LOAD_MAX_WAIT_SECONDS": "30",
    "VERIFY_START_BIRD_LOAD_CHECK_INTERVAL_SECONDS": "10",
    "KERNEL_LOAD_THRESHOLD": "250",
    "KERNEL_LOAD_MAX_WAIT_SECONDS": "30",
    "KERNEL_NODE_CONCURRENCY": "3",
    "KERNEL_LOAD_CHECK_INTERVAL_SECONDS": "10",
    "VERIFY_FIB_LOAD_THRESHOLD": "250",
    "VERIFY_FIB_LOAD_MAX_WAIT_SECONDS": "30",
    "VERIFY_FIB_LOAD_CHECK_INTERVAL_SECONDS": "10",
}

DEFAULT_FLOW_ARGS = [
    "--fast-greedy-score",
    "--post-deploy-seconds",
    "60",
    "--deploy-batch-size",
    "100",
    "--deploy-batch-sleep-seconds",
    "0",
    "--deploy-warmup-batches",
    "12",
    "--deploy-warmup-batch-size",
    "1",
    "--deploy-stabilize-timeout-seconds",
    "36000",
    "--deploy-max-pending-pods",
    "600",
    "--deploy-max-creating-pods",
    "600",
    "--deploy-max-creating-pods-per-node",
    "50",
    "--deploy-max-notready-pods",
    "600",
    "--deploy-max-failed-pods",
    "20",
    "--preload-node-concurrency",
    "6",
    "--preload-image-concurrency",
    "2",
]

def defaultSchedulerBuildParallelism(vm_count: int) -> int:
    """Return a conservative registry-build fanout for a matrix VM count."""
    master_vcpus = max(1, (300 + vm_count - 1) // vm_count)
    return max(24, min(48, master_vcpus * 3))


def algorithmFlowArgs(algorithm: str, vm_count: int) -> list[str]:
    """Return extra full-flow arguments for one algorithm/VM-count pair."""
    if algorithm == "kubernetes-default-scheduler":
        return [
            "--build-parallelism",
            str(defaultSchedulerBuildParallelism(vm_count)),
        ]
    return []



def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vm-counts", default="12,18,24,36,48")
    parser.add_argument("--topology-sizes", default="3083,4954,7001,9955")
    parser.add_argument("--algorithms", default="optimized,kubernetes-default-scheduler")
    parser.add_argument("--results-root", type=Path, default=DEFAULT_RESULTS_ROOT)
    parser.add_argument("--vm-config-dir", type=Path, default=DEFAULT_VM_CONFIG_DIR)
    parser.add_argument("--namespace-template", default="seedemu-b63-matrix-{vm}")
    parser.add_argument("--skip-existing-pass", action="store_true")
    parser.add_argument("--destroy-each-run", action="store_true")
    parser.add_argument("--no-destroy-on-vm-change", action="store_true")
    parser.add_argument("--continue-on-failure", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--extra-arg",
        action="append",
        default=[],
        help="append one extra argument token to every run; repeat for multiple tokens",
    )
    return parser.parse_args()


def csv_ints(value: str) -> list[int]:
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def csv_strings(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def safe_algorithm_name(name: str) -> str:
    return name.replace("_", "-").replace("/", "-")


def now_ts() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def passed_run_exists(results_root: Path, scale: int, vm_count: int, algorithm: str) -> bool:
    pattern = f"{scale}_*_{vm_count}vm_{safe_algorithm_name(algorithm)}"
    for run_dir in sorted(results_root.glob(pattern), reverse=True):
        summary_path = run_dir / "summary.json"
        if not summary_path.is_file():
            continue
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        stages = summary.get("stages") or {}
        dataplane = stages.get("dataplane") or {}
        if summary.get("status") == "PASS" and dataplane.get("status") in {None, "PASS"}:
            return True
    return False


def build_command(
    scale: int,
    vm_count: int,
    algorithm: str,
    run_dir: Path,
    kvm_config: Path,
    namespace: str,
    destroy_cluster: bool,
    extra_args: list[str],
) -> list[str]:
    cmd = [
        sys.executable,
        "./runB63LargeExperiment.py",
        "--topology-size",
        str(scale),
        "--run-dir",
        str(run_dir),
        "--kvm-config",
        str(kvm_config),
        "--namespace",
        namespace,
        "--algorithm",
        algorithm,
        *DEFAULT_FLOW_ARGS,
        *algorithmFlowArgs(algorithm, vm_count),
        *extra_args,
    ]
    if destroy_cluster:
        cmd.append("--destroy-existing-cluster")
    else:
        cmd.append("--skip-cluster-build")
    return cmd


def write_matrix_record(run_dir: Path, record: dict) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "matrix_record.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    (run_dir / "topology_size").write_text(f"{record['scale']}\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    vm_counts = csv_ints(args.vm_counts)
    scales = csv_ints(args.topology_sizes)
    algorithms = csv_strings(args.algorithms)
    results_root = args.results_root.expanduser().resolve()
    vm_config_dir = args.vm_config_dir.expanduser().resolve()
    results_root.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env.update(DEFAULT_ENV)
    destroy_on_vm_change = not args.no_destroy_on_vm_change

    failures: list[dict] = []
    for vm_count in vm_counts:
        kvm_config = vm_config_dir / f"configkvm_b63_{vm_count}vm.yaml"
        if not kvm_config.is_file():
            print(f"Missing VM config: {kvm_config}", file=sys.stderr)
            return 2
        first_run_for_vm = True
        for scale in scales:
            for algorithm in algorithms:
                if args.skip_existing_pass and passed_run_exists(results_root, scale, vm_count, algorithm):
                    print(f"[matrix] skip existing PASS scale={scale} vm={vm_count} algorithm={algorithm}")
                    continue

                tag = safe_algorithm_name(algorithm)
                run_dir = results_root / f"{scale}_{now_ts()}_{vm_count}vm_{tag}"
                namespace = args.namespace_template.format(scale=scale, vm=vm_count, algorithm=tag)
                destroy_cluster = args.destroy_each_run or (destroy_on_vm_change and first_run_for_vm)
                cmd = build_command(
                    scale=scale,
                    vm_count=vm_count,
                    algorithm=algorithm,
                    run_dir=run_dir,
                    kvm_config=kvm_config,
                    namespace=namespace,
                    destroy_cluster=destroy_cluster,
                    extra_args=args.extra_arg,
                )
                outer_log = Path(f"{run_dir}_outer.log")
                record = {
                    "scale": scale,
                    "vmCount": vm_count,
                    "algorithm": algorithm,
                    "namespace": namespace,
                    "runDir": str(run_dir),
                    "outerLog": str(outer_log),
                    "kvmConfig": str(kvm_config),
                    "destroyCluster": destroy_cluster,
                    "skipClusterBuild": not destroy_cluster,
                    "command": cmd,
                    "environment": DEFAULT_ENV,
                    "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                }

                print(f"[matrix] start scale={scale} vm={vm_count} algorithm={algorithm}")
                print(f"[matrix] run_dir={run_dir}")
                if args.dry_run:
                    print(" ".join(cmd))
                    first_run_for_vm = False
                    continue

                write_matrix_record(run_dir, record)

                with outer_log.open("w", encoding="utf-8") as log:
                    rc = subprocess.run(cmd, cwd=SCRIPT_DIR, env=env, stdout=log, stderr=subprocess.STDOUT).returncode
                first_run_for_vm = False
                record["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                record["returnCode"] = rc
                write_matrix_record(run_dir, record)
                if rc != 0:
                    failure = {
                        "scale": scale,
                        "vmCount": vm_count,
                        "algorithm": algorithm,
                        "runDir": str(run_dir),
                        "returnCode": rc,
                    }
                    failures.append(failure)
                    print(f"[matrix] FAIL {failure}", file=sys.stderr)
                    if not args.continue_on_failure:
                        return rc
                else:
                    print(f"[matrix] PASS scale={scale} vm={vm_count} algorithm={algorithm}")

    if failures:
        (results_root / f"matrix_failures_{now_ts()}.json").write_text(
            json.dumps(failures, indent=2), encoding="utf-8"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
