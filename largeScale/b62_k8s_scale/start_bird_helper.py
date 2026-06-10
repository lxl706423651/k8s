#!/usr/bin/env python3
"""Start BIRD in router-like pods through kubectl exec.

Inputs: Kubernetes namespace, kubeconfig path, explicit tuning parameters, and
an experiment artifact directory. Outputs: target and summary JSON files in the
artifact directory. Side effects: starts BIRD processes inside running
SeedEMU router/border/route-server pods. Execution model and defaults match
/home/lxl/k8s/lxl/seed_k8s_start_bird0130.py: nodes run concurrently, pods
within each node run serially, then the stage waits for node load and performs
the same final birdc status convergence pass.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

ROLE_SET = {"r", "brd", "rs"}
EXIT_BIRD_START_FAILED = 10
START_DELAY_SECONDS = 0.08
POD_LIST_TIMEOUT_SECONDS = 60
SYSTEM_LOAD_THRESHOLD = 40.0
LOAD_CHECK_INTERVAL_SECONDS = 20
PHASE3_PROGRESS_EVERY = 200
KUBECONFIG_PATH = ""


@dataclass
class PodTarget:
    name: str
    asn: str
    role: str
    node: str


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log(message: str) -> None:
    print(f"[{now_utc()}] {message}", flush=True)


def run(cmd: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        detail = stderr or f"command timed out after {timeout} seconds"
        return subprocess.CompletedProcess(cmd, 124, stdout, detail)


def kubectl(namespace: str, args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    command = ["kubectl"]
    if KUBECONFIG_PATH:
        command.extend(["--kubeconfig", KUBECONFIG_PATH])
    command.extend(["-n", namespace, *args])
    return run(command, timeout=timeout)


def kubectl_exec(namespace: str, pod: str, shell_cmd: str, timeout: int) -> subprocess.CompletedProcess[str]:
    return kubectl(namespace, ["exec", pod, "--", "sh", "-lc", shell_cmd], timeout=timeout)


def write_targets(artifact_dir: Path, targets: list[PodTarget]) -> None:
    """Write B62 and lxl-compatible target artifacts for later inspection."""
    payload = json.dumps([asdict(target) for target in targets], indent=2)
    for filename in ("start_bird_targets.json", "bird0130_targets.json"):
        (artifact_dir / filename).write_text(payload, encoding="utf-8")


def write_summary(artifact_dir: Path, summary: dict) -> None:
    """Write B62 and lxl-compatible summary artifacts."""
    payload = json.dumps(summary, indent=2)
    for filename in ("start_bird_summary.json", "bird0130_summary.json"):
        (artifact_dir / filename).write_text(payload, encoding="utf-8")


def load_cached_targets(cache_path: Path) -> list[PodTarget]:
    """Load previously discovered pod targets from this experiment run."""
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    targets = [
        PodTarget(
            name=str(item["name"]),
            asn=str(item.get("asn", "")),
            role=str(item.get("role", "")),
            node=str(item.get("node", "")),
        )
        for item in data
    ]
    targets.sort(key=lambda pod: (int(pod.asn or 0), pod.name))
    return targets


def ensure_targets(namespace: str, artifact_dir: Path, pod_list_timeout: int) -> list[PodTarget]:
    cache_path = artifact_dir / "start_bird_targets.json"
    result = kubectl(
        namespace,
        ["get", "pods", "-o", "json", f"--request-timeout={pod_list_timeout}s"],
        timeout=pod_list_timeout + 30,
    )
    if result.returncode != 0:
        if cache_path.exists():
            log(f"pod list failed, reusing cached targets from {cache_path}")
            return load_cached_targets(cache_path)
        result.check_returncode()
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        if cache_path.exists():
            log(f"pod list returned invalid JSON, reusing cached targets from {cache_path}")
            return load_cached_targets(cache_path)
        raise
    targets: list[PodTarget] = []
    for item in data.get("items", []):
        labels = (item.get("metadata", {}) or {}).get("labels", {}) or {}
        if labels.get("seedemu.io/workload") != "seedemu":
            continue
        role = str(labels.get("seedemu.io/role", ""))
        if role not in ROLE_SET:
            continue
        if str(item.get("status", {}).get("phase", "")) != "Running":
            continue
        targets.append(
            PodTarget(
                name=str(item["metadata"]["name"]),
                asn=str(labels.get("seedemu.io/asn", "")),
                role=role,
                node=str(item.get("spec", {}).get("nodeName", "")),
            )
        )
    targets.sort(key=lambda pod: (int(pod.asn or 0), pod.name))
    return targets


def bird_running(namespace: str, pod: str, exec_timeout: int) -> bool:
    check_cmd = (
        "pgrep -x bird >/dev/null 2>&1 && "
        "timeout 5 birdc show status >/dev/null 2>&1"
    )
    result = kubectl_exec(namespace, pod, check_cmd, timeout=exec_timeout)
    return result.returncode == 0


def start_bird_once(namespace: str, pod: str, exec_timeout: int) -> subprocess.CompletedProcess[str]:
    cleanup = (
        "rm -f /run/bird/bird.ctl /run/bird/bird.pid /var/run/bird.ctl /var/run/bird.pid "
        "/run/bird/*.ctl /run/bird/*.pid /var/run/bird/*.ctl /var/run/bird/*.pid 2>/dev/null || true"
    )
    shell_cmd = f"""
if pgrep -x bird >/dev/null 2>&1; then
    exit 0
fi
{cleanup}
bird >/tmp/seedemu-bird.log 2>&1 || (bird -d >/tmp/seedemu-bird.log 2>&1 & sleep 1)
"""
    return kubectl_exec(namespace, pod, shell_cmd, timeout=exec_timeout)


def start_birds_on_node(
    node_name: str,
    namespace: str,
    targets: list[PodTarget],
    exec_timeout: int,
    retries: int,
    retry_backoff: float,
    start_delay: float,
) -> tuple[int, list[tuple[str, str]]]:
    failures: list[tuple[str, str]] = []
    started = 0
    for target in targets:
        success = False
        last_error = ""
        for attempt in range(1, retries + 1):
            result = start_bird_once(namespace, target.name, exec_timeout)
            if result.returncode == 0:
                success = True
                break
            last_error = (result.stderr or result.stdout).strip() or f"rc={result.returncode}"
            if retry_backoff > 0 and attempt < retries:
                time.sleep(retry_backoff)
        if success:
            started += 1
        else:
            failures.append((target.name, last_error))
        time.sleep(start_delay)
    log(f"node={node_name} started={started}/{len(targets)}")
    return started, failures


def get_node_load(namespace: str, probe_pod: str, exec_timeout: int) -> float:
    result = kubectl_exec(namespace, probe_pod, "cat /proc/loadavg", timeout=exec_timeout)
    if result.returncode == 0:
        try:
            return float(result.stdout.split()[0])
        except (IndexError, ValueError):
            return -1.0
    return -1.0


def wait_for_cluster_idle(
    namespace: str,
    nodes_map: dict[str, list[PodTarget]],
    exec_timeout: int,
    threshold: float,
    interval_seconds: int,
) -> None:
    probe_pods = {node: pods[0].name for node, pods in nodes_map.items() if pods}
    log(f"waiting for node load average to drop below {threshold:.1f} across {len(probe_pods)} nodes")
    while True:
        all_idle = True
        status = []
        for node, pod in probe_pods.items():
            load = get_node_load(namespace, pod, exec_timeout)
            status.append(f"{node}={load:.2f}")
            if load < 0 or load >= threshold:
                all_idle = False
        log("load_check " + " ".join(status))
        if all_idle:
            return
        time.sleep(interval_seconds)


def collect_node_loads(
    namespace: str,
    nodes_map: dict[str, list[PodTarget]],
    exec_timeout: int,
) -> dict[str, float]:
    """Probe one pod per node and return current node load averages."""
    loads: dict[str, float] = {}
    for node, pods in nodes_map.items():
        if not pods:
            continue
        loads[node] = get_node_load(namespace, pods[0].name, exec_timeout)
    return loads


def parseArgs() -> argparse.Namespace:
    """Parse explicit B62 BIRD-start parameters."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--kubeconfig", default="")
    parser.add_argument("--pod-list-timeout-seconds", type=int, default=POD_LIST_TIMEOUT_SECONDS)
    parser.add_argument("--kubectl-exec-timeout-seconds", type=int, default=30)
    parser.add_argument("--start-exec-timeout-seconds", type=int, default=45)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--retry-backoff-seconds", type=float, default=1.0)
    parser.add_argument("--start-delay-seconds", type=float, default=START_DELAY_SECONDS)
    parser.add_argument("--load-threshold", type=float, default=SYSTEM_LOAD_THRESHOLD)
    parser.add_argument("--load-check-interval-seconds", type=int, default=LOAD_CHECK_INTERVAL_SECONDS)
    parser.add_argument("--post-start-settle-seconds", type=int, default=60)
    parser.add_argument("--phase-timeout-seconds", type=int, default=1200)
    parser.add_argument("--phase3-progress-every", type=int, default=PHASE3_PROGRESS_EVERY)
    return parser.parse_args()


def main() -> int:
    global KUBECONFIG_PATH
    args = parseArgs()
    namespace = args.namespace
    artifact_dir = args.artifact_dir
    artifact_dir.mkdir(parents=True, exist_ok=True)
    KUBECONFIG_PATH = args.kubeconfig

    base_exec_timeout = args.kubectl_exec_timeout_seconds
    exec_timeout = max(base_exec_timeout, args.start_exec_timeout_seconds)
    retries = max(1, args.retries)
    retry_backoff = args.retry_backoff_seconds
    start_delay = args.start_delay_seconds
    load_threshold = args.load_threshold
    load_check_interval = args.load_check_interval_seconds
    settle_seconds = args.post_start_settle_seconds
    phase_timeout = args.phase_timeout_seconds
    phase3_progress_every = max(1, args.phase3_progress_every)

    start_time = time.time()
    log("=== start bird ===")

    targets = ensure_targets(namespace, artifact_dir, args.pod_list_timeout_seconds)
    write_targets(artifact_dir, targets)

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "namespace": namespace,
        "targets": len(targets),
        "status": "PASS",
        "failure_reason": "",
        "strategy": "node-aware-concurrency",
        "parameters": {
            "pod_list_timeout_seconds": args.pod_list_timeout_seconds,
            "kubectl_exec_timeout_seconds": args.kubectl_exec_timeout_seconds,
            "start_exec_timeout_seconds": args.start_exec_timeout_seconds,
            "start_delay_seconds": start_delay,
            "retries": retries,
            "retry_backoff_seconds": retry_backoff,
            "load_threshold": load_threshold,
            "load_check_interval_seconds": load_check_interval,
            "post_start_settle_seconds": settle_seconds,
            "phase_timeout_seconds": phase_timeout,
            "phase3_progress_every": phase3_progress_every,
        },
    }

    if not targets:
        summary["status"] = "FAIL"
        summary["failure_reason"] = "no_router_like_pods_found"
        write_summary(artifact_dir, summary)
        return EXIT_BIRD_START_FAILED

    nodes_map: dict[str, list[PodTarget]] = defaultdict(list)
    for target in targets:
        nodes_map[target.node].append(target)
    log(f"grouped {len(targets)} targets into {len(nodes_map)} nodes")

    failures: list[tuple[str, str]] = []
    started = 0
    with ThreadPoolExecutor(max_workers=len(nodes_map)) as pool:
        futures = {
            pool.submit(
                start_birds_on_node,
                node,
                namespace,
                pods,
                exec_timeout,
                retries,
                retry_backoff,
                start_delay,
            ): node
            for node, pods in nodes_map.items()
        }
        for future in as_completed(futures):
            try:
                node_started, node_failures = future.result()
                started += node_started
                failures.extend(node_failures)
            except Exception as exc:
                failures.append((f"node:{futures[future]}", str(exc)))

    summary["started"] = started
    if failures:
        summary["status"] = "FAIL"
        summary["failure_reason"] = "bird_start_command_failed"
        summary["failures"] = [{"pod": pod, "stderr": detail} for pod, detail in failures]
        write_summary(artifact_dir, summary)
        return EXIT_BIRD_START_FAILED

    log(f"sleeping {settle_seconds}s before load probe")
    time.sleep(settle_seconds)
    wait_for_cluster_idle(namespace, nodes_map, exec_timeout, load_threshold, load_check_interval)
    summary["post_start_loads"] = collect_node_loads(namespace, nodes_map, exec_timeout)

    deadline = time.time() + phase_timeout
    all_healthy = False
    phase3_round = 0
    while time.time() < deadline:
        phase3_round += 1
        log(f"Phase 3 round {phase3_round}: verifying birdc status across {len(targets)} pods")
        pending: list[str] = []
        for idx, target in enumerate(targets, start=1):
            if not bird_running(namespace, target.name, exec_timeout):
                pending.append(target.name)
            if idx % phase3_progress_every == 0 or idx == len(targets):
                log(f"Phase 3 round {phase3_round}: checked {idx}/{len(targets)} pods, pending={len(pending)}")
        if not pending:
            log("Phase 3: all target pods respond to birdc show status")
            all_healthy = True
            break
        log(f"Phase 3: waiting for BIRD to be ready in {len(pending)} pods")
        time.sleep(10)

    if not all_healthy:
        summary["status"] = "FAIL"
        summary["failure_reason"] = "bird_not_started"
        summary["duration_seconds"] = round(time.time() - start_time, 2)
        write_summary(artifact_dir, summary)
        return EXIT_BIRD_START_FAILED

    summary["duration_seconds"] = round(time.time() - start_time, 2)
    write_summary(artifact_dir, summary)
    log(f"completed duration={summary['duration_seconds']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
