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
import os
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

import yaml


EXIT_BGP_TEST_FAILED = 61
TARGET_RE = re.compile(r"^as(?P<asn>\d+)brd-r(?P<rid>\d+)-")
ROUTE_TOTAL_RE = re.compile(r"^Total:\s+(?P<routes>\d+)\s+of\s+\d+\s+routes\s+for\s+(?P<networks>\d+)\s+networks", re.M)
OUTPUT_MARKER = "__SEEDEMU_BGP_ROUTE_COUNT__"
POD_LIST_TIMEOUT_SECONDS = 300
KUBECTL_REQUEST_TIMEOUT_SECONDS = 120
BGP_TEST_EXEC_TIMEOUT_SECONDS = 45
BGP_TEST_TIMEOUT_SECONDS = 1800
BGP_TEST_RETRY_INTERVAL_SECONDS = 10
KUBECONFIG_PATH: str | None = None


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


def resolveKubeconfig(explicit_path: str | None) -> str | None:
    """Resolve the kubeconfig path used by this read-only test."""
    if explicit_path:
        return explicit_path
    local_path = Path(__file__).resolve().with_name("kubeconfig.yaml")
    if local_path.exists():
        return str(local_path)
    env_path = os.environ.get("KUBECONFIG")
    return env_path or None


def getNested(data: dict[str, Any], dotted: str, default: Any = None) -> Any:
    """Read a dotted key from a nested dict."""
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def resolveAssignmentPath(run_dir: Path) -> Path:
    """Find the assignment.yaml used to infer the namespace for one-arg mode."""
    run_assignment = run_dir / "assignment.yaml"
    if run_assignment.exists():
        return run_assignment
    local_assignment = Path(__file__).resolve().with_name("assignment.yaml")
    if local_assignment.exists():
        return local_assignment
    raise SystemExit(f"Cannot infer namespace: no assignment.yaml in {run_dir} or beside test.py")


def inferNamespace(run_dir: Path) -> str:
    """Infer the Kubernetes namespace from assignment.yaml."""
    assignment_path = resolveAssignmentPath(run_dir)
    assignment = yaml.safe_load(assignment_path.read_text(encoding="utf-8")) or {}
    namespace = str(getNested(assignment, "experiment.namespace", "") or "").strip()
    if namespace:
        return namespace
    topology_size = getNested(assignment, "experiment.topologySize")
    if topology_size is not None:
        return f"seedemu-b62-{int(topology_size)}"
    raise SystemExit(f"Cannot infer namespace from {assignment_path}")


def parseArgs(argv: list[str]) -> argparse.Namespace:
    """Parse CLI arguments while keeping the legacy two-position form."""
    parser = argparse.ArgumentParser(
        description="Validate BGP protocols from one deterministic BRD pod per AS.",
        usage="test.py <run_dir> [--kubeconfig PATH] | test.py <namespace> <artifact_dir> [--kubeconfig PATH]",
    )
    parser.add_argument("values", nargs="+")
    parser.add_argument("--kubeconfig", default=None)
    parsed = parser.parse_args(argv)
    if len(parsed.values) == 1:
        artifact_dir = Path(parsed.values[0])
        parsed.namespace = inferNamespace(artifact_dir)
        parsed.artifact_dir = artifact_dir
        return parsed
    if len(parsed.values) == 2:
        parsed.namespace = parsed.values[0]
        parsed.artifact_dir = Path(parsed.values[1])
        return parsed
    parser.error("expected either <run_dir> or <namespace> <artifact_dir>")
    raise AssertionError("unreachable")


def kubectl(namespace: str, args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    """Run kubectl in one namespace with a stable kubeconfig when available."""
    cmd = ["kubectl"]
    if KUBECONFIG_PATH:
        cmd.extend(["--kubeconfig", KUBECONFIG_PATH])
    cmd.extend([f"--request-timeout={KUBECTL_REQUEST_TIMEOUT_SECONDS}s", "-n", namespace, *args])
    return run(cmd, timeout=timeout)


def loadBrdTargets(namespace: str) -> list[BgpTarget]:
    """Select one BRD pod per AS, choosing the smallest r-number."""
    result = kubectl(namespace, ["get", "pods", "-o", "json"], timeout=POD_LIST_TIMEOUT_SECONDS)
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


def establishedBgp(row: dict[str, Any]) -> bool:
    """Return whether one BGP row is established."""
    return str(row["state"]).lower() == "up" and "established" in str(row.get("info", "")).lower()


def routeCount(output: str) -> tuple[int, int]:
    """Parse `birdc show route count` total route/network counts."""
    match = ROUTE_TOTAL_RE.search(output)
    if not match:
        return 0, 0
    return int(match.group("routes")), int(match.group("networks"))


def splitCombinedOutput(output: str) -> tuple[str, str]:
    """Split combined protocol and route-count command output."""
    if OUTPUT_MARKER not in output:
        return output, ""
    protocols, routes = output.split(OUTPUT_MARKER, 1)
    return protocols, routes


def assessBgpProtocols(rows: list[dict[str, Any]], route_output: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Return BGP failures, reserved warnings, and route metrics."""
    bgp_rows = [row for row in rows if row["proto"].upper() == "BGP" or row["name"].lower().startswith("bgp")]
    routes, networks = routeCount(route_output)
    metrics = {"routes": routes, "networks": networks, "bgpProtocols": len(bgp_rows), "establishedBgpProtocols": 0}
    if not bgp_rows:
        return [{"name": "<none>", "proto": "BGP", "state": "missing", "info": "no BGP protocols found", "line": ""}], [], metrics
    failures = []
    warnings = []
    for row in bgp_rows:
        if establishedBgp(row):
            metrics["establishedBgpProtocols"] += 1
            continue
        failures.append(row)
    if metrics["establishedBgpProtocols"] == 0:
        failures.append({"name": "<none>", "proto": "BGP", "state": "missing", "info": "no established BGP protocols found", "line": ""})
    if routes <= 0 or networks <= 0:
        failures.append({"name": "<routes>", "proto": "Route", "state": "empty", "info": "no IPv4 routes learned", "line": route_output.strip()})
    return failures, warnings, metrics


def checkTarget(namespace: str, target: BgpTarget, exec_timeout: int) -> tuple[int, dict[str, Any] | None, dict[str, Any] | None]:
    """Check one target pod and return a success count plus optional failure."""
    shell_cmd = f"birdc show protocols && printf '\\n{OUTPUT_MARKER}\\n' && birdc show route count"
    result = kubectl(namespace, ["exec", target.pod, "--", "sh", "-lc", shell_cmd], timeout=exec_timeout)
    if result.returncode != 0:
        return 0, {
            "asn": target.asn,
            "pod": target.pod,
            "node": target.node,
            "routerId": target.router_id,
            "reason": "birdc_failed",
            "stderr": (result.stderr or result.stdout).strip(),
            "protocols": [],
        }, None
    protocols_output, route_output = splitCombinedOutput(result.stdout)
    rows = parseProtocolLines(protocols_output)
    failures, warnings, metrics = assessBgpProtocols(rows, route_output)
    warning_payload = None
    if warnings:
        warning_payload = {
            "asn": target.asn,
            "pod": target.pod,
            "node": target.node,
            "routerId": target.router_id,
            "reason": "external_bgp_not_established",
            "metrics": metrics,
            "protocols": warnings[:20],
            "omittedProtocolWarnings": max(0, len(warnings) - 20),
        }
    if failures:
        return 0, {
            "asn": target.asn,
            "pod": target.pod,
            "node": target.node,
            "routerId": target.router_id,
            "reason": "bgp_protocol_not_up",
            "metrics": metrics,
            "protocols": failures,
        }, warning_payload
    return 1, None, warning_payload


def verifyOnce(namespace: str, targets: list[BgpTarget], exec_timeout: int) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]]]:
    """Run one node-aware verification round."""
    nodes_map: dict[str, list[BgpTarget]] = defaultdict(list)
    for target in targets:
        nodes_map[target.node].append(target)
    verified = 0
    failures: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, len(nodes_map))) as pool:
        futures = {
            pool.submit(checkTargetsOnNode, namespace, node, node_targets, exec_timeout): node
            for node, node_targets in nodes_map.items()
        }
        for future in as_completed(futures):
            ok_count, node_failures, node_warnings = future.result()
            verified += ok_count
            failures.extend(node_failures)
            warnings.extend(node_warnings)
    return verified, failures, warnings


def checkTargetsOnNode(namespace: str, node: str, targets: list[BgpTarget], exec_timeout: int) -> tuple[int, list[dict[str, Any]], list[dict[str, Any]]]:
    """Check all selected targets scheduled on one Kubernetes node."""
    verified = 0
    failures: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for idx, target in enumerate(targets, start=1):
        ok_count, failure, warning = checkTarget(namespace, target, exec_timeout)
        verified += ok_count
        if failure:
            failures.append(failure)
        if warning:
            warnings.append(warning)
        if idx % 100 == 0 or idx == len(targets):
            log(f"node={node} checked={idx}/{len(targets)} failures={len(failures)}")
    return verified, failures, warnings


def main() -> int:
    """CLI entrypoint."""
    args = parseArgs(sys.argv[1:])
    namespace = args.namespace
    artifact_dir = args.artifact_dir
    explicit_kubeconfig = args.kubeconfig
    global KUBECONFIG_PATH
    KUBECONFIG_PATH = resolveKubeconfig(explicit_kubeconfig)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    targets_file = artifact_dir / "bgp_test_targets.json"
    summary_file = artifact_dir / "bgp_test_summary.json"
    exec_timeout = BGP_TEST_EXEC_TIMEOUT_SECONDS
    timeout_seconds = BGP_TEST_TIMEOUT_SECONDS
    retry_interval = BGP_TEST_RETRY_INTERVAL_SECONDS

    targets = loadBrdTargets(namespace)
    targets_file.write_text(json.dumps([asdict(target) for target in targets], indent=2), encoding="utf-8")
    summary: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "namespace": namespace,
        "status": "PASS",
        "kubeconfig": KUBECONFIG_PATH,
        "targets": len(targets),
        "verified": 0,
        "failures": [],
        "warnings": [],
        "strategy": "one-smallest-r-brd-per-as-node-aware-concurrency",
    }
    if not targets:
        summary["status"] = "FAIL"
        summary["failure_reason"] = "no_brd_targets_found"
        summary_file.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return EXIT_BGP_TEST_FAILED

    log(f"selected {len(targets)} BRD targets from namespace {namespace}")
    deadline = time.time() + timeout_seconds
    round_id = 0
    target_by_pod = {target.pod: target for target in targets}
    pending_targets = targets
    passed_pods: set[str] = set()
    while time.time() < deadline:
        round_id += 1
        log(f"bgp_test_round={round_id} targets={len(pending_targets)}")
        verified, failures, warnings = verifyOnce(namespace, pending_targets, exec_timeout)
        failed_pods = {str(failure.get("pod", "")) for failure in failures if failure.get("pod")}
        passed_pods.update(target.pod for target in pending_targets if target.pod not in failed_pods)
        summary["verified"] = len(passed_pods)
        summary["failures"] = failures
        summary["warnings"] = warnings
        summary["warningCount"] = len(warnings)
        summary["duration_seconds"] = round(timeout_seconds - max(0, deadline - time.time()), 2)
        if not failures:
            summary["status"] = "PASS"
            summary_file.write_text(json.dumps(summary, indent=2), encoding="utf-8")
            log(f"BGP test completed verified={len(targets)}/{len(targets)}")
            return 0
        log(f"BGP test pending_failures={len(failures)}; retry in {retry_interval}s")
        pending_targets = [target_by_pod[pod] for pod in sorted(failed_pods) if pod in target_by_pod]
        if not pending_targets:
            summary["status"] = "FAIL"
            summary["failure_reason"] = "bgp_protocol_test_internal_failure"
            summary_file.write_text(json.dumps(summary, indent=2), encoding="utf-8")
            return EXIT_BGP_TEST_FAILED
        time.sleep(retry_interval)

    summary["status"] = "FAIL"
    summary["failure_reason"] = "bgp_protocol_test_failed"
    summary_file.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return EXIT_BGP_TEST_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
