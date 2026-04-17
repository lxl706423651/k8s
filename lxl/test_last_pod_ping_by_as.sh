#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -f "${SCRIPT_DIR}/env.sh" ]]; then
  # shellcheck disable=SC1091
  source "${SCRIPT_DIR}/env.sh"
fi

if [[ -z "${KUBECONFIG:-}" && -f "${HOME}/k8s/output/kubeconfigs/seedemu-k3s.yaml" ]]; then
  export KUBECONFIG="${HOME}/k8s/output/kubeconfigs/seedemu-k3s.yaml"
fi

if [[ -z "${KUBECONFIG:-}" ]]; then
  echo "KUBECONFIG 未设置，且默认 kubeconfig 不存在。" >&2
  exit 1
fi

if ! command -v kubectl >/dev/null 2>&1; then
  echo "kubectl 不存在，请先安装 kubectl。" >&2
  exit 1
fi

if ! kubectl version --request-timeout=10s >/dev/null 2>&1; then
  echo "kubectl 当前无法访问集群，请检查 KUBECONFIG 和 API Server。" >&2
  exit 1
fi

if declare -F create_experiment_dir >/dev/null 2>&1; then
  create_experiment_dir
fi

if [[ -z "${EXPERIMENT_DIR:-}" ]]; then
  EXPERIMENT_DIR="${SCRIPT_DIR}"
fi
mkdir -p "${EXPERIMENT_DIR}"

if declare -F log_start >/dev/null 2>&1; then
  log_start "test_last_pod_ping_by_as"
fi

NAMESPACE="${1:-${SEED_NAMESPACE:-seedemu-k3s-real-topo}}"
PING_COUNT="${PING_COUNT:-1}"
PING_TIMEOUT="${PING_TIMEOUT:-2}"
MAX_PAIRS="${MAX_PAIRS:-0}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
SELECTED_FILE="${EXPERIMENT_DIR}/as_last_pods_${TIMESTAMP}.tsv"
RESULT_FILE="${EXPERIMENT_DIR}/as_last_pods_ping_${TIMESTAMP}.tsv"
SUMMARY_FILE="${EXPERIMENT_DIR}/as_last_pods_ping_summary_${TIMESTAMP}.txt"

tmp_selected_base="$(mktemp)"
tmp_selected="$(mktemp)"
trap 'rm -f "${tmp_selected_base}" "${tmp_selected}"' EXIT

get_net_endpoint() {
  local pod_name="$1"

  kubectl exec -n "${NAMESPACE}" "${pod_name}" -- sh -lc \
    "ip -o -4 addr show scope global | awk '\$2 ~ /^net_/ {split(\$4, a, \"/\"); print \$2 \"=\" a[1]}' | sort | head -n 1" \
    2>/dev/null || true
}

echo "收集 namespace=${NAMESPACE} 中每个 AS 排序后的最后一个路由 Pod ..."
kubectl get pods -n "${NAMESPACE}" --no-headers \
  -o custom-columns="POD:.metadata.name,NODE:.spec.nodeName" | \
  awk '
    BEGIN { OFS="\t" }
    match($1, /^as([0-9]+)brd-r/, m) {
      print m[1], $1, $2
    }
  ' | \
  sort -t $'\t' -k1,1n -k2,2 | \
  awk -F'\t' '
    BEGIN { OFS="\t" }
    {
      selected[$1] = $0
    }
    END {
      for (asn in selected) {
        print selected[asn]
      }
    }
  ' | sort -t $'\t' -k1,1n > "${tmp_selected_base}"

if [[ ! -s "${tmp_selected_base}" ]]; then
  echo "没有找到符合 as<ASN>brd-r* 命名规则的路由 Pod。" >&2
  exit 1
fi

while IFS=$'\t' read -r asn pod_name node_name; do
  net_endpoint="$(get_net_endpoint "${pod_name}")"
  printf "%s\t%s\t%s\t%s\n" \
    "${asn}" "${pod_name}" "${node_name}" "${net_endpoint}" >> "${tmp_selected}"
done < "${tmp_selected_base}"

{
  printf "ASN\tPOD_NAME\tNODE_NAME\tNET_ENDPOINT\n"
  cat "${tmp_selected}"
} > "${SELECTED_FILE}"

mapfile -t SELECTED_ROWS < "${tmp_selected}"
TOTAL_AS="${#SELECTED_ROWS[@]}"

{
  printf "SRC_AS\tSRC_POD\tSRC_NODE\tDST_AS\tDST_POD\tDST_NODE\tDST_IFACE\tDST_IP\tSTATUS\tDETAIL\n"
} > "${RESULT_FILE}"

echo "共选出 ${TOTAL_AS} 个 AS 对应的最后一个 Pod。"
echo "Pod 清单已保存到: ${SELECTED_FILE}"
echo "开始执行双向 ping，结果保存到: ${RESULT_FILE}"

pass_count=0
fail_count=0
skip_count=0
pair_count=0

for ((i = 0; i < TOTAL_AS; i++)); do
  IFS=$'\t' read -r src_as src_pod src_node src_endpoint <<< "${SELECTED_ROWS[i]}"

  for ((j = i + 1; j < TOTAL_AS; j++)); do
    IFS=$'\t' read -r dst_as dst_pod dst_node dst_endpoint <<< "${SELECTED_ROWS[j]}"

    if (( MAX_PAIRS > 0 && pair_count >= MAX_PAIRS )); then
      break 2
    fi

    pair_count=$((pair_count + 1))

    if [[ -z "${dst_endpoint}" ]]; then
      printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
        "${src_as}" "${src_pod}" "${src_node}" \
        "${dst_as}" "${dst_pod}" "${dst_node}" "<none>" "<empty>" \
        "SKIP" "missing-dst-net-endpoint" >> "${RESULT_FILE}"
      skip_count=$((skip_count + 1))
      continue
    fi

    dst_iface="${dst_endpoint%%=*}"
    dst_ip="${dst_endpoint#*=}"

    if [[ -z "${dst_iface}" || -z "${dst_ip}" || "${dst_ip}" == "${dst_endpoint}" ]]; then
      printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
        "${src_as}" "${src_pod}" "${src_node}" \
        "${dst_as}" "${dst_pod}" "${dst_node}" "${dst_iface:-<invalid>}" "${dst_ip:-<invalid>}" \
        "SKIP" "invalid-dst-net-endpoint" >> "${RESULT_FILE}"
      skip_count=$((skip_count + 1))
      continue
    fi

    echo "[${pair_count}] ${src_pod} -> ${dst_pod} ${dst_iface}=${dst_ip}"

    detail=""
    if output="$(kubectl exec -n "${NAMESPACE}" "${src_pod}" -- ping -c "${PING_COUNT}" -W "${PING_TIMEOUT}" "${dst_ip}" 2>&1)"; then
      if grep -q " 0% packet loss" <<< "${output}"; then
        status="PASS"
        detail="0%-packet-loss"
        pass_count=$((pass_count + 1))
      else
        status="FAIL"
        detail="ping-succeeded-without-0%-packet-loss"
        fail_count=$((fail_count + 1))
      fi
    else
      status="FAIL"
      detail="$(tr '\n' ' ' <<< "${output}" | sed 's/[[:space:]]\+/ /g' | cut -c1-200)"
      fail_count=$((fail_count + 1))
    fi

    printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
      "${src_as}" "${src_pod}" "${src_node}" \
      "${dst_as}" "${dst_pod}" "${dst_node}" "${dst_iface}" "${dst_ip}" \
      "${status}" "${detail}" >> "${RESULT_FILE}"
  done
done

{
  echo "namespace=${NAMESPACE}"
  echo "selected_as_count=${TOTAL_AS}"
  echo "tested_pairs=${pair_count}"
  echo "pass=${pass_count}"
  echo "fail=${fail_count}"
  echo "skip=${skip_count}"
  echo "selected_file=${SELECTED_FILE}"
  echo "result_file=${RESULT_FILE}"
} > "${SUMMARY_FILE}"

echo "测试完成。"
echo "汇总文件: ${SUMMARY_FILE}"
echo "Pod 清单: ${SELECTED_FILE}"
echo "结果文件: ${RESULT_FILE}"

if declare -F log_end >/dev/null 2>&1; then
  log_end "test_last_pod_ping_by_as"
fi
