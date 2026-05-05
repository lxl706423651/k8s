#!/usr/bin/env bash
#set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck disable=SC1091
source "${SCRIPT_DIR}/env_9node.sh" >/dev/null 2>&1
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/01_cluster_nodes_9node.sh"

seed_load_cluster_nodes

usage() {
    cat <<'EOF'
Usage:
  13_measure_cluster_memory_9node.sh [--label LABEL] [--out-dir DIR]
  13_measure_cluster_memory_9node.sh --compare BEFORE_JSON AFTER_JSON

What it measures:
  1. guest_used_bytes:
     Per-VM in-guest used memory, approximated as MemTotal - MemAvailable.
  2. qemu_rss_bytes:
     Host-side physical memory currently resident for each VM's qemu process.

Recommended workflow:
  1. Run once before deploy:
       ./13_measure_cluster_memory_9node.sh --label before
  2. Run once after the target scale is stable:
       ./13_measure_cluster_memory_9node.sh --label after
  3. Compare:
       ./13_measure_cluster_memory_9node.sh --compare before.json after.json

Notes:
  - For "actual host RAM consumed", use qemu_rss_bytes.
  - For "cluster internal used memory", use guest_used_bytes.
EOF
}

to_gib() {
    python3 - "$1" <<'PY'
import sys
value = int(sys.argv[1])
print(f"{value / (1024 ** 3):.2f}")
PY
}

json_escape() {
    python3 - "$1" <<'PY'
import json
import sys
print(json.dumps(sys.argv[1]))
PY
}

compare_mode=0
before_json=""
after_json=""
label=""
out_dir=""

while [ "$#" -gt 0 ]; do
    case "$1" in
        --label)
            label="${2:-}"
            shift 2
            ;;
        --out-dir)
            out_dir="${2:-}"
            shift 2
            ;;
        --compare)
            compare_mode=1
            before_json="${2:-}"
            after_json="${3:-}"
            shift 3
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

if [ "${compare_mode}" -eq 1 ]; then
    if [ -z "${before_json}" ] || [ -z "${after_json}" ]; then
        echo "--compare requires BEFORE_JSON and AFTER_JSON" >&2
        exit 1
    fi
    python3 - "${before_json}" "${after_json}" <<'PY'
import json
import sys
from pathlib import Path

before_path = Path(sys.argv[1])
after_path = Path(sys.argv[2])

with before_path.open("r", encoding="utf-8") as fh:
    before = json.load(fh)
with after_path.open("r", encoding="utf-8") as fh:
    after = json.load(fh)

def gib(value):
    return value / (1024 ** 3)

print("=== Memory Snapshot Diff ===")
print(f"before: {before_path}")
print(f"after : {after_path}")
print()
print("Totals:")
for key in ("guest_used_bytes", "qemu_rss_bytes", "assigned_memory_bytes"):
    b = int(before["totals"].get(key, 0))
    a = int(after["totals"].get(key, 0))
    d = a - b
    print(f"  {key}: before={gib(b):.2f} GiB after={gib(a):.2f} GiB delta={gib(d):.2f} GiB")

print()
print("Per node delta:")
before_nodes = {n["name"]: n for n in before["nodes"]}
after_nodes = {n["name"]: n for n in after["nodes"]}
all_names = sorted(set(before_nodes) | set(after_nodes))
for name in all_names:
    b = before_nodes.get(name, {})
    a = after_nodes.get(name, {})
    bg = int(b.get("guest_used_bytes", 0))
    ag = int(a.get("guest_used_bytes", 0))
    bq = int(b.get("qemu_rss_bytes", 0))
    aq = int(a.get("qemu_rss_bytes", 0))
    print(
        f"  {name}: "
        f"guest_delta={gib(ag - bg):.2f} GiB "
        f"qemu_rss_delta={gib(aq - bq):.2f} GiB"
    )
PY
    exit 0
fi

timestamp="$(date +%Y%m%d_%H%M%S)"
if [ -z "${label}" ]; then
    label="${timestamp}"
fi

if [ -z "${out_dir}" ]; then
    out_dir="${LOG_BASE_DIR}/memory_snapshots"
fi
mkdir -p "${out_dir}"

snapshot_base="${out_dir}/${timestamp}_${label}"
json_path="${snapshot_base}.json"
tsv_path="${snapshot_base}.tsv"

declare -A NODE_IP_BY_NAME=()
for i in "${!SEED_NODE_NAMES[@]}"; do
    NODE_IP_BY_NAME["${SEED_NODE_NAMES[$i]}"]="${SEED_NODE_IPS[$i]}"
done

declare -A QEMU_RSS_KB=()
declare -A ASSIGNED_BYTES=()

while IFS=$'\t' read -r name rss_kb assigned_bytes; do
    [ -n "${name}" ] || continue
    QEMU_RSS_KB["${name}"]="${rss_kb}"
    ASSIGNED_BYTES["${name}"]="${assigned_bytes}"
done < <(
    python3 - <<'PY'
import re
import subprocess

cmd = "ps -eo rss,args | grep qemu-system | grep -v grep"
out = subprocess.check_output(cmd, shell=True, text=True)
for line in out.splitlines():
    m = re.match(r"\s*(\d+)\s+.*guest=(seed-k3s-[^,]+)", line)
    if not m:
        continue
    rss_kb = int(m.group(1))
    name = m.group(2)
    mem_match = re.search(r"-object \{\"qom-type\":\"memory-backend-ram\",\"id\":\"pc\.ram\",\"size\":(\d+)\}", line)
    assigned_bytes = int(mem_match.group(1)) if mem_match else 0
    print(f"{name}\t{rss_kb}\t{assigned_bytes}")
PY
)

nodes_json=""
total_guest_used_bytes=0
total_qemu_rss_bytes=0
total_assigned_bytes=0

printf "name\tip\tguest_used_bytes\tguest_used_gib\tguest_total_bytes\tguest_available_bytes\tqemu_rss_bytes\tqemu_rss_gib\tassigned_memory_bytes\tassigned_memory_gib\n" > "${tsv_path}"

for node_name in "${SEED_NODE_NAMES[@]}"; do
    node_ip="${NODE_IP_BY_NAME[$node_name]}"

    mem_pair="$(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 "${SEED_K3S_USER}@${node_ip}" \
        "awk '/MemTotal:/{t=\$2}/MemAvailable:/{a=\$2} END{printf \"%s %s\n\", t,a}' /proc/meminfo")"

    mem_total_kb="$(awk '{print $1}' <<< "${mem_pair}")"
    mem_available_kb="$(awk '{print $2}' <<< "${mem_pair}")"

    mem_total_bytes=$((mem_total_kb * 1024))
    mem_available_bytes=$((mem_available_kb * 1024))
    guest_used_bytes=$((mem_total_bytes - mem_available_bytes))

    qemu_rss_kb="${QEMU_RSS_KB[$node_name]:-0}"
    qemu_rss_bytes=$((qemu_rss_kb * 1024))
    assigned_bytes="${ASSIGNED_BYTES[$node_name]:-0}"

    total_guest_used_bytes=$((total_guest_used_bytes + guest_used_bytes))
    total_qemu_rss_bytes=$((total_qemu_rss_bytes + qemu_rss_bytes))
    total_assigned_bytes=$((total_assigned_bytes + assigned_bytes))

    printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
        "${node_name}" \
        "${node_ip}" \
        "${guest_used_bytes}" \
        "$(to_gib "${guest_used_bytes}")" \
        "${mem_total_bytes}" \
        "${mem_available_bytes}" \
        "${qemu_rss_bytes}" \
        "$(to_gib "${qemu_rss_bytes}")" \
        "${assigned_bytes}" \
        "$(to_gib "${assigned_bytes}")" \
        >> "${tsv_path}"

    node_json=$(
        cat <<EOF
{
  "name": $(json_escape "${node_name}"),
  "ip": $(json_escape "${node_ip}"),
  "guest_used_bytes": ${guest_used_bytes},
  "guest_total_bytes": ${mem_total_bytes},
  "guest_available_bytes": ${mem_available_bytes},
  "qemu_rss_bytes": ${qemu_rss_bytes},
  "assigned_memory_bytes": ${assigned_bytes}
}
EOF
    )
    if [ -n "${nodes_json}" ]; then
        nodes_json="${nodes_json},"
    fi
    nodes_json="${nodes_json}${node_json}"
done

cat > "${json_path}" <<EOF
{
  "label": $(json_escape "${label}"),
  "timestamp": $(json_escape "${timestamp}"),
  "topology_size": $(json_escape "${SEED_TOPOLOGY_SIZE}"),
  "namespace": $(json_escape "${SEED_NAMESPACE}"),
  "cluster_inventory": $(json_escape "${SEED_CLUSTER_INVENTORY}"),
  "totals": {
    "guest_used_bytes": ${total_guest_used_bytes},
    "qemu_rss_bytes": ${total_qemu_rss_bytes},
    "assigned_memory_bytes": ${total_assigned_bytes}
  },
  "nodes": [
${nodes_json}
  ]
}
EOF

echo "=== Cluster Memory Snapshot ==="
echo "label: ${label}"
echo "json : ${json_path}"
echo "tsv  : ${tsv_path}"
echo
echo "Totals:"
echo "  guest_used_bytes     = ${total_guest_used_bytes} ($(to_gib "${total_guest_used_bytes}") GiB)"
echo "  qemu_rss_bytes       = ${total_qemu_rss_bytes} ($(to_gib "${total_qemu_rss_bytes}") GiB)"
echo "  assigned_memory      = ${total_assigned_bytes} ($(to_gib "${total_assigned_bytes}") GiB)"
echo
echo "Interpretation:"
echo "  - guest_used_bytes: VM 内部视角的已用内存总和"
echo "  - qemu_rss_bytes  : 宿主机当前为这些 VM 实际驻留的物理内存总和"
echo "  - assigned_memory : 9 台 VM 分配内存总和"
echo
echo "To compare two snapshots:"
echo "  ${SCRIPT_DIR}/13_measure_cluster_memory_9node.sh --compare BEFORE_JSON AFTER_JSON"
