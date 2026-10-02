#!/usr/bin/env python3
"""Run the five-block corrected E1-B independent validation sequence.

The sequence is frozen before deployment, randomized within each block with a
recorded seed, and keeps UDP offered load and TCP capacity in separate fresh
workload phases. Every mapping is redeployed statically; no online migration
is used.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

import runE1bExperiment as e1b


HERE = Path(__file__).resolve().parent
B62_DIR = HERE.parent / "b62_k8s_scale"
sys.path.insert(0, str(B62_DIR))
import runFullExperiment as shared


def writeJson(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def sequence(runDir: Path, seed: int, repetitions: int) -> list[dict[str, Any]]:
    plan = e1b.readPlan(runDir)
    ids = [item["mappingId"] for item in plan["mappings"]]
    rng = random.Random(seed)
    result: list[dict[str, Any]] = []
    for block in range(1, repetitions + 1):
        order = list(ids)
        rng.shuffle(order)
        for position, mappingId in enumerate(order, 1):
            result.append({"block": block, "position": position, "mappingId": mappingId})
    return result


def loadPhaseSummary(pointDir: Path, mode: str) -> dict[str, Any]:
    return json.loads((pointDir / mode / "workload_summary.json").read_text())


def runFormal(runDir: Path, seed: int, repetitions: int, outputSubdir: str = "e1b/formal") -> int:
    config = shared.loadYaml(runDir / "assignment.yaml")
    env = shared.createRuntimeEnv(
        runDir / "assignment.yaml", config, runDir,
        cluster_input=runDir / "cluster-input.yaml", config_k3s=runDir / "configK3s.yaml",
        kubeconfig=runDir / "kubeconfig.yaml", inventory=runDir / "inventory.yaml")
    formalDir = runDir / outputSubdir
    formalDir.mkdir(parents=True, exist_ok=True)
    order = sequence(runDir, seed, repetitions)
    writeJson(formalDir / "frozen_order.json", {
        "status": "RUNNING", "seed": seed, "repetitions": repetitions,
        "pointCount": len(order), "order": order,
    })
    summary: dict[str, Any] = {"status": "RUNNING", "startedAt": datetime.now(timezone.utc).isoformat(),
                               "seed": seed, "repetitions": repetitions, "points": []}
    writeJson(formalDir / "formal_summary.json", summary)
    try:
        traffic = config["e1b"]["traffic"]
        dataplaneDurations = {
            "udp": int(traffic["udpMeasurementSeconds"]),
            "tcp": int(traffic["tcpMeasurementSeconds"]),
        }
        dataplaneRepetitions = {
            "udp": 1,
            "tcp": int(traffic.get("tcpTechnicalRepetitions", 1)),
        }
        writeJson(formalDir / "measurement_settings.json", {
            "udpMeasurementSeconds": dataplaneDurations["udp"],
            "tcpMeasurementSeconds": dataplaneDurations["tcp"],
            "udpTechnicalRepetitions": dataplaneRepetitions["udp"],
            "tcpTechnicalRepetitions": dataplaneRepetitions["tcp"],
            "workloadWarmupSeconds": 20,
            "workloadMeasurementSeconds": 80,
            "note": "Data-plane durations come from the frozen E1-B config; workload phase remains the frozen 20+80 second job image.",
        })
        for index, item in enumerate(order, 1):
            mappingId = item["mappingId"]
            pointDir = formalDir / f"block{item['block']:02d}" / f"{item['position']:02d}-{mappingId}"
            pointDir.mkdir(parents=True, exist_ok=True)
            # Always redeploy, including the first point, so block order has no
            # hidden dependence on the preceding pilot mapping.
            e1b.redeploy(runDir, env, {"stages": [], "tests": {}}, formalDir / "redeploy_summary.json",
                         mappingId, pointDir / "deployment")
            placement = e1b.verifyLivePlacement(runDir, env, pointDir / "placement_actual_summary.json")
            e1b.runDataplane("preflight", runDir, env, pointDir / "preflight", duration=5)
            mapping = e1b.mappingById(runDir, mappingId)
            # Fresh namespaces and collectors isolate the UDP and TCP phases;
            # they therefore cannot hide a phase-specific contention effect.
            udpWorkload = e1b.measurePointWorkload(
                runDir, env, mapping, pointDir / "udp_phase", ("udp",), dataplaneDurations,
                dataplaneRepetitions)
            tcpWorkload = e1b.measurePointWorkload(
                runDir, env, mapping, pointDir / "tcp_phase", ("tcp",), dataplaneDurations,
                dataplaneRepetitions)
            result = {
                "block": item["block"], "position": item["position"], "mappingId": mappingId,
                "placement": placement,
                "preflight": json.loads((pointDir / "preflight/summary.json").read_text()),
                "udp": json.loads((pointDir / "udp_phase/udp/summary.json").read_text()),
                "tcp": json.loads((pointDir / "tcp_phase/tcp/summary.json").read_text()),
                "udpWorkload": udpWorkload, "tcpWorkload": tcpWorkload,
            }
            writeJson(pointDir / "point_summary.json", result)
            summary["points"].append({"block": item["block"], "position": item["position"],
                                      "mappingId": mappingId, "status": "PASS"})
            writeJson(formalDir / "formal_summary.json", summary)
            print(json.dumps({"completed": index, "total": len(order), "mappingId": mappingId}))
        summary["status"] = "PASS"
        summary["finishedAt"] = datetime.now(timezone.utc).isoformat()
        writeJson(formalDir / "formal_summary.json", summary)
        return 0
    except BaseException as error:
        summary["status"] = "FAIL"
        summary["failure"] = {"type": type(error).__name__, "message": str(error)}
        summary["finishedAt"] = datetime.now(timezone.utc).isoformat()
        writeJson(formalDir / "formal_summary.json", summary)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20260918)
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--output-subdir", default="e1b/formal",
                        help="artifact directory relative to the run directory")
    args = parser.parse_args(argv)
    return runFormal(args.run_dir.resolve(), args.seed, args.repetitions, args.output_subdir)


if __name__ == "__main__":
    raise SystemExit(main())
