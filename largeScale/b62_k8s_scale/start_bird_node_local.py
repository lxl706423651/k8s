#!/usr/bin/env python3
"""Start BIRD in B62 workload containers through node-local containerd state.

Inputs: cluster inventory, experiment artifact directory, and namespace.
Outputs: per-node logs and start_bird_summary.json in the artifact directory.
Side effects: SSHes to K3s nodes and starts BIRD inside running containers.
Expected context: run from b62_k8s_scale after the Kubernetes workload is ready.
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
WORK_DIR="$(mktemp -d /tmp/seedemu-start-bird-local.XXXXXX)"
trap 'rm -rf "${WORK_DIR}"' EXIT

CIDS="${WORK_DIR}/cids"
OUT="${WORK_DIR}/out"
START_ONE="${WORK_DIR}/start_one.sh"

crictl ps --label "io.kubernetes.pod.namespace=${NAMESPACE}" -q > "${CIDS}"
total="$(wc -l < "${CIDS}" | tr -d ' ')"

cat > "${START_ONE}" <<'EOS'
#!/usr/bin/env bash
set -u

cid="$1"
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

result="$(timeout "${ONE_TIMEOUT_SECONDS:-120}" nsenter -t "${pid}" -m -u -i -n -p -- sh -lc '
if ps -e 2>/dev/null | awk '\''$NF == "bird" {found=1} END {exit found ? 0 : 1}'\''; then
    echo skipped
    exit 0
fi
if timeout 1 birdc show status >/dev/null 2>&1; then
    echo skipped
    exit 0
fi
rm -f /run/bird/bird.ctl /run/bird/bird.pid /var/run/bird.ctl /var/run/bird.pid /run/bird/*.ctl /run/bird/*.pid /var/run/bird/*.ctl /var/run/bird/*.pid 2>/dev/null || true
mkdir -p /etc/bird/conf /run/bird
: > /etc/bird/conf/00-empty.conf
rm -f /etc/bird/conf/kernel.conf
if grep -F '\''include "/etc/bird/conf/*.conf";'\'' /etc/bird/bird.conf >/dev/null 2>&1; then
    sed -i '\''\#^include "/etc/bird/conf/kernel.conf";$#d'\'' /etc/bird/bird.conf
fi
bird >/tmp/seedemu-bird.log 2>&1 &
echo started
' 2>/dev/null || true)"

case "${result}" in
    skipped) echo "skipped ${cid}" ;;
    started)
        echo "started ${cid}"
        sleep "${START_SLEEP_SECONDS:-0}"
        ;;
    *) echo "failed ${cid}" ;;
esac
EOS
chmod +x "${START_ONE}"

echo "node=$(hostname) namespace=${NAMESPACE} total=${total} parallel=${PARALLEL} one_timeout=${ONE_TIMEOUT_SECONDS:-120}"
xargs -r -n1 -P "${PARALLEL}" "${START_ONE}" < "${CIDS}" > "${OUT}"

started="$(grep -c '^started ' "${OUT}" || true)"
skipped="$(grep -c '^skipped ' "${OUT}" || true)"
failed="$(grep -c '^failed ' "${OUT}" || true)"
missing_pid="$(grep -c '^missing_pid ' "${OUT}" || true)"

bird_pids="$(pgrep -x bird | tr '\n' ' ' || true)"
if [ -n "${bird_pids}" ]; then
    renice 19 -p ${bird_pids} >/dev/null 2>&1 || true
fi
bird_count="$(pgrep -x bird | wc -l | tr -d ' ')"
load_average="$(cat /proc/loadavg)"

echo "SUMMARY total=${total} skipped=${skipped} started=${started} failed=${failed} missing_pid=${missing_pid} bird_count=${bird_count} load=\"${load_average}\""
if [ "${failed}" -gt 0 ] || [ "${missing_pid}" -gt 0 ] || [ "${bird_count}" -ne "${total}" ]; then
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
    """Return a compact UTC timestamp for logs and summaries."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def loadNodes(inventory_path: Path) -> list[Node]:
    """Load SSH node connection details from cluster.inventory.yaml."""
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
    """Load expected per-node BIRD target counts from cached pod targets."""
    target_path = artifact_dir / "start_bird_targets.json"
    if not target_path.exists():
        return {}
    data = json.loads(target_path.read_text(encoding="utf-8"))
    return dict(Counter(str(item.get("node", "")) for item in data if item.get("node")))


def parseSummary(stdout: str) -> dict[str, object]:
    """Extract the remote SUMMARY line into a structured dictionary."""
    for line in reversed(stdout.splitlines()):
        if not line.startswith("SUMMARY "):
            continue
        result: dict[str, object] = {"raw": line}
        for token in shlex.split(line[len("SUMMARY ") :]):
            if "=" not in token:
                continue
            key, value = token.split("=", 1)
            if key in {"total", "skipped", "started", "failed", "missing_pid", "bird_count"}:
                result[key] = int(value)
            else:
                result[key] = value
        return result
    return {"raw": "", "failed": 1, "failure_reason": "missing_summary"}


def runNode(
    node: Node,
    namespace: str,
    parallel: int,
    start_sleep: int,
    one_timeout: int,
    timeout: int,
) -> dict[str, object]:
    """Run the node-local BIRD starter on one K3s node over SSH."""
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
        f"START_SLEEP_SECONDS={start_sleep}",
        f"ONE_TIMEOUT_SECONDS={one_timeout}",
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
    """Write one remote node execution log into the experiment directory."""
    log_path = artifact_dir / f"start_bird_node_local_{result['node']}.log"
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
    """Parse explicit node-local start-bird parameters."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--parallel-per-node", type=int, default=24)
    parser.add_argument("--node-concurrency", type=int, default=0)
    parser.add_argument("--start-sleep-seconds", type=int, default=0)
    parser.add_argument("--one-timeout-seconds", type=int, default=120)
    parser.add_argument("--node-timeout-seconds", type=int, default=7200)
    return parser.parse_args()


def main() -> int:
    args = parseArgs()
    artifact_dir = args.artifact_dir
    artifact_dir.mkdir(parents=True, exist_ok=True)
    nodes = loadNodes(args.inventory)
    node_concurrency = len(nodes) if args.node_concurrency <= 0 else min(args.node_concurrency, len(nodes))

    start_time = time.time()
    print(f"[{nowUtc()}] === start bird node-local ===", flush=True)
    print(
        f"[{nowUtc()}] namespace={args.namespace} nodes={len(nodes)} "
        f"node_concurrency={node_concurrency} parallel_per_node={args.parallel_per_node}",
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
                args.start_sleep_seconds,
                args.one_timeout_seconds,
                args.node_timeout_seconds,
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

    expected_by_node = loadExpectedTargets(artifact_dir)
    for item in results:
        expected = expected_by_node.get(str(item["node"]))
        if expected is not None:
            item["summary"]["expected"] = expected

    total_expected = sum(expected_by_node.values())
    total_containers = sum(int(item["summary"].get("total", 0)) for item in results)
    total_birds = sum(int(item["summary"].get("bird_count", 0)) for item in results)
    failures = [
        item
        for item in results
        if int(item["returncode"]) != 0
        or int(item["summary"].get("failed", 0)) > 0
        or int(item["summary"].get("missing_pid", 0)) > 0
        or int(item["summary"].get("bird_count", 0)) != int(item["summary"].get("total", 0))
        or (
            str(item["node"]) in expected_by_node
            and int(item["summary"].get("bird_count", 0)) != expected_by_node[str(item["node"])]
        )
    ]

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "namespace": args.namespace,
        "strategy": "node-local-nsenter",
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
        "targets": total_containers,
        "expected_targets": total_expected,
        "bird_processes": total_birds,
    }
    if failures:
        summary["failure_reason"] = "one_or_more_nodes_failed"

    (artifact_dir / "start_bird_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(
        f"[{nowUtc()}] completed status={summary['status']} targets={total_containers} "
        f"bird_processes={total_birds} duration={summary['duration_seconds']}",
        flush=True,
    )
    return 0 if not failures else 10


if __name__ == "__main__":
    raise SystemExit(main())
