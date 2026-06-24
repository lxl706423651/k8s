#!/usr/bin/env python3
"""Verify fixed brd pods from 10 ASNs after BIRD writes routes into Linux FIB.

The script waits for every VM in the cluster inventory to have load1 below the
configured threshold, selects the smallest-r brd pod from each of the first 10
ASNs, and checks from the pod's own VM that `ip route | wc -l` is greater than
the BIRD network count through kubectl exec. Outputs are verify_after_fib_write_targets.json and
verify_after_fib_write_summary.json in the run directory.
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

import yaml


EXIT_VERIFY_FAILED = 61
TARGET_RE = re.compile(r"^as(?P<asn>\d+)brd-r(?P<rid>\d+)-")
OUTPUT_MARKER = "__SEEDEMU_FIB_ROUTE_COUNT__"


@dataclass
class InventoryNode:
    name: str
    host: str
    user: str
    key: str


@dataclass
class FibTarget:
    asn: int
    routerId: int
    pod: str
    node: str


def nowUtc() -> str:
    """Return a compact UTC timestamp for logs."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log(message: str) -> None:
    """Print one timestamped log line."""
    print(f"[{nowUtc()}] {message}", flush=True)


def runCommand(cmd: list[str], timeout: int | None) -> subprocess.CompletedProcess[str]:
    """Run one local command and convert timeout into return code 124."""
    try:
        return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else f"timeout after {timeout}s"
        return subprocess.CompletedProcess(cmd, 124, stdout, stderr)


def loadInventory(path: Path) -> dict[str, InventoryNode]:
    """Load node SSH metadata from cluster.inventory.yaml."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    nodes: dict[str, InventoryNode] = {}
    for item in data.get("nodes", []):
        name = str(item.get("name", "")).strip()
        host = str(item.get("managementIp") or item.get("management_ip") or item.get("ip") or "").strip()
        ssh = item.get("ssh", {}) or {}
        if not name or not host:
            continue
        nodes[name] = InventoryNode(
            name=name,
            host=host,
            user=str(ssh.get("user") or item.get("sshUser") or "ubuntu"),
            key=str(Path(str(ssh.get("key") or item.get("sshKey") or "~/.ssh/id_ed25519")).expanduser()),
        )
    if not nodes:
        raise SystemExit(f"No nodes found in {path}")
    return nodes


def sshCommand(node: InventoryNode, remote: str, timeout: int | None) -> subprocess.CompletedProcess[str]:
    """Run one remote shell command on a VM through SSH."""
    return runCommand(
        [
            "ssh",
            "-i",
            node.key,
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=8",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "LogLevel=ERROR",
            f"{node.user}@{node.host}",
            remote,
        ],
        timeout=timeout,
    )


def readNodeLoad(node: InventoryNode) -> tuple[str, float | None, str]:
    """Read load1 from one VM."""
    result = sshCommand(node, "awk '{print $1}' /proc/loadavg", timeout=12)
    if result.returncode != 0:
        return node.name, None, (result.stderr or result.stdout).strip()
    try:
        return node.name, float(result.stdout.strip().split()[0]), ""
    except (IndexError, ValueError) as exc:
        return node.name, None, str(exc)


def collectNodeLoads(nodes: dict[str, InventoryNode]) -> dict[str, dict[str, Any]]:
    """Collect load1 values from every VM in parallel."""
    samples: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(16, max(1, len(nodes)))) as pool:
        futures = {pool.submit(readNodeLoad, node): name for name, node in nodes.items()}
        for future in as_completed(futures):
            name, load1, error = future.result()
            samples[name] = {"load1": load1, "error": error}
    return dict(sorted(samples.items()))


def waitForClusterLoad(
    nodes: dict[str, InventoryNode],
    threshold: float,
    interval_seconds: int,
    max_wait_seconds: int,
) -> tuple[float, int, dict[str, dict[str, Any]]]:
    """Wait until every VM load1 is below threshold and return final samples."""
    started = time.time()
    interval_seconds = max(1, interval_seconds)
    while True:
        samples = collectNodeLoads(nodes)
        loads = [item["load1"] for item in samples.values() if isinstance(item.get("load1"), float)]
        max_load = max(loads) if loads else float("inf")
        blocked = [
            f"{name}={item['load1'] if item.get('load1') is not None else item.get('error')}"
            for name, item in samples.items()
            if item.get("load1") is None or float(item["load1"]) >= threshold
        ]
        waited = int(time.time() - started)
        if not blocked:
            log(f"load gate passed max_load1={max_load:.2f} waited={waited}s")
            return max_load, waited, samples
        log(f"load gate waiting threshold={threshold:g} waited={waited}s blocked={'; '.join(blocked[:8])}")
        if max_wait_seconds > 0 and waited >= max_wait_seconds:
            raise TimeoutError(f"load1 did not fall below {threshold:g} within {max_wait_seconds}s")
        time.sleep(interval_seconds)


def kubectlListPods(namespace: str, kubeconfig: str, timeout: int) -> dict[str, Any]:
    """Return raw Kubernetes pod JSON for one namespace."""
    result = runCommand(
        [
            "kubectl",
            "--kubeconfig",
            kubeconfig,
            f"--request-timeout={timeout}s",
            "-n",
            namespace,
            "get",
            "pods",
            "-o",
            "json",
        ],
        timeout=timeout,
    )
    result.check_returncode()
    return json.loads(result.stdout)


def selectTargets(namespace: str, kubeconfig: str, list_timeout: int, sample_as_count: int) -> list[FibTarget]:
    """Select the smallest-r brd pod from each of the first N ASNs."""
    data = kubectlListPods(namespace, kubeconfig, list_timeout)
    best_by_asn: dict[int, FibTarget] = {}
    for item in data.get("items", []):
        metadata = item.get("metadata", {}) or {}
        labels = metadata.get("labels", {}) or {}
        status = item.get("status", {}) or {}
        if status.get("phase") != "Running":
            continue
        if labels.get("seedemu.io/workload") != "seedemu" or labels.get("seedemu.io/role") != "brd":
            continue
        pod = str(metadata.get("name", ""))
        match = TARGET_RE.match(pod)
        if not match:
            continue
        asn = int(str(labels.get("seedemu.io/asn") or match.group("asn")))
        router_id = int(match.group("rid"))
        target = FibTarget(
            asn=asn,
            routerId=router_id,
            pod=pod,
            node=str((item.get("spec", {}) or {}).get("nodeName", "")),
        )
        current = best_by_asn.get(asn)
        if current is None or (target.routerId, target.pod) < (current.routerId, current.pod):
            best_by_asn[asn] = target
    selected_asns = sorted(best_by_asn)[: max(1, sample_as_count)]
    return [best_by_asn[asn] for asn in selected_asns]


def podExec(kubeconfig: str, namespace: str, pod: str, shell_cmd: str, timeout: int) -> subprocess.CompletedProcess[str]:
    """Run a read-only command inside one selected pod through kubectl exec."""
    return runCommand(
        [
            "kubectl",
            "--kubeconfig",
            kubeconfig,
            "-n",
            namespace,
            "exec",
            pod,
            "--",
            "sh",
            "-lc",
            shell_cmd,
        ],
        timeout=timeout,
    )


def splitOutput(output: str) -> tuple[str, str]:
    """Split combined ip-route and bird route-count output."""
    if OUTPUT_MARKER not in output:
        return output, ""
    return output.split(OUTPUT_MARKER, 1)


def parseFirstInteger(output: str) -> int | None:
    """Parse the first integer from command output."""
    match = re.search(r"\b(\d+)\b", output)
    return int(match.group(1)) if match else None


def parseBirdCount(output: str) -> tuple[int | None, str]:
    """Parse the BIRD network count with a route-count fallback."""
    network_match = re.search(r"for\s+(\d+)\s+networks?", output, re.IGNORECASE)
    if network_match:
        return int(network_match.group(1)), "networks"
    route_match = re.search(r"^\s*(?:Total:\s*)?(\d+)\s+(?:of\s+\d+\s+)?routes?", output, re.IGNORECASE | re.MULTILINE)
    if route_match:
        return int(route_match.group(1)), "routes_fallback"
    return None, "parse_failed"


def checkTarget(
    target: FibTarget,
    nodes: dict[str, InventoryNode],
    kubeconfig: str,
    namespace: str,
    exec_timeout: int,
) -> dict[str, Any]:
    """Check one AS representative pod."""
    node = nodes.get(target.node)
    if node is None:
        return {**asdict(target), "passed": False, "failureReason": "node_missing_from_inventory"}
    command = (
        "ip route | wc -l; rc1=$?; "
        f"printf '\\n{OUTPUT_MARKER}\\n'; "
        "birdc show route count; rc2=$?; "
        'if [ "$rc1" -ne 0 ]; then exit "$rc1"; fi; exit "$rc2"'
    )
    result = podExec(kubeconfig, namespace, target.pod, command, timeout=exec_timeout)
    ip_output, bird_output = splitOutput(result.stdout)
    ip_routes = parseFirstInteger(ip_output)
    bird_count, parse_mode = parseBirdCount(bird_output)
    passed = result.returncode == 0 and ip_routes is not None and bird_count is not None and ip_routes > bird_count
    reason = ""
    if result.returncode != 0:
        reason = "route_commands_failed"
    elif ip_routes is None:
        reason = "ip_route_wc_parse_failed"
    elif bird_count is None:
        reason = "bird_route_count_parse_failed"
    elif ip_routes <= bird_count:
        reason = "ip_route_count_not_greater_than_bird_network_count"
    payload: dict[str, Any] = {
        **asdict(target),
        "passed": passed,
        "returnCode": result.returncode,
        "ip_route_count": ip_routes,
        "bird_network_count": bird_count,
        "bird_count_parse_mode": parse_mode,
    }
    if not passed:
        payload.update(
            {
                "failureReason": reason,
                "stderr": (result.stderr or "").strip(),
                "ipRouteRaw": ip_output.strip(),
                "birdRouteCountRaw": bird_output.strip(),
            }
        )
    return payload


def checkNodeTargets(
    node: str,
    targets: list[FibTarget],
    nodes: dict[str, InventoryNode],
    kubeconfig: str,
    namespace: str,
    exec_timeout: int,
) -> list[dict[str, Any]]:
    """Check all targets on one VM serially."""
    results = []
    for index, target in enumerate(targets, start=1):
        results.append(checkTarget(target, nodes, kubeconfig, namespace, exec_timeout))
        if index % 100 == 0 or index == len(targets):
            failures = len([item for item in results if not item.get("passed")])
            log(f"node={node} checked={index}/{len(targets)} failures={failures}")
    return results


def parseArgs(argv: list[str]) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--artifact-dir", required=True, type=Path)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--load-threshold", type=float, default=50.0)
    parser.add_argument("--load-check-interval-seconds", type=int, default=30)
    parser.add_argument("--load-max-wait-seconds", type=int, default=0)
    parser.add_argument("--exec-timeout-seconds", type=int, default=60)
    parser.add_argument("--list-timeout-seconds", type=int, default=300)
    parser.add_argument("--node-concurrency", type=int, default=0)
    parser.add_argument("--sample-as-count", type=int, default=10)
    return parser.parse_args(argv)


def main() -> int:
    """CLI entrypoint."""
    args = parseArgs(sys.argv[1:])
    args.sample_as_count = max(1, args.sample_as_count)
    started = time.time()
    args.artifact_dir.mkdir(parents=True, exist_ok=True)
    targets_path = args.artifact_dir / "verify_after_fib_write_targets.json"
    summary_path = args.artifact_dir / "verify_after_fib_write_summary.json"
    summary: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "namespace": args.namespace,
        "success": False,
        "status": "FAIL",
        "load1": None,
        "load_wait_seconds": None,
        "as_count": 0,
        "sample_as_count": args.sample_as_count,
        "checked_containers": [],
        "failed_samples": [],
        "duration_seconds": None,
        "strategy": "first-n-as-smallest-r-brd-kubectl-exec",
    }
    try:
        nodes = loadInventory(args.inventory)
        max_load, load_wait, load_samples = waitForClusterLoad(
            nodes,
            threshold=args.load_threshold,
            interval_seconds=args.load_check_interval_seconds,
            max_wait_seconds=args.load_max_wait_seconds,
        )
        targets = selectTargets(args.namespace, args.kubeconfig, args.list_timeout_seconds, args.sample_as_count)
        if not targets:
            raise RuntimeError("no running brd targets found")
        targets_path.write_text(json.dumps([asdict(target) for target in targets], indent=2), encoding="utf-8")
        grouped: dict[str, list[FibTarget]] = defaultdict(list)
        for target in targets:
            grouped[target.node].append(target)
        node_concurrency = args.node_concurrency if args.node_concurrency > 0 else len(grouped)
        node_concurrency = max(1, min(node_concurrency, len(grouped)))
        log(f"checking {len(targets)} AS representatives across {len(grouped)} nodes concurrency={node_concurrency}")
        checks: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=node_concurrency) as pool:
            futures = {
                pool.submit(
                    checkNodeTargets,
                    node,
                    node_targets,
                    nodes,
                    args.kubeconfig,
                    args.namespace,
                    args.exec_timeout_seconds,
                ): node
                for node, node_targets in grouped.items()
            }
            for future in as_completed(futures):
                checks.extend(future.result())
        checks.sort(key=lambda item: (int(item["asn"]), int(item["routerId"]), str(item["pod"])))
        failures = [item for item in checks if not item.get("passed")]
        summary.update(
            {
                "success": not failures,
                "status": "PASS" if not failures else "FAIL",
                "load1": max_load,
                "load_wait_seconds": load_wait,
                "node_loads": load_samples,
                "as_count": len(checks),
                "checked_containers": checks,
                "failed_samples": failures,
                "duration_seconds": round(time.time() - started, 2),
            }
        )
        summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
        if failures:
            for item in failures[:50]:
                print(f"FAIL {item.get('pod')} AS{item.get('asn')} {item.get('failureReason', '')}", file=sys.stderr)
            return EXIT_VERIFY_FAILED
        log(f"verify-after-fib-write passed as_count={len(checks)}")
        return 0
    except Exception as exc:
        summary["failure_reason"] = str(exc)
        summary["duration_seconds"] = round(time.time() - started, 2)
        summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
        print(f"verify-after-fib-write failed: {exc}", file=sys.stderr)
        return EXIT_VERIFY_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
