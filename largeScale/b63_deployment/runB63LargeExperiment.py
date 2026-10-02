#!/usr/bin/env python3
"""Run B62's workflow with checked B63 placement and final RTT/TCP/stress measurements.

Input: one compact KVM/experiment YAML, plus an optional run directory.
Output: run-local cluster state, manifests, placement and routing reports.
Creates isolated KVM/K3s resources, preloads images, deploys the topology and
runs ping/iperf stress tests unless --skipMeasureDataplane is supplied;
--prepare-only writes configuration snapshots without creating resources.
Run from any directory; B62 is imported from the adjacent example directory.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from functools import partial
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
B62_DIR = SCRIPT_DIR.parent / "b62_k8s_scale"
sys.path.insert(0, str(B62_DIR))
import runFullExperiment as shared
import runPlacementExperiment as placement

# These are algorithm options only. Infrastructure/deploy options belong to B62.
PLACEMENT_OPTIONS = {
    "algorithm": "optimized", "networkCostMode": "ratio", "alpha": 1.0,
    "beta": 1.0, "podBalanceWeight": 8.0, "vlanParentBalanceWeight": 4.0,
    "podCapSlack": 1.0, "vlanParentCapSlack": 1.5,
    "capacityBalanceRatio": 1.0, "resourceOverloadPenalty": 0.0,
    "capacityHardCapPenalty": 100000.0, "improvementPasses": 0,
    "ixWeight": 10.0, "peerWeight": 5.0, "intraWeight": 2.0,
    "hostWeight": 0.5,
    "externalCpuMillicores": 0.0, "resourceTargetUtilization": 0.70,
    "admissionMaxPods": 4000, "placementSeed": 20260918,
    "performanceModel": "performance_model.json",
    "staticContentionWeight": 1.0, "contentionKnee": 0.70,
}


def createPlacementArgs(config: dict) -> list[str]:
    """Translate config's placement mapping into the static algorithm CLI."""
    import re
    options = config.get("placement", {})
    allowed = set(PLACEMENT_OPTIONS) | {
        "excludeControlPlane", "nodePodReserve", "fastGreedyScore", "selectedOnly",
        "injectResourceRequests", "pinningMode",
    }
    unknown = set(options) - allowed
    if unknown:
        raise ValueError(f"Unknown placement fields: {sorted(unknown)}")
    for key in ("excludeControlPlane", "fastGreedyScore", "selectedOnly", "injectResourceRequests"):
        if key in options and type(options[key]) is not bool:
            raise ValueError(f"placement.{key} must be a YAML boolean")
    pinning_mode = options.get("pinningMode", "node-selector")
    if pinning_mode not in {"node-selector", "node-name"}:
        raise ValueError("placement.pinningMode must be node-selector or node-name")
    result = ["--pinning-mode", pinning_mode]
    for key, default in PLACEMENT_OPTIONS.items():
        flag = re.sub(r"([A-Z])", r"-\1", key).lower()
        value = options.get(key, default)
        if key == "admissionMaxPods" and key not in options:
            value = (config.get("k3s") or {}).get("maxPods", default)
        result.extend([f"--{flag}", str(value)])
    result.append("--exclude-master" if options.get("excludeControlPlane", False) else "--include-master")
    for key, flag, default in (
        ("fastGreedyScore", "--fast-greedy-score", True),
        ("selectedOnly", "--selected-only", True),
        ("injectResourceRequests", "--inject-resource-requests", False),
    ):
        if options.get(key, default):
            result.append(flag)
    placement.parseArgs(result)  # Validate enums/numbers before building VMs.
    return result


def applyPlacement(summary: dict, summary_path: Path, run_dir: Path, env: dict) -> None:
    """Replace compiled placement using this run's frozen YAML and cluster state."""
    config = shared.loadYaml(run_dir / "assignment.yaml")
    output = run_dir / "output"
    manifest = output / "k8s.yaml"
    # Keep the shared by-AS placement as a reproducible baseline.
    shutil.copy2(manifest, output / "k8s.b62.yaml")
    cmd = [
        sys.executable, str(SCRIPT_DIR / "runPlacementExperiment.py"),
        "--output-dir", str(output), "--input-manifest", str(output / "k8s.b62.yaml"),
        "--source-manifest", str(output / "k8s.source.yaml"),
        "--deploy-manifest", str(manifest),
        "--network-weights", str(output / "network_weights.yaml"),
        "--report", str(output / "placement_report.json"),
        "--kvm-config", env["B62_CONFIG_KVM_PATH"],
        "--config-k3s", env["B62_CONFIG_K3S_PATH"],
        "--inventory", env["B62_INVENTORY_PATH"],
        "--kubeconfig", env["B62_KUBECONFIG_PATH"],
        "--require-live-admission",
        *createPlacementArgs(config),
    ]
    shared.runStage("placement", cmd, summary=summary, summary_path=summary_path,
                    log_path=run_dir / "placement.orchestrator.log", env=env)
    report = json.loads((output / "placement_report.json").read_text())
    algorithm = report["metadata"]["selectedAlgorithm"]
    summary["baselinePlacement"] = summary.get("staticPlacement")
    summary["staticPlacement"] = {
        "recorded": True, "method": algorithm,
        "placementKind": report["metadata"].get("placementKind", "hard-placement"),
        "artifact": str(output / "placement_report.json"),
        "validation": report["staticValidation"][algorithm],
    }
    shared.writeJson(summary_path, summary)


def verifyPlacement(summary: dict, summary_path: Path, run_dir: Path, env: dict) -> None:
    """Run the required live Pod-to-node check before starting BIRD."""
    shared.runVerificationStage(
        "verify-placement",
        [sys.executable, str(SCRIPT_DIR / "verifyPlacement.py"),
         "--run-dir", str(run_dir), "--kubeconfig", env["B62_KUBECONFIG_PATH"],
         "--namespace", env["SEED_NAMESPACE"]],
        summary=summary, summary_path=summary_path,
        log_path=run_dir / "verify-placement.orchestrator.log", env=env,
        test_key="placement", test_filename="placement_actual_summary.json", run_dir=run_dir,
    )


def verifyStaticPlacement(summary: dict, summary_path: Path, run_dir: Path, env: dict) -> None:
    """Validate saved manifests/metrics for this run before preload or build resume."""
    shared.runVerificationStage(
        "verify-static-placement",
        [sys.executable, str(SCRIPT_DIR / "verifyStaticPlacement.py"),
         "--run-dir", str(run_dir)],
        summary=summary, summary_path=summary_path,
        log_path=run_dir / "verify-static-placement.orchestrator.log", env=env,
        test_key="staticPlacement", test_filename="placement_static_summary.json",
        run_dir=run_dir,
    )


def measureDataplane(summary: dict, summary_path: Path, run_dir: Path, env: dict,
                     *, skip: bool = False) -> None:
    """Measure the live run using its resolved env; skip records an explicit stage.

    summary/summary_path track stage status and results; run_dir owns artifacts.
    Nonzero script exits fail the workflow. Per-flow failures remain measurements
    in the report, matching measureDataplane.py's existing semantics.
    """
    if skip:
        summary["stages"].append({
            "name": "dataplane", "status": "SKIPPED", "durationSeconds": 0,
            "exitCode": 0, "reason": "--skipMeasureDataplane",
        })
        shared.writeJson(summary_path, summary)
        return
    config = shared.loadYaml(run_dir / "assignment.yaml")
    if (config.get("study") or {}).get("loadSweepCpuMillicores") is not None:
        shared.runVerificationStage(
            "dataplane-load-sweep",
            [sys.executable, str(SCRIPT_DIR / "runLoadSweep.py"), "--run-dir", str(run_dir)],
            summary=summary, summary_path=summary_path,
            log_path=run_dir / "dataplane-load-sweep.orchestrator.log", env=env,
            test_key="performanceStudy", test_filename="load_sweep/summary.json",
            run_dir=run_dir,
        )
        return
    shared.runVerificationStage(
        "dataplane",
        [sys.executable, str(SCRIPT_DIR / "running/measureDataplane.py"),
         "--namespace", env["SEED_NAMESPACE"],
         "--kubeconfig", env["B62_KUBECONFIG_PATH"],
         "--topology-size", env["SEED_TOPOLOGY_SIZE"],
         "--topology-dir", env["SEED_REAL_TOPOLOGY_DIR"],
         "--artifact-dir", str(run_dir),
         "--pair-plan-version", "2", "--pair-seed", "20260624",
         "--pairs-per-stratum", "32", "--max-pairs-total", "96",
         "--ping-count", "10", "--ping-concurrency", "8",
         "--baseline-tcp-directions", "48", "--baseline-tcp-duration", "8",
         "--baseline-tcp-concurrency", "4",
         "--stress-levels", "8,16,32,48", "--stress-repetitions", "3",
         "--stress-duration", "15", "--loaded-ping-canaries", "16",
         "--loaded-ping-count", "15"],
        summary=summary, summary_path=summary_path,
        log_path=run_dir / "dataplane.orchestrator.log", env=env,
        test_key="dataplane", test_filename="dataplane/latency_throughput_summary.json",
        run_dir=run_dir,
    )


def main(argv: list[str] | None = None) -> int:
    """Delegate cluster and workload stages to B62, using only B63 callbacks."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", "--kvm-config", type=Path,
                        default=SCRIPT_DIR / "configSingleHostKvmMacvlan.yaml")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--skipMeasureDataplane", action="store_true",
                        help="skip the final RTT/TCP and 8/16/32/48-flow stress measurements")
    parser.add_argument("--resume-from", choices=("build", "start-kernel", "reconvergence"))
    args = parser.parse_args(argv)
    if args.resume_from and args.run_dir is None:
        parser.error("--resume-from requires --run-dir")
    config_path = (args.run_dir.expanduser().resolve() / "assignment.yaml" if args.resume_from
                   else args.config.expanduser().resolve())
    config = shared.loadYaml(config_path)
    createPlacementArgs(config)  # Reject unsupported options before VM creation.
    size = config["experiment"]["topologySize"]
    run_dir = (args.run_dir.expanduser().resolve() if args.run_dir else
               SCRIPT_DIR / "experiments" / f"{size}_{time.strftime('%Y%m%d_%H%M%S')}_shared")
    if args.resume_from == "build" and not (run_dir / "output/placement_report.json").is_file():
        parser.error("Resuming B63 build requires an existing placement report")
    argv = ["--config", str(config_path), "--run-dir", str(run_dir),
            "--config-k3s", str(run_dir / "configK3s.yaml"),
            "--kubeconfig", str(run_dir / "kubeconfig.yaml"),
            "--inventory", str(run_dir / "inventory.yaml"),
            "--skip-existing-destroy"]
    if args.prepare_only:
        argv.append("--prepare-only")
    if args.resume_from:
        argv.extend(["--resume-from", args.resume_from])
    return shared.main(argv, after_compile=applyPlacement, after_ready=verifyPlacement,
                       before_build=verifyStaticPlacement,
                       after_workload=partial(measureDataplane, skip=args.skipMeasureDataplane))


if __name__ == "__main__":
    raise SystemExit(main())
