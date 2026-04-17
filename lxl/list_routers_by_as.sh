#!/usr/bin/env bash
set -euo pipefail

# 默认命名空间
NAMESPACE="${1:-seedemu-k3s-real-topo}"
# 自动生成带有时间戳的文件名
OUTPUT_FILE="routers.json"

echo "正在扫描 [${NAMESPACE}] 中的路由 Pod..."
echo "正在进行 JSON 序列化并按 AS 编号排序..."

kubectl get pods -n "${NAMESPACE}" --no-headers \
  -o custom-columns="POD:.metadata.name,NODE:.spec.nodeName" | \
  # 1. 过滤目标 Pod
  grep -E '^as[0-9]+brd-r[0-9]+' | \
  # 2. 提取并纯净处理 AS 编号
  awk '{
      pod_name = $1;
      node_name = $2;
      
      asn = pod_name;
      sub(/^as/, "", asn);
      sub(/brd.*$/, "", asn);
      
      print asn, pod_name, node_name
  }' | \
  # 3. 按 AS 纯数字正序排列
  sort -k1,1n | \
  # 4. 组装为标准 JSON 格式 (完美处理最后一个元素的逗号问题)
  awk '
  BEGIN {
      print "["
      first = 1
  }
  {
      if (!first) {
          print ","
      }
      print "  {"
      print "    \"asn\": " $1 ","
      print "    \"pod_name\": \"" $2 "\","
      print "    \"node_name\": \"" $3 "\""
      printf "  }"
      first = 0
  }
  END {
      print ""
      print "]"
  }' > "${OUTPUT_FILE}"

echo "✅ JSON 导出完成！共处理了 $(grep -c '"asn"' "${OUTPUT_FILE}") 个路由节点。"
echo "📄 文件已保存至: ${OUTPUT_FILE}"