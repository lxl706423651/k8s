#!/usr/bin/env python3
"""Run one placement method or four serial B63 full-workflow experiments.

Input: existing compact KVM YAML and --algorithm (default all).
Output: frozen per-method configs, full stage/dataplane artifacts and comparison
JSON/CSV/Markdown. Actual runs create VMs, build/deploy workloads and measure
traffic. Serial runs destroy each successful cluster before the next method;
failures stop and retain resources. --prepare-only only writes local files.
Run from any directory using the same Python environment as the source run.
"""
from __future__ import annotations

import argparse
import copy
from pathlib import Path
import subprocess
import sys
import time

import yaml

import runB63LargeExperiment as b63
from collectPlacementComparison import collectComparison

SCRIPT_DIR = Path(__file__).resolve().parent
ALGORITHMS = (
    'optimized',
    'network-only',
    'kubernetes-default-scheduler',
    'pod-count-balanced',
)


def createMethodConfig(config: dict, source: Path, algorithm: str) -> dict:
    """Freeze paths in source config and select algorithm without changing resources."""
    frozen = b63.shared.resolveClusterInputPaths(config, source)
    adapted = b63.shared.createSingleConfigAssignment(config, source)
    for key in ('sourceRoot', 'topologyDir', 'canonicalOutputDir'):
        frozen['experiment'][key] = adapted['experiment'][key]
    frozen.setdefault('placement', {})['algorithm'] = algorithm
    frozen['placement']['selectedOnly'] = True
    b63.createPlacementArgs(frozen)
    return frozen


def executeCommand(command: list[str], log: Path) -> int:
    """Execute command from the example directory, saving merged output in log."""
    with log.open('w') as handle:
        return subprocess.run(command, cwd=SCRIPT_DIR, stdout=handle,
                              stderr=subprocess.STDOUT, check=False).returncode


def writeProgress(root: Path, progress: dict) -> None:
    """Persist batch progress under root before collecting available evidence."""
    b63.shared.writeJson(root / 'comparison_runs.json', progress)
    collectComparison(root)


def main(argv: list[str] | None = None) -> int:
    """Parse argv and run selected workflows serially; never destroy failed runs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=SCRIPT_DIR / 'configSingleHostKvmMacvlan.yaml')
    parser.add_argument('--algorithm', choices=('all', *ALGORITHMS), default='all')
    parser.add_argument('--run-dir', type=Path)
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--skipMeasureDataplane', action='store_true')
    parser.add_argument('--destroy-after-pass', action='store_true',
                        help='also destroy a single-method cluster; all always destroys successful rounds')
    args = parser.parse_args(argv)
    source = args.config.expanduser().resolve()
    config = b63.shared.loadYaml(source)
    algorithms = ALGORITHMS if args.algorithm == 'all' else (args.algorithm,)
    # A comparison creates fresh clusters; never adopt or remove existing VMs.
    if config.get('kvm', {}).get('allowExisting', False):
        parser.error('comparison requires kvm.allowExisting: false; existing clusters are not adopted')
    configs = {a: createMethodConfig(copy.deepcopy(config), source, a) for a in algorithms}
    root = (args.run_dir.expanduser().resolve() if args.run_dir else
            SCRIPT_DIR / 'experiments' / f'{config["experiment"]["topologySize"]}_{time.strftime("%Y%m%d_%H%M%S")}_comparison')
    if root.exists():
        parser.error(f'run directory already exists: {root}; use a new directory to preserve results')
    root.mkdir(parents=True)
    (root / 'source_input.yaml').write_text(source.read_text())
    progress = {'status': 'PREPARING' if args.prepare_only else 'RUNNING',
                'sourceConfig': str(source), 'python': sys.executable,
                'pythonVersion': sys.version, 'algorithms': list(algorithms),
                'createdAt': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'runs': []}
    writeProgress(root, progress)
    try:
        for algorithm in algorithms:
            run = root / algorithm
            run.mkdir()
            input_path = run / 'input.yaml'
            input_path.write_text(yaml.safe_dump(configs[algorithm], sort_keys=False))
            command = [sys.executable, str(SCRIPT_DIR / 'runB63LargeExperiment.py'),
                       '--config', str(input_path), '--run-dir', str(run)]
            if args.prepare_only:
                command.append('--prepare-only')
            if args.skipMeasureDataplane:
                command.append('--skipMeasureDataplane')
            row = {'algorithm': algorithm, 'runDir': str(run), 'command': command,
                   'status': 'RUNNING', 'cleanupStatus': 'NOT_REQUESTED'}
            progress['runs'].append(row)
            writeProgress(root, progress)
            print(f'[{algorithm}] starting; log: {run / "console.log"}', flush=True)
            code = executeCommand(command, run / 'console.log')
            row['exitCode'] = code
            summary = b63.shared.loadYaml(run / 'summary.json') if (run / 'summary.json').exists() else {}
            expected = 'PREPARED' if args.prepare_only else 'PASS'
            row['status'] = expected if code == 0 and summary.get('status') == expected else 'FAIL'
            writeProgress(root, progress)
            if row['status'] == 'FAIL':
                raise RuntimeError(f'{algorithm} failed (exit {code}); cluster retained, see {run / "console.log"}')
            if not args.prepare_only and (args.algorithm == 'all' or args.destroy_after_pass):
                row['cleanupStatus'] = 'RUNNING'
                cleanup = [sys.executable, str(SCRIPT_DIR / 'k8sTools.py'), 'destroy',
                           '-d', str(run / 'configK3s.yaml'), '--keep-temp']
                row['cleanupCommand'] = cleanup
                writeProgress(root, progress)
                print(f'[{algorithm}] passed; destroying its cluster before continuing', flush=True)
                row['cleanupExitCode'] = executeCommand(cleanup, run / 'destroy.log')
                row['cleanupStatus'] = 'PASS' if row['cleanupExitCode'] == 0 else 'FAIL'
                writeProgress(root, progress)
                if row['cleanupStatus'] == 'FAIL':
                    raise RuntimeError(f'{algorithm} cleanup failed; no later experiment started')
            print(f'[{algorithm}] {row["status"]}', flush=True)
        progress['status'] = 'PREPARED' if args.prepare_only else 'PASS'
    except KeyboardInterrupt:
        progress['status'] = 'INTERRUPTED'
        progress['failure'] = 'Interrupted; inspect current run and clean its cluster explicitly.'
        return 130
    except Exception as exc:
        progress['status'] = 'FAIL'
        progress['failure'] = str(exc)
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        progress['finishedAt'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
        writeProgress(root, progress)
    print(f'Comparison artifacts: {root / "comparison.md"}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
