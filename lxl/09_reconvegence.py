#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from _seed_runtime_9node import bootstrap

bootstrap()

# ================= 配置区 =================
DEFAULT_NAMESPACE = "seedemu-k3s-real-topo"

# 阶段一：执行 birdc down 的目标容器前缀
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

# 阶段二：验证容器前缀
DEFAULT_VERIFY_TARGETS = [
    "as1277brd-r2-1.2.4.253",
    "as1531brd-r12-1.12.5.251",
    "as1304brd-r12-1.12.5.24",
    "as1841brd-r206-1.206.7.49",
]

# 参考现有 bird/kernal 脚本保持一致
LOAD_THRESHOLD = float(os.environ.get("SEED_RECONVERGENCE_LOAD_THRESHOLD", "40"))
LOAD_CHECK_INTERVAL = int(os.environ.get("SEED_RECONVERGENCE_CHECK_INTERVAL_SECONDS", "15"))
PHASE_TIMEOUT = int(os.environ.get("SEED_RECONVERGENCE_PHASE_TIMEOUT_SECONDS", "1800"))
EXEC_TIMEOUT = int(os.environ.get("SEED_RECONVERGENCE_EXEC_TIMEOUT_SECONDS", "45"))
VERIFY_PROGRESS_EVERY = int(os.environ.get("SEED_RECONVERGENCE_VERIFY_PROGRESS_EVERY", "2"))
AUTO_RECOVER = os.environ.get("SEED_RECONVERGENCE_AUTO_RECOVER", "1").strip().lower() not in {"0", "false", "no"}
# ==========================================

try:
    sys.stdout.reconfigure(line_buffering=True, write_through=True)
    sys.stderr.reconfigure(line_buffering=True, write_through=True)
except Exception:
    pass


@dataclass
class PodInfo:
    name: str
    node: str
    status: str


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(log_path: Path, message: str) -> None:
    stamped = f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}] {message}"
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(stamped + "\n")
        handle.flush()
    print(stamped, flush=True)


def run(cmd: List[str], *, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return subprocess.CompletedProcess(cmd, 124, stdout, stderr or f"command timed out after {timeout}s")


def kubectl(namespace: str, args: List[str], *, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return run(["kubectl", "-n", namespace, *args], timeout=timeout)


def kubectl_exec(namespace: str, pod: str, shell_cmd: str, *, timeout: int) -> subprocess.CompletedProcess[str]:
    return kubectl(namespace, ["exec", pod, "--", "sh", "-lc", shell_cmd], timeout=timeout)


def load_targets_from_env(var_name: str, default: List[str]) -> List[str]:
    raw = os.environ.get(var_name, "").strip()
    if not raw:
        return list(default)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{var_name} must be a JSON array: {exc}")
    if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
        raise SystemExit(f"{var_name} must be a JSON array of strings")
    return data


def list_seedemu_pods(namespace: str) -> List[PodInfo]:
    result = kubectl(namespace, ["get", "pods", "-o", "json"], timeout=60)
    result.check_returncode()
    data = json.loads(result.stdout)
    pods: List[PodInfo] = []
    for item in data.get("items", []):
        labels = (item.get("metadata", {}) or {}).get("labels", {}) or {}
        if labels.get("seedemu.io/workload") != "seedemu":
            continue
        pods.append(
            PodInfo(
                name=str(item["metadata"]["name"]),
                node=str(item.get("spec", {}).get("nodeName", "")),
                status=str(item.get("status", {}).get("phase", "")),
            )
        )
    pods.sort(key=lambda pod: pod.name)
    return pods


def find_prefixed_pod(prefix: str, pods: List[PodInfo]) -> PodInfo | None:
    for pod in pods:
        if pod.name.startswith(prefix):
            return pod
    return None


def build_node_probes(pods: List[PodInfo]) -> Dict[str, str]:
    probes: Dict[str, str] = {}
    for pod in pods:
        if pod.status == "Running" and pod.node and pod.node not in probes:
            probes[pod.node] = pod.name
    return probes


def route_count(namespace: str, pod: str) -> int | None:
    result = kubectl_exec(namespace, pod, "ip route | wc -l", timeout=EXEC_TIMEOUT)
    if result.returncode != 0:
        return None
    try:
        return int((result.stdout or "").strip())
    except ValueError:
        return None


def bird_is_running(namespace: str, pod: str) -> bool:
    result = kubectl_exec(
        namespace,
        pod,
        "pgrep -x bird >/dev/null 2>&1 && timeout 5 birdc show status >/dev/null 2>&1",
        timeout=EXEC_TIMEOUT,
    )
    return result.returncode == 0


def bird_down(namespace: str, pod: str) -> subprocess.CompletedProcess[str]:
    return kubectl_exec(namespace, pod, "timeout 10 birdc down", timeout=EXEC_TIMEOUT)


def bird_up(namespace: str, pod: str) -> subprocess.CompletedProcess[str]:
    shell_cmd = """
if pgrep -x bird >/dev/null 2>&1; then
    timeout 10 birdc configure >/dev/null 2>&1 || true
    exit 0
fi
rm -f /run/bird/bird.ctl /run/bird/bird.pid /var/run/bird.ctl /var/run/bird.pid \
      /run/bird/*.ctl /run/bird/*.pid /var/run/bird/*.ctl /var/run/bird/*.pid 2>/dev/null || true
bird >/tmp/seedemu-bird.log 2>&1 || (bird -d >/tmp/seedemu-bird.log 2>&1 & sleep 1)
"""
    return kubectl_exec(namespace, pod, shell_cmd, timeout=EXEC_TIMEOUT)


def get_node_load(namespace: str, pod_name: str) -> float | None:
    result = kubectl_exec(namespace, pod_name, "cat /proc/loadavg", timeout=EXEC_TIMEOUT)
    if result.returncode != 0:
        return None
    try:
        return float(result.stdout.split()[0])
    except (IndexError, ValueError):
        return None


def wait_for_cluster_idle(namespace: str, node_to_probe: Dict[str, str], log_path: Path, phase_name: str) -> Dict[str, float]:
    deadline = time.time() + PHASE_TIMEOUT
    log(log_path, f"⏳ {phase_name}: waiting for all node load averages to fall below {LOAD_THRESHOLD}")
    last_loads: Dict[str, float] = {}
    round_id = 0
    while time.time() < deadline:
        round_id += 1
        all_clear = True
        status_lines = []
        for node, probe_pod in node_to_probe.items():
            load = get_node_load(namespace, probe_pod)
            if load is None:
                status_lines.append(f"{node}: ERR")
                all_clear = False
                continue
            last_loads[node] = load
            status_lines.append(f"{node}: {load:.2f}")
            if load >= LOAD_THRESHOLD:
                all_clear = False

        log(log_path, f"📈 {phase_name} round {round_id}: " + " | ".join(status_lines))
        if all_clear:
            log(log_path, f"✅ {phase_name}: all nodes stabilized below {LOAD_THRESHOLD}")
            return last_loads
        time.sleep(LOAD_CHECK_INTERVAL)

    raise TimeoutError(f"{phase_name}: timeout waiting for node load to drop below {LOAD_THRESHOLD}")


def wait_for_bird_state(namespace: str, pods: List[str], expected_running: bool, log_path: Path, phase_name: str) -> None:
    deadline = time.time() + PHASE_TIMEOUT
    round_id = 0
    while time.time() < deadline:
        round_id += 1
        mismatched: List[str] = []
        for pod in pods:
            running = bird_is_running(namespace, pod)
            if running != expected_running:
                mismatched.append(pod)
        if not mismatched:
            log(log_path, f"✅ {phase_name}: all {len(pods)} target pods reached expected bird state={expected_running}")
            return
        log(log_path, f"⏳ {phase_name} round {round_id}: pending {len(mismatched)}/{len(pods)} pods")
        time.sleep(5)
    raise TimeoutError(f"{phase_name}: timeout waiting for expected bird state={expected_running}")


def collect_route_counts(namespace: str, verify_pods: List[str], log_path: Path, phase_name: str) -> Dict[str, int | None]:
    result: Dict[str, int | None] = {}
    total = len(verify_pods)
    for idx, pod in enumerate(verify_pods, start=1):
        result[pod] = route_count(namespace, pod)
        if idx % VERIFY_PROGRESS_EVERY == 0 or idx == total:
            log(log_path, f"🔎 {phase_name}: collected route counts for {idx}/{total} verify pods")
    return result


def serialize_route_diff(before: Dict[str, int | None], after: Dict[str, int | None]) -> List[dict]:
    rows = []
    for pod in sorted(before):
        old = before.get(pod)
        new = after.get(pod)
        diff = None if old is None or new is None else new - old
        rows.append({"pod": pod, "before": old, "after": new, "diff": diff})
    return rows


def main() -> int:
    namespace = sys.argv[1] if len(sys.argv) >= 2 else os.environ.get("SEED_NAMESPACE", DEFAULT_NAMESPACE)
    artifact_dir = Path(sys.argv[2]) if len(sys.argv) >= 3 else Path(os.environ.get("EXPERIMENT_DIR", "."))
    artifact_dir.mkdir(parents=True, exist_ok=True)

    chaos_targets = load_targets_from_env("SEED_RECONVERGENCE_CHAOS_TARGETS", DEFAULT_CHAOS_TARGETS)
    verify_targets = load_targets_from_env("SEED_RECONVERGENCE_VERIFY_TARGETS", DEFAULT_VERIFY_TARGETS)

    log_path = artifact_dir / "reconvegence.log"
    summary_path = artifact_dir / "reconvegence_summary.json"

    summary: dict = {
        "generated_at": now_utc(),
        "namespace": namespace,
        "auto_recover": AUTO_RECOVER,
        "load_threshold": LOAD_THRESHOLD,
        "check_interval_seconds": LOAD_CHECK_INTERVAL,
        "phase_timeout_seconds": PHASE_TIMEOUT,
        "chaos_target_prefixes": chaos_targets,
        "verify_target_prefixes": verify_targets,
        "status": "PASS",
        "failure_reason": "",
    }

    start_ts = time.time()
    log(log_path, f"=== Reconvergence Experiment Start ({namespace}) ===")
    log(log_path, f"Config: chaos={len(chaos_targets)}, verify={len(verify_targets)}, auto_recover={AUTO_RECOVER}")

    try:
        pods = list_seedemu_pods(namespace)
        if not pods:
            summary["status"] = "FAIL"
            summary["failure_reason"] = "no_seedemu_pods_found"
            summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
            return 1

        node_to_probe = build_node_probes(pods)
        matched_chaos: List[PodInfo] = []
        matched_verify: List[PodInfo] = []

        for prefix in chaos_targets:
            pod = find_prefixed_pod(prefix, pods)
            if pod is None:
                log(log_path, f"⚠️ chaos target missing: {prefix}")
                continue
            matched_chaos.append(pod)

        for prefix in verify_targets:
            pod = find_prefixed_pod(prefix, pods)
            if pod is None:
                log(log_path, f"⚠️ verify target missing: {prefix}")
                continue
            matched_verify.append(pod)

        if not matched_chaos:
            summary["status"] = "FAIL"
            summary["failure_reason"] = "no_valid_chaos_targets"
            summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
            return 1
        if not matched_verify:
            summary["status"] = "FAIL"
            summary["failure_reason"] = "no_valid_verify_targets"
            summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
            return 1

        summary["matched_chaos_pods"] = [asdict(pod) for pod in matched_chaos]
        summary["matched_verify_pods"] = [asdict(pod) for pod in matched_verify]
        log(log_path, f"Matched chaos pods: {len(matched_chaos)}; verify pods: {len(matched_verify)}")

        baseline = collect_route_counts(namespace, [pod.name for pod in matched_verify], log_path, "baseline")
        summary["baseline_routes"] = baseline

        log(log_path, "💥 Injecting failures with birdc down ...")
        inject_errors: Dict[str, str] = {}
        for pod in matched_chaos:
            result = bird_down(namespace, pod.name)
            if result.returncode != 0:
                inject_errors[pod.name] = (result.stderr or result.stdout).strip() or f"rc={result.returncode}"
                log(log_path, f"⚠️ birdc down failed on {pod.name}: {inject_errors[pod.name]}")
            else:
                log(log_path, f"🗡️ birdc down sent to {pod.name}")

        if inject_errors:
            summary["status"] = "FAIL"
            summary["failure_reason"] = "bird_down_failed"
            summary["inject_errors"] = inject_errors
            summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
            return 1

        down_start = time.time()
        wait_for_bird_state(namespace, [pod.name for pod in matched_chaos], expected_running=False, log_path=log_path, phase_name="fault injection verification")
        loads_after_failure = wait_for_cluster_idle(namespace, node_to_probe, log_path, "post-failure convergence")
        after_failure = collect_route_counts(namespace, [pod.name for pod in matched_verify], log_path, "post-failure route snapshot")

        summary["post_failure_convergence_seconds"] = round(time.time() - down_start, 2)
        summary["post_failure_loads"] = loads_after_failure
        summary["post_failure_routes"] = after_failure
        summary["post_failure_route_diff"] = serialize_route_diff(baseline, after_failure)

        if AUTO_RECOVER:
            log(log_path, "🔧 Starting recovery by bringing BIRD back on faulted pods ...")
            recover_errors: Dict[str, str] = {}
            for pod in matched_chaos:
                result = bird_up(namespace, pod.name)
                if result.returncode != 0:
                    recover_errors[pod.name] = (result.stderr or result.stdout).strip() or f"rc={result.returncode}"
                    log(log_path, f"⚠️ bird up failed on {pod.name}: {recover_errors[pod.name]}")
                else:
                    log(log_path, f"🔄 recovery command sent to {pod.name}")

            if recover_errors:
                summary["status"] = "FAIL"
                summary["failure_reason"] = "bird_recovery_failed"
                summary["recovery_errors"] = recover_errors
                summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
                return 1

            recover_start = time.time()
            wait_for_bird_state(namespace, [pod.name for pod in matched_chaos], expected_running=True, log_path=log_path, phase_name="recovery verification")
            loads_after_recovery = wait_for_cluster_idle(namespace, node_to_probe, log_path, "post-recovery convergence")
            after_recovery = collect_route_counts(namespace, [pod.name for pod in matched_verify], log_path, "post-recovery route snapshot")

            summary["recovery_convergence_seconds"] = round(time.time() - recover_start, 2)
            summary["post_recovery_loads"] = loads_after_recovery
            summary["post_recovery_routes"] = after_recovery
            summary["post_recovery_route_diff"] = serialize_route_diff(baseline, after_recovery)

        summary["total_duration_seconds"] = round(time.time() - start_ts, 2)
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        log(log_path, f"🎉 Reconvergence experiment finished in {summary['total_duration_seconds']}s")
        return 0
    except subprocess.CalledProcessError as exc:
        summary["status"] = "FAIL"
        summary["failure_reason"] = "kubectl_command_failed"
        summary["error"] = {
            "returncode": exc.returncode,
            "cmd": exc.cmd,
            "stdout": exc.stdout,
            "stderr": exc.stderr,
        }
        summary["total_duration_seconds"] = round(time.time() - start_ts, 2)
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        log(log_path, f"❌ kubectl command failed: rc={exc.returncode}")
        return 1
    except TimeoutError as exc:
        summary["status"] = "FAIL"
        summary["failure_reason"] = "phase_timeout"
        summary["error"] = str(exc)
        summary["total_duration_seconds"] = round(time.time() - start_ts, 2)
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        log(log_path, f"❌ timeout: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
