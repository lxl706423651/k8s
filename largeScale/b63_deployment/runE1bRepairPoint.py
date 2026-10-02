#!/usr/bin/env python3
"""Run one independently recorded replacement point for corrected E1-B.

The replacement is deliberately kept outside the pre-randomized formal
sequence.  It reuses the frozen mapping, performs a full static redeploy and
protocol/FIB verification, and writes its own point summary.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import runE1bExperiment as e1b
import runE1bFormal as formal


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_replacement(run_dir: Path, mapping_id: str, output_subdir: str) -> int:
    config = formal.shared.loadYaml(run_dir / "assignment.yaml")
    env = formal.shared.createRuntimeEnv(
        run_dir / "assignment.yaml", config, run_dir,
        cluster_input=run_dir / "cluster-input.yaml",
        config_k3s=run_dir / "configK3s.yaml",
        kubeconfig=run_dir / "kubeconfig.yaml",
        inventory=run_dir / "inventory.yaml",
    )
    output = run_dir / output_subdir
    point_dir = output / f"01-{mapping_id}"
    point_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output / "repair_summary.json"
    summary: dict[str, Any] = {
        "status": "RUNNING",
        "phase": "e1b-repair-point",
        "mappingId": mapping_id,
        "stages": [],
        "tests": {},
    }
    write_json(summary_path, summary)
    try:
        e1b.redeploy(run_dir, env, summary, summary_path, mapping_id, point_dir / "deployment")
        placement = e1b.verifyLivePlacement(
            run_dir, env, point_dir / "placement_actual_summary.json")
        e1b.runDataplane("preflight", run_dir, env, point_dir / "preflight", duration=5)
        mapping = e1b.mappingById(run_dir, mapping_id)
        traffic = config["e1b"]["traffic"]
        durations = {
            "udp": int(traffic["udpMeasurementSeconds"]),
            "tcp": int(traffic["tcpMeasurementSeconds"]),
        }
        repetitions = {
            "udp": 1,
            "tcp": int(traffic.get("tcpTechnicalRepetitions", 1)),
        }
        udp_workload = e1b.measurePointWorkload(
            run_dir, env, mapping, point_dir / "udp_phase", ("udp",),
            durations, repetitions)
        tcp_workload = e1b.measurePointWorkload(
            run_dir, env, mapping, point_dir / "tcp_phase", ("tcp",),
            durations, repetitions)
        result = {
            "mappingId": mapping_id,
            "placement": placement,
            "preflight": json.loads(
                (point_dir / "preflight/summary.json").read_text(encoding="utf-8")),
            "udp": json.loads(
                (point_dir / "udp_phase/udp/summary.json").read_text(encoding="utf-8")),
            "tcp": json.loads(
                (point_dir / "tcp_phase/tcp/summary.json").read_text(encoding="utf-8")),
            "udpWorkload": udp_workload,
            "tcpWorkload": tcp_workload,
        }
        write_json(point_dir / "point_summary.json", result)
        e1b.archiveDeploymentState(run_dir, point_dir)
        summary.update({
            "status": "PASS",
            "point": str(point_dir),
            "udpWorkloadStatus": udp_workload.get("status"),
            "tcpWorkloadStatus": tcp_workload.get("status"),
        })
        write_json(summary_path, summary)
        print(json.dumps(summary, indent=2))
        return 0
    except BaseException as error:
        summary.update({
            "status": "FAIL",
            "error": {"type": type(error).__name__, "message": str(error)},
        })
        write_json(summary_path, summary)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--mapping-id", default="pi04-target084")
    parser.add_argument("--output-subdir", default="e1b/repair_pi04")
    args = parser.parse_args(argv)
    return run_replacement(args.run_dir.resolve(), args.mapping_id, args.output_subdir)


if __name__ == "__main__":
    raise SystemExit(main())
