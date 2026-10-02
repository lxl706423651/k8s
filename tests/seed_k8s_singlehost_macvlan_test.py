#!/usr/bin/env python3

import importlib.util
import json
import os
import sys
import subprocess
import tempfile
import unittest
from unittest import mock
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml


def loadModule(name: str, path: Path):
    """Load one repository script as a module for focused unit tests."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class SeedK8sSingleHostMacvlanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo_root = Path(__file__).resolve().parents[1]
        cls.config_path = (
            cls.repo_root
            / "largeScale/b62_k8s_scale/configSingleHostKvmMacvlan.yaml"
        )
        cls.networks = loadModule(
            "seed_k8s_prepare_kvm_networks",
            cls.repo_root
            / "seedemu/k8sTools/resources/setup/kvm/prepareKvmNetworks.py",
        )
        cls.experiment = loadModule(
            "seed_k8s_singlehost_run_full",
            cls.repo_root / "largeScale/b62_k8s_scale/runFullExperiment.py",
        )
        cls.dataplane = loadModule(
            "seed_k8s_singlehost_dataplane",
            cls.repo_root
            / "largeScale/b62_k8s_scale/validateCrossHostWorkload.py",
        )

    def test_config_resource_budget_and_trunks(self) -> None:
        config = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(config["kind"], "kvm")
        self.assertEqual(config["workers"]["count"], 5)
        total_vcpus = config["master"]["vcpus"] + (
            config["workers"]["count"] * config["workers"]["vcpus"]
        )
        total_memory = self.experiment.memoryMiB(config["master"]) + (
            config["workers"]["count"] * self.experiment.memoryMiB(config["workers"])
        )
        self.assertEqual(total_vcpus, 298)
        self.assertEqual(total_memory, 596 * 1024)
        self.assertEqual(
            [item["masterInterface"] for item in config["fabric"]["vlanTrunks"]],
            ["ens3", "ens4"],
        )

    def test_libvirt_network_xml_isolated_from_management(self) -> None:
        config = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        management = self.networks.managementNetworkSpec(config)
        management_root = ET.fromstring(
            self.networks.managementNetworkXml(management)
        )
        self.assertEqual(management_root.find("forward").get("mode"), "nat")
        self.assertEqual(
            management_root.find("bridge").get("name"),
            "virbrb62sh",
        )

        extras = self.networks.extraNetworkSpecs(config)
        self.assertEqual(len(extras), 2)
        for extra in extras:
            root = ET.fromstring(self.networks.layer2NetworkXml(extra))
            self.assertIsNone(root.find("forward"))
            self.assertIsNone(root.find("ip"))

    def test_compact_cluster_summary_uses_configured_vm_plan(self) -> None:
        cluster = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        assignment = {
            "experiment": {
                "topologySize": 4954,
                "workerCount": 5,
            }
        }
        summary = self.experiment.createSummary(
            Path("/tmp/test-single-host"),
            assignment,
            cluster,
        )
        self.assertEqual(summary["workerCount"], 5)
        self.assertEqual(summary["clusterPlan"]["vmCount"], 6)
        self.assertEqual(summary["clusterPlan"]["totalVcpus"], 298)
        self.assertEqual(summary["clusterPlan"]["totalMemoryGiB"], 596)

    def test_memory_units_and_validation(self) -> None:
        for resource in ({"memoryGiB": 10.5}, {"memoryGb": 10.5},
                         {"memoryMb": 10752}, {"memory_mb": 10752}):
            self.assertEqual(self.experiment.memoryMiB(resource), 10752)
        for resource in ({"memoryGiB": -1}, {"memoryGiB": True},
                         {"memoryGiB": "nan"}, {"memoryGiB": "inf"},
                         {"memoryGiB": 0.0001},
                         {"memoryGiB": 10, "memoryMb": 1024}):
            with self.subTest(resource=resource), self.assertRaises(ValueError):
                self.experiment.memoryMiB(resource)

    def test_memory_units_survive_native_setup(self) -> None:
        from seedemu.k8sTools.config import makeKvmConfig, makeMultiHostKvmConfig
        config = makeKvmConfig(config=self.config_path)
        self.assertEqual(config["master"]["memoryMb"], 98304)
        self.assertEqual(config["workers"]["memoryMb"], 102400)
        parser = loadModule("unit_kvm_config", self.repo_root /
                            "seedemu/k8sTools/resources/setup/kvm/manageKvmConfig.py")
        raw = yaml.safe_load(self.config_path.read_text())
        parser.normalize_config(raw)
        self.assertEqual(raw["master"]["memory_mb"], 98304)
        node = parser.normalize_node(
            {"name": "test", "role": "worker", "ip": "10.0.0.2",
             "mac": "52:54:00:00:00:02", "vcpus": 5,
             "disk_gb": 80, "memoryGiB": 10.5},
            {"memory_mb": 1024},
        )
        self.assertEqual(node["memory_mb"], 10752)
        multi = makeMultiHostKvmConfig(config=self.config_path.with_name("configMultiHostKvmMacvlan.yaml"))
        self.assertEqual(multi["workers"]["memoryMb"], 17408)

    def test_explicit_disk_survives_native_adapter(self) -> None:
        """Preserve explicit-node disk capacity through the public config adapter."""
        from seedemu.k8sTools.config import makeKvmConfig
        parser = loadModule("unit_explicit_disk", self.repo_root /
                            "seedemu/k8sTools/resources/setup/kvm/manageKvmConfig.py")
        node = {"name": "test", "role": "master", "ip": "10.0.0.2",
                "mac": "52:54:00:00:00:02", "vcpus": 2,
                "disk_gb": 40, "memoryGiB": 2}
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "input.yaml"
            source.write_text(yaml.safe_dump({"kind": "kvm", "nodes": [node]}))
            adapted = makeKvmConfig(config=source)
            self.assertEqual(parser.nodes(adapted)[0]["disk_gb"], 40)
        with self.assertRaises(SystemExit):
            parser.normalize_node({**node, "diskGb": 80})

    def test_48_vm_fractional_memory_summary(self) -> None:
        cluster = yaml.safe_load(self.config_path.read_text())
        cluster["workers"].update(count=47, vcpus=5, memoryGiB=10.5)
        summary = self.experiment.createSummary(Path("/tmp/unit-48vm"),
            {"experiment": {"topologySize": 4954, "workerCount": 47}}, cluster)
        self.assertEqual(summary["clusterPlan"]["vmCount"], 48)
        self.assertEqual(summary["clusterPlan"]["totalMemoryGiB"], 589.5)

    def test_reconvergence_targets_ignore_pod_hash_order_and_placement(self) -> None:
        module = loadModule("unit_reconvergence", self.repo_root /
                            "largeScale/b62_k8s_scale/measureReconvergence.py")
        prefixes = [f"as{asn}brd-r{router}-1.{router}.0.1"
                    for asn in range(101, 113) for router in (2, 10)]
        pods = [module.PodInfo(prefix + "-hash-a", "worker1", "Running", "router")
                for prefix in prefixes]
        expected = ([f"as{asn}brd-r10-1.10.0.1" for asn in range(101, 111)],
                    [f"as{asn}brd-r2-1.2.0.1" for asn in range(101, 105)])
        self.assertEqual(module.selectDefaultTargets(pods), expected)
        moved = [module.PodInfo(prefix + "-other-b", "worker2", "Running", "router")
                 for prefix in reversed(prefixes)]
        moved.append(module.PodInfo("as1brd-ix1-5.0.0.1-hash-a", "master", "Running", "router"))
        moved.append(module.PodInfo("as2brd-r1-1.1.0.1-hash-a", "master", "Pending", "router"))
        self.assertEqual(module.selectDefaultTargets(moved), expected)
        self.assertEqual(module.selectDefaultTargets([]), ([], []))

    def test_readiness_rejects_partial_but_ready_pod_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = self.config_path.parent / "wait-ready.sh"
            (root / "wait-ready.sh").write_text(script.read_text())
            (root / "lib.sh").write_text('''
setup_experiment_context() {
    EXPERIMENT_DIR="$1"
    SEED_TOPOLOGY_SIZE=2
    KUBECONFIG=unused
    SEED_NAMESPACE=unit-test
}
ensure_kubeconfig() { :; }
begin_stage_logging() { :; }
sleep() { READY_PHASE=1; }
kubectl() {
    if [ "$2" = namespace ]; then return 0; fi
    printf 'router1 1/1 Running 0 1m seedemu brd\\n'
    if [ "${READY_PHASE:-0}" = 1 ]; then
        printf 'router2 1/1 Running 0 1m seedemu brd\\n'
    fi
    return 0
}
''')
            result = subprocess.run(["bash", str(root / "wait-ready.sh"), directory],
                text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Not ready yet (expected 2 SeedEMU pods)", result.stdout)
            self.assertIn("total=2, running=2, ready=2", result.stdout)

    def test_pair_scope_is_deterministic(self) -> None:
        endpoints = {
            "net-a": [
                {
                    "pod": "a",
                    "node": "node-1",
                    "managementGroup": "10.81.0.0/24",
                },
                {
                    "pod": "b",
                    "node": "node-2",
                    "managementGroup": "10.81.0.0/24",
                },
                {
                    "pod": "c",
                    "node": "node-3",
                    "managementGroup": "10.82.0.0/24",
                },
            ]
        }
        cross_node = self.dataplane.selectPair(endpoints, "cross-node")
        cross_host = self.dataplane.selectPair(endpoints, "cross-host")
        self.assertEqual([item["pod"] for item in cross_node], ["a", "b"])
        self.assertEqual([item["pod"] for item in cross_host], ["a", "c"])

    def test_single_config_derives_experiment_without_legacy_assignment(self) -> None:
        config = yaml.safe_load(self.config_path.read_text())
        config["experiment"]["topologySize"] = 4954
        config["workers"].update(count=47, vcpus=5, memoryGiB=10.5)
        config["experiment"]["topologyDir"] = "topologies"
        before = yaml.safe_dump(config)
        assignment = self.experiment.createSingleConfigAssignment(config, Path("/tmp/unit/config.yaml"))
        self.assertEqual(yaml.safe_dump(config), before)
        self.assertEqual(assignment["experiment"]["workerCount"], 47)
        self.assertEqual(assignment["experiment"]["namespace"], "seedemu-b62-mvlan-vlan-4954")
        self.assertEqual(assignment["experiment"]["canonicalOutputDir"], "/tmp/unit/topologies/output_4954")
        self.assertEqual(assignment["networking"]["vlanTrunks"], config["fabric"]["vlanTrunks"])
        self.assertEqual(assignment["deploy"], config["deploy"])
        self.assertNotIn("ovn", assignment)
        plan = self.experiment.createSummary(Path("/tmp/unit"), assignment, config)["clusterPlan"]
        self.assertEqual((plan["vmCount"], plan["totalVcpus"], plan["totalMemoryGiB"]), (48, 283, 589.5))

    def test_single_config_rejects_duplicate_count_and_invalid_size(self) -> None:
        for section, key, value in (("experiment", "workerCount", 47),
                                    ("experiment", "topologySize", 0),
                                    ("experiment", "topologySize", True),
                                    ("workers", "count", -1)):
            config = yaml.safe_load(self.config_path.read_text())
            config[section][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.experiment.createSingleConfigAssignment(config, self.config_path)

    def test_single_config_prepare_and_standalone_stage_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "1078_test"
            generated = root / "configK3s.yaml"
            kubeconfig = root / "kubeconfig.yaml"
            inventory = root / "inventory.yaml"
            result = subprocess.run([
                "bash", str(self.config_path.parent / "runSingleHostMacvlanExperiment.sh"),
                str(run_dir), "--prepare-only", "--config-k3s", str(generated),
                "--kubeconfig", str(kubeconfig), "--inventory", str(inventory),
            ], cwd=root, text=True, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads((run_dir / "summary.json").read_text())
            self.assertEqual(summary["status"], "PREPARED")
            self.assertEqual(summary["configurationMode"], "single-yaml")
            self.assertFalse(any(path.exists() for path in (generated, kubeconfig, inventory)))
            clean_env = {key: value for key, value in os.environ.items()
                         if not key.startswith(("B62_", "SEED_")) and key != "KUBECONFIG"}
            result = subprocess.run(["bash", "-c", '''
source "$1/lib.sh"
setup_experiment_context "$2"
printf '%s\\n' "$SEED_NAMESPACE" "$B62_WORKER_COUNT" "$KUBECONFIG" "$SEED_K3S_MASTER_IP" "$SEED_CNI_MASTER_INTERFACE" "$B62_CLUSTER_INPUT_CONFIG"
''', "unit", str(self.config_path.parent), str(run_dir)], cwd=root,
                env=clean_env, text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), ["seedemu-b62-mvlan-vlan-1078", "5",
                str(kubeconfig), "10.81.0.10", "ens3", str(run_dir / "cluster-input.yaml")])

    def test_single_config_run_and_resume_use_frozen_inputs(self) -> None:
        runner = self.experiment
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "config.yaml"
            source.write_text(self.config_path.read_text())
            run_dir = root / "1078_test"
            original_load = runner.loadYaml

            def load_without_legacy(path):
                self.assertNotEqual(path, runner.DEFAULT_ASSIGNMENT)
                return original_load(path)

            argv = ["runFullExperiment.py", "--config", str(source), "--run-dir", str(run_dir)]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(runner, "loadYaml", side_effect=load_without_legacy), \
                 mock.patch.object(runner, "maybeDestroyExisting") as destroy, \
                 mock.patch.object(runner, "buildCluster") as build, \
                 mock.patch.object(runner, "runWorkloadStages") as workload:
                self.assertEqual(runner.main(), 0)
                destroy.assert_called_once()
                build.assert_called_once()
                workload.assert_called_once()
                self.assertEqual(build.call_args.args[3]["B62_CONFIG_KVM_PATH"], str(run_dir / "cluster-input.yaml"))
            source.unlink()
            with mock.patch.object(sys, "argv", argv + ["--resume-from", "start-kernel"]), \
                 mock.patch.object(runner, "loadYaml", side_effect=load_without_legacy), \
                 mock.patch.object(runner, "buildCluster") as build, \
                 mock.patch.object(runner, "maybeDestroyExisting") as destroy, \
                 mock.patch.object(runner, "runWorkloadStages") as workload:
                self.assertEqual(runner.main(), 0)
                build.assert_not_called()
                destroy.assert_not_called()
                self.assertTrue(workload.call_args.kwargs["resume_from_kernel"])
                self.assertEqual(workload.call_args.args[3]["SEED_TOPOLOGY_SIZE"], "1078")

    def test_legacy_assignment_entry_still_uses_explicit_assignment(self) -> None:
        runner = self.experiment
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "legacy"
            argv = ["runFullExperiment.py", "--assignment", str(runner.DEFAULT_ASSIGNMENT),
                    "--cluster-config", str(self.config_path), "--run-dir", str(run_dir)]
            with mock.patch.object(sys, "argv", argv), \
                 mock.patch.object(runner, "buildCluster"), \
                 mock.patch.object(runner, "maybeDestroyExisting"), \
                 mock.patch.object(runner, "runWorkloadStages"):
                self.assertEqual(runner.main(), 0)
            self.assertEqual((run_dir / "assignment.yaml").read_text(), runner.DEFAULT_ASSIGNMENT.read_text())

    def test_single_config_rejects_mixed_inputs_before_execution(self) -> None:
        with mock.patch.object(sys, "argv", ["runFullExperiment.py", "--config", str(self.config_path),
                                           "--assignment", "missing.yaml"]), \
             mock.patch("sys.stderr"), self.assertRaises(SystemExit) as error:
            self.experiment.parseArgs()
        self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
