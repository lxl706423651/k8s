#!/usr/bin/env python3
"""Unit tests for B63 research placement, modelling, and frozen study protocol."""
from __future__ import annotations

import json
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "largeScale/b63_deployment"
sys.path.insert(0, str(EXAMPLE))

import fitPerformanceModel as fitting
import performanceStudy as study
import researchPlacement as research
import runPerformanceStudy as runner
import runPlacementExperiment as placement


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


dataplane = load_module("research_measure_dataplane", EXAMPLE / "running/measureDataplane.py")
b62_dataplane = load_module(
    "research_validate_cross_host",
    ROOT / "largeScale/b62_k8s_scale/validateCrossHostWorkload.py",
)
bird_verify = load_module(
    "research_verify_start_bird",
    ROOT / "largeScale/b62_k8s_scale/verifyStartBird.py",
)
load_injector = load_module("research_apply_pod_cpu_load", EXAMPLE / "applyPodCpuLoad.py")
node_load_injector = load_module(
    "research_apply_pod_cpu_load_node_local", EXAMPLE / "applyPodCpuLoadNodeLocal.py")


class ResearchPlacementTest(unittest.TestCase):
    def fixture(self, root: Path):
        nodes = [placement.NodeInfo("n1", "worker", 2, 2048, maxPods=1,
                                    admissionSource="status.allocatable.pods"),
                 placement.NodeInfo("n2", "worker", 2, 2048, maxPods=3,
                                    admissionSource="status.allocatable.pods")]
        network = placement.NetworkInfo("net", "net", "peer", "1", "10.0.0.0/24",
                                        weight=5, vlanId=10, vlanTrunk="trunk0")
        pods = []
        for index in range(3):
            pod = placement.PodInfo(f"p{index}", "ns", "Pod", index, 1, str(index),
                                    "router", ["net"], None, None, cpu=.1, memoryMb=100, q=5)
            pods.append(pod)
            network.endpoints.append(pod.name)
        config = root / "cluster.yaml"
        config.write_text(yaml.safe_dump({"fabric": {"vlanTrunks": [
            {"name": "trunk0", "vlanStart": 1, "vlanEnd": 20}]}}))
        args = SimpleNamespace(admission_max_pods=99, kvm_config=config,
                               external_cpu_millicores=500, resource_target_utilization=.7,
                               placement_seed=7, performance_model=root / "model.json",
                               contention_knee=.7, static_contention_weight=1.0)
        return nodes, pods, {"net": network}, args

    def test_real_per_node_admission_is_used_without_artificial_uniform_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            nodes, pods, networks, args = self.fixture(Path(tmp))
            result, metadata = research.generate("locality-only", pods, nodes, networks, args)
            counts = {node.name: list(result.values()).count(node.name) for node in nodes}
            self.assertLessEqual(counts["n1"], 1)
            self.assertLessEqual(counts["n2"], 3)
            self.assertFalse(metadata["usesArtificialUniformPodCap"])
            self.assertEqual(metadata["admission"]["kubeletMaxPodsByNode"], {"n1": 1, "n2": 3})
            self.assertEqual(set(metadata["admission"]["kubeletAdmissionSourceByNode"].values()),
                             {"status.allocatable.pods"})

    def test_tradeoff_is_deterministic_and_model_coefficients_are_consumed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nodes, pods, networks, args = self.fixture(root)
            first, meta = research.generate("tradeoff-static", pods, nodes, networks, args)
            second, _ = research.generate("tradeoff-static", pods, nodes, networks, args)
            self.assertEqual(first, second)
            self.assertTrue(meta["predeclaredSweepPoint"])
            args.performance_model.write_text(json.dumps({
                "trainingDataHash": "abc", "contentionKnee": .7,
                "coefficients": {"communicationCost": 2, "contentionCost": 3}}))
            model, model_meta = research.generate("performance-model", pods, nodes, networks, args)
            self.assertEqual(set(model), set(first))
            self.assertEqual(model_meta["modelTrainingDataHash"], "abc")

    def test_crescent_resource_aware_keeps_all_pods_when_cpu_is_oversubscribed(self):
        """CPU guides the enhanced baseline but cannot reject required Pods."""
        with tempfile.TemporaryDirectory() as tmp:
            nodes, pods, networks, args = self.fixture(Path(tmp))
            args.external_cpu_millicores = 5000
            result, metadata = research.generate(
                "crescent-resource-aware", pods, nodes, networks, args)
            self.assertEqual(set(result), {pod.name for pod in pods})
            self.assertTrue(metadata["cpuUsedAsPlacementSignal"])
            self.assertFalse(metadata["cpuHardAdmissionLimit"])
            self.assertEqual(metadata["admission"]["status"], "PASS")

    @mock.patch.object(placement.subprocess, "run")
    def test_required_live_admission_reads_allocatable_pods(self, execute):
        with tempfile.TemporaryDirectory() as tmp:
            kubeconfig = Path(tmp) / "kubeconfig"
            kubeconfig.write_text("x")
            execute.side_effect = [
                SimpleNamespace(stdout=json.dumps({"items": [
                    {"metadata": {"name": "n1"}, "status": {"allocatable": {"pods": "4k"}}}]})),
                SimpleNamespace(stdout=json.dumps({"items": [
                    {"metadata": {"name": "system-a"}, "spec": {"nodeName": "n1"},
                     "status": {"phase": "Running"}},
                    {"metadata": {"name": "old-job"}, "spec": {"nodeName": "n1"},
                     "status": {"phase": "Succeeded"}},
                ]})),
            ]
            node = placement.NodeInfo("n1", "worker", 1, 1024)
            args = SimpleNamespace(admission_max_pods=99, kubeconfig=kubeconfig,
                                   require_live_admission=True)
            placement.applyAdmissionCapacities([node], args)
            self.assertEqual(node.kubeletMaxPods, 4000)
            self.assertEqual(node.existingPods, 1)
            self.assertEqual(node.maxPods, 3999)
            self.assertEqual(node.admissionSource,
                             "status.allocatable.pods-minus-existing-nonterminal-pods")

    def test_integer_kubernetes_quantity_rejects_fractional_pods(self):
        self.assertEqual(placement.integerKubernetesQuantity("4k"), 4000)
        self.assertEqual(placement.integerKubernetesQuantity("2Ki"), 2048)
        with self.assertRaises(ValueError):
            placement.integerKubernetesQuantity("500m")


class ModelAndProtocolTest(unittest.TestCase):
    def test_node_local_load_result_parser_preserves_every_pod_status(self):
        parsed, summary = node_load_injector.parseResults(
            "ok\tp1\tc1\nfailed\tp2\tc2\trc=1 boom\n"
            "SUMMARY total=2 ok=1 failed=1 missing_pid=0 expected=2\n")
        self.assertEqual(parsed["p1"]["status"], "ok")
        self.assertEqual(parsed["p2"]["detail"], "rc=1 boom")
        self.assertEqual(summary, {"total": 2, "ok": 1, "failed": 1,
                                   "missing_pid": 0, "expected": 2})
        self.assertIn("crictl pods -o json", node_load_injector.REMOTE_SCRIPT)
        self.assertIn("nsenter -t", node_load_injector.REMOTE_SCRIPT)

    def test_load_injector_retries_transient_pod_list_failure(self):
        failed = SimpleNamespace(returncode=124, stdout="", stderr="timeout")
        passed = SimpleNamespace(returncode=0, stdout=json.dumps({"items": []}), stderr="")
        with mock.patch.object(load_injector, "run", side_effect=[failed, passed]), \
             mock.patch.object(load_injector.time, "sleep"):
            pods = load_injector.podList(Path("k"), "ns", 5, 3, 0)
        self.assertEqual(pods, [])

    def test_load_injector_retries_timeout_and_records_attempts(self):
        pod = {"metadata": {"name": "p1"}, "spec": {"nodeName": "n1"}}
        timeout = SimpleNamespace(returncode=124, stdout="", stderr="timeout")
        passed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch.object(load_injector, "run", side_effect=[timeout, passed]), \
             mock.patch.object(load_injector.time, "sleep"):
            result = load_injector.applyOne(Path("k"), "ns", pod, 15, 5, 3, 0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["attemptCount"], 2)
        self.assertEqual([item["returncode"] for item in result["attempts"]], [124, 0])

    def test_bird_target_selection_checks_all_available_as_when_sample_is_larger(self):
        pods = {"items": []}
        for asn in (3, 7, 11):
            pods["items"].append({
                "metadata": {"name": f"as{asn}brd-r1-unit", "labels": {
                    "seedemu.io/workload": "seedemu", "seedemu.io/role": "brd",
                    "seedemu.io/asn": str(asn)}},
                "status": {"phase": "Running"}, "spec": {"nodeName": "worker1"},
            })
        with mock.patch.object(bird_verify, "kubectlListPods", return_value=pods):
            selected = bird_verify.selectTargets("ns", "kubeconfig", 10, 30)
        self.assertEqual([item.asn for item in selected], [3, 7, 11])

    def test_mtu_samples_cover_occupied_nodes_without_requiring_unused_nodes(self):
        by_network = {"net-a": [
            {"node": "worker1", "pod": "p1", "ip": "10.0.0.1", "network": "net-a"},
            {"node": "worker2", "pod": "p2", "ip": "10.0.0.2", "network": "net-a"},
        ]}
        samples = b62_dataplane.selectNodeMtuSamples(by_network)
        self.assertEqual(set(samples), {"worker1", "worker2"})
        cluster_nodes = {"master", "worker1", "worker2"}
        occupied_nodes = {"worker1", "worker2"}
        self.assertEqual(occupied_nodes - set(samples), set())
        self.assertEqual(cluster_nodes - occupied_nodes, {"master"})

    def test_flow_selection_is_independent_of_pod_to_node_mapping(self):
        base = []
        for index in range(12):
            base.append({
                "directionId": f"d{index}", "pairId": f"p{index // 2}",
                "stratum": "cross_as_multihop",
                "src": {"id": f"src{index}", "node": "node-a"},
                "dst": {"id": f"dst{index}", "node": "node-b"},
                "nodeRelation": "cross_node",
            })
        remapped = []
        for item in base:
            changed = json.loads(json.dumps(item))
            changed["src"]["node"] = "node-c"
            changed["dst"]["node"] = "node-c"
            changed["nodeRelation"] = "same_node"
            remapped.append(changed)
        ordered_a = dataplane.deterministic_directions(base, 17, "topology", "stress")
        ordered_b = dataplane.deterministic_directions(remapped, 17, "topology", "stress")
        selected_a, _ = dataplane.select_balanced_directions(ordered_a, 5, 2, 2, True)
        selected_b, _ = dataplane.select_balanced_directions(ordered_b, 5, 2, 2, True)
        self.assertEqual([item["directionId"] for item in selected_a],
                         [item["directionId"] for item in selected_b])

    def test_protocol_records_all_implementation_hashes(self):
        hashes = runner.implementationHashes()
        self.assertEqual(set(hashes), set(runner.PROTOCOL_IMPLEMENTATIONS))
        self.assertTrue(all(len(value) == 64 for value in hashes.values()))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = runner.snapshotImplementations(root, hashes)
            self.assertEqual(set(manifest["files"]), set(hashes))
            for relative, item in manifest["files"].items():
                copied = root / item["snapshot"]
                self.assertTrue(copied.is_file())
                self.assertEqual(item["sha256"], hashes[relative])

    def test_study_split_rejects_e3_calibration_and_training_leakage(self):
        config_path = EXAMPLE / "research_configs/configE3Topology1078FourVm.yaml"
        config = yaml.safe_load(config_path.read_text())
        variants = runner.variantList(config)
        runner.validateStudySplit(config, config_path, "E3", variants)

        calibration_leak = json.loads(json.dumps(config))
        calibration_leak["study"]["calibrationLoadsMillicores"] = [5]
        with self.assertRaisesRegex(ValueError, "cannot contain calibration"):
            runner.validateStudySplit(calibration_leak, config_path, "E3", variants)

        with tempfile.TemporaryDirectory() as tmp:
            model_path = Path(tmp) / "leaked-model.json"
            model_path.write_text(json.dumps({"trainingTopologySize": 1078}))
            topology_leak = json.loads(json.dumps(config))
            topology_leak["placement"]["performanceModel"] = str(model_path)
            with self.assertRaisesRegex(ValueError, "different topology size"):
                runner.validateStudySplit(topology_leak, config_path, "E3", variants)

    def test_remote_image_build_has_bounded_retry(self):
        script = (ROOT / "largeScale/b62_k8s_scale/build.sh").read_text()
        self.assertIn("SEED_IMAGE_BUILD_TIMEOUT_SECONDS=300", script)
        self.assertIn("SEED_IMAGE_BUILD_RETRIES=3", script)
        self.assertIn("timeout --signal=TERM --kill-after=30s", script)

    def test_analysis_interpreter_can_render_plots(self):
        interpreter = runner.analysisInterpreter()
        completed = __import__("subprocess").run(
            [str(interpreter), "-c", "import matplotlib"], check=False)
        self.assertEqual(completed.returncode, 0)

    def test_small_ridge_fit_recovers_both_features(self):
        samples = []
        for communication, contention in ((0, 0), (1, 0), (0, 1), (1, 1), (.5, 2)):
            samples.append({"communicationPairRatio": communication,
                            "contentionFeature": contention,
                            "performancePenalty": 1 + 2 * communication + 3 * contention})
        result = fitting.fit(samples, 1e-12)
        self.assertAlmostEqual(result["communicationCost"], 2, places=6)
        self.assertAlmostEqual(result["contentionCost"], 3, places=6)
        self.assertAlmostEqual(result["rSquared"], 1, places=8)

    def test_performance_penalty_keeps_failures(self):
        base = {"stressRttMs": 2, "stressGoodputMbps": 100, "stressFailureRate": 0}
        failed = dict(base, stressFailureRate=.5)
        self.assertGreater(study.performancePenalty(failed), study.performancePenalty(base))

    def test_performance_penalty_does_not_reward_survivor_rtt(self):
        healthy = {"stressRttMs": 16, "stressGoodputMbps": 33000,
                   "stressFailureRate": .1875}
        survivor_only = {"stressRttMs": .44, "stressGoodputMbps": 4200,
                         "stressFailureRate": .9375}
        self.assertGreater(study.performancePenalty(survivor_only),
                           study.performancePenalty(healthy))

    def test_checked_in_protocols_are_isolated_and_frozen(self):
        e1_path = EXAMPLE / "research_configs/configE1Topology214FourVm.yaml"
        e1 = yaml.safe_load(e1_path.read_text())
        variants = runner.variantList(e1)
        self.assertEqual(e1["study"]["calibrationLoadsMillicores"], [0, 10, 25])
        self.assertEqual(e1["study"]["holdoutLoadsMillicores"], [50])
        self.assertEqual(len({item["id"] for item in variants}), len(variants))
        self.assertFalse(e1["kvm"]["allowExisting"])
        for name in ("configE3Topology1078FourVm.yaml", "configE3Topology4954FortyEightVm.yaml"):
            config = yaml.safe_load((EXAMPLE / "research_configs" / name).read_text())
            self.assertEqual(config["study"]["phase"], "E3")
            self.assertTrue(config["study"]["continueAfterVariantFailure"])
            self.assertTrue(config["study"]["cleanupFailedVariants"])
            self.assertGreater(config["study"].get("destroyPhysicalTimeoutSeconds", 1800), 0)
            self.assertEqual({item["algorithm"] for item in config["study"]["variants"]},
                             {"crescent", "crescent-resource-aware", "performance-model"})
            self.assertNotEqual(config["experiment"]["topologySize"], 214)


if __name__ == "__main__":
    unittest.main()
