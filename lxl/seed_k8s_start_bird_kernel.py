#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Dict

EXIT_KERNEL_SWITCH_FAILED = 41

# ================= 配置区域 =================
ROLE_SET = {"r", "brd", "rs"}

# Node 内部的串行执行延迟（防止连续发包压垮 Kubelet 隧道）
START_DELAY = 0.3             

# 宿主机负载阈值：判断路由是否完全刷入内核并收敛
SYSTEM_LOAD_THRESHOLD = 40.0  
LOAD_CHECK_INTERVAL = 20       
# ===========================================

@dataclass
class RouterTarget:
    name: str
    asn: str
    node: str

def run(cmd: List[str], *, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        detail = stderr or f"command timed out after {timeout} seconds"
        return subprocess.CompletedProcess(cmd, 124, stdout, detail)

def kubectl(namespace: str, args: List[str], *, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return run(["kubectl", "-n", namespace, *args], timeout=timeout)

def kubectl_exec(namespace: str, pod: str, shell_cmd: str, *, timeout: int) -> subprocess.CompletedProcess[str]:
    return kubectl(namespace, ["exec", pod, "--", "sh", "-lc", shell_cmd], timeout=timeout)

def log(log_path: Path, message: str) -> None:
    stamped = f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}] {message}"
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(stamped + "\n")
    print(stamped)

def router_targets(namespace: str) -> List[RouterTarget]:
    result = kubectl(namespace, ["get", "pods", "-o", "json"], timeout=60)
    result.check_returncode()
    data = json.loads(result.stdout)
    targets: List[RouterTarget] = []
    for item in data.get("items", []):
        labels = (item.get("metadata", {}) or {}).get("labels", {}) or {}
        if labels.get("seedemu.io/workload") != "seedemu":
            continue
        # 兼容你 Docker 脚本的过滤逻辑，只要是路由器角色全抓
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

def kernel_config(export_mode: str) -> str:
    """生成带有抖动的内核配置文件"""
    scan_base = int(os.environ.get("SEED_KERNEL_SCAN_BASE_SECONDS", "6000"))
    scan_jitter = int(os.environ.get("SEED_KERNEL_SCAN_JITTER_SECONDS", "120"))
    interval = scan_base + random.randint(0, max(scan_jitter, 0))
    
    if export_mode == "device_ospf_only":
        return f'''protocol kernel {{
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
'''
    # 全开模式 (export all)
    return f'''protocol kernel {{
    merge paths on;
    persist;
    scan time {interval};
    ipv4 {{
        import none;
        export all;
    }};
}}
'''

def write_kernel_conf(namespace: str, pod: str, content: str, exec_timeout: int, birdc_timeout: int) -> subprocess.CompletedProcess[str]:
    """写回配置文件并执行 birdc reload kernel"""
    payload = content.replace("'", "'\"'\"'")
    write_cmd = (
        "cat <<'EOF' > /etc/bird/conf/kernel.conf\n"
        f"{payload}"
        "EOF\n"
    )
    write_result = kubectl_exec(namespace, pod, write_cmd, timeout=exec_timeout)
    if write_result.returncode != 0:
        return write_result
        
    # 重载路由 (完美匹配你的 Docker 逻辑)
    reload_cmd = (
        f"timeout {birdc_timeout} birdc configure >/dev/null 2>&1 && "
        f"timeout {birdc_timeout} birdc 'reload kernel' >/dev/null 2>&1 || true"
    )
    return kubectl_exec(namespace, pod, reload_cmd, timeout=exec_timeout)

def apply_kernel_conf_on_node(
    node_name: str,
    namespace: str,
    node_targets: List[RouterTarget],
    export_mode: str,
    exec_timeout: int,
    birdc_timeout: int,
    retries: int,
    retry_backoff: float,
    log_path: Path,
) -> tuple[int, list[dict[str, str]]]:
    """负责在指定的单个 Node 上，串行下发 Kernel 协议变更"""
    failures = []
    processed = 0
    total = len(node_targets)
    
    for count, target in enumerate(node_targets, start=1):
        success = False
        last_error = ""
        for attempt in range(1, retries + 1):
            config = kernel_config(export_mode)
            result = write_kernel_conf(namespace, target.name, config, exec_timeout, birdc_timeout)
            
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
            
        if count % 50 == 0:
            log(log_path, f" -> Node [{node_name}] Progress: Switched {count}/{total} pods...")
            
        time.sleep(START_DELAY) # Node 内单步延迟，保护 Kubelet
        
    log(log_path, f"✅ Node [{node_name}] finished: Switched {processed}/{total} pods.")
    return processed, failures

def get_node_load(namespace: str, pod_name: str, exec_timeout: int) -> float:
    """探针：免 SSH 获取宿主机 Load Average"""
    result = kubectl_exec(namespace, pod_name, "cat /proc/loadavg", timeout=exec_timeout)
    if result.returncode == 0:
        try:
            return float(result.stdout.split()[0])
        except (IndexError, ValueError):
            pass
    return -1.0

def wait_for_cluster_idle(namespace: str, nodes_map: Dict[str, List[RouterTarget]], log_path: Path, exec_timeout: int) -> None:
    """按 Node 独立监控集群 Kernel 路由注入负载"""
    probe_pods = {node: pods[0].name for node, pods in nodes_map.items() if pods}
    log(log_path, f"⏳ Waiting for Kernel Route injection convergence (Load < {SYSTEM_LOAD_THRESHOLD}) across {len(probe_pods)} nodes...")
    
    while True:
        all_idle = True
        status_strs = []
        for node, probe_pod in probe_pods.items():
            load = get_node_load(namespace, probe_pod, exec_timeout)
            if load >= 0:
                symbol = "🟢" if load < SYSTEM_LOAD_THRESHOLD else "🔴"
                status_strs.append(f"{node}: {load:.2f} {symbol}")
                if load >= SYSTEM_LOAD_THRESHOLD:
                    all_idle = False
            else:
                status_strs.append(f"{node}: ⚠️ Error")
                all_idle = False 
                
        sys.stdout.write("\r [Load Check] " + " | ".join(status_strs) + f" (Target: <{SYSTEM_LOAD_THRESHOLD}) ")
        sys.stdout.flush()
        
        if all_idle:
            print("") 
            log(log_path, f"✅ Kernel routing stabilized! Final loads: " + ", ".join(status_strs))
            break
        time.sleep(LOAD_CHECK_INTERVAL)

def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: scripts/seed_k8s_start_bird_kernel.py <namespace> <artifact_dir>", file=sys.stderr)
        return 2

    namespace = sys.argv[1]
    artifact_dir = Path(sys.argv[2])
    artifact_dir.mkdir(parents=True, exist_ok=True)

    base_exec_timeout = int(os.environ.get("SEED_KUBECTL_EXEC_TIMEOUT_SECONDS", "30"))
    exec_timeout = int(os.environ.get("SEED_KERNEL_EXEC_TIMEOUT_SECONDS", str(max(base_exec_timeout, 45))))
    birdc_timeout = int(os.environ.get("SEED_KERNEL_BIRDC_TIMEOUT_SECONDS", "10"))
    
    # 核心环境变量：all 代表全开，device_ospf_only 代表拦截 BGP
    export_mode = os.environ.get("SEED_KERNEL_EXPORT_MODE", "all").strip().lower() or "all"
    retries = max(1, int(os.environ.get("SEED_KERNEL_SWITCH_RETRIES", "2")))
    retry_backoff = float(os.environ.get("SEED_KERNEL_SWITCH_RETRY_BACKOFF_SECONDS", "1"))

    log_path = artifact_dir / "bird_kernel.log"
    total_start_time = time.time()
    log(log_path, f"=== K8s BIRD Kernel Switcher ({export_mode.upper()} Mode) (Node-Aware) ===")

    targets = router_targets(namespace)
    (artifact_dir / "bird_kernel_targets.json").write_text(json.dumps([asdict(target) for target in targets], indent=2), encoding="utf-8")

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
        (artifact_dir / "bird_kernel_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return EXIT_KERNEL_SWITCH_FAILED

    # ================= 1. 按 Node 分组 =================
    nodes_map = defaultdict(list)
    for t in targets:
        nodes_map[t.node].append(t)
        
    log(log_path, f"🚀 Grouped {len(targets)} targets into {len(nodes_map)} nodes.")

    # ================= 2. Node 并发下发配置 =================
    all_failures = []
    total_processed = 0
    
    with ThreadPoolExecutor(max_workers=len(nodes_map)) as pool:
        future_to_node = {
            pool.submit(
                apply_kernel_conf_on_node,
                node, namespace, pods, export_mode, exec_timeout, birdc_timeout, retries, retry_backoff, log_path
            ): node for node, pods in nodes_map.items()
        }
        
        for future in as_completed(future_to_node):
            node_name = future_to_node[future]
            try:
                processed, failures = future.result()
                total_processed += processed
                all_failures.extend(failures)
            except Exception as exc:
                log(log_path, f"⚠️ Node {node_name} exception: {exc}")
                all_failures.append({"pod": f"Node_{node_name}", "stderr": str(exc)})

    summary["processed"] = total_processed
    summary["failures"] = all_failures
    if all_failures:
        summary["status"] = "FAIL"
        summary["failure_reason"] = "kernel_switch_failed"
        (artifact_dir / "bird_kernel_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return EXIT_KERNEL_SWITCH_FAILED

    # ================= 3. 监控 Linux 内核收敛 =================
    log(log_path, "Waiting 15 seconds for Kernel routing injection to begin...")
    time.sleep(15)
    wait_for_cluster_idle(namespace, nodes_map, log_path, exec_timeout)

    (artifact_dir / "bird_kernel_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    
    total_duration = time.time() - total_start_time
    log(log_path, f"🎉 Kernel Switch to [{export_mode}] completed flawlessly in {total_duration:.2f}s!")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())