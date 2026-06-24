#!/usr/bin/env python3
"""Verify that BIRD is running in router-like pods for a SeedEMU namespace.

Inputs are the Kubernetes namespace and experiment artifact directory. The
script writes target and summary JSON files and performs read-only kubectl exec
checks for the b63 running flow.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

ROLE_SET = {"r", "brd", "rs"}
EXIT_BIRD_VERIFY_FAILED = 51
PHASE_PROGRESS_EVERY = 100
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


def load_targets(namespace: str) -> list[PodTarget]:
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
    targets.sort(key=lambda pod: (pod.node, int(pod.asn or 0), pod.name))
    return targets


def bird_ok(namespace: str, pod: str, exec_timeout: int) -> tuple[bool, str]:
    check_cmd = (
        "pgrep -x bird >/dev/null 2>&1 && "
        "timeout 5 birdc show status >/dev/null 2>&1"
    )
    result = kubectl_exec(namespace, pod, check_cmd, timeout=exec_timeout)
    ok = result.returncode == 0
    detail = (result.stderr or result.stdout).strip() or f"rc={result.returncode}"
    return ok, detail


def verify_node(
    node_name: str,
    namespace: str,
    targets: list[PodTarget],
    exec_timeout: int,
) -> tuple[int, list[dict[str, str]]]:
    verified = 0
    failures: list[dict[str, str]] = []
    for idx, target in enumerate(targets, start=1):
        ok, detail = bird_ok(namespace, target.name, exec_timeout)
        if ok:
            verified += 1
        else:
            failures.append({"pod": target.name, "stderr": detail, "node": node_name})
        if idx % PHASE_PROGRESS_EVERY == 0 or idx == len(targets):
            log(f"mode=bird node={node_name} checked={idx}/{len(targets)} failures={len(failures)}")
    return verified, failures


def parseArgs() -> argparse.Namespace:
    """Parse explicit BIRD verification parameters."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--kubeconfig", default="")
    parser.add_argument("--kubectl-exec-timeout-seconds", type=int, default=30)
    parser.add_argument("--verify-timeout-seconds", type=int, default=1200)
    parser.add_argument("--verify-retry-interval-seconds", type=int, default=10)
    return parser.parse_args()


def main() -> int:
    global KUBECONFIG_PATH
    args = parseArgs()
    namespace = args.namespace
    artifact_dir = args.artifact_dir
    KUBECONFIG_PATH = args.kubeconfig

    artifact_dir.mkdir(parents=True, exist_ok=True)
    base_exec_timeout = args.kubectl_exec_timeout_seconds
    exec_timeout = max(base_exec_timeout, 45)
    timeout_seconds = args.verify_timeout_seconds
    retry_interval = args.verify_retry_interval_seconds

    targets = load_targets(namespace)
    file_prefix = "verify_bird"
    targets_file = artifact_dir / f"{file_prefix}_targets.json"
    summary_file = artifact_dir / f"{file_prefix}_summary.json"
    targets_file.write_text(json.dumps([asdict(t) for t in targets], indent=2), encoding="utf-8")

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "namespace": namespace,
        "mode": "bird",
        "targets": len(targets),
        "status": "PASS",
        "failure_reason": "",
        "strategy": "node-aware-concurrency",
    }
    if not targets:
        summary["status"] = "FAIL"
        summary["failure_reason"] = "no_router_like_pods_found"
        summary_file.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return EXIT_BIRD_VERIFY_FAILED

    nodes_map: dict[str, list[PodTarget]] = defaultdict(list)
    for target in targets:
        nodes_map[target.node].append(target)
    log("=== verify bird ===")
    log(f"grouped {len(targets)} targets into {len(nodes_map)} nodes")

    deadline = time.time() + timeout_seconds
    round_id = 0
    while time.time() < deadline:
        round_id += 1
        log(f"verify_round={round_id} mode=bird")
        failures: list[dict[str, str]] = []
        verified = 0
        with ThreadPoolExecutor(max_workers=len(nodes_map)) as pool:
            futures = {
                pool.submit(verify_node, node, namespace, pods, exec_timeout): node
                for node, pods in nodes_map.items()
            }
            for future in as_completed(futures):
                try:
                    ok_count, node_failures = future.result()
                    verified += ok_count
                    failures.extend(node_failures)
                except Exception as exc:
                    failures.append({"pod": f"node:{futures[future]}", "stderr": str(exc), "node": futures[future]})
        summary["verified"] = verified
        summary["failures"] = failures
        if not failures:
            summary["duration_seconds"] = round(timeout_seconds - max(0, deadline - time.time()), 2)
            summary_file.write_text(json.dumps(summary, indent=2), encoding="utf-8")
            log(f"verify bird completed verified={verified}/{len(targets)}")
            return 0
        log(f"verify bird pending_failures={len(failures)}; retry in {retry_interval}s")
        time.sleep(retry_interval)

    summary["status"] = "FAIL"
    summary["failure_reason"] = "bird_verify_failed"
    summary["duration_seconds"] = round(timeout_seconds - max(0, deadline - time.time()), 2)
    summary_file.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return EXIT_BIRD_VERIFY_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
