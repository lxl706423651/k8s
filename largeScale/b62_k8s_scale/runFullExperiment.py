#!/usr/bin/env python3
"""Run one complete B62 assignment experiment.

The complete experiment renders assignment.yaml, destroys any previously
recorded cluster, builds the requested KVM/K3s cluster, runs the workload
stages, starts BIRD, and enables kernel export. The post-start BGP test and
after-pass cluster destroy hooks are currently disabled in the main flow.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import yaml


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_ASSIGNMENT = SCRIPT_DIR / "assignment.yaml"


def findRepoRoot(start: Path) -> Path:
    """Return the SeedEMU repository root above start."""
    for candidate in (start, *start.parents):
        if (candidate / "setup.py").is_file() and (candidate / "seedemu").is_dir():
            return candidate
    raise RuntimeError(f"Cannot find SeedEMU repository root above {start}")


def resolvePath(value: str | Path, base: Path = SCRIPT_DIR) -> Path:
    """Resolve path text relative to base."""
    expanded = Path(os.path.expandvars(os.path.expanduser(str(value))))
    if expanded.is_absolute():
        return expanded.resolve()
    return (base / expanded).resolve()


def loadYaml(path: Path) -> dict[str, Any]:
    """Load one YAML mapping from path."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"Invalid YAML root in {path}: expected mapping")
    return data


def getNested(data: dict[str, Any], dotted: str, default: Any = None) -> Any:
    """Return a dotted-path YAML value or default."""
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def writeJson(path: Path, data: dict[str, Any]) -> None:
    """Write formatted JSON to path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def parseTopCpu() -> tuple[str, str, str]:
    """Return user, system, and idle CPU percentages from top."""
    try:
        result = subprocess.run(["top", "-bn1"], text=True, capture_output=True, timeout=5)
    except (subprocess.SubprocessError, FileNotFoundError):
        return ("na", "na", "na")
    line = ""
    for item in result.stdout.splitlines():
        if "Cpu(s)" in item or "%Cpu" in item:
            line = item
            break
    if not line:
        return ("na", "na", "na")

    def before(marker: str) -> str:
        if marker not in line:
            return "na"
        left = line.split(marker, 1)[0].strip()
        return left.split()[-1] if left.split() else "na"

    return (before("us,"), before("sy,"), before("id,"))


def memorySummary() -> tuple[int, int, int, float]:
    """Return total, available, used MiB, and used percent from /proc/meminfo."""
    values: dict[str, int] = {}
    with Path("/proc/meminfo").open("r", encoding="utf-8") as fh:
        for line in fh:
            key, rest = line.split(":", 1)
            if key in {"MemTotal", "MemAvailable"}:
                values[key] = int(rest.strip().split()[0])
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", 0)
    used = max(total - available, 0)
    used_pct = (used * 100 / total) if total else 0.0
    return (round(total / 1024), round(available / 1024), round(used / 1024), used_pct)


def appendLoadSample(log_path: Path, stage_name: str) -> None:
    """Append one host load/CPU/memory sample for a stage."""
    if not log_path.exists() or log_path.stat().st_size == 0:
        log_path.write_text(
            "Timestamp,Stage,ParentPID,Load_1,Load_5,Load_15,"
            "CPU_User,CPU_System,CPU_Idle,"
            "Mem_Total_MiB,Mem_Available_MiB,Mem_Used_MiB,Mem_Used_Pct\n",
            encoding="utf-8",
        )
    load_1, load_5, load_15 = Path("/proc/loadavg").read_text(encoding="utf-8").split()[:3]
    cpu_user, cpu_system, cpu_idle = parseTopCpu()
    mem_total, mem_available, mem_used, mem_used_pct = memorySummary()
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(
            f"{time.strftime('%Y-%m-%d %H:%M:%S')},{stage_name},{os.getpid()},"
            f"{load_1},{load_5},{load_15},{cpu_user},{cpu_system},{cpu_idle},"
            f"{mem_total},{mem_available},{mem_used},{mem_used_pct:.2f}\n"
        )


def startLoadMonitor(run_dir: Path, stage_name: str) -> tuple[threading.Event, threading.Thread]:
    """Start a 30-second host resource monitor for one orchestrated stage."""
    stop_event = threading.Event()
    log_path = run_dir / "loadAverage.log"

    def loop() -> None:
        while not stop_event.is_set():
            try:
                appendLoadSample(log_path, stage_name)
            except Exception:
                pass
            stop_event.wait(30)

    thread = threading.Thread(target=loop, name=f"load-monitor-{stage_name}", daemon=True)
    thread.start()
    return stop_event, thread


def finishRunningStages(summary: dict[str, Any], status: str, exit_code: int | None = None) -> None:
    """Mark any still-running summary stages with a terminal status."""
    now = time.time()
    end_text = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now))
    for stage in summary.get("stages", []):
        if not isinstance(stage, dict) or stage.get("status") != "RUNNING":
            continue
        start_text = stage.get("start")
        duration = None
        if isinstance(start_text, str):
            try:
                started = time.mktime(time.strptime(start_text, "%Y-%m-%dT%H:%M:%S%z"))
                duration = round(now - started, 2)
            except ValueError:
                duration = None
        stage["end"] = end_text
        stage["durationSeconds"] = duration
        stage["exitCode"] = exit_code
        stage["status"] = status


def createSummary(run_dir: Path, assignment: dict[str, Any]) -> dict[str, Any]:
    """Create the initial summary payload."""
    topology_size = int(getNested(assignment, "experiment.topologySize"))
    return {
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "finishedAt": None,
        "status": "RUNNING",
        "runDir": str(run_dir),
        "topologySize": topology_size,
        "workerCount": int(getNested(assignment, "experiment.workerCount")),
        "namespace": experimentNamespace(assignment),
        "stages": [],
        "tests": {},
        "destroyedAfterPass": False,
    }


def runCommand(
    name: str,
    cmd: list[str],
    *,
    cwd: Path,
    log_path: Path,
    summary: dict[str, Any],
    env: dict[str, str] | None = None,
    summary_path: Path,
) -> int:
    """Run one command, stream output to console and log, and record timing."""
    started = time.time()
    stage = {
        "name": name,
        "command": " ".join(cmd),
        "cwd": str(cwd),
        "log": str(log_path),
        "start": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(started)),
        "end": None,
        "durationSeconds": None,
        "exitCode": None,
        "status": "RUNNING",
    }
    summary["stages"].append(stage)
    writeJson(summary_path, summary)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    monitor_stop, monitor_thread = startLoadMonitor(log_path.parent, name)
    child_env = dict(env or os.environ)
    child_env["SEED_STAGE_LOAD_MONITOR_DISABLED"] = "1"
    process: subprocess.Popen[str] | None = None
    try:
        with log_path.open("a", encoding="utf-8", errors="replace") as log_fh:
            log_fh.write(f"\n===== {name} started {stage['start']} =====\n")
            log_fh.write(f"$ {' '.join(cmd)}\n")
            log_fh.flush()
            process = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                env=child_env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
            )
            assert process.stdout is not None
            for line in process.stdout:
                sys.stdout.write(line)
                log_fh.write(line)
            rc = process.wait()
            ended = time.time()
            stage["end"] = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ended))
            stage["durationSeconds"] = round(ended - started, 2)
            stage["exitCode"] = rc
            stage["status"] = "PASS" if rc == 0 else "FAIL"
            log_fh.write(f"===== {name} {stage['status']} {stage['durationSeconds']}s rc={rc} =====\n")
    except KeyboardInterrupt:
        with log_path.open("a", encoding="utf-8", errors="replace") as log_fh:
            if process is not None:
                process.terminate()
                try:
                    rc = process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    rc = process.wait()
            else:
                rc = 130
            ended = time.time()
            stage["end"] = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ended))
            stage["durationSeconds"] = round(ended - started, 2)
            stage["exitCode"] = rc
            stage["status"] = "INTERRUPTED"
            log_fh.write(f"===== {name} INTERRUPTED {stage['durationSeconds']}s rc={rc} =====\n")
            writeJson(summary_path, summary)
        raise
    finally:
        monitor_stop.set()
        monitor_thread.join(timeout=5)
    writeJson(summary_path, summary)
    return rc


def experimentNamespace(assignment: dict[str, Any]) -> str:
    """Return the namespace from assignment.yaml or derive it from topology size."""
    topology_size = int(getNested(assignment, "experiment.topologySize"))
    namespace = str(getNested(assignment, "experiment.namespace", "") or "").strip()
    return namespace or f"seedemu-b62-{topology_size}"


def createRuntimeEnv(assignment_path: Path, assignment: dict[str, Any], run_dir: Path) -> dict[str, str]:
    """Create the environment passed to B62 stage scripts without generated config files."""
    worker_count = int(getNested(assignment, "experiment.workerCount"))
    topology_size = int(getNested(assignment, "experiment.topologySize"))
    namespace = experimentNamespace(assignment)
    experiment_name = str(getNested(assignment, "experiment.name", "") or f"b62-scale-{topology_size}")
    source_root = resolvePath(getNested(assignment, "experiment.sourceRoot", findRepoRoot(SCRIPT_DIR)))
    topology_dir = resolvePath(getNested(assignment, "experiment.topologyDir", Path.home() / "seed-emulator" / "topology"))
    cluster_name = f"{getNested(assignment, 'kvm.clusterNamePrefix', 'seedemu-b62')}-w{worker_count}"
    master_ip = f"{getNested(assignment, 'kvm.ipPrefix')}.{int(getNested(assignment, 'kvm.master.ipStart'))}"
    registry_port = int(getNested(assignment, "registry.port", 5000))
    network_backend = str(getNested(assignment, "networking.backend", "kube-ovn")).strip().lower() or "kube-ovn"
    cni_type = str(getNested(assignment, "networking.cniType", "kube-ovn") or "kube-ovn").strip().lower()
    local_link_cni_type = str(getNested(assignment, "networking.localLinkCniType", cni_type) or cni_type).strip().lower()
    attached_cni_type = str(getNested(assignment, "networking.attachedCniType", network_backend) or network_backend).strip().lower()

    env = os.environ.copy()
    env.update(
        {
            "B62_ASSIGNMENT_FILE": str(assignment_path),
            "B62_WORKER_COUNT": str(worker_count),
            "B62_TOPOLOGY_SIZE": str(topology_size),
            "B62_EXPERIMENT_NAME": experiment_name,
            "B62_CONFIG_KVM_OVN_PATH": str(SCRIPT_DIR / "configKvmOvn.yaml"),
            "B62_CONFIG_K3S_PATH": str(SCRIPT_DIR / "configK3s.yaml"),
            "B62_KUBECONFIG_PATH": str(SCRIPT_DIR / "kubeconfig.yaml"),
            "B62_INVENTORY_PATH": str(SCRIPT_DIR / "cluster.inventory.yaml"),
            "B62_RESOURCE_PLAN_YAML": str(SCRIPT_DIR / "resourcePlan.yaml"),
            "B62_RESOURCE_PLAN_JSON": str(SCRIPT_DIR / "resourcePlan.json"),
            "SEED_SOURCE_ROOT": str(source_root),
            "SEED_TOPOLOGY_SIZE": str(topology_size),
            "SEED_K3S_CLUSTER_NAME": cluster_name,
            "SEED_CLUSTER_INVENTORY": cluster_name,
            "SEED_CLUSTER_INVENTORY_PATH": str(SCRIPT_DIR / "cluster.inventory.yaml"),
            "SEED_KUBECONFIG_PATH": str(SCRIPT_DIR / "kubeconfig.yaml"),
            "KUBECONFIG": str(SCRIPT_DIR / "kubeconfig.yaml"),
            "SEED_K3S_USER": str(getNested(assignment, "kvm.ssh.user", "ubuntu")),
            "SEED_K3S_SSH_KEY": str(resolvePath(getNested(assignment, "kvm.ssh.key", "~/.ssh/id_ed25519"))),
            "SEED_NAMESPACE": namespace,
            "SEED_REAL_TOPOLOGY_DIR": str(topology_dir),
            "SEED_K3S_MASTER_IP": master_ip,
            "SEED_REGISTRY_HOST": master_ip,
            "SEED_REGISTRY_PORT": str(registry_port),
            "SEED_REGISTRY": f"{master_ip}:{registry_port}",
            "SEED_NETWORK_BACKEND": network_backend,
            "SEED_CNI_TYPE": cni_type,
            "SEED_LOCAL_LINK_CNI_TYPE": local_link_cni_type,
            "SEED_ATTACHED_CNI_TYPE": attached_cni_type,
            "SEED_CNI_MASTER_INTERFACE": str(getNested(assignment, "networking.cniMasterInterface", "ens2")),
        }
    )
    return env


def runStage(
    name: str,
    cmd: list[str],
    *,
    summary: dict[str, Any],
    summary_path: Path,
    log_path: Path,
    env: dict[str, str] | None = None,
) -> None:
    """Run a required stage and abort on non-zero exit."""
    rc = runCommand(name, cmd, cwd=SCRIPT_DIR, log_path=log_path, summary=summary, env=env, summary_path=summary_path)
    if rc != 0:
        summary["status"] = "FAIL"
        summary["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        writeJson(summary_path, summary)
        raise SystemExit(rc)


def maybeDestroyExisting(summary: dict[str, Any], summary_path: Path, run_dir: Path, env: dict[str, str]) -> None:
    """Destroy the currently recorded cluster before building a fresh assignment cluster."""
    config = SCRIPT_DIR / "configK3s.yaml"
    if not config.exists():
        summary["stages"].append({
            "name": "destroy-existing-cluster",
            "status": "SKIPPED",
            "reason": "configK3s.yaml not found",
            "durationSeconds": 0,
            "exitCode": 0,
        })
        writeJson(summary_path, summary)
        return
    runStage(
        "destroy-existing-cluster",
        ["python3", "k8sTools.py", "destroy", "-d", str(config), "--keep-temp"],
        summary=summary,
        summary_path=summary_path,
        log_path=run_dir / "destroy-existing-cluster.log",
        env=env,
    )


def buildCluster(summary: dict[str, Any], summary_path: Path, run_dir: Path, env: dict[str, str]) -> None:
    """Build the KVM/K3s cluster from generated assignment config."""
    config_kvm = env["B62_CONFIG_KVM_OVN_PATH"]
    config_k3s = env["B62_CONFIG_K3S_PATH"]
    kubeconfig = env["B62_KUBECONFIG_PATH"]
    inventory = env["B62_INVENTORY_PATH"]
    runStage(
        "prepare-libvirt-dhcp",
        ["sudo", "-n", "python3", "prepareLibvirtDhcp.py", config_kvm],
        summary=summary,
        summary_path=summary_path,
        log_path=run_dir / "prepare-libvirt-dhcp.log",
        env=env,
    )
    runStage(
        "build-cluster",
        [
            "python3",
            "k8sTools.py",
            "build",
            "--input",
            config_kvm,
            "--config-k3s",
            config_k3s,
            "--kubeconfig",
            kubeconfig,
            "--inventory",
            inventory,
            "--keep-temp",
        ],
        summary=summary,
        summary_path=summary_path,
        log_path=run_dir / "build-cluster.log",
        env=env,
    )


def runWorkloadStages(summary: dict[str, Any], summary_path: Path, run_dir: Path, env: dict[str, str]) -> None:
    """Run B62 workload stages on the built cluster."""
    stages = [
        ("clean", "clean.sh"),
        ("preflight", "preflight.sh"),
        ("compile", "compile.sh"),
        ("build", "build.sh"),
        ("deploy", "deploy.sh"),
        ("wait-ready", "wait-ready.sh"),
        ("start-bird", "start_bird.sh"),
        ("start-kernel", "start_bird_kernel.sh"),
    ]
    for name, script in stages:
        runStage(
            name,
            [str(SCRIPT_DIR / script), str(run_dir)],
            summary=summary,
            summary_path=summary_path,
            log_path=run_dir / f"{name}.orchestrator.log",
            env=env,
        )


def runBgpTest(summary: dict[str, Any], summary_path: Path, run_dir: Path, env: dict[str, str]) -> None:
    """Run BGP protocol validation after the start stages."""
    runStage(
        "test-bgp",
        ["python3", "test.py", env["SEED_NAMESPACE"], str(run_dir)],
        summary=summary,
        summary_path=summary_path,
        log_path=run_dir / "test-bgp.log",
        env=env,
    )
    test_summary_path = run_dir / "bgp_test_summary.json"
    if test_summary_path.exists():
        test_summary = json.loads(test_summary_path.read_text(encoding="utf-8"))
        summary["tests"]["bgp"] = test_summary
        if test_summary.get("status") != "PASS":
            summary["status"] = "FAIL"
            writeJson(summary_path, summary)
            raise SystemExit(1)
        writeJson(summary_path, summary)


def destroyAfterPass(summary: dict[str, Any], summary_path: Path, run_dir: Path, env: dict[str, str]) -> None:
    """Destroy the assignment cluster after a fully successful experiment."""
    runStage(
        "destroy-cluster-after-pass",
        ["python3", "k8sTools.py", "destroy", "-d", env["B62_CONFIG_K3S_PATH"], "--keep-temp"],
        summary=summary,
        summary_path=summary_path,
        log_path=run_dir / "destroy-cluster-after-pass.log",
        env=env,
    )
    summary["destroyedAfterPass"] = True
    writeJson(summary_path, summary)


def parseArgs() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("assignment_arg", nargs="?", type=Path, help="assignment YAML path")
    parser.add_argument("--assignment", type=Path)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--skip-existing-destroy", action="store_true")
    parser.add_argument("--skip-final-destroy", action="store_true", help="compatibility flag; final destroy is currently disabled")
    return parser.parse_args()


def main() -> int:
    """Run the full assignment experiment."""
    args = parseArgs()
    assignment_path = resolvePath(args.assignment or args.assignment_arg or DEFAULT_ASSIGNMENT)
    assignment = loadYaml(assignment_path)
    topology_size = int(getNested(assignment, "experiment.topologySize"))
    worker_count = int(getNested(assignment, "experiment.workerCount"))
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    run_dir = resolvePath(args.run_dir) if args.run_dir else SCRIPT_DIR / "runs" / f"{timestamp}_{topology_size}_w{worker_count}"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "topology_size").write_text(f"{topology_size}\n", encoding="utf-8")
    summary_path = run_dir / "summary.json"
    summary = createSummary(run_dir, assignment)
    writeJson(summary_path, summary)

    try:
        runStage(
            "render-assignment",
            ["python3", "renderAssignmentConfig.py", "--assignment", str(assignment_path), "--run-dir", str(run_dir), "--timestamp", timestamp, "prepare"],
            summary=summary,
            summary_path=summary_path,
            log_path=run_dir / "render-assignment.log",
        )
        shutil.copy2(assignment_path, run_dir / "assignment.yaml")
        env = createRuntimeEnv(run_dir / "assignment.yaml", assignment, run_dir)

        if not args.skip_existing_destroy:
            maybeDestroyExisting(summary, summary_path, run_dir, env)

        buildCluster(summary, summary_path, run_dir, env)
        runWorkloadStages(summary, summary_path, run_dir, env)
        # test-bgp is intentionally disabled in the current manual flow.
        # runBgpTest(summary, summary_path, run_dir, env)
        # destroy-cluster-after-pass is intentionally disabled in the current manual flow.
        # if not args.skip_final_destroy:
        #     destroyAfterPass(summary, summary_path, run_dir, env)

        summary["status"] = "PASS"
        summary["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        writeJson(summary_path, summary)
        print(f"Complete B62 experiment passed: {run_dir}")
        return 0
    except KeyboardInterrupt:
        finishRunningStages(summary, "INTERRUPTED", 130)
        summary["status"] = "INTERRUPTED"
        summary["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        writeJson(summary_path, summary)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
