#!/usr/bin/env python3
"""Offline invariants for the corrected E1-B direct-link harness."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "largeScale/b63_deployment"
sys.path.insert(0, str(EXAMPLE))


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


measure = load("e1b_measure_test", EXAMPLE / "measureE1bDataplane.py")
analyzer = load("e1b_analyzer_test", EXAMPLE / "analyzeE1bExperiment.py")


class E1bPlanTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.planRoot = EXAMPLE / "experiments/e1_marginal_pods/e1b_topology214_frozen_20260919/plans"
        cls.selected = json.loads((cls.planRoot / "selected_pair_plan.json").read_text())
        cls.sequence = json.loads((cls.planRoot / "placement_sequence.json").read_text())

    def test_all_flows_are_compiled_direct_networks(self):
        flows = self.selected["flows"]
        self.assertEqual(len(flows), 16)
        self.assertEqual(self.selected["metadata"]["linkSemantics"], "compiled-direct-network")
        self.assertEqual(len({flow["directNetwork"] for flow in flows}), 16)
        self.assertTrue(all(flow["networkCidr"] for flow in flows))

    def test_primary_crossings_strictly_decrease_and_control_matches_counts(self):
        primary = [item for item in self.sequence["mappings"] if item["relation"] == "primary"]
        crossings = [item["fixedFlowCrossings"] for item in primary]
        self.assertEqual(crossings, sorted(crossings, reverse=True))
        self.assertEqual(len(set(crossings)), len(crossings))
        control = next(item for item in self.sequence["mappings"] if item["relation"] == "matched-count-control")
        target = primary[1]
        self.assertEqual(
            {node: value["emulationPods"] for node, value in control["perNode"].items()},
            {node: value["emulationPods"] for node, value in target["perNode"].items()},
        )
        self.assertGreater(control["fixedFlowCrossings"], target["fixedFlowCrossings"])

    def test_admission_uses_real_kubelet_bound_and_workload_projection(self):
        for mapping in self.sequence["mappings"]:
            for node in mapping["perNode"].values():
                self.assertTrue(node["admissionFeasible"])
                self.assertLessEqual(node["projectedAdmissionPods"], node["kubeletMaxPods"])


class E1bMeasurementTest(unittest.TestCase):
    def test_udp_parser_uses_receiver_view(self):
        payload = json.dumps({"end": {
            "sum": {"bits_per_second": 100000000, "lost_percent": 0},
            "sum_received": {"bits_per_second": 80000000, "lost_percent": 20,
                              "bytes": 100, "lost_packets": 2, "packets": 10,
                              "jitter_ms": 1.5},
            "sum_sent": {"bits_per_second": 100000000, "bytes": 120},
        }})
        parsed = measure.parseUdp(payload)
        self.assertEqual(parsed["receivedMbps"], 80.0)
        self.assertEqual(parsed["actualSendMbps"], 100.0)
        self.assertEqual(parsed["lossPct"], 20)

    def test_analyzer_keeps_incomplete_point_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            point = root / "e1b/pilot/pi00"
            point.mkdir(parents=True)
            (point / "point_summary.json").write_text(json.dumps({
                "mappingId": "pi00", "placement": {"status": "PASS", "podCount": 214},
                "preflight": {"passed": 16}, "udp": {"status": "harness_invalid"},
                "tcp": {}, "workload": {},
            }))
            rows = analyzer.pointRows(root)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["udpStatus"], "harness_invalid")


if __name__ == "__main__":
    unittest.main()
