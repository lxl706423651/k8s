#!/usr/bin/env python3
"""Validate BGP protocols from one deterministic BRD pod per AS.

Inputs:
- Kubernetes namespace.
- Experiment artifact directory.

Outputs:
- bgp_test_targets.json with the selected BRD pod per AS.
- bgp_test_summary.json with pass/fail details.

Side effects:
- Read-only kubectl exec calls to run `birdc show protocols`.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EXIT_BGP_TEST_FAILED = 61
TARGET_RE = re.compile(r"^as(?P<asn>\d+)brd-r(?P<rid>\d+)-")
BAD_INFO_RE = re.compile(r"\b(idle|active|connect|start|error|down|passive)\b", re.IGNORECASE)
KUBECONFIG_PATH = ""


@dataclass
class BgpTarget:
    asn: str
    pod: str
    node: str
    router_id: int


def nowUtc() -> str:
    """Return current UTC time text."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log(message: str) -> None:
    """Print one timestamped log line."""
    print(f"[{nowUtc()}] {message}", flush=True)


def run(cmd: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    """Run a command and convert timeouts into return code 124."""
    try:
        return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else f"command timed out after {timeout} seconds"
        return subprocess.CompletedProcess(cmd, 124, stdout, stderr)


def kubectl(namespace: str, args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    """Run kubectl in one namespace with an explicit kubeconfig when provided."""
    command = ["kubectl"]
    if KUBECONFIG_PATH:
        command.extend(["--kubeconfig", KUBECONFIG_PATH])
    command.extend(["-n", namespace, *args])
    return run(command, timeout=timeout)


def loadBrdTargets(namespace: str) -> list[BgpTarget]:
    """Select one BRD pod per AS, choosing the smallest r-number."""
    result = kubectl(namespace, ["get", "pods", "-o", "json"], timeout=90)
    result.check_returncode()
    data = json.loads(result.stdout)
    best: dict[str, BgpTarget] = {}
    for item in data.get("items", []):
        meta = item.get("metadata", {}) or {}
        labels = meta.get("labels", {}) or {}
        if labels.get("seedemu.io/workload") != "seedemu":
            continue
        if labels.get("seedemu.io/role") != "brd":
            continue
        if (item.get("status", {}) or {}).get("phase") != "Running":
            continue
        name = str(meta.get("name", ""))
        match = TARGET_RE.match(name)
        if not match:
            continue
        asn = str(labels.get("seedemu.io/asn") or match.group("asn"))
        router_id = int(match.group("rid"))
        target = BgpTarget(
            asn=asn,
            pod=name,
            node=str((item.get("spec", {}) or {}).get("nodeName", "")),
            router_id=router_id,
        )
        if asn not in best or router_id < best[asn].router_id or (router_id == best[asn].router_id and name < best[asn].pod):
            best[asn] = target
    return sorted(best.values(), key=lambda item: (int(item.asn), item.router_id, item.pod))


def parseProtocolLines(output: str) -> list[dict[str, Any]]:
    """Parse `birdc show protocols` into protocol rows."""
    rows: list[dict[str, Any]] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("BIRD ") or stripped.startswith("Name "):
            continue
        parts = stripped.split(None, 5)
        if len(parts) < 4:
            continue
        name = parts[0]
        proto = parts[1]
        table = parts[2]
        state = parts[3]
        since = parts[4] if len(parts) >= 5 else ""
        info = parts[5] if len(parts) >= 6 else ""
        rows.append({"name": name, "proto": proto, "table": table, "state": state, "since": since, "info": info, "line": stripped})
    return rows


def badBgpProtocols(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return BGP protocol rows that are not in a healthy up/established state."""
    bgp_rows = [row for row in rows if row["proto"].upper() == "BGP" or row["name"].lower().startswith("bgp")]
    if not bgp_rows:
        return [{"name": "<none>", "proto": "BGP", "state": "missing", "info": "no BGP protocols found", "line": ""}]
    bad = []
    for row in bgp_rows:
        state_ok = str(row["state"]).lower() == "up"
        info = str(row.get("info", ""))
        info_ok = not BAD_INFO_RE.search(info)
        if not state_ok or not info_ok:
            bad.append(row)
    return bad


def summarizeFailures(failures: list[dict[str, Any]]) -> dict[str, Any]:
    """Return compact failed-brdnode and failed-protocol views."""
    failed_brdnodes = sorted({str(item.get("pod") or "") for item in failures if item.get("pod")})
    failed_protocols: list[dict[str, Any]] = []
    for item in failures:
        protocols = item.get("protocols") or []
        if not protocols:
            failed_protocols.append(
                {
                    "asn": item.get("asn"),
                    "brdnode": item.get("pod"),
                    "protocol": "<birdc>",
                    "state": item.get("reason"),
                    "info": item.get("stderr", ""),
                }
            )
            continue
        for protocol in protocols:
            failed_protocols.append(
                {
                    "asn": item.get("asn"),
                    "brdnode": item.get("pod"),
                    "protocol": protocol.get("name"),
                    "proto": protocol.get("proto"),
                    "state": protocol.get("state"),
                    "info": protocol.get("info"),
                    "line": protocol.get("line"),
                }
            )
    return {
        "failedBrdnodes": failed_brdnodes,
        "failedProtocols": failed_protocols,
    }


def checkTarget(namespace: str, target: BgpTarget, exec_timeout: int) -> tuple[int, dict[str, Any] | None]:
    """Check one target pod and return a success count plus optional failure."""
    result = kubectl(namespace, ["exec", target.pod, "--", "birdc", "show", "protocols"], timeout=exec_timeout)
    if result.returncode != 0:
        return 0, {
            "asn": target.asn,
            "pod": target.pod,
            "node": target.node,
            "routerId": target.router_id,
            "reason": "birdc_failed",
            "stderr": (result.stderr or result.stdout).strip(),
            "protocols": [],
        }
    rows = parseProtocolLines(result.stdout)
    bad = badBgpProtocols(rows)
    if bad:
        return 0, {
            "asn": target.asn,
            "pod": target.pod,
            "node": target.node,
            "routerId": target.router_id,
            "reason": "bgp_protocol_not_up",
            "protocols": bad,
        }
    return 1, None


def verifyOnce(namespace: str, targets: list[BgpTarget], exec_timeout: int) -> tuple[int, list[dict[str, Any]]]:
    """Run one node-aware verification round."""
    nodes_map: dict[str, list[BgpTarget]] = defaultdict(list)
    for target in targets:
        nodes_map[target.node].append(target)
    verified = 0
    failures: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, len(nodes_map))) as pool:
        futures = {
            pool.submit(checkTargetsOnNode, namespace, node, node_targets, exec_timeout): node
            for node, node_targets in nodes_map.items()
        }
        for future in as_completed(futures):
            ok_count, node_failures = future.result()
            verified += ok_count
            failures.extend(node_failures)
    return verified, failures


def checkTargetsOnNode(namespace: str, node: str, targets: list[BgpTarget], exec_timeout: int) -> tuple[int, list[dict[str, Any]]]:
    """Check all selected targets scheduled on one Kubernetes node."""
    verified = 0
    failures: list[dict[str, Any]] = []
    for idx, target in enumerate(targets, start=1):
        ok_count, failure = checkTarget(namespace, target, exec_timeout)
        verified += ok_count
        if failure:
            failures.append(failure)
        if idx % 100 == 0 or idx == len(targets):
            log(f"node={node} checked={idx}/{len(targets)} failures={len(failures)}")
    return verified, failures


def parseArgs() -> argparse.Namespace:
    """Parse explicit BGP protocol test parameters."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("positional", nargs="*", help="legacy form: <namespace> <artifact_dir>")
    parser.add_argument("--namespace")
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--kubeconfig", default="")
    parser.add_argument("--exec-timeout-seconds", type=int, default=45)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--retry-interval-seconds", type=int, default=10)
    args = parser.parse_args()
    if args.positional:
        if len(args.positional) != 2:
            parser.error("legacy positional usage is: <namespace> <artifact_dir>")
        args.namespace = args.namespace or args.positional[0]
        args.artifact_dir = args.artifact_dir or Path(args.positional[1])
    if not args.namespace or not args.artifact_dir:
        parser.error("--namespace and --artifact-dir are required")
    return args


def main() -> int:
    """CLI entrypoint."""
    global KUBECONFIG_PATH
    args = parseArgs()
    namespace = args.namespace
    artifact_dir = args.artifact_dir
    artifact_dir.mkdir(parents=True, exist_ok=True)
    KUBECONFIG_PATH = args.kubeconfig
    targets_file = artifact_dir / "bgp_test_targets.json"
    summary_file = artifact_dir / "bgp_test_summary.json"
    exec_timeout = args.exec_timeout_seconds
    timeout_seconds = args.timeout_seconds
    retry_interval = args.retry_interval_seconds

    targets = loadBrdTargets(namespace)
    targets_file.write_text(json.dumps([asdict(target) for target in targets], indent=2), encoding="utf-8")
    summary: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "namespace": namespace,
        "status": "PASS",
        "pass": True,
        "targets": len(targets),
        "verified": 0,
        "failures": [],
        "failedBrdnodes": [],
        "failedProtocols": [],
        "strategy": "one-smallest-r-brd-per-as-node-aware-concurrency",
    }
    if not targets:
        summary["status"] = "FAIL"
        summary["pass"] = False
        summary["failure_reason"] = "no_brd_targets_found"
        summary_file.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return EXIT_BGP_TEST_FAILED

    log(f"selected {len(targets)} BRD targets from namespace {namespace}")
    deadline = time.time() + timeout_seconds
    round_id = 0
    while time.time() < deadline:
        round_id += 1
        log(f"bgp_test_round={round_id}")
        verified, failures = verifyOnce(namespace, targets, exec_timeout)
        summary["verified"] = verified
        summary["failures"] = failures
        summary.update(summarizeFailures(failures))
        summary["duration_seconds"] = round(timeout_seconds - max(0, deadline - time.time()), 2)
        if not failures:
            summary["status"] = "PASS"
            summary["pass"] = True
            summary["failedBrdnodes"] = []
            summary["failedProtocols"] = []
            summary_file.write_text(json.dumps(summary, indent=2), encoding="utf-8")
            log(f"BGP test completed verified={verified}/{len(targets)}")
            return 0
        log(f"BGP test pending_failures={len(failures)}; retry in {retry_interval}s")
        time.sleep(retry_interval)

    summary["status"] = "FAIL"
    summary["pass"] = False
    summary["failure_reason"] = "bgp_protocol_test_failed"
    summary_file.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return EXIT_BGP_TEST_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
