#!/usr/bin/env python3
"""Run a serial E1-A calibration matrix for heterogeneous VM profiles.

The runner generates isolated E1-A YAML files from the existing single-worker
configuration and invokes ``executeE1aExperiment.py`` one profile at a time.
It never destroys a cluster belonging to another profile or an unrelated
experiment.  By default the matrix is intentionally small and suitable for
calibration; use ``--phase all`` and larger repetitions for a paper-grade
run.
"""
from __future__ import annotations

import argparse
import csv
import json
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


HERE = Path(__file__).resolve().parent
BASE_CONFIG = HERE / "research_configs/configE1aMarginalPods.yaml"
E1A_RUNNER = HERE / "executeE1aExperiment.py"

DEFAULT_PROFILES: tuple[dict[str, Any], ...] = (
    {"id": "cpu-weak", "vcpus": 2, "memoryGiB": 8, "ipOctet": 94},
    {"id": "balanced", "vcpus": 4, "memoryGiB": 8, "ipOctet": 95},
    {"id": "cpu-strong", "vcpus": 8, "memoryGiB": 8, "ipOctet": 96},
    {"id": "memory-weak", "vcpus": 4, "memoryGiB": 4, "ipOctet": 97},
    {"id": "memory-strong", "vcpus": 4, "memoryGiB": 16, "ipOctet": 98},
)


def read_yaml(path: Path) -> dict[str, Any]:
    """Read one YAML mapping."""
    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise ValueError(f"expected YAML mapping in {path}")
    return value


def write_json(path: Path, value: Any) -> None:
    """Write stable JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def isolated_config(
    base: dict[str, Any],
    profile: dict[str, Any],
    output_dir: Path,
    *,
    phase: str,
    repetitions: int,
    measurement_seconds: int,
    warmup_seconds: int,
    loaded_n: list[int],
    idle_n: list[int],
) -> dict[str, Any]:
    """Build one profile config with unique VM/network/cluster coordinates."""
    result = json.loads(json.dumps(base))
    profile_id = str(profile["id"])
    ip_octet = int(profile["ipOctet"])
    cluster_name = f"seedemu-b63-vmprof-{profile_id}"
    result["clusterName"] = cluster_name
    result["defaults"].update(
        {
            "masterName": f"b63-vmp-{profile_id}-master",
            "workerNamePrefix": f"b63-vmp-{profile_id}-worker",
            "ipPrefix": f"10.{ip_octet}.0",
            "masterIpStart": 10,
            "workerIpStart": 11,
            "macPrefix": f"52:54:00:63:{ip_octet:02x}",
            "masterMacStart": 16,
            "workerMacStart": 17,
        }
    )
    result["master"] = {"vcpus": 2, "memoryGiB": 8, "diskGb": 32}
    result["workers"] = {
        "count": 1,
        "vcpus": int(profile["vcpus"]),
        "memoryGiB": int(profile["memoryGiB"]),
        "diskGb": 32,
    }
    result["kvm"].update(
        {
            "network": f"seedemu-b63-vmprof-{profile_id}",
            "networkBridge": f"virbrb63vp{ip_octet}",
            "networkCidr": f"10.{ip_octet}.0.0/24",
            "networkGateway": f"10.{ip_octet}.0.1",
            "dhcpStart": f"10.{ip_octet}.0.10",
            "dhcpEnd": f"10.{ip_octet}.0.99",
            "storageDir": f"/data/lxl/k8sTools/b63-vmprof-{profile_id}",
            "diskDir": f"/data/lxl/k8sTools/b63-vmprof-{profile_id}/disks",
            "cloudInitDir": f"/data/lxl/k8sTools/b63-vmprof-{profile_id}/cloud-init",
            "allowExisting": False,
        }
    )
    result["registry"] = {"port": 5200 + ip_octet}
    result["k3s"].update(
        {
            "clusterCidr": f"10.{100 + ip_octet}.0.0/16",
            "serviceCidr": f"10.{110 + ip_octet}.0.0/16",
        }
    )
    e1a = result["e1a"]
    e1a["namespacePrefix"] = f"seedemu-b63-vmprof-{profile_id}"
    e1a["phaseSeed"] = 20260920 + ip_octet
    e1a["pilot"].update(
        {
            "warmupSeconds": warmup_seconds,
            "measurementSeconds": measurement_seconds,
            "pointStartLeadSeconds": 5,
            "pointReadyTimeoutSeconds": 600,
            "loadedN": loaded_n,
            "idleN": idle_n,
        }
    )
    e1a["formal"].update(
        {
            "warmupSeconds": max(warmup_seconds, 10),
            "measurementSeconds": max(measurement_seconds, 20),
            "pointStartLeadSeconds": 5,
            "pointReadyTimeoutSeconds": 900,
            "repetitions": repetitions,
            "parallelism": 1,
            "coarseN": loaded_n,
            "fineRadiusPods": 0,
            "fineStepPods": 10,
            "randomSeed": 20260920 + ip_octet,
        }
    )
    # The profile matrix is serial and deliberately unpinned by default.  A
    # caller can add a profile-specific pinning section after checking current
    # virsh CPU affinities; this runner must not guess and collide with a
    # running experiment.
    e1a["pinning"] = {"enabled": False}
    e1a["cleanup"]["destroyClusterAfterRun"] = True
    result["_profile"] = {
        "id": profile_id,
        "vcpus": int(profile["vcpus"]),
        "memoryGiB": int(profile["memoryGiB"]),
        "phase": phase,
    }
    return result


def run_command(command: list[str], log_path: Path) -> int:
    """Run one child process and preserve merged output."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as handle:
        handle.write("$ " + shlex.join(command) + "\n")
        handle.flush()
        result = subprocess.run(command, cwd=HERE, stdout=handle, stderr=subprocess.STDOUT, check=False)
    return result.returncode


def write_summary(output_dir: Path, rows: list[dict[str, Any]]) -> None:
    """Write a compact matrix summary."""
    write_json(output_dir / "calibration_summary.json", {"profiles": rows})
    fields = sorted({key for row in rows for key in row})
    with (output_dir / "calibration_summary.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def parse_arguments() -> argparse.Namespace:
    """Parse calibration options."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-config", type=Path, default=BASE_CONFIG)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=HERE / "experiments/e1_marginal_pods/20260920_vm_profile_calibration",
    )
    parser.add_argument("--profiles", nargs="+", default=[profile["id"] for profile in DEFAULT_PROFILES])
    parser.add_argument("--phase", choices=("pilot", "formal", "all"), default="pilot")
    parser.add_argument("--repetitions", type=int, default=2)
    parser.add_argument("--measurement-seconds", type=int, default=15)
    parser.add_argument("--warmup-seconds", type=int, default=5)
    parser.add_argument("--loaded-n", nargs="+", type=int, default=[0, 20, 40, 60, 80])
    parser.add_argument("--idle-n", nargs="+", type=int, default=[0, 80])
    parser.add_argument("--keep-cluster", action="store_true")
    parser.add_argument("--generate-only", action="store_true",
                        help="write profile configs without starting any VM")
    return parser.parse_args()


def main(arguments: argparse.Namespace) -> int:
    """Generate configs and execute selected profiles serially."""
    base = read_yaml(arguments.base_config.resolve())
    profile_by_id = {str(profile["id"]): profile for profile in DEFAULT_PROFILES}
    unknown = [profile_id for profile_id in arguments.profiles if profile_id not in profile_by_id]
    if unknown:
        raise ValueError(f"unknown profiles {unknown}; choose from {sorted(profile_by_id)}")
    output_dir = arguments.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    manifest = {
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "baseConfig": str(arguments.base_config.resolve()),
        "phase": arguments.phase,
        "profiles": arguments.profiles,
        "serial": True,
        "keepCluster": bool(arguments.keep_cluster),
        "notes": [
            "Each generated config has a unique cluster, libvirt network, IP range, registry port and namespace.",
            "The child runner destroys only its generated cluster unless --keep-cluster is passed.",
            "No existing VM or network is inspected or modified beyond the generated profile resources.",
        ],
    }
    write_json(output_dir / "calibration_manifest.json", manifest)
    for profile_id in arguments.profiles:
        profile = profile_by_id[profile_id]
        profile_dir = output_dir / profile_id
        profile_dir.mkdir(parents=True, exist_ok=True)
        config = isolated_config(
            base,
            profile,
            profile_dir,
            phase=arguments.phase,
            repetitions=arguments.repetitions,
            measurement_seconds=arguments.measurement_seconds,
            warmup_seconds=arguments.warmup_seconds,
            loaded_n=arguments.loaded_n,
            idle_n=arguments.idle_n,
        )
        config_path = profile_dir / "config.yaml"
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        if arguments.generate_only:
            rows.append({
                "profile": profile_id,
                "vcpus": profile["vcpus"],
                "memoryGiB": profile["memoryGiB"],
                "returnCode": 0,
                "runDir": str(profile_dir / "run"),
                "generatedOnly": True,
            })
            write_summary(output_dir, rows)
            continue
        run_dir = profile_dir / "run"
        command = [
            sys.executable,
            str(E1A_RUNNER),
            "--config",
            str(config_path),
            "--run-dir",
            str(run_dir),
            "--phase",
            arguments.phase,
        ]
        if arguments.keep_cluster:
            command.append("--keep-cluster")
        print(f"[{datetime.now().isoformat(timespec='seconds')}] profile={profile_id}", flush=True)
        return_code = run_command(command, profile_dir / "runner.log")
        knee_path = run_dir / "metrics/knee.json"
        knee = json.loads(knee_path.read_text(encoding="utf-8")) if knee_path.exists() else {}
        rows.append(
            {
                "profile": profile_id,
                "vcpus": profile["vcpus"],
                "memoryGiB": profile["memoryGiB"],
                "returnCode": return_code,
                "runDir": str(run_dir),
                "selectedKneeN": knee.get("selectedKneeN"),
                "firstWorkloadOverloadN": knee.get("firstWorkloadOverloadN"),
                "lastStableSuccessN": knee.get("lastStableSuccessN"),
                "firstStableOverloadN": knee.get("firstStableOverloadN"),
                "clearKnee": knee.get("clearKnee"),
            }
        )
        write_summary(output_dir, rows)
        if return_code != 0:
            print(f"profile {profile_id} failed; see {profile_dir / 'runner.log'}", file=sys.stderr, flush=True)
            return return_code
    print(json.dumps({"outputDir": str(output_dir), "profiles": rows}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(parse_arguments()))
