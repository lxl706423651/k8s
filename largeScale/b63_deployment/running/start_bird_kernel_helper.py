#!/usr/bin/env python3
"""Enable BIRD kernel export for router-like pods in a deployed SeedEMU namespace.

Inputs are the Kubernetes namespace and experiment artifact directory. The script
writes per-pod kernel protocol config, reloads BIRD, records JSON summaries, and
modifies only running SeedEMU router/border/route-server pods.
"""
from __future__ import annotations

import argparse
import json
import random
import subprocess
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

EXIT_KERNEL_SWITCH_FAILED = 41
ROLE_SET = {"r", "brd", "rs"}
START_DELAY_SECONDS = 0.3
KUBECONFIG_PATH = ""


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
    command = ["kubectl"]
    if KUBECONFIG_PATH:
        command.extend(["--kubeconfig", KUBECONFIG_PATH])
    command.extend(["-n", namespace, *args])
    return run(command, timeout=timeout)


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


def kernel_config(export_mode: str, scan_base: int, scan_jitter: int) -> str:
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
    # Some IX border-router configs do not include /etc/bird/conf/*.conf and do
    # not have a kernel protocol. Add the include only for pods that need it.
    write_and_reload_cmd = f"""set -eu
mkdir -p /etc/bird/conf
if grep -F 'include "/etc/bird/conf/*.conf";' /etc/bird/bird.conf >/dev/null 2>&1; then
    sed -i '\\#^include "/etc/bird/conf/kernel.conf";$#d' /etc/bird/bird.conf
fi
cat <<'EOF' > /etc/bird/conf/kernel.conf
{content}EOF
if ! grep -F 'include "/etc/bird/conf/*.conf";' /etc/bird/bird.conf >/dev/null 2>&1 &&
   ! grep -F 'include "/etc/bird/conf/kernel.conf";' /etc/bird/bird.conf >/dev/null 2>&1; then
    grep -F 'include "/etc/bird/conf/kernel.conf";' /etc/bird/bird.conf >/dev/null 2>&1 || printf '\\ninclude "/etc/bird/conf/kernel.conf";\\n' >> /etc/bird/bird.conf
fi
timeout {birdc_timeout} birdc configure
timeout {birdc_timeout} birdc show protocols | awk 'NR>2 && $2=="Kernel" && $4=="up" {{ok=1}} END{{exit ok?0:1}}'
"""
    return kubectl_exec(namespace, pod, write_and_reload_cmd, timeout=exec_timeout)


def apply_kernel_conf_on_node(
    node_name: str,
    namespace: str,
    targets: list[RouterTarget],
    export_mode: str,
    exec_timeout: int,
    birdc_timeout: int,
    scan_base: int,
    scan_jitter: int,
    retries: int,
    retry_backoff: float,
) -> tuple[int, list[dict[str, str]]]:
    processed = 0
    failures: list[dict[str, str]] = []
    for idx, target in enumerate(targets, start=1):
        success = False
        last_error = ""
        for attempt in range(1, retries + 1):
            result = write_kernel_conf(
                namespace,
                target.name,
                kernel_config(export_mode, scan_base, scan_jitter),
                exec_timeout,
                birdc_timeout,
            )
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
    timeout_seconds: int,
) -> bool:
    probes = {node: pods[0].name for node, pods in nodes_map.items() if pods}
    log(f"waiting for kernel route injection load to drop below {threshold:.1f}")
    started = time.monotonic()
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
            return False
        if timeout_seconds > 0 and time.monotonic() - started >= timeout_seconds:
            log(f"load_wait_timeout after {timeout_seconds}s; continuing")
            return True
        time.sleep(interval_seconds)


def parseArgs() -> argparse.Namespace:
    """Parse explicit b63 kernel-start parameters."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--kubeconfig", default="")
    parser.add_argument("--kubectl-exec-timeout-seconds", type=int, default=30)
    parser.add_argument("--kernel-exec-timeout-seconds", type=int, default=45)
    parser.add_argument("--birdc-timeout-seconds", type=int, default=10)
    parser.add_argument("--export-mode", default="all")
    parser.add_argument("--scan-base-seconds", type=int, default=6000)
    parser.add_argument("--scan-jitter-seconds", type=int, default=120)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--retry-backoff-seconds", type=float, default=1.0)
    parser.add_argument("--load-threshold", type=float, default=40.0)
    parser.add_argument("--load-check-interval-seconds", type=int, default=20)
    parser.add_argument("--load-wait-timeout-seconds", type=int, default=600)
    parser.add_argument("--post-switch-settle-seconds", type=int, default=15)
    return parser.parse_args()


def main() -> int:
    global KUBECONFIG_PATH
    args = parseArgs()
    namespace = args.namespace
    artifact_dir = args.artifact_dir
    artifact_dir.mkdir(parents=True, exist_ok=True)
    KUBECONFIG_PATH = args.kubeconfig

    base_exec_timeout = args.kubectl_exec_timeout_seconds
    exec_timeout = max(base_exec_timeout, args.kernel_exec_timeout_seconds)
    birdc_timeout = args.birdc_timeout_seconds
    export_mode = args.export_mode.strip().lower() or "all"
    scan_base = args.scan_base_seconds
    scan_jitter = args.scan_jitter_seconds
    retries = max(1, args.retries)
    retry_backoff = args.retry_backoff_seconds
    load_threshold = args.load_threshold
    load_check_interval = args.load_check_interval_seconds
    load_wait_timeout = args.load_wait_timeout_seconds
    settle_seconds = args.post_switch_settle_seconds

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
                scan_base,
                scan_jitter,
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
    summary["load_wait_timed_out"] = wait_for_cluster_idle(
        namespace,
        nodes_map,
        exec_timeout,
        load_threshold,
        load_check_interval,
        load_wait_timeout,
    )

    summary["duration_seconds"] = round(time.time() - start_time, 2)
    (artifact_dir / "start_bird_kernel_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    log(f"completed duration={summary['duration_seconds']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
