#!/usr/bin/env python3
"""Apply identical in-container CPU load through one node-local SSH session.

The Kubernetes API is used once to freeze the expected Running Pod set.  Each
K3s node then enumerates its local containerd state and enters every main
container with nsenter.  This avoids one kubelet-proxied ``kubectl exec`` per
Pod while preserving the container, network namespace, kernel and placement.
"""
from __future__ import annotations

import argparse
import concurrent.futures
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

import yaml

from applyPodCpuLoad import LOAD_PROGRAM, podList


REMOTE_SCRIPT = r"""#!/usr/bin/env bash
set -u

WORK_DIR="$(mktemp -d /tmp/seedemu-pod-load.XXXXXX)"
trap 'rm -rf "${WORK_DIR}"' EXIT
PODS_JSON="${WORK_DIR}/pods.json"
CONTAINERS_JSON="${WORK_DIR}/containers.json"
TARGETS="${WORK_DIR}/targets.tsv"
RESULTS="${WORK_DIR}/results.tsv"
ACTION="${WORK_DIR}/action.sh"

crictl pods -o json >"${PODS_JSON}"
crictl ps -o json >"${CONTAINERS_JSON}"
python3 - "${PODS_JSON}" "${CONTAINERS_JSON}" "${TARGETS}" <<'PY'
import json, os, sys
namespace = os.environ["NAMESPACE"]
pods = json.load(open(sys.argv[1], encoding="utf-8"))
containers = json.load(open(sys.argv[2], encoding="utf-8"))
wanted = set()
for pod in pods.get("items", []):
    meta = pod.get("metadata") or {}
    labels = pod.get("labels") or {}
    if (meta.get("namespace") == namespace and pod.get("state") == "SANDBOX_READY"
            and labels.get("seedemu.io/workload") == "seedemu"):
        name = str(meta.get("name") or labels.get("io.kubernetes.pod.name") or "")
        if name:
            wanted.add(name)
by_pod = {}
for container in containers.get("containers", []):
    labels = container.get("labels") or {}
    if labels.get("io.kubernetes.pod.namespace") != namespace:
        continue
    pod = str(labels.get("io.kubernetes.pod.name") or "")
    if pod not in wanted:
        continue
    name = str(labels.get("io.kubernetes.container.name")
               or (container.get("metadata") or {}).get("name") or "")
    cid = str(container.get("id") or "")
    if cid:
        by_pod.setdefault(pod, []).append((name != "main", name, cid))
with open(sys.argv[3], "w", encoding="utf-8") as handle:
    for pod, values in sorted(by_pod.items()):
        handle.write(f"{sorted(values)[0][2]}\t{pod}\n")
PY

total="$(wc -l <"${TARGETS}" | tr -d ' ')"
if [ "${total}" -ne "${EXPECTED_TOTAL}" ]; then
    echo "SUMMARY total=${total} ok=0 failed=1 missing_pid=0 expected=${EXPECTED_TOTAL}"
    exit 2
fi

cat >"${ACTION}" <<'EOS'
#!/bin/sh
set -u
if [ -s /tmp/seedemu-exogenous-load.pid ]; then
    kill "$(cat /tmp/seedemu-exogenous-load.pid)" 2>/dev/null || true
    rm -f /tmp/seedemu-exogenous-load.pid
fi
if awk -v value="${MILLICORES}" 'BEGIN { exit !(value > 0) }'; then
    python_code="import base64;exec(base64.b64decode('${LOAD_PROGRAM_B64}'))"
    nohup python3 -c "${python_code}" "${MILLICORES}" \
        >/tmp/seedemu-exogenous-load.log 2>&1 &
    echo "$!" >/tmp/seedemu-exogenous-load.pid
    sleep 0.05
    kill -0 "$(cat /tmp/seedemu-exogenous-load.pid)"
fi
EOS

: >"${RESULTS}"
while IFS=$'\t' read -r cid pod; do
    [ -n "${cid}" ] || continue
    pidfile="/run/k3s/containerd/io.containerd.runtime.v2.task/k8s.io/${cid}/init.pid"
    if [ ! -r "${pidfile}" ]; then
        printf 'missing_pid\t%s\t%s\n' "${pod}" "${cid}" >>"${RESULTS}"
        continue
    fi
    pid="$(cat "${pidfile}" 2>/dev/null || true)"
    if [ -z "${pid}" ]; then
        printf 'missing_pid\t%s\t%s\n' "${pod}" "${cid}" >>"${RESULTS}"
        continue
    fi
    output="$(timeout "${ONE_TIMEOUT_SECONDS}" nsenter -t "${pid}" -m -u -i -n -p -- sh -s <"${ACTION}" 2>&1)"
    rc=$?
    if [ "${rc}" -eq 0 ]; then
        printf 'ok\t%s\t%s\n' "${pod}" "${cid}" >>"${RESULTS}"
    else
        clean="$(printf '%s' "${output}" | tr '\t\n' '  ' | cut -c1-200)"
        printf 'failed\t%s\t%s\trc=%s %s\n' "${pod}" "${cid}" "${rc}" "${clean}" >>"${RESULTS}"
    fi
done <"${TARGETS}"

cat "${RESULTS}"
ok="$(awk -F '\t' '$1 == "ok" {n++} END {print n+0}' "${RESULTS}")"
failed="$(awk -F '\t' '$1 == "failed" {n++} END {print n+0}' "${RESULTS}")"
missing="$(awk -F '\t' '$1 == "missing_pid" {n++} END {print n+0}' "${RESULTS}")"
echo "SUMMARY total=${total} ok=${ok} failed=${failed} missing_pid=${missing} expected=${EXPECTED_TOTAL}"
[ "${ok}" -eq "${EXPECTED_TOTAL}" ] && [ "${failed}" -eq 0 ] && [ "${missing}" -eq 0 ]
"""


def loadNodes(path: Path) -> list[dict[str, str]]:
    data = yaml.safe_load(path.read_text()) or {}
    result = []
    for item in data.get("nodes") or []:
        ssh = item.get("ssh") or {}
        result.append({
            "name": str(item.get("name") or ""),
            "ip": str(item.get("managementIp") or item.get("ip") or ""),
            "user": str(ssh.get("user") or "ubuntu"),
            "key": str(Path(str(ssh.get("key") or "~/.ssh/id_ed25519")).expanduser()),
        })
    if not result or any(not item["name"] or not item["ip"] for item in result):
        raise ValueError(f"invalid or empty inventory: {path}")
    return result


def parseResults(stdout: str) -> tuple[dict[str, dict[str, str]], dict[str, int]]:
    results: dict[str, dict[str, str]] = {}
    summary: dict[str, int] = {}
    for line in stdout.splitlines():
        fields = line.split("\t", 3)
        if len(fields) >= 3 and fields[0] in {"ok", "failed", "missing_pid"}:
            results[fields[1]] = {
                "status": fields[0], "containerId": fields[2],
                "detail": fields[3] if len(fields) == 4 else "",
            }
        if line.startswith("SUMMARY "):
            for token in line[len("SUMMARY "):].split():
                if "=" in token:
                    key, value = token.split("=", 1)
                    try:
                        summary[key] = int(value)
                    except ValueError:
                        pass
    return results, summary


def runNode(node: dict[str, str], namespace: str, expected: int, millicores: float,
            program_b64: str, timeout: float, one_timeout: float,
            retries: int, backoff: float) -> dict[str, Any]:
    command = [
        "ssh", "-i", node["key"], "-o", "UserKnownHostsFile=/dev/null",
        "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=60", f"{node['user']}@{node['ip']}",
        "sudo", "-n", "env", f"NAMESPACE={namespace}",
        f"EXPECTED_TOTAL={expected}", f"MILLICORES={millicores:g}",
        f"LOAD_PROGRAM_B64={program_b64}", f"ONE_TIMEOUT_SECONDS={one_timeout:g}",
        "bash", "-s",
    ]
    attempts = []
    completed: subprocess.CompletedProcess[str] | None = None
    started = time.time()
    for attempt in range(1, retries + 1):
        attempt_started = time.time()
        try:
            completed = subprocess.run(command, input=REMOTE_SCRIPT, text=True,
                                       capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            completed = subprocess.CompletedProcess(command, 124, stdout,
                                                     stderr or f"timeout after {timeout:g}s")
        attempts.append({"attempt": attempt, "returncode": completed.returncode,
                         "durationSeconds": time.time() - attempt_started,
                         "stderr": completed.stderr.strip()})
        if completed.returncode == 0:
            break
        if attempt < retries:
            time.sleep(backoff * attempt)
    assert completed is not None
    parsed, summary = parseResults(completed.stdout)
    return {"node": node["name"], "ip": node["ip"],
            "ok": completed.returncode == 0, "returncode": completed.returncode,
            "durationSeconds": time.time() - started, "attempts": attempts,
            "results": parsed, "summary": summary,
            "stdout": completed.stdout, "stderr": completed.stderr}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--cpu-millicores", type=float, required=True)
    parser.add_argument("--node-concurrency", type=int, default=0)
    parser.add_argument("--node-timeout-seconds", type=float, default=900)
    parser.add_argument("--one-timeout-seconds", type=float, default=15)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-backoff-seconds", type=float, default=2)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if not 0 <= args.cpu_millicores <= 1000:
        parser.error("--cpu-millicores must be between 0 and 1000 per Pod")
    if min(args.node_timeout_seconds, args.one_timeout_seconds) <= 0 or args.retries < 1:
        parser.error("timeouts must be positive and retries must be at least 1")

    kubeconfig = args.kubeconfig.expanduser().resolve()
    pods = podList(kubeconfig, args.namespace, 90, args.retries, args.retry_backoff_seconds)
    if not pods or any((item.get("status") or {}).get("phase") != "Running" for item in pods):
        parser.error("all SeedEMU workload Pods must be Running")
    expected = Counter(str((item.get("spec") or {}).get("nodeName") or "") for item in pods)
    pod_nodes = {str(item["metadata"]["name"]): str((item.get("spec") or {}).get("nodeName") or "")
                 for item in pods}
    nodes = loadNodes(args.inventory.expanduser().resolve())
    encoded = __import__("base64").b64encode(LOAD_PROGRAM.encode()).decode()
    workers = len(nodes) if args.node_concurrency <= 0 else min(args.node_concurrency, len(nodes))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(runNode, node, args.namespace, expected.get(node["name"], 0),
                               args.cpu_millicores, encoded, args.node_timeout_seconds,
                               args.one_timeout_seconds, args.retries,
                               args.retry_backoff_seconds) for node in nodes]
        node_results = [future.result() for future in concurrent.futures.as_completed(futures)]

    by_node = {item["node"]: item for item in node_results}
    results = []
    for pod, node in sorted(pod_nodes.items()):
        node_result = by_node.get(node) or {}
        item = (node_result.get("results") or {}).get(pod) or {}
        ok = node_result.get("ok") is True and item.get("status") == "ok"
        results.append({"pod": pod, "node": node, "millicores": args.cpu_millicores,
                        "ok": ok, "returncode": 0 if ok else int(node_result.get("returncode", 1)),
                        "stdout": "", "stderr": item.get("detail") or node_result.get("stderr", ""),
                        "durationSeconds": node_result.get("durationSeconds", 0),
                        "attemptCount": len(node_result.get("attempts") or []),
                        "attempts": node_result.get("attempts") or []})
    failures = [item for item in results if not item["ok"]]
    log_dir = args.output.parent / f"{args.output.stem}_node_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    compact_nodes = []
    for item in sorted(node_results, key=lambda value: value["node"]):
        (log_dir / f"{item['node']}.log").write_text(
            f"returncode={item['returncode']}\n--- stdout ---\n{item['stdout']}\n--- stderr ---\n{item['stderr']}\n")
        compact_nodes.append({key: item[key] for key in
                              ("node", "ip", "ok", "returncode", "durationSeconds", "attempts", "summary")})
    report = {
        "schemaVersion": 2, "createdAt": datetime.now().astimezone().isoformat(),
        "executionStrategy": "node-local-nsenter-serial-per-node",
        "namespace": args.namespace, "requestedCpuMillicoresPerPod": args.cpu_millicores,
        "podCount": len(results), "successCount": len(results) - len(failures),
        "failureCount": len(failures), "status": "PASS" if not failures else "FAIL",
        "nodeResults": compact_nodes, "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"{report['status']}: {report['successCount']}/{report['podCount']} Pods at "
          f"{args.cpu_millicores:g}m via node-local nsenter")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
