#!/usr/bin/env bash
# Purpose:
#   Test whether an ARP request emitted on one SeedEMU macvlan-backed Pod
#   interface is visible on observer Pod interfaces that do not belong to the
#   same simulated IP prefix.
#
# Required inputs:
#   --kubeconfig: kubeconfig used to access the target K3s cluster.
#   --namespace: Kubernetes namespace containing the SeedEMU workload.
#   --source-pod: Pod that will trigger ARP by pinging --target-ip.
#   --source-interface: Source Pod interface used for neighbor cache flush.
#   --target-ip: Destination IP that forces ARP resolution.
#   --observer label:pod:interface: Observer capture tuple. Can be repeated.
#
# Generated outputs:
#   A timestamped output directory containing tcpdump logs, source ping output,
#   and result.md.
#
# Side effects:
#   Flushes the source Pod neighbor entry for --target-ip on --source-interface.
#   It does not create, delete, or modify Kubernetes resources.
#
# Expected execution context:
#   Run from a machine with kubectl access to the cluster. Target Pods must have
#   tcpdump installed and enough privileges to capture on their interfaces.

set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
CAPTURE_SECONDS=10
OUTPUT_DIR=""
KUBECONFIG_PATH=""
NAMESPACE=""
SOURCE_POD=""
SOURCE_INTERFACE=""
TARGET_IP=""
declare -a OBSERVERS=()

printUsage() {
    cat <<USAGE
Usage:
  ${SCRIPT_NAME} \\
    --kubeconfig <path> \\
    --namespace <namespace> \\
    --source-pod <pod> \\
    --source-interface <interface> \\
    --target-ip <ip> \\
    --observer <label:pod:interface> [--observer <label:pod:interface> ...] \\
    [--capture-seconds <seconds>] \\
    [--output-dir <dir>]

Example:
  ${SCRIPT_NAME} \\
    --kubeconfig /home/lxl/k8s/output/kubeconfigs/seedemu-k3s.yaml \\
    --namespace seedemu-k3s-real-topo \\
    --source-pod as2brd-r101-10.101.0.2-6bfb57c455-6b99s \\
    --source-interface net_101_102 \\
    --target-ip 10.2.1.253 \\
    --observer same-node-nonprefix:as160brd-router0-10.160.0.254-58db8fbc4d-snrnf:net0 \\
    --observer cross-node-nonprefix:as150h-host-0-10.150.0.71-76647f4d48-rv7gv:net0
USAGE
}

parseArgs() {
    # Parse CLI flags into global variables.
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --kubeconfig)
                KUBECONFIG_PATH="$2"
                shift 2
                ;;
            --namespace)
                NAMESPACE="$2"
                shift 2
                ;;
            --source-pod)
                SOURCE_POD="$2"
                shift 2
                ;;
            --source-interface)
                SOURCE_INTERFACE="$2"
                shift 2
                ;;
            --target-ip)
                TARGET_IP="$2"
                shift 2
                ;;
            --observer)
                OBSERVERS+=("$2")
                shift 2
                ;;
            --capture-seconds)
                CAPTURE_SECONDS="$2"
                shift 2
                ;;
            --output-dir)
                OUTPUT_DIR="$2"
                shift 2
                ;;
            -h|--help)
                printUsage
                exit 0
                ;;
            *)
                echo "Unknown argument: $1" >&2
                printUsage >&2
                exit 2
                ;;
        esac
    done
}

requireInputs() {
    # Validate all required CLI values before touching the cluster.
    local missing=0
    for value_name in KUBECONFIG_PATH NAMESPACE SOURCE_POD SOURCE_INTERFACE TARGET_IP; do
        if [[ -z "${!value_name}" ]]; then
            echo "Missing required argument: ${value_name}" >&2
            missing=1
        fi
    done
    if [[ ${#OBSERVERS[@]} -eq 0 ]]; then
        echo "Missing required argument: --observer" >&2
        missing=1
    fi
    if [[ "${missing}" -ne 0 ]]; then
        printUsage >&2
        exit 2
    fi
    if [[ ! -f "${KUBECONFIG_PATH}" ]]; then
        echo "kubeconfig not found: ${KUBECONFIG_PATH}" >&2
        exit 2
    fi
    if ! [[ "${CAPTURE_SECONDS}" =~ ^[0-9]+$ ]] || [[ "${CAPTURE_SECONDS}" -lt 3 ]]; then
        echo "--capture-seconds must be an integer >= 3" >&2
        exit 2
    fi
}

sanitizeLabel() {
    # Convert an observer label into a filesystem-safe basename.
    local label="$1"
    printf '%s' "${label}" | tr -c 'A-Za-z0-9._-' '_'
}

kubectlExec() {
    # Execute a command in one Pod.
    # Args:
    #   $1=podName, remaining arguments=command and args passed after --
    local pod_name="$1"
    shift
    kubectl --kubeconfig "${KUBECONFIG_PATH}" -n "${NAMESPACE}" exec "${pod_name}" -- "$@"
}

writeClusterSnapshot() {
    # Save lightweight context used to interpret the capture results.
    kubectl --kubeconfig "${KUBECONFIG_PATH}" -n "${NAMESPACE}" get pod "${SOURCE_POD}" -o wide > "${OUTPUT_DIR}/source-pod.txt"
    kubectlExec "${SOURCE_POD}" ip -br addr > "${OUTPUT_DIR}/source-interfaces.txt"
    for observer in "${OBSERVERS[@]}"; do
        IFS=':' read -r label pod_name iface <<<"${observer}"
        local safe_label
        safe_label="$(sanitizeLabel "${label}")"
        kubectl --kubeconfig "${KUBECONFIG_PATH}" -n "${NAMESPACE}" get pod "${pod_name}" -o wide > "${OUTPUT_DIR}/observer-${safe_label}-pod.txt"
        kubectlExec "${pod_name}" ip -br addr > "${OUTPUT_DIR}/observer-${safe_label}-interfaces.txt"
    done
}

startCapture() {
    # Start one bounded tcpdump capture in the background.
    # Args:
    #   $1=observerLabel, $2=podName, $3=interfaceName
    local label="$1"
    local pod_name="$2"
    local iface="$3"
    local safe_label
    safe_label="$(sanitizeLabel "${label}")"
    local log_path="${OUTPUT_DIR}/observer-${safe_label}.tcpdump.log"

    echo "[capture] ${label}: pod=${pod_name} iface=${iface} log=${log_path}"
    (
        set +e
        timeout "${CAPTURE_SECONDS}s" \
            kubectl --kubeconfig "${KUBECONFIG_PATH}" -n "${NAMESPACE}" exec "${pod_name}" -- \
            tcpdump -l -nn -e -i "${iface}" "arp and host ${TARGET_IP}" \
            > "${log_path}" 2>&1
        echo $? > "${OUTPUT_DIR}/observer-${safe_label}.tcpdump.rc"
    ) &
    echo "$!" > "${OUTPUT_DIR}/observer-${safe_label}.pid"
}

triggerArp() {
    # Flush the source neighbor entry and ping target IP to force ARP.
    local flush_log="${OUTPUT_DIR}/source-neigh-flush.log"
    local ping_log="${OUTPUT_DIR}/source-ping.log"

    echo "[trigger] flushing ${TARGET_IP} on ${SOURCE_POD}:${SOURCE_INTERFACE}"
    set +e
    kubectlExec "${SOURCE_POD}" ip neigh flush "${TARGET_IP}" dev "${SOURCE_INTERFACE}" > "${flush_log}" 2>&1
    local flush_rc=$?
    if [[ "${flush_rc}" -ne 0 ]]; then
        kubectlExec "${SOURCE_POD}" ip neigh flush dev "${SOURCE_INTERFACE}" >> "${flush_log}" 2>&1
        flush_rc=$?
    fi
    echo "${flush_rc}" > "${OUTPUT_DIR}/source-neigh-flush.rc"

    echo "[trigger] pinging ${TARGET_IP} from ${SOURCE_POD}"
    kubectlExec "${SOURCE_POD}" ping -c 3 -W 2 "${TARGET_IP}" > "${ping_log}" 2>&1
    local ping_rc=$?
    echo "${ping_rc}" > "${OUTPUT_DIR}/source-ping.rc"
    set -e
}

waitForCaptures() {
    # Wait for all tcpdump background jobs to complete.
    local pid_file
    for pid_file in "${OUTPUT_DIR}"/observer-*.pid; do
        [[ -f "${pid_file}" ]] || continue
        local pid
        pid="$(cat "${pid_file}")"
        wait "${pid}" || true
    done
}

observerSawArp() {
    # Return success if the observer tcpdump log saw a target ARP request.
    # Args:
    #   $1=logPath
    local log_path="$1"
    grep -Eq "ARP, Request who-has ${TARGET_IP} tell|who-has ${TARGET_IP}" "${log_path}"
}

writeResultMarkdown() {
    # Create a compact Markdown report next to the raw logs.
    local result_path="${OUTPUT_DIR}/result.md"
    {
        echo "# macvlan ARP visibility test"
        echo
        echo "## Test Setup"
        echo
        echo "- Namespace: \`${NAMESPACE}\`"
        echo "- Source Pod: \`${SOURCE_POD}\`"
        echo "- Source interface: \`${SOURCE_INTERFACE}\`"
        echo "- Target IP: \`${TARGET_IP}\`"
        echo "- Capture seconds: \`${CAPTURE_SECONDS}\`"
        echo "- Output directory: \`${OUTPUT_DIR}\`"
        echo
        echo "## Result Summary"
        echo
        echo "| Observer | Pod | Interface | Saw ARP for target? |"
        echo "| --- | --- | --- | --- |"
        for observer in "${OBSERVERS[@]}"; do
            IFS=':' read -r label pod_name iface <<<"${observer}"
            local safe_label
            safe_label="$(sanitizeLabel "${label}")"
            local log_path="${OUTPUT_DIR}/observer-${safe_label}.tcpdump.log"
            local saw="no"
            if observerSawArp "${log_path}"; then
                saw="yes"
            fi
            echo "| \`${label}\` | \`${pod_name}\` | \`${iface}\` | \`${saw}\` |"
        done
        echo
        echo "## Source Ping"
        echo
        echo '```text'
        sed -n '1,80p' "${OUTPUT_DIR}/source-ping.log"
        echo '```'
        echo
        echo "## Observer Captures"
        for observer in "${OBSERVERS[@]}"; do
            IFS=':' read -r label _pod_name _iface <<<"${observer}"
            local safe_label
            safe_label="$(sanitizeLabel "${label}")"
            local log_path="${OUTPUT_DIR}/observer-${safe_label}.tcpdump.log"
            echo
            echo "### ${label}"
            echo
            echo '```text'
            sed -n '1,120p' "${log_path}"
            echo '```'
        done
        echo
        echo "## Interpretation"
        echo
        echo "- If a non-\`10.2.1.0/24\` observer sees \`ARP, Request who-has ${TARGET_IP}\`, the ARP broadcast is visible outside the simulated IP prefix at the current macvlan/L2 layer."
        echo "- If it does not see the packet, this script alone cannot prove strict L2 isolation; it only proves the selected observer interface did not receive the captured ARP during this run."
        echo "- This test does not modify Kubernetes resources. It only flushes one source neighbor entry and sends ICMP from the source Pod."
    } > "${result_path}"
    echo "[result] ${result_path}"
}

main() {
    parseArgs "$@"
    requireInputs

    if [[ -z "${OUTPUT_DIR}" ]]; then
        OUTPUT_DIR="$(pwd)/macvlan-arp-visibility-$(date +%Y%m%d_%H%M%S)"
    fi
    mkdir -p "${OUTPUT_DIR}"

    echo "[setup] output_dir=${OUTPUT_DIR}"
    writeClusterSnapshot

    for observer in "${OBSERVERS[@]}"; do
        IFS=':' read -r label pod_name iface extra <<<"${observer}"
        if [[ -z "${label}" || -z "${pod_name}" || -z "${iface}" || -n "${extra:-}" ]]; then
            echo "Invalid --observer format: ${observer}; expected label:pod:interface" >&2
            exit 2
        fi
        startCapture "${label}" "${pod_name}" "${iface}"
    done

    sleep 2
    triggerArp
    waitForCaptures
    writeResultMarkdown
}

main "$@"
