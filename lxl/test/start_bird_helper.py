#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

ROLE_SET = {"r", "brd", "rs"}
EXIT_BIRD_START_FAILED = 10
START_DELAY_SECONDS = 0.08
PHASE3_PROGRESS_EVERY = 200


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
    return run(["kubectl", "-n", namespace, *args], timeout=timeout)


def kubectl_exec(namespace: str, pod: str, shell_cmd: str, timeout: int) -> subprocess.CompletedProcess[str]:
    return kubectl(namespace, ["exec", pod, "--", "sh", "-lc", shell_cmd], timeout=timeout)


def ensure_targets(namespace: str) -> list[PodTarget]:
    result = kubectl(namespace, ["get", "pods", "-o", "json"], timeout=60)
    result.check_returncode()
    data = json.loads(result.stdout)
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
    return kubectl_exec(namespace, pod, check_cmd, timeout=exec_timeout).returncode == 0


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
        time.sleep(START_DELAY_SECONDS)
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


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: start_bird_helper.py <namespace> <artifact_dir>", file=sys.stderr)
        return 2

    namespace = sys.argv[1]
    artifact_dir = Path(sys.argv[2])
    artifact_dir.mkdir(parents=True, exist_ok=True)

    base_exec_timeout = int(os.environ.get("SEED_KUBECTL_EXEC_TIMEOUT_SECONDS", "30"))
    exec_timeout = int(os.environ.get("SEED_BIRD_START_EXEC_TIMEOUT_SECONDS", str(max(base_exec_timeout, 45))))
    phase_timeout = int(os.environ.get("SEED_BIRD_PHASE_TIMEOUT_SECONDS", "1200"))
    retries = max(1, int(os.environ.get("SEED_BIRD_START_RETRIES", "2")))
    retry_backoff = float(os.environ.get("SEED_BIRD_START_RETRY_BACKOFF_SECONDS", "1"))
    load_threshold = float(os.environ.get("SEED_BIRD_LOAD_THRESHOLD", "40"))
    load_check_interval = int(os.environ.get("SEED_BIRD_LOAD_CHECK_INTERVAL_SECONDS", "20"))
    settle_seconds = int(os.environ.get("SEED_BIRD_POST_START_SETTLE_SECONDS", "60"))

    start_time = time.time()
    log("=== start bird ===")

    targets = ensure_targets(namespace)
    (artifact_dir / "start_bird_targets.json").write_text(
        json.dumps([asdict(target) for target in targets], indent=2),
        encoding="utf-8",
    )

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "namespace": namespace,
        "targets": len(targets),
        "status": "PASS",
        "failure_reason": "",
        "strategy": "node-aware-concurrency",
    }

    if not targets:
        summary["status"] = "FAIL"
        summary["failure_reason"] = "no_router_like_pods_found"
        (artifact_dir / "start_bird_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return EXIT_BIRD_START_FAILED

    nodes_map: dict[str, list[PodTarget]] = defaultdict(list)
    for target in targets:
        nodes_map[target.node].append(target)
    log(f"grouped {len(targets)} targets into {len(nodes_map)} nodes")

    failures: list[tuple[str, str]] = []
    started = 0
    with ThreadPoolExecutor(max_workers=len(nodes_map)) as pool:
        futures = {
            pool.submit(start_birds_on_node, node, namespace, pods, exec_timeout, retries, retry_backoff): node
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
        (artifact_dir / "start_bird_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return EXIT_BIRD_START_FAILED

    log(f"sleeping {settle_seconds}s before load probe")
    time.sleep(settle_seconds)
    wait_for_cluster_idle(namespace, nodes_map, exec_timeout, load_threshold, load_check_interval)

    deadline = time.time() + phase_timeout
    round_id = 0
    while time.time() < deadline:
        round_id += 1
        pending: list[str] = []
        for idx, target in enumerate(targets, start=1):
            if not bird_running(namespace, target.name, exec_timeout):
                pending.append(target.name)
            if idx % PHASE3_PROGRESS_EVERY == 0 or idx == len(targets):
                log(f"verify_round={round_id} checked={idx}/{len(targets)} pending={len(pending)}")
        if not pending:
            summary["duration_seconds"] = round(time.time() - start_time, 2)
            (artifact_dir / "start_bird_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
            log(f"completed duration={summary['duration_seconds']}s")
            return 0
        log(f"waiting for bird readiness in {len(pending)} pods")
        time.sleep(10)

    summary["status"] = "FAIL"
    summary["failure_reason"] = "bird_not_started"
    summary["duration_seconds"] = round(time.time() - start_time, 2)
    (artifact_dir / "start_bird_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return EXIT_BIRD_START_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
