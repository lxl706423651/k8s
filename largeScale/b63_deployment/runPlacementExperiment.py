#!/usr/bin/env python3
"""Run the b63 Kubernetes placement experiment.

Inputs:
  - output/k8s.raw.yaml or another Kubernetes manifest from running/compile.sh.
  - configkvm_b63.yaml, configK3s YAML, kubeconfig, or inventory files for
    node names and heterogeneous CPU/memory capacities.

Outputs:
  - output/k8s.yaml as the immutable source manifest when absent.
  - output/network_weights.yaml with per-network type and weight.
  - a deploy manifest with nodeName or nodeSelector placement injected.
  - output/placement_report.json with static and optional dynamic metrics.

Side effects:
  - Without --deploy, the script only writes files under the b63 directory.
  - With --deploy, it may create local KVM VMs, build a K3s/Kube-OVN cluster,
    build/push images, apply the workload, and sample node/overlay metrics.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import signal
import shutil
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SCRIPT_DIR / "output"
SOURCE_MANIFEST = OUTPUT_DIR / "k8s.yaml"
DEPLOY_MANIFEST = OUTPUT_DIR / "k8s.kube-ovn.yaml"
NETWORK_WEIGHTS = OUTPUT_DIR / "network_weights.yaml"
PLACEMENT_REPORT = OUTPUT_DIR / "placement_report.json"
NETWORK_PREAPPLY_MANIFEST = OUTPUT_DIR / "k8s.network-preapply.yaml"
DEFAULT_KVM_CONFIG = SCRIPT_DIR / "configkvm_b63.yaml"
DEFAULT_CONFIG_K3S = SCRIPT_DIR / "configK3s-b63.yaml"
DEFAULT_KUBECONFIG = SCRIPT_DIR / "kubeconfig-b63.yaml"
DEFAULT_INVENTORY = SCRIPT_DIR / "inventory-b63.yaml"

SEED_NETWORKS_ANNOTATION = "k8s.v1.cni.cncf.io/networks"
SEED_PREFIX = "org.seedsecuritylabs.seedemu.meta."


@dataclass
class NetworkInfo:
    """Network hyperedge derived from a NetworkAttachmentDefinition."""

    name: str
    logicalName: str
    seedType: str
    scope: str
    cidr: str
    kind: str = "host"
    weight: float = 0.0
    endpoints: list[str] = field(default_factory=list)


@dataclass
class PodInfo:
    """One schedulable workload and its estimated resource demand."""

    name: str
    namespace: str
    kind: str
    docIndex: int
    replicas: int
    asn: str
    role: str
    networks: list[str]
    cpuRequest: float | None
    memoryRequestMb: float | None
    cpu: float = 0.0
    memoryMb: float = 0.0
    q: float = 0.0

    @property
    def nicCount(self) -> int:
        return len(self.networks) * max(1, self.replicas)

    @property
    def podCount(self) -> int:
        return max(1, self.replicas)

    @property
    def cpuTotal(self) -> float:
        return self.cpu * self.podCount

    @property
    def memoryTotalMb(self) -> float:
        return self.memoryMb * self.podCount


@dataclass
class NodeInfo:
    """One Kubernetes node with resource capacity."""

    name: str
    role: str
    cpu: float
    memoryMb: float
    ip: str = ""
    sshUser: str = ""
    sshKey: str = ""
    connection: str = "ssh"
    schedulable: bool = True


class DynamicSampler:
    """Sample node CPU, memory, and overlay interface counters over SSH."""

    def __init__(self, nodes: list[NodeInfo], interval: float) -> None:
        self.nodes = nodes
        self.interval = interval
        self.samples: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._previous: dict[str, dict[str, Any]] = {}

    def startSampler(self) -> None:
        """Start the background sampler thread."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._sampleLoop, daemon=True)
        self._thread.start()

    def stopSampler(self) -> None:
        """Stop the background sampler thread."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(5.0, self.interval * 2))

    def _sampleLoop(self) -> None:
        while not self._stop.is_set():
            self.collectOnce()
            self._stop.wait(self.interval)

    def collectOnce(self) -> None:
        """Collect one sample from every node."""
        timestamp = time.time()
        node_samples: dict[str, Any] = {}
        for node in self.nodes:
            raw = self._readNodeCounters(node)
            if not raw:
                continue
            previous = self._previous.get(node.name)
            computed = self._computeRates(raw, previous)
            self._previous[node.name] = raw
            node_samples[node.name] = computed
        self.samples.append({"timestamp": timestamp, "nodes": node_samples})

    def summarizeSamples(self) -> dict[str, Any]:
        """Return aggregate dynamic validation metrics."""
        cpu_by_node: dict[str, list[float]] = {node.name: [] for node in self.nodes}
        mem_by_node: dict[str, list[float]] = {node.name: [] for node in self.nodes}
        traffic_rates: list[float] = []
        interfaces: dict[str, list[str]] = {}
        for sample in self.samples:
            aggregate_rate = 0.0
            for name, item in sample.get("nodes", {}).items():
                if item.get("cpuPct") is not None:
                    cpu_by_node.setdefault(name, []).append(float(item["cpuPct"]))
                if item.get("memoryPct") is not None:
                    mem_by_node.setdefault(name, []).append(float(item["memoryPct"]))
                aggregate_rate += float(item.get("trafficBytesPerSecond") or 0.0)
                if item.get("interfaces"):
                    interfaces[name] = sorted(set(interfaces.get(name, []) + item["interfaces"]))
            if sample.get("nodes"):
                traffic_rates.append(aggregate_rate)
        return {
            "sampleCount": len(self.samples),
            "intervalSeconds": self.interval,
            "nodeUtilization": {
                name: {
                    "cpuAveragePct": average(values),
                    "cpuPeakPct": max(values) if values else 0.0,
                    "memoryAveragePct": average(mem_by_node.get(name, [])),
                    "memoryPeakPct": max(mem_by_node.get(name, [])) if mem_by_node.get(name) else 0.0,
                }
                for name, values in cpu_by_node.items()
            },
            "traffic": {
                "aggregateBytesPerSecondAverage": average(traffic_rates),
                "aggregateBytesPerSecondPeak": max(traffic_rates) if traffic_rates else 0.0,
                "aggregateBytesPerSecondP95": percentile(traffic_rates, 95.0),
                "interfacesObserved": interfaces,
            },
            "samples": self.samples,
            "errors": self.errors,
        }

    def _readNodeCounters(self, node: NodeInfo) -> dict[str, Any] | None:
        script = r"""
set -eu
read cpu user nice system idle iowait irq softirq steal guest guest_nice < /proc/stat
total=$((user + nice + system + idle + iowait + irq + softirq + steal))
idle_all=$((idle + iowait))
mem_total=$(awk '/MemTotal/ {print $2}' /proc/meminfo)
mem_avail=$(awk '/MemAvailable/ {print $2}' /proc/meminfo)
echo "cpu_total=${total}"
echo "cpu_idle=${idle_all}"
echo "mem_total_kb=${mem_total}"
echo "mem_available_kb=${mem_avail}"
seen=0
for dev in genev_sys_6081 vxlan_sys_4789 ovn0 flannel.1; do
    if [ -e "/sys/class/net/${dev}/statistics/rx_bytes" ]; then
        rx=$(cat "/sys/class/net/${dev}/statistics/rx_bytes")
        tx=$(cat "/sys/class/net/${dev}/statistics/tx_bytes")
        echo "iface=${dev},$((rx + tx))"
        seen=1
    fi
done
if [ "${seen}" = "0" ]; then
    echo "iface=none,0"
fi
"""
        try:
            completed = runNodeCommand(node, script, timeout=15)
        except Exception as exc:  # noqa: BLE001 - keep collection best-effort.
            self.errors.append(f"{node.name}: {exc}")
            return None
        values: dict[str, Any] = {"interfaces": {}, "timestamp": time.time()}
        for raw in completed.stdout.splitlines():
            if raw.startswith("iface="):
                name, value = raw.split("=", 1)[1].split(",", 1)
                values["interfaces"][name] = int(value)
            elif "=" in raw:
                key, value = raw.split("=", 1)
                values[key] = int(value)
        return values

    def _computeRates(self, raw: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
        mem_total = float(raw.get("mem_total_kb") or 0)
        mem_avail = float(raw.get("mem_available_kb") or 0)
        memory_pct = 100.0 * (mem_total - mem_avail) / mem_total if mem_total else 0.0
        cpu_pct = None
        traffic_bps = 0.0
        if previous:
            total_delta = float(raw["cpu_total"] - previous["cpu_total"])
            idle_delta = float(raw["cpu_idle"] - previous["cpu_idle"])
            if total_delta > 0:
                cpu_pct = max(0.0, min(100.0, 100.0 * (1.0 - idle_delta / total_delta)))
            dt = max(0.001, float(raw["timestamp"] - previous["timestamp"]))
            interfaces = raw.get("interfaces", {})
            old_interfaces = previous.get("interfaces", {})
            for name, value in interfaces.items():
                old = int(old_interfaces.get(name, value))
                traffic_bps += max(0, int(value) - old) / dt
        return {
            "cpuPct": cpu_pct,
            "memoryPct": memory_pct,
            "trafficBytesPerSecond": traffic_bps,
            "interfaces": [name for name in raw.get("interfaces", {}) if name != "none"],
        }


def parseArgs() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--input-manifest", type=Path, default=None)
    parser.add_argument("--source-manifest", type=Path, default=SOURCE_MANIFEST)
    parser.add_argument("--deploy-manifest", type=Path, default=DEPLOY_MANIFEST)
    parser.add_argument("--network-weights", type=Path, default=NETWORK_WEIGHTS)
    parser.add_argument("--report", type=Path, default=PLACEMENT_REPORT)
    parser.add_argument("--kvm-config", type=Path, default=DEFAULT_KVM_CONFIG)
    parser.add_argument("--config-k3s", type=Path, default=DEFAULT_CONFIG_K3S)
    parser.add_argument("--kubeconfig", type=Path, default=DEFAULT_KUBECONFIG)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--compile", action="store_true", help="legacy option; use running/compile.sh instead")
    parser.add_argument("--deploy", action="store_true", help="run k8sTools build/up and dynamic collection")
    parser.add_argument("--reuse-cluster", action="store_true", help="reuse existing config-k3s/kubeconfig files")
    parser.add_argument("--include-master", action="store_true", help="allow workload placement on the master node")
    parser.add_argument("--network-cost-mode", choices=("ratio", "pair", "endpoint-exposure"), default="ratio")
    parser.add_argument(
        "--algorithm",
        default="optimized",
        choices=(
            "optimized",
            "network-only",
            "pod-count",
            "pod-count-balanced",
            "resource-only",
            "hypergraph",
            "kubernetes-default-scheduler",
        ),
    )
    parser.add_argument("--alpha", type=float, default=1.0)
    parser.add_argument("--beta", type=float, default=1.0)
    parser.add_argument("--improvement-passes", type=int, default=3)
    parser.add_argument("--fast-greedy-score", action="store_true")
    parser.add_argument("--ix-weight", type=float, default=10.0)
    parser.add_argument("--peer-weight", type=float, default=5.0)
    parser.add_argument("--intra-weight", type=float, default=2.0)
    parser.add_argument("--host-weight", type=float, default=0.5)
    parser.add_argument("--c0", type=float, default=0.05)
    parser.add_argument("--theta-c", type=float, default=0.015)
    parser.add_argument("--m0-mb", type=float, default=192.0)
    parser.add_argument("--sample-interval-seconds", type=float, default=15.0)
    parser.add_argument("--post-deploy-seconds", type=float, default=600.0)
    parser.add_argument(
        "--deploy-command-timeout-seconds",
        type=float,
        default=1800.0,
        help="maximum time to wait for k8sTools.py up before recording a dynamic failure",
    )
    parser.add_argument(
        "--network-preapply-timeout-seconds",
        type=float,
        default=180.0,
        help="maximum time to wait for Kube-OVN network resources before creating Pods",
    )
    parser.add_argument("--skip-resource-request-injection", action="store_true")
    parser.add_argument(
        "--pinning-mode",
        choices=("node-name", "node-selector"),
        default="node-name",
        help="how to pin selected non-default placements into workload manifests",
    )
    parser.add_argument("--keep-temp", action="store_true")
    return parser.parse_args()


def main() -> int:
    """Run static placement and optional deployment validation."""
    args = parseArgs()
    commands: list[dict[str, Any]] = []
    output_dir = resolvePath(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.compile:
        raise SystemExit("--compile is no longer part of b63 placement; run running/compile.sh first")

    input_manifest = resolveInputManifest(args.input_manifest, args.source_manifest, args.deploy_manifest)
    source_manifest = resolvePath(args.source_manifest)
    deploy_manifest = resolvePath(args.deploy_manifest)
    if input_manifest.resolve() != source_manifest.resolve():
        source_manifest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(input_manifest, source_manifest)

    docs = loadYamlDocs(source_manifest)
    networks = parseNetworks(docs)
    pods = parsePods(docs, networks)
    assignNetworkWeights(networks, pods, args)
    estimatePodResources(pods, networks, args)
    writeNetworkWeights(resolvePath(args.network_weights), networks)

    nodes = loadNodeInfo(args)
    eligible_nodes = [node for node in nodes if node.schedulable or args.include_master]
    if not eligible_nodes:
        raise SystemExit("No eligible worker nodes found for placement")

    placements = generatePlacements(pods, eligible_nodes, networks, args)
    selected_name = normalizeAlgorithmName(args.algorithm)
    selected_placement = placements[selected_name]
    static_validation = {
        name: evaluatePlacement(pods, nodes, networks, placement, args.network_cost_mode, args.alpha, args.beta)
        for name, placement in placements.items()
    }
    existing = readExistingPlacement(pods, docs)
    if existing:
        placements["manifest-existing"] = existing
        static_validation["manifest-existing"] = evaluatePlacement(
            pods, nodes, networks, existing, args.network_cost_mode, args.alpha, args.beta
        )

    injectPlacement(
        docs,
        pods,
        selected_placement,
        inject_resources=not args.skip_resource_request_injection,
        pinning_mode="none" if selected_name == "kubernetes-default-scheduler" else args.pinning_mode,
    )
    deploy_manifest.parent.mkdir(parents=True, exist_ok=True)
    writeYamlDocs(deploy_manifest, docs)

    report: dict[str, Any] = {
        "metadata": {
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "sourceManifest": str(source_manifest),
            "deployManifest": str(deploy_manifest),
            "selectedAlgorithm": selected_name,
            "networkCostMode": args.network_cost_mode,
            "alpha": args.alpha,
            "beta": args.beta,
        },
        "nodes": [node.__dict__ for node in nodes],
        "networks": {name: network.__dict__ for name, network in sorted(networks.items())},
        "pods": [pod.__dict__ | {"nicCount": pod.nicCount} for pod in pods],
        "placements": placements,
        "staticValidation": static_validation,
        "dynamicValidation": None,
        "commands": commands,
    }
    writeJson(resolvePath(args.report), report)

    if args.deploy:
        dynamic = runDeploymentValidation(args, nodes, commands)
        report["dynamicValidation"] = dynamic
        report["commands"] = commands
        writeJson(resolvePath(args.report), report)

    print(f"Wrote {deploy_manifest}")
    print(f"Wrote {resolvePath(args.network_weights)}")
    print(f"Wrote {resolvePath(args.report)}")
    return 0


def resolvePath(path: Path) -> Path:
    """Resolve a path relative to the b63 example directory."""
    expanded = path.expanduser()
    if expanded.is_absolute():
        return expanded.resolve()
    return (SCRIPT_DIR / expanded).resolve()


def loadYamlDocs(path: Path) -> list[dict[str, Any]]:
    """Load YAML documents from a manifest path."""
    with path.open("r", encoding="utf-8") as handle:
        return [doc for doc in yaml.safe_load_all(handle) if isinstance(doc, dict)]


def writeYamlDocs(path: Path, docs: list[dict[str, Any]]) -> None:
    """Write Kubernetes YAML documents."""
    path.write_text(yaml.safe_dump_all(docs, sort_keys=False), encoding="utf-8")


def writeJson(path: Path, payload: dict[str, Any]) -> None:
    """Write formatted JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def renderNetworkPreapplyManifest(deploy_manifest: Path, output_dir: Path) -> tuple[Path, list[str]]:
    """Write a manifest containing only namespace and Kube-OVN network resources."""
    priority = {
        "Namespace": 0,
        "Vpc": 1,
        "VPC": 1,
        "Subnet": 2,
        "NetworkAttachmentDefinition": 3,
    }
    selected = [
        doc
        for doc in loadYamlDocs(deploy_manifest)
        if str(doc.get("kind") or "") in priority
    ]
    selected.sort(
        key=lambda doc: (
            priority.get(str(doc.get("kind") or ""), 100),
            str((doc.get("metadata") or {}).get("name") or ""),
        )
    )
    preapply_manifest = output_dir / NETWORK_PREAPPLY_MANIFEST.name
    if selected:
        writeYamlDocs(preapply_manifest, selected)
    subnet_names = [
        str((doc.get("metadata") or {}).get("name"))
        for doc in selected
        if str(doc.get("kind") or "") == "Subnet" and (doc.get("metadata") or {}).get("name")
    ]
    return preapply_manifest, subnet_names


def resolveInputManifest(input_manifest: Path | None, source_manifest: Path, deploy_manifest: Path) -> Path:
    """Return the manifest that should be treated as the unplaced source."""
    if input_manifest:
        candidate = resolvePath(input_manifest)
        if not candidate.exists():
            raise SystemExit(f"Input manifest not found: {candidate}")
        return candidate
    source = resolvePath(source_manifest)
    if source.exists():
        return source
    deploy = resolvePath(deploy_manifest)
    if deploy.exists():
        return deploy
    raise SystemExit(f"No manifest found: expected {source} or {deploy}")


def parseNetworks(docs: list[dict[str, Any]]) -> dict[str, NetworkInfo]:
    """Parse all NetworkAttachmentDefinitions into NetworkInfo objects."""
    networks: dict[str, NetworkInfo] = {}
    for doc in docs:
        if doc.get("kind") != "NetworkAttachmentDefinition":
            continue
        metadata = doc.get("metadata") or {}
        annotations = metadata.get("annotations") or {}
        name = str(metadata.get("name") or "")
        if not name:
            continue
        logical = str(annotations.get(SEED_PREFIX + "name") or name)
        networks[name] = NetworkInfo(
            name=name,
            logicalName=logical,
            seedType=str(annotations.get(SEED_PREFIX + "type") or ""),
            scope=str(annotations.get(SEED_PREFIX + "scope") or ""),
            cidr=str(annotations.get(SEED_PREFIX + "prefix") or ""),
        )
    return networks


def parsePods(docs: list[dict[str, Any]], networks: dict[str, NetworkInfo]) -> list[PodInfo]:
    """Parse Pod and Deployment workloads and their attached networks."""
    default_namespace = findNamespace(docs)
    pods: list[PodInfo] = []
    for index, doc in enumerate(docs):
        pod_spec = getPodSpec(doc)
        if pod_spec is None:
            continue
        metadata = doc.get("metadata") or {}
        template_metadata = getPodMetadata(doc)
        annotations = template_metadata.get("annotations") or {}
        namespace = str(metadata.get("namespace") or default_namespace)
        name = str(metadata.get("name") or f"workload-{index}")
        asn = str(annotations.get(SEED_PREFIX + "asn") or "")
        role = str(annotations.get(SEED_PREFIX + "role") or "unknown")
        replicas = int((doc.get("spec") or {}).get("replicas") or 1) if doc.get("kind") != "Pod" else 1
        attached = parseAttachedNetworks(annotations, asn, networks)
        cpu_request, memory_request = parseResourceRequests(pod_spec)
        pod = PodInfo(
            name=name,
            namespace=namespace,
            kind=str(doc.get("kind") or ""),
            docIndex=index,
            replicas=replicas,
            asn=asn,
            role=role,
            networks=attached,
            cpuRequest=cpu_request,
            memoryRequestMb=memory_request,
        )
        pods.append(pod)
        for network_name in attached:
            if network_name in networks:
                networks[network_name].endpoints.append(name)
    return pods


def findNamespace(docs: list[dict[str, Any]]) -> str:
    """Return the namespace used by the manifest."""
    for doc in docs:
        if doc.get("kind") == "Namespace":
            name = (doc.get("metadata") or {}).get("name")
            if name:
                return str(name)
    for doc in docs:
        namespace = (doc.get("metadata") or {}).get("namespace")
        if namespace:
            return str(namespace)
    return "default"


def getPodSpec(doc: dict[str, Any]) -> dict[str, Any] | None:
    """Return a PodSpec for Pods and workload controllers."""
    kind = str(doc.get("kind") or "")
    if kind == "Pod":
        return doc.get("spec") if isinstance(doc.get("spec"), dict) else None
    if kind in {"Deployment", "ReplicaSet", "StatefulSet", "DaemonSet", "Job"}:
        template = (doc.get("spec") or {}).get("template") or {}
        spec = template.get("spec")
        return spec if isinstance(spec, dict) else None
    return None


def getPodMetadata(doc: dict[str, Any]) -> dict[str, Any]:
    """Return Pod template metadata for Pods and workload controllers."""
    if doc.get("kind") == "Pod":
        return doc.setdefault("metadata", {})
    template = doc.setdefault("spec", {}).setdefault("template", {})
    return template.setdefault("metadata", {})


def parseAttachedNetworks(annotations: dict[str, Any], asn: str, networks: dict[str, NetworkInfo]) -> list[str]:
    """Parse Multus network-selection annotations into NAD names."""
    raw = annotations.get(SEED_NETWORKS_ANNOTATION)
    attached: list[str] = []
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = [item.strip() for item in raw.split(",") if item.strip()]
        if isinstance(parsed, list):
            for item in parsed:
                name = parseNetworkSelectionItem(item)
                if name:
                    attached.append(name)
    if attached:
        return dedupe(attached)

    logical_to_nad = buildLogicalNetworkMap(networks)
    index = 0
    while True:
        logical = annotations.get(f"{SEED_PREFIX}net.{index}.name")
        if logical is None:
            break
        attached_name = logical_to_nad.get((asn, str(logical))) or logical_to_nad.get(("", str(logical)))
        if attached_name:
            attached.append(attached_name)
        index += 1
    return dedupe(attached)


def parseNetworkSelectionItem(item: Any) -> str:
    """Return a NAD name from one Multus network selection item."""
    if isinstance(item, dict):
        raw = str(item.get("name") or "")
    else:
        raw = str(item or "")
    if "/" in raw:
        raw = raw.rsplit("/", 1)[-1]
    return raw.strip()


def buildLogicalNetworkMap(networks: dict[str, NetworkInfo]) -> dict[tuple[str, str], str]:
    """Map SeedEMU logical network names to NAD names."""
    mapping: dict[tuple[str, str], str] = {}
    for name, network in networks.items():
        mapping[("", network.logicalName)] = name
        if network.scope and network.scope != "ix":
            mapping[(network.scope, network.logicalName)] = name
    return mapping


def parseResourceRequests(pod_spec: dict[str, Any]) -> tuple[float | None, float | None]:
    """Parse summed CPU and memory requests from Pod containers."""
    cpu_total = 0.0
    memory_total = 0.0
    cpu_seen = False
    memory_seen = False
    for container in pod_spec.get("containers") or []:
        requests = ((container.get("resources") or {}).get("requests") or {}) if isinstance(container, dict) else {}
        if "cpu" in requests:
            cpu_total += parseCpu(str(requests["cpu"]))
            cpu_seen = True
        if "memory" in requests:
            memory_total += parseMemoryMb(str(requests["memory"]))
            memory_seen = True
    return (cpu_total if cpu_seen else None, memory_total if memory_seen else None)


def parseCpu(value: str) -> float:
    """Parse Kubernetes CPU quantity to cores."""
    value = value.strip()
    if value.endswith("m"):
        return float(value[:-1]) / 1000.0
    if value.endswith("u"):
        return float(value[:-1]) / 1_000_000.0
    if value.endswith("n"):
        return float(value[:-1]) / 1_000_000_000.0
    return float(value)


def parseMemoryMb(value: str) -> float:
    """Parse Kubernetes memory quantity to MiB."""
    value = value.strip()
    units = {
        "Ki": 1.0 / 1024.0,
        "Mi": 1.0,
        "Gi": 1024.0,
        "Ti": 1024.0 * 1024.0,
        "K": 1000.0 / 1024.0 / 1024.0,
        "M": 1000.0 * 1000.0 / 1024.0 / 1024.0,
        "G": 1000.0 * 1000.0 * 1000.0 / 1024.0 / 1024.0,
    }
    for suffix, multiplier in units.items():
        if value.endswith(suffix):
            return float(value[: -len(suffix)]) * multiplier
    return float(value) / 1024.0 / 1024.0


def formatCpu(value: float) -> str:
    """Format cores as a Kubernetes millicore request."""
    return f"{max(1, int(math.ceil(value * 1000.0)))}m"


def formatMemory(value_mb: float) -> str:
    """Format MiB as a Kubernetes memory request."""
    return f"{max(1, int(math.ceil(value_mb)))}Mi"


def assignNetworkWeights(networks: dict[str, NetworkInfo], pods: list[PodInfo], args: argparse.Namespace) -> None:
    """Infer network types and assign network-level weights."""
    role_by_pod = {pod.name: pod.role for pod in pods}
    asn_by_pod = {pod.name: pod.asn for pod in pods}
    weights = {"IX": args.ix_weight, "peer": args.peer_weight, "intra-AS": args.intra_weight, "host": args.host_weight}
    for network in networks.values():
        endpoint_roles = {role_by_pod.get(pod, "") for pod in network.endpoints}
        endpoint_asns = {asn_by_pod.get(pod, "") for pod in network.endpoints if asn_by_pod.get(pod, "")}
        if network.seedType == "global" or network.scope == "ix" or network.logicalName.startswith("ix"):
            kind = "IX"
        elif len(endpoint_asns) > 1:
            kind = "peer"
        elif network.logicalName == "net0" or "Host" in endpoint_roles:
            kind = "host"
        else:
            kind = "intra-AS"
        network.kind = kind
        network.weight = float(weights[kind])


def writeNetworkWeights(path: Path, networks: dict[str, NetworkInfo]) -> None:
    """Write network_weights.yaml."""
    payload = {
        "networks": {
            name: {"type": network.kind, "weight": float(network.weight)}
            for name, network in sorted(networks.items())
        }
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def estimatePodResources(pods: list[PodInfo], networks: dict[str, NetworkInfo], args: argparse.Namespace) -> None:
    """Estimate CPU and memory for every workload."""
    role_memory = {
        "route server": 384.0,
        "route-server": 384.0,
        "borderrouter": 320.0,
        "border router": 320.0,
        "router": 256.0,
        "host": 128.0,
    }
    for pod in pods:
        pod.q = sum(networks[name].weight for name in pod.networks if name in networks)
        pod.cpu = pod.cpuRequest if pod.cpuRequest is not None else args.c0 + args.theta_c * pod.q
        role_key = pod.role.lower().replace("_", " ")
        pod.memoryMb = pod.memoryRequestMb if pod.memoryRequestMb is not None else role_memory.get(role_key, args.m0_mb)


def loadNodeInfo(args: argparse.Namespace) -> list[NodeInfo]:
    """Load heterogeneous node capacities from inventory, configK3s, or KVM config."""
    inventory = resolvePath(args.inventory)
    if inventory.exists():
        nodes = loadNodesFromInventory(inventory)
        if nodes:
            nodes = mergeNodeCapacities(nodes, loadCapacityFallbackNodes(args))
            if any(node.cpu <= 0 or node.memoryMb <= 0 for node in nodes):
                missing = ", ".join(node.name for node in nodes if node.cpu <= 0 or node.memoryMb <= 0)
                raise SystemExit(f"Inventory is missing CPU/memory capacity for: {missing}")
            return markSchedulable(nodes)

    config_k3s = resolvePath(args.config_k3s)
    if config_k3s.exists():
        nodes = loadNodesFromConfigK3s(config_k3s)
        if nodes and all(node.cpu > 0 and node.memoryMb > 0 for node in nodes):
            return markSchedulable(nodes)

    kvm_config = resolvePath(args.kvm_config)
    if kvm_config.exists():
        nodes = loadNodesFromKvmConfig(kvm_config)
        if nodes:
            return markSchedulable(nodes)

    if config_k3s.exists():
        nodes = loadNodesFromConfigK3s(config_k3s)
        if nodes:
            return markSchedulable(nodes)
    raise SystemExit("Cannot load node information from inventory, configK3s, or KVM config")


def loadCapacityFallbackNodes(args: argparse.Namespace) -> list[NodeInfo]:
    """Load node capacity sources used to enrich inventory records."""
    result: list[NodeInfo] = []
    config_k3s = resolvePath(args.config_k3s)
    if config_k3s.exists():
        result.extend(loadNodesFromConfigK3s(config_k3s))
    kvm_config = resolvePath(args.kvm_config)
    if kvm_config.exists():
        result.extend(loadNodesFromKvmConfig(kvm_config))
    return result


def mergeNodeCapacities(primary: list[NodeInfo], fallback: list[NodeInfo]) -> list[NodeInfo]:
    """Fill missing primary node CPU/memory fields from matching fallback nodes."""
    fallback_by_name = {node.name: node for node in fallback if node.name}
    for node in primary:
        capacity = fallback_by_name.get(node.name)
        if capacity is None:
            continue
        if node.cpu <= 0:
            node.cpu = capacity.cpu
        if node.memoryMb <= 0:
            node.memoryMb = capacity.memoryMb
        if not node.role and capacity.role:
            node.role = capacity.role
    return primary


def markSchedulable(nodes: list[NodeInfo]) -> list[NodeInfo]:
    """Mark masters as unschedulable for workload placement by default."""
    for node in nodes:
        node.schedulable = node.role.lower() not in {"master", "control-plane", "server"}
    return nodes


def loadYamlMapping(path: Path) -> dict[str, Any]:
    """Load one YAML mapping."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"Invalid YAML mapping: {path}")
    return data


def loadNodesFromInventory(path: Path) -> list[NodeInfo]:
    """Read generated inventory YAML with resources."""
    data = loadYamlMapping(path)
    default_ssh = data.get("ssh") or {}
    default_ssh_user = str(default_ssh.get("user") or "")
    default_ssh_key = (
        str(Path(str(default_ssh.get("default_key_path") or default_ssh.get("key") or "")).expanduser())
        if default_ssh.get("default_key_path") or default_ssh.get("key")
        else ""
    )
    nodes: list[NodeInfo] = []
    for item in data.get("nodes") or []:
        resources = item.get("resources") or {}
        ssh = item.get("ssh") or {}
        nodes.append(
            NodeInfo(
                name=str(item.get("name") or ""),
                role=str(item.get("role") or ""),
                cpu=float(resources.get("vcpus") or resources.get("cpu") or 0),
                memoryMb=float(resources.get("memory_mb") or resources.get("memoryMb") or 0),
                ip=str(item.get("managementIp") or item.get("management_ip") or item.get("ip") or ""),
                sshUser=str(ssh.get("user") or default_ssh_user),
                sshKey=str(Path(str(ssh.get("key") or default_ssh_key)).expanduser()) if ssh.get("key") or default_ssh_key else "",
                connection=str(item.get("connection") or "ssh"),
            )
        )
    if nodes:
        return nodes
    all_hosts = ((data.get("all") or {}).get("children") or {})
    for role_name, role_payload in all_hosts.items():
        for name, item in ((role_payload or {}).get("hosts") or {}).items():
            resources = item.get("resources") or {}
            nodes.append(
                NodeInfo(
                    name=str(name),
                    role=str(item.get("k3s_role") or role_name),
                    cpu=float(resources.get("vcpus") or resources.get("cpu") or 0),
                    memoryMb=float(resources.get("memory_mb") or resources.get("memoryMb") or 0),
                    ip=str(item.get("ansible_host") or ""),
                    sshUser=str(item.get("ansible_user") or default_ssh_user),
                    sshKey=str(Path(str(item.get("ansible_ssh_private_key_file") or default_ssh_key)).expanduser())
                    if item.get("ansible_ssh_private_key_file") or default_ssh_key
                    else "",
                    connection=str(item.get("connection") or "ssh"),
                )
            )
    return nodes


def loadNodesFromConfigK3s(path: Path) -> list[NodeInfo]:
    """Read node names and optional capacity metadata from configK3s.yaml."""
    data = loadYamlMapping(path)
    resource_by_name: dict[str, dict[str, Any]] = {}
    for item in (((data.get("k8sTools") or {}).get("destroy") or {}).get("state") or {}).get("nodes") or []:
        resource_by_name[str(item.get("name") or "")] = item
    nodes: list[NodeInfo] = []
    for item in data.get("nodes") or []:
        name = str(item.get("name") or "")
        resources = item.get("resources") or resource_by_name.get(name, {})
        ssh = item.get("ssh") or {}
        nodes.append(
            NodeInfo(
                name=name,
                role=str(item.get("role") or ""),
                cpu=float(resources.get("vcpus") or resources.get("cpu") or 0),
                memoryMb=float(resources.get("memoryMb") or resources.get("memory_mb") or 0),
                ip=str(item.get("ip") or item.get("managementIp") or item.get("management_ip") or ""),
                sshUser=str(ssh.get("user") or ""),
                sshKey=str(Path(str(ssh.get("key") or "")).expanduser()) if ssh.get("key") else "",
                connection=str(item.get("connection") or "ssh"),
            )
        )
    return nodes


def loadNodesFromKvmConfig(path: Path) -> list[NodeInfo]:
    """Read explicit nodes or master/workers resources from KVM input YAML."""
    data = loadYamlMapping(path)
    nodes: list[NodeInfo] = []
    default_ssh = data.get("ssh") or {}
    if isinstance(data.get("nodes"), list):
        for item in data["nodes"]:
            nodes.append(
                NodeInfo(
                    name=str(item.get("name") or ""),
                    role=str(item.get("role") or ""),
                    cpu=float(item.get("vcpus") or 0),
                    memoryMb=float(item.get("memory_mb") or item.get("memoryMb") or 0),
                    ip=str(item.get("ip") or ""),
                    sshUser=str(default_ssh.get("user") or ""),
                    sshKey=str(Path(str(default_ssh.get("key") or "")).expanduser()) if default_ssh.get("key") else "",
                )
            )
        return nodes
    defaults = data.get("defaults") or {}
    master = data.get("master") or {}
    if master:
        nodes.append(
            NodeInfo(
                name=str(defaults.get("masterName") or defaults.get("master_name") or "seed-k3s-master"),
                role="master",
                cpu=float(master.get("vcpus") or 0),
                memoryMb=float(master.get("memoryMb") or master.get("memory_mb") or 0),
                sshUser=str(default_ssh.get("user") or ""),
                sshKey=str(Path(str(default_ssh.get("key") or "")).expanduser()) if default_ssh.get("key") else "",
            )
        )
    workers = data.get("workers") or {}
    prefix = str(defaults.get("workerNamePrefix") or defaults.get("worker_name_prefix") or "seed-k3s-worker")
    for index in range(1, int(workers.get("count") or 0) + 1):
        nodes.append(
            NodeInfo(
                name=f"{prefix}{index}",
                role="worker",
                cpu=float(workers.get("vcpus") or 0),
                memoryMb=float(workers.get("memoryMb") or workers.get("memory_mb") or 0),
                sshUser=str(default_ssh.get("user") or ""),
                sshKey=str(Path(str(default_ssh.get("key") or "")).expanduser()) if default_ssh.get("key") else "",
            )
        )
    return nodes


def generatePlacements(
    pods: list[PodInfo],
    nodes: list[NodeInfo],
    networks: dict[str, NetworkInfo],
    args: argparse.Namespace,
) -> dict[str, dict[str, str]]:
    """Generate the optimized placement and all baselines."""
    return {
        "kubernetes-default-scheduler": generateDefaultSchedulerPlacement(pods, nodes),
        "network-only": generateGreedyPlacement(
            pods, nodes, networks, args.network_cost_mode, 1.0, 0.0, args.improvement_passes, args.fast_greedy_score
        ),
        "pod-count-balanced": generatePodCountPlacement(pods, nodes),
        "resource-only": generateGreedyPlacement(
            pods, nodes, networks, args.network_cost_mode, 0.0, 1.0, args.improvement_passes, args.fast_greedy_score
        ),
        "hypergraph": generateHypergraphPlacement(pods, nodes, networks),
        "optimized": generateGreedyPlacement(
            pods,
            nodes,
            networks,
            args.network_cost_mode,
            args.alpha,
            args.beta,
            args.improvement_passes,
            args.fast_greedy_score,
        ),
    }


def normalizeAlgorithmName(value: str) -> str:
    """Map CLI algorithm names to report keys."""
    return {"pod-count": "pod-count-balanced"}.get(value, value)


def sortedPodsForPlacement(pods: list[PodInfo]) -> list[PodInfo]:
    """Return workloads ordered by scheduling importance."""
    return sorted(pods, key=lambda pod: (pod.q, pod.cpuTotal + pod.memoryTotalMb / 1024.0, pod.name), reverse=True)


def generateGreedyPlacement(
    pods: list[PodInfo],
    nodes: list[NodeInfo],
    networks: dict[str, NetworkInfo],
    network_mode: str,
    alpha: float,
    beta: float,
    improvement_passes: int,
    fast_score: bool,
) -> dict[str, str]:
    """Greedily minimize the configured network/resource objective."""
    if fast_score:
        return generateFastGreedyPlacement(pods, nodes, networks, network_mode, alpha, beta, improvement_passes)
    placement: dict[str, str] = {}
    for pod in sortedPodsForPlacement(pods):
        best_node = min(
            nodes,
            key=lambda node: scoreCandidate(pods, nodes, networks, placement, pod, node, network_mode, alpha, beta),
        )
        placement[pod.name] = best_node.name
    return improvePlacement(pods, nodes, networks, placement, network_mode, alpha, beta, improvement_passes)


def generateFastGreedyPlacement(
    pods: list[PodInfo],
    nodes: list[NodeInfo],
    networks: dict[str, NetworkInfo],
    network_mode: str,
    alpha: float,
    beta: float,
    improvement_passes: int,
) -> dict[str, str]:
    """Greedy placement with incremental candidate scoring for large manifests."""
    placement: dict[str, str] = {}
    load = {node.name: {"cpu": 0.0, "memory": 0.0, "pods": 0} for node in nodes}
    network_counts: dict[str, dict[str, int]] = {name: {} for name in networks}
    total_network_weight = max(1e-9, sum(network.weight for network in networks.values() if len(network.endpoints) > 1))
    for pod in sortedPodsForPlacement(pods):
        best_node = min(
            nodes,
            key=lambda node: fastCandidateScore(
                pod,
                node,
                nodes,
                networks,
                load,
                network_counts,
                network_mode,
                alpha,
                beta,
                total_network_weight,
            ),
        )
        placement[pod.name] = best_node.name
        load[best_node.name]["cpu"] += pod.cpuTotal
        load[best_node.name]["memory"] += pod.memoryTotalMb
        load[best_node.name]["pods"] += pod.podCount
        for network_name in pod.networks:
            if network_name in network_counts:
                counts = network_counts[network_name]
                counts[best_node.name] = counts.get(best_node.name, 0) + pod.podCount
    return improvePlacement(pods, nodes, networks, placement, network_mode, alpha, beta, improvement_passes)


def fastCandidateScore(
    pod: PodInfo,
    node: NodeInfo,
    nodes: list[NodeInfo],
    networks: dict[str, NetworkInfo],
    load: dict[str, dict[str, float]],
    network_counts: dict[str, dict[str, int]],
    network_mode: str,
    alpha: float,
    beta: float,
    total_network_weight: float,
) -> float:
    """Score a candidate by only recomputing affected network edges and node loads."""
    resource_score, overload = fastResourceScore(pod, node, nodes, load)
    network_score = 0.0
    for network_name in pod.networks:
        network = networks.get(network_name)
        if network is None:
            continue
        counts = dict(network_counts.get(network_name) or {})
        counts[node.name] = counts.get(node.name, 0) + pod.podCount
        network_score += network.weight * networkCostForCounts(counts, network_mode)
    network_score /= total_network_weight
    pod_count = load[node.name]["pods"] + pod.podCount
    return alpha * network_score + beta * resource_score + overload * 1000.0 + pod_count * 0.0001


def fastResourceScore(
    pod: PodInfo,
    candidate_node: NodeInfo,
    nodes: list[NodeInfo],
    load: dict[str, dict[str, float]],
) -> tuple[float, float]:
    """Return resource score and overload after placing pod on candidate_node."""
    cpu_utils: list[float] = []
    mem_utils: list[float] = []
    dominant: list[float] = []
    overload = 0.0
    for node in nodes:
        cpu = load[node.name]["cpu"]
        memory = load[node.name]["memory"]
        if node.name == candidate_node.name:
            cpu += pod.cpuTotal
            memory += pod.memoryTotalMb
        cpu_util = cpu / max(node.cpu, 1e-9)
        mem_util = memory / max(node.memoryMb, 1e-9)
        dom = max(cpu_util, mem_util)
        cpu_utils.append(cpu_util)
        mem_utils.append(mem_util)
        dominant.append(dom)
        overload += max(0.0, dom - 1.0)
    skew = (coefficientOfVariation(cpu_utils) + coefficientOfVariation(mem_utils)) / 2.0
    return 0.7 * (max(dominant) if dominant else 0.0) + 0.3 * skew, overload


def networkCostForCounts(counts: dict[str, int], network_mode: str) -> float:
    """Return one unweighted network cut cost from endpoint counts per node."""
    values = [count for count in counts.values() if count > 0]
    total = sum(values)
    if total <= 1:
        return 0.0
    if network_mode == "ratio":
        return (len(values) - 1) / max(1, total - 1)
    if network_mode == "pair":
        total_pairs = total * (total - 1) / 2.0
        same_pairs = sum(value * (value - 1) / 2.0 for value in values)
        return (total_pairs - same_pairs) / max(1.0, total_pairs)
    return (total - max(values)) / total


def scoreCandidate(
    pods: list[PodInfo],
    nodes: list[NodeInfo],
    networks: dict[str, NetworkInfo],
    placement: dict[str, str],
    pod: PodInfo,
    node: NodeInfo,
    network_mode: str,
    alpha: float,
    beta: float,
) -> float:
    """Score one candidate node for the next greedy assignment."""
    candidate = dict(placement)
    candidate[pod.name] = node.name
    metrics = evaluatePlacement(pods, nodes, networks, candidate, network_mode, alpha, beta)
    overload = sum(max(0.0, item["dominantUtilization"] - 1.0) for item in metrics["perNode"].values())
    pod_count = metrics["perNode"][node.name]["podCount"]
    return float(metrics["objective"]) + overload * 1000.0 + pod_count * 0.0001


def improvePlacement(
    pods: list[PodInfo],
    nodes: list[NodeInfo],
    networks: dict[str, NetworkInfo],
    placement: dict[str, str],
    network_mode: str,
    alpha: float,
    beta: float,
    improvement_passes: int,
) -> dict[str, str]:
    """Run a small deterministic local search over single-pod moves."""
    best = dict(placement)
    if improvement_passes <= 0:
        return best
    best_score = float(evaluatePlacement(pods, nodes, networks, best, network_mode, alpha, beta)["objective"])
    for _ in range(improvement_passes):
        changed = False
        for pod in sortedPodsForPlacement(pods):
            current = best[pod.name]
            for node in nodes:
                if node.name == current:
                    continue
                candidate = dict(best)
                candidate[pod.name] = node.name
                metrics = evaluatePlacement(pods, nodes, networks, candidate, network_mode, alpha, beta)
                overload = sum(max(0.0, item["dominantUtilization"] - 1.0) for item in metrics["perNode"].values())
                score = float(metrics["objective"]) + overload * 1000.0
                if score + 1e-9 < best_score:
                    best = candidate
                    best_score = score
                    changed = True
            if changed:
                break
        if not changed:
            break
    return best


def generateDefaultSchedulerPlacement(pods: list[PodInfo], nodes: list[NodeInfo]) -> dict[str, str]:
    """Simulate a simple Kubernetes least-allocated scheduler baseline."""
    placement: dict[str, str] = {}
    load = {node.name: {"cpu": 0.0, "memory": 0.0, "pods": 0} for node in nodes}
    node_by_name = {node.name: node for node in nodes}
    for pod in pods:
        best = min(
            nodes,
            key=lambda node: (
                load[node.name]["cpu"] / max(node.cpu, 1e-9)
                + load[node.name]["memory"] / max(node.memoryMb, 1e-9)
                + load[node.name]["pods"] / 1000.0,
                node.name,
            ),
        )
        placement[pod.name] = best.name
        load[best.name]["cpu"] += pod.cpuTotal
        load[best.name]["memory"] += pod.memoryTotalMb
        load[best.name]["pods"] += pod.podCount
        node_by_name[best.name] = best
    return placement


def generatePodCountPlacement(pods: list[PodInfo], nodes: list[NodeInfo]) -> dict[str, str]:
    """Balance workloads by pod count only."""
    placement: dict[str, str] = {}
    counts = {node.name: 0 for node in nodes}
    for pod in pods:
        node = min(nodes, key=lambda item: (counts[item.name], item.name))
        placement[pod.name] = node.name
        counts[node.name] += pod.podCount
    return placement


def generateHypergraphPlacement(pods: list[PodInfo], nodes: list[NodeInfo], networks: dict[str, NetworkInfo]) -> dict[str, str]:
    """Generic hypergraph-style baseline using edge affinity and balance."""
    placement: dict[str, str] = {}
    counts = {node.name: 0 for node in nodes}
    network_members: dict[str, set[str]] = {}
    for pod in pods:
        for network_name in pod.networks:
            network_members.setdefault(network_name, set()).add(pod.name)
    for pod in sortedPodsForPlacement(pods):
        def score(node: NodeInfo) -> tuple[float, int, str]:
            affinity = 0.0
            for network_name in pod.networks:
                weight = networks.get(network_name, NetworkInfo(network_name, network_name, "", "", "")).weight
                for other in network_members.get(network_name, set()):
                    if placement.get(other) == node.name:
                        affinity += weight
            return (-affinity, counts[node.name], node.name)

        node = min(nodes, key=score)
        placement[pod.name] = node.name
        counts[node.name] += pod.podCount
    return placement


def evaluatePlacement(
    pods: list[PodInfo],
    nodes: list[NodeInfo],
    networks: dict[str, NetworkInfo],
    placement: dict[str, str],
    network_mode: str,
    alpha: float,
    beta: float,
) -> dict[str, Any]:
    """Compute static validation metrics for one placement."""
    per_node = {
        node.name: {
            "cpuRequested": 0.0,
            "memoryRequestedMb": 0.0,
            "podCount": 0,
            "nicCount": 0,
            "ovsSecondaryPortEstimate": 0,
            "ovsTotalPortEstimate": 0,
        }
        for node in nodes
    }
    node_by_name = {node.name: node for node in nodes}
    for pod in pods:
        node_name = placement.get(pod.name)
        if node_name not in per_node:
            continue
        per_node[node_name]["cpuRequested"] += pod.cpuTotal
        per_node[node_name]["memoryRequestedMb"] += pod.memoryTotalMb
        per_node[node_name]["podCount"] += pod.podCount
        per_node[node_name]["nicCount"] += pod.nicCount
    for node_name, item in per_node.items():
        node = node_by_name[node_name]
        cpu_util = item["cpuRequested"] / max(node.cpu, 1e-9)
        mem_util = item["memoryRequestedMb"] / max(node.memoryMb, 1e-9)
        item["cpuUtilization"] = cpu_util
        item["memoryUtilization"] = mem_util
        item["dominantUtilization"] = max(cpu_util, mem_util)
        item["ovsSecondaryPortEstimate"] = item["nicCount"]
        item["ovsTotalPortEstimate"] = item["nicCount"] + item["podCount"]
    network_costs = calculateNetworkCosts(pods, networks, placement)
    resource = calculateResourceCost(per_node)
    pressure = calculateNodePressure(pods, networks, placement, nodes)
    return {
        "objective": alpha * network_costs[network_mode] + beta * resource["score"],
        "networkCosts": network_costs,
        "resourceCost": resource,
        "perNode": per_node,
        "nodeCrossNetworkPressure": pressure,
        "utilizationSummary": summarizeUtilization(per_node),
    }


def calculateNetworkCosts(
    pods: list[PodInfo],
    networks: dict[str, NetworkInfo],
    placement: dict[str, str],
) -> dict[str, float]:
    """Compute ratio, pair, and endpoint-exposure network cut costs."""
    pod_by_name = {pod.name: pod for pod in pods}
    totals = {"ratio": 0.0, "pair": 0.0, "endpoint-exposure": 0.0}
    total_weight = 0.0
    for network in networks.values():
        endpoints = [name for name in network.endpoints if name in placement]
        if len(endpoints) <= 1:
            continue
        counts: dict[str, int] = {}
        total_endpoints = 0
        for pod_name in endpoints:
            count = pod_by_name[pod_name].podCount
            counts[placement[pod_name]] = counts.get(placement[pod_name], 0) + count
            total_endpoints += count
        if total_endpoints <= 1:
            continue
        node_count = len(counts)
        cross_pairs = 0
        total_pairs = total_endpoints * (total_endpoints - 1) / 2.0
        values = list(counts.values())
        for idx, left in enumerate(values):
            for right in values[idx + 1 :]:
                cross_pairs += left * right
        totals["ratio"] += network.weight * ((node_count - 1) / max(1, total_endpoints - 1))
        totals["pair"] += network.weight * (cross_pairs / max(1.0, total_pairs))
        totals["endpoint-exposure"] += network.weight * ((total_endpoints - max(values)) / total_endpoints)
        total_weight += network.weight
    if total_weight <= 0:
        return totals
    return {key: value / total_weight for key, value in totals.items()}


def calculateResourceCost(per_node: dict[str, dict[str, Any]]) -> dict[str, float]:
    """Compute dominant utilization and capacity-weighted skew."""
    dominant = [float(item["dominantUtilization"]) for item in per_node.values()]
    cpu = [float(item["cpuUtilization"]) for item in per_node.values()]
    memory = [float(item["memoryUtilization"]) for item in per_node.values()]
    skew = (coefficientOfVariation(cpu) + coefficientOfVariation(memory)) / 2.0
    max_dom = max(dominant) if dominant else 0.0
    avg_dom = average(dominant)
    return {
        "score": 0.7 * max_dom + 0.3 * skew,
        "dominantMax": max_dom,
        "dominantAverage": avg_dom,
        "capacityWeightedSkew": skew,
    }


def calculateNodePressure(
    pods: list[PodInfo],
    networks: dict[str, NetworkInfo],
    placement: dict[str, str],
    nodes: list[NodeInfo],
) -> dict[str, float]:
    """Estimate potential cross-node network pressure per node."""
    pod_by_name = {pod.name: pod for pod in pods}
    pressure = {node.name: 0.0 for node in nodes}
    for network in networks.values():
        counts: dict[str, int] = {}
        total = 0
        for pod_name in network.endpoints:
            node_name = placement.get(pod_name)
            if node_name is None:
                continue
            count = pod_by_name[pod_name].podCount
            counts[node_name] = counts.get(node_name, 0) + count
            total += count
        for node_name, count in counts.items():
            pressure[node_name] += network.weight * count * max(0, total - count)
    return pressure


def summarizeUtilization(per_node: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Summarize CPU, memory, and dominant utilization."""
    cpu = [float(item["cpuUtilization"]) for item in per_node.values()]
    memory = [float(item["memoryUtilization"]) for item in per_node.values()]
    dominant = [float(item["dominantUtilization"]) for item in per_node.values()]
    return {
        "cpu": metricSummary(cpu),
        "memory": metricSummary(memory),
        "dominant": metricSummary(dominant),
    }


def metricSummary(values: list[float]) -> dict[str, float]:
    """Return max, average, and coefficient of variation."""
    return {"max": max(values) if values else 0.0, "average": average(values), "cv": coefficientOfVariation(values)}


def injectPlacement(
    docs: list[dict[str, Any]],
    pods: list[PodInfo],
    placement: dict[str, str],
    *,
    inject_resources: bool,
    pinning_mode: str = "node-name",
) -> None:
    """Inject node placement and optional resource requests into workload manifests."""
    pod_by_index = {pod.docIndex: pod for pod in pods}
    for index, doc in enumerate(docs):
        pod = pod_by_index.get(index)
        if pod is None:
            continue
        pod_spec = getPodSpec(doc)
        if pod_spec is None:
            continue
        assigned_node = placement[pod.name]
        if pinning_mode == "node-name":
            pod_spec["nodeName"] = placement[pod.name]
            removeHostnameNodeSelector(pod_spec)
        elif pinning_mode == "node-selector":
            pod_spec.pop("nodeName", None)
            selector = pod_spec.setdefault("nodeSelector", {})
            selector["kubernetes.io/hostname"] = assigned_node
        else:
            pod_spec.pop("nodeName", None)
            removeHostnameNodeSelector(pod_spec)
        metadata = getPodMetadata(doc)
        annotations = metadata.setdefault("annotations", {})
        annotations["seedemu.k8s.placement/assigned-node"] = (
            assigned_node if pinning_mode != "none" else "kubernetes-default-scheduler"
        )
        annotations["seedemu.k8s.placement/pinning-mode"] = pinning_mode
        annotations["seedemu.k8s.placement/cpu-estimate"] = f"{pod.cpu:.6f}"
        annotations["seedemu.k8s.placement/memory-mb-estimate"] = f"{pod.memoryMb:.3f}"
        if inject_resources:
            injectResourceRequests(pod_spec, pod)


def removeHostnameNodeSelector(pod_spec: dict[str, Any]) -> None:
    """Remove only the hostname selector used by generated hard placement."""
    selector = pod_spec.get("nodeSelector")
    if not isinstance(selector, dict):
        return
    selector.pop("kubernetes.io/hostname", None)
    if not selector:
        pod_spec.pop("nodeSelector", None)


def injectResourceRequests(pod_spec: dict[str, Any], pod: PodInfo) -> None:
    """Write resource requests when the manifest lacks them."""
    containers = pod_spec.get("containers") or []
    if not containers:
        return
    first = containers[0]
    resources = first.setdefault("resources", {})
    requests = resources.setdefault("requests", {})
    if pod.cpuRequest is None:
        requests["cpu"] = formatCpu(pod.cpu)
    if pod.memoryRequestMb is None:
        requests["memory"] = formatMemory(pod.memoryMb)


def readExistingPlacement(pods: list[PodInfo], docs: list[dict[str, Any]]) -> dict[str, str]:
    """Read nodeName or hostname nodeSelector placement already present in a manifest."""
    result: dict[str, str] = {}
    for pod in pods:
        spec = getPodSpec(docs[pod.docIndex])
        if spec and spec.get("nodeName"):
            result[pod.name] = str(spec["nodeName"])
            continue
        selector = spec.get("nodeSelector") if spec else None
        if isinstance(selector, dict) and selector.get("kubernetes.io/hostname"):
            result[pod.name] = str(selector["kubernetes.io/hostname"])
    return result if len(result) == len(pods) else {}


def preApplyNetworkResources(args: argparse.Namespace, kubeconfig: Path, commands: list[dict[str, Any]]) -> None:
    """Apply Kube-OVN network resources before workload Pods are created."""
    deploy_manifest = resolvePath(args.deploy_manifest)
    if not deploy_manifest.exists():
        return
    output_dir = resolvePath(args.output_dir)
    preapply_manifest, subnet_names = renderNetworkPreapplyManifest(deploy_manifest, output_dir)
    if not preapply_manifest.exists():
        return
    runLoggedCommand(
        ["kubectl", "--kubeconfig", str(kubeconfig), "apply", "-f", str(preapply_manifest)],
        cwd=SCRIPT_DIR,
        commands=commands,
        timeout=args.network_preapply_timeout_seconds,
    )
    waitForKubeOvnSubnetsReady(kubeconfig, subnet_names, args.network_preapply_timeout_seconds, commands)


def waitForKubeOvnSubnetsReady(
    kubeconfig: Path,
    subnet_names: list[str],
    timeout: float,
    commands: list[dict[str, Any]],
) -> None:
    """Wait until every pre-applied Kube-OVN Subnet reports Ready=True."""
    if not subnet_names:
        return
    started = time.time()
    record = {
        "command": f"wait for Kube-OVN subnets ready ({len(subnet_names)} subnets)",
        "cwd": str(SCRIPT_DIR),
        "startedAt": started,
        "timeoutSeconds": timeout,
    }
    deadline = started + timeout
    last_error = ""
    while time.time() < deadline:
        completed = subprocess.run(
            ["kubectl", "--kubeconfig", str(kubeconfig), "get", "subnet", *subnet_names, "-o", "json"],
            cwd=str(SCRIPT_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=min(20.0, max(5.0, timeout)),
        )
        if completed.returncode == 0:
            payload = json.loads(completed.stdout)
            items = payload.get("items") if isinstance(payload.get("items"), list) else [payload]
            ready = {str((item.get("metadata") or {}).get("name")): isSubnetReady(item) for item in items}
            if all(ready.get(name, False) for name in subnet_names):
                record["returnCode"] = 0
                record["finishedAt"] = time.time()
                commands.append(record)
                return
            last_error = f"not ready: {[name for name in subnet_names if not ready.get(name, False)]}"
        else:
            last_error = (completed.stderr or completed.stdout).strip()
        time.sleep(2.0)
    record["returnCode"] = None
    record["timedOut"] = True
    record["finishedAt"] = time.time()
    record["lastError"] = last_error
    commands.append(record)
    raise RuntimeError(f"timed out waiting for Kube-OVN subnets: {last_error}")


def isSubnetReady(item: dict[str, Any]) -> bool:
    """Return true when a Kube-OVN Subnet status has Ready=True."""
    for condition in (item.get("status") or {}).get("conditions") or []:
        if condition.get("type") == "Ready" and str(condition.get("status")).lower() == "true":
            return True
    return False


def runDeploymentValidation(args: argparse.Namespace, nodes: list[NodeInfo], commands: list[dict[str, Any]]) -> dict[str, Any]:
    """Build or reuse the cluster, deploy workload, and collect dynamic metrics."""
    config_k3s = resolvePath(args.config_k3s)
    kubeconfig = resolvePath(args.kubeconfig)
    kvm_config = resolvePath(args.kvm_config)
    inventory = resolvePath(args.inventory)
    keep_temp_args = ["--keep-temp"] if args.keep_temp else []
    if not args.reuse_cluster or not (config_k3s.exists() and kubeconfig.exists()):
        runLoggedCommand(
            [
                "python3",
                "./k8sTools.py",
                "build",
                "--input",
                str(kvm_config),
                "--config-k3s",
                str(config_k3s),
                "--kubeconfig",
                str(kubeconfig),
                "--inventory",
                str(inventory),
                *keep_temp_args,
            ],
            cwd=SCRIPT_DIR,
            commands=commands,
            timeout=args.deploy_command_timeout_seconds,
        )
    runtime_nodes = loadNodesFromConfigK3s(config_k3s)
    if not runtime_nodes:
        runtime_nodes = nodes
    deploy_error = None
    namespace = "default"
    deploy_manifest = resolvePath(args.deploy_manifest)
    if deploy_manifest.exists():
        namespace = findNamespace(loadYamlDocs(deploy_manifest))
    preApplyNetworkResources(args, kubeconfig, commands)
    sampler = DynamicSampler(runtime_nodes, args.sample_interval_seconds)
    sampler.startSampler()
    workload_after_deploy: dict[str, Any] = {}
    start = time.time()
    try:
        runLoggedCommand(
            [
                "python3",
                "./k8sTools.py",
                "up",
                "-f",
                str(resolvePath(args.output_dir)),
                "-k",
                str(kubeconfig),
                "-d",
                str(config_k3s),
                *keep_temp_args,
            ],
            cwd=SCRIPT_DIR,
            commands=commands,
            timeout=args.deploy_command_timeout_seconds,
        )
        workload_after_deploy = collectKubernetesState(kubeconfig, namespace)
        end_deploy = time.time()
        while time.time() - end_deploy < args.post_deploy_seconds:
            time.sleep(min(args.sample_interval_seconds, max(0.0, args.post_deploy_seconds - (time.time() - end_deploy))))
    except Exception as exc:  # noqa: BLE001 - report the failed experiment.
        deploy_error = str(exc)
    finally:
        sampler.stopSampler()
    summary = sampler.summarizeSamples()
    summary["deploymentStart"] = start
    summary["deploymentEnd"] = time.time()
    summary["deployError"] = deploy_error
    summary["postDeploySampleSeconds"] = args.post_deploy_seconds
    summary["workloadStateAfterDeploy"] = workload_after_deploy
    summary["workloadStateFinal"] = collectKubernetesState(kubeconfig, namespace)
    return summary


def collectKubernetesState(kubeconfig: Path, namespace: str) -> dict[str, Any]:
    """Collect actual Kubernetes node and pod placement state after deployment."""
    result: dict[str, Any] = {"namespace": namespace}
    try:
        pods_raw = subprocess.run(
            ["kubectl", "--kubeconfig", str(kubeconfig), "-n", namespace, "get", "pods", "-o", "json"],
            text=True,
            capture_output=True,
            check=True,
            timeout=60,
        ).stdout
        nodes_raw = subprocess.run(
            ["kubectl", "--kubeconfig", str(kubeconfig), "get", "nodes", "-o", "json"],
            text=True,
            capture_output=True,
            check=True,
            timeout=60,
        ).stdout
    except Exception as exc:  # noqa: BLE001 - keep dynamic reporting best-effort.
        result["error"] = str(exc)
        return result

    pods_payload = json.loads(pods_raw)
    nodes_payload = json.loads(nodes_raw)
    phase_counts: dict[str, int] = {}
    node_counts: dict[str, int] = {}
    pods: dict[str, Any] = {}
    ready_count = 0
    for item in pods_payload.get("items", []):
        name = str((item.get("metadata") or {}).get("name") or "")
        status = item.get("status") or {}
        spec = item.get("spec") or {}
        phase = str(status.get("phase") or "Unknown")
        node_name = str(spec.get("nodeName") or "")
        ready = any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in status.get("conditions") or []
        )
        phase_counts[phase] = phase_counts.get(phase, 0) + 1
        if node_name:
            node_counts[node_name] = node_counts.get(node_name, 0) + 1
        if ready:
            ready_count += 1
        pods[name] = {
            "nodeName": node_name,
            "phase": phase,
            "ready": ready,
            "podIP": str(status.get("podIP") or ""),
            "hostIP": str(status.get("hostIP") or ""),
        }

    node_ready_counts = {"Ready": 0, "NotReady": 0}
    for item in nodes_payload.get("items", []):
        ready = any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in (item.get("status") or {}).get("conditions") or []
        )
        node_ready_counts["Ready" if ready else "NotReady"] += 1

    result.update(
        {
            "podCount": len(pods),
            "readyPodCount": ready_count,
            "phaseCounts": phase_counts,
            "nodeCounts": node_counts,
            "nodeReadyCounts": node_ready_counts,
            "pods": pods,
        }
    )
    return result


def runNodeCommand(node: NodeInfo, script: str, timeout: int) -> subprocess.CompletedProcess[str]:
    """Run a shell snippet on one local or SSH node."""
    if node.connection == "local":
        return subprocess.run(["bash", "-s"], input=script, text=True, capture_output=True, check=True, timeout=timeout)
    if not node.ip or not node.sshUser or not node.sshKey:
        raise RuntimeError("missing ip/sshUser/sshKey")
    return subprocess.run(
        [
            "ssh",
            "-i",
            node.sshKey,
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "LogLevel=ERROR",
            "-o",
            "ConnectTimeout=10",
            f"{node.sshUser}@{node.ip}",
            "bash -s",
        ],
        input=script,
        text=True,
        capture_output=True,
        check=True,
        timeout=timeout,
    )


def runLoggedCommand(
    args: list[str],
    *,
    cwd: Path,
    commands: list[dict[str, Any]],
    timeout: float | None = None,
) -> None:
    """Run one command and append a concise command record."""
    started = time.time()
    record = {"command": " ".join(args), "cwd": str(cwd), "startedAt": started}
    process: subprocess.Popen[Any] | None = None
    try:
        process = subprocess.Popen(args, cwd=str(cwd), start_new_session=True)
        return_code = process.wait(timeout=timeout)
        if return_code != 0:
            raise subprocess.CalledProcessError(return_code, args)
        record["returnCode"] = 0
    except subprocess.CalledProcessError as exc:
        record["returnCode"] = exc.returncode
        commands.append(record)
        raise
    except subprocess.TimeoutExpired as exc:
        record["returnCode"] = None
        record["timedOut"] = True
        record["timeoutSeconds"] = timeout
        if process is not None:
            terminateProcessGroup(process)
        commands.append(record)
        raise RuntimeError(f"command timed out after {timeout} seconds: {' '.join(args)}") from exc
    finally:
        record["finishedAt"] = time.time()
        if record not in commands:
            commands.append(record)


def terminateProcessGroup(process: subprocess.Popen[Any]) -> None:
    """Terminate a timed-out child command and any subprocesses it started."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    process.wait(timeout=10)


def average(values: list[float]) -> float:
    """Return average or zero for an empty list."""
    return float(sum(values) / len(values)) if values else 0.0


def coefficientOfVariation(values: list[float]) -> float:
    """Return coefficient of variation for non-negative metrics."""
    if not values:
        return 0.0
    mean = average(values)
    if abs(mean) < 1e-12:
        return 0.0
    return float(statistics.pstdev(values) / mean)


def percentile(values: list[float], p: float) -> float:
    """Return percentile with nearest-rank interpolation."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil((p / 100.0) * len(ordered)) - 1))
    return float(ordered[index])


def dedupe(values: list[str]) -> list[str]:
    """Deduplicate while preserving order."""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        raise SystemExit(130)
