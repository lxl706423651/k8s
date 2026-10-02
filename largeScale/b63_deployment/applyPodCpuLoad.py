#!/usr/bin/env python3
"""Start or stop an identical CPU duty-cycle worker in every SeedEMU Pod.

The worker runs inside the existing router container, so the topology, CNI,
kernel and forwarding process stay unchanged.  A zero-millicore request stops
the previous worker.  Output is a complete per-Pod audit JSON.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
from datetime import datetime
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time


LOAD_PROGRAM = r"""
import hashlib, os, sys, time
millicores = float(sys.argv[1])
duty = max(0.0, min(1.0, millicores / 1000.0))
period = 0.1
payload = (os.uname().nodename.encode() + b'\0') * 512
while True:
    started = time.perf_counter()
    busy_until = started + period * duty
    while time.perf_counter() < busy_until:
        payload = hashlib.sha256(payload).digest() * 512
    remaining = period - (time.perf_counter() - started)
    if remaining > 0:
        time.sleep(remaining)
"""


def run(command: list[str], timeout: float = 60) -> subprocess.CompletedProcess[str]:
    """Run a command and turn a client timeout into an auditable result."""
    try:
        return subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        detail = stderr.strip() or f"command timed out after {timeout:g}s"
        return subprocess.CompletedProcess(command, 124, stdout, detail)


def podList(kubeconfig: Path, namespace: str, command_timeout: float = 90,
            retries: int = 3, retry_backoff_seconds: float = 2) -> list[dict]:
    """List workloads with bounded retries for transient API-server failures."""
    command = [
        "kubectl", "--kubeconfig", str(kubeconfig),
        f"--request-timeout={command_timeout:g}s", "-n", namespace,
        "get", "pods", "-l", "seedemu.io/workload=seedemu", "-o", "json",
    ]
    result: subprocess.CompletedProcess[str] | None = None
    for attempt in range(1, max(1, retries) + 1):
        result = run(command, timeout=command_timeout)
        if result.returncode == 0:
            break
        if attempt < max(1, retries):
            time.sleep(max(0.0, retry_backoff_seconds) * attempt)
    assert result is not None
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    items = json.loads(result.stdout).get("items") or []
    return sorted(items, key=lambda item: item["metadata"]["name"])


def applyOne(kubeconfig: Path, namespace: str, pod: dict, millicores: float,
             command_timeout: float = 90, retries: int = 3,
             retry_backoff_seconds: float = 2) -> dict:
    name = pod["metadata"]["name"]
    node = (pod.get("spec") or {}).get("nodeName")
    encoded = base64.b64encode(LOAD_PROGRAM.encode()).decode()
    stop = "if [ -s /tmp/seedemu-exogenous-load.pid ]; then kill $(cat /tmp/seedemu-exogenous-load.pid) 2>/dev/null || true; rm -f /tmp/seedemu-exogenous-load.pid; fi"
    if millicores > 0:
        python = f"import base64;exec(base64.b64decode({encoded!r}))"
        start = (
            f"nohup python3 -c {shlex.quote(python)} {shlex.quote(str(millicores))} "
            ">/tmp/seedemu-exogenous-load.log 2>&1 & echo $! >/tmp/seedemu-exogenous-load.pid; "
            "sleep 0.2; kill -0 $(cat /tmp/seedemu-exogenous-load.pid)"
        )
        shell = f"{stop}; {start}"
    else:
        shell = stop
    command = [
        "kubectl", "--kubeconfig", str(kubeconfig),
        f"--request-timeout={command_timeout:g}s", "-n", namespace,
        "exec", name, "-c", "main", "--", "/bin/sh", "-lc", shell,
    ]
    started = time.time()
    result: subprocess.CompletedProcess[str] | None = None
    attempt_records = []
    for attempt in range(1, max(1, retries) + 1):
        attempt_started = time.time()
        result = run(command, timeout=command_timeout)
        attempt_records.append({
            "attempt": attempt, "returncode": result.returncode,
            "durationSeconds": time.time() - attempt_started,
            "stderr": result.stderr.strip(),
        })
        if result.returncode == 0:
            break
        if attempt < max(1, retries):
            time.sleep(max(0.0, retry_backoff_seconds) * attempt)
    assert result is not None
    return {
        "pod": name, "node": node, "millicores": millicores,
        "ok": result.returncode == 0, "returncode": result.returncode,
        "stdout": result.stdout.strip(), "stderr": result.stderr.strip(),
        "durationSeconds": time.time() - started,
        "attemptCount": len(attempt_records), "attempts": attempt_records,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--cpu-millicores", type=float, required=True)
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--command-timeout-seconds", type=float, default=90)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-backoff-seconds", type=float, default=2)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if not 0 <= args.cpu_millicores <= 1000:
        parser.error("--cpu-millicores must be between 0 and 1000 per Pod")
    if args.concurrency < 1:
        parser.error("--concurrency must be at least 1")
    if args.command_timeout_seconds <= 0:
        parser.error("--command-timeout-seconds must be positive")
    if args.retries < 1:
        parser.error("--retries must be at least 1")
    if args.retry_backoff_seconds < 0:
        parser.error("--retry-backoff-seconds cannot be negative")
    pods = podList(args.kubeconfig.expanduser().resolve(), args.namespace,
                   args.command_timeout_seconds, args.retries,
                   args.retry_backoff_seconds)
    if not pods:
        parser.error("no SeedEMU workload Pods found")
    bad_phase = [item["metadata"]["name"] for item in pods
                 if (item.get("status") or {}).get("phase") != "Running"]
    if bad_phase:
        parser.error(f"all workload Pods must be Running; examples: {bad_phase[:5]}")
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futures = [pool.submit(applyOne, args.kubeconfig, args.namespace, pod, args.cpu_millicores,
                               args.command_timeout_seconds, args.retries,
                               args.retry_backoff_seconds)
                   for pod in pods]
        results = [future.result() for future in concurrent.futures.as_completed(futures)]
    results.sort(key=lambda item: item["pod"])
    failures = [item for item in results if not item["ok"]]
    report = {
        "schemaVersion": 1,
        "createdAt": datetime.now().astimezone().isoformat(),
        "namespace": args.namespace,
        "requestedCpuMillicoresPerPod": args.cpu_millicores,
        "commandTimeoutSeconds": args.command_timeout_seconds,
        "retries": args.retries,
        "retryBackoffSeconds": args.retry_backoff_seconds,
        "podCount": len(results),
        "successCount": len(results) - len(failures),
        "failureCount": len(failures),
        "status": "PASS" if not failures else "FAIL",
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"{report['status']}: {report['successCount']}/{report['podCount']} Pods at {args.cpu_millicores:g}m")
    if failures:
        print(json.dumps(failures[:5], indent=2), file=sys.stderr)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
