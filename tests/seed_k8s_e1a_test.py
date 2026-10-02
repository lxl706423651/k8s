#!/usr/bin/env python3
"""Unit tests for the B63 E1-A fixed-arrival workload and analysis."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "largeScale/b63_deployment"


def loadModule(name: str, path: Path):
    """Load one example module without requiring package installation."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sys.path.insert(0, str(EXAMPLE))
analysis = loadModule("e1a_analysis_test", EXAMPLE / "analyzeE1aExperiment.py")
runner = loadModule("e1a_runner_test", EXAMPLE / "executeE1aExperiment.py")


class E1aWorkloadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.temporary.name) / "e1aWorkload"
        subprocess.run(["gcc", "-O2", "-static", "-o", str(cls.binary),
                        str(EXAMPLE / "e1aWorkload.c"), "-lm"], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def executeWorkload(self, *, units: int, backlog: float = 1.0) -> dict:
        """Run a short local workload with a future synchronized epoch."""
        epoch = time.time_ns() + 100_000_000
        result = subprocess.run([
            str(self.binary), "--jobs-per-second", "50", "--work-units", str(units),
            "--phase-seed", "17", "--max-backlog-seconds", str(backlog),
            "--warmup-seconds", "0", "--duration-seconds", "0.5",
            "--deadline-microseconds", "20000", "--epoch-unix-ns", str(epoch),
            "--pod-id", "stable-0",
        ], check=True, text=True, capture_output=True, timeout=10)
        return json.loads(result.stdout)

    def test_task_count_conservation(self):
        record = self.executeWorkload(units=1000)
        self.assertTrue(record["count_conservation_ok"])
        self.assertEqual(record["planned_arrivals"], record["completed"] +
                         record["dropped"] + record["pending_at_end"])

    def test_overload_does_not_self_throttle_arrivals(self):
        record = self.executeWorkload(units=30_000_000, backlog=0.1)
        self.assertGreaterEqual(record["planned_arrivals"], 24)
        self.assertTrue(record["dropped"] > 0 or record["pending_at_end"] > 0)
        self.assertTrue(record["count_conservation_ok"])

    def test_fixed_phase_keeps_task_count_and_work_sequence(self):
        first = self.executeWorkload(units=1000)
        second = self.executeWorkload(units=1000)
        self.assertEqual(first["planned_arrivals"], second["planned_arrivals"])
        self.assertEqual(first["work_sink"], second["work_sink"])

    def test_calibration_returns_positive_frozen_units(self):
        result = subprocess.run([str(self.binary), "--calibrate-target-microseconds", "200",
                                 "--calibrate-samples", "9"], check=True, text=True,
                                capture_output=True, timeout=10)
        record = json.loads(result.stdout)
        self.assertGreater(record["recommended_work_units"], 0)
        self.assertEqual(record["samples"], 9)


class E1aProtocolTest(unittest.TestCase):
    def test_cluster_access_accepts_generated_management_ip_spelling(self):
        with tempfile.TemporaryDirectory() as temporary:
            runDir = Path(temporary)
            (runDir / "inventory.yaml").write_text(
                "nodes:\n- name: worker-a\n  role: worker\n  managementIp: 192.0.2.11\n")
            config = {"ssh": {"user": "ubuntu", "key": "~/.ssh/id_ed25519"}}
            access = runner.clusterAccess(runDir, config)
            self.assertEqual(access.workerIp, "192.0.2.11")

    def test_manifest_has_real_node_placement_without_cpu_limit(self):
        access = runner.ClusterAccess(Path("k"), "worker-a", "192.0.2.1", "ubuntu", Path("key"))
        job = runner.jobObject("ns", "background", "background", 10, access, "pause:3.6",
                               ["--epoch-file", "/control/epoch"], "10m", "16Mi", "/tmp/e1a")
        container = job["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(job["spec"]["template"]["spec"]["nodeName"], "worker-a")
        self.assertEqual(container["resources"]["requests"]["cpu"], "10m")
        self.assertNotIn("limits", container["resources"])
        self.assertEqual(job["spec"]["completionMode"], "Indexed")

    def test_formal_plan_freezes_five_pod_neighbors_and_five_blocks(self):
        config = {"e1a": {"formal": {
            "coarseN": list(range(0, 201, 20)), "fineRadiusPods": 10,
            "fineStepPods": 5, "repetitions": 5, "randomSeed": 17,
        }}}
        plan = runner.formalPlan(config, {"maxBackgroundN": 236}, {"selectedKneeN": 80})
        counts = {count for _, _, count in plan}
        self.assertTrue({70, 75, 80, 85, 90}.issubset(counts))
        self.assertEqual(len(plan), 5 * 15 * 2)
        self.assertEqual({block for block, _, _ in plan}, {1, 2, 3, 4, 5})

    def test_parallel_waves_assign_one_point_per_worker(self):
        accesses = [runner.ClusterAccess(Path("k"), f"w{index}", f"192.0.2.{index}",
                                         "ubuntu", Path("key")) for index in range(1, 4)]
        plan = [(1, "loaded", value) for value in (0, 20, 40, 60, 80)]
        calls = []

        def fakePoint(access, binary, image, runDir, config, phase, block, group, count,
                      workloadUnits, barrier):
            calls.append((access.workerName, count, barrier.parties))
            return {"pointId": f"{access.workerName}-{count}", "status": "success"}

        with mock.patch.object(runner, "executePoint", side_effect=fakePoint):
            result = runner.executePlanParallel(
                accesses, Path("bin"), {item.workerName: "pause" for item in accesses},
                Path("run"), {}, "formal", plan, {"background": 1, "canary": 1}, 3)
        self.assertEqual(len(result), 5)
        self.assertEqual([parties for _, _, parties in calls], [3, 3, 3, 2, 2])
        self.assertEqual(len({worker for worker, _, _ in calls[:3]}), 3)

    def test_pilot_plan_uses_real_admission_without_uniform_cap(self):
        config = {"e1a": {"pilot": {"loadedN": [0, 20, 40], "idleN": [0, 40]}}}
        plan = runner.pilotPlan(config, {"maxBackgroundN": 25})
        self.assertEqual(plan, [(1, "loaded", 0), (1, "loaded", 20), (1, "idle", 0)])

    def test_marginal_calculation_and_knee_detection(self):
        rows = []
        for podCount in range(0, 120, 20):
            latency = 1.0 + 0.002 * podCount if podCount <= 60 else 1.12 + 0.08 * (podCount - 60)
            rows.append({"phase": "pilot", "group": "loaded", "N": podCount,
                         "canaryP99MedianMs": latency})
        analysis.addMarginalMetrics(rows)
        knee = analysis.detectKnee(rows, "pilot", 0.20)
        self.assertTrue(knee["clearKnee"])
        self.assertIn(knee["segmentedKneeN"], {40, 60, 80})
        self.assertIsNotNone(rows[1]["marginalP99Ms"])

    def test_raw_to_summary_preserves_idle_and_loaded(self):
        points = [
            {"phase": "pilot", "group": group, "N": count, "status": "success",
             "canary": {"latency_p99_ms": float(count + 1)}, "canaryOnTimeRate": 1.0,
             "nodeMetrics": {"cpuBusyMean": 0.1, "runQueueMean": 1.0,
                             "cpuPsiSomeAvg10Mean": 0.0},
             "background": {"completionRate": 1.0, "dropRate": 0.0}}
            for group in ("loaded", "idle") for count in (0, 20)
        ]
        rows = analysis.aggregatePoints(points)
        self.assertEqual({row["group"] for row in rows}, {"loaded", "idle"})
        self.assertEqual(len(rows), 4)

    def test_repeated_outcomes_define_a_transition_band(self):
        points = []
        for podCount, overloads in ((40, 0), (60, 2), (70, 5)):
            for repeat in range(5):
                points.append({
                    "phase": "formal", "group": "loaded", "N": podCount,
                    "status": "workload_overload" if repeat < overloads else "success",
                    "canary": {"latency_p99_ms": float(podCount + repeat)},
                    "canaryOnTimeRate": 1.0,
                    "nodeMetrics": {"cpuBusyMean": 0.5, "runQueueMean": 2.0,
                                    "cpuPsiSomeAvg10Mean": 0.0},
                    "background": {"completionRate": 1.0, "dropRate": 0.0},
                })
        rows = analysis.aggregatePoints(points)
        analysis.addMarginalMetrics(rows)
        knee = analysis.detectKnee(rows, "formal", 0.20)
        self.assertEqual(knee["lastStableSuccessN"], 40)
        self.assertEqual(knee["firstStableOverloadN"], 70)
        self.assertEqual(knee["repeatabilityTransitionBracket"], [40, 70])
        self.assertEqual(knee["mixedOutcomeN"], [60])
        self.assertEqual(next(row for row in rows if row["N"] == 60)["workloadOverloadRate"], 0.4)


if __name__ == "__main__":
    unittest.main()
