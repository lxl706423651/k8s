#!/usr/bin/env python3
"""Measure the frozen E1-B direct-link flow plan.

Modes are deliberately separate: preflight validates addresses/routes, UDP
checks a fixed offered load and single-sided fabric TX counters, and TCP checks
capacity.  TCP bytes are never reported as static communication savings.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import importlib.util
import json
import statistics
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Any


HERE = Path(__file__).resolve().parent


def loadMeasure() -> Any:
    path = HERE / "running/measureDataplane.py"
    spec = importlib.util.spec_from_file_location("e1b_dataplane_base", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def writeJson(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def resolveFlows(plan: dict[str, Any], endpoints: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    resolved: list[dict[str, Any]] = []
    missing: list[str] = []
    for flow in plan.get("flows") or []:
        src = endpoints.get(str(flow["src"]["id"]))
        dst = endpoints.get(str(flow["dst"]["id"]))
        if src is None or dst is None:
            missing.append(str(flow["flowId"]))
            continue
        resolved.append({
            "directionId": flow["flowId"], "flowId": flow["flowId"],
            "pairId": flow["pairId"], "stratum": flow["stratum"],
            "src": {**flow["src"], "pod": src["pod"], "node": src["node"], "podIp": src["podIp"]},
            "dst": {**flow["dst"], "pod": dst["pod"], "node": dst["node"], "podIp": dst["podIp"]},
            "nodeRelation": "same_node" if src["node"] == dst["node"] else "cross_node",
            "logicalPath": flow["logicalPath"], "logicalPathKind": flow["logicalPathKind"],
            "offeredMbps": float(flow["offeredMbps"]),
        })
    return resolved, missing


def kubectl(kubeconfig: Path, namespace: str, args: list[str], timeout: float) -> dict[str, Any]:
    command = ["kubectl", "--kubeconfig", str(kubeconfig), "-n", namespace, *args]
    started = time.time()
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False)
        return {"ok": result.returncode == 0, "returncode": result.returncode,
                "stdout": result.stdout, "stderr": result.stderr,
                "durationSeconds": time.time() - started, "command": command, "timedOut": False}
    except subprocess.TimeoutExpired as error:
        return {"ok": False, "returncode": None, "stdout": error.stdout or "",
                "stderr": error.stderr or "", "durationSeconds": time.time() - started,
                "command": command, "timedOut": True}


def preflightOne(kubeconfig: Path, namespace: str, flow: dict[str, Any], rawDir: Path) -> dict[str, Any]:
    route = kubectl(kubeconfig, namespace,
                    ["exec", flow["src"]["pod"], "--", "ip", "route", "get", flow["dst"]["targetIp"]], 30)
    ping = kubectl(kubeconfig, namespace,
                   ["exec", flow["src"]["pod"], "--", "ping", "-c", "5", "-i", "0.2", "-W", "2",
                    flow["dst"]["targetIp"]], 30)
    rawDir.mkdir(parents=True, exist_ok=True)
    (rawDir / f"{flow['flowId']}.route.txt").write_text(route["stdout"] + route["stderr"])
    (rawDir / f"{flow['flowId']}.ping.txt").write_text(ping["stdout"] + ping["stderr"])
    return {"flowId": flow["flowId"], "stratum": flow["stratum"],
            "src": flow["src"], "dst": flow["dst"], "nodeRelation": flow["nodeRelation"],
            "logicalPath": flow["logicalPath"], "routeOk": route["ok"], "routeOutput": route["stdout"].strip(),
            "pingOk": ping["ok"], "ping": ping}


def parseUdp(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {"jsonOk": False, "error": "client stdout is not JSON"}
    end = value.get("end") or {}
    # iperf3's UDP ``end.sum`` is the sender view on the client. Prefer the
    # receiver view so loss reduces receivedMbps and deliveryRatio as intended.
    received = end.get("sum_received") or end.get("sum") or {}
    sent = end.get("sum_sent") or {}
    receiverBps = received.get("bits_per_second")
    senderBps = sent.get("bits_per_second")
    return {
        "jsonOk": True,
        "actualSendMbps": float(senderBps) / 1e6 if senderBps is not None else None,
        "receivedMbps": float(receiverBps) / 1e6 if receiverBps is not None else None,
        "sentBytes": sent.get("bytes"), "receivedBytes": received.get("bytes"),
        "lostPackets": received.get("lost_packets"), "packets": received.get("packets"),
        "lossPct": received.get("lost_percent"), "jitterMs": received.get("jitter_ms"),
        "iperfError": value.get("error"),
    }


def udpOne(kubeconfig: Path, namespace: str, flow: dict[str, Any], duration: int,
           port: int, rawDir: Path) -> dict[str, Any]:
    rawDir.mkdir(parents=True, exist_ok=True)
    serverCommand = ["kubectl", "--kubeconfig", str(kubeconfig), "-n", namespace, "exec",
                     flow["dst"]["pod"], "--", "iperf3", "-s", "-1", "-J", "-p", str(port)]
    server = subprocess.Popen(serverCommand, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(1)
    client = kubectl(kubeconfig, namespace,
                     ["exec", flow["src"]["pod"], "--", "iperf3", "-c", flow["dst"]["targetIp"],
                      "-u", "-b", f"{flow['offeredMbps']:g}M", "-P", "1", "-t", str(duration),
                      "-J", "-p", str(port)], duration + 30)
    try:
        serverOut, serverErr = server.communicate(timeout=duration + 30)
        serverTimedOut = False
    except subprocess.TimeoutExpired:
        server.kill()
        serverOut, serverErr = server.communicate()
        serverTimedOut = True
    (rawDir / f"{flow['flowId']}.client.json").write_text(str(client["stdout"]))
    (rawDir / f"{flow['flowId']}.server.json").write_text(serverOut or "")
    parsed = parseUdp(str(client["stdout"]))
    ok = bool(client["ok"] and parsed.get("jsonOk") and parsed.get("receivedMbps") is not None)
    return {"flowId": flow["flowId"], "stratum": flow["stratum"], "port": port,
            "src": flow["src"], "dst": flow["dst"], "nodeRelation": flow["nodeRelation"],
            "offeredMbps": flow["offeredMbps"], "ok": ok, "clientReturncode": client["returncode"],
            "clientTimedOut": client["timedOut"], "serverReturncode": server.returncode,
            "serverTimedOut": serverTimedOut, "serverStderr": serverErr, **parsed}


def inventoryWorkers(path: Path) -> list[dict[str, str]]:
    import yaml
    value = yaml.safe_load(path.read_text()) or {}
    result = []
    for node in value.get("nodes") or []:
        if node.get("role") != "worker":
            continue
        ssh = node.get("ssh") or {}
        result.append({"name": str(node["name"]),
                       "ip": str(node.get("managementIp") or node.get("ip")),
                       "user": str(ssh.get("user") or "ubuntu"),
                       "key": str(Path(str(ssh.get("key") or "~/.ssh/id_ed25519")).expanduser())})
    return result


def fabricCounters(workers: list[dict[str, str]], interfaces: list[str]) -> dict[str, Any]:
    scriptParts = []
    for interface in interfaces:
        for metric in ("tx_bytes", "tx_packets", "rx_bytes", "rx_packets"):
            scriptParts.append(f"printf '{interface} {metric} '; cat /sys/class/net/{interface}/statistics/{metric}")
    script = "; ".join(scriptParts)
    result: dict[str, Any] = {}
    for worker in workers:
        command = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
                   "-o", "UserKnownHostsFile=/dev/null", "-i", worker["key"],
                   f"{worker['user']}@{worker['ip']}", script]
        completed = subprocess.run(command, text=True, capture_output=True, timeout=30, check=False)
        counters: dict[str, int] = {}
        for line in completed.stdout.splitlines():
            fields = line.split()
            if len(fields) == 3:
                counters[f"{fields[0]}.{fields[1]}"] = int(fields[2])
        result[worker["name"]] = {"ok": completed.returncode == 0, "counters": counters,
                                  "stderr": completed.stderr.strip()}
    return result


def counterDelta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    perNode: dict[str, Any] = {}
    totals: Counter[str] = Counter()
    for node in sorted(set(before) | set(after)):
        left = (before.get(node) or {}).get("counters") or {}
        right = (after.get(node) or {}).get("counters") or {}
        delta = {key: int(right.get(key, 0)) - int(value) for key, value in left.items() if key in right}
        perNode[node] = delta
        totals.update(delta)
    return {"perNode": perNode, "sumAcrossWorkers": dict(totals),
            "interpretation": "sum of worker-side interface counters; use TX as the single-sided primary quantity"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("preflight", "udp", "tcp"), required=True)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--pair-plan", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--duration-seconds", type=int, default=60)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--ping-count", type=int, default=10)
    parser.add_argument("--interfaces", default="ens3,ens4")
    args = parser.parse_args(argv)
    if args.duration_seconds < 1 or args.repetitions < 1:
        parser.error("duration and repetitions must be positive")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    measure = loadMeasure()
    endpoints = measure.list_router_endpoints(args.kubeconfig.resolve(), args.namespace, 180)
    plan = json.loads(args.pair_plan.resolve().read_text())
    flows, missing = resolveFlows(plan, endpoints)
    runtime = {"endpointCount": len(endpoints), "flowCount": len(flows), "missingFlows": missing,
               "flowNodeRelations": dict(Counter(item["nodeRelation"] for item in flows))}
    writeJson(output / "runtime.json", runtime)
    if missing or len(flows) != 16:
        writeJson(output / "summary.json", {"mode": args.mode, "status": "harness_invalid", **runtime})
        return 1

    if args.mode == "preflight":
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda flow: preflightOne(args.kubeconfig.resolve(), args.namespace,
                                                               flow, output / "raw"), flows))
        ok = all(item["routeOk"] and item["pingOk"] for item in results)
        summary = {"mode": "preflight", "status": "success" if ok else "harness_invalid",
                   "flowCount": len(results), "passed": sum(item["routeOk"] and item["pingOk"] for item in results),
                   "results": results}
        writeJson(output / "summary.json", summary)
        return 0 if ok else 1

    if args.mode == "udp":
        if args.inventory is None:
            parser.error("--inventory is required for UDP fabric counters")
        workers = inventoryWorkers(args.inventory.resolve())
        interfaces = [item.strip() for item in args.interfaces.split(",") if item.strip()]
        before = fabricCounters(workers, interfaces)
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            futures = [pool.submit(udpOne, args.kubeconfig.resolve(), args.namespace, flow,
                                   args.duration_seconds, 6100 + index, output / "raw")
                       for index, flow in enumerate(flows)]
            results = [future.result() for future in futures]
        after = fabricCounters(workers, interfaces)
        successful = [item for item in results if item["ok"]]
        offered = sum(float(item["offeredMbps"]) for item in results)
        actual = sum(float(item.get("actualSendMbps") or 0.0) for item in successful)
        received = sum(float(item.get("receivedMbps") or 0.0) for item in successful)
        summary = {"mode": "udp-fixed-offered", "status": "success" if len(successful) == 16 else "workload_overload",
                   "durationSeconds": args.duration_seconds, "flowCount": 16, "successfulFlows": len(successful),
                   "offeredMbps": offered, "actualSendMbps": actual, "receivedMbps": received,
                   "senderAchievementRatio": actual / offered if offered else None,
                   "deliveryRatio": received / actual if actual else None,
                   "lossPctMean": statistics.fmean(float(item.get("lossPct") or 0.0) for item in successful)
                                      if successful else None,
                   "fabricBefore": before, "fabricAfter": after,
                   "fabricDelta": counterDelta(before, after), "flows": sorted(results, key=lambda x: x["flowId"])}
        writeJson(output / "summary.json", summary)
        return 0

    rounds = measure.run_stress_suite(
        kubeconfig=args.kubeconfig.resolve(), namespace=args.namespace, directions=flows,
        raw_dir=output / "raw", topology_hash=str(plan.get("metadata", {}).get("topologyHash") or "e1b"),
        seed=int(plan.get("metadata", {}).get("selectionSeed") or 20260918), levels=[16],
        repetitions=args.repetitions, duration=args.duration_seconds,
        canary_ping_count=args.ping_count, canary_ping_pairs=min(8, len(flows)),
        server_startup_seconds=1.0, max_endpoint_appearances=4,
        avoid_bidirectional_pairs=True,
    )
    aggregates = [float(item["aggregateGoodputMbps"]) for item in rounds]
    summary = {"mode": "tcp-capacity", "status": "success", "flowCount": 16,
               "durationSeconds": args.duration_seconds, "technicalRepetitions": args.repetitions,
               "aggregateGoodputMbps": {"values": aggregates,
                                           "mean": statistics.fmean(aggregates) if aggregates else None},
               "rounds": rounds}
    writeJson(output / "summary.json", summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
