#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

EXIT_KERNEL_SWITCH_FAILED = 41
ROLE_SET = {"r", "brd", "rs"}
START_DELAY_SECONDS = 0.3


@dataclass
class RouterTarget:
    name: str
    asn: str
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


def router_targets(namespace: str) -> list[RouterTarget]:
    result = kubectl(namespace, ["get", "pods", "-o", "json"], timeout=60)
    result.check_returncode()
    data = json.loads(result.stdout)
    targets: list[RouterTarget] = []
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
            RouterTarget(
                name=str(item["metadata"]["name"]),
                asn=str(labels.get("seedemu.io/asn", "")),
                node=str(item.get("spec", {}).get("nodeName", "")),
            )
        )
    targets.sort(key=lambda pod: (int(pod.asn or 0), pod.name))
    return targets


def kernel_config(export_mode: str) -> str:
    scan_base = int(os.environ.get("SEED_KERNEL_SCAN_BASE_SECONDS", "6000"))
    scan_jitter = int(os.environ.get("SEED_KERNEL_SCAN_JITTER_SECONDS", "120"))
    interval = scan_base + random.randint(0, max(scan_jitter, 0))
    if export_mode == "device_ospf_only":
        return f"""protocol kernel {{
    merge paths on;
    persist;
    scan time {interval};
    ipv4 {{
        import none;
        export filter {{
            if source = RTS_DEVICE then accept;
            if source = RTS_OSPF then accept;
            reject;
        }};
    }};
}}
"""
    return f"""protocol kernel {{
    merge paths on;
    persist;
    scan time {interval};
    ipv4 {{
        import none;
        export all;
    }};
}}
"""


def write_kernel_conf(namespace: str, pod: str, content: str, exec_timeout: int, birdc_timeout: int) -> subprocess.CompletedProcess[str]:
    payload = content.replace("'", "'\"'\"'")
    write_cmd = "cat <<'EOF' > /etc/bird/conf/kernel.conf\n" + payload + "EOF\n"
    result = kubectl_exec(namespace, pod, write_cmd, timeout=exec_timeout)
    if result.returncode != 0:
        return result
    reload_cmd = (
        f"timeout {birdc_timeout} birdc configure >/dev/null 2>&1 && "
        f"timeout {birdc_timeout} birdc 'reload kernel' >/dev/null 2>&1 || true"
    )
    return kubectl_exec(namespace, pod, reload_cmd, timeout=exec_timeout)


def apply_kernel_conf_on_node(
    node_name: str,
    namespace: str,
    targets: list[RouterTarget],
    export_mode: str,
    exec_timeout: int,
    birdc_timeout: int,
    retries: int,
    retry_backoff: float,
) -> tuple[int, list[dict[str, str]]]:
    processed = 0
    failures: list[dict[str, str]] = []
    for idx, target in enumerate(targets, start=1):
        success = False
        last_error = ""
        for attempt in range(1, retries + 1):
            result = write_kernel_conf(namespace, target.name, kernel_config(export_mode), exec_timeout, birdc_timeout)
            if result.returncode == 0:
                success = True
                break
            last_error = (result.stderr or result.stdout).strip() or f"rc={result.returncode}"
            if retry_backoff > 0 and attempt < retries:
                time.sleep(retry_backoff)
        if success:
            processed += 1
        else:
            failures.append({"pod": target.name, "stderr": last_error})
        if idx % 50 == 0 or idx == len(targets):
            log(f"node={node_name} switched={idx}/{len(targets)}")
        time.sleep(START_DELAY_SECONDS)
    return processed, failures


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
    nodes_map: dict[str, list[RouterTarget]],
    exec_timeout: int,
    threshold: float,
    interval_seconds: int,
) -> None:
    probes = {node: pods[0].name for node, pods in nodes_map.items() if pods}
    log(f"waiting for kernel route injection load to drop below {threshold:.1f}")
    while True:
        all_idle = True
        status = []
        for node, pod in probes.items():
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
        print("Usage: start_bird_kernel_helper.py <namespace> <artifact_dir>", file=sys.stderr)
        return 2

    namespace = sys.argv[1]
    artifact_dir = Path(sys.argv[2])
    artifact_dir.mkdir(parents=True, exist_ok=True)

    base_exec_timeout = int(os.environ.get("SEED_KUBECTL_EXEC_TIMEOUT_SECONDS", "30"))
    exec_timeout = int(os.environ.get("SEED_KERNEL_EXEC_TIMEOUT_SECONDS", str(max(base_exec_timeout, 45))))
    birdc_timeout = int(os.environ.get("SEED_KERNEL_BIRDC_TIMEOUT_SECONDS", "10"))
    export_mode = os.environ.get("SEED_KERNEL_EXPORT_MODE", "all").strip().lower() or "all"
    retries = max(1, int(os.environ.get("SEED_KERNEL_SWITCH_RETRIES", "2")))
    retry_backoff = float(os.environ.get("SEED_KERNEL_SWITCH_RETRY_BACKOFF_SECONDS", "1"))
    load_threshold = float(os.environ.get("SEED_KERNEL_LOAD_THRESHOLD", "40"))
    load_check_interval = int(os.environ.get("SEED_KERNEL_LOAD_CHECK_INTERVAL_SECONDS", "20"))
    settle_seconds = int(os.environ.get("SEED_KERNEL_POST_SWITCH_SETTLE_SECONDS", "15"))

    start_time = time.time()
    log(f"=== start bird kernel mode={export_mode} ===")

    targets = router_targets(namespace)
    (artifact_dir / "start_bird_kernel_targets.json").write_text(
        json.dumps([asdict(target) for target in targets], indent=2),
        encoding="utf-8",
    )
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "namespace": namespace,
        "targets": len(targets),
        "kernel_export_mode": export_mode,
        "status": "PASS",
        "failure_reason": "",
        "strategy": "node-aware-concurrency",
    }
    if not targets:
        summary["status"] = "FAIL"
        summary["failure_reason"] = "no_router_pods_found"
        (artifact_dir / "start_bird_kernel_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return EXIT_KERNEL_SWITCH_FAILED

    nodes_map: dict[str, list[RouterTarget]] = defaultdict(list)
    for target in targets:
        nodes_map[target.node].append(target)
    log(f"grouped {len(targets)} targets into {len(nodes_map)} nodes")

    failures: list[dict[str, str]] = []
    processed = 0
    with ThreadPoolExecutor(max_workers=len(nodes_map)) as pool:
        futures = {
            pool.submit(
                apply_kernel_conf_on_node,
                node,
                namespace,
                pods,
                export_mode,
                exec_timeout,
                birdc_timeout,
                retries,
                retry_backoff,
            ): node
            for node, pods in nodes_map.items()
        }
        for future in as_completed(futures):
            try:
                done, node_failures = future.result()
                processed += done
                failures.extend(node_failures)
            except Exception as exc:
                failures.append({"pod": f"node:{futures[future]}", "stderr": str(exc)})

    summary["processed"] = processed
    if failures:
        summary["status"] = "FAIL"
        summary["failure_reason"] = "kernel_switch_failed"
        summary["failures"] = failures
        (artifact_dir / "start_bird_kernel_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return EXIT_KERNEL_SWITCH_FAILED

    log(f"sleeping {settle_seconds}s before load probe")
    time.sleep(settle_seconds)
    wait_for_cluster_idle(namespace, nodes_map, exec_timeout, load_threshold, load_check_interval)

    summary["duration_seconds"] = round(time.time() - start_time, 2)
    (artifact_dir / "start_bird_kernel_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    log(f"completed duration={summary['duration_seconds']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
