#!/usr/bin/env bash
# Purpose:
#   Test whether an ARP request emitted by one SeedEMU Docker container on a
#   simulated network is visible outside that Docker network.
#
# Required inputs:
#   --source-container: Container that triggers ARP by pinging --target-ip.
#   --source-interface: Source container interface used for neighbor flush.
#   --target-ip: Destination IP that should trigger ARP resolution.
#   --container-observer label:container:interface: Container capture target.
#   --bridge-observer label:bridge: Host Docker bridge capture target.
#
# Generated outputs:
#   A timestamped output directory with tcpdump logs, interface snapshots,
#   source ping output, and result.md.
#
# Side effects:
#   Flushes the source container neighbor entry for --target-ip on
#   --source-interface and sends ICMP from the source container. It does not
#   create, delete, connect, or disconnect Docker containers/networks.
#
# Expected execution context:
#   Run on the Docker host. Source and observer containers must be running and
#   have tcpdump installed for container observers. Host bridge observers require
#   tcpdump on the host and permission to capture on bridge interfaces.

set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
CAPTURE_SECONDS=10
OUTPUT_DIR=""
SOURCE_CONTAINER=""
SOURCE_INTERFACE=""
TARGET_IP=""
declare -a CONTAINER_OBSERVERS=()
declare -a BRIDGE_OBSERVERS=()

printUsage() {
    cat <<USAGE
Usage:
  ${SCRIPT_NAME} \\
    --source-container <container> \\
    --source-interface <interface> \\
    --target-ip <ip> \\
    [--container-observer <label:container:interface> ...] \\
    [--bridge-observer <label:bridge> ...] \\
    [--capture-seconds <seconds>] \\
    [--output-dir <dir>]

Example:
  ${SCRIPT_NAME} \\
    --source-container as2brd-r101-10.101.0.2 \\
    --source-interface net_101_102 \\
    --target-ip 10.2.1.253 \\
    --container-observer target-in-prefix:as2brd-r102-10.102.0.2:net_101_102 \\
    --container-observer nonprefix-host:as150h-host_0-10.150.0.71:net0 \\
    --bridge-observer target-bridge:br-37b29e35d299 \\
    --bridge-observer nonprefix-bridge:br-4de4b2ae28c4
USAGE
}

parseArgs() {
    # Parse command line flags into global variables.
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --source-container)
                SOURCE_CONTAINER="$2"
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
            --container-observer)
                CONTAINER_OBSERVERS+=("$2")
                shift 2
                ;;
            --bridge-observer)
                BRIDGE_OBSERVERS+=("$2")
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
    # Validate required arguments and referenced Docker objects.
    local missing=0
    for value_name in SOURCE_CONTAINER SOURCE_INTERFACE TARGET_IP; do
        if [[ -z "${!value_name}" ]]; then
            echo "Missing required argument: ${value_name}" >&2
            missing=1
        fi
    done
    if [[ ${#CONTAINER_OBSERVERS[@]} -eq 0 && ${#BRIDGE_OBSERVERS[@]} -eq 0 ]]; then
        echo "At least one --container-observer or --bridge-observer is required" >&2
        missing=1
    fi
    if [[ "${missing}" -ne 0 ]]; then
        printUsage >&2
        exit 2
    fi
    if ! [[ "${CAPTURE_SECONDS}" =~ ^[0-9]+$ ]] || [[ "${CAPTURE_SECONDS}" -lt 3 ]]; then
        echo "--capture-seconds must be an integer >= 3" >&2
        exit 2
    fi
    docker inspect "${SOURCE_CONTAINER}" >/dev/null
}

sanitizeLabel() {
    # Convert a user label into a filesystem-safe basename.
    local label="$1"
    printf '%s' "${label}" | tr -c 'A-Za-z0-9._-' '_'
}

dockerExec() {
    # Execute a command inside a Docker container.
    # Args:
    #   $1=containerName, remaining args=command
    local container_name="$1"
    shift
    docker exec "${container_name}" "$@"
}

writeContainerSnapshot() {
    # Save container/network state needed to interpret results.
    docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' > "${OUTPUT_DIR}/docker-ps.txt"
    docker network ls > "${OUTPUT_DIR}/docker-network-ls.txt"
    docker inspect "${SOURCE_CONTAINER}" > "${OUTPUT_DIR}/source-container.inspect.json"
    dockerExec "${SOURCE_CONTAINER}" ip -br addr > "${OUTPUT_DIR}/source-interfaces.txt"
    dockerExec "${SOURCE_CONTAINER}" ip route > "${OUTPUT_DIR}/source-routes.txt"

    local observer
    for observer in "${CONTAINER_OBSERVERS[@]}"; do
        IFS=':' read -r label container_name iface extra <<<"${observer}"
        if [[ -z "${label}" || -z "${container_name}" || -z "${iface}" || -n "${extra:-}" ]]; then
            echo "Invalid --container-observer: ${observer}; expected label:container:interface" >&2
            exit 2
        fi
        local safe_label
        safe_label="$(sanitizeLabel "${label}")"
        docker inspect "${container_name}" > "${OUTPUT_DIR}/container-observer-${safe_label}.inspect.json"
        dockerExec "${container_name}" ip -br addr > "${OUTPUT_DIR}/container-observer-${safe_label}-interfaces.txt"
        dockerExec "${container_name}" ip route > "${OUTPUT_DIR}/container-observer-${safe_label}-routes.txt"
    done

    for observer in "${BRIDGE_OBSERVERS[@]}"; do
        IFS=':' read -r label bridge_name extra <<<"${observer}"
        if [[ -z "${label}" || -z "${bridge_name}" || -n "${extra:-}" ]]; then
            echo "Invalid --bridge-observer: ${observer}; expected label:bridge" >&2
            exit 2
        fi
        local safe_label
        safe_label="$(sanitizeLabel "${label}")"
        ip -br addr show "${bridge_name}" > "${OUTPUT_DIR}/bridge-observer-${safe_label}-addr.txt"
        bridge link show master "${bridge_name}" > "${OUTPUT_DIR}/bridge-observer-${safe_label}-ports.txt" 2>&1 || true
    done
}

startContainerCapture() {
    # Start one bounded tcpdump capture inside a container.
    # Args:
    #   $1=label, $2=containerName, $3=interfaceName
    local label="$1"
    local container_name="$2"
    local iface="$3"
    local safe_label
    safe_label="$(sanitizeLabel "${label}")"
    local log_path="${OUTPUT_DIR}/container-observer-${safe_label}.tcpdump.log"

    echo "[capture:container] ${label}: container=${container_name} iface=${iface} log=${log_path}"
    (
        set +e
        timeout "${CAPTURE_SECONDS}s" \
            docker exec "${container_name}" \
            tcpdump -l -nn -e -i "${iface}" "arp and host ${TARGET_IP}" \
            > "${log_path}" 2>&1
        echo $? > "${OUTPUT_DIR}/container-observer-${safe_label}.tcpdump.rc"
    ) &
    echo "$!" > "${OUTPUT_DIR}/container-observer-${safe_label}.pid"
}

startBridgeCapture() {
    # Start one bounded tcpdump capture on a host Docker bridge.
    # Args:
    #   $1=label, $2=bridgeName
    local label="$1"
    local bridge_name="$2"
    local safe_label
    safe_label="$(sanitizeLabel "${label}")"
    local log_path="${OUTPUT_DIR}/bridge-observer-${safe_label}.tcpdump.log"

    echo "[capture:bridge] ${label}: bridge=${bridge_name} log=${log_path}"
    (
        set +e
        local tcpdump_prefix=()
        if [[ "$(id -u)" -ne 0 ]]; then
            tcpdump_prefix=(sudo -n)
        fi
        timeout "${CAPTURE_SECONDS}s" \
            "${tcpdump_prefix[@]}" tcpdump -l -nn -e -i "${bridge_name}" "arp and host ${TARGET_IP}" \
            > "${log_path}" 2>&1
        echo $? > "${OUTPUT_DIR}/bridge-observer-${safe_label}.tcpdump.rc"
    ) &
    echo "$!" > "${OUTPUT_DIR}/bridge-observer-${safe_label}.pid"
}

triggerArp() {
    # Flush source neighbor cache and ping target to trigger ARP.
    local flush_log="${OUTPUT_DIR}/source-neigh-flush.log"
    local ping_log="${OUTPUT_DIR}/source-ping.log"

    echo "[trigger] flushing ${TARGET_IP} on ${SOURCE_CONTAINER}:${SOURCE_INTERFACE}"
    set +e
    dockerExec "${SOURCE_CONTAINER}" ip neigh flush "${TARGET_IP}" dev "${SOURCE_INTERFACE}" > "${flush_log}" 2>&1
    local flush_rc=$?
    if [[ "${flush_rc}" -ne 0 ]]; then
        dockerExec "${SOURCE_CONTAINER}" ip neigh flush dev "${SOURCE_INTERFACE}" >> "${flush_log}" 2>&1
        flush_rc=$?
    fi
    echo "${flush_rc}" > "${OUTPUT_DIR}/source-neigh-flush.rc"

    echo "[trigger] pinging ${TARGET_IP} from ${SOURCE_CONTAINER}"
    dockerExec "${SOURCE_CONTAINER}" ping -c 3 -W 2 "${TARGET_IP}" > "${ping_log}" 2>&1
    local ping_rc=$?
    echo "${ping_rc}" > "${OUTPUT_DIR}/source-ping.rc"
    set -e
}

waitForCaptures() {
    # Wait for every tcpdump background job to finish.
    local pid_file
    for pid_file in "${OUTPUT_DIR}"/*.pid; do
        [[ -f "${pid_file}" ]] || continue
        local pid
        pid="$(cat "${pid_file}")"
        wait "${pid}" || true
    done
}

logSawArpRequest() {
    # Return success if a capture log saw an ARP request for the target IP.
    # Args:
    #   $1=logPath
    local log_path="$1"
    grep -Eq "ARP, Request who-has ${TARGET_IP} tell|who-has ${TARGET_IP}" "${log_path}"
}

writeResultMarkdown() {
    # Render result.md with summary and selected tcpdump output.
    local result_path="${OUTPUT_DIR}/result.md"
    {
        echo "# Docker ARP isolation test"
        echo
        echo "## Test Setup"
        echo
        echo "- Source container: \`${SOURCE_CONTAINER}\`"
        echo "- Source interface: \`${SOURCE_INTERFACE}\`"
        echo "- Target IP: \`${TARGET_IP}\`"
        echo "- Capture seconds: \`${CAPTURE_SECONDS}\`"
        echo "- Output directory: \`${OUTPUT_DIR}\`"
        echo
        echo "## Result Summary"
        echo
        echo "| Type | Observer | Target | Saw ARP for target? |"
        echo "| --- | --- | --- | --- |"
        local observer safe_label log_path saw
        for observer in "${CONTAINER_OBSERVERS[@]}"; do
            IFS=':' read -r label container_name iface <<<"${observer}"
            safe_label="$(sanitizeLabel "${label}")"
            log_path="${OUTPUT_DIR}/container-observer-${safe_label}.tcpdump.log"
            saw="no"
            if logSawArpRequest "${log_path}"; then
                saw="yes"
            fi
            echo "| container | \`${label}\` | \`${container_name}:${iface}\` | \`${saw}\` |"
        done
        for observer in "${BRIDGE_OBSERVERS[@]}"; do
            IFS=':' read -r label bridge_name <<<"${observer}"
            safe_label="$(sanitizeLabel "${label}")"
            log_path="${OUTPUT_DIR}/bridge-observer-${safe_label}.tcpdump.log"
            saw="no"
            if logSawArpRequest "${log_path}"; then
                saw="yes"
            fi
            echo "| bridge | \`${label}\` | \`${bridge_name}\` | \`${saw}\` |"
        done
        echo
        echo "## Source Ping"
        echo
        echo '```text'
        sed -n '1,80p' "${OUTPUT_DIR}/source-ping.log"
        echo '```'
        echo
        echo "## Captures"
        for observer in "${CONTAINER_OBSERVERS[@]}"; do
            IFS=':' read -r label _container_name _iface <<<"${observer}"
            safe_label="$(sanitizeLabel "${label}")"
            echo
            echo "### container: ${label}"
            echo
            echo '```text'
            sed -n '1,120p' "${OUTPUT_DIR}/container-observer-${safe_label}.tcpdump.log"
            echo '```'
        done
        for observer in "${BRIDGE_OBSERVERS[@]}"; do
            IFS=':' read -r label _bridge_name <<<"${observer}"
            safe_label="$(sanitizeLabel "${label}")"
            echo
            echo "### bridge: ${label}"
            echo
            echo '```text'
            sed -n '1,120p' "${OUTPUT_DIR}/bridge-observer-${safe_label}.tcpdump.log"
            echo '```'
        done
        echo
        echo "## Interpretation"
        echo
        echo "- If a non-prefix container or bridge sees the ARP request, that Docker deployment leaks the ARP broadcast outside the intended simulated network."
        echo "- If only the target network container/bridge sees the ARP request, Docker's per-network bridge isolation is working for this test."
        echo "- This script does not modify Docker resources. It only flushes one neighbor entry and sends ICMP from the source container."
    } > "${result_path}"
    echo "[result] ${result_path}"
}

main() {
    parseArgs "$@"
    requireInputs

    if [[ -z "${OUTPUT_DIR}" ]]; then
        OUTPUT_DIR="$(pwd)/docker-arp-isolation-$(date +%Y%m%d_%H%M%S)"
    fi
    mkdir -p "${OUTPUT_DIR}"

    echo "[setup] output_dir=${OUTPUT_DIR}"
    writeContainerSnapshot

    local observer
    for observer in "${CONTAINER_OBSERVERS[@]}"; do
        IFS=':' read -r label container_name iface _extra <<<"${observer}"
        startContainerCapture "${label}" "${container_name}" "${iface}"
    done
    for observer in "${BRIDGE_OBSERVERS[@]}"; do
        IFS=':' read -r label bridge_name _extra <<<"${observer}"
        startBridgeCapture "${label}" "${bridge_name}"
    done

    sleep 2
    triggerArp
    waitForCaptures
    writeResultMarkdown
}

main "$@"
