#!/usr/bin/env python3
"""Run a frozen E1 or E3 B63 performance-placement study serially.

Each variant receives a fresh cluster built from the same source configuration.
Only the declared placement method/options change.  The study may clean a
failed variant and continue when the frozen protocol requests independent
failure-tolerant comparison; all failure artifacts and exit codes remain in the
study directory.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time
from typing import Any

import yaml

import runB63LargeExperiment as b63


SCRIPT_DIR = Path(__file__).resolve().parent
RESEARCH_ALGORITHMS = {"locality-only", "capacity-only", "tradeoff-static", "crescent",
                       "crescent-resource-aware", "performance-model"}
VARIANT_PLACEMENT_FIELDS = {"staticContentionWeight", "contentionKnee", "externalCpuMillicores",
                            "resourceTargetUtilization", "placementSeed", "performanceModel"}
PROTOCOL_IMPLEMENTATIONS = [
    "runPerformanceStudy.py",
    "runB63LargeExperiment.py",
    "runPlacementExperiment.py",
    "researchPlacement.py",
    "runLoadSweep.py",
    "applyPodCpuLoad.py",
    "applyPodCpuLoadNodeLocal.py",
    "performanceStudy.py",
    "analyzePerformanceStudy.py",
    "fitPerformanceModel.py",
    "running/measureDataplane.py",
    "../b62_k8s_scale/verifyStartBird.py",
    "../b62_k8s_scale/verifyFibRoutes.py",
    "../b62_k8s_scale/validateCrossHostWorkload.py",
    "../b62_k8s_scale/runFullExperiment.py",
    "../b62_k8s_scale/lib.sh",
    "../b62_k8s_scale/compile.sh",
    "../b62_k8s_scale/build.sh",
    "../b62_k8s_scale/deploy.sh",
    "../b62_k8s_scale/wait-ready.sh",
    "../b62_k8s_scale/start_bird.sh",
    "../b62_k8s_scale/start_bird_node_local.py",
    "../b62_k8s_scale/start_bird_kernel.sh",
    "../b62_k8s_scale/start_bird_kernel_node_local.py",
    "../../seedemu/compiler/kubernetes.py",
]


def execute(command: list[str], log: Path, *, env: dict[str, str] | None = None) -> int:
    with log.open("w") as handle:
        return subprocess.run(command, cwd=SCRIPT_DIR, stdout=handle,
                              stderr=subprocess.STDOUT, check=False, env=env).returncode


def writeJson(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def implementationHashes() -> dict[str, str]:
    """Freeze every implementation that can affect placement or measurement."""
    hashes: dict[str, str] = {}
    for relative in PROTOCOL_IMPLEMENTATIONS:
        path = (SCRIPT_DIR / relative).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"protocol implementation is missing: {path}")
        hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def snapshotImplementations(root: Path, hashes: dict[str, str]) -> dict[str, Any]:
    """Copy the exact frozen implementation into the study directory."""
    sources = [(relative, (SCRIPT_DIR / relative).resolve())
               for relative in PROTOCOL_IMPLEMENTATIONS]
    common_root = Path(os.path.commonpath([str(path) for _, path in sources]))
    snapshot_root = root / "implementation_snapshot"
    files: dict[str, Any] = {}
    for relative, source in sources:
        target = snapshot_root / source.relative_to(common_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        if copied_hash != hashes[relative]:
            raise RuntimeError(f"implementation changed while freezing protocol: {source}")
        files[relative] = {
            "source": str(source),
            "snapshot": str(target.relative_to(root)),
            "sha256": copied_hash,
        }
    manifest = {"commonSourceRoot": str(common_root), "files": files}
    writeJson(snapshot_root / "manifest.json", manifest)
    return manifest


def hostEnvironment(config: dict[str, Any]) -> dict[str, Any]:
    """Capture host capacity and pre-existing libvirt resources without mutation."""
    def lines(command: list[str]) -> dict[str, Any]:
        try:
            completed = subprocess.run(command, text=True, capture_output=True,
                                       timeout=30, check=False)
            return {"exitCode": completed.returncode,
                    "lines": [line for line in completed.stdout.splitlines() if line.strip()],
                    "stderr": completed.stderr.strip()}
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"exitCode": None, "lines": [], "stderr": str(exc)}

    memory: dict[str, int] = {}
    try:
        for raw in Path("/proc/meminfo").read_text().splitlines():
            key, value = raw.split(":", 1)
            if key in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}:
                memory[key + "KiB"] = int(value.split()[0])
    except (OSError, ValueError, IndexError):
        pass
    cpu_model = None
    try:
        for raw in Path("/proc/cpuinfo").read_text().splitlines():
            if raw.startswith("model name"):
                cpu_model = raw.split(":", 1)[1].strip()
                break
    except (OSError, ValueError, IndexError):
        pass
    storage_path = Path(str((config.get("kvm") or {}).get("storageDir") or "/"))
    probe = storage_path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    usage = shutil.disk_usage(probe)
    return {
        "capturedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "hostname": platform.node(), "kernel": platform.release(),
        "cpuLogicalCount": os.cpu_count(), "cpuModel": cpu_model,
        "loadAverage": list(os.getloadavg()), "memory": memory,
        "storage": {"configuredPath": str(storage_path), "probePath": str(probe),
                    "totalBytes": usage.total, "usedBytes": usage.used,
                    "freeBytes": usage.free},
        "libvirtDomains": lines(["virsh", "list", "--all", "--name"]),
        "libvirtNetworks": lines(["virsh", "net-list", "--all", "--name"]),
    }


def variantList(config: dict[str, Any]) -> list[dict[str, Any]]:
    study = config.get("study") or {}
    values = study.get("variants")
    if not isinstance(values, list) or not values:
        raise ValueError("study.variants must be a non-empty list")
    result = []
    ids = set()
    for raw in values:
        if not isinstance(raw, dict):
            raise ValueError("each study variant must be a mapping")
        item = copy.deepcopy(raw)
        identifier = str(item.get("id") or "")
        algorithm = str(item.get("algorithm") or "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", identifier) or identifier in ids:
            raise ValueError(f"invalid or duplicate study variant id: {identifier!r}")
        if algorithm not in RESEARCH_ALGORITHMS:
            raise ValueError(f"unsupported research algorithm in {identifier}: {algorithm}")
        overrides = item.get("placement") or {}
        unknown = set(overrides) - VARIANT_PLACEMENT_FIELDS
        if unknown:
            raise ValueError(f"variant {identifier} has unsupported placement fields: {sorted(unknown)}")
        ids.add(identifier)
        result.append({"id": identifier, "algorithm": algorithm, "placement": overrides})
    return result


def freezeConfig(source_config: dict[str, Any], source: Path, variant: dict[str, Any]) -> dict[str, Any]:
    frozen = b63.shared.resolveClusterInputPaths(copy.deepcopy(source_config), source)
    adapted = b63.shared.createSingleConfigAssignment(source_config, source)
    for key in ("sourceRoot", "topologyDir", "canonicalOutputDir"):
        frozen["experiment"][key] = adapted["experiment"][key]
    placement = frozen.setdefault("placement", {})
    placement["algorithm"] = variant["algorithm"]
    placement["selectedOnly"] = True
    placement["injectResourceRequests"] = False
    placement.update(copy.deepcopy(variant["placement"]))
    if placement.get("performanceModel"):
        model = Path(str(placement["performanceModel"])).expanduser()
        if not model.is_absolute():
            placement["performanceModel"] = str((source.parent / model).resolve())
    b63.createPlacementArgs(frozen)
    return frozen


def validateStudySplit(config: dict[str, Any], source: Path, phase: str,
                       variants: list[dict[str, Any]]) -> None:
    """Reject calibration leakage before any run directory or VM is created."""
    study = config.get("study") or {}
    sweep = {float(value) for value in study.get("loadSweepCpuMillicores") or []}
    calibration = {float(value) for value in study.get("calibrationLoadsMillicores") or []}
    holdout = {float(value) for value in study.get("holdoutLoadsMillicores") or []}
    if phase == "E3" and calibration:
        raise ValueError("E3 cannot contain calibration loads")
    if calibration & holdout:
        raise ValueError("calibration and holdout load sets must be disjoint")
    if not (calibration | holdout) <= sweep:
        raise ValueError("calibration and holdout loads must belong to the configured load sweep")
    if phase == "E1" and (not calibration or not holdout):
        raise ValueError("E1 requires non-empty calibration and holdout load sets")
    if phase != "E3":
        return
    if holdout != sweep:
        raise ValueError("every E3 load must be declared as holdout")
    evaluation_size = int((config.get("experiment") or {}).get("topologySize") or 0)
    for variant in variants:
        if variant["algorithm"] != "performance-model":
            continue
        placement = copy.deepcopy(config.get("placement") or {})
        placement.update(variant.get("placement") or {})
        raw_path = str(placement.get("performanceModel") or "").strip()
        if not raw_path:
            raise ValueError("E3 performance-model variant requires placement.performanceModel")
        model_path = Path(raw_path).expanduser()
        if not model_path.is_absolute():
            model_path = (source.parent / model_path).resolve()
        if not model_path.is_file():
            raise ValueError(f"E3 performance model does not exist: {model_path}")
        model = json.loads(model_path.read_text())
        training_size = int(model.get("trainingTopologySize") or 0)
        if not training_size or training_size == evaluation_size:
            raise ValueError(
                "E3 performance model must be trained on a different topology size: "
                f"training={training_size}, evaluation={evaluation_size}")


def analysisInterpreter() -> Path:
    """Use an available Python with Matplotlib, falling back to this runtime."""
    candidates = [Path(sys.executable), Path("/home/lxl/anaconda3/bin/python3"), Path("/usr/bin/python3")]
    seen: set[Path] = set()
    fallback = Path(sys.executable)
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate in seen or not candidate.is_file():
            continue
        seen.add(candidate)
        check = subprocess.run([str(candidate), "-c", "import matplotlib"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if check.returncode == 0:
            return candidate
        fallback = candidate if not fallback.is_file() else fallback
    return fallback


def analyze(root: Path) -> None:
    command = [str(analysisInterpreter()),
               str(SCRIPT_DIR / "analyzePerformanceStudy.py"), "--run-dir", str(root)]
    subprocess.run(command, cwd=SCRIPT_DIR, check=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--variant", help="run only one declared variant id")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--skipMeasureDataplane", action="store_true")
    args = parser.parse_args(argv)
    source = args.config.expanduser().resolve()
    config = b63.shared.loadYaml(source)
    try:
        variants = variantList(config)
    except ValueError as exc:
        parser.error(str(exc))
    if args.variant:
        variants = [item for item in variants if item["id"] == args.variant]
        if not variants:
            parser.error(f"variant not declared in config: {args.variant}")
    if config.get("kvm", {}).get("allowExisting", False):
        parser.error("performance studies require kvm.allowExisting: false")
    study = config.get("study") or {}
    phase = str(study.get("phase") or "")
    if phase not in {"E1", "E3"}:
        parser.error("study.phase must be E1 or E3")
    try:
        validateStudySplit(config, source, phase, variants)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    continue_after_failure = bool(study.get("continueAfterVariantFailure", False))
    cleanup_failed_variants = bool(study.get("cleanupFailedVariants", False))
    destroy_physical_timeout = int(study.get("destroyPhysicalTimeoutSeconds", 1800))
    if destroy_physical_timeout <= 0:
        parser.error("study.destroyPhysicalTimeoutSeconds must be positive")
    if continue_after_failure and not cleanup_failed_variants and not args.prepare_only:
        parser.error(
            "study.continueAfterVariantFailure requires study.cleanupFailedVariants: true "
            "so the next variant can receive a fresh cluster")
    root = (args.run_dir.expanduser().resolve() if args.run_dir else
            SCRIPT_DIR / "experiments" / f"{config['experiment']['topologySize']}_{phase.lower()}_{time.strftime('%Y%m%d_%H%M%S')}")
    if root.exists():
        parser.error(f"run directory already exists: {root}")
    root.mkdir(parents=True)
    (root / "source_input.yaml").write_text(source.read_text())
    host_before = hostEnvironment(config)
    writeJson(root / "host_environment_before.json", host_before)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    implementation_hashes = implementationHashes()
    implementation_snapshot = snapshotImplementations(root, implementation_hashes)
    protocol = {
        "phase": phase,
        "sourceConfigSha256": source_hash,
        "implementationSha256": implementation_hashes,
        "implementationSnapshot": implementation_snapshot,
        "hostEnvironmentBefore": host_before,
        "fixedTopologySize": config["experiment"]["topologySize"],
        "fixedNamespace": config["experiment"]["namespace"],
        "loadSweepCpuMillicores": study.get("loadSweepCpuMillicores"),
        "calibrationLoadsMillicores": study.get("calibrationLoadsMillicores", []),
        "holdoutLoadsMillicores": study.get("holdoutLoadsMillicores", []),
        "contentionKnee": (config.get("placement") or {}).get("contentionKnee", 0.70),
        "basePlacementConfiguration": copy.deepcopy(config.get("placement") or {}),
        "clusterResourceConfiguration": {
            "master": copy.deepcopy(config.get("master") or {}),
            "workers": copy.deepcopy(config.get("workers") or {}),
            "kubeletMaxPods": (config.get("k3s") or {}).get("maxPods"),
        },
        "controlPlaneCarriesWorkloadPods": not bool(
            (config.get("placement") or {}).get("excludeControlPlane", False)),
        "allContainersOnline": True, "staticPredeploymentPlacement": True,
        "onlineMigration": False, "kernelCniForwardingChangedBetweenVariants": False,
        "variantsFrozenBeforeDynamicMeasurement": variants,
        "continueAfterVariantFailure": continue_after_failure,
        "cleanupFailedVariants": cleanup_failed_variants,
        "destroyPhysicalTimeoutSeconds": destroy_physical_timeout,
    }
    progress: dict[str, Any] = {
        "schemaVersion": 1, "status": "PREPARING" if args.prepare_only else "RUNNING",
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "sourceConfig": str(source),
        "python": sys.executable, "protocol": protocol, "runs": [],
    }
    writeJson(root / "study_runs.json", progress)
    return_code = 0
    variant_failures: list[str] = []
    try:
        for variant in variants:
            run = root / variant["id"]
            run.mkdir()
            frozen = freezeConfig(config, source, variant)
            input_path = run / "input.yaml"
            input_path.write_text(yaml.safe_dump(frozen, sort_keys=False))
            command = [sys.executable, str(SCRIPT_DIR / "runB63LargeExperiment.py"),
                       "--config", str(input_path), "--run-dir", str(run)]
            if args.prepare_only:
                command.append("--prepare-only")
            if args.skipMeasureDataplane:
                command.append("--skipMeasureDataplane")
            row = {"id": variant["id"], "algorithm": variant["algorithm"], "runDir": str(run),
                   "placementOverrides": variant["placement"], "command": command,
                   "status": "RUNNING", "cleanupStatus": "NOT_REQUESTED"}
            progress["runs"].append(row)
            writeJson(root / "study_runs.json", progress)
            print(f"[{variant['id']}] starting; log={run / 'console.log'}", flush=True)
            code = execute(command, run / "console.log")
            row["exitCode"] = code
            summary = b63.shared.loadYaml(run / "summary.json") if (run / "summary.json").is_file() else {}
            expected = "PREPARED" if args.prepare_only else "PASS"
            row["status"] = expected if code == 0 and summary.get("status") == expected else "FAIL"
            writeJson(root / "study_runs.json", progress)
            analyze(root)
            failed = row["status"] == "FAIL"
            if failed:
                variant_failures.append(variant["id"])
            should_cleanup = (not args.prepare_only
                              and (not failed or cleanup_failed_variants))
            if should_cleanup:
                cleanup = [sys.executable, str(SCRIPT_DIR / "k8sTools.py"), "destroy",
                           "-d", str(run / "configK3s.yaml"), "--keep-temp"]
                row["cleanupStatus"] = "RUNNING"
                row["cleanupCommand"] = cleanup
                row["cleanupPhysicalTimeoutSeconds"] = destroy_physical_timeout
                writeJson(root / "study_runs.json", progress)
                cleanup_env = os.environ.copy()
                cleanup_env["SEED_K8S_DESTROY_PHYSICAL_TIMEOUT_SECONDS"] = str(
                    destroy_physical_timeout)
                row["cleanupExitCode"] = execute(
                    cleanup, run / "destroy.log", env=cleanup_env)
                row["cleanupStatus"] = "PASS" if row["cleanupExitCode"] == 0 else "FAIL"
                writeJson(root / "study_runs.json", progress)
                if row["cleanupStatus"] != "PASS":
                    raise RuntimeError(f"{variant['id']} cleanup failed; later variants not started")
            if failed and not continue_after_failure:
                retained = "retained" if not should_cleanup else "cleaned"
                raise RuntimeError(
                    f"{variant['id']} failed; cluster {retained}: {run / 'console.log'}")
        if phase == "E1" and not args.prepare_only:
            fit_command = [sys.executable, str(SCRIPT_DIR / "fitPerformanceModel.py"),
                           "--run-dir", str(root)]
            progress["modelFit"] = {"status": "RUNNING", "command": fit_command}
            writeJson(root / "study_runs.json", progress)
            fit_code = execute(fit_command, root / "model_fit.log")
            progress["modelFit"].update({"exitCode": fit_code,
                                         "status": "PASS" if fit_code == 0 else "FAIL"})
            writeJson(root / "study_runs.json", progress)
            if fit_code:
                raise RuntimeError(f"E1 model fit failed: {root / 'model_fit.log'}")
        if variant_failures:
            progress["status"] = "FAIL"
            progress["variantFailures"] = variant_failures
            progress["failure"] = (
                "All declared variants were attempted; failed variants: "
                + ", ".join(variant_failures))
            return_code = 1
        else:
            progress["status"] = "PREPARED" if args.prepare_only else "PASS"
    except KeyboardInterrupt:
        progress["status"] = "INTERRUPTED"
        progress["failure"] = "Interrupted; active cluster retained."
        return_code = 130
    except Exception as exc:  # noqa: BLE001 - preserve partial empirical evidence.
        progress["status"] = "FAIL"
        progress["failure"] = str(exc)
        print(str(exc), file=sys.stderr)
        return_code = 1
    finally:
        progress["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        writeJson(root / "host_environment_after.json", hostEnvironment(config))
        writeJson(root / "study_runs.json", progress)
        analyze(root)
    print(f"study artifacts: {root / 'study_report.md'}", flush=True)
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
