#!/usr/bin/env bash
# Check route-table growth in 10 different AS border-router pods.
#
# Inputs: one experiment run directory. The script derives kubeconfig and
# namespace from assignment.yaml through lib.sh.
# Outputs: test.log and test.json under the run directory.
# Side effects: read-only kubectl exec calls into existing brd pods.
# Context: run after start-bird and start-kernel have completed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib.sh"

setup_experiment_context "${1:-}"
ensure_kubeconfig

TEST_TARGET_COUNT=10
KUBECTL_EXEC_TIMEOUT_SECONDS=45
KUBECTL_LIST_TIMEOUT_SECONDS=300
TEST_JSON="${EXPERIMENT_DIR}/test.json"

begin_stage_logging "test"

echo "EXPERIMENT_DIR=${EXPERIMENT_DIR}"
echo "SEED_NAMESPACE=${SEED_NAMESPACE}"
echo "KUBECONFIG=${KUBECONFIG}"
echo "TEST_TARGET_COUNT=${TEST_TARGET_COUNT}"
echo "TEST_JSON=${TEST_JSON}"

kubectl get namespace "${SEED_NAMESPACE}" >/dev/null 2>&1 || {
    echo "Namespace ${SEED_NAMESPACE} does not exist." >&2
    exit 1
}

python3 - "${SEED_NAMESPACE}" "${TEST_JSON}" "${TEST_TARGET_COUNT}" \
    "${KUBECTL_EXEC_TIMEOUT_SECONDS}" "${KUBECTL_LIST_TIMEOUT_SECONDS}" <<'PY'
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


EXIT_TEST_FAILED = 61


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return subprocess.CompletedProcess(cmd, 124, stdout, stderr or f"timeout after {timeout}s")


def kubectl(namespace: str, args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    return run(["kubectl", "-n", namespace, *args], timeout=timeout)


def select_targets(namespace: str, target_count: int, list_timeout: int) -> list[dict[str, str]]:
    result = kubectl(namespace, ["get", "pods", "-o", "json"], timeout=list_timeout)
    result.check_returncode()
    data = json.loads(result.stdout)
    selected_by_asn: dict[str, dict[str, str]] = {}

    for item in data.get("items", []):
        metadata = item.get("metadata", {}) or {}
        labels = metadata.get("labels", {}) or {}
        status = item.get("status", {}) or {}
        if status.get("phase") != "Running":
            continue
        if labels.get("seedemu.io/workload") != "seedemu":
            continue
        role = str(labels.get("seedemu.io/role", ""))
        name = str(metadata.get("name", ""))
        if role != "brd" and "brd" not in name:
            continue
        asn = str(labels.get("seedemu.io/asn", ""))
        if not asn:
            match = re.match(r"as([0-9]+)brd", name)
            asn = match.group(1) if match else ""
        if not asn or asn in selected_by_asn:
            continue
        selected_by_asn[asn] = {
            "pod": name,
            "asn": asn,
            "node": str((item.get("spec", {}) or {}).get("nodeName", "")),
        }

    def sort_key(target: dict[str, str]) -> tuple[int, str]:
        asn = target["asn"]
        return (int(asn) if asn.isdigit() else 10**12, target["pod"])

    return sorted(selected_by_asn.values(), key=sort_key)[:target_count]


def parse_wc_line_count(text: str) -> int | None:
    match = re.search(r"\b([0-9]+)\b", text)
    return int(match.group(1)) if match else None


def parse_bird_network_count(text: str) -> int | None:
    matches = re.findall(r"\b([0-9]+)\s+networks?\b", text, flags=re.IGNORECASE)
    if matches:
        return int(matches[-1])
    return None


def check_pod(namespace: str, target: dict[str, str], exec_timeout: int) -> dict[str, object]:
    pod = target["pod"]
    ip_cmd = "ip route | wc"
    bird_cmd = "birdc show route count"
    ip_result = kubectl(namespace, ["exec", pod, "--", "sh", "-lc", ip_cmd], timeout=exec_timeout)
    bird_result = kubectl(namespace, ["exec", pod, "--", "sh", "-lc", bird_cmd], timeout=exec_timeout)

    ip_route_lines = parse_wc_line_count(ip_result.stdout)
    bird_networks = parse_bird_network_count(bird_result.stdout)
    passed = (
        ip_result.returncode == 0
        and bird_result.returncode == 0
        and ip_route_lines is not None
        and bird_networks is not None
        and ip_route_lines > bird_networks
    )
    reason = ""
    if ip_result.returncode != 0:
        reason = "ip_route_wc_failed"
    elif bird_result.returncode != 0:
        reason = "bird_route_count_failed"
    elif ip_route_lines is None:
        reason = "ip_route_wc_parse_failed"
    elif bird_networks is None:
        reason = "bird_network_count_parse_failed"
    elif ip_route_lines <= bird_networks:
        reason = "ip_route_count_not_greater_than_bird_network_count"

    return {
        **target,
        "ip_route_wc_raw": ip_result.stdout.strip(),
        "ip_route_count": ip_route_lines,
        "bird_route_count_raw": bird_result.stdout.strip(),
        "bird_network_count": bird_networks,
        "passed": passed,
        "failure_reason": reason,
        "ip_route_stderr": ip_result.stderr.strip(),
        "bird_stderr": bird_result.stderr.strip(),
    }


def main() -> int:
    if len(sys.argv) != 6:
        print("Usage: test.sh <run_dir>", file=sys.stderr)
        return 2

    namespace = sys.argv[1]
    output_path = Path(sys.argv[2])
    target_count = int(sys.argv[3])
    exec_timeout = int(sys.argv[4])
    list_timeout = int(sys.argv[5])

    summary: dict[str, object] = {
        "generated_at": now_iso(),
        "namespace": namespace,
        "target_count_requested": target_count,
        "status": "FAIL",
        "failure_reason": "",
        "targets": [],
        "failures": [],
    }

    try:
        targets = select_targets(namespace, target_count, list_timeout)
    except Exception as exc:
        summary["failure_reason"] = "pod_list_failed"
        summary["error"] = str(exc)
        output_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"Failed to list brd pods: {exc}", file=sys.stderr)
        return EXIT_TEST_FAILED

    summary["targets"] = targets
    if len(targets) < target_count:
        summary["failure_reason"] = "not_enough_distinct_as_brd_pods"
        summary["available_distinct_as_count"] = len(targets)
        output_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"Only found {len(targets)} distinct AS brd pods; need {target_count}.", file=sys.stderr)
        return EXIT_TEST_FAILED

    checks = [check_pod(namespace, target, exec_timeout) for target in targets]
    failures = [item for item in checks if not item["passed"]]
    summary["checks"] = checks
    summary["checked"] = len(checks)
    summary["failures"] = failures
    summary["failed_pods"] = [str(item["pod"]) for item in failures]

    if failures:
        summary["failure_reason"] = "route_count_check_failed"
        output_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        for item in failures:
            print(str(item["pod"]))
        print(f"FAIL: {len(failures)}/{len(checks)} pods did not satisfy ip route count > BIRD network count.")
        return EXIT_TEST_FAILED

    summary["status"] = "PASS"
    output_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"PASS: {len(checks)}/{len(checks)} pods satisfy ip route count > BIRD network count.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
PY

echo "Wrote ${TEST_JSON}"
