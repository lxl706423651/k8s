#!/usr/bin/env python3
"""Run a B62 experiment from a unified single-host YAML or legacy assignment.

The complete experiment renders assignment.yaml, destroys any previously
recorded cluster, builds the requested KVM/K3s cluster, runs the workload
stages, starts BIRD, and enables kernel export. The post-start BGP test and
after-pass cluster destroy hooks are currently disabled in the main flow.
--config freezes one user YAML into run-local cluster/assignment snapshots;
--prepare-only validates and writes those snapshots without cluster commands.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

import yaml

from performanceMetrics import PerformanceMonitor, summarizePerformanceMetrics

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "seedemu/k8sTools/resources/setup"))
from normalizeResources import memoryMiB


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


def canonicalClusterKind(config: dict[str, Any]) -> str:
    """Return the infrastructure workflow kind without coupling it to CNI."""
    raw = str(config.get("kind") or "").strip()
    aliases = {
        "kvmOvn": "kvm",
        "physicalOvn": "physical",
        "multiHostKvmOvn": "multiHostKvm",
    }
    kind = aliases.get(raw, raw)
    if kind not in {"kvm", "physical", "multiHostKvm"}:
        raise SystemExit(f"Unsupported cluster config kind: {raw or '<missing>'}")
    return kind


def writeJson(path: Path, data: dict[str, Any]) -> None:
    """Write formatted JSON to path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def createSingleConfigAssignment(config: dict[str, Any], source: Path) -> dict[str, Any]:
    """Adapt one compact single-host config to the legacy stage contract.

    config is the sole user input; source anchors relative experiment paths.
    This function never reads assignment.yaml or touches cluster resources.
    """
    if canonicalClusterKind(config) != "kvm" or config.get("nodes"):
        raise ValueError("--config requires a single-host compact master/workers KVM plan")
    assignment = copy.deepcopy(config)
    experiment = assignment.setdefault("experiment", {})
    if not isinstance(experiment, dict):
        raise ValueError("experiment must be a mapping")
    size = experiment.get("topologySize")
    workers = getNested(config, "workers.count")
    for name, value, minimum in (("experiment.topologySize", size, 1),
                                  ("workers.count", workers, 0)):
        if type(value) is not int or value < minimum:
            raise ValueError(f"{name} must be an integer >= {minimum}")
    if "workerCount" in experiment:
        raise ValueError("Remove experiment.workerCount; it is derived from workers.count")
    experiment["workerCount"] = workers
    experiment.setdefault("name", f"b62-macvlan-vlan-{size}")
    experiment.setdefault("namespace", f"seedemu-b62-mvlan-vlan-{size}")
    experiment["sourceRoot"] = str(resolvePath(
        experiment.get("sourceRoot", findRepoRoot(SCRIPT_DIR)), source.parent))
    experiment["topologyDir"] = str(resolvePath(
        experiment.get("topologyDir", Path.home() / "seed-emulator/topology"), source.parent))
    experiment["canonicalOutputDir"] = str(resolvePath(
        experiment.get("canonicalOutputDir", Path(experiment["topologyDir"]) / f"output_{size}"),
        source.parent))
    # Only the internal snapshot needs these legacy address aliases.
    kvm = assignment.setdefault("kvm", {})
    kvm["ipPrefix"] = getNested(config, "defaults.ipPrefix", "192.168.122")
    kvm["master"] = {"ipStart": getNested(config, "defaults.masterIpStart", 110)}
    kvm["ssh"] = copy.deepcopy(config.get("ssh", {}))
    kvm["ssh"]["key"] = str(resolvePath(kvm["ssh"].get("key", "~/.ssh/id_ed25519"), source.parent))
    fabric = config.get("fabric", {})
    cni = config.get("cni", {})
    backend = str(fabric.get("type", "macvlan-vlan")).removesuffix("-vlan")
    trunks = copy.deepcopy(fabric.get("vlanTrunks", []))
    interface = cni.get("defaultMasterInterface") or (trunks[0]["masterInterface"] if trunks else "ens3")
    assignment["networking"] = {
        "backend": backend,
        "cniType": cni.get("cniType", backend),
        "localLinkCniType": cni.get("localLinkCniType", cni.get("cniType", backend)),
        "attachedCniType": cni.get("attachedCniType", cni.get("cniType", backend)),
        "cniMasterInterface": interface,
        "cniMtu": fabric.get("mtu", 0),
        "macvlanVlanMode": str(fabric.get("type", "")).endswith("-vlan"),
        "macvlanVlanIdStart": trunks[0].get("vlanStart", 1) if trunks else 1,
        "vlanTrunks": trunks,
    }
    return assignment


def resolveClusterInputPaths(config: dict[str, Any], source: Path) -> dict[str, Any]:
    """Freeze compact KVM input paths before copying config away from source."""
    result = copy.deepcopy(config)
    for section, keys in {
        "ssh": ("key",),
        "kvm": ("storageDir", "diskDir", "cloudInitDir", "baseImagePath", "legacyBaseImagePath"),
        "seedemu": ("hostImageCacheDir",),
    }.items():
        for key in keys:
            if getNested(result, f"{section}.{key}"):
                result[section][key] = str(resolvePath(result[section][key], source.parent))
    if getNested(result, "seedemu.imageCacheDirs"):
        result["seedemu"]["imageCacheDirs"] = [
            str(resolvePath(path, source.parent)) for path in result["seedemu"]["imageCacheDirs"]]
    return result


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


def createSummary(
    run_dir: Path,
    assignment: dict[str, Any],
    cluster_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create the initial summary payload."""
    topology_size = int(getNested(assignment, "experiment.topologySize"))
    worker_count = int(getNested(assignment, "experiment.workerCount"))
    cluster_plan: dict[str, Any] | None = None
    if cluster_input is not None:
        kind = canonicalClusterKind(cluster_input)
        if kind == "multiHostKvm":
            hypervisors = cluster_input.get("hypervisors")
            host_records = hypervisors if isinstance(hypervisors, list) else []
            vm_count = sum(
                int(host.get("vmCount") or 0)
                for host in host_records
                if isinstance(host, dict)
            )
            worker_count = max(vm_count - 1, 0)
            cluster_plan = {
                "kind": kind,
                "clusterName": str(cluster_input.get("clusterName") or ""),
                "hypervisorCount": len(host_records),
                "vmCount": vm_count,
                "workerCount": worker_count,
            }
            master = (
                cluster_input.get("master")
                if isinstance(cluster_input.get("master"), dict)
                else {}
            )
            workers = (
                cluster_input.get("workers")
                if isinstance(cluster_input.get("workers"), dict)
                else {}
            )
            master_vcpus = int(master.get("vcpus") or 0)
            worker_vcpus = int(workers.get("vcpus") or 0)
            master_memory_mib = memoryMiB(master)
            worker_memory_mib = memoryMiB(workers)
            if master_vcpus and worker_vcpus:
                cluster_plan["totalVcpus"] = (
                    master_vcpus + worker_count * worker_vcpus
                )
            if master_memory_mib and worker_memory_mib:
                total_memory_mib = (
                    master_memory_mib + worker_count * worker_memory_mib
                )
                cluster_plan["totalMemoryMiB"] = total_memory_mib
                if total_memory_mib % 1024 == 0:
                    cluster_plan["totalMemoryGiB"] = total_memory_mib // 1024
            master_disk = int(master.get("diskGb") or 0)
            worker_disk = int(workers.get("diskGb") or 0)
            if master_disk and master_disk == worker_disk:
                cluster_plan["diskGiBPerVm"] = master_disk
        elif kind == "kvm":
            nodes = cluster_input.get("nodes")
            node_records = nodes if isinstance(nodes, list) else []
            if node_records:
                worker_count = sum(
                    1
                    for node in node_records
                    if isinstance(node, dict)
                    and str(node.get("role") or "").lower() not in {"master", "control-plane"}
                )
                vm_count = len(node_records)
                total_vcpus = sum(
                    int(node.get("vcpus") or 0)
                    for node in node_records
                    if isinstance(node, dict)
                )
                total_memory_mib = sum(
                    memoryMiB(node)
                    for node in node_records
                    if isinstance(node, dict)
                )
                disk_sizes = {
                    int(node.get("diskGb") or node.get("disk_gb") or 0)
                    for node in node_records
                    if isinstance(node, dict)
                }
            else:
                master = (
                    cluster_input.get("master")
                    if isinstance(cluster_input.get("master"), dict)
                    else {}
                )
                workers = (
                    cluster_input.get("workers")
                    if isinstance(cluster_input.get("workers"), dict)
                    else {}
                )
                worker_count = int(workers.get("count") or 0)
                vm_count = worker_count + (1 if master else 0)
                total_vcpus = int(master.get("vcpus") or 0) + worker_count * int(
                    workers.get("vcpus") or 0
                )
                total_memory_mib = memoryMiB(master) + worker_count * memoryMiB(workers)
                disk_sizes = {
                    int(master.get("diskGb") or master.get("disk_gb") or 0),
                    int(workers.get("diskGb") or workers.get("disk_gb") or 0),
                }
                disk_sizes.discard(0)
            cluster_plan = {
                "kind": kind,
                "clusterName": str(cluster_input.get("clusterName") or ""),
                "hypervisorCount": 1,
                "vmCount": vm_count,
                "workerCount": worker_count,
            }
            if total_vcpus:
                cluster_plan["totalVcpus"] = total_vcpus
            if total_memory_mib:
                cluster_plan["totalMemoryMiB"] = total_memory_mib
                cluster_plan["totalMemoryGiB"] = total_memory_mib / 1024
            if len(disk_sizes) == 1:
                cluster_plan["diskGiBPerVm"] = next(iter(disk_sizes))
        else:
            nodes = cluster_input.get("nodes")
            node_records = nodes if isinstance(nodes, list) else []
            worker_count = sum(
                1
                for node in node_records
                if isinstance(node, dict)
                and str(node.get("role") or "").lower() not in {"master", "control-plane"}
            )
            cluster_plan = {
                "kind": kind,
                "clusterName": str(cluster_input.get("clusterName") or ""),
                "vmCount": None,
                "workerCount": worker_count,
            }
    summary = {
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "finishedAt": None,
        "status": "RUNNING",
        "runDir": str(run_dir),
        "topologySize": topology_size,
        "workerCount": worker_count,
        "namespace": experimentNamespace(assignment),
        "stages": [],
        "tests": {},
        "destroyedAfterPass": False,
    }
    if cluster_plan is not None:
        summary["clusterPlan"] = cluster_plan
    return summary


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
    performance_monitor = PerformanceMonitor(log_path.parent, name, intervalSeconds=5)
    performance_monitor.start()
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
                sys.stdout.flush()
                log_fh.flush()
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
        performance_monitor.stop()
        monitor_stop.set()
        monitor_thread.join(timeout=5)
    writeJson(summary_path, summary)
    return rc


def experimentNamespace(assignment: dict[str, Any]) -> str:
    """Return the namespace from assignment.yaml or derive it from topology size."""
    topology_size = int(getNested(assignment, "experiment.topologySize"))
    namespace = str(getNested(assignment, "experiment.namespace", "") or "").strip()
    return namespace or f"seedemu-b62-{topology_size}"


def createRuntimeEnv(
    assignment_path: Path,
    assignment: dict[str, Any],
    run_dir: Path,
    *,
    cluster_input: Path | None = None,
    config_k3s: Path = SCRIPT_DIR / "configK3s.yaml",
    kubeconfig: Path = SCRIPT_DIR / "kubeconfig.yaml",
    inventory: Path = SCRIPT_DIR / "cluster.inventory.yaml",
) -> dict[str, str]:
    """Create the environment passed to B62 cluster and workload stages."""
    worker_count = int(getNested(assignment, "experiment.workerCount"))
    topology_size = int(getNested(assignment, "experiment.topologySize"))
    namespace = experimentNamespace(assignment)
    experiment_name = str(getNested(assignment, "experiment.name", "") or f"b62-scale-{topology_size}")
    source_root = resolvePath(getNested(assignment, "experiment.sourceRoot", findRepoRoot(SCRIPT_DIR)))
    topology_dir = resolvePath(getNested(assignment, "experiment.topologyDir", Path.home() / "seed-emulator" / "topology"))
    canonical_output_dir = resolvePath(
        getNested(
            assignment,
            "experiment.canonicalOutputDir",
            topology_dir / f"output_{topology_size}",
        )
    )
    cluster_name = f"{getNested(assignment, 'kvm.clusterNamePrefix', 'seedemu-b62')}-w{worker_count}"
    master_ip = f"{getNested(assignment, 'kvm.ipPrefix')}.{int(getNested(assignment, 'kvm.master.ipStart'))}"
    registry_port = int(getNested(assignment, "registry.port", 5000))
    network_backend = str(getNested(assignment, "networking.backend", "macvlan")).strip().lower() or "macvlan"
    cni_type = str(getNested(assignment, "networking.cniType", "macvlan") or "macvlan").strip().lower()
    local_link_cni_type = str(getNested(assignment, "networking.localLinkCniType", cni_type) or cni_type).strip().lower()
    attached_cni_type = str(getNested(assignment, "networking.attachedCniType", local_link_cni_type) or local_link_cni_type).strip().lower()
    vlan_capable_cni = cni_type.replace("_", "-") in {"macvlan", "ipvlan", "bridge"}
    vlan_backend = network_backend.replace("_", "-") in {
        "macvlan",
        "macvlan-vlan",
        "ipvlan",
        "ipvlan-vlan",
        "bridge",
        "bridge-vlan",
    }
    macvlan_vlan_mode = getNested(assignment, "networking.macvlanVlanMode", None)
    if isinstance(macvlan_vlan_mode, str):
        macvlan_vlan_enabled = macvlan_vlan_mode.strip().lower() in {"true", "1", "yes", "on"}
    elif macvlan_vlan_mode is None:
        macvlan_vlan_enabled = vlan_backend and vlan_capable_cni
    else:
        macvlan_vlan_enabled = bool(macvlan_vlan_mode)
    macvlan_vlan_start = int(getNested(assignment, "networking.macvlanVlanIdStart", 100) or 100)
    vlan_trunks_raw = getNested(assignment, "networking.vlanTrunks", [])
    vlan_trunks = vlan_trunks_raw if isinstance(vlan_trunks_raw, list) else []
    cni_mtu = int(getNested(assignment, "networking.cniMtu", 0) or 0)
    if cluster_input is not None:
        cluster_data = loadYaml(cluster_input)
        cni_mtu = int(getNested(cluster_data, "fabric.mtu", cni_mtu) or 0)

    env = os.environ.copy()
    env.update(
        {
            "B62_ASSIGNMENT_FILE": str(assignment_path),
            "B62_WORKER_COUNT": str(worker_count),
            "B62_TOPOLOGY_SIZE": str(topology_size),
            "B62_EXPERIMENT_NAME": experiment_name,
            "B62_CONFIG_KVM_PATH": str(cluster_input or SCRIPT_DIR / "configKvm.yaml"),
            "B62_CONFIG_K3S_PATH": str(config_k3s),
            "B62_KUBECONFIG_PATH": str(kubeconfig),
            "B62_INVENTORY_PATH": str(inventory),
            "B62_RESOURCE_PLAN_YAML": str(SCRIPT_DIR / "resourcePlan.yaml"),
            "B62_RESOURCE_PLAN_JSON": str(SCRIPT_DIR / "resourcePlan.json"),
            "SEED_SOURCE_ROOT": str(source_root),
            "SEED_TOPOLOGY_SIZE": str(topology_size),
            "SEED_K3S_CLUSTER_NAME": cluster_name,
            "SEED_CLUSTER_INVENTORY": cluster_name,
            "SEED_CLUSTER_INVENTORY_PATH": str(inventory),
            "SEED_KUBECONFIG_PATH": str(kubeconfig),
            "KUBECONFIG": str(kubeconfig),
            "SEED_K3S_USER": str(getNested(assignment, "kvm.ssh.user", "ubuntu")),
            "SEED_K3S_SSH_KEY": str(resolvePath(getNested(assignment, "kvm.ssh.key", "~/.ssh/id_ed25519"))),
            "SEED_NAMESPACE": namespace,
            "SEED_REAL_TOPOLOGY_DIR": str(topology_dir),
            "SEED_CANONICAL_OUTPUT_DIR": str(canonical_output_dir),
            "SEED_K3S_MASTER_IP": master_ip,
            "SEED_REGISTRY_HOST": master_ip,
            "SEED_REGISTRY_PORT": str(registry_port),
            "SEED_REGISTRY": f"{master_ip}:{registry_port}",
            "SEED_NETWORK_BACKEND": network_backend,
            "SEED_CNI_TYPE": cni_type,
            "SEED_LOCAL_LINK_CNI_TYPE": local_link_cni_type,
            "SEED_ATTACHED_CNI_TYPE": attached_cni_type,
            "SEED_CNI_MASTER_INTERFACE": str(getNested(assignment, "networking.cniMasterInterface", "ens2")),
            "SEED_CNI_MTU": str(cni_mtu) if cni_mtu else "",
            "SEED_MACVLAN_VLAN_MODE": "1" if macvlan_vlan_enabled else "0",
            "SEED_MACVLAN_VLAN_START": str(macvlan_vlan_start),
            "SEED_MACVLAN_VLAN_TRUNKS_JSON": json.dumps(vlan_trunks, separators=(",", ":")),
            "SEED_VERIFY_LOAD_THRESHOLD": str(
                getNested(assignment, "verification.loadThreshold", 50)
            ),
            "SEED_VERIFY_LOAD_CHECK_INTERVAL_SECONDS": str(
                getNested(assignment, "verification.loadCheckIntervalSeconds", 30)
            ),
            "SEED_VERIFY_LOAD_MAX_WAIT_SECONDS": str(
                getNested(assignment, "verification.loadMaxWaitSeconds", 36000)
            ),
            "SEED_VERIFY_PROTOCOL_WAIT_SECONDS": str(
                getNested(assignment, "verification.protocolWaitSeconds", 1800)
            ),
            "SEED_VERIFY_PROTOCOL_CHECK_INTERVAL_SECONDS": str(
                getNested(assignment, "verification.protocolCheckIntervalSeconds", 30)
            ),
            "SEED_VERIFY_FIB_SAMPLE_AS_COUNT": str(
                getNested(assignment, "verification.fibSampleAsCount", 0)
            ),
            "SEED_RECONVERGENCE_CHAOS_TARGETS_JSON": json.dumps(
                getNested(assignment, "reconvergence.chaosTargetPrefixes", []),
                separators=(",", ":"),
            ),
            "SEED_RECONVERGENCE_VERIFY_TARGETS_JSON": json.dumps(
                getNested(assignment, "reconvergence.verifyTargetPrefixes", []),
                separators=(",", ":"),
            ),
            "SEED_RECONVERGENCE_LOAD_THRESHOLD": str(
                getNested(assignment, "reconvergence.loadThreshold", 50)
            ),
            "SEED_RECONVERGENCE_LOAD_CHECK_INTERVAL_SECONDS": str(
                getNested(assignment, "reconvergence.loadCheckIntervalSeconds", 5)
            ),
            "SEED_RECONVERGENCE_ROUTE_CHECK_INTERVAL_SECONDS": str(
                getNested(assignment, "reconvergence.routeCheckIntervalSeconds", 2)
            ),
            "SEED_RECONVERGENCE_PHASE_TIMEOUT_SECONDS": str(
                getNested(assignment, "reconvergence.phaseTimeoutSeconds", 1800)
            ),
        }
    )
    if cluster_input is not None:
        env["B62_CLUSTER_INPUT_CONFIG"] = str(cluster_input)
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


def runNonBlockingStage(
    name: str,
    cmd: list[str],
    *,
    summary: dict[str, Any],
    summary_path: Path,
    log_path: Path,
    env: dict[str, str] | None = None,
) -> int:
    """Run one diagnostic stage and continue even if it reports failure."""
    rc = runCommand(name, cmd, cwd=SCRIPT_DIR, log_path=log_path, summary=summary, env=env, summary_path=summary_path)
    if rc != 0:
        failures = summary.setdefault("nonBlockingStageFailures", [])
        if isinstance(failures, list):
            failures.append({"name": name, "exitCode": rc, "log": str(log_path)})
        writeJson(summary_path, summary)
    return rc


def recordTestSummary(
    summary: dict[str, Any],
    summary_path: Path,
    run_dir: Path,
    key: str,
    filename: str,
) -> None:
    """Copy a verification JSON payload into the orchestrator summary."""
    test_path = run_dir / filename
    if not test_path.exists():
        summary.setdefault("tests", {})[key] = {"success": False, "missing": str(test_path)}
        writeJson(summary_path, summary)
        return
    summary.setdefault("tests", {})[key] = json.loads(test_path.read_text(encoding="utf-8"))
    writeJson(summary_path, summary)


def recordPlacementSummary(
    summary: dict[str, Any],
    summary_path: Path,
    run_dir: Path,
) -> None:
    """Record compact deterministic placement statistics after compile."""
    plan_path = run_dir / "placement_plan.json"
    if not plan_path.exists():
        summary["staticPlacement"] = {
            "recorded": False,
            "missing": str(plan_path),
        }
        writeJson(summary_path, summary)
        return
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    nodes = plan.get("nodes") if isinstance(plan.get("nodes"), list) else []
    assignments = (
        plan.get("assignments")
        if isinstance(plan.get("assignments"), dict)
        else {}
    )
    controls = plan.get("controls") if isinstance(plan.get("controls"), dict) else {}
    assigned_counts = [
        int(node.get("assigned_pods") or 0)
        for node in nodes
        if isinstance(node, dict)
    ]
    master_pods = next(
        (
            int(node.get("assigned_pods") or 0)
            for node in nodes
            if isinstance(node, dict) and node.get("name") == "seed-k3s-master"
        ),
        None,
    )
    summary["staticPlacement"] = {
        "recorded": True,
        "method": "deterministic-by-as-hard",
        "artifact": str(plan_path),
        "nodeCount": len(nodes),
        "asCount": len(assignments),
        "podCount": sum(assigned_counts),
        "minPodsPerNode": min(assigned_counts) if assigned_counts else None,
        "maxPodsPerNode": max(assigned_counts) if assigned_counts else None,
        "masterPods": master_pods,
        "excludeControlPlane": bool(controls.get("exclude_control_plane", False)),
    }
    writeJson(summary_path, summary)


def runVerificationStage(
    name: str,
    cmd: list[str],
    *,
    summary: dict[str, Any],
    summary_path: Path,
    log_path: Path,
    env: dict[str, str] | None,
    test_key: str,
    test_filename: str,
    run_dir: Path,
) -> None:
    """Run one required verification stage and always record its JSON summary."""
    rc = runCommand(name, cmd, cwd=SCRIPT_DIR, log_path=log_path, summary=summary, env=env, summary_path=summary_path)
    recordTestSummary(summary, summary_path, run_dir, test_key, test_filename)
    if rc != 0:
        summary["status"] = "FAIL"
        summary["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        writeJson(summary_path, summary)
        raise SystemExit(rc)


def maybeDestroyExisting(summary: dict[str, Any], summary_path: Path, run_dir: Path, env: dict[str, str]) -> None:
    """Destroy the currently recorded cluster before building a fresh assignment cluster."""
    config = Path(env["B62_CONFIG_K3S_PATH"])
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
    destroy_env = dict(env)
    destroy_env.setdefault("SEED_DESTROY_CLUSTER_TIMEOUT_SECONDS", "180")
    if env.get("B62_CLUSTER_INPUT_CONFIG"):
        command = ["python3", "k8sTools.py", "destroy", "-d", str(config), "--keep-temp"]
    else:
        command = [str(SCRIPT_DIR / "destroyCluster.sh")]
    runStage(
        "destroy-existing-cluster",
        command,
        summary=summary,
        summary_path=summary_path,
        log_path=run_dir / "destroy-existing-cluster.log",
        env=destroy_env,
    )


def buildCluster(summary: dict[str, Any], summary_path: Path, run_dir: Path, env: dict[str, str]) -> None:
    """Build KVM/K3s from either generated single-host or explicit input YAML."""
    config_kvm = env["B62_CONFIG_KVM_PATH"]
    config_k3s = env["B62_CONFIG_K3S_PATH"]
    kubeconfig = env["B62_KUBECONFIG_PATH"]
    inventory = env["B62_INVENTORY_PATH"]
    cluster_input = loadYaml(Path(config_kvm))
    kind = canonicalClusterKind(cluster_input)
    if kind == "kvm":
        explicit_nodes = cluster_input.get("nodes")
        if isinstance(explicit_nodes, list) and explicit_nodes:
            runStage(
                "prepare-libvirt-dhcp",
                ["sudo", "-n", "python3", "prepareLibvirtDhcp.py", config_kvm],
                summary=summary,
                summary_path=summary_path,
                log_path=run_dir / "prepare-libvirt-dhcp.log",
                env=env,
            )
        else:
            summary["stages"].append(
                {
                    "name": "prepare-libvirt-dhcp",
                    "status": "SKIPPED",
                    "reason": (
                        "compact master/workers plan; kvm/createKvmVms.py "
                        "reserves each generated address before VM boot"
                    ),
                    "durationSeconds": 0,
                    "exitCode": 0,
                }
            )
            writeJson(summary_path, summary)
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
    generated = loadYaml(Path(config_k3s))
    nodes = generated.get("nodes") if isinstance(generated.get("nodes"), list) else []
    summary["cluster"] = {
        "kind": canonicalClusterKind(generated),
        "name": generated.get("clusterName"),
        "nodeCount": len(nodes),
        "workerCount": max(len(nodes) - 1, 0),
        "fabric": generated.get("fabric"),
        "input": config_kvm,
        "configK3s": config_k3s,
        "kubeconfig": kubeconfig,
        "inventory": inventory,
    }
    summary["workerCount"] = max(len(nodes) - 1, 0)
    writeJson(summary_path, summary)
    runStage(
        "ensure-all-nodes-schedulable",
        [str(SCRIPT_DIR / "ensureAllNodesSchedulable.sh"), str(run_dir)],
        summary=summary,
        summary_path=summary_path,
        log_path=run_dir / "ensure-all-nodes-schedulable.orchestrator.log",
        env=env,
    )
    if kind == "multiHostKvm":
        runVerificationStage(
            "validate-cross-host-k3s",
            [
                "python3",
                str(SCRIPT_DIR / "validateMultiHostK3s.py"),
                "--kubeconfig",
                kubeconfig,
                "--config-k3s",
                config_k3s,
                "--output",
                str(run_dir / "k3s_cross_host_summary.json"),
            ],
            summary=summary,
            summary_path=summary_path,
            log_path=run_dir / "validate-cross-host-k3s.log",
            env=env,
            test_key="crossHostK3s",
            test_filename="k3s_cross_host_summary.json",
            run_dir=run_dir,
        )


def runWorkloadStages(
    summary: dict[str, Any],
    summary_path: Path,
    run_dir: Path,
    env: dict[str, str],
    *,
    resume_from_kernel: bool = False,
    resume_from_build: bool = False,
    after_compile: Callable | None = None,
    after_ready: Callable | None = None,
    before_build: Callable | None = None,
) -> None:
    """Run shared stages; optional callbacks receive summary, path, run dir, env.

    after_compile can replace placement before image preload; after_ready can
    verify that placement before routing starts. before_build validates artifacts
    before preload, including build resumes. Defaults preserve B62 behavior.
    """
    if not resume_from_kernel:
        required_before_bird = [
            ("clean", "clean.sh"),
            ("preflight", "preflight.sh"),
            ("compile", "compile.sh"),
            ("build", "build.sh"),
            ("deploy", "deploy.sh"),
            ("wait-ready", "wait-ready.sh"),
            ("start-bird", "start_bird.sh"),
        ]
        if resume_from_build:
            if not (run_dir / "output/k8s.yaml").is_file():
                raise FileNotFoundError("Resuming build requires the compiled manifest")
            required_before_bird = required_before_bird[3:]
        for name, script in required_before_bird:
            if name == "build" and before_build is not None:
                before_build(summary, summary_path, run_dir, env)
            runStage(
                name,
                [str(SCRIPT_DIR / script), str(run_dir)],
                summary=summary,
                summary_path=summary_path,
                log_path=run_dir / f"{name}.orchestrator.log",
                env=env,
            )
            if name == "compile":
                recordPlacementSummary(summary, summary_path, run_dir)
                if after_compile is not None:
                    after_compile(summary, summary_path, run_dir, env)
            if name == "wait-ready" and after_ready is not None:
                after_ready(summary, summary_path, run_dir, env)
        runVerificationStage(
            "verify-after-start-bird",
            [str(SCRIPT_DIR / "verifyStartBird.sh"), str(run_dir)],
            summary=summary,
            summary_path=summary_path,
            log_path=run_dir / "verify-after-start-bird.orchestrator.log",
            env=env,
            test_key="verify_after_start_bird",
            test_filename="verify_after_start_bird_summary.json",
            run_dir=run_dir,
        )
    runStage(
        "start-kernel",
        [str(SCRIPT_DIR / "start_bird_kernel.sh"), str(run_dir)],
        summary=summary,
        summary_path=summary_path,
        log_path=run_dir / "start-kernel.orchestrator.log",
        env=env,
    )
    runVerificationStage(
        "verify-after-fib-write",
        [str(SCRIPT_DIR / "verifyFibRoutes.sh"), str(run_dir)],
        summary=summary,
        summary_path=summary_path,
        log_path=run_dir / "verify-after-fib-write.orchestrator.log",
        env=env,
        test_key="verify_after_fib_write",
        test_filename="verify_after_fib_write_summary.json",
        run_dir=run_dir,
    )
    cluster_config_path = Path(
        env.get("B62_CLUSTER_INPUT_CONFIG") or env["B62_CONFIG_K3S_PATH"]
    )
    if cluster_config_path.is_file():
        cluster_input = loadYaml(cluster_config_path)
        kind = canonicalClusterKind(cluster_input)
        if kind in {"kvm", "multiHostKvm"}:
            pair_scope = "cross-host" if kind == "multiHostKvm" else "cross-node"
            stage_name = (
                "validate-cross-host-workload"
                if pair_scope == "cross-host"
                else "validate-cross-node-workload"
            )
            output_name = (
                "cross_host_workload_summary.json"
                if pair_scope == "cross-host"
                else "cross_node_workload_summary.json"
            )
            runVerificationStage(
                stage_name,
                [
                    "python3",
                    str(SCRIPT_DIR / "validateCrossHostWorkload.py"),
                    "--kubeconfig",
                    env["B62_KUBECONFIG_PATH"],
                    "--config-k3s",
                    env["B62_CONFIG_K3S_PATH"],
                    "--namespace",
                    env["SEED_NAMESPACE"],
                    "--expected-mtu",
                    env.get("SEED_CNI_MTU", "0") or "0",
                    "--pair-scope",
                    pair_scope,
                    "--output",
                    str(run_dir / output_name),
                ],
                summary=summary,
                summary_path=summary_path,
                log_path=run_dir / f"{stage_name}.log",
                env=env,
                test_key=(
                    "crossHostWorkload"
                    if pair_scope == "cross-host"
                    else "crossNodeWorkload"
                ),
                test_filename=output_name,
                run_dir=run_dir,
            )
    runReconvergenceStage(summary, summary_path, run_dir, env)


def runReconvergenceStage(summary: dict[str, Any], summary_path: Path,
                         run_dir: Path, env: dict[str, str]) -> None:
    """Run only the fault/recovery stage using this run's recorded context."""
    runVerificationStage(
        "reconvergence",
        [str(SCRIPT_DIR / "runReconvergence.sh"), str(run_dir)],
        summary=summary,
        summary_path=summary_path,
        log_path=run_dir / "reconvergence.orchestrator.log",
        env=env,
        test_key="reconvergence",
        test_filename="reconvergence_summary.json",
        run_dir=run_dir,
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


def parseArgs(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse argv, or the process CLI when argv is omitted."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("assignment_arg", nargs="?", type=Path, help="assignment YAML path")
    parser.add_argument("--assignment", type=Path)
    parser.add_argument("--config", type=Path,
                        help="sole compact single-host YAML containing infrastructure and experiment settings")
    parser.add_argument("--prepare-only", action="store_true",
                        help="write resolved configuration and resource summary only; no cluster commands")
    parser.add_argument(
        "--cluster-config",
        type=Path,
        help="explicit neutral kvm/physical/multiHostKvm input; skips single-host assignment rendering",
    )
    parser.add_argument("--config-k3s", type=Path, default=SCRIPT_DIR / "configK3s.yaml")
    parser.add_argument("--kubeconfig", type=Path, default=SCRIPT_DIR / "kubeconfig.yaml")
    parser.add_argument("--inventory", type=Path, default=SCRIPT_DIR / "cluster.inventory.yaml")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument(
        "--resume-from",
        choices=["build", "start-kernel", "reconvergence"],
        help="resume an existing run from image build, kernel export or fault/recovery measurement",
    )
    parser.add_argument("--skip-existing-destroy", action="store_true")
    parser.add_argument("--skip-final-destroy", action="store_true", help="compatibility flag; final destroy is currently disabled")
    args = parser.parse_args(argv)
    if args.config and (args.assignment or args.assignment_arg or args.cluster_config):
        parser.error("--config cannot be combined with --assignment or --cluster-config")
    if args.prepare_only and args.resume_from:
        parser.error("--prepare-only cannot be combined with --resume-from")
    if args.prepare_only and not args.config:
        parser.error("--prepare-only requires --config")
    return args


def main(argv: list[str] | None = None, *, after_compile: Callable | None = None,
         after_ready: Callable | None = None, after_workload: Callable | None = None,
         before_build: Callable | None = None) -> int:
    """Run argv with optional placement, validation and final workload callbacks.

    Each callback receives summary, summary_path, run_dir and env. The final
    callback runs after successful workload/recovery stages, before marking PASS;
    it also runs on resume, but never during prepare-only.
    """
    args = parseArgs(argv)
    config_k3s = resolvePath(args.config_k3s)
    kubeconfig = resolvePath(args.kubeconfig)
    inventory = resolvePath(args.inventory)
    if args.resume_from:
        if args.run_dir is None:
            raise ValueError("--resume-from requires --run-dir")
        run_dir = resolvePath(args.run_dir)
        assignment_path = run_dir / "assignment.yaml"
        assignment = loadYaml(assignment_path)
        runtime = assignment.get("runtime", {})
        cluster_input = run_dir / "cluster-input.yaml" if (run_dir / "cluster-input.yaml").is_file() else (
            resolvePath(args.cluster_config) if args.cluster_config else None)
        config_k3s = resolvePath(runtime.get("configK3s", config_k3s))
        kubeconfig = resolvePath(runtime.get("kubeconfig", kubeconfig))
        inventory = resolvePath(runtime.get("inventory", inventory))
        cluster_input_data = loadYaml(cluster_input) if cluster_input else None
    elif args.config:
        cluster_input = resolvePath(args.config)
        raw_config = loadYaml(cluster_input)
        assignment = createSingleConfigAssignment(raw_config, cluster_input)
        cluster_input_data = resolveClusterInputPaths(raw_config, cluster_input)
        assignment_path = cluster_input
    else:
        assignment_path = resolvePath(args.assignment or args.assignment_arg or DEFAULT_ASSIGNMENT)
        assignment = loadYaml(assignment_path)
        cluster_input = resolvePath(args.cluster_config) if args.cluster_config else None
        cluster_input_data = loadYaml(cluster_input) if cluster_input is not None else None
    if cluster_input_data is not None:
        canonicalClusterKind(cluster_input_data)
    topology_size = int(getNested(assignment, "experiment.topologySize"))
    worker_count = int(getNested(assignment, "experiment.workerCount"))
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    if args.resume_from and args.run_dir is None:
        raise ValueError("--resume-from requires --run-dir")
    suffix = "singlehost_macvlan_vlan" if args.config else f"w{worker_count}"
    run_dir = resolvePath(args.run_dir) if args.run_dir else SCRIPT_DIR / "runs" / f"{topology_size}_{timestamp}_{suffix}"
    run_dir.mkdir(parents=True, exist_ok=True)
    summary_path = run_dir / "summary.json"
    if args.config and not args.resume_from and summary_path.exists():
        raise FileExistsError(f"Refusing to overwrite existing run: {run_dir}; use a new directory or --resume-from")
    if args.resume_from:
        assignment_path = run_dir / "assignment.yaml"
        if not assignment_path.is_file() or not summary_path.is_file():
            raise FileNotFoundError(
                f"resume requires existing assignment/summary in {run_dir}"
            )
        assignment = loadYaml(assignment_path)
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        if summary.get("status") == "PREPARED":
            raise ValueError("Cannot resume start-kernel from a prepare-only run; no cluster was built")
        summary["status"] = "RUNNING"
        summary.pop("finishedAt", None)
        summary.setdefault("resumeAttempts", []).append(
            {
                "from": args.resume_from,
                "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
        )
        writeJson(summary_path, summary)
    else:
        (run_dir / "topology_size").write_text(f"{topology_size}\n", encoding="utf-8")
        summary = createSummary(run_dir, assignment, cluster_input_data)
        if args.config:
            summary["configurationMode"] = "single-yaml"
            summary["sourceConfig"] = str(cluster_input)
            assignment["runtime"] = {
                "clusterInput": str(run_dir / "cluster-input.yaml"),
                "configK3s": str(config_k3s),
                "kubeconfig": str(kubeconfig),
                "inventory": str(inventory),
            }
        writeJson(summary_path, summary)

    try:
        if args.resume_from:
            env = createRuntimeEnv(
                assignment_path,
                assignment,
                run_dir,
                cluster_input=cluster_input,
                config_k3s=config_k3s,
                kubeconfig=kubeconfig,
                inventory=inventory,
            )
            if args.resume_from == "reconvergence":
                runReconvergenceStage(summary, summary_path, run_dir, env)
            else:
                runWorkloadStages(
                    summary, summary_path, run_dir, env,
                    resume_from_kernel=args.resume_from == "start-kernel",
                    resume_from_build=args.resume_from == "build",
                    after_compile=after_compile, after_ready=after_ready,
                    before_build=before_build,
                )
        elif cluster_input is None:
            runStage(
                "render-assignment",
                ["python3", "renderAssignmentConfig.py", "--assignment", str(assignment_path), "--run-dir", str(run_dir), "--timestamp", timestamp, "prepare"],
                summary=summary,
                summary_path=summary_path,
                log_path=run_dir / "render-assignment.log",
            )
        else:
            if args.config:
                (run_dir / "cluster-input.yaml").write_text(
                    yaml.safe_dump(cluster_input_data, sort_keys=False), encoding="utf-8")
                cluster_input = run_dir / "cluster-input.yaml"
            else:
                shutil.copy2(cluster_input, run_dir / "cluster-input.yaml")
            summary["clusterInput"] = str(cluster_input)
            summary["stages"].append(
                {
                    "name": "render-assignment",
                    "status": "SKIPPED",
                    "reason": "single YAML adapted to run-local snapshot" if args.config else "explicit --cluster-config supplied",
                    "durationSeconds": 0,
                    "exitCode": 0,
                }
            )
            writeJson(summary_path, summary)
        if not args.resume_from:
            if args.config:
                (run_dir / "assignment.yaml").write_text(
                    yaml.safe_dump(assignment, sort_keys=False), encoding="utf-8")
            else:
                shutil.copy2(assignment_path, run_dir / "assignment.yaml")
            env = createRuntimeEnv(
                run_dir / "assignment.yaml",
                assignment,
                run_dir,
                cluster_input=cluster_input,
                config_k3s=config_k3s,
                kubeconfig=kubeconfig,
                inventory=inventory,
            )

            if args.prepare_only:
                summary["status"] = "PREPARED"
                writeJson(summary_path, summary)
                print(f"Configuration prepared without cluster operations: {run_dir}")
                return 0

            if not args.skip_existing_destroy:
                maybeDestroyExisting(summary, summary_path, run_dir, env)

            buildCluster(summary, summary_path, run_dir, env)
            runWorkloadStages(summary, summary_path, run_dir, env,
                              after_compile=after_compile, after_ready=after_ready,
                              before_build=before_build)
        if after_workload is not None:
            after_workload(summary, summary_path, run_dir, env)
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
    finally:
        summary["performance"] = summarizePerformanceMetrics(run_dir)
        writeJson(summary_path, summary)


if __name__ == "__main__":
    raise SystemExit(main())
