#!/usr/bin/env python3
"""Enable BIRD kernel export through node-local containerd state.

Inputs: cluster inventory, experiment artifact directory, and namespace.
Outputs: per-node logs and start_bird_kernel_summary.json.
Side effects: SSHes to K3s nodes, writes kernel.conf inside workload
containers, and reloads BIRD with birdc configure.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml


REMOTE_SCRIPT = r"""#!/usr/bin/env bash
set -u

RUNTIME_DIR="/run/k3s/containerd/io.containerd.runtime.v2.task/k8s.io"
WORK_DIR="$(mktemp -d /tmp/seedemu-start-kernel-local.XXXXXX)"
trap 'rm -rf "${WORK_DIR}"' EXIT

CIDS="${WORK_DIR}/cids"
OUT="${WORK_DIR}/out"
START_ONE="${WORK_DIR}/start_one.sh"
KERNEL_SCRIPT="${WORK_DIR}/kernel_switch.sh"
EXPECTED_TOTAL="${EXPECTED_TOTAL:-0}"
CID_ENUM_RETRIES="${CID_ENUM_RETRIES:-60}"
CID_ENUM_SLEEP_SECONDS="${CID_ENUM_SLEEP_SECONDS:-5}"

attempt=0
: > "${CIDS}"
while [ "${attempt}" -lt "${CID_ENUM_RETRIES}" ]; do
    attempt=$((attempt + 1))
    if crictl ps --label "io.kubernetes.pod.namespace=${NAMESPACE}" -q > "${CIDS}.tmp" 2>"${WORK_DIR}/crictl.err"; then
        mv "${CIDS}.tmp" "${CIDS}"
    else
        : > "${CIDS}"
    fi
    total="$(wc -l < "${CIDS}" | tr -d ' ')"
    if [ "${EXPECTED_TOTAL}" -le 0 ] || [ "${total}" -ge "${EXPECTED_TOTAL}" ]; then
        break
    fi
    echo "waiting_for_containers attempt=${attempt} total=${total} expected=${EXPECTED_TOTAL}"
    sleep "${CID_ENUM_SLEEP_SECONDS}"
done
total="$(wc -l < "${CIDS}" | tr -d ' ')"
if [ "${EXPECTED_TOTAL}" -gt 0 ] && [ "${total}" -ne "${EXPECTED_TOTAL}" ]; then
    load_average="$(cat /proc/loadavg)"
    echo "SUMMARY total=${total} switched=0 failed=1 missing_pid=0 expected=${EXPECTED_TOTAL} load=\"${load_average}\""
    echo "Container enumeration did not match expected target count."
    cat "${WORK_DIR}/crictl.err" 2>/dev/null || true
    exit 2
fi

cat > "${KERNEL_SCRIPT}" <<'EOS'
#!/bin/sh
set -eu

birdc show status >/dev/null 2>&1
mkdir -p /etc/bird/conf

interval="${SCAN_BASE_SECONDS}"
if [ "${SCAN_JITTER_SECONDS}" -gt 0 ] 2>/dev/null; then
    jitter="$(awk -v max="${SCAN_JITTER_SECONDS}" 'BEGIN{srand(); print int(rand() * (max + 1))}')"
    interval="$((SCAN_BASE_SECONDS + jitter))"
fi

if [ "${EXPORT_MODE}" = "device_ospf_only" ]; then
    cat > /etc/bird/conf/kernel.conf <<EOF
protocol kernel {
    merge paths on;
    persist;
    scan time ${interval};
    ipv4 {
        import none;
        export filter {
            if source = RTS_DEVICE then accept;
            if source = RTS_OSPF then accept;
            reject;
        };
    };
}
EOF
else
    cat > /etc/bird/conf/kernel.conf <<EOF
protocol kernel {
    merge paths on;
    persist;
    scan time ${interval};
    ipv4 {
        import none;
        export all;
    };
}
EOF
fi

if grep -F 'include "/etc/bird/conf/*.conf";' /etc/bird/bird.conf >/dev/null 2>&1; then
    sed -i '\#^include "/etc/bird/conf/kernel.conf";$#d' /etc/bird/bird.conf
elif ! grep -F 'include "/etc/bird/conf/kernel.conf";' /etc/bird/bird.conf >/dev/null 2>&1; then
    printf '\ninclude "/etc/bird/conf/kernel.conf";\n' >> /etc/bird/bird.conf
fi

timeout "${BIRDC_TIMEOUT_SECONDS}" birdc configure >/tmp/seedemu-birdc-configure.log 2>&1
timeout "${BIRDC_TIMEOUT_SECONDS}" birdc show protocols | awk 'NR>2 && $2=="Kernel" && $4=="up" {ok=1} END{exit ok?0:1}'
EOS
chmod +x "${KERNEL_SCRIPT}"

cat > "${START_ONE}" <<'EOS'
#!/usr/bin/env bash
set -u

cid="$1"
kernel_script="$2"
runtime_dir="/run/k3s/containerd/io.containerd.runtime.v2.task/k8s.io"
pidfile="${runtime_dir}/${cid}/init.pid"
if [ ! -r "${pidfile}" ]; then
    echo "missing_pid ${cid}"
    exit 0
fi

pid="$(cat "${pidfile}" 2>/dev/null || true)"
if [ -z "${pid}" ]; then
    echo "missing_pid ${cid}"
    exit 0
fi

output="$(
    EXPORT_MODE="${EXPORT_MODE}" \
    SCAN_BASE_SECONDS="${SCAN_BASE_SECONDS}" \
    SCAN_JITTER_SECONDS="${SCAN_JITTER_SECONDS}" \
    BIRDC_TIMEOUT_SECONDS="${BIRDC_TIMEOUT_SECONDS}" \
    timeout "${ONE_TIMEOUT_SECONDS:-120}" \
    nsenter -t "${pid}" -m -u -i -n -p -- sh -s < "${kernel_script}" 2>&1
)"
rc=$?
if [ "${rc}" -eq 0 ]; then
    echo "switched ${cid}"
    sleep "${SWITCH_SLEEP_SECONDS:-0}"
else
    printf 'failed %s rc=%s %s\n' "${cid}" "${rc}" "$(printf '%s' "${output}" | tr '\n' ' ' | cut -c1-240)"
fi
EOS
chmod +x "${START_ONE}"

echo "node=$(hostname) namespace=${NAMESPACE} total=${total} expected=${EXPECTED_TOTAL} parallel=${PARALLEL} one_timeout=${ONE_TIMEOUT_SECONDS:-120} export_mode=${EXPORT_MODE}"
xargs -r -n1 -P "${PARALLEL}" -I{} "${START_ONE}" "{}" "${KERNEL_SCRIPT}" < "${CIDS}" > "${OUT}"

switched="$(grep -c '^switched ' "${OUT}" || true)"
failed="$(grep -c '^failed ' "${OUT}" || true)"
missing_pid="$(grep -c '^missing_pid ' "${OUT}" || true)"
load_average="$(cat /proc/loadavg)"

echo "SUMMARY total=${total} switched=${switched} failed=${failed} missing_pid=${missing_pid} expected=${EXPECTED_TOTAL} load=\"${load_average}\""
if [ "${failed}" -gt 0 ] || [ "${missing_pid}" -gt 0 ] || [ "${switched}" -lt "${total}" ]; then
    echo "Non-passing node result; first failures, if any:"
    grep -E '^(failed|missing_pid) ' "${OUT}" | head -20 || true
    exit 2
fi
"""


@dataclass
class Node:
    name: str
    ip: str
    user: str
    key: str


def nowUtc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def loadNodes(inventory_path: Path) -> list[Node]:
    data = yaml.safe_load(inventory_path.read_text(encoding="utf-8")) or {}
    nodes: list[Node] = []
    for item in data.get("nodes", []):
        ssh = item.get("ssh", {}) or {}
        nodes.append(
            Node(
                name=str(item["name"]),
                ip=str(item.get("managementIp") or item.get("ip")),
                user=str(ssh.get("user") or "ubuntu"),
                key=str(Path(str(ssh.get("key") or "~/.ssh/id_ed25519")).expanduser()),
            )
        )
    if not nodes:
        raise ValueError(f"no nodes found in {inventory_path}")
    return nodes


def loadExpectedTargets(artifact_dir: Path) -> dict[str, int]:
    target_path = artifact_dir / "start_bird_targets.json"
    if target_path.exists():
        data = json.loads(target_path.read_text(encoding="utf-8"))
        return dict(Counter(str(item.get("node", "")) for item in data if item.get("node")))

    summary_path = artifact_dir / "start_bird_summary.json"
    if not summary_path.exists():
        return {}
    data = json.loads(summary_path.read_text(encoding="utf-8"))
    result: dict[str, int] = {}
    for item in data.get("nodes", []):
        node = str(item.get("node", ""))
        summary = item.get("summary", {}) if isinstance(item, dict) else {}
        total = int(summary.get("bird_count") or summary.get("total") or 0)
        if node and total:
            result[node] = total
    return result


def parseSummary(stdout: str) -> dict[str, object]:
    for line in reversed(stdout.splitlines()):
        if not line.startswith("SUMMARY "):
            continue
        result: dict[str, object] = {"raw": line}
        for token in shlex.split(line[len("SUMMARY ") :]):
            if "=" not in token:
                continue
            key, value = token.split("=", 1)
            if key in {"total", "switched", "failed", "missing_pid", "expected"}:
                result[key] = int(value)
            else:
                result[key] = value
        return result
    return {"raw": "", "failed": 1, "failure_reason": "missing_summary"}


def runNode(
    node: Node,
    namespace: str,
    parallel: int,
    one_timeout: int,
    timeout: int,
    export_mode: str,
    scan_base: int,
    scan_jitter: int,
    birdc_timeout: int,
    expected_total: int,
    switch_sleep: int,
) -> dict[str, object]:
    command = [
        "ssh",
        "-i",
        node.key,
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=60",
        f"{node.user}@{node.ip}",
        "sudo",
        "-n",
        "env",
        f"NAMESPACE={namespace}",
        f"PARALLEL={parallel}",
        f"ONE_TIMEOUT_SECONDS={one_timeout}",
        f"EXPORT_MODE={export_mode}",
        f"SCAN_BASE_SECONDS={scan_base}",
        f"SCAN_JITTER_SECONDS={scan_jitter}",
        f"BIRDC_TIMEOUT_SECONDS={birdc_timeout}",
        f"EXPECTED_TOTAL={expected_total}",
        f"SWITCH_SLEEP_SECONDS={switch_sleep}",
        "CID_ENUM_RETRIES=60",
        "CID_ENUM_SLEEP_SECONDS=5",
        "bash",
        "-s",
    ]
    started_at = time.time()
    try:
        completed = subprocess.run(
            command,
            input=REMOTE_SCRIPT,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return {
            "node": node.name,
            "ip": node.ip,
            "returncode": 124,
            "duration_seconds": round(time.time() - started_at, 2),
            "stdout": stdout,
            "stderr": stderr or f"timeout after {timeout}s",
            "summary": {"failed": 1, "failure_reason": "ssh_timeout"},
        }
    return {
        "node": node.name,
        "ip": node.ip,
        "returncode": completed.returncode,
        "duration_seconds": round(time.time() - started_at, 2),
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "summary": parseSummary(completed.stdout),
    }


def writeNodeLog(artifact_dir: Path, result: dict[str, object]) -> None:
    log_path = artifact_dir / f"start_bird_kernel_node_local_{result['node']}.log"
    log_path.write_text(
        "\n".join(
            [
                f"node={result['node']}",
                f"ip={result['ip']}",
                f"returncode={result['returncode']}",
                f"duration_seconds={result['duration_seconds']}",
                "--- stdout ---",
                str(result.get("stdout", "")),
                "--- stderr ---",
                str(result.get("stderr", "")),
                "",
            ]
        ),
        encoding="utf-8",
    )


def parseArgs() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--parallel-per-node", type=int, default=12)
    parser.add_argument("--node-concurrency", type=int, default=0)
    parser.add_argument("--switch-sleep-seconds", type=int, default=0)
    parser.add_argument("--one-timeout-seconds", type=int, default=180)
    parser.add_argument("--node-timeout-seconds", type=int, default=7200)
    parser.add_argument("--export-mode", default="all")
    parser.add_argument("--scan-base-seconds", type=int, default=6000)
    parser.add_argument("--scan-jitter-seconds", type=int, default=120)
    parser.add_argument("--birdc-timeout-seconds", type=int, default=20)
    return parser.parse_args()


def main() -> int:
    args = parseArgs()
    artifact_dir = args.artifact_dir
    artifact_dir.mkdir(parents=True, exist_ok=True)
    nodes = loadNodes(args.inventory)
    expected_by_node = loadExpectedTargets(artifact_dir)
    node_concurrency = len(nodes) if args.node_concurrency <= 0 else min(args.node_concurrency, len(nodes))

    start_time = time.time()
    print(f"[{nowUtc()}] === start bird kernel node-local ===", flush=True)
    print(
        f"[{nowUtc()}] namespace={args.namespace} nodes={len(nodes)} "
        f"node_concurrency={node_concurrency} parallel_per_node={args.parallel_per_node} "
        f"export_mode={args.export_mode}",
        flush=True,
    )

    results: list[dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=node_concurrency) as pool:
        futures = {
            pool.submit(
                runNode,
                node,
                args.namespace,
                args.parallel_per_node,
                args.one_timeout_seconds,
                args.node_timeout_seconds,
                args.export_mode,
                args.scan_base_seconds,
                args.scan_jitter_seconds,
                args.birdc_timeout_seconds,
                expected_by_node.get(node.name, 0),
                args.switch_sleep_seconds,
            ): node
            for node in nodes
        }
        for future in as_completed(futures):
            result = future.result()
            writeNodeLog(artifact_dir, result)
            results.append(result)
            print(
                f"[{nowUtc()}] node={result['node']} rc={result['returncode']} "
                f"duration={result['duration_seconds']} summary={result['summary']}",
                flush=True,
            )

    for item in results:
        expected = expected_by_node.get(str(item["node"]))
        if expected is not None:
            item["summary"]["expected"] = expected

    total_expected = sum(expected_by_node.values())
    total_targets = sum(int(item["summary"].get("total", 0)) for item in results)
    total_switched = sum(int(item["summary"].get("switched", 0)) for item in results)
    failures = [
        item
        for item in results
        if int(item["returncode"]) != 0
        or int(item["summary"].get("failed", 0)) > 0
        or int(item["summary"].get("missing_pid", 0)) > 0
        or int(item["summary"].get("switched", 0)) < int(item["summary"].get("total", 0))
        or (
            str(item["node"]) in expected_by_node
            and int(item["summary"].get("switched", 0)) < expected_by_node[str(item["node"])]
        )
    ]

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "namespace": args.namespace,
        "strategy": "node-local-nsenter",
        "kernel_export_mode": args.export_mode,
        "status": "PASS" if not failures else "FAIL",
        "duration_seconds": round(time.time() - start_time, 2),
        "nodes": [
            {
                "node": item["node"],
                "ip": item["ip"],
                "returncode": item["returncode"],
                "duration_seconds": item["duration_seconds"],
                "summary": item["summary"],
            }
            for item in sorted(results, key=lambda result: str(result["node"]))
        ],
        "targets": total_targets,
        "expected_targets": total_expected,
        "switched": total_switched,
    }
    if failures:
        summary["failure_reason"] = "one_or_more_nodes_failed"

    (artifact_dir / "start_bird_kernel_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(
        f"[{nowUtc()}] completed status={summary['status']} targets={total_targets} "
        f"switched={total_switched} duration={summary['duration_seconds']}",
        flush=True,
    )
    return 0 if not failures else 41


if __name__ == "__main__":
    raise SystemExit(main())
