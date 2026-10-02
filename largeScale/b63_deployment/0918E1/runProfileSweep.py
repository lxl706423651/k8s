#!/usr/bin/env python3
"""Run a sequence of VM resource profiles for the 0918E1 placement model.

Inputs: profileSweep.yaml, experiment.yaml, existing builds, and reachable experiment VMs.
Outputs: one result run per executed profile plus profile_sweep_summary.json.
Side effects: reconfigures only the declared experiment VMs, runs executeVmMatrix.py, and analyzes results.
Context: host-side orchestration after topology compilation and VM provisioning are complete.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml


SCRIPT_DIR = Path(__file__).resolve().parent


def parseArgs() -> argparse.Namespace:
    """Parse profile selection and run controls."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=SCRIPT_DIR / "experiment.yaml")
    parser.add_argument("--profiles", type=Path, default=SCRIPT_DIR / "profileSweep.yaml")
    parser.add_argument("--run-prefix", default=None)
    parser.add_argument("--only-profile", action="append")
    parser.add_argument("--compile-first", action="store_true")
    parser.add_argument(
        "--rerun-existing",
        action="store_true",
        help="ignore profile existingRun entries and execute them with the declared resources",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def runCommand(command: list[str], *, check: bool = False, dryRun: bool = False) -> subprocess.CompletedProcess[str]:
    """Print and execute one sweep command."""
    print("+ " + shlex.join(command), flush=True)
    if dryRun:
        return subprocess.CompletedProcess(command, 0, "", "")
    return subprocess.run(command, text=True, capture_output=False, check=check)


def loadSweep(path: Path) -> dict[str, Any]:
    """Load the YAML profile plan."""
    data = yaml.safe_load(path.expanduser().read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("profiles"), list):
        raise ValueError("profile sweep YAML must contain profiles: [...]")
    return data


def analyzeRun(runDir: Path, dryRun: bool) -> int:
    """Regenerate analyzer artifacts even when a matrix has failed scales."""
    result = runCommand(
        ["python3", str(SCRIPT_DIR / "analyzeVmMatrix.py"), "--run-dir", str(runDir)],
        dryRun=dryRun,
    )
    return result.returncode


def main() -> int:
    """Apply each profile, run the matrix, and record every outcome."""
    args = parseArgs()
    sweep = loadSweep(args.profiles)
    selected = set(args.only_profile or [profile["id"] for profile in sweep["profiles"]])
    profiles = [profile for profile in sweep["profiles"] if profile["id"] in selected]
    if not profiles:
        raise ValueError("no selected profiles")

    if args.compile_first:
        runCommand(
            [
                "python3",
                str(SCRIPT_DIR / "executeVmMatrix.py"),
                "--config",
                str(args.config),
                "--compile-only",
                "--compile-workers",
                "4",
            ],
            check=True,
            dryRun=args.dry_run,
        )

    runPrefix = args.run_prefix or time.strftime("run-%Y%m%d-profile")
    summary: dict[str, Any] = {
        "schemaVersion": 1,
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "runPrefix": runPrefix,
        "profiles": [],
    }
    summaryPath = SCRIPT_DIR / "results" / f"{runPrefix}-profile_sweep_summary.json"
    summaryPath.parent.mkdir(parents=True, exist_ok=True)
    sink = sweep.get("sinkProfile") or {}

    for profile in profiles:
        profileId = str(profile["id"])
        source = profile["source"]
        record: dict[str, Any] = {
            "id": profileId,
            "label": profile.get("label", profileId),
            "source": source,
            "sink": sink,
            "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        existingRun = profile.get("existingRun")
        if existingRun and not args.rerun_existing:
            runDir = (SCRIPT_DIR / str(existingRun)).resolve()
            record.update({"runId": runDir.name, "runDir": str(runDir), "status": "EXISTING"})
            record["analysisReturnCode"] = analyzeRun(runDir, args.dry_run)
        else:
            apply = [
                "python3",
                str(SCRIPT_DIR / "applyVmResources.py"),
                "--config",
                str(args.config),
                "--source-vcpus",
                str(int(source["vcpus"])),
                "--source-memory-gib",
                str(int(source["memoryGiB"])),
                "--sink-vcpus",
                str(int(sink.get("vcpus", source["vcpus"]))),
                "--sink-memory-gib",
                str(int(sink.get("memoryGiB", source["memoryGiB"]))),
                "--force-shutdown",
            ]
            applyResult = runCommand(apply, dryRun=args.dry_run)
            record["applyReturnCode"] = applyResult.returncode
            runId = f"{runPrefix}-{profileId}"
            execute = [
                "python3",
                str(SCRIPT_DIR / "executeVmMatrix.py"),
                "--config",
                str(args.config),
                "--run-only",
                "--skip-build",
                "--run-id",
                runId,
            ]
            executeResult = runCommand(execute, dryRun=args.dry_run)
            record["runId"] = runId
            record["executeReturnCode"] = executeResult.returncode
            runDir = SCRIPT_DIR / "results" / runId
            record["runDir"] = str(runDir)
            if runDir.exists():
                record["analysisReturnCode"] = analyzeRun(runDir, args.dry_run)
            record["status"] = "PASS" if executeResult.returncode == 0 else "PARTIAL_OR_FAIL"
        record["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        summary["profiles"].append(record)
        summaryPath.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    summary["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    summaryPath.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": "PASS", "summary": str(summaryPath)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
