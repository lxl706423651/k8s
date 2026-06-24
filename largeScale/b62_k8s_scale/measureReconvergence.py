#!/usr/bin/env python3
"""Measure BGP reconvergence after selected BIRD failures in a B62 run.

Inputs are the active namespace, run artifact directory, cluster inventory,
and kubeconfig. The stage records baseline `ip route` counts on deterministic
verify pods, sends `birdc down` to configured chaos pods, waits for VM load to
settle, and treats the failure side as converged once at least one verify pod
has fewer Linux FIB routes. It then restarts BIRD on the chaos pods and treats
recovery as converged once at least one verify pod has more Linux FIB routes.

Outputs are reconvergence.log, reconvergence_targets.json, and
reconvergence_summary.json in the run directory.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


DEFAULT_CHAOS_TARGETS = [
    "as1277brd-r219-1.219.4.253",
    "as1304brd-r40-1.40.5.24",
    "as1841brd-r17-1.17.7.49",
    "as1488brd-r12-1.12.5.208",
    "as1485brd-r18-1.18.5.205",
    "as1725brd-r21-1.21.6.189",
    "as1113brd-ix1113-5.89.4.89",
    "as1700brd-r13-1.13.6.164",
    "as1797brd-r12-1.12.7.5",
    "as1531brd-r7-1.7.5.251",
]

DEFAULT_VERIFY_TARGETS = [
    "as1277brd-r2-1.2.4.253",
    "as1531brd-r12-1.12.5.251",
    "as1304brd-r12-1.12.5.24",
    "as1841brd-r206-1.206.7.49",
]


@dataclass
class InventoryNode:
    name: str
    host: str
    user: str
    key: str


@dataclass
class PodInfo:
    name: str
    node: str
    status: str
    role: str


def nowUtc() -> str:
    """Return a compact UTC timestamp for logs."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log(message: str) -> None:
    """Print one timestamped line."""
    print(f"[{nowUtc()}] {message}", flush=True)


def runCommand(cmd: list[str], timeout: int | None) -> subprocess.CompletedProcess[str]:
    """Run one local command and convert timeout into a normal result."""
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
    with ThreadPoolExecutor(max_workers=min(32, max(1, len(nodes)))) as pool:
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


def kubectl(
    kubeconfig: str,
    namespace: str,
    args: list[str],
    timeout: int | None,
) -> subprocess.CompletedProcess[str]:
    """Run kubectl against one namespace."""
    return runCommand(
        ["kubectl", "--kubeconfig", kubeconfig, "-n", namespace, *args],
        timeout=timeout,
    )


def kubectlExec(
    kubeconfig: str,
    namespace: str,
    pod: str,
    shell_cmd: str,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    """Run one shell command inside a pod."""
    return kubectl(kubeconfig, namespace, ["exec", pod, "--", "sh", "-lc", shell_cmd], timeout=timeout)


def listSeedemuPods(kubeconfig: str, namespace: str, timeout: int) -> list[PodInfo]:
    """List running SeedEMU workload pods in a namespace."""
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
    data = json.loads(result.stdout)
    pods: list[PodInfo] = []
    for item in data.get("items", []):
        metadata = item.get("metadata", {}) or {}
        labels = metadata.get("labels", {}) or {}
        if labels.get("seedemu.io/workload") != "seedemu":
            continue
        pods.append(
            PodInfo(
                name=str(metadata.get("name", "")),
                node=str((item.get("spec", {}) or {}).get("nodeName", "")),
                status=str((item.get("status", {}) or {}).get("phase", "")),
                role=str(labels.get("seedemu.io/role", "")),
            )
        )
    pods.sort(key=lambda item: item.name)
    return pods


def findPrefixedPod(prefix: str, pods: list[PodInfo]) -> PodInfo | None:
    """Return the first running pod whose generated name starts with prefix."""
    for pod in pods:
        if pod.status == "Running" and pod.name.startswith(prefix):
            return pod
    return None


def routeCount(kubeconfig: str, namespace: str, pod: str, timeout: int) -> tuple[int | None, str]:
    """Return `ip route | wc -l` for one pod."""
    result = kubectlExec(kubeconfig, namespace, pod, "ip route | wc -l", timeout)
    raw = (result.stdout or result.stderr or "").strip()
    if result.returncode != 0:
        return None, raw or f"rc={result.returncode}"
    try:
        return int((result.stdout or "").strip().split()[0]), raw
    except (IndexError, ValueError) as exc:
        return None, f"{exc}: {raw}"


def collectRouteCounts(
    kubeconfig: str,
    namespace: str,
    pods: list[PodInfo],
    exec_timeout: int,
) -> dict[str, dict[str, Any]]:
    """Collect route counts from verify pods in parallel."""
    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(16, max(1, len(pods)))) as pool:
        futures = {
            pool.submit(routeCount, kubeconfig, namespace, pod.name, exec_timeout): pod
            for pod in pods
        }
        for future in as_completed(futures):
            pod = futures[future]
            count, raw = future.result()
            results[pod.name] = {
                "pod": pod.name,
                "node": pod.node,
                "routeCount": count,
                "raw": raw,
            }
    return dict(sorted(results.items()))


def compareCounts(before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Compare route-count snapshots."""
    rows = []
    total_before = 0
    total_after = 0
    comparable = 0
    decreased = 0
    increased = 0
    for pod in sorted(before):
        old = before[pod].get("routeCount")
        new = after.get(pod, {}).get("routeCount")
        diff = None
        if isinstance(old, int) and isinstance(new, int):
            diff = new - old
            total_before += old
            total_after += new
            comparable += 1
            if diff < 0:
                decreased += 1
            elif diff > 0:
                increased += 1
        rows.append({"pod": pod, "before": old, "after": new, "diff": diff})
    return {
        "totalBefore": total_before,
        "totalAfter": total_after,
        "totalDiff": total_after - total_before if comparable else None,
        "comparable": comparable,
        "decreasedPods": decreased,
        "increasedPods": increased,
        "rows": rows,
    }


def sendBirdDown(kubeconfig: str, namespace: str, pod: PodInfo, timeout: int) -> subprocess.CompletedProcess[str]:
    """Send `birdc down` to one chaos pod."""
    return kubectlExec(kubeconfig, namespace, pod.name, "timeout 10 birdc down", timeout)


def sendBirdUp(kubeconfig: str, namespace: str, pod: PodInfo, timeout: int) -> subprocess.CompletedProcess[str]:
    """Start or reconfigure BIRD in one chaos pod after fault injection."""
    shell_cmd = r"""
set -eu
if pgrep -x bird >/dev/null 2>&1; then
    timeout 10 birdc configure >/dev/null 2>&1 || true
else
    rm -f /run/bird/bird.ctl /run/bird/bird.pid /var/run/bird.ctl /var/run/bird.pid \
          /run/bird/*.ctl /run/bird/*.pid /var/run/bird/*.ctl /var/run/bird/*.pid 2>/dev/null || true
    bird >/tmp/seedemu-bird-recovery.log 2>&1 || (bird -d >/tmp/seedemu-bird-recovery.log 2>&1 & sleep 1)
fi
timeout 10 birdc show status >/dev/null
"""
    return kubectlExec(kubeconfig, namespace, pod.name, shell_cmd, timeout)


def sendCommands(
    action_name: str,
    pods: list[PodInfo],
    func: Any,
    kubeconfig: str,
    namespace: str,
    exec_timeout: int,
) -> tuple[float, dict[str, str]]:
    """Run one action on all chaos pods sequentially and return errors."""
    started = time.time()
    errors: dict[str, str] = {}
    for pod in pods:
        result = func(kubeconfig, namespace, pod, exec_timeout)
        if result.returncode != 0:
            errors[pod.name] = (result.stderr or result.stdout or f"rc={result.returncode}").strip()
            log(f"{action_name} failed pod={pod.name}: {errors[pod.name]}")
        else:
            log(f"{action_name} ok pod={pod.name}")
    return round(time.time() - started, 2), errors


def waitForRouteChange(
    *,
    phase: str,
    direction: str,
    reference: dict[str, dict[str, Any]],
    nodes: dict[str, InventoryNode],
    verify_pods: list[PodInfo],
    kubeconfig: str,
    namespace: str,
    load_threshold: float,
    load_interval_seconds: int,
    route_check_interval_seconds: int,
    timeout_seconds: int,
    exec_timeout: int,
) -> tuple[float, dict[str, Any], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Wait for settled load and then a route-count decrease or increase."""
    started = time.time()
    attempts = 0
    last_loads: dict[str, dict[str, Any]] = {}
    last_counts: dict[str, dict[str, Any]] = {}
    last_comparison: dict[str, Any] = {}
    while True:
        elapsed = int(time.time() - started)
        if timeout_seconds > 0 and elapsed >= timeout_seconds:
            raise TimeoutError(f"{phase}: no {direction} route-count change within {timeout_seconds}s")
        remaining = max(1, timeout_seconds - elapsed) if timeout_seconds > 0 else 0
        _, _, last_loads = waitForClusterLoad(nodes, load_threshold, load_interval_seconds, remaining)
        attempts += 1
        last_counts = collectRouteCounts(kubeconfig, namespace, verify_pods, exec_timeout)
        last_comparison = compareCounts(reference, last_counts)
        if direction == "decrease":
            changed = last_comparison["decreasedPods"] > 0 or (
                isinstance(last_comparison.get("totalDiff"), int) and last_comparison["totalDiff"] < 0
            )
        else:
            changed = last_comparison["increasedPods"] > 0 or (
                isinstance(last_comparison.get("totalDiff"), int) and last_comparison["totalDiff"] > 0
            )
        log(
            f"{phase} attempt={attempts} total_diff={last_comparison.get('totalDiff')} "
            f"decreased={last_comparison['decreasedPods']} increased={last_comparison['increasedPods']}"
        )
        if changed:
            return round(time.time() - started, 2), last_comparison, last_counts, last_loads
        time.sleep(max(1, route_check_interval_seconds))


def writeJson(path: Path, payload: dict[str, Any] | list[dict[str, Any]]) -> None:
    """Write formatted JSON."""
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def parseArgs(argv: list[str]) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--artifact-dir", required=True, type=Path)
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--load-threshold", type=float, default=80)
    parser.add_argument("--load-check-interval-seconds", type=int, default=15)
    parser.add_argument("--route-check-interval-seconds", type=int, default=10)
    parser.add_argument("--phase-timeout-seconds", type=int, default=1800)
    parser.add_argument("--exec-timeout-seconds", type=int, default=45)
    parser.add_argument("--list-timeout-seconds", type=int, default=300)
    parser.add_argument("--chaos-target-prefix", action="append", default=[])
    parser.add_argument("--verify-target-prefix", action="append", default=[])
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    """CLI entrypoint."""
    args = parseArgs(argv)
    args.artifact_dir.mkdir(parents=True, exist_ok=True)
    summary_path = args.artifact_dir / "reconvergence_summary.json"
    targets_path = args.artifact_dir / "reconvergence_targets.json"
    summary: dict[str, Any] = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "namespace": args.namespace,
        "status": "RUNNING",
        "loadThreshold": args.load_threshold,
        "loadCheckIntervalSeconds": args.load_check_interval_seconds,
        "routeCheckIntervalSeconds": args.route_check_interval_seconds,
        "phaseTimeoutSeconds": args.phase_timeout_seconds,
    }
    started = time.time()
    try:
        nodes = loadInventory(args.inventory)
        pods = listSeedemuPods(args.kubeconfig, args.namespace, args.list_timeout_seconds)
        chaos_prefixes = args.chaos_target_prefix or DEFAULT_CHAOS_TARGETS
        verify_prefixes = args.verify_target_prefix or DEFAULT_VERIFY_TARGETS
        chaos_pods = [findPrefixedPod(prefix, pods) for prefix in chaos_prefixes]
        verify_pods = [findPrefixedPod(prefix, pods) for prefix in verify_prefixes]
        missing_chaos = [prefix for prefix, pod in zip(chaos_prefixes, chaos_pods) if pod is None]
        missing_verify = [prefix for prefix, pod in zip(verify_prefixes, verify_pods) if pod is None]
        chaos = [pod for pod in chaos_pods if pod is not None]
        verify = [pod for pod in verify_pods if pod is not None]
        targets_payload = {
            "chaosTargetPrefixes": chaos_prefixes,
            "verifyTargetPrefixes": verify_prefixes,
            "missingChaosTargets": missing_chaos,
            "missingVerifyTargets": missing_verify,
            "matchedChaosPods": [asdict(pod) for pod in chaos],
            "matchedVerifyPods": [asdict(pod) for pod in verify],
        }
        writeJson(targets_path, targets_payload)
        summary.update(targets_payload)
        if missing_chaos or missing_verify or not chaos or not verify:
            summary["status"] = "FAIL"
            summary["failureReason"] = "target_resolution_failed"
            summary["totalDurationSeconds"] = round(time.time() - started, 2)
            writeJson(summary_path, summary)
            return 1

        log(f"matched chaos pods={len(chaos)} verify pods={len(verify)}")
        _, baseline_load_wait, baseline_loads = waitForClusterLoad(
            nodes, args.load_threshold, args.load_check_interval_seconds, args.phase_timeout_seconds
        )
        baseline_counts = collectRouteCounts(args.kubeconfig, args.namespace, verify, args.exec_timeout_seconds)
        summary["baselineLoadWaitSeconds"] = baseline_load_wait
        summary["baselineLoads"] = baseline_loads
        summary["baselineRoutes"] = baseline_counts
        log("baseline route counts: " + ", ".join(f"{name}={item['routeCount']}" for name, item in baseline_counts.items()))

        fault_injection_seconds, fault_errors = sendCommands(
            "birdc down", chaos, sendBirdDown, args.kubeconfig, args.namespace, args.exec_timeout_seconds
        )
        summary["faultInjectionSeconds"] = fault_injection_seconds
        if fault_errors:
            summary["status"] = "FAIL"
            summary["failureReason"] = "fault_injection_failed"
            summary["faultErrors"] = fault_errors
            summary["totalDurationSeconds"] = round(time.time() - started, 2)
            writeJson(summary_path, summary)
            return 1

        failure_seconds, failure_comparison, failure_counts, failure_loads = waitForRouteChange(
            phase="failure convergence",
            direction="decrease",
            reference=baseline_counts,
            nodes=nodes,
            verify_pods=verify,
            kubeconfig=args.kubeconfig,
            namespace=args.namespace,
            load_threshold=args.load_threshold,
            load_interval_seconds=args.load_check_interval_seconds,
            route_check_interval_seconds=args.route_check_interval_seconds,
            timeout_seconds=args.phase_timeout_seconds,
            exec_timeout=args.exec_timeout_seconds,
        )
        summary["failureConvergenceSeconds"] = failure_seconds
        summary["failureRoutes"] = failure_counts
        summary["failureRouteDiff"] = failure_comparison
        summary["failureLoads"] = failure_loads

        recovery_injection_seconds, recovery_errors = sendCommands(
            "bird recovery", chaos, sendBirdUp, args.kubeconfig, args.namespace, args.exec_timeout_seconds
        )
        summary["recoveryInjectionSeconds"] = recovery_injection_seconds
        if recovery_errors:
            summary["status"] = "FAIL"
            summary["failureReason"] = "recovery_injection_failed"
            summary["recoveryErrors"] = recovery_errors
            summary["totalDurationSeconds"] = round(time.time() - started, 2)
            writeJson(summary_path, summary)
            return 1

        recovery_seconds, recovery_comparison, recovery_counts, recovery_loads = waitForRouteChange(
            phase="recovery convergence",
            direction="increase",
            reference=failure_counts,
            nodes=nodes,
            verify_pods=verify,
            kubeconfig=args.kubeconfig,
            namespace=args.namespace,
            load_threshold=args.load_threshold,
            load_interval_seconds=args.load_check_interval_seconds,
            route_check_interval_seconds=args.route_check_interval_seconds,
            timeout_seconds=args.phase_timeout_seconds,
            exec_timeout=args.exec_timeout_seconds,
        )
        summary["recoveryConvergenceSeconds"] = recovery_seconds
        summary["recoveryRoutes"] = recovery_counts
        summary["recoveryRouteDiff"] = recovery_comparison
        summary["recoveryLoads"] = recovery_loads
        summary["status"] = "PASS"
        summary["totalDurationSeconds"] = round(time.time() - started, 2)
        writeJson(summary_path, summary)
        log(
            f"reconvergence passed failure={failure_seconds}s recovery={recovery_seconds}s "
            f"total={summary['totalDurationSeconds']}s"
        )
        return 0
    except (subprocess.CalledProcessError, TimeoutError, RuntimeError) as exc:
        summary["status"] = "FAIL"
        summary["failureReason"] = type(exc).__name__
        summary["error"] = str(exc)
        summary["totalDurationSeconds"] = round(time.time() - started, 2)
        writeJson(summary_path, summary)
        log(f"reconvergence failed: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
