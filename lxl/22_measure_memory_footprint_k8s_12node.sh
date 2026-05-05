#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck disable=SC1091
source "${SCRIPT_DIR}/env_12node.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/01_cluster_nodes_12node.sh"

seed_load_cluster_nodes

LABEL=""
OUTPUT_DIR="${HOME}/k8s/lxl/logs/memory_footprint"
ONLY_NODE=""
NAMESPACE="${SEED_NAMESPACE:-seedemu-k3s-real-topo}"
INCLUDE_BIRD_INTERNAL="false"

usage() {
    cat <<'EOF'
Usage:
  ./22_measure_memory_footprint_k8s_12node.sh [--label LABEL] [--output-dir DIR] [--namespace NS] [--only-node NODE] [--include-bird-internal]

Metrics:
  - total_effective_memory_bytes
      MemTotal - MemAvailable, summed across guests.
  - namespace_pod_cgroup_memory_bytes
      Sum of cgroup memory.current for pod slices belonging to the target namespace.
      This is the main "container memory" metric.
  - runtime_orchestration_pss_bytes
      Sum of process PSS for k3s/containerd/containerd-shim on each guest.
  - all_process_pss_bytes
      Sum of PSS for all guest processes. Explanatory only.
  - kernel_side_memory_bytes
      Slab + KernelStack + PageTables + Percpu + VmallocUsed. Explanatory only.
  - network_related_slab_active_bytes
      Selected active slab caches: fib/skbuff/neigh/route/netns.
  - bird_internal_bytes
      Optional sum of birdc show memory across running BIRD pods on each node.

Notes:
  - This script is designed for explanatory footprint analysis, not strict additive accounting.
  - For paper plots, use total_effective_memory_bytes as the main total, and use the other
    metrics as parallel explanatory series.
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
        --namespace)
            NAMESPACE="${2:-}"
            shift 2
            ;;
        --only-node)
            ONLY_NODE="${2:-}"
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
tmp_raw="$(mktemp -d "${SCRIPT_DIR}/tmp.k8smem.XXXXXX")"

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
        "sudo -n bash -s -- '${node_name}' '${node_ip}' '${NAMESPACE}' '${INCLUDE_BIRD_INTERNAL}'" <<'REMOTE' > "${out_file}"
set -euo pipefail

node_name="${1}"
node_ip="${2}"
namespace="${3}"
include_bird_internal="${4}"

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

all_pss = 0
runtime_pss = 0
runtime_tokens = ("k3s", "containerd", "containerd-shim")

for proc in Path('/proc').iterdir():
    if not proc.name.isdigit():
        continue
    try:
        cmdline = (proc / 'cmdline').read_bytes().replace(b'\x00', b' ').decode('utf-8', errors='ignore').strip()
    except Exception:
        cmdline = ""
    try:
        pss = 0
        with (proc / 'smaps_rollup').open('r', encoding='utf-8', errors='ignore') as fh:
            for line in fh:
                if line.startswith('Pss:'):
                    pss = int(line.split()[1])
                    break
    except Exception:
        continue
    all_pss += pss
    if any(token in cmdline for token in runtime_tokens):
        runtime_pss += pss

print(f"{all_pss} {runtime_pss}")
PY
)"
read -r all_pss_kb runtime_pss_kb <<< "${pss_metrics}"
all_pss_kb="${all_pss_kb:-0}"
runtime_pss_kb="${runtime_pss_kb:-0}"

namespace_pod_cgroup_memory_bytes="$(
python3 - "${namespace}" <<'PY'
import subprocess
import sys
from pathlib import Path

namespace = sys.argv[1]
hostname = subprocess.getoutput("hostname").strip()
pod_uids = []
try:
    out = subprocess.check_output(
        (
            "KUBECONFIG=/etc/rancher/k3s/k3s.yaml "
            f"kubectl -n {namespace} get pods "
            f"--field-selector spec.nodeName={hostname} "
            r"-o jsonpath='{range .items[*]}{.metadata.uid}{\"\n\"}{end}'"
        ),
        shell=True,
        text=True,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )
    for uid in out.splitlines():
        if uid:
            pod_uids.append(uid.replace("-", "_"))
except Exception:
    print(0)
    raise SystemExit

base = Path("/sys/fs/cgroup/kubepods.slice")
total = 0
seen = set()
for uid in pod_uids:
    matches = list(base.glob(f"**/*pod{uid}.slice/memory.current"))
    if not matches:
        continue
    path = str(matches[0].parent)
    if path in seen:
        continue
    seen.add(path)
    try:
        total += int(matches[0].read_text().strip())
    except Exception:
        pass
print(total)
PY
)"

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
python3 - "${namespace}" <<'PY'
import subprocess
import sys

namespace = sys.argv[1]
hostname = subprocess.getoutput("hostname").strip()
total = 0
try:
    out = subprocess.check_output(
        (
            "KUBECONFIG=/etc/rancher/k3s/k3s.yaml "
            f"kubectl -n {namespace} get pods "
            f"--field-selector spec.nodeName={hostname},status.phase=Running "
            r"-o custom-columns=NAME:.metadata.name --no-headers"
        ),
        shell=True,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=30,
    )
    for name in out.splitlines():
        if "brd" not in name:
            continue
        try:
            cmd = f"KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl -n {namespace} exec {name} -- birdc show memory"
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
all_pss_kb=${all_pss_kb}
runtime_pss_kb=${runtime_pss_kb}
namespace_pod_cgroup_memory_bytes=${namespace_pod_cgroup_memory_bytes}
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

python3 - "${tmp_raw}" "${json_path}" "${tsv_path}" "${LABEL}" "${NAMESPACE}" <<'PY'
import json
import sys
from pathlib import Path

raw_dir = Path(sys.argv[1])
json_path = Path(sys.argv[2])
tsv_path = Path(sys.argv[3])
label = sys.argv[4]
namespace = sys.argv[5]

def parse_env(path: Path):
    data = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        data[k] = v
    return data

nodes = []
for path in sorted(raw_dir.glob("*.env")):
    env = parse_env(path)
    mem_total = int(env.get("mem_total_kb", "0")) * 1024
    mem_avail = int(env.get("mem_available_kb", "0")) * 1024
    slab = int(env.get("slab_kb", "0")) * 1024
    kernel_stack = int(env.get("kernel_stack_kb", "0")) * 1024
    page_tables = int(env.get("page_tables_kb", "0")) * 1024
    percpu = int(env.get("percpu_kb", "0")) * 1024
    vmalloc_used = int(env.get("vmalloc_used_kb", "0")) * 1024
    all_pss = int(env.get("all_pss_kb", "0")) * 1024
    runtime_pss = int(env.get("runtime_pss_kb", "0")) * 1024
    namespace_pod_mem = int(env.get("namespace_pod_cgroup_memory_bytes", "0"))
    network_slab = int(env.get("network_slab_active_bytes", "0"))
    bird_internal_raw = env.get("bird_internal_bytes", "")
    bird_internal = int(bird_internal_raw) if bird_internal_raw.strip().isdigit() else None

    nodes.append({
        "node_name": env.get("node_name", path.stem),
        "node_ip": env.get("node_ip", ""),
        "effective_memory_bytes": mem_total - mem_avail,
        "namespace_pod_cgroup_memory_bytes": namespace_pod_mem,
        "runtime_orchestration_pss_bytes": runtime_pss,
        "all_process_pss_bytes": all_pss,
        "kernel_side_memory_bytes": slab + kernel_stack + page_tables + percpu + vmalloc_used,
        "slab_bytes": slab,
        "kernel_stack_bytes": kernel_stack,
        "page_tables_bytes": page_tables,
        "percpu_bytes": percpu,
        "vmalloc_used_bytes": vmalloc_used,
        "network_related_slab_active_bytes": network_slab,
        "bird_internal_bytes": bird_internal,
    })

totals = {}
for key in [
    "effective_memory_bytes",
    "namespace_pod_cgroup_memory_bytes",
    "runtime_orchestration_pss_bytes",
    "all_process_pss_bytes",
    "kernel_side_memory_bytes",
    "slab_bytes",
    "kernel_stack_bytes",
    "page_tables_bytes",
    "percpu_bytes",
    "vmalloc_used_bytes",
    "network_related_slab_active_bytes",
]:
    totals[key] = sum(node[key] for node in nodes)

bird_values = [node["bird_internal_bytes"] for node in nodes if node["bird_internal_bytes"] is not None]
totals["bird_internal_bytes"] = sum(bird_values) if bird_values else None

payload = {
    "label": label,
    "namespace": namespace,
    "node_count": len(nodes),
    "nodes": nodes,
    "totals": totals,
    "notes": {
        "effective_memory_bytes": "Primary total footprint: MemTotal - MemAvailable, summed across guests",
        "namespace_pod_cgroup_memory_bytes": "Main container-memory metric: sum of pod cgroup memory.current for the target namespace",
        "runtime_orchestration_pss_bytes": "PSS of k3s/containerd/containerd-shim processes",
        "all_process_pss_bytes": "All-process PSS, explanatory only; do not add to the total",
        "kernel_side_memory_bytes": "Selected meminfo counters: Slab + KernelStack + PageTables + Percpu + VmallocUsed; explanatory only",
        "network_related_slab_active_bytes": "Selected active slab caches for FIB/skbuff/neigh/route/netns; explanatory only",
        "bird_internal_bytes": "Optional sum of birdc show memory across running BIRD pods on each node",
    },
}

json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

with tsv_path.open("w", encoding="utf-8") as fh:
    fh.write(
        "node_name\tnode_ip\teffective_memory_bytes\tnamespace_pod_cgroup_memory_bytes\t"
        "runtime_orchestration_pss_bytes\tall_process_pss_bytes\tkernel_side_memory_bytes\t"
        "network_related_slab_active_bytes\tbird_internal_bytes\n"
    )
    for node in nodes:
        fh.write(
            f"{node['node_name']}\t{node['node_ip']}\t{node['effective_memory_bytes']}\t"
            f"{node['namespace_pod_cgroup_memory_bytes']}\t{node['runtime_orchestration_pss_bytes']}\t"
            f"{node['all_process_pss_bytes']}\t{node['kernel_side_memory_bytes']}\t"
            f"{node['network_related_slab_active_bytes']}\t"
            f"{'' if node['bird_internal_bytes'] is None else node['bird_internal_bytes']}\n"
        )
    fh.write(
        f"TOTAL\t-\t{totals['effective_memory_bytes']}\t{totals['namespace_pod_cgroup_memory_bytes']}\t"
        f"{totals['runtime_orchestration_pss_bytes']}\t{totals['all_process_pss_bytes']}\t"
        f"{totals['kernel_side_memory_bytes']}\t{totals['network_related_slab_active_bytes']}\t"
        f"{'' if totals['bird_internal_bytes'] is None else totals['bird_internal_bytes']}\n"
    )

def gib(value: int) -> float:
    return value / (1024 ** 3)

print("=== K8s Memory Footprint ===")
print(f"JSON: {json_path}")
print(f"TSV : {tsv_path}")
print()
print(f"Nodes                    : {len(nodes)}")
print(f"Namespace                : {namespace}")
print(f"Total effective memory   : {totals['effective_memory_bytes']} bytes ({gib(totals['effective_memory_bytes']):.2f} GiB)")
print(f"Namespace pod memory     : {totals['namespace_pod_cgroup_memory_bytes']} bytes ({gib(totals['namespace_pod_cgroup_memory_bytes']):.2f} GiB)")
print(f"Runtime/orchestration    : {totals['runtime_orchestration_pss_bytes']} bytes ({gib(totals['runtime_orchestration_pss_bytes']):.2f} GiB)")
print(f"All-process PSS          : {totals['all_process_pss_bytes']} bytes ({gib(totals['all_process_pss_bytes']):.2f} GiB)")
print(f"Kernel-side counters     : {totals['kernel_side_memory_bytes']} bytes ({gib(totals['kernel_side_memory_bytes']):.2f} GiB)")
print(f"Network-related slab     : {totals['network_related_slab_active_bytes']} bytes ({gib(totals['network_related_slab_active_bytes']):.2f} GiB)")
if totals["bird_internal_bytes"] is not None:
    print(f"BIRD internal memory     : {totals['bird_internal_bytes']} bytes ({gib(totals['bird_internal_bytes']):.2f} GiB)")
PY
