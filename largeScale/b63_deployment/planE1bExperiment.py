#!/usr/bin/env python3
"""Freeze the E1-B topology214 flow plan and Pod-to-Node mapping sequence.

The planner uses topology and manifest data only.  It never reads measured
throughput or latency when choosing a mapping.  Dynamic pilot results are used
later only to accept or reject the already selected flow directions.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import yaml


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_SOURCE = SCRIPT_DIR / "experiments/research_e1_topology214_20260918/capacity/output/k8s.source.yaml"
DEFAULT_REPORT = SCRIPT_DIR / "experiments/research_e1_topology214_20260918/capacity/output/placement_report.json"
DEFAULT_OLD_PINGS = SCRIPT_DIR / "experiments/research_e1_topology214_20260918/capacity/load_sweep/load_0m/dataplane/ping_results.json"
DEFAULT_OUTPUT = SCRIPT_DIR / "experiments/e1_marginal_pods/e1b_topology214_frozen_20260919/plans"
DEFAULT_TOPOLOGY = Path("/data/lxl/seed-emulator/topology/real_topology_214.txt")
DEFAULT_ASSIGNMENT = Path("/data/lxl/seed-emulator/topology/assignment.pkl")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stableKey(seed: int, *parts: str) -> str:
    payload = "\0".join([str(seed), *map(str, parts)]).encode()
    return hashlib.sha256(payload).hexdigest()


def writeJson(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def loadMeasureModule() -> Any:
    path = SCRIPT_DIR / "running/measureDataplane.py"
    spec = importlib.util.spec_from_file_location("e1b_measure_dataplane", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def manifestEndpoints(docs: list[dict[str, Any]], measure: Any) -> dict[str, dict[str, Any]]:
    endpoints: dict[str, dict[str, Any]] = {}
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        metadata = (((doc.get("spec") or {}).get("template") or {}).get("metadata") or {})
        labels = metadata.get("labels") or {}
        app = str(labels.get("app") or "")
        match = measure.APP_RE.match(app)
        if labels.get("seedemu.io/role") != "brd" or match is None:
            continue
        annotations = metadata.get("annotations") or {}
        rawNetworks = annotations.get("k8s.v1.cni.cncf.io/networks") or "[]"
        networkIps: dict[str, str] = {}
        try:
            attachments = json.loads(rawNetworks) if isinstance(rawNetworks, str) else rawNetworks
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid Multus network annotation for {app}: {error}") from error
        for attachment in attachments or []:
            if not isinstance(attachment, dict) or not attachment.get("name"):
                continue
            ips = attachment.get("ips") or []
            if ips:
                networkIps[str(attachment["name"])] = str(ips[0]).split("/", 1)[0]
        endpoints[app] = {
            "id": app,
            "app": app,
            "asn": int(labels.get("seedemu.io/asn") or match.group("asn")),
            "router": labels.get("seedemu.io/name") or f"r{match.group('router_id')}",
            "routerId": int(match.group("router_id")),
            "targetIp": match.group("ip"),
            "networkIps": networkIps,
        }
    return endpoints


def directCandidatePlan(
    report: dict[str, Any], endpoints: dict[str, dict[str, Any]], measure: Any,
    topology: dict[str, Any], assignment: dict[str, Any], topologyHash: str,
    topologySize: int, seed: int, pairsPerStratum: int,
) -> dict[str, Any]:
    """Build candidates whose measured traffic uses one real virtual link.

    The generic dataplane planner's ``intra_as_link`` stratum represents an
    IX-to-IX demand inside a transit AS and may span several emulated routers.
    E1-B defines X(pi) from host crossings of the measured logical link, so its
    intra-AS candidates must instead be the two endpoints of one compiled
    ``net-<asn>-...`` network. Each endpoint is addressed through that exact
    interface; the IX/eBGP candidates receive the same treatment.
    """
    intra: list[dict[str, Any]] = []
    for networkName, network in sorted((report.get("networks") or {}).items()):
        if str(network.get("kind")) != "intra-AS":
            continue
        names = sorted(set(str(item) for item in (network.get("endpoints") or [])))
        if len(names) != 2 or any(name not in endpoints for name in names):
            continue
        left, right = ({**endpoints[name]} for name in names)
        leftIp = left.get("networkIps", {}).get(networkName)
        rightIp = right.get("networkIps", {}).get(networkName)
        if not leftIp or not rightIp:
            continue
        left["targetIp"], right["targetIp"] = leftIp, rightIp
        pair = measure.make_pair("intra_as_link", left, right)
        if pair:
            pair.update({"directNetwork": networkName, "networkCidr": network.get("cidr")})
            intra.append(pair)

    # Keep only declared eBGP adjacencies and address them on their shared IX.
    generic = measure.generate_pair_plan(
        topology=topology, assignment=assignment, endpoints=endpoints,
        topology_size=topologySize, topology_hash=topologyHash, pair_seed=seed,
        pairs_per_stratum=10000, max_pairs_total=0,
        enabled_strata={"ebgp_ix_peer"}, pair_endpoint_cap=10000, plan_version=3,
    )
    ebgp: list[dict[str, Any]] = []
    for item in generic.get("pairs") or []:
        left = {**endpoints[str(item["src"]["id"])]}
        right = {**endpoints[str(item["dst"]["id"])]}
        shared = sorted(set(left.get("networkIps", {})) & set(right.get("networkIps", {})))
        ixNetworks = [name for name in shared if name.startswith("net-ix-")]
        if len(ixNetworks) != 1:
            continue
        networkName = ixNetworks[0]
        left["targetIp"] = left["networkIps"][networkName]
        right["targetIp"] = right["networkIps"][networkName]
        pair = measure.make_pair("ebgp_ix_peer", left, right)
        if pair:
            network = (report.get("networks") or {}).get(networkName) or {}
            pair.update({"directNetwork": networkName, "networkCidr": network.get("cidr")})
            ebgp.append(pair)

    selected: list[dict[str, Any]] = []
    for stratum, candidates in (("intra_as_link", intra), ("ebgp_ix_peer", ebgp)):
        unique = {str(item["pairId"]): item for item in candidates}
        ordered = sorted(unique.values(), key=lambda item: stableKey(
            seed, topologyHash, stratum, str(item["pairId"])))
        core: list[dict[str, Any]] = []
        used: set[str] = set()
        for item in ordered:
            endpointIds = {str(item["src"]["id"]), str(item["dst"]["id"])}
            if endpointIds & used:
                continue
            core.append(item)
            used.update(endpointIds)
            if len(core) == 8:
                break
        if len(core) != 8:
            raise ValueError(f"only {len(core)} vertex-disjoint direct {stratum} pairs exist")
        chosenIds = {str(item["pairId"]) for item in core}
        extras = [item for item in ordered if str(item["pairId"]) not in chosenIds]
        selected.extend((core + extras)[:pairsPerStratum])
    return {
        "metadata": {
            "planVersion": 4,
            "pairSeed": seed,
            "topologySize": topologySize,
            "topologyHash": topologyHash,
            "status": "pilot_pending",
            "linkSemantics": "compiled-direct-network",
            "pairsPerStratum": pairsPerStratum,
            "candidateCounts": {
                "intra_as_link": len({item["pairId"] for item in intra}),
                "ebgp_ix_peer": len({item["pairId"] for item in ebgp}),
            },
            "selectionSummary": measure.pair_plan_selection_summary(selected),
        },
        "pairs": selected,
    }


def selectVertexDisjointPairs(candidatePlan: dict[str, Any], countPerStratum: int) -> list[dict[str, Any]]:
    """Choose eight pairs per stratum without endpoint reuse."""
    selected: list[dict[str, Any]] = []
    used: set[str] = set()
    for stratum in ("intra_as_link", "ebgp_ix_peer"):
        candidates = [item for item in candidatePlan["pairs"] if item["stratum"] == stratum]
        chosen = 0
        for item in candidates:
            endpoints = {str(item["src"]["id"]), str(item["dst"]["id"])}
            if endpoints & used:
                continue
            selected.append(item)
            used.update(endpoints)
            chosen += 1
            if chosen == countPerStratum:
                break
        if chosen != countPerStratum:
            raise ValueError(f"only {chosen} vertex-disjoint {stratum} pairs are available")
    return selected


def networkAdjacency(report: dict[str, Any]) -> dict[str, dict[str, float]]:
    """Build a topology-only affinity proxy; this is not the X(pi) metric."""
    adjacency: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for network in (report.get("networks") or {}).values():
        endpoints = sorted(set(network.get("endpoints") or []))
        if len(endpoints) < 2:
            continue
        perPair = float(network.get("weight") or 1.0) / max(1, len(endpoints) - 1)
        for index, left in enumerate(endpoints):
            for right in endpoints[index + 1:]:
                adjacency[left][right] += perPair
                adjacency[right][left] += perPair
    return {pod: dict(peers) for pod, peers in adjacency.items()}


def assignInitial(
    podNames: list[str], workers: list[str], capacities: dict[str, int],
    reserved: dict[str, str], adjacency: dict[str, dict[str, float]], seed: int,
) -> dict[str, str]:
    placement = dict(reserved)
    remaining = dict(capacities)
    for node in placement.values():
        remaining[node] -= 1
    if min(remaining.values()) < 0:
        raise ValueError("reserved endpoints exceed initial node capacity")

    unassigned = [name for name in podNames if name not in placement]
    unassigned.sort(key=lambda name: (-sum(adjacency.get(name, {}).values()), stableKey(seed, name)))
    for pod in unassigned:
        available = [node for node in workers if remaining[node] > 0]
        if not available:
            raise ValueError("initial capacity exhausted before all Pods were assigned")
        def score(node: str) -> tuple[float, str]:
            affinity = sum(weight for peer, weight in adjacency.get(pod, {}).items()
                           if placement.get(peer) == node)
            return (-affinity, stableKey(seed, pod, node))
        chosen = min(available, key=score)
        placement[pod] = chosen
        remaining[chosen] -= 1
    if any(remaining.values()):
        raise ValueError(f"initial assignment did not fill capacities: {remaining}")
    return placement


def cutDelta(pod: str, source: str, target: str, placement: dict[str, str],
             adjacency: dict[str, dict[str, float]]) -> float:
    before = after = 0.0
    for peer, weight in adjacency.get(pod, {}).items():
        peerNode = placement.get(peer)
        before += weight if peerNode is not None and peerNode != source else 0.0
        after += weight if peerNode is not None and peerNode != target else 0.0
    return after - before


def flowCrossings(placement: dict[str, str], flows: list[dict[str, Any]]) -> int:
    return sum(placement[item["src"]["id"]] != placement[item["dst"]["id"]] for item in flows)


def summarizePlacement(
    mappingId: str, placement: dict[str, str], workers: list[str], report: dict[str, Any],
    flows: list[dict[str, Any]], offeredMbps: float, kubeletMaxPods: int,
    systemPodReserve: int, moved: list[dict[str, Any]], relation: str,
) -> dict[str, Any]:
    pods = {item["name"]: item for item in report["pods"]}
    networks = report["networks"]
    perNode: dict[str, dict[str, Any]] = {}
    for node in workers:
        names = sorted(name for name, assigned in placement.items() if assigned == node)
        networkNames = {network for name in names for network in pods[name].get("networks", [])}
        perNode[node] = {
            "emulationPods": len(names),
            "syntheticWorkloadPods": len(names),
            "fixedCanaryPods": 1,
            "systemPodReserve": systemPodReserve,
            "projectedAdmissionPods": len(names) * 2 + 1 + systemPodReserve,
            "kubeletMaxPods": kubeletMaxPods,
            "admissionFeasible": len(names) * 2 + 1 + systemPodReserve <= kubeletMaxPods,
            "cpuEstimateCores": sum(float(pods[name].get("cpu") or 0.0) for name in names),
            "memoryEstimateMiB": sum(float(pods[name].get("memoryMb") or 0.0) for name in names),
            "secondaryInterfaces": sum(len(pods[name].get("networks") or []) for name in names),
            "uniqueVlanParents": len({n for n in networkNames if networks.get(n, {}).get("vlanId") is not None}),
        }
    crossNetworks = 0
    weightedPairCut = 0.0
    for network in networks.values():
        endpoints = [name for name in set(network.get("endpoints") or []) if name in placement]
        nodes = {placement[name] for name in endpoints}
        if len(nodes) > 1:
            crossNetworks += 1
        for index, left in enumerate(endpoints):
            for right in endpoints[index + 1:]:
                if placement[left] != placement[right]:
                    weightedPairCut += float(network.get("weight") or 1.0)
    crossings = flowCrossings(placement, flows)
    canonical = json.dumps(placement, sort_keys=True, separators=(",", ":")).encode()
    return {
        "mappingId": mappingId,
        "relation": relation,
        "placementHash": hashlib.sha256(canonical).hexdigest(),
        "targetWorker": workers[0],
        "targetWorkerPodCount": perNode[workers[0]]["emulationPods"],
        "totalEmulationPods": len(placement),
        "perNode": perNode,
        "fixedFlowCrossings": crossings,
        "xExpectedPayloadMbpsCrossings": crossings * offeredMbps,
        "topologyProxy": {
            "crossNodeNetworkCount": crossNetworks,
            "weightedExpandedPairCut": weightedPairCut,
            "scope": "selection/reporting proxy only; not X(pi)",
        },
        "movedFromPrior": moved,
        "assignments": dict(sorted(placement.items())),
    }


def injectManifest(docs: list[dict[str, Any]], placement: dict[str, str]) -> list[dict[str, Any]]:
    rendered = json.loads(json.dumps(docs))
    seen: set[str] = set()
    for doc in rendered:
        if doc.get("kind") != "Deployment":
            continue
        name = str((doc.get("metadata") or {}).get("name") or "")
        if name not in placement:
            continue
        template = (doc.setdefault("spec", {}).setdefault("template", {}))
        spec = template.setdefault("spec", {})
        spec.pop("nodeName", None)
        selector = spec.setdefault("nodeSelector", {})
        selector["kubernetes.io/hostname"] = placement[name]
        annotations = template.setdefault("metadata", {}).setdefault("annotations", {})
        annotations["seedemu.k8s.placement/assigned-node"] = placement[name]
        annotations["seedemu.k8s.placement/pinning-mode"] = "node-selector"
        annotations["seedemu.k8s.e1b/mapping-id"] = "frozen"
        seen.add(name)
    missing = set(placement) - seen
    if missing:
        raise ValueError(f"manifest lacks {len(missing)} planned Deployments: {sorted(missing)[:3]}")
    return rendered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-manifest", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--placement-report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--topology", type=Path, default=DEFAULT_TOPOLOGY)
    parser.add_argument("--assignment", type=Path, default=DEFAULT_ASSIGNMENT)
    parser.add_argument("--historical-pings", type=Path, default=DEFAULT_OLD_PINGS)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--seed", type=int, default=20260918)
    parser.add_argument("--offered-mbps-per-flow", type=float, default=25.0)
    parser.add_argument("--kubelet-max-pods", type=int, default=240)
    parser.add_argument("--system-pod-reserve", type=int, default=20)
    args = parser.parse_args(argv)

    source = args.source_manifest.expanduser().resolve()
    reportPath = args.placement_report.expanduser().resolve()
    output = args.output_dir.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    docs = [item for item in yaml.safe_load_all(source.read_text()) if isinstance(item, dict)]
    report = json.loads(reportPath.read_text())
    podNames = sorted(item["name"] for item in report["pods"])
    if len(podNames) != 214 or len(set(podNames)) != 214:
        raise ValueError(f"E1-B requires exactly 214 unique emulation Pods, found {len(set(podNames))}")

    measure = loadMeasureModule()
    endpoints = manifestEndpoints(docs, measure)
    topology, topologyHash = measure.parse_topology(args.topology.expanduser().resolve())
    assignment = measure.load_assignment(args.assignment.expanduser().resolve())
    candidates = directCandidatePlan(
        report=report, endpoints=endpoints, measure=measure,
        topology=topology, assignment=assignment, topologyHash=topologyHash,
        topologySize=214, seed=args.seed, pairsPerStratum=12,
    )
    selectedRaw = selectVertexDisjointPairs(candidates, 8)

    oldReachability: dict[tuple[str, str], dict[str, Any]] = {}
    if args.historical_pings.is_file():
        for item in json.loads(args.historical_pings.read_text()):
            oldReachability[(str(item.get("pairId")), str(item.get("directionId"))[-1:])] = {
                "ok": item.get("ok"), "lossPct": item.get("lossPct"),
                "source": str(args.historical_pings.resolve()),
            }
    flows = []
    for index, pair in enumerate(selectedRaw):
        suffix = "a" if index % 2 == 0 else "b"
        srcKey, dstKey = (("src", "dst") if suffix == "a" else ("dst", "src"))
        flows.append({
            "flowId": f"e1b-{index:02d}-{pair['pairId']}-{suffix}",
            "pairId": pair["pairId"], "stratum": pair["stratum"],
            "directionSuffix": suffix, "src": pair[srcKey], "dst": pair[dstKey],
            "logicalPath": [pair[srcKey]["id"], pair[dstKey]["id"]],
            "logicalPathKind": "compiled-direct-network",
            "directNetwork": pair["directNetwork"],
            "networkCidr": pair.get("networkCidr"),
            "offeredMbps": args.offered_mbps_per_flow,
            "historicalReachability": oldReachability.get((pair["pairId"], suffix)),
            "pilotValidationRequired": True,
        })

    workers = [f"b63-e1b-worker{index}" for index in range(1, 5)]
    target = workers[0]
    capacities = dict(zip(workers, [44, 57, 57, 56]))
    beneficial = flows[:6] + flows[8:14]
    remainingFlows = flows[6:8] + flows[14:16]
    batches = [beneficial[index:index + 3] for index in range(0, 12, 3)]
    reserved: dict[str, str] = {}
    moveEndpoint: dict[str, str] = {}
    sourceByMoved: dict[str, str] = {}
    for batchIndex, batch in enumerate(batches):
        for within, flow in enumerate(batch):
            sourceNode = workers[1 + within]
            # Alternate the anchored side so sender and receiver load are both represented.
            anchorKey, movingKey = (("src", "dst") if (batchIndex + within) % 2 == 0 else ("dst", "src"))
            anchor = str(flow[anchorKey]["id"])
            moving = str(flow[movingKey]["id"])
            reserved[anchor] = target
            reserved[moving] = sourceNode
            moveEndpoint[flow["flowId"]] = moving
            sourceByMoved[moving] = sourceNode
    for index, flow in enumerate(remainingFlows):
        left = workers[1 + index % 3]
        right = workers[1 + (index + 1) % 3]
        reserved[str(flow["src"]["id"])] = left
        reserved[str(flow["dst"]["id"])] = right

    adjacency = networkAdjacency(report)
    current = assignInitial(podNames, workers, capacities, reserved, adjacency, args.seed)
    sequence: list[dict[str, Any]] = []
    sequence.append(summarizePlacement(
        "pi00-target044", current, workers, report, flows, args.offered_mbps_per_flow,
        args.kubelet_max_pods, args.system_pod_reserve, [], "primary",
    ))
    targetCounts = [54, 64, 74, 84]
    sourceQuotas = [
        {workers[1]: 3, workers[2]: 4, workers[3]: 3},
        {workers[1]: 4, workers[2]: 3, workers[3]: 3},
        {workers[1]: 3, workers[2]: 3, workers[3]: 4},
        {workers[1]: 3, workers[2]: 4, workers[3]: 3},
    ]
    allFlowEndpoints = {str(flow[key]["id"]) for flow in flows for key in ("src", "dst")}
    for batchIndex, (batch, quota, targetCount) in enumerate(zip(batches, sourceQuotas, targetCounts), 1):
        moves: list[dict[str, Any]] = []
        for flow in batch:
            pod = moveEndpoint[flow["flowId"]]
            sourceNode = current[pod]
            current[pod] = target
            quota[sourceNode] -= 1
            moves.append({"pod": pod, "from": sourceNode, "to": target,
                          "reason": "fixed-flow-locality", "flowId": flow["flowId"]})
        for sourceNode in workers[1:]:
            for _ in range(quota[sourceNode]):
                candidatesForMove = [name for name, node in current.items()
                                     if node == sourceNode and name not in allFlowEndpoints]
                if not candidatesForMove:
                    raise ValueError(f"no neutral move candidate remains on {sourceNode}")
                pod = min(candidatesForMove,
                          key=lambda name: (cutDelta(name, sourceNode, target, current, adjacency),
                                            stableKey(args.seed, str(batchIndex), sourceNode, name)))
                delta = cutDelta(pod, sourceNode, target, current, adjacency)
                current[pod] = target
                moves.append({"pod": pod, "from": sourceNode, "to": target,
                              "reason": "topology-affinity-filler", "topologyProxyCutDelta": delta})
        item = summarizePlacement(
            f"pi{batchIndex:02d}-target{targetCount:03d}", current, workers, report, flows,
            args.offered_mbps_per_flow, args.kubelet_max_pods, args.system_pod_reserve,
            sorted(moves, key=lambda value: value["pod"]), "primary",
        )
        if item["targetWorkerPodCount"] != targetCount:
            raise ValueError(f"target count mismatch at batch {batchIndex}")
        if item["fixedFlowCrossings"] >= sequence[-1]["fixedFlowCrossings"]:
            raise ValueError(f"X(pi) did not strictly decrease at batch {batchIndex}")
        sequence.append(item)

    matched = dict(sequence[1]["assignments"])
    matchedMoves: list[dict[str, Any]] = []
    for flow in batches[0]:
        moved = moveEndpoint[flow["flowId"]]
        originalSource = sourceByMoved[moved]
        swapCandidates = [name for name, node in matched.items()
                          if node == originalSource and name not in allFlowEndpoints]
        swap = min(swapCandidates,
                   key=lambda name: (cutDelta(name, target, originalSource, matched, adjacency),
                                     stableKey(args.seed, "matched", flow["flowId"], name)))
        matched[moved] = originalSource
        matched[swap] = target
        matchedMoves.extend([
            {"pod": moved, "from": target, "to": originalSource,
             "reason": "restore-cross-worker-flow", "flowId": flow["flowId"]},
            {"pod": swap, "from": originalSource, "to": target,
             "reason": "preserve-identical-node-counts"},
        ])
    matchedItem = summarizePlacement(
        "matched-count-target054-low-locality", matched, workers, report, flows,
        args.offered_mbps_per_flow, args.kubelet_max_pods, args.system_pod_reserve,
        matchedMoves, "matched-count-control",
    )
    if {node: value["emulationPods"] for node, value in matchedItem["perNode"].items()} != {
        node: value["emulationPods"] for node, value in sequence[1]["perNode"].items()
    }:
        raise ValueError("matched-count control changed the per-node Pod counts")
    if matchedItem["fixedFlowCrossings"] <= sequence[1]["fixedFlowCrossings"]:
        raise ValueError("matched-count control does not have higher cross-worker demand")

    mappings = sequence + [matchedItem]
    writeJson(output / "candidate_pair_plan.json", candidates)
    writeJson(output / "selected_pair_plan.json", {
        "metadata": {
            "status": "pilot_pending", "selectionSeed": args.seed,
            "candidatePairsPerStratum": 12, "selectedPairsPerStratum": 8,
            "oneDirectionPerPair": True, "offeredMbpsPerFlow": args.offered_mbps_per_flow,
            "topologyHash": topologyHash, "linkSemantics": "compiled-direct-network",
        },
        "flows": flows,
    })
    writeJson(output / "placement_sequence.json", {
        "metadata": {
            "status": "pilot_pending", "selectionUsesDynamicPerformance": False,
            "linkSemantics": "compiled-direct-network",
            "workers": workers, "targetWorker": target, "batchSize": 10,
            "kubeletMaxPods": args.kubelet_max_pods,
            "systemPodReserveForAdmissionOnly": args.system_pod_reserve,
            "syntheticWorkloadPodsPerEmulationPod": 1,
        },
        "primarySequence": [item["mappingId"] for item in sequence],
        "matchedCountControls": [matchedItem["mappingId"]],
        "mappings": mappings,
    })
    writeJson(output / "group_moves.json", {
        item["mappingId"]: item["movedFromPrior"] for item in mappings if item["movedFromPrior"]
    })
    manifests = output / "manifests"
    manifests.mkdir(exist_ok=True)
    for item in mappings:
        rendered = injectManifest(docs, item["assignments"])
        for doc in rendered:
            if doc.get("kind") == "Deployment":
                name = str((doc.get("metadata") or {}).get("name") or "")
                if name in item["assignments"]:
                    metadata = (((doc.get("spec") or {}).get("template") or {}).get("metadata") or {})
                    metadata.setdefault("annotations", {})["seedemu.k8s.e1b/mapping-id"] = item["mappingId"]
        (manifests / f"{item['mappingId']}.yaml").write_text(
            yaml.safe_dump_all(rendered, sort_keys=False), encoding="utf-8")

    protocol = {
        "schemaVersion": 2, "status": "pilot_pending",
        "linkSemantics": "compiled-direct-network",
        "sourceManifest": str(source), "sourceManifestSha256": sha256(source),
        "placementReport": str(reportPath), "placementReportSha256": sha256(reportPath),
        "topology": str(args.topology.resolve()), "topologySha256": sha256(args.topology.resolve()),
        "assignment": str(args.assignment.resolve()), "assignmentSha256": sha256(args.assignment.resolve()),
        "planner": str(Path(__file__).resolve()), "plannerSha256": sha256(Path(__file__).resolve()),
        "mappingIds": [item["mappingId"] for item in mappings],
        "expectedPrimaryCrossings": [item["fixedFlowCrossings"] for item in sequence],
        "expectedPrimaryXPayloadMbpsCrossings": [item["xExpectedPayloadMbpsCrossings"] for item in sequence],
        "invariants": {
            "emulationPodCount": 214, "workerCount": 4, "controlPlaneCarriesExperimentPods": False,
            "onlineMigration": False, "sameTopologyAndProtocol": True,
            "mappingSelectionReadsDynamicPerformance": False,
        },
    }
    writeJson(output / "frozen_protocol.json", protocol)
    print(json.dumps({
        "output": str(output),
        "primary": [{"mappingId": item["mappingId"], "counts": {
            node: item["perNode"][node]["emulationPods"] for node in workers},
            "crossings": item["fixedFlowCrossings"],
            "xMbpsCrossings": item["xExpectedPayloadMbpsCrossings"]} for item in sequence],
        "matchedControl": {"mappingId": matchedItem["mappingId"],
                           "crossings": matchedItem["fixedFlowCrossings"]},
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
