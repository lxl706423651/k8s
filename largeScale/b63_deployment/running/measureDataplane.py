#!/usr/bin/env python3
# Measure SeedEMU router dataplane latency and throughput after BIRD/FIB setup.
#
# Inputs: live Kubernetes namespace, topology files, optional deterministic pair
#         plan, and measurement parameters.
# Outputs: dataplane/*.json under the experiment artifact directory.
# Side effects: executes ping and iperf3 inside running router pods.
# Context: run from b63_deployment against the active b63 K3s cluster.
from __future__ import annotations

import argparse
import ast
import concurrent.futures
import hashlib
import json
import math
import os
import re
import statistics
import subprocess
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


APP_RE = re.compile(
    r"^as(?P<asn>\d+)brd-r(?P<router_id>\d+)-(?P<ip>\d+\.\d+\.\d+\.\d+)$"
)


def now_ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def stable_hash(*parts: Any) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(str(part).encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_cmd(cmd: list[str], timeout: float | None = None) -> dict[str, Any]:
    started = time.time()
    try:
        cp = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        return {
            "ok": cp.returncode == 0,
            "returncode": cp.returncode,
            "stdout": cp.stdout,
            "stderr": cp.stderr,
            "durationSeconds": time.time() - started,
            "timedOut": False,
            "cmd": cmd,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "returncode": None,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
            "durationSeconds": time.time() - started,
            "timedOut": True,
            "cmd": cmd,
        }


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    idx = (len(ordered) - 1) * pct / 100.0
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] * (hi - idx) + ordered[hi] * (idx - lo)


def summarize_values(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {
            "count": 0,
            "min": None,
            "p10": None,
            "p25": None,
            "avg": None,
            "median": None,
            "p75": None,
            "p90": None,
            "p95": None,
            "max": None,
        }
    return {
        "count": len(values),
        "min": min(values),
        "p10": percentile(values, 10),
        "p25": percentile(values, 25),
        "avg": statistics.fmean(values),
        "median": statistics.median(values),
        "p75": percentile(values, 75),
        "p90": percentile(values, 90),
        "p95": percentile(values, 95),
        "max": max(values),
    }


def jain_fairness(values: list[float]) -> float | None:
    if not values:
        return None
    numerator = sum(values) ** 2
    denominator = len(values) * sum(v * v for v in values)
    if denominator <= 0:
        return None
    return numerator / denominator


def coefficient_of_variation(values: list[float]) -> float | None:
    if not values:
        return None
    avg = statistics.fmean(values)
    if avg == 0:
        return 0.0
    if len(values) == 1:
        return 0.0
    return statistics.pstdev(values) / avg


def quality_score(
    aggregate_goodput_mbps: float,
    fairness: float | None,
    loaded_ping_rtt: dict[str, Any],
    loaded_ping_loss: dict[str, Any],
) -> float | None:
    """Return a latency/fairness-normalized goodput score for stress rounds."""
    fair = fairness if fairness is not None else 0.0
    rtt = loaded_ping_rtt.get("avg")
    if rtt is None or float(rtt) <= 0:
        return None
    loss_pct = float(loaded_ping_loss.get("avg") or 0.0)
    loss_factor = max(0.0, 1.0 - loss_pct / 100.0)
    return aggregate_goodput_mbps * fair * loss_factor / float(rtt)


def parse_topology(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_text(encoding="utf-8")
    topology_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    content = re.sub(r"^(\w+):", r'"\1":', raw.strip(), flags=re.MULTILINE)
    lines = content.split("\n")
    if lines:
        lines = [line + "," for line in lines[:-1]] + [lines[-1]]
    return ast.literal_eval("{" + "\n".join(lines) + "}"), topology_hash


def load_assignment(path: Path) -> dict[str, Any]:
    import pickle

    with path.open("rb") as f:
        data = pickle.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"assignment file must contain dict: {path}")
    return data


def kubectl_base(kubeconfig: Path, namespace: str) -> list[str]:
    return ["kubectl", "--kubeconfig", str(kubeconfig), "-n", namespace]


def kubectl_json(kubeconfig: Path, namespace: str, args: list[str], timeout: float) -> Any:
    result = run_cmd(kubectl_base(kubeconfig, namespace) + args + ["-o", "json"], timeout=timeout)
    if not result["ok"]:
        raise RuntimeError(
            f"kubectl {' '.join(args)} failed: rc={result['returncode']} stderr={result['stderr']}"
        )
    return json.loads(result["stdout"])


def normalize_endpoint_from_pod(pod: dict[str, Any]) -> dict[str, Any] | None:
    meta = pod.get("metadata", {})
    labels = meta.get("labels", {}) or {}
    if labels.get("seedemu.io/role") != "brd":
        return None
    app = labels.get("app")
    if not app:
        return None
    match = APP_RE.match(app)
    if not match:
        return None
    status = pod.get("status", {}) or {}
    spec = pod.get("spec", {}) or {}
    return {
        "id": app,
        "app": app,
        "pod": meta.get("name"),
        "asn": int(labels.get("seedemu.io/asn") or match.group("asn")),
        "router": labels.get("seedemu.io/name") or f"r{match.group('router_id')}",
        "routerId": int(match.group("router_id")),
        "targetIp": match.group("ip"),
        "node": spec.get("nodeName"),
        "podIp": status.get("podIP"),
        "phase": status.get("phase"),
    }


def list_router_endpoints(kubeconfig: Path, namespace: str, timeout: float) -> dict[str, dict[str, Any]]:
    pods = kubectl_json(kubeconfig, namespace, ["get", "pods"], timeout=timeout)
    endpoints: dict[str, dict[str, Any]] = {}
    for pod in pods.get("items", []):
        ep = normalize_endpoint_from_pod(pod)
        if not ep:
            continue
        if ep["phase"] != "Running":
            continue
        endpoints[ep["id"]] = ep
    return endpoints


def internal_transit_asns(
    topology: dict[str, Any], assignment: dict[str, Any]
) -> set[int]:
    result: set[int] = set()
    for raw_asn in topology.get("transit_asns", []):
        entry = assignment.get(str(raw_asn))
        if entry and "asn" in entry:
            result.add(int(entry["asn"]))
    return result


def endpoint_key(asn: int, router_id: int) -> str:
    return f"{asn}:r{router_id}"


def build_endpoint_index(endpoints: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index = {}
    for ep in endpoints.values():
        index[endpoint_key(int(ep["asn"]), int(ep["routerId"]))] = ep
    return index


def canonical_pair_id(src: str, dst: str) -> str:
    a, b = sorted([src, dst])
    return stable_hash(a, b)[:16]


def make_pair(stratum: str, a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any] | None:
    if a["id"] == b["id"]:
        return None
    src_id, dst_id = sorted([a["id"], b["id"]])
    src = a if a["id"] == src_id else b
    dst = b if src is a else a
    return {
        "pairId": canonical_pair_id(src["id"], dst["id"]),
        "stratum": stratum,
        "src": {
            "id": src["id"],
            "app": src["app"],
            "asn": src["asn"],
            "router": src["router"],
            "routerId": src["routerId"],
            "targetIp": src["targetIp"],
        },
        "dst": {
            "id": dst["id"],
            "app": dst["app"],
            "asn": dst["asn"],
            "router": dst["router"],
            "routerId": dst["routerId"],
            "targetIp": dst["targetIp"],
        },
    }


def select_pairs(
    candidates: list[dict[str, Any]], seed: int, topology_hash: str, limit: int
) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for pair in candidates:
        unique.setdefault(pair["pairId"], pair)
    ordered = sorted(
        unique.values(),
        key=lambda p: stable_hash(seed, topology_hash, p["stratum"], p["src"]["id"], p["dst"]["id"]),
    )
    return ordered[:limit]


def pair_endpoint_ids(pair: dict[str, Any]) -> tuple[str, str]:
    return str(pair["src"]["id"]), str(pair["dst"]["id"])


def select_balanced_pairs(
    candidates: list[dict[str, Any]],
    seed: int,
    topology_hash: str,
    limit: int,
    initial_endpoint_cap: int,
) -> list[dict[str, Any]]:
    """Select pairs deterministically while spreading endpoint appearances."""
    if limit <= 0:
        return []
    unique: dict[str, dict[str, Any]] = {}
    for pair in candidates:
        unique.setdefault(pair["pairId"], pair)
    ordered = sorted(
        unique.values(),
        key=lambda p: stable_hash(seed, topology_hash, p["stratum"], p["src"]["id"], p["dst"]["id"]),
    )
    if len(ordered) <= limit:
        return ordered

    initial_cap = max(1, initial_endpoint_cap)
    for cap in range(initial_cap, max(initial_cap + 12, initial_cap * 4 + 1)):
        counts: Counter[str] = Counter()
        selected: list[dict[str, Any]] = []
        for pair in ordered:
            src_id, dst_id = pair_endpoint_ids(pair)
            if counts[src_id] >= cap or counts[dst_id] >= cap:
                continue
            selected.append(pair)
            counts[src_id] += 1
            counts[dst_id] += 1
            if len(selected) >= limit:
                return selected

    # Fall back to deterministic fill if the topology has unavoidable hubs.
    selected_by_id = {pair["pairId"]: pair for pair in selected}
    for pair in ordered:
        selected_by_id.setdefault(pair["pairId"], pair)
        if len(selected_by_id) >= limit:
            break
    return list(selected_by_id.values())[:limit]


def pair_plan_selection_summary(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    endpoint_counts: Counter[str] = Counter()
    stratum_counts: Counter[str] = Counter()
    asn_pair_counts: Counter[str] = Counter()
    for pair in pairs:
        src_id, dst_id = pair_endpoint_ids(pair)
        endpoint_counts[src_id] += 1
        endpoint_counts[dst_id] += 1
        stratum_counts[str(pair.get("stratum"))] += 1
        asn_pair = tuple(sorted([str(pair["src"]["asn"]), str(pair["dst"]["asn"])]))
        asn_pair_counts["--".join(asn_pair)] += 1
    endpoint_values = list(endpoint_counts.values())
    return {
        "pairCount": len(pairs),
        "directionCount": len(pairs) * 2,
        "strata": dict(sorted(stratum_counts.items())),
        "uniqueEndpointCount": len(endpoint_counts),
        "endpointAppearance": summarize_values([float(v) for v in endpoint_values]),
        "endpointAppearanceCv": coefficient_of_variation([float(v) for v in endpoint_values]),
        "maxAsnPairReuse": max(asn_pair_counts.values()) if asn_pair_counts else 0,
    }


def generate_pair_plan(
    *,
    endpoints: dict[str, dict[str, Any]],
    topology: dict[str, Any],
    assignment: dict[str, Any],
    topology_hash: str,
    topology_size: int,
    pair_seed: int,
    pairs_per_stratum: int,
    max_pairs_total: int,
    enabled_strata: set[str],
    plan_version: int,
    pair_endpoint_cap: int,
) -> dict[str, Any]:
    transit_asns = internal_transit_asns(topology, assignment)
    filtered = {
        eid: ep for eid, ep in endpoints.items() if not transit_asns or int(ep["asn"]) in transit_asns
    }
    index = build_endpoint_index(filtered)
    intra_candidates: list[dict[str, Any]] = []
    ebgp_candidates: list[dict[str, Any]] = []
    direct_pair_ids: set[str] = set()

    for ix_a, ix_b, raw_asn, *_ in topology.get("ix_ix_transit_edges", []):
        asn_entry = assignment.get(str(raw_asn))
        ix_a_entry = assignment.get(str(ix_a))
        ix_b_entry = assignment.get(str(ix_b))
        if not (asn_entry and ix_a_entry and ix_b_entry):
            continue
        asn = int(asn_entry["asn"])
        ep_a = index.get(endpoint_key(asn, int(ix_a_entry["asn"])))
        ep_b = index.get(endpoint_key(asn, int(ix_b_entry["asn"])))
        if not (ep_a and ep_b):
            continue
        pair = make_pair("intra_as_link", ep_a, ep_b)
        if pair:
            intra_candidates.append(pair)

    for raw_a, raw_b, raw_ix, *_ in topology.get("as_as_ix_edges", []):
        asn_a_entry = assignment.get(str(raw_a))
        asn_b_entry = assignment.get(str(raw_b))
        ix_entry = assignment.get(str(raw_ix))
        if not (asn_a_entry and asn_b_entry and ix_entry):
            continue
        ix = int(ix_entry["asn"])
        ep_a = index.get(endpoint_key(int(asn_a_entry["asn"]), ix))
        ep_b = index.get(endpoint_key(int(asn_b_entry["asn"]), ix))
        if not (ep_a and ep_b):
            continue
        pair = make_pair("ebgp_ix_peer", ep_a, ep_b)
        if pair:
            ebgp_candidates.append(pair)
            direct_pair_ids.add(pair["pairId"])

    endpoint_list = sorted(filtered.values(), key=lambda ep: ep["id"])
    cross_candidates: list[dict[str, Any]] = []
    for i, a in enumerate(endpoint_list):
        for b in endpoint_list[i + 1 :]:
            if int(a["asn"]) == int(b["asn"]):
                continue
            pair = make_pair("cross_as_multihop", a, b)
            if not pair or pair["pairId"] in direct_pair_ids:
                continue
            cross_candidates.append(pair)

    candidate_by_stratum = {
        "intra_as_link": intra_candidates,
        "ebgp_ix_peer": ebgp_candidates,
        "cross_as_multihop": cross_candidates,
    }
    selected: list[dict[str, Any]] = []
    for stratum in ["intra_as_link", "ebgp_ix_peer", "cross_as_multihop"]:
        if stratum not in enabled_strata:
            continue
        if plan_version <= 1:
            selected.extend(
                select_pairs(candidate_by_stratum[stratum], pair_seed, topology_hash, pairs_per_stratum)
            )
        else:
            selected.extend(
                select_balanced_pairs(
                    candidate_by_stratum[stratum],
                    pair_seed,
                    topology_hash,
                    pairs_per_stratum,
                    pair_endpoint_cap,
                )
            )

    deduped: dict[str, dict[str, Any]] = {}
    for pair in selected:
        deduped.setdefault(pair["pairId"], pair)
    selected = sorted(
        deduped.values(),
        key=lambda p: stable_hash(pair_seed, topology_hash, "final", p["stratum"], p["pairId"]),
    )
    if max_pairs_total > 0:
        selected = selected[:max_pairs_total]

    return {
        "metadata": {
            "planVersion": plan_version,
            "pairSeed": pair_seed,
            "topologySize": topology_size,
            "topologyHash": topology_hash,
            "generatedAt": now_ts(),
            "endpointCount": len(filtered),
            "pairsPerStratum": pairs_per_stratum,
            "maxPairsTotal": max_pairs_total,
            "pairEndpointCap": pair_endpoint_cap,
            "enabledStrata": sorted(enabled_strata),
            "candidateCounts": {
                "intra_as_link": len({p["pairId"] for p in intra_candidates}),
                "ebgp_ix_peer": len({p["pairId"] for p in ebgp_candidates}),
                "cross_as_multihop": len({p["pairId"] for p in cross_candidates}),
            },
            "selectionSummary": pair_plan_selection_summary(selected),
        },
        "pairs": selected,
    }


def directions_from_pairs(pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    directions: list[dict[str, Any]] = []
    for pair in pairs:
        for src_key, dst_key, suffix in [("src", "dst", "a"), ("dst", "src", "b")]:
            direction_id = f"{pair['pairId']}-{suffix}"
            directions.append(
                {
                    "directionId": direction_id,
                    "pairId": pair["pairId"],
                    "stratum": pair["stratum"],
                    "src": pair[src_key],
                    "dst": pair[dst_key],
                }
            )
    return directions


def resolve_direction(direction: dict[str, Any], endpoints: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    src = endpoints.get(direction["src"]["id"])
    dst = endpoints.get(direction["dst"]["id"])
    if not src or not dst:
        return None
    resolved = dict(direction)
    resolved["src"] = {**direction["src"], **{"pod": src["pod"], "node": src["node"], "podIp": src["podIp"]}}
    resolved["dst"] = {**direction["dst"], **{"pod": dst["pod"], "node": dst["node"], "podIp": dst["podIp"]}}
    resolved["nodeRelation"] = "same_node" if src["node"] == dst["node"] else "cross_node"
    return resolved


def resolve_directions(
    plan: dict[str, Any], endpoints: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    resolved: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for direction in directions_from_pairs(plan.get("pairs", [])):
        item = resolve_direction(direction, endpoints)
        if item:
            resolved.append(item)
        else:
            missing.append(direction)
    return resolved, missing


def parse_ping(stdout: str) -> dict[str, Any]:
    loss_pct = None
    transmitted = None
    received = None
    packet_line = re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received, ([\d.]+)% packet loss", stdout)
    if packet_line:
        transmitted = int(packet_line.group(1))
        received = int(packet_line.group(2))
        loss_pct = float(packet_line.group(3))
    rtt = {}
    rtt_line = re.search(
        r"(?:rtt|round-trip) min/avg/max/(?:mdev|stddev) = ([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)",
        stdout,
    )
    if rtt_line:
        rtt = {
            "minMs": float(rtt_line.group(1)),
            "avgMs": float(rtt_line.group(2)),
            "maxMs": float(rtt_line.group(3)),
            "mdevMs": float(rtt_line.group(4)),
        }
    return {
        "transmitted": transmitted,
        "received": received,
        "lossPct": loss_pct,
        "rtt": rtt,
    }


def run_ping(
    *,
    kubeconfig: Path,
    namespace: str,
    direction: dict[str, Any],
    count: int,
    interval: float,
    wait_seconds: int,
    raw_dir: Path,
    timeout: float,
) -> dict[str, Any]:
    raw_dir.mkdir(parents=True, exist_ok=True)
    dst_ip = direction["dst"]["targetIp"]
    cmd = kubectl_base(kubeconfig, namespace) + [
        "exec",
        direction["src"]["pod"],
        "--",
        "ping",
        "-c",
        str(count),
        "-i",
        str(interval),
        "-W",
        str(wait_seconds),
        dst_ip,
    ]
    result = run_cmd(cmd, timeout=timeout)
    parsed = parse_ping(result["stdout"])
    raw_path = raw_dir / f"{direction['directionId']}.txt"
    raw_path.write_text(
        "\n".join(
            [
                "$ " + " ".join(cmd),
                "",
                "STDOUT:",
                result["stdout"],
                "",
                "STDERR:",
                result["stderr"],
            ]
        ),
        encoding="utf-8",
    )
    return {
        "directionId": direction["directionId"],
        "pairId": direction["pairId"],
        "stratum": direction["stratum"],
        "nodeRelation": direction["nodeRelation"],
        "src": direction["src"],
        "dst": direction["dst"],
        "ok": result["ok"],
        "returncode": result["returncode"],
        "timedOut": result["timedOut"],
        "durationSeconds": result["durationSeconds"],
        "rawFile": str(raw_path),
        **parsed,
    }


def parse_iperf_client(stdout: str) -> dict[str, Any]:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return {"jsonOk": False, "error": "client stdout is not JSON"}
    end = data.get("end", {}) or {}
    sum_received = end.get("sum_received") or {}
    sum_sent = end.get("sum_sent") or {}
    stream_values = []
    retransmits = 0
    for stream in end.get("streams", []) or []:
        sender = stream.get("sender") or {}
        if "bits_per_second" in sender:
            stream_values.append(float(sender["bits_per_second"]))
        retransmits += int(sender.get("retransmits") or 0)
    bps = sum_received.get("bits_per_second") or sum_sent.get("bits_per_second")
    return {
        "jsonOk": True,
        "bitsPerSecond": float(bps) if bps is not None else None,
        "mbps": float(bps) / 1_000_000.0 if bps is not None else None,
        "bytes": sum_received.get("bytes") or sum_sent.get("bytes"),
        "retransmits": retransmits,
        "streamMbps": [v / 1_000_000.0 for v in stream_values],
        "iperfError": data.get("error"),
    }


def terminate_process(proc: subprocess.Popen[str], timeout: float = 5.0) -> tuple[str, str, bool]:
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return stdout or "", stderr or "", False
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
        return stdout or "", stderr or "", True


def run_iperf_direction(
    *,
    kubeconfig: Path,
    namespace: str,
    direction: dict[str, Any],
    duration: int,
    port: int,
    parallel: int,
    raw_dir: Path,
    server_startup_seconds: float,
    timeout_margin_seconds: int,
) -> dict[str, Any]:
    raw_dir.mkdir(parents=True, exist_ok=True)
    server_cmd = kubectl_base(kubeconfig, namespace) + [
        "exec",
        direction["dst"]["pod"],
        "--",
        "iperf3",
        "-s",
        "-1",
        "-J",
        "-p",
        str(port),
    ]
    client_cmd = kubectl_base(kubeconfig, namespace) + [
        "exec",
        direction["src"]["pod"],
        "--",
        "iperf3",
        "-c",
        direction["dst"]["targetIp"],
        "-J",
        "-t",
        str(duration),
        "-P",
        str(parallel),
        "-p",
        str(port),
    ]
    started = time.time()
    server_proc = subprocess.Popen(
        server_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(server_startup_seconds)
    client = run_cmd(client_cmd, timeout=duration + timeout_margin_seconds)
    server_stdout, server_stderr, server_killed = terminate_process(
        server_proc, timeout=duration + timeout_margin_seconds
    )
    parsed = parse_iperf_client(client["stdout"])
    base = f"{direction['directionId']}_p{port}"
    client_raw = raw_dir / f"{base}.client.json"
    server_raw = raw_dir / f"{base}.server.json"
    meta_raw = raw_dir / f"{base}.meta.json"
    client_raw.write_text(client["stdout"] or "", encoding="utf-8")
    server_raw.write_text(server_stdout or "", encoding="utf-8")
    write_json(
        meta_raw,
        {
            "serverCmd": server_cmd,
            "clientCmd": client_cmd,
            "clientReturncode": client["returncode"],
            "clientTimedOut": client["timedOut"],
            "clientStderr": client["stderr"],
            "serverReturncode": server_proc.returncode,
            "serverTimedOut": server_killed,
            "serverStderr": server_stderr,
        },
    )
    ok = bool(client["ok"] and parsed.get("jsonOk") and parsed.get("bitsPerSecond") is not None)
    return {
        "directionId": direction["directionId"],
        "pairId": direction["pairId"],
        "stratum": direction["stratum"],
        "nodeRelation": direction["nodeRelation"],
        "src": direction["src"],
        "dst": direction["dst"],
        "ok": ok,
        "port": port,
        "parallel": parallel,
        "durationSeconds": time.time() - started,
        "clientReturncode": client["returncode"],
        "clientTimedOut": client["timedOut"],
        "serverReturncode": server_proc.returncode,
        "serverTimedOut": server_killed,
        "clientRawFile": str(client_raw),
        "serverRawFile": str(server_raw),
        "metaFile": str(meta_raw),
        **parsed,
    }


def check_iperf_available(
    kubeconfig: Path,
    namespace: str,
    endpoints: dict[str, dict[str, Any]],
    sample_size: int,
    timeout: float,
) -> dict[str, Any]:
    checked = []
    missing = []
    for ep in sorted(endpoints.values(), key=lambda item: item["id"])[:sample_size]:
        cmd = kubectl_base(kubeconfig, namespace) + [
            "exec",
            ep["pod"],
            "--",
            "sh",
            "-lc",
            "command -v iperf3",
        ]
        result = run_cmd(cmd, timeout=timeout)
        checked.append(
            {
                "id": ep["id"],
                "pod": ep["pod"],
                "node": ep["node"],
                "ok": result["ok"],
                "stdout": result["stdout"].strip(),
                "stderr": result["stderr"].strip(),
            }
        )
        if not result["ok"] or not result["stdout"].strip():
            missing.append(ep["id"])
    return {"checked": checked, "missing": missing, "ok": not missing}


def group_metric(records: list[dict[str, Any]], metric: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "overall": summarize_values([float(r[metric]) for r in records if r.get(metric) is not None]),
        "byStratum": {},
        "byNodeRelation": {},
    }
    by_stratum: defaultdict[str, list[float]] = defaultdict(list)
    by_relation: defaultdict[str, list[float]] = defaultdict(list)
    for record in records:
        value = record.get(metric)
        if value is None:
            continue
        by_stratum[str(record.get("stratum"))].append(float(value))
        by_relation[str(record.get("nodeRelation"))].append(float(value))
    result["byStratum"] = {k: summarize_values(v) for k, v in sorted(by_stratum.items())}
    result["byNodeRelation"] = {k: summarize_values(v) for k, v in sorted(by_relation.items())}
    return result


def run_ping_suite(
    *,
    kubeconfig: Path,
    namespace: str,
    directions: list[dict[str, Any]],
    raw_dir: Path,
    count: int,
    interval: float,
    wait_seconds: int,
    concurrency: int,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    timeout = count * (interval + wait_seconds) + 10
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [
            executor.submit(
                run_ping,
                kubeconfig=kubeconfig,
                namespace=namespace,
                direction=direction,
                count=count,
                interval=interval,
                wait_seconds=wait_seconds,
                raw_dir=raw_dir,
                timeout=timeout,
            )
            for direction in directions
        ]
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
    return sorted(results, key=lambda r: r["directionId"])


def run_iperf_suite(
    *,
    kubeconfig: Path,
    namespace: str,
    directions: list[dict[str, Any]],
    raw_dir: Path,
    duration: int,
    concurrency: int,
    parallel: int,
    base_port: int,
    server_startup_seconds: float,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = []
        for idx, direction in enumerate(directions):
            futures.append(
                executor.submit(
                    run_iperf_direction,
                    kubeconfig=kubeconfig,
                    namespace=namespace,
                    direction=direction,
                    duration=duration,
                    port=base_port + idx,
                    parallel=parallel,
                    raw_dir=raw_dir,
                    server_startup_seconds=server_startup_seconds,
                    timeout_margin_seconds=20,
                )
            )
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
    return sorted(results, key=lambda r: r["directionId"])


def deterministic_directions(
    directions: list[dict[str, Any]], seed: int, topology_hash: str, label: str
) -> list[dict[str, Any]]:
    return sorted(
        directions,
        key=lambda d: stable_hash(seed, topology_hash, label, d["directionId"], d["src"]["id"], d["dst"]["id"]),
    )


def rotated_take(items: list[dict[str, Any]], count: int, offset: int) -> list[dict[str, Any]]:
    if not items or count <= 0:
        return []
    selected = []
    for i in range(count):
        selected.append(items[(offset + i) % len(items)])
    return selected


def direction_endpoint_ids(direction: dict[str, Any]) -> tuple[str, str]:
    return str(direction["src"]["id"]), str(direction["dst"]["id"])


def select_balanced_directions(
    directions: list[dict[str, Any]],
    count: int,
    offset: int,
    max_endpoint_appearances: int,
    avoid_bidirectional_pairs: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Select a deterministic direction subset with per-round anti-hotspot rules."""
    if not directions or count <= 0:
        return [], {"relaxedEndpointCap": None, "bidirectionalPairsAllowed": False}
    if count >= len(directions):
        selected = list(directions)
        return selected, {
            "relaxedEndpointCap": None,
            "bidirectionalPairsAllowed": True,
            "reason": "requested count covers all directions",
        }

    ordered = [directions[(offset + i) % len(directions)] for i in range(len(directions))]
    initial_cap = max(1, max_endpoint_appearances)
    attempts: list[tuple[int, bool]] = []
    for cap in range(initial_cap, max(initial_cap + 12, initial_cap * 4 + 1)):
        attempts.append((cap, avoid_bidirectional_pairs))
    if avoid_bidirectional_pairs:
        for cap in range(initial_cap, max(initial_cap + 12, initial_cap * 4 + 1)):
            attempts.append((cap, False))

    best: list[dict[str, Any]] = []
    best_meta: dict[str, Any] = {}
    for cap, avoid_bidi in attempts:
        endpoint_counts: Counter[str] = Counter()
        pair_counts: Counter[str] = Counter()
        selected = []
        for direction in ordered:
            if direction["directionId"] in {d["directionId"] for d in selected}:
                continue
            if avoid_bidi and pair_counts[str(direction["pairId"])] > 0:
                continue
            src_id, dst_id = direction_endpoint_ids(direction)
            if endpoint_counts[src_id] >= cap or endpoint_counts[dst_id] >= cap:
                continue
            selected.append(direction)
            endpoint_counts[src_id] += 1
            endpoint_counts[dst_id] += 1
            pair_counts[str(direction["pairId"])] += 1
            if len(selected) >= count:
                return selected, {
                    "relaxedEndpointCap": cap,
                    "bidirectionalPairsAllowed": not avoid_bidi,
                    "requestedCount": count,
                    "selectedCount": len(selected),
                }
        if len(selected) > len(best):
            best = selected
            best_meta = {
                "relaxedEndpointCap": cap,
                "bidirectionalPairsAllowed": not avoid_bidi,
                "requestedCount": count,
                "selectedCount": len(selected),
                "reason": "partial constrained selection",
            }

    selected_by_id = {d["directionId"]: d for d in best}
    for direction in ordered:
        selected_by_id.setdefault(direction["directionId"], direction)
        if len(selected_by_id) >= count:
            break
    selected = list(selected_by_id.values())[:count]
    best_meta.update(
        {
            "selectedCount": len(selected),
            "reason": "deterministic fill after relaxed constraints",
        }
    )
    return selected, best_meta


def direction_selection_summary(directions: list[dict[str, Any]]) -> dict[str, Any]:
    endpoint_counts: Counter[str] = Counter()
    node_counts: Counter[str] = Counter()
    pair_counts: Counter[str] = Counter()
    relation_counts: Counter[str] = Counter()
    stratum_counts: Counter[str] = Counter()
    for direction in directions:
        src_id, dst_id = direction_endpoint_ids(direction)
        endpoint_counts[src_id] += 1
        endpoint_counts[dst_id] += 1
        node_counts[str(direction["src"].get("node"))] += 1
        node_counts[str(direction["dst"].get("node"))] += 1
        pair_counts[str(direction["pairId"])] += 1
        relation_counts[str(direction.get("nodeRelation"))] += 1
        stratum_counts[str(direction.get("stratum"))] += 1
    node_values = [float(v) for v in node_counts.values()]
    endpoint_values = [float(v) for v in endpoint_counts.values()]
    return {
        "directionCount": len(directions),
        "nodeEndpointAppearances": dict(sorted(node_counts.items())),
        "nodeEndpointAppearanceCv": coefficient_of_variation(node_values),
        "endpointAppearance": summarize_values(endpoint_values),
        "endpointAppearanceCv": coefficient_of_variation(endpoint_values),
        "unorderedPairCount": len(pair_counts),
        "bidirectionalPairCount": sum(1 for value in pair_counts.values() if value > 1),
        "maxPairReuse": max(pair_counts.values()) if pair_counts else 0,
        "directionsByNodeRelation": dict(sorted(relation_counts.items())),
        "directionsByStratum": dict(sorted(stratum_counts.items())),
    }


def run_stress_suite(
    *,
    kubeconfig: Path,
    namespace: str,
    directions: list[dict[str, Any]],
    raw_dir: Path,
    topology_hash: str,
    seed: int,
    levels: list[int],
    repetitions: int,
    duration: int,
    canary_ping_count: int,
    canary_ping_pairs: int,
    server_startup_seconds: float,
    max_endpoint_appearances: int,
    avoid_bidirectional_pairs: bool,
) -> list[dict[str, Any]]:
    ordered = deterministic_directions(directions, seed, topology_hash, "stress")
    canary_ordered = deterministic_directions(directions, seed, topology_hash, "loaded-ping")
    canaries, canary_selection = select_balanced_directions(
        canary_ordered,
        canary_ping_pairs,
        0,
        max_endpoint_appearances=max_endpoint_appearances,
        avoid_bidirectional_pairs=avoid_bidirectional_pairs,
    )
    rounds: list[dict[str, Any]] = []
    round_index = 0
    for level in levels:
        for repetition in range(repetitions):
            selected, selection_meta = select_balanced_directions(
                ordered,
                level,
                round_index * level,
                max_endpoint_appearances=max_endpoint_appearances,
                avoid_bidirectional_pairs=avoid_bidirectional_pairs,
            )
            round_dir = raw_dir / f"level_{level}_rep_{repetition}"
            round_dir.mkdir(parents=True, exist_ok=True)
            started = time.time()
            with concurrent.futures.ThreadPoolExecutor(max_workers=level + len(canaries) + 2) as executor:
                iperf_future = executor.submit(
                    run_iperf_suite,
                    kubeconfig=kubeconfig,
                    namespace=namespace,
                    directions=selected,
                    raw_dir=round_dir / "iperf",
                    duration=duration,
                    concurrency=level,
                    parallel=1,
                    base_port=5301 + round_index * 100,
                    server_startup_seconds=server_startup_seconds,
                )
                time.sleep(max(1.0, server_startup_seconds))
                ping_future = executor.submit(
                    run_ping_suite,
                    kubeconfig=kubeconfig,
                    namespace=namespace,
                    directions=canaries,
                    raw_dir=round_dir / "loaded_ping",
                    count=canary_ping_count,
                    interval=1.0,
                    wait_seconds=1,
                    concurrency=max(1, min(len(canaries), 8)),
                )
                iperf_results = iperf_future.result()
                ping_results = ping_future.result()
            mbps_values = [float(r["mbps"]) for r in iperf_results if r.get("ok") and r.get("mbps") is not None]
            ping_rtts = [
                float(r["rtt"]["avgMs"])
                for r in ping_results
                if r.get("ok") and isinstance(r.get("rtt"), dict) and r["rtt"].get("avgMs") is not None
            ]
            ping_losses = [
                float(r["lossPct"]) for r in ping_results if r.get("lossPct") is not None
            ]
            loaded_rtt_summary = summarize_values(ping_rtts)
            loaded_loss_summary = summarize_values(ping_losses)
            fairness = jain_fairness(mbps_values)
            round_record = {
                "level": level,
                "repetition": repetition,
                "roundIndex": round_index,
                "durationSeconds": time.time() - started,
                "selectedDirections": [d["directionId"] for d in selected],
                "canaryDirections": [d["directionId"] for d in canaries],
                "flowCount": len(iperf_results),
                "successCount": len(mbps_values),
                "failureRate": 1.0 - (len(mbps_values) / len(iperf_results) if iperf_results else 0.0),
                "aggregateGoodputMbps": sum(mbps_values),
                "flowMbps": summarize_values(mbps_values),
                "fairness": fairness,
                "loadedPingAvgRttMs": loaded_rtt_summary,
                "loadedPingLossPct": loaded_loss_summary,
                "qualityScore": quality_score(sum(mbps_values), fairness, loaded_rtt_summary, loaded_loss_summary),
                "selection": {
                    **selection_meta,
                    "selected": direction_selection_summary(selected),
                    "canaries": direction_selection_summary(canaries),
                    "canarySelection": canary_selection,
                },
                "iperfResultsFile": str(round_dir / "iperf_results.json"),
                "loadedPingResultsFile": str(round_dir / "loaded_ping_results.json"),
            }
            write_json(round_dir / "iperf_results.json", iperf_results)
            write_json(round_dir / "loaded_ping_results.json", ping_results)
            write_json(round_dir / "round_summary.json", round_record)
            rounds.append(round_record)
            round_index += 1
    return rounds


def summarize_runtime(endpoints: dict[str, dict[str, Any]], resolved: list[dict[str, Any]]) -> dict[str, Any]:
    nodes = Counter(ep.get("node") for ep in endpoints.values())
    selected_nodes = Counter()
    relation = Counter()
    strata = Counter()
    for d in resolved:
        selected_nodes[d["src"]["node"]] += 1
        selected_nodes[d["dst"]["node"]] += 1
        relation[d["nodeRelation"]] += 1
        strata[d["stratum"]] += 1
    return {
        "endpointCount": len(endpoints),
        "endpointsByNode": dict(sorted(nodes.items())),
        "selectedEndpointAppearancesByNode": dict(sorted(selected_nodes.items())),
        "directionCount": len(resolved),
        "directionsByNodeRelation": dict(sorted(relation.items())),
        "directionsByStratum": dict(sorted(strata.items())),
    }


def parse_levels(raw: str) -> list[int]:
    if not raw.strip():
        return []
    return [int(x.strip()) for x in raw.split(",") if x.strip()]


def parse_strata(raw: str) -> set[str]:
    allowed = {"intra_as_link", "ebgp_ix_peer", "cross_as_multihop"}
    values = {x.strip() for x in raw.split(",") if x.strip()}
    unknown = values - allowed
    if unknown:
        raise ValueError(f"unknown strata: {', '.join(sorted(unknown))}; allowed={', '.join(sorted(allowed))}")
    return values


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run deterministic SeedEMU dataplane ping/iperf measurements for b63 placements."
    )
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--kubeconfig", default="./kubeconfig-b63.yaml")
    parser.add_argument("--topology-size", type=int, required=True)
    parser.add_argument("--topology-dir", default="/data/lxl/seed-emulator/topology")
    parser.add_argument("--artifact-dir", default=None)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--pair-plan", default=None)
    parser.add_argument("--pair-plan-out", default=None)
    parser.add_argument("--pair-plan-version", type=int, default=2, choices=(1, 2))
    parser.add_argument("--pair-seed", type=int, default=20260624)
    parser.add_argument("--pairs-per-stratum", type=int, default=32)
    parser.add_argument("--max-pairs-total", type=int, default=96)
    parser.add_argument(
        "--pair-endpoint-cap",
        type=int,
        default=2,
        help="initial per-endpoint appearance cap while generating v2 pair plans",
    )
    parser.add_argument(
        "--strata",
        default="intra_as_link,ebgp_ix_peer,cross_as_multihop",
        help=(
            "comma-separated pair strata. v2 includes cross_as_multihop so larger-scale "
            "runs include non-direct AS paths; unreachable paths are measured as failures."
        ),
    )
    parser.add_argument("--ping-count", type=int, default=10)
    parser.add_argument("--ping-interval", type=float, default=0.2)
    parser.add_argument("--ping-wait-seconds", type=int, default=1)
    parser.add_argument("--ping-concurrency", type=int, default=8)
    parser.add_argument("--baseline-tcp-directions", type=int, default=48)
    parser.add_argument("--baseline-tcp-duration", type=int, default=8)
    parser.add_argument("--baseline-tcp-concurrency", type=int, default=4)
    parser.add_argument("--stress-levels", default="8,16,32,48")
    parser.add_argument("--stress-repetitions", type=int, default=3)
    parser.add_argument("--stress-duration", type=int, default=15)
    parser.add_argument("--loaded-ping-canaries", type=int, default=16)
    parser.add_argument("--loaded-ping-count", type=int, default=15)
    parser.add_argument(
        "--round-max-endpoint-appearances",
        type=int,
        default=2,
        help="initial per-endpoint cap for each baseline/stress round",
    )
    parser.add_argument(
        "--allow-bidirectional-round-pairs",
        action="store_true",
        help="allow both directions of the same unordered pair in one iperf round",
    )
    parser.add_argument("--iperf-parallel", type=int, default=1)
    parser.add_argument("--server-startup-seconds", type=float, default=1.5)
    parser.add_argument("--kubectl-timeout", type=float, default=60.0)
    parser.add_argument("--iperf-check-sample", type=int, default=6)
    parser.add_argument("--skip-ping", action="store_true")
    parser.add_argument("--skip-iperf", action="store_true")
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--allow-missing-iperf", action="store_true")
    args = parser.parse_args()

    kubeconfig = Path(args.kubeconfig).resolve()
    topology_dir = Path(args.topology_dir).resolve()
    topology_file = topology_dir / f"real_topology_{args.topology_size}.txt"
    assignment_file = topology_dir / "assignment.pkl"
    topology, topology_hash = parse_topology(topology_file)
    assignment = load_assignment(assignment_file)
    enabled_strata = parse_strata(args.strata)

    if args.artifact_dir:
        artifact_dir = Path(args.artifact_dir).resolve()
    else:
        artifact_dir = Path.cwd() / "experiments" / f"{args.topology_size}_{now_ts()}_dataplane"
    measurement_dir = artifact_dir / "dataplane"
    measurement_dir.mkdir(parents=True, exist_ok=True)

    endpoints = list_router_endpoints(kubeconfig, args.namespace, timeout=args.kubectl_timeout)
    if not endpoints:
        raise RuntimeError(f"no running brd router pods found in namespace {args.namespace}")

    if args.pair_plan:
        pair_plan_path = Path(args.pair_plan).resolve()
        pair_plan = load_json(pair_plan_path)
    else:
        pair_plan = generate_pair_plan(
            endpoints=endpoints,
            topology=topology,
            assignment=assignment,
            topology_hash=topology_hash,
            topology_size=args.topology_size,
            pair_seed=args.pair_seed,
            pairs_per_stratum=args.pairs_per_stratum,
            max_pairs_total=args.max_pairs_total,
            enabled_strata=enabled_strata,
            plan_version=args.pair_plan_version,
            pair_endpoint_cap=args.pair_endpoint_cap,
        )
        pair_plan_path = Path(args.pair_plan_out).resolve() if args.pair_plan_out else (
            measurement_dir
            / f"pair_plan_{args.topology_size}_{topology_hash}_seed{args.pair_seed}_v{args.pair_plan_version}.json"
        )
        write_json(pair_plan_path, pair_plan)

    resolved, missing = resolve_directions(pair_plan, endpoints)
    if not resolved:
        raise RuntimeError("pair plan resolved to zero live directions")

    ordered_directions = deterministic_directions(
        resolved, args.pair_seed, topology_hash, "measurement-order"
    )
    runtime_state = summarize_runtime(endpoints, resolved)
    write_json(measurement_dir / "resolved_directions.json", ordered_directions)
    write_json(measurement_dir / "runtime_state.json", runtime_state)
    if missing:
        write_json(measurement_dir / "missing_directions.json", missing)

    if args.plan_only:
        write_json(
            measurement_dir / "latency_throughput_summary.json",
            {
                "metadata": {
                    "namespace": args.namespace,
                    "topologySize": args.topology_size,
                    "topologyHash": topology_hash,
                    "pairPlan": str(pair_plan_path),
                    "measurementDir": str(measurement_dir),
                    "planOnly": True,
                },
                "runtimeState": runtime_state,
            },
        )
        return 0

    iperf_check = None
    if not args.skip_iperf:
        iperf_check = check_iperf_available(
            kubeconfig,
            args.namespace,
            endpoints,
            sample_size=args.iperf_check_sample,
            timeout=args.kubectl_timeout,
        )
        write_json(measurement_dir / "iperf_check.json", iperf_check)
        if not iperf_check["ok"] and not args.allow_missing_iperf:
            raise RuntimeError(
                "iperf3 is missing in sampled router pods; rebuild/redeploy after addSoftware('iperf3') "
                "or rerun with --skip-iperf"
            )

    ping_results: list[dict[str, Any]] = []
    baseline_iperf_results: list[dict[str, Any]] = []
    baseline_dirs: list[dict[str, Any]] = []
    baseline_selection: dict[str, Any] = {}
    stress_rounds: list[dict[str, Any]] = []

    if not args.skip_ping:
        ping_results = run_ping_suite(
            kubeconfig=kubeconfig,
            namespace=args.namespace,
            directions=ordered_directions,
            raw_dir=measurement_dir / "ping_raw",
            count=args.ping_count,
            interval=args.ping_interval,
            wait_seconds=args.ping_wait_seconds,
            concurrency=args.ping_concurrency,
        )
        write_json(measurement_dir / "ping_results.json", ping_results)

    if not args.skip_iperf:
        baseline_dirs, baseline_selection = select_balanced_directions(
            deterministic_directions(ordered_directions, args.pair_seed, topology_hash, "baseline-tcp"),
            max(0, args.baseline_tcp_directions),
            0,
            max_endpoint_appearances=args.round_max_endpoint_appearances,
            avoid_bidirectional_pairs=not args.allow_bidirectional_round_pairs,
        )
        if baseline_dirs:
            baseline_iperf_results = run_iperf_suite(
                kubeconfig=kubeconfig,
                namespace=args.namespace,
                directions=baseline_dirs,
                raw_dir=measurement_dir / "iperf_baseline_raw",
                duration=args.baseline_tcp_duration,
                concurrency=args.baseline_tcp_concurrency,
                parallel=args.iperf_parallel,
                base_port=5201,
                server_startup_seconds=args.server_startup_seconds,
            )
            write_json(measurement_dir / "iperf_baseline_results.json", baseline_iperf_results)

        levels = parse_levels(args.stress_levels)
        if levels:
            stress_rounds = run_stress_suite(
                kubeconfig=kubeconfig,
                namespace=args.namespace,
                directions=ordered_directions,
                raw_dir=measurement_dir / "stress_raw",
                topology_hash=topology_hash,
                seed=args.pair_seed,
                levels=levels,
                repetitions=args.stress_repetitions,
                duration=args.stress_duration,
                canary_ping_count=args.loaded_ping_count,
                canary_ping_pairs=args.loaded_ping_canaries,
                server_startup_seconds=args.server_startup_seconds,
                max_endpoint_appearances=args.round_max_endpoint_appearances,
                avoid_bidirectional_pairs=not args.allow_bidirectional_round_pairs,
            )
            write_json(measurement_dir / "stress_rounds.json", stress_rounds)

    ping_ok = [r for r in ping_results if r.get("ok")]
    ping_loss = [float(r["lossPct"]) for r in ping_results if r.get("lossPct") is not None]
    ping_rtt = [
        float(r["rtt"]["avgMs"])
        for r in ping_ok
        if isinstance(r.get("rtt"), dict) and r["rtt"].get("avgMs") is not None
    ]
    baseline_mbps = [
        float(r["mbps"])
        for r in baseline_iperf_results
        if r.get("ok") and r.get("mbps") is not None
    ]
    summary = {
        "metadata": {
            "namespace": args.namespace,
            "topologySize": args.topology_size,
            "topologyHash": topology_hash,
            "pairSeed": args.pair_seed,
            "pairPlanVersion": args.pair_plan_version,
            "pairPlan": str(pair_plan_path),
            "measurementDir": str(measurement_dir),
            "generatedAt": now_ts(),
            "skipPing": args.skip_ping,
            "skipIperf": args.skip_iperf,
        },
        "runtimeState": runtime_state,
        "missingDirections": len(missing),
        "iperfCheck": iperf_check,
        "ping": {
            "total": len(ping_results),
            "ok": len(ping_ok),
            "failureRate": 1.0 - (len(ping_ok) / len(ping_results) if ping_results else 0.0),
            "avgRttMs": summarize_values(ping_rtt),
            "packetLossPct": summarize_values(ping_loss),
            "avgRttMsGrouped": group_metric(
                [
                    {**r, "avgRttMs": r.get("rtt", {}).get("avgMs") if isinstance(r.get("rtt"), dict) else None}
                    for r in ping_results
                ],
                "avgRttMs",
            ),
        },
        "baselineTcpIperf": {
            "total": len(baseline_iperf_results),
            "ok": len(baseline_mbps),
            "failureRate": 1.0
            - (len(baseline_mbps) / len(baseline_iperf_results) if baseline_iperf_results else 0.0),
            "flowMbps": summarize_values(baseline_mbps),
            "aggregateGoodputMbps": sum(baseline_mbps),
            "fairness": jain_fairness(baseline_mbps),
            "flowMbpsGrouped": group_metric(baseline_iperf_results, "mbps"),
            "selection": {
                **baseline_selection,
                "selected": direction_selection_summary(baseline_dirs) if baseline_dirs else {},
            },
        },
        "stress": {
            "rounds": stress_rounds,
            "byLevel": {},
        },
    }

    by_level: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    for record in stress_rounds:
        by_level[int(record["level"])].append(record)
    for level, records in sorted(by_level.items()):
        summary["stress"]["byLevel"][str(level)] = {
            "rounds": len(records),
            "aggregateGoodputMbps": summarize_values(
                [float(r["aggregateGoodputMbps"]) for r in records]
            ),
            "failureRate": summarize_values([float(r["failureRate"]) for r in records]),
            "fairness": summarize_values(
                [float(r["fairness"]) for r in records if r.get("fairness") is not None]
            ),
            "loadedPingAvgRttMs": summarize_values(
                [
                    float(r["loadedPingAvgRttMs"]["avg"])
                    for r in records
                    if r.get("loadedPingAvgRttMs", {}).get("avg") is not None
                ]
            ),
            "loadedPingLossPct": summarize_values(
                [
                    float(r["loadedPingLossPct"]["avg"])
                    for r in records
                    if r.get("loadedPingLossPct", {}).get("avg") is not None
                ]
            ),
            "qualityScore": summarize_values(
                [
                    float(r["qualityScore"])
                    for r in records
                    if r.get("qualityScore") is not None
                ]
            ),
            "nodeEndpointAppearanceCv": summarize_values(
                [
                    float(r["selection"]["selected"]["nodeEndpointAppearanceCv"])
                    for r in records
                    if r.get("selection", {}).get("selected", {}).get("nodeEndpointAppearanceCv") is not None
                ]
            ),
            "bidirectionalPairCount": summarize_values(
                [
                    float(r["selection"]["selected"]["bidirectionalPairCount"])
                    for r in records
                    if r.get("selection", {}).get("selected", {}).get("bidirectionalPairCount") is not None
                ]
            ),
        }

    write_json(measurement_dir / "latency_throughput_summary.json", summary)
    write_json(
        measurement_dir / "latency_throughput_report.json",
        {
            "summary": summary,
            "pairPlan": pair_plan,
            "pingResults": ping_results,
            "baselineTcpIperfResults": baseline_iperf_results,
            "stressRounds": stress_rounds,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
