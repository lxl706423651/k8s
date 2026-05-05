#!/usr/bin/env bash
set -euo pipefail

LABEL=""
OUTPUT_DIR="${HOME}/k8s/lxl/logs/memory_footprint"
CONTAINER_NAME_REGEX=".*"
INCLUDE_BIRD_INTERNAL="false"

usage() {
    cat <<'EOF'
Usage:
  ./23_measure_memory_footprint_docker_host.sh [--label LABEL] [--output-dir DIR] [--container-name-regex REGEX] [--include-bird-internal]

Metrics:
  - total_effective_memory_bytes
      Host MemTotal - MemAvailable.
  - container_cgroup_memory_bytes
      Sum of memory.current for running Docker containers matching the name regex.
  - runtime_orchestration_pss_bytes
      Sum of process PSS for dockerd/containerd/containerd-shim.
  - all_process_pss_bytes
      Sum of PSS for all host processes. Explanatory only.
  - kernel_side_memory_bytes
      Slab + KernelStack + PageTables + Percpu + VmallocUsed. Explanatory only.
  - network_related_slab_active_bytes
      Selected active slab caches: fib/skbuff/neigh/route/netns.
  - bird_internal_bytes
      Optional sum of birdc show memory across matching containers.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --label)
            LABEL="${2:-}"
            shift 2
            ;;
        --output-dir)
            OUTPUT_DIR="${2:-}"
            shift 2
            ;;
        --container-name-regex)
            CONTAINER_NAME_REGEX="${2:-}"
            shift 2
            ;;
        --include-bird-internal)
            INCLUDE_BIRD_INTERNAL="true"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

mkdir -p "${OUTPUT_DIR}"
timestamp="$(date +%Y%m%d_%H%M%S)"
suffix=""
if [ -n "${LABEL}" ]; then
    suffix="_${LABEL}"
fi
json_path="${OUTPUT_DIR}/${timestamp}${suffix}.json"
tsv_path="${OUTPUT_DIR}/${timestamp}${suffix}.tsv"

python3 - "${json_path}" "${tsv_path}" "${CONTAINER_NAME_REGEX}" "${INCLUDE_BIRD_INTERNAL}" "${LABEL}" <<'PY'
import json
import re
import subprocess
import sys
from pathlib import Path

json_path = Path(sys.argv[1])
tsv_path = Path(sys.argv[2])
container_name_regex = re.compile(sys.argv[3])
include_bird_internal = sys.argv[4].lower() == "true"
label = sys.argv[5]

def read_meminfo():
    data = {}
    with open('/proc/meminfo', 'r', encoding='utf-8', errors='ignore') as fh:
        for line in fh:
            parts = line.split()
            if len(parts) >= 2:
                data[parts[0].rstrip(':')] = int(parts[1]) * 1024
    return data

def sum_pss(cmd_tokens):
    total = 0
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            cmdline = (proc / 'cmdline').read_bytes().replace(b'\x00', b' ').decode('utf-8', errors='ignore').strip()
        except Exception:
            cmdline = ""
        if cmd_tokens is not None and not any(token in cmdline for token in cmd_tokens):
            continue
        try:
            with (proc / 'smaps_rollup').open('r', encoding='utf-8', errors='ignore') as fh:
                for line in fh:
                    if line.startswith('Pss:'):
                        total += int(line.split()[1]) * 1024
                        break
        except Exception:
            continue
    return total

def network_slab():
    patterns = [
        re.compile(r'^ip_fib_'),
        re.compile(r'^fib'),
        re.compile(r'^skbuff'),
        re.compile(r'^skb'),
        re.compile(r'^neigh'),
        re.compile(r'^dst'),
        re.compile(r'^route'),
        re.compile(r'^net_namespace$'),
        re.compile(r'^nf_conntrack'),
    ]
    total = 0
    with open('/proc/slabinfo', 'r', encoding='utf-8', errors='ignore') as fh:
        for line in fh:
            if line.startswith('slabinfo') or line.startswith('#'):
                continue
            parts = line.split()
            if len(parts) < 4:
                continue
            name = parts[0]
            active_objs = int(parts[1])
            objsize = int(parts[3])
            if any(p.match(name) for p in patterns):
                total += active_objs * objsize
    return total

def docker_containers():
    out = subprocess.check_output(
        ["docker", "ps", "--format", "{{.ID}}\t{{.Names}}"],
        text=True,
        stderr=subprocess.DEVNULL,
    )
    items = []
    for line in out.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        cid, name = parts
        if container_name_regex.search(name):
            items.append((cid, name))
    return items

containers = docker_containers()
container_rows = []
container_cgroup_total = 0
bird_total = 0

for cid, name in containers:
    mem_path = Path(f"/sys/fs/cgroup/system.slice/docker-{cid}.scope/memory.current")
    mem_bytes = 0
    if mem_path.exists():
        try:
            mem_bytes = int(mem_path.read_text().strip())
        except Exception:
            mem_bytes = 0
    container_cgroup_total += mem_bytes

    bird_bytes = None
    if include_bird_internal:
        try:
            out = subprocess.check_output(
                ["docker", "exec", cid, "birdc", "show", "memory"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            for line in out.splitlines():
                if "Total" in line:
                    nums = [int(tok) for tok in line.replace(',', ' ').split() if tok.isdigit()]
                    if nums:
                        bird_bytes = nums[-1]
                        bird_total += bird_bytes
                    break
        except Exception:
            bird_bytes = None

    container_rows.append({
        "container_id": cid,
        "container_name": name,
        "container_cgroup_memory_bytes": mem_bytes,
        "bird_internal_bytes": bird_bytes,
    })

mem = read_meminfo()
effective = mem.get("MemTotal", 0) - mem.get("MemAvailable", 0)
slab = mem.get("Slab", 0)
kernel_stack = mem.get("KernelStack", 0)
page_tables = mem.get("PageTables", 0)
percpu = mem.get("Percpu", 0)
vmalloc_used = mem.get("VmallocUsed", 0)

payload = {
    "label": label,
    "container_name_regex": sys.argv[3],
    "container_count": len(container_rows),
    "containers": container_rows,
    "totals": {
        "effective_memory_bytes": effective,
        "container_cgroup_memory_bytes": container_cgroup_total,
        "runtime_orchestration_pss_bytes": sum_pss(("dockerd", "containerd", "containerd-shim")),
        "all_process_pss_bytes": sum_pss(None),
        "kernel_side_memory_bytes": slab + kernel_stack + page_tables + percpu + vmalloc_used,
        "slab_bytes": slab,
        "kernel_stack_bytes": kernel_stack,
        "page_tables_bytes": page_tables,
        "percpu_bytes": percpu,
        "vmalloc_used_bytes": vmalloc_used,
        "network_related_slab_active_bytes": network_slab(),
        "bird_internal_bytes": bird_total if include_bird_internal else None,
    },
    "notes": {
        "effective_memory_bytes": "Primary total footprint on the host",
        "container_cgroup_memory_bytes": "Main container-memory metric: sum of memory.current for matching Docker containers",
        "runtime_orchestration_pss_bytes": "PSS of dockerd/containerd/containerd-shim",
        "all_process_pss_bytes": "All-process PSS, explanatory only",
        "kernel_side_memory_bytes": "Selected meminfo counters, explanatory only",
        "network_related_slab_active_bytes": "Selected active slab caches for FIB/skbuff/neigh/route/netns",
        "bird_internal_bytes": "Optional sum of birdc show memory across matching containers",
    },
}

json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding='utf-8')
with tsv_path.open("w", encoding='utf-8') as fh:
    fh.write("container_id\tcontainer_name\tcontainer_cgroup_memory_bytes\tbird_internal_bytes\n")
    for row in container_rows:
        fh.write(
            f"{row['container_id']}\t{row['container_name']}\t{row['container_cgroup_memory_bytes']}\t"
            f"{'' if row['bird_internal_bytes'] is None else row['bird_internal_bytes']}\n"
        )

def gib(value: int) -> float:
    return value / (1024 ** 3)

totals = payload["totals"]
print("=== Docker Host Memory Footprint ===")
print(f"JSON: {json_path}")
print(f"TSV : {tsv_path}")
print()
print(f"Containers               : {payload['container_count']}")
print(f"Total effective memory   : {totals['effective_memory_bytes']} bytes ({gib(totals['effective_memory_bytes']):.2f} GiB)")
print(f"Container cgroup memory  : {totals['container_cgroup_memory_bytes']} bytes ({gib(totals['container_cgroup_memory_bytes']):.2f} GiB)")
print(f"Runtime/orchestration    : {totals['runtime_orchestration_pss_bytes']} bytes ({gib(totals['runtime_orchestration_pss_bytes']):.2f} GiB)")
print(f"All-process PSS          : {totals['all_process_pss_bytes']} bytes ({gib(totals['all_process_pss_bytes']):.2f} GiB)")
print(f"Kernel-side counters     : {totals['kernel_side_memory_bytes']} bytes ({gib(totals['kernel_side_memory_bytes']):.2f} GiB)")
print(f"Network-related slab     : {totals['network_related_slab_active_bytes']} bytes ({gib(totals['network_related_slab_active_bytes']):.2f} GiB)")
if totals["bird_internal_bytes"] is not None:
    print(f"BIRD internal memory     : {totals['bird_internal_bytes']} bytes ({gib(totals['bird_internal_bytes']):.2f} GiB)")
PY
