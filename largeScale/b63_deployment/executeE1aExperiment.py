#!/usr/bin/env python3
"""Execute the isolated E1-A marginal Pod co-location experiment.

Required input is the dedicated KVM/K3s YAML. Generated output follows the
experiments/e1_marginal_pods layout and includes raw Pod/node records, frozen
plans, metrics, figures, and cleanup ownership. Side effects are limited to the
VMs, libvirt network, K3s cluster, namespaces, and worker hostPath declared by
that YAML. Run from the B63 example directory on the libvirt host.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import csv
import hashlib
import json
import os
import random
import shlex
import shutil
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, IO

import yaml

import analyzeE1aExperiment as analyzer


HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "research_configs/configE1aMarginalPods.yaml"
K8S_TOOLS = HERE.parent / "b62_k8s_scale/k8sTools.py"
WORKLOAD_SOURCE = HERE / "e1aWorkload.c"
COLLECTOR_SOURCE = HERE / "collectE1aNodeMetrics.py"
DESIGN_SOURCE = HERE / "E1_DESIGN_PROPOSAL.md"


@dataclass(frozen=True)
class ClusterAccess:
    """Stable access coordinates for the dedicated worker and K3s API."""

    kubeconfig: Path
    workerName: str
    workerIp: str
    sshUser: str
    sshKey: Path


def readYaml(path: Path) -> dict[str, Any]:
    """Read one mapping-valued YAML file."""
    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise ValueError(f"expected YAML mapping in {path}")
    return value


def writeJson(path: Path, value: Any) -> None:
    """Write stable, human-readable JSON and create parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def executeCommand(command: list[str], *, cwd: Path = HERE, logPath: Path | None = None,
                   inputText: str | None = None, timeout: float | None = None,
                   check: bool = True) -> subprocess.CompletedProcess[str]:
    """Execute one command, optionally teeing merged output to a durable log."""
    started = time.monotonic()
    result = subprocess.run(command, cwd=cwd, input=inputText, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            timeout=timeout, check=False)
    if logPath:
        logPath.parent.mkdir(parents=True, exist_ok=True)
        with logPath.open("a", encoding="utf-8") as handle:
            handle.write(f"$ {shlex.join(command)}\n")
            handle.write(result.stdout)
            handle.write(f"[exit={result.returncode} elapsed={time.monotonic() - started:.3f}s]\n")
    if check and result.returncode != 0:
        raise RuntimeError(f"command failed ({result.returncode}): {shlex.join(command)}\n{result.stdout[-4000:]}")
    return result


def kubectl(access: ClusterAccess, arguments: list[str], *, inputText: str | None = None,
            timeout: float | None = 120, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run kubectl only against the run-local kubeconfig."""
    return executeCommand(["kubectl", "--kubeconfig", str(access.kubeconfig),
                           "--request-timeout=120s", *arguments],
                          inputText=inputText, timeout=timeout, check=check)


def sshCommand(access: ClusterAccess, remoteCommand: str, *, timeout: float | None = 120,
               check: bool = True) -> subprocess.CompletedProcess[str]:
    """Execute a bounded command on the dedicated worker."""
    command = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
               "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
               "-i", str(access.sshKey),
               f"{access.sshUser}@{access.workerIp}", remoteCommand]
    return executeCommand(command, timeout=timeout, check=check)


def sha256File(path: Path) -> str:
    """Return the content digest of one implementation input."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepareRunDirectory(runDir: Path, configPath: Path) -> None:
    """Create the documented artifact tree and freeze design/source inputs."""
    for name in ("env_snapshot", "workload_source", "workload_tests", "pilot",
                 "plans", "raw", "metrics", "figures", "logs"):
        (runDir / name).mkdir(parents=True, exist_ok=True)
    shutil.copy2(configPath, runDir / "config.yaml")
    shutil.copy2(DESIGN_SOURCE, runDir / "design_frozen.md")
    for source in (WORKLOAD_SOURCE, COLLECTOR_SOURCE, Path(__file__).resolve(), HERE / "analyzeE1aExperiment.py"):
        shutil.copy2(source, runDir / "workload_source" / source.name)
    (runDir / "README.md").write_text(
        "# E1-A marginal Pod experiment artifact\n\n"
        "`config.yaml` and `design_frozen.md` are immutable run inputs. `raw/` stores one directory per point, "
        "`metrics/` and `figures/` are derived offline, and `cleanup_manifest.json` lists only resources owned by this run.\n",
        encoding="utf-8",
    )


def compileWorkload(runDir: Path) -> Path:
    """Build and strip the static workload binary used by every Pod."""
    binary = runDir / "workload_source/e1aWorkload"
    executeCommand(["gcc", "-O2", "-Wall", "-Wextra", "-Werror", "-static",
                    "-o", str(binary), str(WORKLOAD_SOURCE), "-lm"],
                   logPath=runDir / "workload_tests/build.log")
    executeCommand(["strip", str(binary)], logPath=runDir / "workload_tests/build.log")
    size = binary.stat().st_size
    if size >= 1024 * 1024:
        raise RuntimeError(f"workload binary {size} bytes exceeds ConfigMap raw-data limit")
    return binary


def snapshotEnvironment(runDir: Path) -> None:
    """Capture host, libvirt, source, and process context without modifying it."""
    commands = {
        "date.txt": ["date", "--iso-8601=seconds"],
        "uname.txt": ["uname", "-a"],
        "lscpu.txt": ["lscpu", "-e=CPU,NODE,SOCKET,CORE,ONLINE"],
        "memory.txt": ["free", "-h"],
        "loadavg.txt": ["cat", "/proc/loadavg"],
        "virsh_domains.txt": ["virsh", "list", "--all"],
        "virsh_networks.txt": ["virsh", "net-list", "--all"],
        "git_status.txt": ["git", "status", "--short"],
        "git_commit.txt": ["git", "rev-parse", "HEAD"],
    }
    for filename, command in commands.items():
        result = executeCommand(command, check=False)
        (runDir / "env_snapshot" / filename).write_text(result.stdout, encoding="utf-8")


def buildCluster(runDir: Path, configPath: Path) -> None:
    """Create the dedicated two-VM K3s cluster using k8sTools."""
    command = [sys.executable, str(K8S_TOOLS), "build", "--input", str(configPath),
               "--config-k3s", str(runDir / "configK3s.yaml"),
               "--kubeconfig", str(runDir / "kubeconfig.yaml"),
               "--inventory", str(runDir / "inventory.yaml"), "--keep-temp"]
    executeCommand(command, cwd=K8S_TOOLS.parent, logPath=runDir / "logs/build_cluster.log", timeout=7200)


def clusterAccesses(runDir: Path, config: dict[str, Any]) -> list[ClusterAccess]:
    """Resolve every dedicated worker's management and SSH coordinates."""
    inventory = readYaml(runDir / "inventory.yaml")
    workers = [item for item in inventory.get("nodes", []) if item.get("role") == "worker"]
    if not workers:
        raise ValueError("E1-A requires at least one dedicated worker")
    sshConfig = config.get("ssh", {})
    accesses: list[ClusterAccess] = []
    for worker in workers:
        workerIp = worker.get("managementIp") or worker.get("management_ip") or worker.get("ip")
        if not workerIp:
            raise ValueError(f"worker inventory entry has no management IP: {worker}")
        nodeSsh = worker.get("ssh", {}) if isinstance(worker.get("ssh"), dict) else {}
        accesses.append(ClusterAccess(
            kubeconfig=(runDir / "kubeconfig.yaml").resolve(),
            workerName=str(worker["name"]),
            workerIp=str(workerIp),
            sshUser=str(nodeSsh.get("user") or sshConfig.get("user", "ubuntu")),
            sshKey=Path(str(nodeSsh.get("key") or sshConfig.get("key", "~/.ssh/id_ed25519"))).expanduser().resolve(),
        ))
    return sorted(accesses, key=lambda item: item.workerName)


def clusterAccess(runDir: Path, config: dict[str, Any]) -> ClusterAccess:
    """Return the sole worker access for single-worker compatibility."""
    accesses = clusterAccesses(runDir, config)
    if len(accesses) != 1:
        raise ValueError(f"expected exactly one worker, found {len(accesses)}")
    return accesses[0]


def waitForCluster(access: ClusterAccess, runDir: Path) -> None:
    """Require both nodes Ready and capture runtime versions."""
    executeCommand(["kubectl", "--kubeconfig", str(access.kubeconfig), "wait", "--for=condition=Ready",
                    "nodes", "--all", "--timeout=600s"], logPath=runDir / "logs/cluster_ready.log", timeout=660)
    nodes = json.loads(kubectl(access, ["get", "nodes", "-o", "json"]).stdout)
    writeJson(runDir / "env_snapshot/kubernetes_nodes.json", nodes)
    version = kubectl(access, ["version", "-o", "json"], check=False).stdout
    (runDir / "env_snapshot/kubernetes_version.json").write_text(version, encoding="utf-8")
    for name in ("uname -a", "sudo k3s --version", "stat -fc %T /sys/fs/cgroup",
                 "sudo crictl --version", "cat /proc/cmdline"):
        result = sshCommand(access, name, check=False)
        safeName = name.replace(" ", "_").replace("/", "_") + ".txt"
        (runDir / "env_snapshot" / safeName).write_text(result.stdout, encoding="utf-8")


def pinExperimentVcpus(config: dict[str, Any], runDir: Path) -> None:
    """Pin only E1-A VM vCPUs to disjoint configured pCPUs and record XML."""
    pinning = config.get("e1a", {}).get("pinning", {})
    if not pinning.get("enabled", False):
        return
    workerCount = int(config["workers"]["count"])
    workerSets = pinning.get("workerPcpuSets")
    if workerSets is None:
        workerSets = [pinning.get("workerPcpus", [])]
    if len(workerSets) != workerCount:
        raise ValueError(f"pinning.workerPcpuSets has {len(workerSets)} entries for {workerCount} workers")
    namesAndCpus = [(str(config["defaults"]["masterName"]), list(pinning.get("masterPcpus", [])))]
    namesAndCpus.extend(
        (str(config["defaults"]["workerNamePrefix"]) + str(index), list(workerSets[index - 1]))
        for index in range(1, workerCount + 1)
    )
    logPath = runDir / "logs/vcpu_pinning.log"
    for vmName, pcpus in namesAndCpus:
        for vcpu, pcpu in enumerate(pcpus):
            executeCommand(["virsh", "vcpupin", vmName, str(vcpu), str(pcpu), "--live", "--config"],
                           logPath=logPath)
        xml = executeCommand(["virsh", "dumpxml", vmName]).stdout
        (runDir / "env_snapshot" / f"{vmName}.xml").write_text(xml, encoding="utf-8")
        affinity = executeCommand(["virsh", "vcpupin", vmName]).stdout
        (runDir / "env_snapshot" / f"{vmName}.vcpupin.txt").write_text(affinity, encoding="utf-8")


def copyCollector(access: ClusterAccess, runDir: Path) -> None:
    """Copy the read-only node collector to the dedicated worker."""
    command = ["scp", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
               "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
               "-i", str(access.sshKey),
               str(COLLECTOR_SOURCE), f"{access.sshUser}@{access.workerIp}:/tmp/collectE1aNodeMetrics.py"]
    executeCommand(command, logPath=runDir / "logs/copy_collector.log", timeout=120)


def findPauseImage(access: ClusterAccess) -> str:
    """Return an already imported K3s pause image suitable for a static binary."""
    result = sshCommand(access, "sudo k3s ctr images list -q | awk '/pause/ {print; exit}'")
    candidates = [line.strip() for line in result.stdout.splitlines()
                  if "pause" in line and ":" in line and " " not in line.strip()]
    image = candidates[-1] if candidates else ""
    if not image:
        raise RuntimeError("no preloaded pause image found on E1-A worker")
    return image


def readAdmission(access: ClusterAccess) -> dict[str, Any]:
    """Read real kubelet Pod admission and current nonterminal occupancy."""
    node = json.loads(kubectl(access, ["get", "node", access.workerName, "-o", "json"]).stdout)
    pods = json.loads(kubectl(access, ["get", "pods", "-A", "-o", "json"]).stdout)
    existing = [item for item in pods.get("items", [])
                if item.get("spec", {}).get("nodeName") == access.workerName
                and item.get("status", {}).get("phase") not in {"Succeeded", "Failed"}]
    allocatable = int(node["status"]["allocatable"]["pods"])
    return {
        "workerName": access.workerName,
        "allocatablePods": allocatable,
        "existingNonterminalPods": len(existing),
        "existingPodNames": [f"{item['metadata']['namespace']}/{item['metadata']['name']}" for item in existing],
        "maxBackgroundN": allocatable - len(existing) - 1,
        "allocatableCpu": node["status"]["allocatable"].get("cpu"),
        "allocatableMemory": node["status"]["allocatable"].get("memory"),
    }


def createConfigMap(namespace: str, binary: Path) -> dict[str, Any]:
    """Create the binaryData ConfigMap shared by all point Pods."""
    encoded = base64.b64encode(binary.read_bytes()).decode("ascii")
    return {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": "e1a-workload", "namespace": namespace},
        "binaryData": {"e1aWorkload": encoded},
    }


def jobObject(namespace: str, name: str, role: str, count: int, access: ClusterAccess,
              image: str, args: list[str], cpuRequest: str, memoryRequest: str,
              controlPath: str | None = None) -> dict[str, Any]:
    """Return one fixed-node Kubernetes Job; indexed Jobs provide stable IDs."""
    volumes: list[dict[str, Any]] = [{"name": "binary", "configMap": {"name": "e1a-workload", "defaultMode": 365}}]
    mounts: list[dict[str, Any]] = [{"name": "binary", "mountPath": "/opt/e1a", "readOnly": True}]
    if controlPath:
        volumes.append({"name": "control", "hostPath": {"path": controlPath, "type": "DirectoryOrCreate"}})
        mounts.append({"name": "control", "mountPath": "/control", "readOnly": True})
    podSpec = {
        "nodeName": access.workerName,
        "restartPolicy": "Never",
        "terminationGracePeriodSeconds": 5,
        "containers": [{
            "name": "workload", "image": image, "imagePullPolicy": "Never",
            "command": ["/opt/e1a/e1aWorkload"], "args": args,
            "resources": {"requests": {"cpu": cpuRequest, "memory": memoryRequest}},
            "securityContext": {"allowPrivilegeEscalation": False},
            "volumeMounts": mounts,
        }],
        "volumes": volumes,
    }
    job: dict[str, Any] = {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": name, "namespace": namespace,
                     "labels": {"e1a.seedemu.io/role": role}},
        "spec": {
            "backoffLimit": 0,
            "parallelism": count,
            "completions": count,
            "template": {"metadata": {"labels": {"e1a.seedemu.io/role": role}}, "spec": podSpec},
        },
    }
    if count > 1:
        job["spec"]["completionMode"] = "Indexed"
    return job


def applyObjects(access: ClusterAccess, objects: list[dict[str, Any]]) -> None:
    """Create fresh point objects without a large last-applied annotation."""
    manifest = "---\n".join(yaml.safe_dump(item, sort_keys=False) for item in objects)
    kubectl(access, ["create", "-f", "-"], inputText=manifest, timeout=180)


def waitForJob(access: ClusterAccess, namespace: str, name: str, timeoutSeconds: int) -> None:
    """Wait for a bounded Job completion and surface Pod diagnostics on failure."""
    result = kubectl(access, ["-n", namespace, "wait", "--for=condition=complete", f"job/{name}",
                              f"--timeout={timeoutSeconds}s"], timeout=timeoutSeconds + 30, check=False)
    if result.returncode != 0:
        pods = kubectl(access, ["-n", namespace, "get", "pods", "-o", "wide"], check=False).stdout
        events = kubectl(access, ["-n", namespace, "get", "events", "--sort-by=.lastTimestamp"], check=False).stdout
        raise RuntimeError(f"job {namespace}/{name} did not complete\n{result.stdout}\n{pods}\n{events}")


def waitForPointPods(access: ClusterAccess, namespace: str, expected: int, timeoutSeconds: int) -> None:
    """Wait until all point Pods exist, are running, and report container Ready."""
    deadline = time.monotonic() + timeoutSeconds
    last = ""
    while time.monotonic() < deadline:
        result = kubectl(access, ["-n", namespace, "get", "pods", "-o", "json"], check=False)
        if result.returncode == 0:
            payload = json.loads(result.stdout)
            pods = payload.get("items", [])
            ready = [item for item in pods if item.get("status", {}).get("phase") == "Running"
                     and item.get("status", {}).get("containerStatuses")
                     and all(status.get("ready") for status in item["status"]["containerStatuses"])]
            last = f"pods={len(pods)} ready={len(ready)} expected={expected}"
            if len(pods) == expected and len(ready) == expected:
                return
        time.sleep(2)
    details = kubectl(access, ["-n", namespace, "get", "pods", "-o", "wide"], check=False).stdout
    raise TimeoutError(f"point admission/ready timeout: {last}\n{details}")


def parseJsonLogLines(text: str) -> list[dict[str, Any]]:
    """Parse JSON records even when kubectl --prefix prepends Pod identity."""
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        start = line.find("{")
        if start < 0:
            continue
        try:
            records.append(json.loads(line[start:]))
        except json.JSONDecodeError:
            continue
    return records


def readRoleLogs(access: ClusterAccess, namespace: str, role: str, maxRequests: int) -> list[dict[str, Any]]:
    """Fetch all bounded one-line workload results for a point role."""
    result = kubectl(access, ["-n", namespace, "logs", "-l", f"e1a.seedemu.io/role={role}",
                              "--prefix=true", f"--max-log-requests={maxRequests}"], timeout=300)
    return parseJsonLogLines(result.stdout)


def calibrateOne(access: ClusterAccess, binary: Path, image: str, runDir: Path,
                 label: str, targetMicroseconds: float, cpuRequest: str,
                 memoryRequest: str) -> dict[str, Any]:
    """Calibrate one fixed CPU task size inside the otherwise idle worker."""
    namespace = f"seedemu-b63-e1a-cal-{label}"
    kubectl(access, ["create", "namespace", namespace])
    try:
        args = ["--calibrate-target-microseconds", str(targetMicroseconds), "--calibrate-samples", "31"]
        objects = [createConfigMap(namespace, binary),
                   jobObject(namespace, "calibrate", "calibration", 1, access, image, args,
                             cpuRequest, memoryRequest)]
        applyObjects(access, objects)
        waitForJob(access, namespace, "calibrate", 300)
        records = readRoleLogs(access, namespace, "calibration", 2)
        if len(records) != 1 or records[0].get("mode") != "calibration":
            raise RuntimeError(f"invalid {label} calibration output: {records}")
        writeJson(runDir / "pilot" / f"calibration_{label}.json", records[0])
        return records[0]
    finally:
        kubectl(access, ["delete", "namespace", namespace, "--wait=true", "--timeout=180s"],
                timeout=210, check=False)


def triggerEpoch(access: ClusterAccess, controlPath: str, leadSeconds: float) -> int:
    """Atomically publish one worker-clock epoch through the shared hostPath."""
    script = ("import os,pathlib,sys,time; p=pathlib.Path(sys.argv[1]); p.mkdir(parents=True,exist_ok=True); "
              "e=time.time_ns()+int(float(sys.argv[2])*1e9); t=p/'epoch.tmp'; "
              "t.write_text(str(e)+'\\n'); os.replace(t,p/'epoch'); print(e)")
    remote = f"sudo python3 -c {shlex.quote(script)} {shlex.quote(controlPath)} {leadSeconds}"
    result = sshCommand(access, remote)
    return int(result.stdout.strip().splitlines()[-1])


def startNodeCollector(access: ClusterAccess, epochNs: int, durationSeconds: float,
                       intervalSeconds: float, outputPath: Path, errorPath: Path) -> tuple[subprocess.Popen[str], IO[str], IO[str]]:
    """Start one SSH node collector and return process plus owned file handles."""
    command = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
               "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
               "-i", str(access.sshKey),
               f"{access.sshUser}@{access.workerIp}",
               f"python3 /tmp/collectE1aNodeMetrics.py --epoch-unix-ns {epochNs} "
               f"--duration-seconds {durationSeconds} --interval-seconds {intervalSeconds}"]
    outputHandle = outputPath.open("w", encoding="utf-8")
    errorHandle = errorPath.open("w", encoding="utf-8")
    process = subprocess.Popen(command, text=True, stdout=outputHandle, stderr=errorHandle)
    return process, outputHandle, errorHandle


def summarizeNodeMetrics(path: Path, measurementStartNs: int, measurementEndNs: int) -> dict[str, Any]:
    """Reduce raw node samples to measurement-window CPU/PSI/runqueue metrics."""
    samples = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    selected = [item for item in samples if measurementStartNs <= item["timestamp_unix_ns"] <= measurementEndNs]
    busy: list[float] = []
    steal: list[float] = []
    for previous, current in zip(selected, selected[1:]):
        names = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")
        deltas = {name: current.get(f"cpu_{name}", 0) - previous.get(f"cpu_{name}", 0) for name in names}
        total = sum(deltas.values())
        if total > 0:
            busy.append(1.0 - (deltas["idle"] + deltas["iowait"]) / total)
            steal.append(deltas["steal"] / total)
    first = selected[0] if selected else {}
    last = selected[-1] if selected else {}
    elapsed = (last.get("timestamp_unix_ns", 0) - first.get("timestamp_unix_ns", 0)) / 1e9
    deltaRate = lambda key: (last.get(key, 0) - first.get(key, 0)) / elapsed if elapsed > 0 else None
    return {
        "sampleCount": len(selected),
        "cpuBusyMean": statistics.fmean(busy) if busy else None,
        "cpuBusyMax": max(busy, default=None),
        "cpuStealMean": statistics.fmean(steal) if steal else None,
        "runQueueMean": statistics.fmean(item["run_queue"] for item in selected) if selected else None,
        "runQueueMax": max((item["run_queue"] for item in selected), default=None),
        "cpuPsiSomeAvg10Mean": statistics.fmean(item.get("psi_cpu_some_avg10", 0.0) for item in selected) if selected else None,
        "cpuPsiSomeAvg10Max": max((item.get("psi_cpu_some_avg10", 0.0) for item in selected), default=None),
        "contextSwitchesPerSecond": deltaRate("ctxt"),
        "softirqPerSecond": deltaRate("softirq"),
        "cgroupThrottledUsecDelta": (last.get("cgroup_throttled_usec", 0) - first.get("cgroup_throttled_usec", 0)) if selected else None,
        "memoryAvailableMinKiB": min((item.get("memory_memavailable_kib", 0) for item in selected), default=None),
    }


def aggregateBackground(records: list[dict[str, Any]], expected: int) -> dict[str, Any]:
    """Aggregate background task accounting without hiding missing Pods."""
    totals = {key: sum(int(item.get(key, 0)) for item in records)
              for key in ("planned_arrivals", "started", "completed", "completed_on_time", "dropped", "pending_at_end")}
    planned = totals["planned_arrivals"]
    return {
        "expectedPods": expected,
        "reportedPods": len(records),
        **totals,
        "completionRate": totals["completed"] / planned if planned else (1.0 if expected == 0 else None),
        "onTimeRate": totals["completed_on_time"] / planned if planned else (1.0 if expected == 0 else None),
        "dropRate": totals["dropped"] / planned if planned else 0.0,
        "countConservationOk": len(records) == expected and all(item.get("count_conservation_ok") for item in records),
    }


def cleanRemoteControl(access: ClusterAccess, controlPath: str) -> None:
    """Remove only the run-owned worker control directory."""
    script = "import pathlib,shutil,sys; p=pathlib.Path(sys.argv[1]); shutil.rmtree(p) if p.exists() else None"
    remote = f"sudo python3 -c {shlex.quote(script)} {shlex.quote(controlPath)}"
    sshCommand(access, remote, check=False)


def executePoint(access: ClusterAccess, binary: Path, image: str, runDir: Path,
                 config: dict[str, Any], phase: str, blockId: int, group: str,
                 podCount: int, workloadUnits: dict[str, int],
                 readyBarrier: threading.Barrier | None = None) -> dict[str, Any]:
    """Deploy, trigger, collect, classify, and fully clean one E1-A point."""
    e1a = config["e1a"]
    phaseConfig = e1a[phase]
    workload = e1a["workload"]
    pointId = f"b{blockId:02d}-{group}-n{podCount:03d}"
    namespace = f"{e1a['namespacePrefix']}-{phase[:1]}{blockId:02d}-{group[:1]}{podCount:03d}"
    pointDir = runDir / "raw" / phase / pointId
    pointDir.mkdir(parents=True, exist_ok=True)
    controlPath = f"/var/lib/seedemu-e1a/{namespace}"
    warmup = float(phaseConfig["warmupSeconds"])
    measurement = float(phaseConfig["measurementSeconds"])
    common = ["--phase-seed", str(e1a["phaseSeed"]), "--max-backlog-seconds", str(workload["maxBacklogSeconds"]),
              "--warmup-seconds", str(warmup), "--duration-seconds", str(measurement),
              "--epoch-file", "/control/epoch"]
    canaryArgs = ["--jobs-per-second", str(workload["canaryJobsPerSecond"]),
                  "--work-units", str(workloadUnits["canary"]),
                  "--deadline-microseconds", str(workload["canaryDeadlineMicroseconds"]),
                  "--pod-id", "canary", *common]
    backgroundRate = workload["backgroundJobsPerSecond"] if group == "loaded" else 0
    backgroundArgs = ["--jobs-per-second", str(backgroundRate),
                      "--work-units", str(workloadUnits["background"]),
                      "--deadline-microseconds", str(workload["backgroundDeadlineMicroseconds"]), *common]
    startedIso = datetime.now(timezone.utc).isoformat()
    status = "harness_invalid"
    reason = "point did not finish"
    collector: tuple[subprocess.Popen[str], IO[str], IO[str]] | None = None
    epochNs = 0
    try:
        kubectl(access, ["create", "namespace", namespace])
        objects = [createConfigMap(namespace, binary),
                   jobObject(namespace, "canary", "canary", 1, access, image, canaryArgs,
                             workload["cpuRequest"], workload["memoryRequest"], controlPath)]
        if podCount:
            objects.append(jobObject(namespace, "background", "background", podCount, access, image,
                                     backgroundArgs, workload["cpuRequest"], workload["memoryRequest"], controlPath))
        (pointDir / "manifest.yaml").write_text("---\n".join(yaml.safe_dump(item, sort_keys=False) for item in objects), encoding="utf-8")
        applyObjects(access, objects)
        waitForPointPods(access, namespace, podCount + 1, int(phaseConfig["pointReadyTimeoutSeconds"]))
        podSnapshot = json.loads(kubectl(access, ["-n", namespace, "get", "pods", "-o", "json"]).stdout)
        writeJson(pointDir / "pods_ready.json", podSnapshot)
        if readyBarrier is not None:
            readyBarrier.wait(timeout=int(phaseConfig["pointReadyTimeoutSeconds"]))
        epochNs = triggerEpoch(access, controlPath, float(phaseConfig["pointStartLeadSeconds"]))
        collectorDuration = float(phaseConfig["pointStartLeadSeconds"]) + warmup + measurement + 3
        collector = startNodeCollector(access, epochNs, collectorDuration,
                                       float(e1a["measurement"]["nodeSampleIntervalSeconds"]),
                                       pointDir / "node_metrics.jsonl", pointDir / "node_metrics.stderr")
        jobTimeout = int(warmup + measurement + phaseConfig["pointStartLeadSeconds"] + 120)
        waitForJob(access, namespace, "canary", jobTimeout)
        if podCount:
            waitForJob(access, namespace, "background", jobTimeout)
        if collector:
            collector[0].wait(timeout=30)
            collector[1].close()
            collector[2].close()
            collector = None
        canaryRecords = readRoleLogs(access, namespace, "canary", 2)
        backgroundRecords = readRoleLogs(access, namespace, "background", max(2, podCount + 2)) if podCount else []
        with (pointDir / "canary.jsonl").open("w", encoding="utf-8") as handle:
            for record in canaryRecords:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
        with (pointDir / "background.jsonl").open("w", encoding="utf-8") as handle:
            for record in backgroundRecords:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
        if len(canaryRecords) != 1:
            raise RuntimeError(f"expected one canary record, got {len(canaryRecords)}")
        canary = canaryRecords[0]
        background = aggregateBackground(backgroundRecords, podCount)
        canaryPlanned = int(canary.get("planned_arrivals", 0))
        canaryOnTimeRate = int(canary.get("completed_on_time", 0)) / canaryPlanned if canaryPlanned else None
        measurementStartNs = epochNs + int(warmup * 1e9)
        measurementEndNs = measurementStartNs + int(measurement * 1e9)
        nodeMetrics = summarizeNodeMetrics(pointDir / "node_metrics.jsonl", measurementStartNs, measurementEndNs)
        accountingOk = bool(canary.get("count_conservation_ok")) and background["countConservationOk"]
        if not accountingOk:
            status, reason = "harness_invalid", "workload task accounting or Pod log count failed"
        elif (canary.get("dropped", 0) or canary.get("pending_at_end", 0)
              or (canaryOnTimeRate is not None and canaryOnTimeRate < float(e1a["measurement"]["overloadOnTimeThreshold"]))
              or (background.get("onTimeRate") is not None and background["onTimeRate"] < float(e1a["measurement"]["overloadOnTimeThreshold"]))):
            status, reason = "workload_overload", "bounded workload completed with lateness, drops, or pending tasks"
        else:
            status, reason = "success", "all planned task accounting and timeliness checks passed"
        summary = {
            "experiment": "E1-A-marginal-pods", "phase": phase, "blockId": blockId,
            "pointId": pointId, "group": group, "N": podCount,
            "workloadPodCount": podCount, "canaryPodCount": 1,
            "pointPodCount": podCount + 1, "namespace": namespace,
            "workerName": access.workerName, "epochUnixNs": epochNs,
            "warmupSeconds": warmup, "measurementSeconds": measurement,
            "startedAt": startedIso, "finishedAt": datetime.now(timezone.utc).isoformat(),
            "status": status, "reason": reason,
            "canary": canary, "canaryOnTimeRate": canaryOnTimeRate,
            "background": background, "nodeMetrics": nodeMetrics,
            "workloadUnits": workloadUnits,
        }
        writeJson(pointDir / "point_summary.json", summary)
        return summary
    except TimeoutError as error:
        if readyBarrier is not None:
            readyBarrier.abort()
        summary = {
            "experiment": "E1-A-marginal-pods", "phase": phase, "blockId": blockId,
            "pointId": pointId, "group": group, "N": podCount, "namespace": namespace,
            "startedAt": startedIso, "finishedAt": datetime.now(timezone.utc).isoformat(),
            "status": "admission_failed", "reason": str(error), "epochUnixNs": epochNs,
        }
        writeJson(pointDir / "point_summary.json", summary)
        return summary
    except Exception as error:
        if readyBarrier is not None:
            readyBarrier.abort()
        summary = {
            "experiment": "E1-A-marginal-pods", "phase": phase, "blockId": blockId,
            "pointId": pointId, "group": group, "N": podCount, "namespace": namespace,
            "startedAt": startedIso, "finishedAt": datetime.now(timezone.utc).isoformat(),
            "status": status, "reason": f"{type(error).__name__}: {error}", "epochUnixNs": epochNs,
        }
        writeJson(pointDir / "point_summary.json", summary)
        raise
    finally:
        if collector:
            collector[0].terminate()
            try:
                collector[0].wait(timeout=10)
            except subprocess.TimeoutExpired:
                collector[0].kill()
            collector[1].close()
            collector[2].close()
        kubectl(access, ["delete", "namespace", namespace, "--wait=true", "--timeout=300s"],
                timeout=330, check=False)
        cleanRemoteControl(access, controlPath)


def pilotPlan(config: dict[str, Any], admission: dict[str, Any]) -> list[tuple[int, str, int]]:
    """Return the frozen exploratory ascending plan within real admission."""
    phase = config["e1a"]["pilot"]
    limit = int(admission["maxBackgroundN"])
    points = [(1, "loaded", int(value)) for value in phase["loadedN"] if int(value) <= limit]
    points.extend((1, "idle", int(value)) for value in phase.get("idleN", []) if int(value) <= limit)
    return points


def formalPlan(config: dict[str, Any], admission: dict[str, Any], knee: dict[str, Any]) -> list[tuple[int, str, int]]:
    """Freeze coarse plus predeclared knee-neighbor points and balanced orders."""
    formal = config["e1a"]["formal"]
    limit = int(admission["maxBackgroundN"])
    counts = {int(value) for value in formal["coarseN"] if int(value) <= limit}
    candidate = knee.get("selectedKneeN") or knee.get("firstReferenceCrossingN")
    if candidate is not None:
        radius = int(formal["fineRadiusPods"])
        step = int(formal["fineStepPods"])
        counts.update(value for value in range(max(0, int(candidate) - radius), int(candidate) + radius + 1, step)
                      if value <= limit)
    base = [(group, count) for count in sorted(counts) for group in ("loaded", "idle")]
    plan: list[tuple[int, str, int]] = []
    for block in range(1, int(formal["repetitions"]) + 1):
        order = list(base)
        random.Random(int(formal["randomSeed"]) + block).shuffle(order)
        plan.extend((block, group, count) for group, count in order)
    return plan


def writePlan(runDir: Path, phase: str, plan: list[tuple[int, str, int]], config: dict[str, Any],
              admission: dict[str, Any], workloadUnits: dict[str, int]) -> None:
    """Freeze the exact point order and workload/admission inputs before use."""
    payload = {
        "phase": phase, "createdAt": datetime.now(timezone.utc).isoformat(),
        "points": [{"order": index + 1, "blockId": block, "group": group, "N": count}
                   for index, (block, group, count) in enumerate(plan)],
        "admission": admission, "workloadUnits": workloadUnits,
        "phaseConfig": config["e1a"][phase],
    }
    writeJson(runDir / "plans" / f"{phase}_plan.json", payload)


def executePlan(access: ClusterAccess, binary: Path, image: str, runDir: Path,
                config: dict[str, Any], phase: str, plan: list[tuple[int, str, int]],
                workloadUnits: dict[str, int]) -> list[dict[str, Any]]:
    """Execute every frozen point, retaining overload as valid evidence."""
    results: list[dict[str, Any]] = []
    for index, (block, group, count) in enumerate(plan, start=1):
        print(f"[{datetime.now().isoformat(timespec='seconds')}] E1-A {phase} "
              f"point {index}/{len(plan)} block={block} group={group} N={count}", flush=True)
        result = executePoint(access, binary, image, runDir, config, phase, block, group, count, workloadUnits)
        results.append(result)
        print(f"  status={result['status']} reason={result['reason']}", flush=True)
        if result["status"] in {"admission_failed", "safety_stopped"} and phase == "pilot":
            break
    return results


def executePlanParallel(accesses: list[ClusterAccess], binary: Path, images: dict[str, str],
                        runDir: Path, config: dict[str, Any], phase: str,
                        plan: list[tuple[int, str, int]], workloadUnits: dict[str, int],
                        parallelism: int) -> list[dict[str, Any]]:
    """Execute synchronized waves with at most one point on each worker."""
    width = min(max(1, parallelism), len(accesses))
    results: list[dict[str, Any]] = []
    waveCount = (len(plan) + width - 1) // width
    for waveIndex, offset in enumerate(range(0, len(plan), width), start=1):
        wave = plan[offset:offset + width]
        barrier = threading.Barrier(len(wave))
        print(f"[{datetime.now().isoformat(timespec='seconds')}] E1-A {phase} "
              f"wave {waveIndex}/{waveCount} points={len(wave)}", flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(wave)) as pool:
            futures: list[concurrent.futures.Future[dict[str, Any]]] = []
            for slot, (block, group, count) in enumerate(wave):
                access = accesses[(waveIndex - 1 + slot) % len(accesses)]
                print(f"  worker={access.workerName} block={block} group={group} N={count}", flush=True)
                futures.append(pool.submit(
                    executePoint, access, binary, images[access.workerName], runDir, config,
                    phase, block, group, count, workloadUnits, barrier,
                ))
            waveResults = [future.result() for future in futures]
        results.extend(waveResults)
        print("  " + ", ".join(f"{item['pointId']}={item['status']}" for item in waveResults), flush=True)
    return results


def destroyCluster(runDir: Path) -> dict[str, Any]:
    """Destroy only infrastructure described by this run's generated config."""
    configK3s = runDir / "configK3s.yaml"
    if not configK3s.exists():
        return {"attempted": False, "reason": "configK3s.yaml absent"}
    result = executeCommand([sys.executable, str(K8S_TOOLS), "destroy", "-d", str(configK3s), "--keep-temp"],
                            cwd=K8S_TOOLS.parent, logPath=runDir / "logs/destroy_cluster.log",
                            timeout=3600, check=False)
    return {"attempted": True, "returncode": result.returncode,
            "finishedAt": datetime.now(timezone.utc).isoformat()}


def parseArguments() -> argparse.Namespace:
    """Parse orchestration options; persistent experiment choices remain YAML."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--phase", choices=("pilot", "formal", "all"), default="all")
    parser.add_argument("--reuse-cluster", action="store_true")
    parser.add_argument("--keep-cluster", action="store_true")
    return parser.parse_args()


def executeExperiment(arguments: argparse.Namespace) -> int:
    """Coordinate cluster setup, calibration, scans, analysis, and cleanup."""
    configPath = arguments.config.resolve()
    config = readYaml(configPath)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    runDir = (arguments.run_dir or HERE / f"experiments/e1_marginal_pods/{timestamp}").resolve()
    isNew = not runDir.exists()
    if isNew:
        prepareRunDirectory(runDir, configPath)
        snapshotEnvironment(runDir)
    binary = compileWorkload(runDir)
    workerNames = [config["defaults"]["workerNamePrefix"] + str(index)
                   for index in range(1, int(config["workers"]["count"]) + 1)]
    cleanup = {
        "ownedVmNames": [config["defaults"]["masterName"], *workerNames],
        "ownedLibvirtNetwork": config.get("kvm", {}).get("network"),
        "ownedStorageDir": config.get("kvm", {}).get("storageDir"),
        "namespacePrefix": config["e1a"]["namespacePrefix"],
        "destroy": None,
    }
    writeJson(runDir / "cleanup_manifest.json", cleanup)
    completed = False
    try:
        if not arguments.reuse_cluster:
            buildCluster(runDir, configPath)
        accesses = clusterAccesses(runDir, config)
        access = accesses[0]
        waitForCluster(access, runDir)
        pinExperimentVcpus(config, runDir)
        for workerAccess in accesses:
            copyCollector(workerAccess, runDir)
        images = {workerAccess.workerName: findPauseImage(workerAccess) for workerAccess in accesses}
        admissions = {workerAccess.workerName: readAdmission(workerAccess) for workerAccess in accesses}
        admission = {
            "maxBackgroundN": min(item["maxBackgroundN"] for item in admissions.values()),
            "workerCount": len(accesses),
            "byWorker": admissions,
        }
        writeJson(runDir / "env_snapshot/admission.json", admission)
        workload = config["e1a"]["workload"]
        unitsPath = runDir / "pilot/calibrated_work_units.json"
        if unitsPath.exists():
            workloadUnits = json.loads(unitsPath.read_text(encoding="utf-8"))
        else:
            backgroundUnits = workload.get("backgroundWorkUnits")
            canaryUnits = workload.get("canaryWorkUnits")
            backgroundCalibration = None
            canaryCalibration = None
            if backgroundUnits is None:
                backgroundCalibration = calibrateOne(access, binary, images[access.workerName], runDir, "background",
                                                     float(workload["backgroundTargetCpuMicroseconds"]),
                                                     workload["cpuRequest"], workload["memoryRequest"])
                backgroundUnits = int(backgroundCalibration["recommended_work_units"])
            if canaryUnits is None:
                canaryCalibration = calibrateOne(access, binary, images[access.workerName], runDir, "canary",
                                                 float(workload["canaryTargetCpuMicroseconds"]),
                                                 workload["cpuRequest"], workload["memoryRequest"])
                canaryUnits = int(canaryCalibration["recommended_work_units"])
            workloadUnits = {"background": int(backgroundUnits), "canary": int(canaryUnits)}
            writeJson(unitsPath, workloadUnits)
        metadata = {
            "experiment": "E1-A-marginal-pods", "runDir": str(runDir),
            "createdAt": datetime.now(timezone.utc).isoformat(), "phaseRequested": arguments.phase,
            "pauseImages": images, "binarySha256": sha256File(binary),
            "sourceSha256": sha256File(WORKLOAD_SOURCE), "workloadUnits": workloadUnits,
            "relativeSlowdownReference": config["e1a"]["measurement"]["relativeSlowdownReference"],
            "overloadOnTimeThreshold": config["e1a"]["measurement"]["overloadOnTimeThreshold"],
            "admission": admission,
        }
        writeJson(runDir / "run_metadata.json", metadata)
        knee: dict[str, Any] = {}
        if arguments.phase in {"pilot", "all"}:
            plan = pilotPlan(config, admission)
            writePlan(runDir, "pilot", plan, config, admission, workloadUnits)
            executePlan(access, binary, images[access.workerName], runDir, config, "pilot", plan, workloadUnits)
            knee = analyzer.analyzeExperiment(runDir, "pilot")
            print(json.dumps({"pilotKnee": knee}, indent=2), flush=True)
        if arguments.phase in {"formal", "all"}:
            if not knee:
                kneePath = runDir / "metrics/knee.json"
                if not kneePath.exists():
                    reference = config["e1a"].get("pilotReference", {}).get("runDir")
                    if not reference:
                        raise RuntimeError("formal phase requires local knee.json or e1a.pilotReference.runDir")
                    referenceDir = Path(str(reference))
                    if not referenceDir.is_absolute():
                        referenceDir = HERE / referenceDir
                    referenceKnee = referenceDir.resolve() / "metrics/knee.json"
                    if not referenceKnee.is_file():
                        raise RuntimeError(f"pilot reference knee not found: {referenceKnee}")
                    kneePath = referenceKnee
                    writeJson(runDir / "plans/pilot_reference.json", {
                        "runDir": str(referenceDir.resolve()),
                        "kneePath": str(referenceKnee),
                        "kneeSha256": sha256File(referenceKnee),
                    })
                knee = json.loads(kneePath.read_text(encoding="utf-8"))
            plan = formalPlan(config, admission, knee)
            writePlan(runDir, "formal", plan, config, admission, workloadUnits)
            parallelism = int(config["e1a"]["formal"].get("parallelism", 1))
            if parallelism > 1:
                executePlanParallel(accesses, binary, images, runDir, config, "formal", plan,
                                    workloadUnits, parallelism)
            else:
                executePlan(access, binary, images[access.workerName], runDir, config, "formal", plan,
                            workloadUnits)
            analyzer.analyzeExperiment(runDir, "formal")
        completed = True
        print(f"E1-A artifacts: {runDir}", flush=True)
        return 0
    finally:
        destroyAfter = bool(config["e1a"]["cleanup"].get("destroyClusterAfterRun", True))
        if completed and destroyAfter and not arguments.keep_cluster:
            cleanup["destroy"] = destroyCluster(runDir)
        else:
            cleanup["destroy"] = {"attempted": False, "reason": "run incomplete or cluster retained by option"}
        writeJson(runDir / "cleanup_manifest.json", cleanup)


if __name__ == "__main__":
    raise SystemExit(executeExperiment(parseArguments()))
