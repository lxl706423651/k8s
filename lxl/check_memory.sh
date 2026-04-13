#!/bin/bash
# 统计三台虚拟机内存使用情况
# 使用方法: ./check_memory.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${SCRIPT_DIR}/logs/memory"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUTPUT_FILE="${LOG_DIR}/memory_${TIMESTAMP}.txt"

# 虚拟机配置
NODES=(
    "192.168.122.110:master"
    "192.168.122.111:worker1"
    "192.168.122.112:worker2"
)

SSH_OPTS=(
    -o StrictHostKeyChecking=no
    -o UserKnownHostsFile=/dev/null
    -o LogLevel=ERROR
    -o ConnectTimeout=10
)

# 创建日志目录
mkdir -p "${LOG_DIR}"

# 写入文件头
echo "========================================" > "${OUTPUT_FILE}"
echo "Memory Usage Report" >> "${OUTPUT_FILE}"
echo "Timestamp: $(date '+%Y-%m-%d %H:%M:%S')" >> "${OUTPUT_FILE}"
echo "========================================" >> "${OUTPUT_FILE}"
echo "" >> "${OUTPUT_FILE}"

# 获取每台虚拟机的内存信息
for node in "${NODES[@]}"; do
    IFS=':' read -r ip name <<< "${node}"
    
    echo "--- ${name} (${ip}) ---" >> "${OUTPUT_FILE}"
    
    # 内存总量
    total=$(ssh "${SSH_OPTS[@]}" "ubuntu@${ip}" "free -h | grep Mem | awk '{print \$2}'" 2>/dev/null || echo "N/A")
    echo "  Total Memory: ${total}" >> "${OUTPUT_FILE}"
    
    # 已使用内存
    used=$(ssh "${SSH_OPTS[@]}" "ubuntu@${ip}" "free -h | grep Mem | awk '{print \$3}'" 2>/dev/null || echo "N/A")
    echo "  Used Memory: ${used}" >> "${OUTPUT_FILE}"
    
    # 可用内存
    available=$(ssh "${SSH_OPTS[@]}" "ubuntu@${ip}" "free -h | grep Mem | awk '{print \$7}'" 2>/dev/null || echo "N/A")
    echo "  Available Memory: ${available}" >> "${OUTPUT_FILE}"
    
    # 使用百分比
    percent=$(ssh "${SSH_OPTS[@]}" "ubuntu@${ip}" "free | grep Mem | awk '{printf \"%.1f\", (\$3/\$2)*100}'" 2>/dev/null || echo "N/A")
    echo "  Usage: ${percent}%" >> "${OUTPUT_FILE}"
    
    # 详细内存信息
    echo "" >> "${OUTPUT_FILE}"
    echo "  Detailed:" >> "${OUTPUT_FILE}"
    ssh "${SSH_OPTS[@]}" "ubuntu@${ip}" "free -h | tail -n +2" 2>/dev/null | sed 's/^/    /' >> "${OUTPUT_FILE}"
    
    echo "" >> "${OUTPUT_FILE}"
done

echo "Memory report saved to: ${OUTPUT_FILE}"
cat "${OUTPUT_FILE}"