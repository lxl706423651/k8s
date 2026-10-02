#!/usr/bin/env python3
"""Run the E1-B topology214 pilot on one reusable four-worker K3s cluster.

The pilot validates frozen mappings, real admission, BIRD/FIB state and the 16
fixed direct-link flows.  It also performs short UDP/TCP windows under the
frozen E1-A per-Pod workload.  Pilot data is kept separate from formal blocks.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import copy
import json
import shutil
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, IO

import yaml

import executeE1aExperiment as e1a
import planE1bExperiment as planner


HERE = Path(__file__).resolve().parent
B62_DIR = HERE.parent / "b62_k8s_scale"
sys.path.insert(0, str(B62_DIR))
import runFullExperiment as shared


DEFAULT_CONFIG = HERE / "research_configs/configE1bTopology214FourWorkers.yaml"
DEFAULT_PLAN_DIR = HERE / "experiments/e1_marginal_pods/e1b_topology214_frozen_20260919/plans"


def writeJson(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def readPlan(runDir: Path) -> dict[str, Any]:
    return json.loads((runDir / "e1b/plans/placement_sequence.json").read_text())


def mappingById(runDir: Path, mappingId: str) -> dict[str, Any]:
    for item in readPlan(runDir)["mappings"]:
        if item["mappingId"] == mappingId:
            return item
    raise KeyError(mappingId)


def prepareArtifacts(runDir: Path, configPath: Path, planDir: Path) -> None:
    root = runDir / "e1b"
    for name in ("plans", "pilot", "logs", "workload_source", "env_snapshot"):
        (root / name).mkdir(parents=True, exist_ok=True)
    shutil.copy2(configPath, root / "config.yaml")
    shutil.copy2(HERE / "E1_DESIGN_PROPOSAL.md", root / "design_frozen.md")
    for path in planDir.iterdir():
        if path.is_file():
            shutil.copy2(path, root / "plans" / path.name)
    for source in (Path(__file__).resolve(), HERE / "planE1bExperiment.py",
                   HERE / "measureE1bDataplane.py", HERE / "e1aWorkload.c",
                   HERE / "collectE1aNodeMetrics.py"):
        shutil.copy2(source, root / "workload_source" / source.name)
    e1a.snapshotEnvironment(root)
    writeJson(root / "cleanup_manifest.json", {
        "ownedVmNames": ["b63-e1b-master", "b63-e1b-worker1", "b63-e1b-worker2",
                         "b63-e1b-worker3", "b63-e1b-worker4"],
        "ownedLibvirtNetworks": ["seedemu-b63-e1b-214", "seedemu-b63-e1b-214-fabric0",
                                 "seedemu-b63-e1b-214-fabric1"],
        "ownedStorageDir": "/data/lxl/k8sTools/b63-e1b-214",
        "namespace": "seedemu-b63-e1b-214",
    })


def loadCompiledWorkloads(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    docs = [item for item in yaml.safe_load_all(path.read_text()) if isinstance(item, dict)]
    names = sorted(str((item.get("metadata") or {}).get("name") or "")
                   for item in docs if item.get("kind") == "Deployment")
    return docs, names


def writeMappedManifest(runDir: Path, mappingId: str) -> None:
    source = runDir / "output/k8s.source.yaml"
    docs, names = loadCompiledWorkloads(source)
    mapping = mappingById(runDir, mappingId)
    if set(names) != set(mapping["assignments"]):
        raise RuntimeError(f"compiled workload set differs from frozen mapping: compiled={len(names)} "
                           f"planned={len(mapping['assignments'])}")
    rendered = planner.injectManifest(docs, mapping["assignments"])
    for doc in rendered:
        if doc.get("kind") == "Deployment":
            name = str((doc.get("metadata") or {}).get("name") or "")
            if name in mapping["assignments"]:
                metadata = (((doc.get("spec") or {}).get("template") or {}).get("metadata") or {})
                metadata.setdefault("annotations", {})["seedemu.k8s.e1b/mapping-id"] = mappingId
    (runDir / "output/k8s.yaml").write_text(yaml.safe_dump_all(rendered, sort_keys=False))
    writeJson(runDir / "output/placement_report.json", {
        "metadata": {"selectedAlgorithm": "e1b-frozen", "includeMaster": False,
                     "placementKind": "hard-placement", "mappingId": mappingId},
        "nodes": [{"name": name, "schedulable": True} for name in readPlan(runDir)["metadata"]["workers"]],
        "pods": [{"name": name, "replicas": 1} for name in names],
        "placements": {"e1b-frozen": mapping["assignments"]},
        "staticValidation": {"e1b-frozen": {key: mapping[key] for key in (
            "mappingId", "placementHash", "targetWorkerPodCount", "totalEmulationPods", "perNode",
            "fixedFlowCrossings", "xExpectedPayloadMbpsCrossings", "topologyProxy")}},
    })
    writeJson(runDir / "e1b/current_mapping.json", mapping)


def applyInitialMapping(summary: dict[str, Any], summaryPath: Path, runDir: Path,
                        env: dict[str, str]) -> None:
    output = runDir / "output"
    shutil.copy2(output / "k8s.yaml", output / "k8s.b62.yaml")
    shutil.copy2(output / "k8s.yaml", output / "k8s.source.yaml")
    mappingId = str((shared.loadYaml(runDir / "assignment.yaml").get("e1b") or {})["pilot"]["mappingIds"][0])
    writeMappedManifest(runDir, mappingId)
    config = shared.loadYaml(runDir / "assignment.yaml")
    e1a.pinExperimentVcpus({**config, "e1a": {"pinning": config["e1b"]["pinning"]}}, runDir / "e1b")
    summary["e1b"] = {"phase": "pilot", "currentMapping": mappingId,
                      "plan": str(runDir / "e1b/plans/placement_sequence.json")}
    shared.writeJson(summaryPath, summary)


def verifyLivePlacement(runDir: Path, env: dict[str, str], output: Path) -> dict[str, Any]:
    mapping = json.loads((runDir / "e1b/current_mapping.json").read_text())
    command = ["kubectl", "--kubeconfig", env["B62_KUBECONFIG_PATH"], "-n", env["SEED_NAMESPACE"],
               "get", "pods", "-l", "seedemu.io/workload=seedemu", "-o", "json"]
    result = subprocess.run(command, text=True, capture_output=True, timeout=180, check=False)
    failures: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    actual: dict[str, str] = {}
    if result.returncode != 0:
        failures.append({"reason": result.stderr.strip(), "returncode": result.returncode})
        pods: list[dict[str, Any]] = []
    else:
        pods = json.loads(result.stdout).get("items") or []
    for pod in pods:
        metadata = pod.get("metadata") or {}
        labels = metadata.get("labels") or {}
        workload = str(labels.get("app") or metadata.get("name") or "")
        node = str((pod.get("spec") or {}).get("nodeName") or "")
        ready = any(item.get("type") == "Ready" and item.get("status") == "True"
                    for item in (pod.get("status") or {}).get("conditions") or [])
        counts[node] += 1
        actual[workload] = node
        if mapping["assignments"].get(workload) != node or not ready:
            failures.append({"pod": metadata.get("name"), "workload": workload,
                             "expected": mapping["assignments"].get(workload), "actual": node, "ready": ready})
    if len(pods) != 214 or set(actual) != set(mapping["assignments"]):
        failures.append({"reason": "live workload set is not the frozen 214-Pod set",
                         "actualPodCount": len(pods), "actualWorkloadCount": len(actual)})
    summary = {"status": "PASS" if not failures else "FAIL", "mappingId": mapping["mappingId"],
               "podCount": len(pods), "perNode": dict(sorted(counts.items())),
               "allReadyAndCorrectlyPlaced": not failures, "failures": failures,
               "actualPlacement": actual}
    writeJson(output, summary)
    if failures:
        raise RuntimeError(f"live E1-B placement verification failed: {failures[:3]}")
    return summary


def afterReady(summary: dict[str, Any], summaryPath: Path, runDir: Path, env: dict[str, str]) -> None:
    result = verifyLivePlacement(runDir, env, runDir / "placement_actual_summary.json")
    summary.setdefault("tests", {})["e1bPlacement"] = result
    shared.writeJson(summaryPath, summary)


def runLogged(name: str, command: list[str], summary: dict[str, Any], summaryPath: Path,
              runDir: Path, env: dict[str, str], logPath: Path) -> None:
    shared.runStage(name, command, summary=summary, summary_path=summaryPath,
                    log_path=logPath, env=env)


def runDataplane(mode: str, runDir: Path, env: dict[str, str], output: Path,
                 duration: int = 15, repetitions: int = 1) -> None:
    command = [sys.executable, str(HERE / "measureE1bDataplane.py"), "--mode", mode,
               "--kubeconfig", env["B62_KUBECONFIG_PATH"], "--namespace", env["SEED_NAMESPACE"],
               "--pair-plan", str(runDir / "e1b/plans/selected_pair_plan.json"),
               "--output-dir", str(output), "--duration-seconds", str(duration),
               "--repetitions", str(repetitions)]
    if mode == "udp":
        command.extend(["--inventory", env["B62_INVENTORY_PATH"]])
    result = subprocess.run(command, cwd=HERE, text=True, capture_output=True, timeout=1800, check=False)
    (output.parent / f"{output.name}.log").write_text(result.stdout + result.stderr)
    if result.returncode != 0:
        raise RuntimeError(f"E1-B {mode} measurement failed; see {output.parent / (output.name + '.log')}")


def preloadMappingImages(runDir: Path, mappingId: str, output: Path) -> dict[str, Any]:
    """Pull each mapping's images to its assigned workers before deployment."""
    config = shared.loadYaml(runDir / "assignment.yaml")
    accesses = {item.workerName: item for item in e1a.clusterAccesses(runDir, config)}
    docs = [item for item in yaml.safe_load_all((runDir / "output/k8s.yaml").read_text())
            if isinstance(item, dict)]
    perNode: dict[str, set[str]] = {name: set() for name in accesses}
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        podSpec = (((doc.get("spec") or {}).get("template") or {}).get("spec") or {})
        selector = podSpec.get("nodeSelector") or {}
        node = str(selector.get("kubernetes.io/hostname") or "")
        if node not in perNode:
            raise RuntimeError(f"{doc.get('metadata', {}).get('name')} has invalid preload node {node!r}")
        for container in (podSpec.get("initContainers") or []) + (podSpec.get("containers") or []):
            image = str(container.get("image") or "").strip()
            if image:
                perNode[node].add(image)

    output.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {}
    unionMarker = runDir / "e1b/image_union_preloaded.json"

    def importArchiveUnion() -> None:
        """Import every initial-build archive on every worker.

        Fast archive mode intentionally distributes images by the first
        mapping. Later static mappings can move a Pod to another node, so the
        union of those archives is the deterministic preload set for this
        sequence. The archives remain on the controller VM's master under the
        namespace-specific build work directory.
        """
        inventory = e1a.readYaml(runDir / "inventory.yaml")
        master = next((item for item in inventory.get("nodes", [])
                       if item.get("role") == "master"), None)
        if not master:
            raise RuntimeError("inventory has no master for archive transfer")
        masterIp = str(master.get("managementIp") or master.get("ip"))
        sshConfig = config.get("ssh", {})
        masterSsh = master.get("ssh") if isinstance(master.get("ssh"), dict) else {}
        user = str(masterSsh.get("user") or sshConfig.get("user", "ubuntu"))
        key = str(masterSsh.get("key") or sshConfig.get("key", "~/.ssh/id_ed25519"))
        archiveRoot = f"/tmp/seedemu-build-{config['experiment']['namespace']}/node_archives"
        archiveNames = [f"images_{node}.tar" for node in sorted(accesses)]

        def importOnWorker(node: str) -> tuple[str, dict[str, Any]]:
            access = accesses[node]
            log = output / f"archive_union_{node}.log"
            lines: list[str] = []
            for archiveName in archiveNames:
                archivePath = f"{archiveRoot}/{archiveName}"
                source = subprocess.Popen(
                    ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
                     "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", "-i", key,
                     f"{user}@{masterIp}", f"sudo -n cat {archivePath}"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                destination = subprocess.Popen(
                    ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
                     "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", "-i", str(access.sshKey),
                     f"{access.sshUser}@{access.workerIp}", "sudo -n k3s ctr images import -"],
                    stdin=source.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                assert source.stdout is not None
                source.stdout.close()
                dstOut, dstErr = destination.communicate(timeout=1800)
                srcOut, srcErr = source.communicate(timeout=30)
                lines.append(f"{archiveName}: source_rc={source.returncode} destination_rc={destination.returncode}\n"
                             f"{dstOut.decode(errors='replace')}\n{dstErr.decode(errors='replace')}\n"
                             f"{srcErr.decode(errors='replace')}")
                if source.returncode != 0 or destination.returncode != 0:
                    log.write_text("\n".join(lines))
                    raise RuntimeError(f"archive union import failed on {node}: {archiveName}")
            log.write_text("\n".join(lines))
            return node, {"archiveCount": len(archiveNames), "status": "PASS"}

        unionResults: dict[str, Any] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(accesses)) as pool:
            for node, result in pool.map(importOnWorker, sorted(accesses)):
                unionResults[node] = result
        writeJson(unionMarker, {"status": "PASS", "mappingId": mappingId,
                               "archiveRoot": archiveRoot, "perNode": unionResults})

    script = r'''set -euo pipefail
expected=$(mktemp)
current=$(mktemp)
missing=$(mktemp)
trap 'rm -f "$expected" "$current" "$missing"' EXIT
LC_ALL=C sort -u >"$expected"
checked=$(wc -l <"$expected")
# Fast archive mode intentionally does not publish every image tag to the
# registry.  Treat the four successfully imported archives as the source of
# truth and wait for containerd to expose every required tag locally.
for attempt in $(seq 1 300); do
  sudo -n k3s ctr images list -q | LC_ALL=C sort -u >"$current"
  comm -23 "$expected" "$current" >"$missing"
  if [ ! -s "$missing" ]; then
    echo "summary checked=$checked local_ready=$checked waited_seconds=$((attempt - 1))"
    exit 0
  fi
  sleep 1
done
echo "missing local images after 300 seconds:" >&2
cat "$missing" >&2
exit 1
'''

    def preload(node: str) -> tuple[str, dict[str, Any]]:
        access = accesses[node]
        images = sorted(perNode[node])
        command = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
                   "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR",
                   "-i", str(access.sshKey), f"{access.sshUser}@{access.workerIp}",
                   "sudo -n bash -s"]
        # Encode both files so image references remain data, never shell source.
        import base64
        encodedScript = base64.b64encode(script.encode()).decode()
        encodedImages = base64.b64encode(("\n".join(images) + "\n").encode()).decode()
        listPath = f"/tmp/e1b-preload-{mappingId}.txt"
        scriptPath = f"/tmp/e1b-preload-{mappingId}.sh"
        remoteCommand = (f"printf %s {encodedImages} | base64 -d >{listPath} && "
                         f"printf %s {encodedScript} | base64 -d >{scriptPath} && "
                         f"sudo -n bash {scriptPath} <{listPath} && "
                         f"rm -f {listPath} {scriptPath}")
        completed = subprocess.run(command[:-1] + [remoteCommand], text=True, capture_output=True,
                                   timeout=1800, check=False)
        (output / f"preload_{node}.log").write_text(completed.stdout + completed.stderr)
        if completed.returncode != 0:
            raise RuntimeError(f"image preload failed on {node}; see {output / ('preload_' + node + '.log')}")
        pulled = sum(line.startswith("pulled ") for line in completed.stdout.splitlines())
        return node, {"imageCount": len(images), "pulledCount": pulled,
                      "workerIp": access.workerIp, "status": "PASS"}

    if not unionMarker.is_file():
        importArchiveUnion()
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(accesses)) as pool:
        for node, result in pool.map(preload, sorted(accesses)):
            results[node] = result
    summary = {"status": "PASS", "mappingId": mappingId,
               "perNode": results, "totalUniqueNodeImages": sum(len(v) for v in perNode.values())}
    writeJson(output / "preload_summary.json", summary)
    return summary


def prepareLoadNamespace(accesses: list[e1a.ClusterAccess], binary: Path, image: str,
                         mapping: dict[str, Any], config: dict[str, Any], output: Path) -> tuple[str, str]:
    workload = config["e1b"]["workload"]
    namespace = "seedemu-e1b-load-" + mapping["mappingId"].lower().replace("_", "-")[:36]
    controlPath = f"/var/lib/seedemu-e1b/{namespace}"
    primary = accesses[0]
    e1a.kubectl(primary, ["delete", "namespace", namespace, "--ignore-not-found=true", "--wait=true"],
                 timeout=330, check=False)
    e1a.kubectl(primary, ["create", "namespace", namespace])
    common = ["--phase-seed", "20260918", "--max-backlog-seconds", str(workload["maxBacklogSeconds"]),
              "--warmup-seconds", "20", "--duration-seconds", "80", "--epoch-file", "/control/epoch"]
    objects = [e1a.createConfigMap(namespace, binary)]
    for index, access in enumerate(accesses, 1):
        count = int(mapping["perNode"][access.workerName]["emulationPods"])
        canaryArgs = ["--jobs-per-second", str(workload["canaryJobsPerSecond"]),
                      "--work-units", str(workload["canaryWorkUnits"]),
                      "--deadline-microseconds", str(workload["canaryDeadlineMicroseconds"]),
                      "--pod-id", f"canary-{access.workerName}", *common]
        backgroundArgs = ["--jobs-per-second", str(workload["backgroundJobsPerSecond"]),
                          "--work-units", str(workload["backgroundWorkUnits"]),
                          "--deadline-microseconds", str(workload["backgroundDeadlineMicroseconds"]), *common]
        objects.append(e1a.jobObject(namespace, f"canary-w{index}", "canary", 1, access, image,
                                     canaryArgs, workload["cpuRequest"], workload["memoryRequest"], controlPath))
        objects.append(e1a.jobObject(namespace, f"background-w{index}", "background", count, access, image,
                                     backgroundArgs, workload["cpuRequest"], workload["memoryRequest"], controlPath))
    (output / "workload_manifest.yaml").write_text(
        "---\n".join(yaml.safe_dump(item, sort_keys=False) for item in objects))
    e1a.applyObjects(primary, objects)
    expected = 214 + len(accesses)
    e1a.waitForPointPods(primary, namespace, expected, 1200)
    snapshot = json.loads(e1a.kubectl(primary, ["-n", namespace, "get", "pods", "-o", "json"]).stdout)
    writeJson(output / "workload_pods_ready.json", snapshot)
    return namespace, controlPath


def collectWorkloadLogs(access: e1a.ClusterAccess, namespace: str, output: Path) -> dict[str, Any]:
    pods = json.loads(e1a.kubectl(access, ["-n", namespace, "get", "pods", "-o", "json"]).stdout)
    records: list[dict[str, Any]] = []
    for item in pods.get("items") or []:
        metadata = item.get("metadata") or {}
        name = str(metadata.get("name"))
        result = e1a.kubectl(access, ["-n", namespace, "logs", name], timeout=60, check=False)
        parsed = e1a.parseJsonLogLines(result.stdout)
        for record in parsed:
            records.append({**record, "kubernetesPod": name,
                            "workerName": (item.get("spec") or {}).get("nodeName"),
                            "role": (metadata.get("labels") or {}).get("e1a.seedemu.io/role")})
    with (output / "workload_records.jsonl").open("w") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    backgrounds = [item for item in records if item.get("role") == "background"]
    canaries = [item for item in records if item.get("role") == "canary"]
    return {"recordCount": len(records), "background": e1a.aggregateBackground(backgrounds, 214),
            "canaries": canaries}


def measurePointWorkload(runDir: Path, env: dict[str, str], mapping: dict[str, Any],
                         pointDir: Path, modes: tuple[str, ...] = ("udp", "tcp"),
                         dataplaneDurations: dict[str, int] | None = None,
                         dataplaneRepetitions: dict[str, int] | None = None) -> dict[str, Any]:
    pointDir.mkdir(parents=True, exist_ok=True)
    config = shared.loadYaml(runDir / "assignment.yaml")
    accesses = e1a.clusterAccesses(runDir, config)
    binary = runDir / "e1b/workload_source/e1aWorkload"
    if not binary.is_file():
        binary = e1a.compileWorkload(runDir / "e1b")
    for access in accesses:
        e1a.copyCollector(access, runDir / "e1b")
    image = e1a.findPauseImage(accesses[0])
    namespace, controlPath = prepareLoadNamespace(accesses, binary, image, mapping, config, pointDir)
    collectors: list[tuple[subprocess.Popen[str], IO[str], IO[str]]] = []
    epochs: dict[str, int] = {}
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(e1a.triggerEpoch, access, controlPath, 10): access for access in accesses}
            for future, access in [(future, futures[future]) for future in futures]:
                epochs[access.workerName] = future.result()
        for access in accesses:
            collector = e1a.startNodeCollector(
                access, epochs[access.workerName], 113, 1,
                pointDir / f"node_metrics_{access.workerName}.jsonl",
                pointDir / f"node_metrics_{access.workerName}.stderr")
            collectors.append(collector)
        time.sleep(31)
        durations = {"udp": 20, "tcp": 15}
        if dataplaneDurations:
            durations.update({key: int(value) for key, value in dataplaneDurations.items()})
        repetitions = {"udp": 1, "tcp": 1}
        if dataplaneRepetitions:
            repetitions.update({key: int(value) for key, value in dataplaneRepetitions.items()})
        if "udp" in modes:
            runDataplane("udp", runDir, env, pointDir / "udp", duration=durations["udp"],
                         repetitions=repetitions["udp"])
        if "tcp" in modes:
            runDataplane("tcp", runDir, env, pointDir / "tcp", duration=durations["tcp"],
                         repetitions=repetitions["tcp"])
        for index in range(1, 5):
            e1a.waitForJob(accesses[0], namespace, f"canary-w{index}", 240)
            e1a.waitForJob(accesses[0], namespace, f"background-w{index}", 240)
        for process, stdout, stderr in collectors:
            process.wait(timeout=60)
            stdout.close(); stderr.close()
        collectors.clear()
        workload = collectWorkloadLogs(accesses[0], namespace, pointDir)
        perNodeMetrics = {}
        for access in accesses:
            start = epochs[access.workerName] + int(20e9)
            perNodeMetrics[access.workerName] = e1a.summarizeNodeMetrics(
                pointDir / f"node_metrics_{access.workerName}.jsonl", start, start + int(80e9))
        workload["perNodeMetrics"] = perNodeMetrics
        workload["status"] = ("success" if workload["background"]["countConservationOk"]
                              and len(workload["canaries"]) == 4 else "harness_invalid")
        writeJson(pointDir / "workload_summary.json", workload)
        return workload
    finally:
        for process, stdout, stderr in collectors:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
            stdout.close(); stderr.close()
        e1a.kubectl(accesses[0], ["delete", "namespace", namespace, "--wait=true", "--timeout=300s"],
                     timeout=330, check=False)
        for access in accesses:
            e1a.cleanRemoteControl(access, controlPath)


def archiveDeploymentState(runDir: Path, pointDir: Path) -> None:
    target = pointDir / "deployment"
    target.mkdir(parents=True, exist_ok=True)
    for relative in ("placement_actual_summary.json", "verify_after_start_bird_summary.json",
                     "verify_after_fib_write_summary.json", "cross_node_workload_summary.json",
                     "output/k8s.yaml", "output/placement_report.json"):
        source = runDir / relative
        if source.is_file():
            destination = target / relative.replace("/", "__")
            shutil.copy2(source, destination)


def redeploy(runDir: Path, env: dict[str, str], summary: dict[str, Any], summaryPath: Path,
             mappingId: str, pointDir: Path) -> None:
    writeMappedManifest(runDir, mappingId)
    preloadMappingImages(runDir, mappingId, pointDir / "preload")
    scripts = [
        ("clean", "clean.sh"), ("deploy", "deploy.sh"), ("wait-ready", "wait-ready.sh"),
    ]
    for label, script in scripts:
        runLogged(f"e1b-{mappingId}-{label}", [str(B62_DIR / script), str(runDir)], summary,
                  summaryPath, runDir, env, pointDir / f"{label}.log")
    verifyLivePlacement(runDir, env, pointDir / "placement_actual_summary.json")
    for label, script in (("start-bird", "start_bird.sh"),
                          ("verify-start-bird", "verifyStartBird.sh"),
                          ("start-kernel", "start_bird_kernel.sh"),
                          ("verify-fib", "verifyFibRoutes.sh")):
        runLogged(f"e1b-{mappingId}-{label}", [str(B62_DIR / script), str(runDir)], summary,
                  summaryPath, runDir, env, pointDir / f"{label}.log")


def pointIsComplete(pointDir: Path, mappingId: str) -> bool:
    path = pointDir / "point_summary.json"
    if not path.is_file():
        return False
    try:
        item = json.loads(path.read_text())
        return (
            item.get("mappingId") == mappingId
            and item.get("placement", {}).get("status") == "PASS"
            and item.get("preflight", {}).get("status") == "success"
            and item.get("udp", {}).get("status") == "success"
            and item.get("tcp", {}).get("status") == "success"
            and item.get("workload", {}).get("status") == "success"
        )
    except (OSError, ValueError, TypeError):
        return False


def runPilot(summary: dict[str, Any], summaryPath: Path, runDir: Path, env: dict[str, str],
             resume: bool = False) -> None:
    config = shared.loadYaml(runDir / "assignment.yaml")
    mappingIds = list(config["e1b"]["pilot"]["mappingIds"])
    results = []
    for index, mappingId in enumerate(mappingIds):
        pointDir = runDir / "e1b/pilot" / mappingId
        pointDir.mkdir(parents=True, exist_ok=True)
        if resume and pointIsComplete(pointDir, mappingId):
            results.append(json.loads((pointDir / "point_summary.json").read_text()))
            continue
        if index or resume:
            redeploy(runDir, env, summary, summaryPath, mappingId, pointDir)
        placement = verifyLivePlacement(runDir, env, pointDir / "placement_actual_summary.json")
        runDataplane("preflight", runDir, env, pointDir / "preflight", duration=5)
        workload = measurePointWorkload(runDir, env, mappingById(runDir, mappingId), pointDir)
        archiveDeploymentState(runDir, pointDir)
        result = {"mappingId": mappingId, "placement": placement,
                  "preflight": json.loads((pointDir / "preflight/summary.json").read_text()),
                  "udp": json.loads((pointDir / "udp/summary.json").read_text()),
                  "tcp": json.loads((pointDir / "tcp/summary.json").read_text()),
                  "workload": workload}
        writeJson(pointDir / "point_summary.json", result)
        results.append(result)
    allPreflighted = all(item["preflight"]["status"] == "success" for item in results)
    allMeasurementsValid = all(
        item["placement"]["status"] == "PASS"
        and item["udp"]["status"] == "success"
        and item["tcp"]["status"] == "success"
        and item["workload"]["status"] == "success"
        for item in results
    )
    pilot = {"status": "PASS" if allPreflighted and allMeasurementsValid else "FAIL",
             "mappingIds": mappingIds, "pointCount": len(results),
             "allFlowsPreflighted": allPreflighted,
             "allMeasurementsValid": allMeasurementsValid, "results": results}
    writeJson(runDir / "e1b/pilot_summary.json", pilot)
    summary.setdefault("tests", {})["e1bPilot"] = {
        "status": pilot["status"], "pointCount": pilot["pointCount"],
        "allFlowsPreflighted": pilot["allFlowsPreflighted"],
        "artifact": str(runDir / "e1b/pilot_summary.json")}
    shared.writeJson(summaryPath, summary)
    if pilot["status"] != "PASS":
        raise RuntimeError("E1-B pilot validity checks failed; formal execution is not allowed")


def runOnReusedCluster(args: argparse.Namespace, runDir: Path) -> int:
    """Run a corrected pilot in a fresh artifact directory on an existing cluster."""
    sourceRun = args.reuse_cluster_from.resolve()
    if runDir.exists() and any(runDir.iterdir()):
        raise FileExistsError(f"refusing to overwrite non-empty reused-cluster run: {runDir}")
    for name in ("configK3s.yaml", "kubeconfig.yaml", "inventory.yaml", "cluster-input.yaml",
                 "assignment.yaml", "summary.json"):
        if not (sourceRun / name).is_file():
            raise FileNotFoundError(f"reuse source lacks {name}: {sourceRun}")
    if not (sourceRun / "output/k8s.source.yaml").is_file():
        raise FileNotFoundError(f"reuse source lacks compiled output: {sourceRun / 'output/k8s.source.yaml'}")

    runDir.mkdir(parents=True, exist_ok=True)
    prepareArtifacts(runDir, args.config.resolve(), args.plan_dir.resolve())
    shutil.copytree(sourceRun / "output", runDir / "output")
    for name in ("configK3s.yaml", "kubeconfig.yaml", "inventory.yaml", "cluster-input.yaml"):
        shutil.copy2(sourceRun / name, runDir / name)
    assignment = shared.loadYaml(sourceRun / "assignment.yaml")
    rawConfig = shared.loadYaml(args.config.resolve())
    assignment["e1b"] = copy.deepcopy(rawConfig["e1b"])
    assignment["runtime"] = {
        "clusterInput": str(runDir / "cluster-input.yaml"),
        "configK3s": str(runDir / "configK3s.yaml"),
        "kubeconfig": str(runDir / "kubeconfig.yaml"),
        "inventory": str(runDir / "inventory.yaml"),
    }
    (runDir / "assignment.yaml").write_text(yaml.safe_dump(assignment, sort_keys=False))
    (runDir / "topology_size").write_text("214\n")
    summaryPath = runDir / "summary.json"
    summary: dict[str, Any] = {
        "status": "RUNNING", "startedAt": datetime.now(timezone.utc).isoformat(),
        "runDirectory": str(runDir), "stages": [], "tests": {},
        "clusterReuse": {"sourceRun": str(sourceRun), "clusterRebuilt": False,
                         "compiledTopologyReused": True},
    }
    writeJson(summaryPath, summary)
    env = shared.createRuntimeEnv(
        runDir / "assignment.yaml", assignment, runDir,
        cluster_input=runDir / "cluster-input.yaml", config_k3s=runDir / "configK3s.yaml",
        kubeconfig=runDir / "kubeconfig.yaml", inventory=runDir / "inventory.yaml")
    mappingId = str(rawConfig["e1b"]["pilot"]["mappingIds"][0])
    writeMappedManifest(runDir, mappingId)
    e1a.pinExperimentVcpus({**rawConfig, "e1a": {"pinning": rawConfig["e1b"]["pinning"]}},
                           runDir / "e1b")
    summary["e1b"] = {"phase": "corrected-pilot", "currentMapping": mappingId,
                      "plan": str(runDir / "e1b/plans/placement_sequence.json")}
    writeJson(summaryPath, summary)
    try:
        for label, script in (("clean", "clean.sh"), ("preflight", "preflight.sh"),
                              ("build", "build.sh"), ("deploy", "deploy.sh"),
                              ("wait-ready", "wait-ready.sh")):
            runLogged(f"reuse-{label}", [str(B62_DIR / script), str(runDir)], summary,
                      summaryPath, runDir, env, runDir / f"{label}.orchestrator.log")
        afterReady(summary, summaryPath, runDir, env)
        for label, script in (("start-bird", "start_bird.sh"),
                              ("verify-start-bird", "verifyStartBird.sh"),
                              ("start-kernel", "start_bird_kernel.sh"),
                              ("verify-fib", "verifyFibRoutes.sh")):
            runLogged(f"reuse-{label}", [str(B62_DIR / script), str(runDir)], summary,
                      summaryPath, runDir, env, runDir / f"{label}.orchestrator.log")
        runPilot(summary, summaryPath, runDir, env)
        summary["status"] = "PASS"
        summary["finishedAt"] = datetime.now(timezone.utc).isoformat()
        writeJson(summaryPath, summary)
        return 0
    except BaseException as error:
        summary["status"] = "FAIL"
        summary["finishedAt"] = datetime.now(timezone.utc).isoformat()
        summary["failure"] = {"type": type(error).__name__, "message": str(error)}
        writeJson(summaryPath, summary)
        raise


def resumePilot(runDir: Path) -> int:
    """Continue an interrupted pilot without rebuilding its live cluster."""
    for name in ("assignment.yaml", "configK3s.yaml", "kubeconfig.yaml", "inventory.yaml",
                 "cluster-input.yaml", "summary.json", "output/k8s.source.yaml"):
        if not (runDir / name).is_file():
            raise FileNotFoundError(f"resume run lacks {name}: {runDir / name}")
    assignment = shared.loadYaml(runDir / "assignment.yaml")
    summaryPath = runDir / "summary.json"
    summary = json.loads(summaryPath.read_text())
    previousFailure = summary.pop("failure", None)
    summary["status"] = "RUNNING"
    summary.setdefault("resumptions", []).append({
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "previousFailure": previousFailure,
    })
    writeJson(summaryPath, summary)
    env = shared.createRuntimeEnv(
        runDir / "assignment.yaml", assignment, runDir,
        cluster_input=runDir / "cluster-input.yaml", config_k3s=runDir / "configK3s.yaml",
        kubeconfig=runDir / "kubeconfig.yaml", inventory=runDir / "inventory.yaml")
    try:
        runPilot(summary, summaryPath, runDir, env, resume=True)
        summary["status"] = "PASS"
        summary["finishedAt"] = datetime.now(timezone.utc).isoformat()
        writeJson(summaryPath, summary)
        return 0
    except BaseException as error:
        summary["status"] = "FAIL"
        summary["finishedAt"] = datetime.now(timezone.utc).isoformat()
        summary["failure"] = {"type": type(error).__name__, "message": str(error)}
        writeJson(summaryPath, summary)
        raise


def parseArgs(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--plan-dir", type=Path, default=DEFAULT_PLAN_DIR)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--reuse-cluster-from", type=Path,
                        help="reuse this run's live cluster and compiled topology in a fresh run directory")
    parser.add_argument("--resume-run", type=Path,
                        help="continue missing pilot points in this existing run and live cluster")
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args(argv)
    if args.reuse_cluster_from and args.run_dir is None:
        parser.error("--reuse-cluster-from requires --run-dir")
    if args.reuse_cluster_from and args.prepare_only:
        parser.error("--reuse-cluster-from cannot be combined with --prepare-only")
    if args.resume_run and (args.reuse_cluster_from or args.run_dir or args.prepare_only):
        parser.error("--resume-run cannot be combined with --reuse-cluster-from, --run-dir, or --prepare-only")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parseArgs(argv)
    if args.resume_run:
        return resumePilot(args.resume_run.resolve())
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    runDir = (args.run_dir or HERE / f"experiments/e1_marginal_pods/{timestamp}_e1b_pilot").resolve()
    if args.reuse_cluster_from:
        return runOnReusedCluster(args, runDir)
    prepareArtifacts(runDir, args.config.resolve(), args.plan_dir.resolve())
    sharedArgs = ["--config", str(args.config.resolve()), "--run-dir", str(runDir),
                  "--config-k3s", str(runDir / "configK3s.yaml"),
                  "--kubeconfig", str(runDir / "kubeconfig.yaml"),
                  "--inventory", str(runDir / "inventory.yaml"), "--skip-existing-destroy"]
    if args.prepare_only:
        sharedArgs.append("--prepare-only")
    return shared.main(sharedArgs, after_compile=applyInitialMapping, after_ready=afterReady,
                       after_workload=None if args.prepare_only else runPilot)


if __name__ == "__main__":
    raise SystemExit(main())
