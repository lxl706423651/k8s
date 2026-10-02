#!/usr/bin/env python3
"""Measure one fixed Pod-to-Node mapping under declared per-Pod CPU loads.

All Pods and routing processes remain online while load changes.  Every load
level gets a routing/FIB gate, deterministic dataplane measurement and host
CPU/memory/fabric samples.  The script always stops its injected workers.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any

import yaml

import runPlacementExperiment as placement


SCRIPT_DIR = Path(__file__).resolve().parent
B62_DIR = SCRIPT_DIR.parent / "b62_k8s_scale"


def run(command: list[str], log: Path, timeout: float | None = None) -> int:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w") as handle:
        completed = subprocess.run(command, cwd=SCRIPT_DIR, stdout=handle,
                                   stderr=subprocess.STDOUT, timeout=timeout, check=False)
    return completed.returncode


def loadNodes(run_dir: Path, assignment: dict[str, Any]) -> list[placement.NodeInfo]:
    runtime = assignment["runtime"]
    args = placement.parseArgs([
        "--inventory", runtime["inventory"],
        "--config-k3s", runtime["configK3s"],
        "--kvm-config", str(run_dir / "cluster-input.yaml"),
    ])
    return placement.loadNodeInfo(args)


def measurementCommand(run_dir: Path, assignment: dict[str, Any], destination: Path,
                       options: dict[str, Any]) -> list[str]:
    experiment = assignment["experiment"]
    runtime = assignment["runtime"]
    return [
        sys.executable, str(SCRIPT_DIR / "running/measureDataplane.py"),
        "--namespace", experiment["namespace"],
        "--kubeconfig", runtime["kubeconfig"],
        "--topology-size", str(experiment["topologySize"]),
        "--topology-dir", experiment["topologyDir"],
        "--artifact-dir", str(destination),
        "--pair-plan-version", str(options.get("pairPlanVersion", 2)),
        "--pair-seed", str(options.get("pairSeed", 20260918)),
        "--pairs-per-stratum", str(options.get("pairsPerStratum", 12)),
        "--max-pairs-total", str(options.get("maxPairsTotal", 36)),
        "--ping-count", str(options.get("pingCount", 5)),
        "--ping-concurrency", str(options.get("pingConcurrency", 8)),
        "--baseline-tcp-directions", str(options.get("baselineTcpDirections", 16)),
        "--baseline-tcp-duration", str(options.get("baselineTcpDuration", 4)),
        "--baseline-tcp-concurrency", str(options.get("baselineTcpConcurrency", 4)),
        "--stress-levels", str(options.get("stressLevels", "8,16,32")),
        "--stress-repetitions", str(options.get("stressRepetitions", 1)),
        "--stress-duration", str(options.get("stressDuration", 8)),
        "--loaded-ping-canaries", str(options.get("loadedPingCanaries", 8)),
        "--loaded-ping-count", str(options.get("loadedPingCount", 8)),
    ]


def compactMetrics(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    return {
        "metadata": data.get("metadata"),
        "ping": data.get("ping"),
        "baselineTcpIperf": data.get("baselineTcpIperf"),
        "stress": data.get("stress"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    run_dir = args.run_dir.expanduser().resolve()
    assignment = yaml.safe_load((run_dir / "assignment.yaml").read_text())
    study = assignment.get("study") or {}
    levels = [float(item) for item in study.get("loadSweepCpuMillicores", [0, 10, 20, 40])]
    if not levels or any(item < 0 or item > 1000 for item in levels):
        parser.error("study.loadSweepCpuMillicores must contain values in 0..1000")
    settle = float(study.get("loadSettleSeconds", 20))
    options = study.get("dataplane") or {}
    output = run_dir / "load_sweep"
    output.mkdir(parents=True, exist_ok=True)
    nodes = loadNodes(run_dir, assignment)
    runtime = assignment["runtime"]
    namespace = assignment["experiment"]["namespace"]
    records = []
    status = "PASS"
    try:
        for level in levels:
            label = f"load_{level:g}m".replace(".", "p")
            level_dir = output / label
            level_dir.mkdir()
            apply_command = [
                sys.executable, str(SCRIPT_DIR / "applyPodCpuLoadNodeLocal.py"),
                "--kubeconfig", runtime["kubeconfig"], "--namespace", namespace,
                "--inventory", runtime["inventory"], "--cpu-millicores", str(level),
                "--node-concurrency", str(study.get("loadApplyNodeConcurrency", 0)),
                "--node-timeout-seconds", str(study.get("loadApplyNodeTimeoutSeconds", 900)),
                "--retries", str(study.get("loadApplyRetries", 3)),
                "--retry-backoff-seconds", str(study.get("loadApplyRetryBackoffSeconds", 2)),
                "--output", str(level_dir / "load_application.json"),
            ]
            record: dict[str, Any] = {"cpuMillicoresPerPod": level, "directory": str(level_dir)}
            apply_sampler = placement.DynamicSampler(
                nodes, float(study.get("hostSampleIntervalSeconds", 2)))
            apply_sampler.startSampler()
            try:
                record["loadExitCode"] = run(
                    apply_command, level_dir / "load_application.log", timeout=3600)
            finally:
                apply_sampler.stopSampler()
            record["loadApplicationHostMetrics"] = apply_sampler.summarizeSamples()
            (level_dir / "load_application_host_metrics.json").write_text(
                json.dumps(record["loadApplicationHostMetrics"], indent=2) + "\n")
            if record["loadExitCode"]:
                status = "FAIL"
                records.append(record)
                (output / "summary.partial.json").write_text(
                    json.dumps({"status": status, "levels": records}, indent=2) + "\n")
                break
            time.sleep(settle)
            route_commands = [
                ("bird", ["bash", str(B62_DIR / "verifyStartBird.sh"), str(run_dir)]),
                ("fib", ["bash", str(B62_DIR / "verifyFibRoutes.sh"), str(run_dir)]),
            ]
            record["routingGates"] = {}
            for name, command in route_commands:
                code = run(command, level_dir / f"verify_{name}.log", timeout=3600)
                record["routingGates"][name] = {"exitCode": code, "status": "PASS" if code == 0 else "FAIL"}
                prefix = "verify_after_start_bird" if name == "bird" else "verify_after_fib_write"
                copied = []
                for source in run_dir.glob(f"{prefix}*"):
                    if source.is_file():
                        target = level_dir / source.name
                        shutil.copy2(source, target)
                        copied.append(str(target))
                record["routingGates"][name]["artifacts"] = copied
                if code:
                    status = "FAIL"
                    break
            if status == "FAIL":
                records.append(record)
                break
            sampler = placement.DynamicSampler(nodes, float(study.get("hostSampleIntervalSeconds", 2)))
            sampler.startSampler()
            command = measurementCommand(run_dir, assignment, level_dir, options)
            try:
                record["dataplaneExitCode"] = run(command, level_dir / "dataplane.log", timeout=7200)
            finally:
                sampler.stopSampler()
            record["hostMetrics"] = sampler.summarizeSamples()
            host_path = level_dir / "host_metrics.json"
            host_path.write_text(json.dumps(record["hostMetrics"], indent=2) + "\n")
            summary_path = level_dir / "dataplane/latency_throughput_summary.json"
            if record["dataplaneExitCode"] or not summary_path.is_file():
                status = "FAIL"
            else:
                record["metrics"] = compactMetrics(summary_path)
            records.append(record)
            (output / "summary.partial.json").write_text(json.dumps({"status": status, "levels": records}, indent=2) + "\n")
            if status == "FAIL":
                break
    finally:
        stop_command = [
            sys.executable, str(SCRIPT_DIR / "applyPodCpuLoadNodeLocal.py"),
            "--kubeconfig", runtime["kubeconfig"], "--namespace", namespace,
            "--inventory", runtime["inventory"], "--cpu-millicores", "0",
            "--node-concurrency", str(study.get("loadApplyNodeConcurrency", 0)),
            "--node-timeout-seconds", str(study.get("loadApplyNodeTimeoutSeconds", 900)),
            "--retries", str(study.get("loadApplyRetries", 3)),
            "--retry-backoff-seconds", str(study.get("loadApplyRetryBackoffSeconds", 2)),
            "--output", str(output / "final_stop.json"),
        ]
        stop_code = run(stop_command, output / "final_stop.log", timeout=3600)
        if stop_code:
            status = "FAIL"
    summary = {
        "schemaVersion": 1,
        "createdAt": datetime.now().astimezone().isoformat(),
        "status": status,
        "topologySize": assignment["experiment"]["topologySize"],
        "algorithm": (assignment.get("placement") or {}).get("algorithm"),
        "allPodsRemainOnline": True,
        "loadImplementation": "in-container Python duty-cycle worker; no Pod restart or migration",
        "levels": records,
        "finalStopExitCode": stop_code,
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(f"load sweep {status}: {len(records)}/{len(levels)} levels")
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
