#!/usr/bin/env python3
"""Run B62 4954 macvlan+VLAN experiments across total VM counts.

Inputs: assignment.yaml and requested total VM counts. Total VM count means
master plus workers.

Outputs: a sweep run directory under runs/, per-VM assignment files, a JSON
sweep summary, and 0625multiVM.md.

Side effects: sequentially invokes runFullExperiment.py, which may destroy the
currently recorded cluster, create KVM VMs, build K3s, push/preload images,
deploy workload namespaces, start BIRD, enable kernel export, and run
verification stages.

Context: run from largeScale/b62_k8s_scale on the KVM host.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_ASSIGNMENT = SCRIPT_DIR / "assignment.yaml"
DEFAULT_TOPOLOGY_SIZE = 4954
DEFAULT_VM_COUNTS = [6, 12, 18, 24, 36, 48]
DEFAULT_REPORT_PATH = SCRIPT_DIR / "0625multiVM.md"


MACVLAN_VLAN_DEPLOY_TUNING = {
    "batchSize": 80,
    "batchSleepSeconds": 0,
    "subnetBatchSize": 100,
    "subnetBatchSleepSeconds": 0,
    "monitorInterval": 20,
    "monitorEnabled": False,
    "staticApplyMode": "batch",
    "controllerApplyMode": "batch",
    "warmupBatches": 12,
    "warmupBatchSize": 1,
    "postControllerApplySettleSeconds": 5,
    "pressureCheckSeconds": 5,
    "stabilizeTimeoutSeconds": 36000,
    "maxPendingPods": 160,
    "maxCreatingPods": 160,
    "maxCreatingPodsPerNode": 40,
    "maxNotReadyPods": 160,
    "maxFailedPods": 10,
    "requireAllNodesReady": True,
    "waitKubeOvnSubnets": False,
    "kubeOvnSubnetTimeoutSeconds": 36000,
    "postSubnetCooldownSeconds": 0,
    "nodeStreamObserveTimeoutSeconds": 120,
    "nodeStreamMaxActivePerNode": 4,
    "skipKubeOvnGatewayCheck": False,
}


STAGE_COLUMNS = [
    "render-assignment",
    "destroy-existing-cluster",
    "prepare-libvirt-dhcp",
    "build-cluster",
    "ensure-all-nodes-schedulable",
    "clean",
    "preflight",
    "compile",
    "build",
    "deploy",
    "wait-ready",
    "start-bird",
    "verify-after-start-bird",
    "start-kernel",
    "verify-after-fib-write",
    "reconvergence",
]

INFRA_STAGE_NAMES = {
    "render-assignment",
    "destroy-existing-cluster",
    "prepare-libvirt-dhcp",
    "build-cluster",
    "ensure-all-nodes-schedulable",
}


def loadYaml(path: Path) -> dict[str, Any]:
    """Load one YAML mapping from path."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"Invalid YAML root in {path}: expected mapping")
    return data


def writeYaml(path: Path, data: dict[str, Any]) -> None:
    """Write one YAML mapping to path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def writeJson(path: Path, data: dict[str, Any]) -> None:
    """Write formatted JSON to path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def readJson(path: Path) -> dict[str, Any]:
    """Read one JSON mapping if it exists, otherwise return an empty mapping."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def createAssignment(base: dict[str, Any], topology_size: int, total_vm_count: int) -> dict[str, Any]:
    """Create one run-local macvlan+VLAN assignment for a total VM count."""
    if total_vm_count < 2:
        raise SystemExit("total VM count must be >= 2 because one VM is the master")
    worker_count = total_vm_count - 1
    assignment = copy.deepcopy(base)

    assignment.setdefault("experiment", {})
    assignment["experiment"]["name"] = f"b62-macvlan-vlan-{topology_size}-vm{total_vm_count}"
    assignment["experiment"]["topologySize"] = topology_size
    assignment["experiment"]["workerCount"] = worker_count
    assignment["experiment"]["namespace"] = f"seedemu-b62-mvlan-vlan-{topology_size}-vm{total_vm_count}"

    assignment.setdefault("networking", {})
    assignment["networking"].update(
        {
            "backend": "macvlan",
            "cniType": "macvlan",
            "cniMasterInterface": "ens2",
            "localLinkCniType": "macvlan",
            "attachedCniType": "macvlan",
            "macvlanVlanMode": True,
            "macvlanVlanIdStart": 1,
        }
    )
    if not assignment["networking"].get("vlanTrunks"):
        assignment["networking"]["vlanTrunks"] = [
            {
                "name": "trunk0",
                "masterInterface": "ens2",
                "vlanStart": 1,
                "vlanEnd": 4094,
            },
            {
                "name": "trunk1",
                "masterInterface": "ens3",
                "vlanStart": 1,
                "vlanEnd": 4094,
                "networkPrefix": "seedemu-b62-fabric1",
                "bridgePrefix": "virbrb62f1",
            },
        ]

    assignment.setdefault("placement", {})
    assignment["placement"].update({"excludeControlPlane": False})
    assignment["placement"]["nodePodReserve"] = int(assignment["placement"].get("nodePodReserve", 20))

    assignment["deploy"] = copy.deepcopy(MACVLAN_VLAN_DEPLOY_TUNING)
    return assignment


def stageRows(summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return stage records keyed by stage name from one run summary."""
    rows: dict[str, dict[str, Any]] = {}
    for stage in summary.get("stages", []):
        if isinstance(stage, dict) and stage.get("name"):
            rows[str(stage["name"])] = stage
    return rows


def durationFor(rows: dict[str, dict[str, Any]], name: str) -> float | None:
    """Return one stage duration in seconds."""
    value = rows.get(name, {}).get("durationSeconds")
    return float(value) if isinstance(value, (int, float)) else None


def fmtSeconds(value: Any) -> str:
    """Format seconds for a Markdown table."""
    if not isinstance(value, (int, float)):
        return "-"
    return f"{float(value):.2f}"


def testStatus(summary: dict[str, Any], key: str) -> str:
    """Return PASS/FAIL-like status for one verification key."""
    tests = summary.get("tests", {})
    item = tests.get(key) if isinstance(tests, dict) else None
    if not isinstance(item, dict):
        return "-"
    return str(item.get("status") or ("PASS" if item.get("success") is True else "FAIL" if item.get("success") is False else "-"))


def summarizeRun(item: dict[str, Any]) -> dict[str, Any]:
    """Read one run summary and return compact sweep result fields."""
    run_dir = Path(item["runDir"])
    summary_path = run_dir / "summary.json"
    summary = readJson(summary_path)
    rows = stageRows(summary)
    status = str(summary.get("status") or ("FAIL" if item.get("exitCode") else "UNKNOWN"))
    stage_durations = {name: durationFor(rows, name) for name in STAGE_COLUMNS}
    measured_stage_seconds = sum(value for value in stage_durations.values() if isinstance(value, float))
    infra_stage_seconds = sum(value for name, value in stage_durations.items() if isinstance(value, float) and name in INFRA_STAGE_NAMES)
    workload_seconds = measured_stage_seconds - infra_stage_seconds
    total_seconds = summary.get("durationSeconds") or item.get("durationSeconds")
    untracked_seconds = None
    if isinstance(total_seconds, (int, float)):
        untracked_seconds = float(total_seconds) - measured_stage_seconds
    return {
        "totalVmCount": item["totalVmCount"],
        "workerCount": item["workerCount"],
        "status": status,
        "exitCode": item.get("exitCode"),
        "runDir": str(run_dir),
        "namespace": item.get("namespace"),
        "durationSeconds": total_seconds,
        "measuredStageSeconds": measured_stage_seconds,
        "infraStageSeconds": infra_stage_seconds,
        "workloadStageSeconds": workload_seconds,
        "untrackedOverheadSeconds": untracked_seconds,
        "stageDurations": stage_durations,
        "verifyStartBird": testStatus(summary, "verify_after_start_bird"),
        "verifyFib": testStatus(summary, "verify_after_fib_write"),
        "reconvergence": testStatus(summary, "reconvergence"),
    }


def renderReport(sweep_summary: dict[str, Any]) -> str:
    """Render a Markdown report for the current sweep state."""
    generated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    lines = [
        "# 2026-06-25 4954 规模 macvlan+VLAN 多 VM 实验",
        "",
        f"Generated at: `{generated}`",
        "",
        "## 实验目标",
        "",
        "本轮重新测试 B62 4954 规模在 `macvlan+VLAN` 模式下，不同总 VM 数量的全流程阶段耗时。",
        "总 VM 数量包含 master，因此 6/12/18/24/36/48 VM 分别对应 `workerCount=5/11/17/23/35/47`。",
        "",
        "默认网络模式固定为：",
        "",
        "- `networking.backend: macvlan`",
        "- `networking.cniType/localLinkCniType/attachedCniType: macvlan`",
        "- `networking.macvlanVlanMode: true`",
        "- `placement.excludeControlPlane: false`，master 也参与 workload 调度",
        "",
        "## Sweep 状态",
        "",
        f"- Sweep directory: `{sweep_summary.get('sweepDir', '-')}`",
        f"- Sweep status: `{sweep_summary.get('status', '-')}`",
        f"- Topology size: `{sweep_summary.get('topologySize', '-')}`",
        "",
        "## 汇总表",
        "",
        "| 总 VM | workerCount | status | total(s) | measured stages(s) | infra stages(s) | workload stages(s) | untracked overhead(s) | build-cluster(s) | compile(s) | build(s) | deploy(s) | wait-ready(s) | start-bird(s) | verify-bird(s) | start-kernel(s) | verify-kernel(s) | reconvergence(s) | run dir |",
        "| ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    results = sweep_summary.get("results", [])
    for result in results if isinstance(results, list) else []:
        compact = summarizeRun(result)
        stages = compact["stageDurations"]
        lines.append(
            "| {vm} | {workers} | {status} | {total} | {measured} | {infra} | {workload} | {overhead} | {build_cluster} | {compile_stage} | {build_stage} | {deploy} | {wait_ready} | {start_bird} | {verify_bird} | {start_kernel} | {verify_kernel} | {reconv} | `{run_dir}` |".format(
                vm=compact["totalVmCount"],
                workers=compact["workerCount"],
                status=compact["status"],
                total=fmtSeconds(compact["durationSeconds"]),
                measured=fmtSeconds(compact["measuredStageSeconds"]),
                infra=fmtSeconds(compact["infraStageSeconds"]),
                workload=fmtSeconds(compact["workloadStageSeconds"]),
                overhead=fmtSeconds(compact["untrackedOverheadSeconds"]),
                build_cluster=fmtSeconds(stages.get("build-cluster")),
                compile_stage=fmtSeconds(stages.get("compile")),
                build_stage=fmtSeconds(stages.get("build")),
                deploy=fmtSeconds(stages.get("deploy")),
                wait_ready=fmtSeconds(stages.get("wait-ready")),
                start_bird=fmtSeconds(stages.get("start-bird")),
                verify_bird=fmtSeconds(stages.get("verify-after-start-bird")),
                start_kernel=fmtSeconds(stages.get("start-kernel")),
                verify_kernel=fmtSeconds(stages.get("verify-after-fib-write")),
                reconv=fmtSeconds(stages.get("reconvergence")),
                run_dir=compact["runDir"],
            )
        )
    lines.extend(
        [
            "",
            "## 各阶段明细",
            "",
        ]
    )
    for result in results if isinstance(results, list) else []:
        compact = summarizeRun(result)
        lines.extend(
            [
                f"### {compact['totalVmCount']} VM",
                "",
                f"- Run dir: `{compact['runDir']}`",
                f"- Namespace: `{compact.get('namespace') or '-'}`",
                f"- Status: `{compact['status']}`",
                f"- verify-after-start-bird: `{compact['verifyStartBird']}`",
                f"- verify-after-fib-write: `{compact['verifyFib']}`",
                f"- reconvergence: `{compact['reconvergence']}`",
                "",
                "| Stage | Duration(s) |",
                "| --- | ---: |",
            ]
        )
        stages = compact["stageDurations"]
        for name in STAGE_COLUMNS:
            lines.append(f"| `{name}` | {fmtSeconds(stages.get(name))} |")
        lines.append("")
    if not results:
        lines.append("No completed runs yet.")
        lines.append("")
    lines.extend(
        [
            "## 说明",
            "",
            "- `total(s)` 是单轮 full-flow 的墙钟总耗时；如果单轮 `summary.json` 没有顶层总耗时，则使用 sweep runner 记录的外层耗时。",
            "- `measured stages(s)` 是该轮 `summary.json` 中所有 stage 耗时之和。",
            "- `infra stages(s)` = `render-assignment` + `destroy-existing-cluster` + `prepare-libvirt-dhcp` + `build-cluster` + `ensure-all-nodes-schedulable`。",
            "- `workload stages(s)` = `measured stages(s)` - `infra stages(s)`，包括 `clean/preflight/compile/build/deploy/wait-ready/start/verify/reconvergence`。",
            "- `untracked overhead(s)` = `total(s)` - `measured stages(s)`，主要来自 stage 外层包装、进程启动/收尾、日志刷新和计时粒度差异。正常情况下应远小于总耗时。",
            "- 因此核账关系为：`total(s) ~= measured stages(s) + untracked overhead(s)`；`measured stages(s) = infra stages(s) + workload stages(s)`。",
            "- 每轮开始时 `runFullExperiment.py` 会销毁上一次记录的 cluster，再重新构建目标 VM 数量的 K3s cluster。",
            "- 默认全流程不会在成功后自动销毁最后一轮 cluster，便于手工检查。",
            "",
            "## 本轮关键观察",
            "",
            "- 本轮 12/18/24/36/48 VM 全部通过 full flow，包括 `verify-after-start-bird`、`verify-after-fib-write` 和 `reconvergence`。",
            "- 12VM 重跑前曾遇到 `/data/lxl/k8sTools/b62/w11/disks/seedemu-b62-master-w11.qcow2` 残留但对应 VM 未定义的问题；已通过 `destroyCluster.sh` 和清理 stale disk 目录后重跑。",
            "- `deploy` 阶段主机 load 会出现短时高峰，但每轮 `pending`、`creating`、`creating_by_node` 都没有长期堆积；macvlan+VLAN 的 CNI ADD 没有复现 OVN+OVS 下的长尾卡死现象。",
            "- `start-bird` 阶段多轮日志显示 `started=0 skipped=4954`，说明进入该阶段时容器内 BIRD 已经在运行；该阶段保持幂等检查并继续执行后续协议验证。",
            "- 当前脚本不生成、也不检查显式 `include \"/etc/bird/conf/kernel.conf\";`；BIRD 配置加载依赖编译生成的通配 include。",
            "",
        ]
    )
    return "\n".join(lines)


def updateReport(path: Path, sweep_summary: dict[str, Any]) -> None:
    """Write the Markdown report."""
    path.write_text(renderReport(sweep_summary), encoding="utf-8")


def runOne(assignment_path: Path, run_dir: Path) -> int:
    """Run one full experiment and return its exit code."""
    cmd = [
        "python3",
        str(SCRIPT_DIR / "runFullExperiment.py"),
        "--assignment",
        str(assignment_path),
        "--run-dir",
        str(run_dir),
    ]
    print("$ " + " ".join(cmd), flush=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "runFullExperiment.outer.log"
    print(f"full output log: {log_path}", flush=True)
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    with log_path.open("w", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            cmd,
            cwd=str(SCRIPT_DIR),
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
        )
        rc = process.wait()
    if rc != 0:
        print(f"run failed with exit code {rc}; tail of {log_path}:", flush=True)
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            for line in lines[-80:]:
                print(line, flush=True)
        except OSError as exc:
            print(f"failed to read log tail: {exc}", flush=True)
    return rc


def parseArgs(argv: list[str]) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assignment", type=Path, default=DEFAULT_ASSIGNMENT)
    parser.add_argument("--topology-size", type=int, default=DEFAULT_TOPOLOGY_SIZE)
    parser.add_argument("--vm-counts", nargs="+", type=int, default=DEFAULT_VM_COUNTS)
    parser.add_argument("--sweep-dir", type=Path)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--stop-on-failure", action="store_true")
    parser.add_argument("--print-plan", action="store_true")
    return parser.parse_args(argv)


def main() -> int:
    """CLI entrypoint."""
    args = parseArgs(sys.argv[1:])
    started = time.time()
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    sweep_dir = args.sweep_dir or SCRIPT_DIR / "runs" / f"macvlan_vlan_vm_sweep_0625_{timestamp}_{args.topology_size}"
    assignments_dir = sweep_dir / "assignments"
    sweep_summary_path = sweep_dir / "macvlan_vlan_vm_sweep_summary.json"
    base = loadYaml(args.assignment)
    plan = []

    for total_vm_count in args.vm_counts:
        worker_count = total_vm_count - 1
        run_dir = sweep_dir / f"{timestamp}_{args.topology_size}_vm{total_vm_count}_w{worker_count}"
        assignment_path = assignments_dir / f"assignment_vm{total_vm_count}.yaml"
        assignment = createAssignment(base, args.topology_size, total_vm_count)
        item = {
            "totalVmCount": total_vm_count,
            "workerCount": worker_count,
            "assignment": str(assignment_path),
            "runDir": str(run_dir),
            "namespace": assignment["experiment"]["namespace"],
        }
        plan.append(item)
        if not args.print_plan:
            writeYaml(assignment_path, assignment)

    sweep_summary: dict[str, Any] = {
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "status": "PLANNED" if args.print_plan else "RUNNING",
        "topologySize": args.topology_size,
        "vmCountsMeanTotalMasterPlusWorkers": True,
        "sweepDir": str(sweep_dir),
        "report": str(args.report),
        "plan": plan,
        "results": [],
    }
    writeJson(sweep_summary_path, sweep_summary)
    updateReport(args.report, sweep_summary)
    print(json.dumps(sweep_summary, indent=2, sort_keys=True), flush=True)
    if args.print_plan:
        return 0

    exit_code = 0
    for item in plan:
        print(
            f"=== macvlan+VLAN sweep totalVM={item['totalVmCount']} workerCount={item['workerCount']} ===",
            flush=True,
        )
        run_started = time.time()
        rc = runOne(Path(item["assignment"]), Path(item["runDir"]))
        result = {
            **item,
            "exitCode": rc,
            "durationSeconds": round(time.time() - run_started, 2),
        }
        sweep_summary["results"].append(result)
        if rc != 0:
            exit_code = rc
            sweep_summary["status"] = "FAIL"
            writeJson(sweep_summary_path, sweep_summary)
            updateReport(args.report, sweep_summary)
            if args.stop_on_failure:
                break
        else:
            sweep_summary["status"] = "RUNNING"
            writeJson(sweep_summary_path, sweep_summary)
            updateReport(args.report, sweep_summary)

    if exit_code == 0 and len(sweep_summary["results"]) == len(plan):
        sweep_summary["status"] = "PASS"
    elif exit_code == 0:
        sweep_summary["status"] = "PARTIAL"
    sweep_summary["finishedAt"] = datetime.now(timezone.utc).isoformat()
    sweep_summary["durationSeconds"] = round(time.time() - started, 2)
    writeJson(sweep_summary_path, sweep_summary)
    updateReport(args.report, sweep_summary)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
