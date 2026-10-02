#!/usr/bin/env python3
"""Check B63's shared workflow boundaries without creating cluster resources.

Inputs: local B62/B63 modules and a temporary synthetic Pod set.
Outputs: unittest results only; subprocess stage execution is mocked.
"""
import copy
from dataclasses import asdict
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'largeScale/b63_deployment'))
import runB63LargeExperiment as b63
import runPlacementExperiment as placement
import verifyPlacement
import verifyStaticPlacement as static


class SharedWorkflowTest(unittest.TestCase):
    def test_hooks_run_before_preload_and_before_bird(self):
        """Placement must precede preload, and live validation must gate routing."""
        events = []
        def record(name, *args, **kwargs):
            events.append(name)
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            env = {'B62_CONFIG_K3S_PATH': str(run/'missing')}
            with mock.patch.object(b63.shared, 'runStage', side_effect=record), \
                 mock.patch.object(b63.shared, 'runVerificationStage', side_effect=record), \
                 mock.patch.object(b63.shared, 'recordPlacementSummary'):
                b63.shared.runWorkloadStages({}, run/'summary.json', run, env,
                    after_compile=lambda *args: events.append('placement'),
                    after_ready=lambda *args: events.append('verify-placement'))
        self.assertEqual(events, ['clean','preflight','compile','placement','build',
            'deploy','wait-ready','verify-placement','start-bird',
            'verify-after-start-bird','start-kernel','verify-after-fib-write','reconvergence'])

    def test_failed_placement_never_preloads_or_deploys(self):
        events = []
        def fail(*args):
            raise RuntimeError('invalid placement')
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            with mock.patch.object(b63.shared, 'runStage', side_effect=lambda name,*a,**k: events.append(name)), \
                 mock.patch.object(b63.shared, 'recordPlacementSummary'):
                with self.assertRaises(RuntimeError):
                    b63.shared.runWorkloadStages({}, run/'summary.json', run, {}, after_compile=fail)
        self.assertNotIn('build', events)
        self.assertNotIn('deploy', events)

    def test_resume_build_preserves_placement_and_still_gates_routing(self):
        events = []
        def record(name, *args, **kwargs):
            events.append(name)
        def fail(*args):
            events.append('verify-placement')
            raise RuntimeError('wrong node')
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            (run/'output').mkdir()
            (run/'output/k8s.yaml').write_text('kind: Pod\n')
            with mock.patch.object(b63.shared, 'runStage', side_effect=record), \
                 mock.patch.object(b63.shared, 'runVerificationStage', side_effect=record):
                with self.assertRaisesRegex(RuntimeError, 'wrong node'):
                    b63.shared.runWorkloadStages({}, run/'summary.json', run, {},
                        resume_from_build=True, after_ready=fail)
        self.assertEqual(events, ['build','deploy','wait-ready','verify-placement'])

    def test_static_failure_blocks_preload_on_fresh_and_resumed_runs(self):
        """The static gate must precede any image/deployment operation on both paths."""
        for resume in (False, True):
            with self.subTest(resume=resume), tempfile.TemporaryDirectory() as tmp:
                run = Path(tmp)
                (run / 'output').mkdir()
                (run / 'output/k8s.yaml').write_text('kind: Pod\n')
                events = []
                with mock.patch.object(b63.shared, 'runStage', side_effect=lambda n,*a,**k: events.append(n)), \
                     mock.patch.object(b63.shared, 'recordPlacementSummary'):
                    with self.assertRaisesRegex(RuntimeError, 'bad static placement'):
                        b63.shared.runWorkloadStages({}, run/'summary.json', run, {},
                            resume_from_build=resume,
                            before_build=mock.Mock(side_effect=RuntimeError('bad static placement')))
                self.assertNotIn('build', events)
                self.assertNotIn('deploy', events)

    def test_compact_memory_and_isolation(self):
        path = b63.SCRIPT_DIR/'configSingleHostKvmMacvlan.yaml'
        config = yaml.safe_load(path.read_text())
        nodes = placement.loadNodesFromKvmConfig(path)
        self.assertEqual(len(nodes), 1 + config['workers']['count'])
        self.assertEqual(sum(n.cpu for n in nodes),
                         config['master']['vcpus'] + config['workers']['count'] * config['workers']['vcpus'])
        self.assertEqual(nodes[0].memoryMb, config['master']['memoryGiB'] * 1024)
        self.assertTrue(all(n.memoryMb == config['workers']['memoryGiB'] * 1024 for n in nodes[1:]))
        self.assertEqual(nodes[0].name, config['defaults']['masterName'])
        self.assertTrue(all(n.name.startswith(config['defaults']['workerNamePrefix']) for n in nodes[1:]))
        self.assertEqual(config['kvm']['networkCidr'], '10.83.0.0/24')
        options = b63.createPlacementArgs(config)
        self.assertIn('node-selector', options)
        self.assertNotIn('--inject-resource-requests', options)

    def test_live_check_rejects_wrong_missing_extra_and_unready_pods(self):
        report = {'metadata': {'selectedAlgorithm':'optimized'},
                  'placements': {'optimized': {'router1':'node1'}},
                  'pods': [{'name':'router1','replicas':1}]}
        pod = {'metadata': {'name':'router1-abc', 'labels':{'app':'router1'}},
               'spec':{'nodeName':'node1'},
               'status':{'conditions':[{'type':'Ready','status':'True'}]}}
        self.assertEqual(verifyPlacement.validatePlacement(report,[pod])['status'],'PASS')
        wrong = copy.deepcopy(pod)
        wrong['spec']['nodeName']='node2'
        unready = copy.deepcopy(pod)
        unready['status']={}
        for pods in ([], [wrong], [unready], [pod,pod]):
            with self.subTest(pods=pods):
                self.assertEqual(verifyPlacement.validatePlacement(report,pods)['status'],'FAIL')

    def test_pinning_preserves_networks_images_and_resources(self):
        doc = {'kind':'Pod','metadata':{'name':'r1','annotations':{
            'k8s.v1.cni.cncf.io/networks':'net1'}}, 'spec': {
            'containers':[{'name':'router','image':'example/router:one'}],
            'nodeSelector':{'kubernetes.io/hostname':'old','other':'keep'}}}
        pods = placement.parsePods([doc], {})
        before = copy.deepcopy(doc)
        placement.injectPlacement([doc], pods, {'r1':'new'}, inject_resources=False,
                                  pinning_mode='node-selector')
        self.assertEqual(doc['spec']['containers'],before['spec']['containers'])
        self.assertEqual(doc['metadata']['annotations']['k8s.v1.cni.cncf.io/networks'],'net1')
        self.assertEqual(doc['spec']['nodeSelector'],{'kubernetes.io/hostname':'new','other':'keep'})


class StaticPlacementTest(unittest.TestCase):
    """Reject corrupted artifacts using a two-node synthetic network fixture."""

    def setUp(self):
        self.args = placement.parseArgs(b63.createPlacementArgs({'placement': {}}))
        self.nodes = [placement.NodeInfo(name='n1', role='worker', cpu=8, memoryMb=24576),
                      placement.NodeInfo(name='n2', role='worker', cpu=8, memoryMb=24576)]
        self.source = [{'kind': 'NetworkAttachmentDefinition', 'metadata': {
            'name': 'net1', 'annotations': {placement.SEED_PREFIX+'vlan-id': '10'}}}]
        for i in (1, 2):
            self.source.append({'kind': 'Pod', 'metadata': {'name': f'r{i}', 'annotations': {
                'k8s.v1.cni.cncf.io/networks': 'net1'}}, 'spec': {
                'containers': [{'name': 'router', 'image': 'router:original'}],
                'nodeSelector': {'kubernetes.io/hostname': 'n1'}}})
        networks = placement.parseNetworks(self.source)
        pods = placement.parsePods(self.source, networks)
        placement.assignNetworkWeights(networks, pods, self.args)
        placement.estimatePodResources(pods, networks, self.args)
        mappings = {'optimized': {'r1':'n1', 'r2':'n2'}, 'manifest-existing': {'r1':'n1', 'r2':'n1'}}
        self.report = {'metadata': {'selectedAlgorithm': 'optimized'},
                       'pods': [asdict(p) | {'nicCount':p.nicCount} for p in pods],
                       'networks': {k:asdict(v) for k,v in networks.items()},
                       'nodes': [asdict(n) for n in self.nodes], 'placements': mappings,
                       'staticValidation': {k:placement.evaluatePlacementWithArgs(
                           pods, self.nodes, networks, v, self.args) for k,v in mappings.items()}}
        self.deployed = copy.deepcopy(self.source)
        placement.injectPlacement(self.deployed, pods, mappings['optimized'],
                                  inject_resources=False, pinning_mode='node-selector')

    def validateFixture(self):
        """Validate the current in-memory artifacts without cluster access."""
        return static.validateStaticPlacement(self.source, self.deployed, self.report,
                                              self.nodes, self.args)

    def test_valid_metrics_and_population_statistics(self):
        result = self.validateFixture()
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['distributions']['optimized']['pods']['variance'], 0)
        self.assertEqual(result['distributions']['manifest-existing']['pods']['variance'], 1)
        self.assertEqual(result['distributions']['optimized']['vlanParents']['total'], 2)

    def test_missing_or_unknown_assignments_are_rejected(self):
        original = copy.deepcopy(self.report)
        for mapping in ({'r1':'n1'}, {'r1':'n1','r2':'absent'}):
            self.report = copy.deepcopy(original)
            self.report['placements']['optimized'] = mapping
            self.assertEqual(self.validateFixture()['status'], 'FAIL')

    def test_manifest_image_or_selector_changes_are_rejected(self):
        original = copy.deepcopy(self.deployed)
        for field in ('image', 'selector'):
            self.deployed = copy.deepcopy(original)
            if field == 'image':
                self.deployed[1]['spec']['containers'][0]['image'] = 'unexpected:tag'
            else:
                self.deployed[1]['spec']['nodeSelector']['kubernetes.io/hostname'] = 'n2'
            self.assertEqual(self.validateFixture()['status'], 'FAIL')

    def test_modified_metrics_are_rejected(self):
        self.report['staticValidation']['optimized']['fabricMetrics']['vlanParentTotal'] += 1
        result = self.validateFixture()
        self.assertEqual(result['status'], 'FAIL')
        self.assertTrue(any('vlanParentTotal' in s for s in result['failures']))

    def test_excluded_control_plane_is_rejected(self):
        self.args.include_master = False
        self.nodes[0].schedulable = False
        self.report['nodes'][0]['schedulable'] = False
        self.assertEqual(self.validateFixture()['status'], 'FAIL')


class DataplaneWorkflowTest(unittest.TestCase):
    """Exercise the real runner lifecycle with cluster/subprocess actions replaced."""

    def invokeRun(self, root, flags=(), measure_rc=0):
        """Run B63 under root with CLI flags and a controlled measurement exit code."""
        config_path = root / 'input.yaml'
        run = root / 'run'
        if '--resume-from' not in flags:
            config = yaml.safe_load((b63.SCRIPT_DIR / 'configSingleHostKvmMacvlan.yaml').read_text())
            config['experiment']['namespace'] = 'isolated-dataplane-test'
            config['experiment']['topologyDir'] = str(root / 'topology-input')
            config_path.write_text(yaml.safe_dump(config))
        events, commands = [], []

        def recordCommand(name, cmd, **kwargs):
            """Stand in for the external measurement and emit its synthetic report."""
            events.append(name)
            commands.append(cmd)
            self.assertEqual(kwargs['summary']['status'], 'RUNNING')
            kwargs['summary']['stages'].append({
                'name': name, 'status': 'FAIL' if measure_rc else 'PASS',
                'exitCode': measure_rc, 'durationSeconds': 1.25,
            })
            if not measure_rc:
                report = run / 'dataplane/latency_throughput_summary.json'
                report.parent.mkdir(parents=True, exist_ok=True)
                # Flow failures are measurements, not a new orchestration gate.
                report.write_text(json.dumps({'ping': {'failureRate': 0.25},
                                             'stress': {'byLevel': {'48': {'rounds': 3}}}}))
            return measure_rc

        with mock.patch.object(b63.shared, 'buildCluster', side_effect=lambda *a: events.append('cluster')), \
             mock.patch.object(b63.shared, 'runWorkloadStages', side_effect=lambda *a, **k: events.append('workload')), \
             mock.patch.object(b63.shared, 'runReconvergenceStage', side_effect=lambda *a: events.append('reconvergence')), \
             mock.patch.object(b63.shared, 'runCommand', side_effect=recordCommand):
            try:
                rc = b63.main(['--config', str(config_path), '--run-dir', str(run), *flags])
            except SystemExit as exc:
                rc = exc.code
        return rc, json.loads((run / 'summary.json').read_text()), events, commands

    def test_default_measures_before_pass_with_run_specific_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rc, summary, events, commands = self.invokeRun(root)
            self.assertEqual(rc, 0)
            self.assertEqual(events, ['cluster', 'workload', 'dataplane'])
            self.assertEqual(summary['status'], 'PASS')
            cmd = commands[0]
            for flag, value in {
                '--namespace': 'isolated-dataplane-test',
                '--kubeconfig': str(root / 'run/kubeconfig.yaml'),
                '--artifact-dir': str(root / 'run'),
                '--topology-dir': str(root / 'topology-input'),
                '--stress-levels': '8,16,32,48', '--stress-repetitions': '3',
            }.items():
                self.assertEqual(cmd[cmd.index(flag) + 1], value)
            self.assertEqual(summary['tests']['dataplane']['ping']['failureRate'], 0.25)
            self.assertEqual(summary['stages'][-1]['durationSeconds'], 1.25)

    def test_explicit_skip_preserves_workload_and_records_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, summary, events, commands = self.invokeRun(Path(tmp), ['--skipMeasureDataplane'])
            self.assertEqual(rc, 0)
            self.assertEqual(events, ['cluster', 'workload'])
            self.assertEqual(commands, [])
            self.assertEqual(summary['stages'][-1]['status'], 'SKIPPED')
            self.assertEqual(summary['stages'][-1]['reason'], '--skipMeasureDataplane')

    def test_prepare_only_never_builds_or_measures(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, summary, events, commands = self.invokeRun(Path(tmp), ['--prepare-only'])
            self.assertEqual(rc, 0)
            self.assertEqual(summary['status'], 'PREPARED')
            self.assertEqual(events, [])
            self.assertEqual(commands, [])

    def test_measurement_failure_cannot_finish_as_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, summary, events, _ = self.invokeRun(Path(tmp), measure_rc=7)
            self.assertEqual(rc, 7)
            self.assertEqual(summary['status'], 'FAIL')
            self.assertEqual(summary['stages'][-1]['exitCode'], 7)
            self.assertEqual(events, ['cluster', 'workload', 'dataplane'])

    def test_resume_uses_frozen_inputs_and_runs_measurement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.invokeRun(root, ['--skipMeasureDataplane'])
            (root / 'input.yaml').unlink()
            rc, summary, events, commands = self.invokeRun(root, ['--resume-from', 'reconvergence'])
            self.assertEqual(rc, 0)
            self.assertEqual(events, ['reconvergence', 'dataplane'])
            self.assertEqual(summary['status'], 'PASS')
            self.assertIn('isolated-dataplane-test', commands[0])


if __name__ == '__main__':
    unittest.main()
