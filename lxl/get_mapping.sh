#!/usr/bin/env bash
set -e

# 默认读取的命名空间
NAMESPACE=${1:-"seedemu-k3s-real-topo"}
OUTPUT_FILE="node_as_pod_mapping_$(date +%Y%m%d_%H%M%S).txt"

echo "正在扫描 [${NAMESPACE}] 中的 Pod..."
echo "排序策略：[1. 物理节点] -> [2. 所属 AS 号] -> [3. Pod 名字]"

# 1. 写入表头（根据新逻辑，将 NODE_NAME 放在第一列）
printf "%-20s %-10s %-55s\n" "NODE_NAME" "ASN" "POD_NAME" > "${OUTPUT_FILE}"
echo "----------------------------------------------------------------------------------------" >> "${OUTPUT_FILE}"

# 2. 提取 -> 解析 -> 排序 -> 格式化
kubectl get pods -n "${NAMESPACE}" --no-headers \
  -o custom-columns="POD:.metadata.name,NODE:.spec.nodeName" | \
  awk '{
      # 提取 ASN
      asn = $1;
      if (asn ~ /^as[0-9]+/) {
          sub(/^as/, "", asn);         
          sub(/[^0-9].*$/, "", asn);  
      } else {
          asn = 999999;               
      }
      # 预处理输出顺序：节点名 纯数字AS号 Pod名
      print $2, asn, $1
  }' | \
  # 🏆 核心排序逻辑变更：
  # -k1,1  : 第一优先级按【节点名】字母序排
  # -k2,2n : 第二优先级按【纯数字AS号】大小排
  # -k3,3  : 第三优先级按【Pod名】字母序排
  sort -k1,1 -k2,2n -k3,3 | \
  awk '{
      # 格式化美化阶段
      asn_str = ($2 == 999999) ? "N/A" : "AS" $2;
      printf "%-20s %-10s %-55s\n", $1, asn_str, $3
  }' >> "${OUTPUT_FILE}"

# 3. 统计总数（减去表头的两行）
TOTAL_PODS=$(($(wc -l < "${OUTPUT_FILE}") - 2))

echo "✅ 扫描与排序完成！共处理了 ${TOTAL_PODS} 个 Pod。"
echo "📄 结果已保存至: ${OUTPUT_FILE}"
echo "----------------------------------------"
echo "前 15 行预览："
head -n 17 "${OUTPUT_FILE}"
echo "----------------------------------------"