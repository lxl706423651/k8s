#!/usr/bin/env python3
"""Run a B62 BIRD stage against native Docker containers over VM SSH.

The shell entrypoints own all tuning parameters.  This helper only adapts the
same serial-per-node start-bird/start-kernel operations to containers carrying
the native runtime's exact experiment label.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import shlex
import subprocess
import time

import yaml


REMOTE_SCRIPT = r'''#!/usr/bin/env bash
set -u

WORK_DIR="$(mktemp -d /tmp/seedemu-b62-docker-bird.XXXXXX)"
trap 'rm -rf "${WORK_DIR}"' EXIT
TARGETS="${WORK_DIR}/targets.txt"
OUT="${WORK_DIR}/out.txt"
KERNEL_SCRIPT="${WORK_DIR}/kernel.sh"

docker ps \
    --filter "label=seedemu.native.experiment=${EXPERIMENT_ID}" \
    --format '{{.ID}} {{.Names}} {{.Label "seedemu.native.node"}}' \
    | sort -k3,3V > "${TARGETS}"

total="$(wc -l < "${TARGETS}" | tr -d ' ')"
if [ "${total}" -ne "${EXPECTED_TOTAL}" ]; then
    echo "SUMMARY total=${total} started=0 skipped=0 switched=0 failed=1 missing_pid=0 expected=${EXPECTED_TOTAL} bird_count=0 load=\"$(cat /proc/loadavg)\""
    echo "Docker target enumeration did not match the native deployment plan." >&2
    exit 2
fi

load_is_below_threshold() {
    awk -v load_value="$1" -v limit="${LOAD_THRESHOLD}" 'BEGIN { exit !(load_value < limit) }'
}

wait_for_load() {
    local load_average started_at now waited
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
            echo "cooldown_max_wait_reached load=${load_average} threshold=${LOAD_THRESHOLD} waited=${waited}; continuing"
            return 0
        fi
        echo "cooldown load=${load_average} threshold=${LOAD_THRESHOLD} sleep=${LOAD_CHECK_INTERVAL_SECONDS}"
        sleep "${LOAD_CHECK_INTERVAL_SECONDS}"
    done
}

cat > "${KERNEL_SCRIPT}" <<'EOS'
#!/bin/sh
set -eu
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
timeout "${BIRDC_TIMEOUT_SECONDS}" birdc configure >/tmp/seedemu-birdc-configure.log 2>&1
timeout "${BIRDC_TIMEOUT_SECONDS}" birdc show protocols >/tmp/seedemu-birdc-show-protocols.log 2>&1
kernel_protocol="$(awk '$2 == "Kernel" { print $1; exit }' /tmp/seedemu-birdc-show-protocols.log)"
if [ -z "${kernel_protocol}" ]; then
    echo "unable to resolve the configured BIRD Kernel protocol" >&2
    cat /tmp/seedemu-birdc-show-protocols.log >&2
    exit 1
fi
timeout "${BIRDC_TIMEOUT_SECONDS}" birdc "reload ${kernel_protocol}" >/tmp/seedemu-birdc-reload-kernel.log 2>&1
EOS
chmod +x "${KERNEL_SCRIPT}"

echo "node=$(hostname) runtime=docker stage=${STAGE} total=${total} expected=${EXPECTED_TOTAL} serial=1 load_threshold=${LOAD_THRESHOLD} load_max_wait=${LOAD_MAX_WAIT_SECONDS}"
: > "${OUT}"
count=0
batch_count=0
while read -r cid container node_id; do
    [ -n "${cid}" ] || continue
    if [ "${STAGE}" = "start-bird" ]; then
        if docker exec "${cid}" /bin/sh -c 'ps -eo stat=,comm= | awk '\''$2 == "bird" && $1 !~ /^Z/ {found=1} END {exit found ? 0 : 1}'\''' >/dev/null 2>&1; then
            echo "skipped ${container} ${cid} ${node_id}" >> "${OUT}"
        elif docker exec "${cid}" /bin/sh -c 'rm -f /run/bird/bird.ctl /run/bird/bird.pid /var/run/bird.ctl /var/run/bird.pid /run/bird/*.ctl /run/bird/*.pid /var/run/bird/*.ctl /var/run/bird/*.pid 2>/dev/null || true; mkdir -p /etc/bird/conf /run/bird' >/dev/null 2>&1 &&
             timeout "${ONE_TIMEOUT_SECONDS}" docker exec -d "${cid}" /bin/sh -c 'exec bird >/tmp/seedemu-bird.log 2>&1'; then
            echo "started ${container} ${cid} ${node_id}" >> "${OUT}"
            batch_count=$((batch_count + 1))
        else
            echo "failed ${container} ${cid} ${node_id}" >> "${OUT}"
        fi
        batch_size="${START_BATCH_SIZE}"
        batch_cooldown="${START_BATCH_COOLDOWN_SECONDS}"
        sleep_seconds="${ACTION_SLEEP_SECONDS}"
    else
        output="$(
            timeout "${ONE_TIMEOUT_SECONDS}" docker exec -i \
                -e EXPORT_MODE="${EXPORT_MODE}" \
                -e SCAN_BASE_SECONDS="${SCAN_BASE_SECONDS}" \
                -e SCAN_JITTER_SECONDS="${SCAN_JITTER_SECONDS}" \
                -e BIRDC_TIMEOUT_SECONDS="${BIRDC_TIMEOUT_SECONDS}" \
                "${cid}" /bin/sh -s \
                < "${KERNEL_SCRIPT}" 2>&1
        )"
        rc=$?
        if [ "${rc}" -eq 0 ]; then
            echo "switched ${container} ${cid} ${node_id}" >> "${OUT}"
            batch_count=$((batch_count + 1))
        else
            printf 'failed %s %s %s rc=%s %s\n' "${container}" "${cid}" "${node_id}" "${rc}" "$(printf '%s' "${output}" | tr '\n' ' ' | cut -c1-240)" >> "${OUT}"
        fi
        batch_size="${SWITCH_BATCH_SIZE}"
        batch_cooldown="${SWITCH_BATCH_COOLDOWN_SECONDS}"
        sleep_seconds="${ACTION_SLEEP_SECONDS}"
    fi
    count=$((count + 1))
    if [ $((count % 50)) -eq 0 ] || [ "${count}" -eq "${total}" ]; then
        echo "progress attempted=${count}/${total} load=\"$(cat /proc/loadavg)\""
    fi
    if [ "${batch_size}" -gt 0 ] 2>/dev/null && [ "${batch_count}" -ge "${batch_size}" ]; then
        echo "batch_cooldown count=${batch_count} sleep=${batch_cooldown} load=\"$(cat /proc/loadavg)\""
        batch_count=0
        sleep "${batch_cooldown}"
    fi
    sleep "${sleep_seconds}"
done < "${TARGETS}"

echo "post_action_wait load_threshold=${LOAD_THRESHOLD} load_max_wait=${LOAD_MAX_WAIT_SECONDS} load=\"$(cat /proc/loadavg)\""
wait_for_load

started="$(grep -c '^started ' "${OUT}" || true)"
skipped="$(grep -c '^skipped ' "${OUT}" || true)"
switched="$(grep -c '^switched ' "${OUT}" || true)"
failed="$(grep -c '^failed ' "${OUT}" || true)"
bird_count=0
while read -r cid container node_id; do
    if docker exec "${cid}" /bin/sh -c 'ps -eo stat=,comm= | awk '\''$2 == "bird" && $1 !~ /^Z/ {found=1} END {exit found ? 0 : 1}'\''' >/dev/null 2>&1; then
        bird_count=$((bird_count + 1))
    fi
done < "${TARGETS}"

echo "SUMMARY total=${total} started=${started} skipped=${skipped} switched=${switched} failed=${failed} missing_pid=0 expected=${EXPECTED_TOTAL} bird_count=${bird_count} load=\"$(cat /proc/loadavg)\""
if [ "${failed}" -gt 0 ]; then
    grep '^failed ' "${OUT}" | head -20 >&2 || true
    exit 2
fi
'''


@dataclass
class Node:
    """One native execution VM and its exact planned target count."""

    name: str
    ip: str
    user: str
    key: str
    expected_targets: int


def nowUtc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def loadNodes(path: Path) -> list[Node]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    nodes: list[Node] = []
    for item in data.get("nodes", []):
        ssh = item.get("ssh", {}) or {}
        nodes.append(
            Node(
                name=str(item["name"]),
                ip=str(item.get("managementIp") or item.get("ip")),
                user=str(ssh.get("user") or "ubuntu"),
                key=str(Path(str(ssh.get("key") or "~/.ssh/id_ed25519")).expanduser()),
                expected_targets=int(item.get("expectedTargets") or 0),
            )
        )
    if not nodes or any(node.expected_targets <= 0 for node in nodes):
        raise ValueError(f"inventory must contain positive expectedTargets: {path}")
    return nodes


def parseSummary(stdout: str) -> dict[str, object]:
    for line in reversed(stdout.splitlines()):
        if not line.startswith("SUMMARY "):
            continue
        parsed: dict[str, object] = {"raw": line}
        for token in shlex.split(line[8:]):
            if "=" not in token:
                continue
            key, value = token.split("=", 1)
            if key in {"total", "started", "skipped", "switched", "failed", "missing_pid", "expected", "bird_count"}:
                parsed[key] = int(value)
            else:
                parsed[key] = value
        return parsed
    return {"failed": 1, "failure_reason": "missing_summary"}


def runNode(node: Node, args: argparse.Namespace) -> dict[str, object]:
    command = [
        "ssh", "-i", node.key,
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=60",
        f"{node.user}@{node.ip}",
        "sudo", "-n", "env",
        f"STAGE={args.stage}",
        f"EXPERIMENT_ID={args.experiment_id}",
        f"EXPECTED_TOTAL={node.expected_targets}",
        f"ACTION_SLEEP_SECONDS={args.action_sleep_seconds}",
        f"START_BATCH_SIZE={args.start_batch_size}",
        f"START_BATCH_COOLDOWN_SECONDS={args.start_batch_cooldown_seconds}",
        f"SWITCH_BATCH_SIZE={args.switch_batch_size}",
        f"SWITCH_BATCH_COOLDOWN_SECONDS={args.switch_batch_cooldown_seconds}",
        f"ONE_TIMEOUT_SECONDS={args.one_timeout_seconds}",
        f"LOAD_THRESHOLD={args.load_threshold}",
        f"LOAD_CHECK_INTERVAL_SECONDS={args.load_check_interval_seconds}",
        f"LOAD_MAX_WAIT_SECONDS={args.load_max_wait_seconds}",
        f"EXPORT_MODE={args.export_mode}",
        f"SCAN_BASE_SECONDS={args.scan_base_seconds}",
        f"SCAN_JITTER_SECONDS={args.scan_jitter_seconds}",
        f"BIRDC_TIMEOUT_SECONDS={args.birdc_timeout_seconds}",
        "bash", "-s",
    ]
    started_at = time.monotonic()
    try:
        result = subprocess.run(
            command,
            input=REMOTE_SCRIPT,
            text=True,
            capture_output=True,
            timeout=args.node_timeout_seconds,
        )
        return {
            "node": node.name,
            "ip": node.ip,
            "expected": node.expected_targets,
            "returncode": result.returncode,
            "duration_seconds": round(time.monotonic() - started_at, 2),
            "stdout": result.stdout,
            "stderr": result.stderr,
            "summary": parseSummary(result.stdout),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "node": node.name,
            "ip": node.ip,
            "expected": node.expected_targets,
            "returncode": 124,
            "duration_seconds": round(time.monotonic() - started_at, 2),
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or f"timeout after {args.node_timeout_seconds}s",
            "summary": {"failed": 1, "failure_reason": "ssh_timeout"},
        }


def parseArgs() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("start-bird", "start-kernel"), required=True)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--node-concurrency", type=int, default=0)
    parser.add_argument("--action-sleep-seconds", type=float, required=True)
    parser.add_argument("--start-batch-size", type=int, default=0)
    parser.add_argument("--start-batch-cooldown-seconds", type=int, default=0)
    parser.add_argument("--switch-batch-size", type=int, default=0)
    parser.add_argument("--switch-batch-cooldown-seconds", type=int, default=0)
    parser.add_argument("--one-timeout-seconds", type=int, required=True)
    parser.add_argument("--node-timeout-seconds", type=int, required=True)
    parser.add_argument("--load-threshold", type=float, required=True)
    parser.add_argument("--load-check-interval-seconds", type=int, required=True)
    parser.add_argument("--load-max-wait-seconds", type=int, required=True)
    parser.add_argument("--export-mode", default="all")
    parser.add_argument("--scan-base-seconds", type=int, default=6000)
    parser.add_argument("--scan-jitter-seconds", type=int, default=120)
    parser.add_argument("--birdc-timeout-seconds", type=int, default=10)
    return parser.parse_args()


def main() -> int:
    args = parseArgs()
    args.artifact_dir.mkdir(parents=True, exist_ok=True)
    nodes = loadNodes(args.inventory)
    concurrency = len(nodes) if args.node_concurrency <= 0 else min(args.node_concurrency, len(nodes))
    started_at = time.monotonic()
    print(f"[{nowUtc()}] runtime=docker stage={args.stage} nodes={len(nodes)} concurrency={concurrency}", flush=True)

    results: list[dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(runNode, node, args): node for node in nodes}
        for future in as_completed(futures):
            item = future.result()
            results.append(item)
            log_name = f"{args.stage.replace('-', '_')}_node_local_{item['node']}.log"
            (args.artifact_dir / log_name).write_text(
                "\n".join(
                    [
                        f"node={item['node']}",
                        f"ip={item['ip']}",
                        f"returncode={item['returncode']}",
                        f"duration_seconds={item['duration_seconds']}",
                        "--- stdout ---",
                        str(item["stdout"]),
                        "--- stderr ---",
                        str(item["stderr"]),
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            print(
                f"[{nowUtc()}] node={item['node']} rc={item['returncode']} duration={item['duration_seconds']} summary={item['summary']}",
                flush=True,
            )

    action_key = "switched" if args.stage == "start-kernel" else "started"
    failures = []
    for item in results:
        remote = item["summary"]
        expected = int(item["expected"])
        processed = int(remote.get("switched", 0)) if args.stage == "start-kernel" else int(remote.get("started", 0)) + int(remote.get("skipped", 0))
        if int(item["returncode"]) != 0 or int(remote.get("failed", 0)) != 0 or int(remote.get("total", 0)) != expected or processed != expected:
            failures.append(item)

    expected_total = sum(int(item["expected"]) for item in results)
    summary: dict[str, object] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runtime": "docker",
        "strategy": "node-local-docker-exec-serial-per-node",
        "stage": args.stage,
        "status": "FAIL" if failures else "PASS",
        "duration_seconds": round(time.monotonic() - started_at, 2),
        "parameters": {
            "node_concurrency": concurrency,
            "serial_per_node": True,
            "action_sleep_seconds": args.action_sleep_seconds,
            "start_batch_size": args.start_batch_size,
            "start_batch_cooldown_seconds": args.start_batch_cooldown_seconds,
            "switch_batch_size": args.switch_batch_size,
            "switch_batch_cooldown_seconds": args.switch_batch_cooldown_seconds,
            "one_timeout_seconds": args.one_timeout_seconds,
            "node_timeout_seconds": args.node_timeout_seconds,
            "load_threshold": args.load_threshold,
            "load_check_interval_seconds": args.load_check_interval_seconds,
            "load_max_wait_seconds": args.load_max_wait_seconds,
            "export_mode": args.export_mode,
            "scan_base_seconds": args.scan_base_seconds,
            "scan_jitter_seconds": args.scan_jitter_seconds,
            "birdc_timeout_seconds": args.birdc_timeout_seconds,
        },
        "nodes": [
            {
                "node": item["node"],
                "ip": item["ip"],
                "returncode": item["returncode"],
                "duration_seconds": item["duration_seconds"],
                "summary": item["summary"],
            }
            for item in sorted(results, key=lambda row: str(row["node"]))
        ],
        "targets": sum(int(item["summary"].get("total", 0)) for item in results),
        "expected_targets": expected_total,
        action_key: sum(int(item["summary"].get(action_key, 0)) for item in results),
    }
    if args.stage == "start-bird":
        summary["skipped"] = sum(int(item["summary"].get("skipped", 0)) for item in results)
    if failures:
        summary["failure_reason"] = "one_or_more_nodes_failed"
    filename = "start_bird_kernel_summary.json" if args.stage == "start-kernel" else "start_bird_summary.json"
    (args.artifact_dir / filename).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"[{nowUtc()}] completed status={summary['status']} expected={expected_total} duration={summary['duration_seconds']}", flush=True)
    return 0 if not failures else 41


if __name__ == "__main__":
    raise SystemExit(main())
