#!/usr/bin/env python3
"""Test scheduler semantics and serial orchestration without creating VMs.

Uses synthetic manifests, frozen config fixtures and mocked external stages;
outputs unittest results and temporary artifacts only.
"""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'largeScale/b63_deployment'))
sys.path.insert(0, str(ROOT / 'tests'))
import runPlacementComparison as batch
import collectPlacementComparison as collect
import runDefaultSchedulerBaseline as default
import runPlacementExperiment as placement
import verifyPlacement as actual
import verifyStaticPlacement as static
import seed_k8s_b63_shared_test as fixtures


class SchedulerSemanticsTest(unittest.TestCase):
    def createFixture(self):
        fixture = fixtures.StaticPlacementTest()
        fixture.setUp()
        name = 'kubernetes-default-scheduler'
        fixture.args.algorithm = name
        fixture.report['metadata'] = {'selectedAlgorithm': name, 'includeMaster': True}
        fixture.report['placements'][name] = fixture.report['placements'].pop('optimized')
        fixture.report['staticValidation'][name] = fixture.report['staticValidation'].pop('optimized')
        fixture.deployed = copy.deepcopy(fixture.source)
        pods = placement.parsePods(fixture.source, placement.parseNetworks(fixture.source))
        placement.injectPlacement(fixture.deployed, pods, fixture.report['placements'][name],
                                  inject_resources=False, pinning_mode='none')
        default.prepareSchedulerManifest(fixture.deployed, pods[0].namespace)
        return fixture

    def test_static_default_is_unpinned_simulation_not_hard_plan(self):
        f = self.createFixture()
        result = f.validateFixture()
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['placementKind'], 'scheduler-simulation')
        for doc in f.deployed[1:]:
            self.assertNotIn('nodeSelector', doc['spec'])
            self.assertEqual(doc['spec']['schedulerName'], 'default-scheduler')
        f.deployed[1]['spec']['nodeSelector'] = {'kubernetes.io/hostname': 'n1'}
        self.assertEqual(f.validateFixture()['status'], 'FAIL')

    def test_default_scrubber_preserves_requests_images_networks(self):
        docs = [{'kind': 'Pod', 'metadata': {'name': 'r', 'annotations': {
            'k8s.v1.cni.cncf.io/networks': 'net1', 'seedemu.k8s.placement/assigned-node': 'n1'}},
            'spec': {'nodeName': 'n1', 'nodeSelector': {'kubernetes.io/hostname': 'n1'},
                     'containers': [{'image': 'router:v1', 'resources': {'requests': {'cpu': '50m'}}}]}}]
        before = copy.deepcopy(docs[0]['spec']['containers'])
        default.prepareSchedulerManifest(docs, 'test', ['n1', 'n2'])
        self.assertEqual(docs[0]['spec']['containers'], before)
        self.assertEqual(docs[0]['metadata']['annotations'], {'k8s.v1.cni.cncf.io/networks': 'net1'})
        expression = docs[0]['spec']['affinity']['nodeAffinity']['requiredDuringSchedulingIgnoredDuringExecution']['nodeSelectorTerms'][0]['matchExpressions'][0]
        self.assertEqual(expression['values'], ['n1', 'n2'])
        self.assertNotIn('nodeName', docs[0]['spec'])

    def test_live_default_accepts_real_mapping_and_rejects_bad_pods(self):
        f = self.createFixture()
        pods = [{'metadata': {'name': f'r{i}-suffix', 'labels': {'app': f'r{i}'}},
                 'spec': {'nodeName': 'n2', 'schedulerName': 'default-scheduler'},
                 'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}} for i in (1, 2)]
        result = actual.validatePlacement(f.report, pods)
        self.assertEqual(result['status'], 'PASS')  # r1 differs from simulated n1.
        self.assertEqual(result['perNode'], {'n1': 0, 'n2': 2})
        for field, value in [('nodeName', 'unknown'), ('schedulerName', 'custom'),
                             ('nodeSelector', {'kubernetes.io/hostname': 'n2'})]:
            bad = copy.deepcopy(pods)
            bad[0]['spec'][field] = value
            self.assertEqual(actual.validatePlacement(f.report, bad)['status'], 'FAIL')
        self.assertEqual(actual.validatePlacement(f.report, pods[:1])['status'], 'FAIL')
        self.assertEqual(actual.validatePlacement(f.report, pods + pods[:1])['status'], 'FAIL')
        pods[0]['status'] = {}
        self.assertEqual(actual.validatePlacement(f.report, pods)['status'], 'FAIL')

    def test_all_node_preload_includes_init_images_and_all_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root/'k8s.yaml'
            manifest.write_text(yaml.safe_dump({'kind': 'Pod', 'metadata': {'name': 'router'},
                'spec': {'containers': [{'image': 'router:v1'}], 'initContainers': [{'image': 'init:v1'}]}}))
            command = [sys.executable, str(ROOT/'largeScale/b62_k8s_scale/generate_node_image_refs.py')]
            subprocess.run(command + [str(manifest), str(root/'pinned'), 'n1', 'n2'], check=True)
            self.assertEqual(json.loads((root/'pinned/summary.json').read_text())['missing_selector_count'], 1)
            subprocess.run(command + ['--all-nodes', str(manifest), str(root/'all'), 'n1', 'n2'], check=True)
            self.assertEqual((root/'all/images_n1.txt').read_text(), 'init:v1\nrouter:v1\n')
            self.assertEqual((root/'all/images_n1.txt').read_text(), (root/'all/images_n2.txt').read_text())

    def test_pod_balanced_equal_count_on_heterogeneous_nodes(self):
        nodes = [placement.NodeInfo(name='master', role='master', cpu=16, memoryMb=24576),
                 placement.NodeInfo(name='worker', role='worker', cpu=8, memoryMb=24576)]
        docs = [{'kind': 'Pod', 'metadata': {'name': f'r{i}'},
                 'spec': {'containers': [{'image': 'router'}]}} for i in range(5)]
        pods = placement.parsePods(docs, {})
        result = placement.generatePodCountPlacement(pods, nodes)
        self.assertEqual(sorted([list(result.values()).count(n.name) for n in nodes]), [2, 3])


class SerialWorkflowTest(unittest.TestCase):
    def test_comparison_exposes_network_only_in_all_order(self):
        self.assertEqual(batch.ALGORITHMS, (
            'optimized', 'network-only',
            'kubernetes-default-scheduler', 'pod-count-balanced'))

    def invokeBatch(self, root, flags=(), fail_run=None, fail_cleanup=None):
        config = yaml.safe_load((batch.SCRIPT_DIR/'configSingleHostKvmMacvlan.yaml').read_text())
        config['experiment']['topologyDir'] = './relative-topology'
        config['kvm']['baseImagePath'] = './base.img'
        source = root/'config.yaml'
        source.write_text(yaml.safe_dump(config))
        calls = []
        def execute(command, log):
            algorithm = log.parent.name
            cleanup = 'destroy' in command
            calls.append((algorithm, 'destroy' if cleanup else 'run'))
            if cleanup:
                return 1 if algorithm == fail_cleanup else 0
            code = 1 if algorithm == fail_run else 0
            status = 'FAIL' if code else 'PREPARED' if '--prepare-only' in command else 'PASS'
            (log.parent/'summary.json').write_text(json.dumps({'status': status, 'stages': []}))
            return code
        with mock.patch.object(batch, 'executeCommand', side_effect=execute):
            code = batch.main(['--config', str(source), '--run-dir', str(root/'runs'), *flags])
        return code, calls, json.loads((root/'runs/comparison_runs.json').read_text())

    def test_serial_destroy_between_methods_and_freeze_relative_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rc, calls, progress = self.invokeBatch(root)
            self.assertEqual(rc, 0)
            self.assertEqual(calls, [(a, op) for a in batch.ALGORITHMS for op in ('run', 'destroy')])
            self.assertEqual(progress['status'], 'PASS')
            for a in batch.ALGORITHMS:
                cfg = yaml.safe_load((root/'runs'/a/'input.yaml').read_text())
                self.assertEqual(cfg['placement']['algorithm'], a)
                self.assertEqual(cfg['experiment']['topologyDir'], str(root/'relative-topology'))
                self.assertEqual(cfg['kvm']['baseImagePath'], str(root/'base.img'))
                self.assertNotIn('workerCount', cfg['experiment'])
            report = json.loads((root/'runs/comparison_summary.json').read_text())
            self.assertEqual(report['sampleComparison'], 'INCOMPLETE')
            self.assertIsNone(report['rows'][0]['pingMeanRttMs'])

    def test_failure_keeps_cluster_and_stops_next_method(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, calls, progress = self.invokeBatch(Path(tmp), fail_run='optimized')
            self.assertEqual(rc, 1)
            self.assertEqual(calls, [('optimized', 'run')])
            self.assertEqual(progress['status'], 'FAIL')

    def test_cleanup_failure_stops_next_method(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, calls, progress = self.invokeBatch(Path(tmp), fail_cleanup='optimized')
            self.assertEqual(rc, 1)
            self.assertEqual(calls, [('optimized', 'run'), ('optimized', 'destroy')])
            self.assertEqual(progress['runs'][0]['cleanupStatus'], 'FAIL')

    def test_prepare_never_destroys_and_single_run_keeps_cluster(self):
        for flags, expected in [(['--prepare-only'], [(a, 'run') for a in batch.ALGORITHMS]),
                                (['--algorithm', 'network-only'], [('network-only', 'run')]),
                                (['--algorithm', 'pod-count-balanced'], [('pod-count-balanced', 'run')])]:
            with tempfile.TemporaryDirectory() as tmp:
                rc, calls, _ = self.invokeBatch(Path(tmp), flags=flags)
                self.assertEqual(rc, 0)
                self.assertEqual(calls, expected)

    def test_existing_results_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.invokeBatch(root)
            with mock.patch.object(batch, 'executeCommand') as execute:
                with self.assertRaises(SystemExit):
                    batch.main(['--config', str(root/'config.yaml'), '--run-dir', str(root/'runs')])
                execute.assert_not_called()


class ComparisonCollectionTest(unittest.TestCase):
    def test_collector_uses_observed_distribution_and_detects_sample_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entries = []
            algorithms = ('optimized', 'kubernetes-default-scheduler')
            for algorithm in algorithms:
                run = root/algorithm
                run.mkdir()
                entries.append({'algorithm': algorithm, 'runDir': str(run),
                                'status': 'PASS', 'cleanupStatus': 'PASS'})
                (run/'placement_static_summary.json').write_text(json.dumps({
                    'status': 'PASS', 'distributions': {algorithm: {'pods': {'mean': 999}}}}))
                (run/'placement_actual_summary.json').write_text(json.dumps({
                    'status': 'PASS', 'distributions': {'pods': {'mean': 2, 'variance': 1}}}))
            (root/'comparison_runs.json').write_text(json.dumps({
                'status': 'PASS', 'algorithms': algorithms, 'runs': entries}))
            with mock.patch.object(collect, 'createSampleSignature', side_effect=[{'flow': 'a'}, {'flow': 'b'}]):
                result = collect.collectComparison(root)
            self.assertEqual(result['sampleComparison'], 'MISMATCH')
            self.assertEqual(result['rows'][1]['podMean'], 2)
            self.assertIsNone(result['rows'][1]['baselineTcpMeanMbps'])
            self.assertTrue((root/'comparison_metrics.csv').exists())

    def test_log_provisioning_counts_include_zero_nodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'deploy.orchestrator.log').write_text(
                '[vlan-cni] node=n1 ip=10.0.0.1 planned=4\n')
            result = collect.readProvisionedVlans(root, ['n1', 'n2'])
            self.assertEqual(result['perNode'], {'n1': 4, 'n2': 0})
            self.assertEqual(result['mean'], 2)
            self.assertEqual(result['variance'], 4)


if __name__ == '__main__':
    unittest.main()
