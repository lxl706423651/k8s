#!/usr/bin/env python3

import importlib.util
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
import warnings
from pathlib import Path

import yaml

from seedemu.k8sTools.config import detectKind


class SeedK8sMultiHostMacvlanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo_root = Path(__file__).resolve().parents[1]
        cls.helper = (
            cls.repo_root
            / "seedemu/k8sTools/resources/setup/multiHostKvm/manageMultiHostKvmConfig.py"
        )
        experiment_path = (
            cls.repo_root / "largeScale/b62_k8s_scale/runFullExperiment.py"
        )
        spec = importlib.util.spec_from_file_location(
            "b62_run_full_experiment",
            experiment_path,
        )
        assert spec is not None and spec.loader is not None
        cls.experiment = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.experiment
        spec.loader.exec_module(cls.experiment)

    def writeConfig(self, directory: Path, master_interface: str = "ens3") -> Path:
        """Write a minimal two-hypervisor macvlan+VLAN config for one test."""
        path = directory / "multi-host.yaml"
        path.write_text(
            textwrap.dedent(
                f"""
                kind: multiHostKvm
                clusterName: test-multihost
                hypervisors:
                  - name: left
                    ip: 192.0.2.10
                    connection: local
                    routedSubnet:
                      cidr: 10.80.1.0/24
                      gateway: 10.80.1.1
                    vmCount: 2
                  - name: right
                    ip: 192.0.2.20
                    ssh:
                      user: tester
                    routedSubnet:
                      cidr: 10.80.2.0/24
                      gateway: 10.80.2.1
                    vmCount: 1
                fabric:
                  type: macvlan-vlan
                  mtu: 1400
                  vlanTrunks:
                    - name: trunk0
                      masterInterface: {master_interface}
                      vlanStart: 1
                      vlanEnd: 4094
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        return path

    def test_canonical_and_legacy_kinds(self) -> None:
        self.assertEqual(detectKind({"kind": "multiHostKvm"}, "test"), "multiHostKvm")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.assertEqual(
                detectKind({"kind": "multiHostKvmOvn"}, "legacy.yaml"),
                "multiHostKvm",
            )
        self.assertIn("deprecated kind", str(caught[0].message))

    def test_host_local_config_contains_dedicated_fabric_nic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            config = self.writeConfig(directory)
            output = directory / "host-kvm.yaml"
            result = subprocess.run(
                [
                    "python3",
                    str(self.helper),
                    "--config",
                    str(config),
                    "write-host-local-kvm",
                    "--host",
                    "left",
                    "--output",
                    str(output),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            data = yaml.safe_load(output.read_text(encoding="utf-8"))
            self.assertEqual(len(data["nodes"]), 2)
            self.assertEqual(
                data["kvm"]["extraNetworks"][0]["masterInterface"],
                "ens3",
            )
            self.assertEqual(data["kvm"]["extraNetworks"][0]["trunk"], "trunk0")
            self.assertTrue(data["kvm"]["prepareImageCache"])

            remote_output = directory / "remote-host-kvm.yaml"
            remote_result = subprocess.run(
                [
                    "python3",
                    str(self.helper),
                    "--config",
                    str(config),
                    "write-host-local-kvm",
                    "--host",
                    "right",
                    "--output",
                    str(remote_output),
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(remote_result.returncode, 0, msg=remote_result.stderr)
            remote_data = yaml.safe_load(remote_output.read_text(encoding="utf-8"))
            self.assertFalse(remote_data["kvm"]["prepareImageCache"])

    def test_management_nic_cannot_be_used_as_cross_host_trunk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = self.writeConfig(Path(tmp), master_interface="ens2")
            result = subprocess.run(
                ["python3", str(self.helper), "--config", str(config), "validate"],
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("dedicated VM NIC", result.stderr)

    def test_multi_host_summary_uses_explicit_vm_and_resource_plan(self) -> None:
        assignment = {
            "experiment": {
                "topologySize": 4954,
                "workerCount": 11,
            }
        }
        cluster = {
            "kind": "multiHostKvm",
            "clusterName": "test-multihost",
            "hypervisors": [
                {"name": "left", "vmCount": 2},
                {"name": "right", "vmCount": 1},
            ],
            "master": {"vcpus": 8, "memoryMb": 8192, "diskGb": 80},
            "workers": {"vcpus": 4, "memoryMb": 4096, "diskGb": 80},
        }
        summary = self.experiment.createSummary(
            Path("/tmp/test-run"),
            assignment,
            cluster,
        )
        self.assertEqual(summary["workerCount"], 2)
        self.assertEqual(
            summary["clusterPlan"],
            {
                "kind": "multiHostKvm",
                "clusterName": "test-multihost",
                "hypervisorCount": 2,
                "vmCount": 3,
                "workerCount": 2,
                "totalVcpus": 16,
                "totalMemoryMiB": 16384,
                "totalMemoryGiB": 16,
                "diskGiBPerVm": 80,
            },
        )

    def test_record_placement_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            plan = {
                "controls": {"exclude_control_plane": False},
                "assignments": {"64500": "node-a", "64501": "node-b"},
                "nodes": [
                    {"name": "seed-k3s-master", "assigned_pods": 3},
                    {"name": "seed-k3s-worker1", "assigned_pods": 2},
                ],
            }
            (run_dir / "placement_plan.json").write_text(
                json.dumps(plan),
                encoding="utf-8",
            )
            summary: dict[str, object] = {}
            summary_path = run_dir / "summary.json"
            self.experiment.recordPlacementSummary(
                summary,
                summary_path,
                run_dir,
            )
            placement = summary["staticPlacement"]
            self.assertEqual(placement["nodeCount"], 2)
            self.assertEqual(placement["asCount"], 2)
            self.assertEqual(placement["podCount"], 5)
            self.assertEqual(placement["minPodsPerNode"], 2)
            self.assertEqual(placement["maxPodsPerNode"], 3)
            self.assertEqual(placement["masterPods"], 3)
            self.assertFalse(placement["excludeControlPlane"])


if __name__ == "__main__":
    unittest.main()
