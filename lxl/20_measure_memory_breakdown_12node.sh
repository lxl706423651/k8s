#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck disable=SC1091
source "${SCRIPT_DIR}/env_12node.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/01_cluster_nodes_12node.sh"

seed_load_cluster_nodes

LABEL=""
OUTPUT_DIR="${HOME}/k8s/lxl/logs/memory_breakdown"
INCLUDE_BIRD_INTERNAL="false"
ONLY_NODE=""

usage() {
    cat <<'EOF'
Usage:
  ./20_measure_memory_breakdown_12node.sh [--label LABEL] [--output-dir DIR] [--include-bird-internal] [--only-node NODE_NAME]

Description:
  Collects per-guest memory measurements for the current K3s + KVM cluster:
    - Total effective memory      = MemTotal - MemAvailable
    - All-process PSS             = sum(PSS) over all processes
    - User working set            = sum(Pss_Anon + Pss_Shmem) over all processes
    - File-backed process PSS     = sum(Pss_File) over all processes
    - Kernel-side memory          = major directly attributable kernel-side counters:
                                    Slab + KernelStack + PageTables + Percpu + VmallocUsed
    - Total Slab                  = Slab
    - Network-related Slab        = selected caches from /proc/slabinfo
    - Non-user effective memory   = Effective - User working set
    - Accounting gap (debug)      = Effective - AllProcessPSS - KernelSide

  Outputs:
    - summary text to stdout
    - JSON snapshot
    - TSV snapshot

Notes:
  - This script measures from inside each KVM guest, not from host-side QEMU RSS.
  - PSS collection is expensive and may take noticeable time on large deployments.
  - The reported metrics are parallel observables, not a strict additive partition of all
    physical pages.
  - The accounting gap is a debug field only. It reflects mismatches between accounting
    conventions and should not be treated as a primary experimental result.
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
        --include-bird-internal)
            INCLUDE_BIRD_INTERNAL="true"
            shift
            ;;
        --only-node)
            ONLY_NODE="${2:-}"
            shift 2
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

tmp_raw="$(mktemp -d "${SCRIPT_DIR}/tmp.mem.XXXXXX")"
cleanup() {
    rm -rf "${tmp_raw}"
}
trap cleanup EXIT

collect_node() {
    local node_name="$1"
    local node_ip="$2"
    local out_file="$3"

    ssh -i "${SEED_K3S_SSH_KEY}" \
        -o BatchMode=yes \
        -o ConnectTimeout=10 \
        -o StrictHostKeyChecking=no \
        -o UserKnownHostsFile=/dev/null \
        "${SEED_K3S_USER}@${node_ip}" \
        "sudo -n bash -s -- '${node_name}' '${node_ip}' '${INCLUDE_BIRD_INTERNAL}'" <<'REMOTE' > "${out_file}"
set -euo pipefail

node_name="${1}"
node_ip="${2}"
include_bird_internal="${3}"

declare -A mem
while read -r key value _; do
    mem["${key%:}"]="${value}"
done < /proc/meminfo

mem_total_kb="${mem[MemTotal]:-0}"
mem_available_kb="${mem[MemAvailable]:-0}"
slab_kb="${mem[Slab]:-0}"
kernel_stack_kb="${mem[KernelStack]:-0}"
page_tables_kb="${mem[PageTables]:-0}"
percpu_kb="${mem[Percpu]:-0}"
vmalloc_used_kb="${mem[VmallocUsed]:-0}"

pss_metrics="$(
python3 - <<'PY'
from pathlib import Path

pss_total = 0
pss_anon = 0
pss_file = 0
pss_shmem = 0
for proc in Path('/proc').iterdir():
    if not proc.name.isdigit():
        continue
    smaps = proc / 'smaps_rollup'
    try:
        local = {"Pss": 0, "Pss_Anon": 0, "Pss_File": 0, "Pss_Shmem": 0}
        with smaps.open('r', encoding='utf-8', errors='ignore') as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 2:
                    continue
                key = parts[0].rstrip(':')
                if key in local:
                    local[key] = int(parts[1])
    except Exception:
        continue
    pss_total += local["Pss"]
    pss_anon += local["Pss_Anon"]
    pss_file += local["Pss_File"]
    pss_shmem += local["Pss_Shmem"]
print(f"{pss_total} {pss_anon} {pss_file} {pss_shmem}")
PY
)"
read -r user_pss_kb user_anon_pss_kb user_file_pss_kb user_shmem_pss_kb <<< "${pss_metrics}"
user_pss_kb="${user_pss_kb:-0}"
user_anon_pss_kb="${user_anon_pss_kb:-0}"
user_file_pss_kb="${user_file_pss_kb:-0}"
user_shmem_pss_kb="${user_shmem_pss_kb:-0}"

network_slab_active_bytes="$(
python3 - <<'PY'
import re

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
print(total)
PY
)"

bird_internal_bytes=""
if [ "${include_bird_internal}" = "true" ]; then
    bird_internal_bytes="$(
        python3 - <<'PY'
import json
import subprocess

total = 0
try:
    out = subprocess.check_output(
        "KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl -n seedemu-k3s-real-topo get pods -o json",
        shell=True,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=20,
    )
    data = json.loads(out)
    items = data.get("items", [])
    pods = []
    for item in items:
        if item.get("spec", {}).get("nodeName") != subprocess.getoutput("hostname").strip():
            continue
        name = item.get("metadata", {}).get("name", "")
        if "brd" not in name:
            continue
        if item.get("status", {}).get("phase") != "Running":
            continue
        pods.append(name)

    for pod in pods[:200]:
        try:
            cmd = f"KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl -n seedemu-k3s-real-topo exec {pod} -- birdc show memory"
            mem_out = subprocess.check_output(cmd, shell=True, stderr=subprocess.DEVNULL, text=True, timeout=10)
        except Exception:
            continue
        for line in mem_out.splitlines():
            if "Total" in line:
                nums = [int(tok) for tok in line.replace(',', ' ').split() if tok.isdigit()]
                if nums:
                    total += nums[-1]
                break
except Exception:
    pass
print(total)
PY
    )"
fi

cat <<EOF
node_name=${node_name}
node_ip=${node_ip}
mem_total_kb=${mem_total_kb}
mem_available_kb=${mem_available_kb}
slab_kb=${slab_kb}
kernel_stack_kb=${kernel_stack_kb}
page_tables_kb=${page_tables_kb}
percpu_kb=${percpu_kb}
vmalloc_used_kb=${vmalloc_used_kb}
user_pss_kb=${user_pss_kb}
user_anon_pss_kb=${user_anon_pss_kb}
user_file_pss_kb=${user_file_pss_kb}
user_shmem_pss_kb=${user_shmem_pss_kb}
network_slab_active_bytes=${network_slab_active_bytes}
bird_internal_bytes=${bird_internal_bytes}
EOF
REMOTE
}

for i in "${!SEED_NODE_NAMES[@]}"; do
    if [ -n "${ONLY_NODE}" ] && [ "${SEED_NODE_NAMES[$i]}" != "${ONLY_NODE}" ]; then
        continue
    fi
    collect_node "${SEED_NODE_NAMES[$i]}" "${SEED_NODE_IPS[$i]}" "${tmp_raw}/${SEED_NODE_NAMES[$i]}.env"
done

python3 - "${tmp_raw}" "${json_path}" "${tsv_path}" "${LABEL}" <<'PY'
import json
import os
import sys
from pathlib import Path

raw_dir = Path(sys.argv[1])
json_path = Path(sys.argv[2])
tsv_path = Path(sys.argv[3])
label = sys.argv[4]

nodes = []

def parse_env(path: Path):
    data = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        data[k] = v
    return data

for path in sorted(raw_dir.glob("*.env")):
    env = parse_env(path)
    mem_total = int(env.get("mem_total_kb", "0")) * 1024
    mem_avail = int(env.get("mem_available_kb", "0")) * 1024
    slab = int(env.get("slab_kb", "0")) * 1024
    kernel_stack = int(env.get("kernel_stack_kb", "0")) * 1024
    page_tables = int(env.get("page_tables_kb", "0")) * 1024
    percpu = int(env.get("percpu_kb", "0")) * 1024
    vmalloc_used = int(env.get("vmalloc_used_kb", "0")) * 1024
    user_pss = int(env.get("user_pss_kb", "0")) * 1024
    user_anon_pss = int(env.get("user_anon_pss_kb", "0")) * 1024
    user_file_pss = int(env.get("user_file_pss_kb", "0")) * 1024
    user_shmem_pss = int(env.get("user_shmem_pss_kb", "0")) * 1024
    network_slab = int(env.get("network_slab_active_bytes", "0"))
    bird_internal = env.get("bird_internal_bytes", "")
    bird_internal_bytes = int(bird_internal) if bird_internal.strip().isdigit() else None

    effective = mem_total - mem_avail
    kernel_side = slab + kernel_stack + page_tables + percpu + vmalloc_used
    user_working_set = user_anon_pss + user_shmem_pss
    non_user_effective = effective - user_working_set
    accounting_gap = effective - user_pss - kernel_side

    node = {
        "node_name": env.get("node_name", path.stem),
        "node_ip": env.get("node_ip", ""),
        "mem_total_bytes": mem_total,
        "mem_available_bytes": mem_avail,
        "effective_memory_bytes": effective,
        "user_pss_bytes": user_pss,
        "user_anon_pss_bytes": user_anon_pss,
        "user_file_pss_bytes": user_file_pss,
        "user_shmem_pss_bytes": user_shmem_pss,
        "user_working_set_bytes": user_working_set,
        "kernel_side_memory_bytes": kernel_side,
        "slab_bytes": slab,
        "kernel_stack_bytes": kernel_stack,
        "page_tables_bytes": page_tables,
        "percpu_bytes": percpu,
        "vmalloc_used_bytes": vmalloc_used,
        "network_related_slab_active_bytes": network_slab,
        "non_user_effective_bytes": non_user_effective,
        "bird_internal_bytes": bird_internal_bytes,
        "accounting_gap_bytes": accounting_gap,
    }
    nodes.append(node)

totals = {}
for key in [
    "mem_total_bytes",
    "mem_available_bytes",
    "effective_memory_bytes",
    "user_pss_bytes",
    "user_anon_pss_bytes",
    "user_file_pss_bytes",
    "user_shmem_pss_bytes",
    "user_working_set_bytes",
    "kernel_side_memory_bytes",
    "slab_bytes",
    "kernel_stack_bytes",
    "page_tables_bytes",
    "percpu_bytes",
    "vmalloc_used_bytes",
    "network_related_slab_active_bytes",
    "non_user_effective_bytes",
    "accounting_gap_bytes",
]:
    totals[key] = sum(n[key] for n in nodes)

bird_values = [n["bird_internal_bytes"] for n in nodes if n["bird_internal_bytes"] is not None]
totals["bird_internal_bytes"] = sum(bird_values) if bird_values else None

payload = {
    "label": label,
    "node_count": len(nodes),
    "nodes": nodes,
    "totals": totals,
    "notes": {
        "effective_memory_bytes": "MemTotal - MemAvailable",
        "user_pss_bytes": "Sum of PSS over all processes from /proc/*/smaps_rollup; use as all-process user-space footprint, not as a strict subtractive component",
        "user_working_set_bytes": "Primary strict user-space plotting metric: sum of Pss_Anon + Pss_Shmem over all processes",
        "user_file_pss_bytes": "File-backed process PSS; informative but intentionally excluded from the strict user/non-user split because it is often reclaimable",
        "kernel_side_memory_bytes": "Major directly attributable kernel-side counters: Slab + KernelStack + PageTables + Percpu + VmallocUsed",
        "network_related_slab_active_bytes": "Selected active slab objects related to fib/skbuff/neigh/route/netns",
        "non_user_effective_bytes": "Primary strict complement for plotting: Effective - UserWorkingSet",
        "accounting_gap_bytes": "Debug-only gap: Effective - AllProcessPSS - KernelSide. May be positive or negative because the metrics are not a strict additive decomposition",
    },
}

json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

with tsv_path.open("w", encoding="utf-8") as fh:
    fh.write(
        "node_name\tnode_ip\teffective_memory_bytes\tuser_working_set_bytes\tuser_pss_bytes\t"
        "user_file_pss_bytes\tkernel_side_memory_bytes\tslab_bytes\tnetwork_related_slab_active_bytes\t"
        "non_user_effective_bytes\taccounting_gap_bytes\tbird_internal_bytes\n"
    )
    for n in nodes:
        fh.write(
            f"{n['node_name']}\t{n['node_ip']}\t{n['effective_memory_bytes']}\t{n['user_working_set_bytes']}\t"
            f"{n['user_pss_bytes']}\t{n['user_file_pss_bytes']}\t{n['kernel_side_memory_bytes']}\t"
            f"{n['slab_bytes']}\t{n['network_related_slab_active_bytes']}\t{n['non_user_effective_bytes']}\t"
            f"{n['accounting_gap_bytes']}\t{'' if n['bird_internal_bytes'] is None else n['bird_internal_bytes']}\n"
        )
    fh.write(
        f"TOTAL\t-\t{totals['effective_memory_bytes']}\t{totals['user_working_set_bytes']}\t"
        f"{totals['user_pss_bytes']}\t{totals['user_file_pss_bytes']}\t{totals['kernel_side_memory_bytes']}\t"
        f"{totals['slab_bytes']}\t{totals['network_related_slab_active_bytes']}\t{totals['non_user_effective_bytes']}\t{totals['accounting_gap_bytes']}\t"
        f"{'' if totals['bird_internal_bytes'] is None else totals['bird_internal_bytes']}\n"
    )

def gib(x):
    return x / (1024 ** 3)

print("=== Cluster Memory Breakdown ===")
print(f"JSON: {json_path}")
print(f"TSV : {tsv_path}")
print()
print(f"Nodes                  : {len(nodes)}")
print(f"Total effective memory : {totals['effective_memory_bytes']} bytes ({gib(totals['effective_memory_bytes']):.2f} GiB)")
print(f"User working set       : {totals['user_working_set_bytes']} bytes ({gib(totals['user_working_set_bytes']):.2f} GiB)")
print(f"All-process PSS        : {totals['user_pss_bytes']} bytes ({gib(totals['user_pss_bytes']):.2f} GiB)")
print(f"File-backed proc PSS   : {totals['user_file_pss_bytes']} bytes ({gib(totals['user_file_pss_bytes']):.2f} GiB)")
print(f"Kernel-side memory     : {totals['kernel_side_memory_bytes']} bytes ({gib(totals['kernel_side_memory_bytes']):.2f} GiB)")
print(f"Total Slab             : {totals['slab_bytes']} bytes ({gib(totals['slab_bytes']):.2f} GiB)")
print(f"Network-related Slab   : {totals['network_related_slab_active_bytes']} bytes ({gib(totals['network_related_slab_active_bytes']):.2f} GiB)")
print(f"Non-user effective mem : {totals['non_user_effective_bytes']} bytes ({gib(totals['non_user_effective_bytes']):.2f} GiB)")
if totals["bird_internal_bytes"] is not None:
    print(f"BIRD internal memory   : {totals['bird_internal_bytes']} bytes ({gib(totals['bird_internal_bytes']):.2f} GiB)")
print(f"Accounting gap (debug) : {totals['accounting_gap_bytes']} bytes ({gib(totals['accounting_gap_bytes']):.2f} GiB)")
print()
print("Per-node effective/user_working_set/kernel GiB:")
for n in nodes:
    print(
        f"  {n['node_name']}: effective={gib(n['effective_memory_bytes']):.2f} "
        f"user_ws={gib(n['user_working_set_bytes']):.2f} kernel={gib(n['kernel_side_memory_bytes']):.2f} "
        f"non_user={gib(n['non_user_effective_bytes']):.2f} net_slab={gib(n['network_related_slab_active_bytes']):.2f} gap={gib(n['accounting_gap_bytes']):.2f}"
    )
PY
