#!/usr/bin/env python3
"""Test B62 BIRD recovery sampling with a fake clock; never access a cluster."""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "b62_recovery_test_module", ROOT / "largeScale/b62_k8s_scale/measureReconvergence.py")
recovery = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = recovery
SPEC.loader.exec_module(recovery)


class RecoveryStabilityTest(unittest.TestCase):
    def test_two_unchanged_rounds_backdate_to_first_plateau_sample(self):
        state = recovery.RouteStability()
        state.update(100, 1)
        state.update(200, 6)
        state.update(200, 11)
        self.assertIsNone(state.confirmedAtSeconds)
        state.update(200, 16)
        self.assertEqual(state.stableSinceSeconds, 6)
        self.assertEqual(state.confirmedAtSeconds, 16)

    def test_late_change_revokes_previous_confirmation(self):
        state = recovery.RouteStability()
        for elapsed in (0, 5, 10):
            state.update(100, elapsed)
        self.assertEqual(state.confirmedAtSeconds, 10)
        state.update(99, 15)
        self.assertIsNone(state.confirmedAtSeconds)
        for elapsed in (20, 25):
            state.update(99, elapsed)
        self.assertEqual(state.stableSinceSeconds, 15)

    def test_missing_or_zero_reading_cannot_confirm_stability(self):
        for invalid in (None, 0):
            state = recovery.RouteStability()
            state.update(20, 0)
            state.update(20, 5)
            state.update(invalid, 10)
            state.update(20, 15)
            state.update(20, 20)
            self.assertIsNone(state.confirmedAtSeconds)
            state.update(20, 25)
            self.assertEqual(state.stableSinceSeconds, 15)

    def run_monitor(self, series, *, healthy_at=0, timeout=60, load_high_until=0):
        """Feed per-pod count series to the real poll loop using a fake clock."""
        clock = [0.0]
        pods = [recovery.PodInfo(name, "node1", "Running", "brd") for name in series]
        observer = recovery.PodInfo("observer", "node1", "Running", "brd")

        def collect(*args):
            checks = {}
            for pod in args[2]:
                values = series.get(pod.name, [100])
                count = values[min(int(clock[0] / 5), len(values) - 1)]
                checks[pod.name] = {"pod": pod.name, "returnCode": 0 if count is not None else 1,
                    "metrics": {"routes": count}, "sampledAtMonotonic": clock[0],
                    "passed": clock[0] >= healthy_at and count is not None and count > 0}
            return checks

        def sleep(seconds):
            clock[0] += seconds

        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(recovery.time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch.object(recovery.time, "sleep", side_effect=sleep), \
             mock.patch.object(recovery, "collectProtocolHealth", side_effect=collect), \
             mock.patch.object(recovery, "collectNodeLoads", side_effect=lambda _: {
                 "node1": {"load1": 55.0 if clock[0] < load_high_until else 1.0}}), \
             mock.patch.object(recovery, "log"):
            path = Path(directory)
            try:
                result = recovery.waitForBirdRecovery(chaos_pods=pods, verify_pods=[observer],
                    nodes={}, kubeconfig="unused", namespace="test", load_threshold=50,
                    timeout_seconds=timeout, exec_timeout=10, started=0, artifact_dir=path)
            except TimeoutError:
                self.assertEqual(json.loads((path / "recovery_stability.json").read_text())["status"], "TIMEOUT")
                raise
            samples = [json.loads(line) for line in (path / "recovery_samples.jsonl").read_text().splitlines()]
            return result[0], samples

    def test_global_time_is_slowest_router_first_stable_sample(self):
        result, samples = self.run_monitor({"a": [10, 20, 20, 20], "b": [1, 2, 3, 3, 3]})
        self.assertEqual(result["recoveryConvergenceSeconds"], 10)
        self.assertEqual(result["stabilityConfirmedSeconds"], 20)
        self.assertEqual(result["stablePods"], 2)
        self.assertEqual([s["elapsedSeconds"] for s in samples], [0, 5, 10, 15, 20])

    def test_unhealthy_protocols_are_polled_without_losing_route_timestamp(self):
        result, _ = self.run_monitor({"a": [10, 20, 20, 20]}, healthy_at=25)
        self.assertEqual(result["recoveryConvergenceSeconds"], 5)
        self.assertEqual(result["validationCompletedSeconds"], 25)

    def test_load_gate_delays_success_without_stopping_route_sampling(self):
        result, samples = self.run_monitor({"a": [10, 20, 20, 20]}, load_high_until=25)
        self.assertEqual(result["validationCompletedSeconds"], 25)
        self.assertEqual(result["recoveryConvergenceSeconds"], 5)
        self.assertFalse(samples[0]["loadGatePassed"])

    def test_zero_routes_timeout_instead_of_false_success(self):
        with self.assertRaises(TimeoutError):
            self.run_monitor({"a": [0]}, timeout=20)

    def test_one_router_read_failure_does_not_count_as_stability(self):
        result, _ = self.run_monitor({"a": [1, 2, None, 2, 2, 2]})
        self.assertEqual(result["recoveryConvergenceSeconds"], 15)
        self.assertEqual(result["stabilityConfirmedSeconds"], 25)

    def test_uses_bird_total_routes_not_network_count(self):
        routes, networks = recovery.parseBirdRouteCount(
            "BIRD 2.17.2 ready.\nTotal: 14000 of 14000 routes for 4954 networks in 2 tables")
        self.assertEqual((routes, networks), (14000, 4954))

    def test_reconvergence_resume_does_not_rebuild_or_redeploy(self):
        script_dir = ROOT / "largeScale/b62_k8s_scale"
        sys.path.insert(0, str(script_dir))
        import runFullExperiment as runner
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            config = yaml.safe_load((script_dir / "configSingleHostKvmMacvlan.yaml").read_text())
            assignment = runner.createSingleConfigAssignment(config, script_dir / "configSingleHostKvmMacvlan.yaml")
            (run_dir / "assignment.yaml").write_text(yaml.safe_dump(assignment))
            (run_dir / "cluster-input.yaml").write_text(yaml.safe_dump(config))
            (run_dir / "summary.json").write_text(json.dumps({"status": "FAIL", "stages": []}))
            with mock.patch.object(sys, "argv", ["runFullExperiment.py", "--config", "missing.yaml",
                      "--run-dir", str(run_dir), "--resume-from", "reconvergence"]), \
                 mock.patch.object(runner, "runReconvergenceStage") as stage, \
                 mock.patch.object(runner, "buildCluster") as build, \
                 mock.patch.object(runner, "runWorkloadStages") as workload, \
                 mock.patch.object(runner, "maybeDestroyExisting") as destroy:
                self.assertEqual(runner.main(), 0)
                stage.assert_called_once()
                build.assert_not_called()
                workload.assert_not_called()
                destroy.assert_not_called()

    def test_failure_before_monitor_does_not_reuse_old_stability(self):
        pods = [recovery.PodInfo(f"as1brd-r{i}-1.{i}.0.1-hash-x", "node1", "Running", "brd")
                for i in (1, 2)]
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(recovery, "loadInventory", return_value={}), \
             mock.patch.object(recovery, "listSeedemuPods", return_value=pods), \
             mock.patch.object(recovery, "waitForClusterLoad", side_effect=TimeoutError("load timeout")), \
             mock.patch.object(recovery, "log"):
            root = Path(directory)
            (root / "recovery_stability.json").write_text('{"status":"PASS"}')
            self.assertEqual(recovery.main(["--namespace", "test", "--artifact-dir", directory,
                "--inventory", "unused", "--kubeconfig", "unused"]), 1)
            summary = json.loads((root / "reconvergence_summary.json").read_text())
            self.assertEqual(summary["status"], "FAIL")
            self.assertNotIn("recoveryStability", summary)


if __name__ == "__main__":
    unittest.main()
