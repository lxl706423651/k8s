#!/usr/bin/env python3
"""Start BIRD through node-local containerd state.

Inputs: cluster inventory, kubeconfig, namespace, and an experiment artifact
directory. Outputs: per-node logs, start_bird_targets.json, and
start_bird_summary.json. Side effects: SSHes to K3s nodes and starts BIRD
inside running SeedEMU router/border/route-server containers with nsenter.
Expected context: run from b62_k8s_scale after deployment is ready.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml


ROLE_SET = {"r", "brd", "rs"}

REMOTE_SCRIPT = r"""#!/usr/bin/env bash
set -u

WORK_DIR="$(mktemp -d /tmp/seedemu-start-bird-local.XXXXXX)"
trap 'rm -rf "${WORK_DIR}"' EXIT

PODS_JSON="${WORK_DIR}/pods.json"
CONTAINERS_JSON="${WORK_DIR}/containers.json"
TARGETS="${WORK_DIR}/targets.tsv"
OUT="${WORK_DIR}/out"
START_ONE="${WORK_DIR}/start_one.sh"
EXPECTED_TOTAL="${EXPECTED_TOTAL:-0}"
CID_ENUM_RETRIES="${CID_ENUM_RETRIES:-60}"
CID_ENUM_SLEEP_SECONDS="${CID_ENUM_SLEEP_SECONDS:-5}"
LOAD_THRESHOLD="${LOAD_THRESHOLD:-50}"
LOAD_CHECK_INTERVAL_SECONDS="${LOAD_CHECK_INTERVAL_SECONDS:-30}"
LOAD_MAX_WAIT_SECONDS="${LOAD_MAX_WAIT_SECONDS:-600}"
START_BATCH_SIZE="${START_BATCH_SIZE:-0}"
START_BATCH_COOLDOWN_SECONDS="${START_BATCH_COOLDOWN_SECONDS:-0}"

collect_targets() {
    if ! crictl pods -o json > "${PODS_JSON}" 2>"${WORK_DIR}/crictl-pods.err"; then
        : > "${PODS_JSON}"
    fi
    if ! crictl ps -o json > "${CONTAINERS_JSON}" 2>"${WORK_DIR}/crictl-ps.err"; then
        : > "${CONTAINERS_JSON}"
    fi
    python3 - "${PODS_JSON}" "${CONTAINERS_JSON}" "${TARGETS}" <<'PY'
import json
import os
import sys

namespace = os.environ["NAMESPACE"]
role_set = {"r", "brd", "rs"}
pods_path, containers_path, output_path = sys.argv[1:4]

try:
    pods_data = json.load(open(pods_path, encoding="utf-8"))
except Exception:
    pods_data = {}
try:
    containers_data = json.load(open(containers_path, encoding="utf-8"))
except Exception:
    containers_data = {}

target_pods = {}
for pod in pods_data.get("items", []):
    meta = pod.get("metadata") or {}
    labels = pod.get("labels") or {}
    if meta.get("namespace") != namespace:
        continue
    if pod.get("state") != "SANDBOX_READY":
        continue
    if labels.get("seedemu.io/workload") != "seedemu":
        continue
    role = str(labels.get("seedemu.io/role", ""))
    if role not in role_set:
        continue
    name = str(meta.get("name") or labels.get("io.kubernetes.pod.name") or "")
    if not name:
        continue
    target_pods[name] = {
        "role": role,
        "asn": str(labels.get("seedemu.io/asn", "")),
    }

containers_by_pod = {}
for container in containers_data.get("containers", []):
    labels = container.get("labels") or {}
    if labels.get("io.kubernetes.pod.namespace") != namespace:
        continue
    pod_name = str(labels.get("io.kubernetes.pod.name", ""))
    if pod_name not in target_pods:
        continue
    name = str(labels.get("io.kubernetes.container.name") or (container.get("metadata") or {}).get("name") or "")
    item = {
        "id": str(container.get("id", "")),
        "pod": pod_name,
        "container": name,
        "role": target_pods[pod_name]["role"],
        "asn": target_pods[pod_name]["asn"],
    }
    if not item["id"]:
        continue
    containers_by_pod.setdefault(pod_name, []).append(item)

rows = []
for pod_name, items in containers_by_pod.items():
    items.sort(key=lambda item: (item["container"] != "main", item["container"]))
    item = items[0]
    try:
        asn_key = int(item["asn"] or 0)
    except ValueError:
        asn_key = 0
    rows.append((asn_key, pod_name, item["id"], item["role"]))

with open(output_path, "w", encoding="utf-8") as handle:
    for _, pod_name, cid, role in sorted(rows):
        handle.write(f"{cid}\t{pod_name}\t{role}\n")
PY
}

attempt=0
: > "${TARGETS}"
while [ "${attempt}" -lt "${CID_ENUM_RETRIES}" ]; do
    attempt=$((attempt + 1))
    collect_targets
    total="$(wc -l < "${TARGETS}" | tr -d ' ')"
    if [ "${EXPECTED_TOTAL}" -le 0 ] || [ "${total}" -ge "${EXPECTED_TOTAL}" ]; then
        break
    fi
    echo "waiting_for_containers attempt=${attempt} total=${total} expected=${EXPECTED_TOTAL}"
    sleep "${CID_ENUM_SLEEP_SECONDS}"
done

total="$(wc -l < "${TARGETS}" | tr -d ' ')"
if [ "${EXPECTED_TOTAL}" -gt 0 ] && [ "${total}" -ne "${EXPECTED_TOTAL}" ]; then
    load_average="$(cat /proc/loadavg)"
    echo "SUMMARY total=${total} skipped=0 started=0 failed=1 missing_pid=0 expected=${EXPECTED_TOTAL} bird_count=0 load=\"${load_average}\""
    echo "Container enumeration did not match expected target count."
    cat "${WORK_DIR}/crictl-pods.err" "${WORK_DIR}/crictl-ps.err" 2>/dev/null || true
    exit 2
fi

load_is_below_threshold() {
    awk -v load_value="$1" -v limit="${LOAD_THRESHOLD}" 'BEGIN { exit !(load_value < limit) }'
}

wait_for_load() {
    local load_average
    local started_at
    local now
    local waited
    started_at="$(date +%s)"
    while true; do
        load_average="$(awk '{print $1}' /proc/loadavg)"
        if load_is_below_threshold "${load_average}"; then
            return 0
        fi
        now="$(date +%s)"
        waited=$((now - started_at))
        if [ "${LOAD_MAX_WAIT_SECONDS}" -gt 0 ] 2>/dev/null &&
           [ "${waited}" -ge "${LOAD_MAX_WAIT_SECONDS}" ]; then
            echo "cooldown_max_wait_reached load=${load_average} threshold=${LOAD_THRESHOLD} waited=${waited}; continuing one small batch"
            return 0
        fi
        echo "cooldown load=${load_average} threshold=${LOAD_THRESHOLD} sleep=${LOAD_CHECK_INTERVAL_SECONDS}"
        sleep "${LOAD_CHECK_INTERVAL_SECONDS}"
    done
}

target_state() {
    local cid="$1"
    local runtime_dir="/run/k3s/containerd/io.containerd.runtime.v2.task/k8s.io"
    local pidfile="${runtime_dir}/${cid}/init.pid"
    local pid
    if [ ! -r "${pidfile}" ]; then
        echo "missing_pid"
        return 0
    fi
    pid="$(cat "${pidfile}" 2>/dev/null || true)"
    if [ -z "${pid}" ]; then
        echo "missing_pid"
        return 0
    fi
    if timeout 5 nsenter -t "${pid}" -m -u -i -n -p -- sh -lc 'ps -e 2>/dev/null | awk '\''$NF == "bird" {found=1} END {exit found ? 0 : 1}'\''' >/dev/null 2>&1; then
        echo "running"
        return 0
    fi
    echo "not_running"
}

cat > "${START_ONE}" <<'EOS'
#!/usr/bin/env bash
set -u

cid="$1"
pod="$2"
role="$3"
runtime_dir="/run/k3s/containerd/io.containerd.runtime.v2.task/k8s.io"
pidfile="${runtime_dir}/${cid}/init.pid"
if [ ! -r "${pidfile}" ]; then
    echo "missing_pid ${pod} ${cid}"
    exit 0
fi

pid="$(cat "${pidfile}" 2>/dev/null || true)"
if [ -z "${pid}" ]; then
    echo "missing_pid ${pod} ${cid}"
    exit 0
fi

result="$(timeout "${ONE_TIMEOUT_SECONDS:-120}" nsenter -t "${pid}" -m -u -i -n -p -- sh -lc '
if ps -e 2>/dev/null | awk '\''$NF == "bird" {found=1} END {exit found ? 0 : 1}'\''; then
    echo skipped
    exit 0
fi
rm -f /run/bird/bird.ctl /run/bird/bird.pid /var/run/bird.ctl /var/run/bird.pid /run/bird/*.ctl /run/bird/*.pid /var/run/bird/*.ctl /var/run/bird/*.pid 2>/dev/null || true
mkdir -p /etc/bird/conf /run/bird
if [ ! -f /etc/bird/conf/kernel.conf ] && [ -f /etc/bird/conf/kernel.conf.disabled-before-start-kernel ]; then
    cp -f /etc/bird/conf/kernel.conf.disabled-before-start-kernel /etc/bird/conf/kernel.conf
fi
if [ -f /etc/bird/conf/kernel.conf ]; then
    if grep -F '\''include "/etc/bird/conf/*.conf";'\'' /etc/bird/bird.conf >/dev/null 2>&1; then
        sed -i '\''\#^include "/etc/bird/conf/kernel.conf";$#d'\'' /etc/bird/bird.conf
    elif ! grep -F '\''include "/etc/bird/conf/kernel.conf";'\'' /etc/bird/bird.conf >/dev/null 2>&1; then
        printf '\''\ninclude "/etc/bird/conf/kernel.conf";\n'\'' >> /etc/bird/bird.conf
    fi
elif ! grep -F '\''include "/etc/bird/conf/*.conf";'\'' /etc/bird/bird.conf >/dev/null 2>&1; then
    sed -i '\''\#^include "/etc/bird/conf/kernel.conf";$#d'\'' /etc/bird/bird.conf
fi
bird >/tmp/seedemu-bird.log 2>&1 &
echo started
' 2>/dev/null || true)"

case "${result}" in
    skipped) echo "skipped ${pod} ${cid} ${role}" ;;
    started) echo "started ${pod} ${cid} ${role}" ;;
    *) echo "failed ${pod} ${cid} ${role}" ;;
esac
EOS
chmod +x "${START_ONE}"

echo "node=$(hostname) namespace=${NAMESPACE} total=${total} expected=${EXPECTED_TOTAL} serial=1 one_timeout=${ONE_TIMEOUT_SECONDS:-120} load_threshold=${LOAD_THRESHOLD} load_max_wait=${LOAD_MAX_WAIT_SECONDS} start_batch_size=${START_BATCH_SIZE} start_batch_cooldown=${START_BATCH_COOLDOWN_SECONDS}"
: > "${OUT}"
count=0
started_batch=0
while IFS=$'\t' read -r cid pod role; do
    [ -n "${cid}" ] || continue
    state="$(target_state "${cid}")"
    case "${state}" in
        running)
            printf 'skipped %s %s %s\n' "${pod}" "${cid}" "${role}" >> "${OUT}"
            count=$((count + 1))
            continue
            ;;
        missing_pid)
            printf 'missing_pid %s %s\n' "${pod}" "${cid}" >> "${OUT}"
            count=$((count + 1))
            continue
            ;;
    esac
    line="$("${START_ONE}" "${cid}" "${pod}" "${role}")"
    printf '%s\n' "${line}" >> "${OUT}"
    case "${line}" in
        started*)
            started_batch=$((started_batch + 1))
            ;;
    esac
    count=$((count + 1))
    if [ $((count % 50)) -eq 0 ] || [ "${count}" -eq "${total}" ]; then
        echo "progress started_or_skipped=${count}/${total} load=\"$(cat /proc/loadavg)\""
    fi
    if [ "${START_BATCH_SIZE}" -gt 0 ] 2>/dev/null &&
       [ "${started_batch}" -ge "${START_BATCH_SIZE}" ]; then
        echo "batch_cooldown started_batch=${started_batch} sleep=${START_BATCH_COOLDOWN_SECONDS} load=\"$(cat /proc/loadavg)\""
        started_batch=0
        sleep "${START_BATCH_COOLDOWN_SECONDS}"
    fi
    sleep "${START_SLEEP_SECONDS:-0}"
done < "${TARGETS}"

echo "post_start_wait load_threshold=${LOAD_THRESHOLD} load_max_wait=${LOAD_MAX_WAIT_SECONDS} load=\"$(cat /proc/loadavg)\""
wait_for_load

started="$(grep -c '^started ' "${OUT}" || true)"
skipped="$(grep -c '^skipped ' "${OUT}" || true)"
failed="$(grep -c '^failed ' "${OUT}" || true)"
missing_pid="$(grep -c '^missing_pid ' "${OUT}" || true)"
bird_count="$(pgrep -x bird | wc -l | tr -d ' ')"
load_average="$(cat /proc/loadavg)"

echo "SUMMARY total=${total} skipped=${skipped} started=${started} failed=${failed} missing_pid=${missing_pid} expected=${EXPECTED_TOTAL} bird_count=${bird_count} load=\"${load_average}\""
if [ "${failed}" -gt 0 ] || [ "${missing_pid}" -gt 0 ]; then
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


@dataclass
class PodTarget:
    name: str
    asn: str
    role: str
    node: str


def nowUtc() -> str:
    """Return a compact UTC timestamp for logs and summaries."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def run(cmd: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    """Run a local command and capture text output."""
    try:
        return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        detail = stderr or f"command timed out after {timeout} seconds"
        return subprocess.CompletedProcess(cmd, 124, stdout, detail)


def kubectl(namespace: str, kubeconfig: str, args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    """Run one lightweight kubectl command for target discovery only."""
    command = ["kubectl"]
    if kubeconfig:
        command.extend(["--kubeconfig", kubeconfig])
    command.extend(["-n", namespace, *args])
    return run(command, timeout=timeout)


def collectTargets(namespace: str, kubeconfig: str, pod_list_timeout: int) -> list[PodTarget]:
    """Collect running router-like target pods once for expected node counts."""
    result = kubectl(
        namespace,
        kubeconfig,
        ["get", "pods", "-o", "json", f"--request-timeout={pod_list_timeout}s"],
        timeout=pod_list_timeout + 30,
    )
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


def writeTargets(artifact_dir: Path, targets: list[PodTarget]) -> None:
    """Write target files compatible with the earlier kubectl-based helper."""
    payload = json.dumps([asdict(target) for target in targets], indent=2)
    for filename in ("start_bird_targets.json", "bird0130_targets.json"):
        (artifact_dir / filename).write_text(payload, encoding="utf-8")


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
            if key in {"total", "skipped", "started", "failed", "missing_pid", "expected", "bird_count"}:
                result[key] = int(value)
            else:
                result[key] = value
        return result
    return {"raw": "", "failed": 1, "failure_reason": "missing_summary"}


def runNode(
    node: Node,
    namespace: str,
    expected_total: int,
    start_sleep: float,
    start_batch_size: int,
    start_batch_cooldown: int,
    one_timeout: int,
    timeout: int,
    load_threshold: float,
    load_check_interval: int,
    load_max_wait: int,
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
        f"EXPECTED_TOTAL={expected_total}",
        f"START_SLEEP_SECONDS={start_sleep}",
        f"START_BATCH_SIZE={start_batch_size}",
        f"START_BATCH_COOLDOWN_SECONDS={start_batch_cooldown}",
        f"ONE_TIMEOUT_SECONDS={one_timeout}",
        f"LOAD_THRESHOLD={load_threshold}",
        f"LOAD_CHECK_INTERVAL_SECONDS={load_check_interval}",
        f"LOAD_MAX_WAIT_SECONDS={load_max_wait}",
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
    parser.add_argument("--kubeconfig", default="")
    parser.add_argument("--pod-list-timeout-seconds", type=int, default=60)
    parser.add_argument("--parallel-per-node", type=int, default=1)
    parser.add_argument("--node-concurrency", type=int, default=0)
    parser.add_argument("--start-delay-seconds", type=float, default=0.08)
    parser.add_argument("--start-batch-size", type=int, default=20)
    parser.add_argument("--start-batch-cooldown-seconds", type=int, default=60)
    parser.add_argument("--one-timeout-seconds", type=int, default=120)
    parser.add_argument("--node-timeout-seconds", type=int, default=36000)
    parser.add_argument("--load-threshold", type=float, default=50.0)
    parser.add_argument("--load-check-interval-seconds", type=int, default=30)
    parser.add_argument("--load-max-wait-seconds", type=int, default=600)
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
        f"node_concurrency={node_concurrency} serial_per_node=1 "
        f"requested_parallel_per_node={args.parallel_per_node} "
        f"load_threshold={args.load_threshold}",
        flush=True,
    )

    targets = collectTargets(args.namespace, args.kubeconfig, args.pod_list_timeout_seconds)
    writeTargets(artifact_dir, targets)
    expected_by_node = dict(Counter(target.node for target in targets if target.node))
    print(f"[{nowUtc()}] discovered {len(targets)} running router-like targets", flush=True)
    for node in nodes:
        print(f"[{nowUtc()}] target_count node={node.name} expected={expected_by_node.get(node.name, 0)}", flush=True)

    results: list[dict[str, object]] = []
    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=node_concurrency) as pool:
        futures = {
            pool.submit(
                runNode,
                node,
                args.namespace,
                expected_by_node.get(node.name, 0),
                args.start_delay_seconds,
                args.start_batch_size,
                args.start_batch_cooldown_seconds,
                args.one_timeout_seconds,
                args.node_timeout_seconds,
                args.load_threshold,
                args.load_check_interval_seconds,
                args.load_max_wait_seconds,
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
        expected = expected_by_node.get(str(item["node"]), 0)
        item["summary"]["expected"] = expected

    total_expected = sum(expected_by_node.values())
    total_containers = sum(int(item["summary"].get("total", 0)) for item in results)
    total_started = sum(int(item["summary"].get("started", 0)) for item in results)
    total_skipped = sum(int(item["summary"].get("skipped", 0)) for item in results)
    total_birds = sum(int(item["summary"].get("bird_count", 0)) for item in results)
    failures = [
        item
        for item in results
        if int(item["returncode"]) != 0
        or int(item["summary"].get("failed", 0)) > 0
        or int(item["summary"].get("missing_pid", 0)) > 0
        or int(item["summary"].get("total", 0)) != expected_by_node.get(str(item["node"]), 0)
    ]

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "namespace": args.namespace,
        "strategy": "node-local-nsenter-serial-per-node",
        "status": "PASS" if not failures else "FAIL",
        "duration_seconds": round(time.time() - start_time, 2),
        "parameters": {
            "node_concurrency": node_concurrency,
            "serial_per_node": True,
            "requested_parallel_per_node": args.parallel_per_node,
            "start_delay_seconds": args.start_delay_seconds,
            "start_batch_size": args.start_batch_size,
            "start_batch_cooldown_seconds": args.start_batch_cooldown_seconds,
            "one_timeout_seconds": args.one_timeout_seconds,
            "node_timeout_seconds": args.node_timeout_seconds,
            "load_threshold": args.load_threshold,
            "load_check_interval_seconds": args.load_check_interval_seconds,
            "load_max_wait_seconds": args.load_max_wait_seconds,
        },
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
        "started": total_started,
        "skipped": total_skipped,
        "bird_processes": total_birds,
    }
    if failures:
        summary["failure_reason"] = "one_or_more_nodes_failed"

    (artifact_dir / "start_bird_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(
        f"[{nowUtc()}] completed status={summary['status']} targets={total_containers} "
        f"expected={total_expected} started={total_started} skipped={total_skipped} "
        f"duration={summary['duration_seconds']}",
        flush=True,
    )
    return 0 if not failures else 10


if __name__ == "__main__":
    raise SystemExit(main())
