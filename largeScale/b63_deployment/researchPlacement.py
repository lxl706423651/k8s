#!/usr/bin/env python3
"""Research placements for communication/contention experiments.

The algorithms in this module deliberately avoid Pod-count and utilization-
variance objectives.  They use the configured kubelet Pod capacity and VLAN
trunk ranges only as admission constraints.  Crescent is an adaptation of the
randomized weighted component merging algorithm from NSDI'24; the resource-
aware variant uses CPU demand when mapping components to hosts.  CPU remains
oversubscribable because every emulation container must stay online.  The
performance model consumes frozen coefficients fitted by fitPerformanceModel.py.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable


@dataclass
class Demand:
    cpu: float
    memory: float
    pods: int

    def __add__(self, other: "Demand") -> "Demand":
        return Demand(self.cpu + other.cpu, self.memory + other.memory, self.pods + other.pods)


def stableUnit(seed: int, *parts: str) -> float:
    """Return a deterministic value in (0, 1) for weighted sampling."""
    raw = hashlib.sha256((str(seed) + "\0" + "\0".join(parts)).encode()).digest()
    return (int.from_bytes(raw[:8], "big") + 1) / (2**64 + 1)


def sparseGraphEdges(networks: dict[str, Any], seed: int) -> list[tuple[float, str, str]]:
    """Project point-to-point links exactly and multiaccess links as a ring.

    A normalized ring keeps the projection linear in topology size while
    retaining connectivity of each L2 hyperedge.  This adaptation is recorded
    in every placement report; it does not claim to be Crescent source code.
    """
    weights: dict[tuple[str, str], float] = defaultdict(float)
    for network in networks.values():
        endpoints = sorted(set(network.endpoints))
        if len(endpoints) < 2:
            continue
        pairs: list[tuple[str, str]]
        if len(endpoints) == 2:
            pairs = [(endpoints[0], endpoints[1])]
        else:
            offset = int(stableUnit(seed, network.name) * len(endpoints))
            rotated = endpoints[offset:] + endpoints[:offset]
            pairs = [(rotated[index], rotated[(index + 1) % len(rotated)])
                     for index in range(len(rotated))]
        per_edge = float(network.weight or 1.0) / len(pairs)
        for left, right in pairs:
            key = tuple(sorted((left, right)))
            weights[key] += per_edge
    edges = []
    for (left, right), weight in weights.items():
        # Exponential-race ordering implements weighted sampling without replacement.
        priority = -math.log(stableUnit(seed, left, right)) / max(weight, 1e-12)
        edges.append((priority, left, right))
    return sorted(edges)


def podDemands(pods: list[Any], nodes: list[Any], external_cpu_millicores: float,
               target_utilization: float) -> dict[str, Demand]:
    """Scale relative predeployment estimates to a declared cluster load level."""
    total_memory_weight = sum(max(float(p.memoryTotalMb), 1.0) for p in pods)
    total_base_cpu_weight = sum(max(float(p.cpuTotal), 1e-6) for p in pods)
    memory_budget = sum(float(n.memoryMb) for n in nodes) * target_utilization
    base_cpu_budget = sum(float(n.cpu) for n in nodes) * min(target_utilization, 0.35)
    external_cpu = max(0.0, external_cpu_millicores) / 1000.0
    return {
        pod.name: Demand(
            cpu=(max(float(pod.cpuTotal), 1e-6) / total_base_cpu_weight * base_cpu_budget
                 + external_cpu * pod.podCount),
            memory=max(float(pod.memoryTotalMb), 1.0) / total_memory_weight * memory_budget,
            pods=int(pod.podCount),
        )
        for pod in pods
    }


def trunkCapacities(kvm_config: Path) -> dict[str, int]:
    """Read physical VLAN ID capacity per configured trunk."""
    import yaml
    data = yaml.safe_load(kvm_config.read_text()) or {}
    result = {}
    for item in (data.get("fabric") or {}).get("vlanTrunks") or []:
        start = int(item.get("vlanStart", 1))
        end = int(item.get("vlanEnd", 4094))
        result[str(item.get("name") or "")] = end - start + 1
    return result


def admissionState(nodes: list[Any]) -> dict[str, dict[str, Any]]:
    return {node.name: {"pods": 0, "trunks": defaultdict(set)} for node in nodes}


def canAdmit(pod: Any, node: Any, state: dict[str, dict[str, Any]],
             networks: dict[str, Any], max_pods: int,
             trunk_capacity: dict[str, int]) -> bool:
    """Check kubelet Pod capacity and concrete VLAN IDs required on the node."""
    item = state[node.name]
    node_max_pods = int(getattr(node, "maxPods", 0) or max_pods)
    if item["pods"] + pod.podCount > node_max_pods:
        return False
    additions: dict[str, set[int]] = defaultdict(set)
    for network_name in pod.networks:
        network = networks.get(network_name)
        if network is None or network.vlanId is None or not network.vlanTrunk:
            continue
        additions[network.vlanTrunk].add(int(network.vlanId))
    for trunk, values in additions.items():
        if len(item["trunks"][trunk] | values) > trunk_capacity.get(trunk, 4094):
            return False
    return True


def admit(pod: Any, node: Any, state: dict[str, dict[str, Any]], networks: dict[str, Any]) -> None:
    item = state[node.name]
    item["pods"] += pod.podCount
    for network_name in pod.networks:
        network = networks.get(network_name)
        if network is not None and network.vlanId is not None and network.vlanTrunk:
            item["trunks"][network.vlanTrunk].add(int(network.vlanId))


def networkIncrement(pod: Any, node_name: str, placement: dict[str, str],
                     networks: dict[str, Any]) -> float:
    """Return weighted already-placed endpoints separated by this decision."""
    cost = 0.0
    for network_name in pod.networks:
        network = networks.get(network_name)
        if network is None:
            continue
        for peer in network.endpoints:
            peer_node = placement.get(peer)
            if peer_node is not None and peer_node != node_name:
                cost += float(network.weight or 1.0)
    return cost


def communicationNormalizer(networks: dict[str, Any]) -> float:
    """Return the maximum weighted endpoint-pair cost of the fixed topology."""
    return max(1.0, sum(
        float(network.weight or 1.0) * len(set(network.endpoints)) * (len(set(network.endpoints)) - 1) / 2
        for network in networks.values()
    ))


def contention(load: Demand, node: Any, knee: float = 0.7) -> float:
    cpu = load.cpu / max(float(node.cpu), 1e-9)
    memory = load.memory / max(float(node.memoryMb), 1e-9)
    return max(0.0, cpu - knee) ** 2 + max(0.0, memory - knee) ** 2


def greedyPlacement(pods: list[Any], nodes: list[Any], networks: dict[str, Any],
                    *, mode: str, demands: dict[str, Demand], max_pods: int,
                    trunk_capacity: dict[str, int], model: dict[str, Any] | None = None) -> dict[str, str]:
    """Generate locality-only, capacity-only, or fitted performance placement."""
    placement: dict[str, str] = {}
    state = admissionState(nodes)
    load = {node.name: Demand(0.0, 0.0, 0) for node in nodes}
    ordered = sorted(pods, key=lambda p: (float(p.q), p.name), reverse=True)
    coefficients = (model or {}).get("coefficients") or {}
    knee = float((model or {}).get("contentionKnee", 0.7))
    network_coefficient = float(coefficients.get("communicationCost", 1.0))
    contention_coefficient = float(coefficients.get("contentionCost", 1.0))
    communication_normalizer = communicationNormalizer(networks)
    for pod in ordered:
        choices = []
        for node in nodes:
            if not canAdmit(pod, node, state, networks, max_pods, trunk_capacity):
                continue
            before = contention(load[node.name], node, knee)
            after_load = load[node.name] + demands[pod.name]
            # Memory is a safety capacity, while CPU remains deliberately
            # oversubscribable so E1 can observe contention.
            if after_load.memory > float(node.memoryMb):
                continue
            after = contention(after_load, node, knee)
            communication = networkIncrement(pod, node.name, placement, networks) / communication_normalizer
            if mode == "locality-only":
                score = communication
            elif mode == "capacity-only":
                score = after
            else:
                score = network_coefficient * communication + contention_coefficient * (after - before)
            choices.append((score, node.name, node))
        if not choices:
            raise ValueError(f"no node satisfies kubelet/CNI admission for {pod.name}")
        node = min(choices)[-1]
        placement[pod.name] = node.name
        load[node.name] = load[node.name] + demands[pod.name]
        admit(pod, node, state, networks)
    return placement


class UnionFind:
    def __init__(self, names: Iterable[str], demands: dict[str, Demand]) -> None:
        self.parent = {name: name for name in names}
        self.members = {name: [name] for name in names}
        self.demand = dict(demands)

    def root(self, name: str) -> str:
        while self.parent[name] != name:
            self.parent[name] = self.parent[self.parent[name]]
            name = self.parent[name]
        return name

    def union(self, left: str, right: str) -> None:
        left, right = self.root(left), self.root(right)
        if left == right:
            return
        if (len(self.members[left]), left) < (len(self.members[right]), right):
            left, right = right, left
        self.parent[right] = left
        self.members[left].extend(self.members.pop(right))
        self.demand[left] = self.demand[left] + self.demand.pop(right)


def crescentPlacement(pods: list[Any], nodes: list[Any], networks: dict[str, Any],
                      *, resource_aware: bool, demands: dict[str, Demand], max_pods: int,
                      trunk_capacity: dict[str, int], seed: int) -> tuple[dict[str, str], dict[str, Any]]:
    """Adapt Crescent weighted component merging, then map components to hosts."""
    pods_by_name = {pod.name: pod for pod in pods}
    uf = UnionFind(pods_by_name, demands)
    max_memory = max(float(node.memoryMb) for node in nodes)
    max_cpu = max(float(node.cpu) for node in nodes)
    max_node_pods = max(int(getattr(node, "maxPods", 0) or max_pods) for node in nodes)
    for _, left, right in sparseGraphEdges(networks, seed):
        left_root, right_root = uf.root(left), uf.root(right)
        if left_root == right_root:
            continue
        combined = uf.demand[left_root] + uf.demand[right_root]
        if combined.memory > max_memory or combined.pods > max_node_pods:
            continue
        if resource_aware and combined.cpu > max_cpu:
            continue
        uf.union(left_root, right_root)

    components = sorted(uf.members.values(),
                        key=lambda names: (uf.demand[uf.root(names[0])].memory, names), reverse=True)
    placement: dict[str, str] = {}
    state = admissionState(nodes)
    load = {node.name: Demand(0.0, 0.0, 0) for node in nodes}
    split_components = 0

    def place_names(names: list[str]) -> bool:
        demand = Demand(0.0, 0.0, 0)
        for name in names:
            demand += demands[name]
        candidates = []
        for node in nodes:
            if load[node.name].memory + demand.memory > float(node.memoryMb):
                continue
            shadow = {key: {"pods": value["pods"], "trunks": defaultdict(set, {t: set(v) for t, v in value["trunks"].items()})}
                      for key, value in state.items()}
            feasible = True
            for name in names:
                pod = pods_by_name[name]
                if not canAdmit(pod, node, shadow, networks, max_pods, trunk_capacity):
                    feasible = False
                    break
                admit(pod, node, shadow, networks)
            if feasible:
                memory_fraction = ((load[node.name].memory + demand.memory)
                                   / max(float(node.memoryMb), 1e-9))
                if resource_aware:
                    # CPU is a placement signal, not a hard admission limit.
                    # The load sweep intentionally drives aggregate requested
                    # work beyond physical CPU to expose contention while all
                    # emulation containers remain online.
                    cpu_fraction = ((load[node.name].cpu + demand.cpu)
                                    / max(float(node.cpu), 1e-9))
                    dominant = max(memory_fraction, cpu_fraction)
                else:
                    dominant = memory_fraction
                candidates.append((dominant, state[node.name]["pods"], node.name, node))
        if not candidates:
            return False
        node = min(candidates)[-1]
        for name in names:
            pod = pods_by_name[name]
            placement[name] = node.name
            admit(pod, node, state, networks)
        load[node.name] += demand
        return True

    for component in components:
        if place_names(component):
            continue
        split_components += 1
        for name in sorted(component, key=lambda item: (demands[item].memory, item), reverse=True):
            if not place_names([name]):
                raise ValueError(f"Crescent component member cannot satisfy admission: {name}")
    return placement, {
        "adaptation": "weighted-exponential component merging with normalized ring projection for multiaccess networks",
        "componentCount": len(components),
        "splitComponentCount": split_components,
        "resourceAware": resource_aware,
        "cpuUsedAsPlacementSignal": resource_aware,
        "cpuHardAdmissionLimit": False,
        "seed": seed,
    }


def validateAdmission(placement: dict[str, str], pods: list[Any], nodes: list[Any],
                      networks: dict[str, Any], max_pods: int,
                      trunk_capacity: dict[str, int]) -> dict[str, Any]:
    state = admissionState(nodes)
    nodes_by_name = {node.name: node for node in nodes}
    failures = []
    for pod in sorted(pods, key=lambda item: item.name):
        node = nodes_by_name.get(placement.get(pod.name, ""))
        if node is None or not canAdmit(pod, node, state, networks, max_pods, trunk_capacity):
            failures.append(pod.name)
            continue
        admit(pod, node, state, networks)
    return {
        "status": "PASS" if not failures else "FAIL",
        "configuredKubeletMaxPods": max_pods,
        "kubeletMaxPodsByNode": {
            node.name: int(getattr(node, "kubeletMaxPods", 0)
                           or getattr(node, "maxPods", 0) or max_pods) for node in nodes
        },
        "existingNonTerminalPodsByNode": {
            node.name: int(getattr(node, "existingPods", 0)) for node in nodes
        },
        "workloadPodCapacityByNode": {
            node.name: int(getattr(node, "maxPods", 0) or max_pods) for node in nodes
        },
        "kubeletAdmissionSourceByNode": {
            node.name: str(getattr(node, "admissionSource", "configuration")) for node in nodes
        },
        "trunkVlanCapacities": trunk_capacity,
        "failedWorkloads": failures,
        "perNode": {
            name: {"pods": item["pods"], "vlansByTrunk": {key: len(value) for key, value in item["trunks"].items()}}
            for name, item in state.items()
        },
    }


def generate(algorithm: str, pods: list[Any], nodes: list[Any], networks: dict[str, Any],
             args: Any) -> tuple[dict[str, str], dict[str, Any]]:
    """Dispatch a research algorithm and return mapping plus method metadata."""
    max_pods = int(args.admission_max_pods)
    trunk_capacity = trunkCapacities(Path(args.kvm_config).expanduser().resolve())
    demands = podDemands(pods, nodes, args.external_cpu_millicores, args.resource_target_utilization)
    metadata: dict[str, Any] = {
        "externalCpuMillicoresPerPod": args.external_cpu_millicores,
        "resourceTargetUtilization": args.resource_target_utilization,
        "usesArtificialUniformPodCap": False,
    }
    if algorithm in {"locality-only", "capacity-only", "tradeoff-static"}:
        model = None
        mode = algorithm
        if algorithm == "tradeoff-static":
            mode = "performance-model"
            model = {
                "contentionKnee": float(args.contention_knee),
                "coefficients": {
                    "communicationCost": 1.0,
                    "contentionCost": float(args.static_contention_weight),
                },
            }
        placement = greedyPlacement(pods, nodes, networks, mode=mode, demands=demands,
                                    max_pods=max_pods, trunk_capacity=trunk_capacity, model=model)
        if algorithm == "tradeoff-static":
            metadata.update({
                "staticContentionWeight": float(args.static_contention_weight),
                "contentionKnee": float(args.contention_knee),
                "predeclaredSweepPoint": True,
            })
    elif algorithm in {"crescent", "crescent-resource-aware"}:
        placement, details = crescentPlacement(
            pods, nodes, networks, resource_aware=algorithm.endswith("resource-aware"),
            demands=demands, max_pods=max_pods, trunk_capacity=trunk_capacity,
            seed=int(args.placement_seed),
        )
        metadata.update(details)
    elif algorithm == "performance-model":
        path = Path(args.performance_model).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"performance model is required for performance-model placement: {path}")
        model = json.loads(path.read_text())
        placement = greedyPlacement(pods, nodes, networks, mode=algorithm, demands=demands,
                                    max_pods=max_pods, trunk_capacity=trunk_capacity, model=model)
        metadata.update({"modelPath": str(path), "modelTrainingDataHash": model.get("trainingDataHash")})
    else:
        raise ValueError(f"unsupported research algorithm: {algorithm}")
    admission = validateAdmission(placement, pods, nodes, networks, max_pods, trunk_capacity)
    if admission["status"] != "PASS":
        raise ValueError(f"placement violates kubelet/CNI admission: {admission['failedWorkloads'][:5]}")
    metadata["admission"] = admission
    return placement, metadata
