#!/usr/bin/env python3
"""Validate one deterministic cross-node SeedEMU workload path.

The validator selects the lexicographically first pair of ready non-IX router
Pods that share a Multus network. Cross-host mode requires different management
/24s; cross-node mode only requires different K3s nodes. The fixed pair must
contain iperf3 and pass bidirectional ping and TCP iperf3.
"""

from __future__ import annotations

import argparse
import ipaddress
import itertools
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml


NETWORK_STATUS_ANNOTATION = "k8s.v1.cni.cncf.io/network-status"
NETWORKS_ANNOTATION = "k8s.v1.cni.cncf.io/networks"


def loadYaml(path: Path) -> dict[str, Any]:
    """Load one YAML mapping."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"Invalid YAML root in {path}: expected a mapping")
    return data


def writeJson(path: Path, data: dict[str, Any]) -> None:
    """Write one formatted JSON result."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def kubectl(
    kubeconfig: Path,
    args: list[str],
    *,
    request_timeout: int,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run kubectl with bounded client and subprocess timeouts."""
    return subprocess.run(
        [
            "kubectl",
            "--kubeconfig",
            str(kubeconfig),
            f"--request-timeout={request_timeout}s",
            *args,
        ],
        text=True,
        capture_output=True,
        check=check,
        timeout=request_timeout + 15,
    )


def execPod(
    kubeconfig: Path,
    namespace: str,
    pod: str,
    command: list[str],
    *,
    request_timeout: int,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Execute one argv-style command in a Pod."""
    return kubectl(
        kubeconfig,
        ["-n", namespace, "exec", pod, "--", *command],
        request_timeout=request_timeout,
        check=check,
    )


def nodeManagementGroups(config: dict[str, Any]) -> dict[str, str]:
    """Map node names to deterministic management /24 identifiers."""
    raw_nodes = config.get("nodes")
    nodes = raw_nodes if isinstance(raw_nodes, list) else []
    groups: dict[str, str] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        name = str(node.get("name") or "")
        address = str(node.get("ip") or "")
        if not name or not address:
            continue
        ip = ipaddress.ip_address(address)
        groups[name] = str(ipaddress.ip_network(f"{ip}/24", strict=False))
    return groups


def normalizeNetworkName(value: Any) -> str:
    """Return the namespace-free NetworkAttachmentDefinition name."""
    name = str(value or "").strip()
    return name.rsplit("/", 1)[-1]


def parseNetworkEntries(annotations: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Return secondary (network, IP, interface) entries from Multus annotations."""
    entries: list[tuple[str, str, str]] = []
    raw_status = annotations.get(NETWORK_STATUS_ANNOTATION)
    if isinstance(raw_status, str) and raw_status.strip():
        try:
            status = json.loads(raw_status)
        except json.JSONDecodeError:
            status = []
        if isinstance(status, list):
            for item in status:
                if not isinstance(item, dict) or item.get("default"):
                    continue
                network = normalizeNetworkName(item.get("name"))
                ips = item.get("ips")
                interface = str(item.get("interface") or "")
                if not network or not isinstance(ips, list):
                    continue
                for address in ips:
                    ip = str(address or "").split("/", 1)[0]
                    if ip:
                        entries.append((network, ip, interface))
    if entries:
        return sorted(set(entries))

    raw_networks = annotations.get(NETWORKS_ANNOTATION)
    if not isinstance(raw_networks, str) or not raw_networks.strip():
        return []
    try:
        networks = json.loads(raw_networks)
    except json.JSONDecodeError:
        return []
    if not isinstance(networks, list):
        return []
    for item in networks:
        if isinstance(item, str):
            continue
        if not isinstance(item, dict):
            continue
        network = normalizeNetworkName(item.get("name"))
        ips = item.get("ips")
        interface = str(item.get("interface") or "")
        if not network or not isinstance(ips, list):
            continue
        for address in ips:
            ip = str(address or "").split("/", 1)[0]
            if ip:
                entries.append((network, ip, interface))
    return sorted(set(entries))


def readyRouterEndpoints(
    kubeconfig: Path,
    namespace: str,
    node_groups: dict[str, str],
    request_timeout: int,
) -> dict[str, list[dict[str, str]]]:
    """Index ready router Pod secondary addresses by network."""
    response = kubectl(
        kubeconfig,
        [
            "-n",
            namespace,
            "get",
            "pods",
            "-l",
            "seedemu.io/role=brd",
            "-o",
            "json",
        ],
        request_timeout=request_timeout,
    )
    payload = json.loads(response.stdout)
    by_network: dict[str, list[dict[str, str]]] = {}
    for pod in payload.get("items", []):
        if not isinstance(pod, dict):
            continue
        metadata = pod.get("metadata") if isinstance(pod.get("metadata"), dict) else {}
        spec = pod.get("spec") if isinstance(pod.get("spec"), dict) else {}
        status = pod.get("status") if isinstance(pod.get("status"), dict) else {}
        labels = metadata.get("labels") if isinstance(metadata.get("labels"), dict) else {}
        conditions = status.get("conditions") if isinstance(status.get("conditions"), list) else []
        ready = any(
            isinstance(condition, dict)
            and condition.get("type") == "Ready"
            and condition.get("status") == "True"
            for condition in conditions
        )
        node = str(spec.get("nodeName") or "")
        pod_name = str(metadata.get("name") or "")
        router_name = str(labels.get("seedemu.io/name") or "")
        group = node_groups.get(node)
        if (
            not ready
            or not node
            or not pod_name
            or group is None
            or router_name.startswith("ix")
        ):
            continue
        annotations = (
            metadata.get("annotations")
            if isinstance(metadata.get("annotations"), dict)
            else {}
        )
        for network, address, interface in parseNetworkEntries(annotations):
            by_network.setdefault(network, []).append(
                {
                    "network": network,
                    "pod": pod_name,
                    "node": node,
                    "routerName": router_name,
                    "managementGroup": group,
                    "ip": address,
                    "interface": interface,
                }
            )
    for endpoints in by_network.values():
        endpoints.sort(key=lambda item: (item["pod"], item["ip"]))
    return by_network


def selectPair(
    by_network: dict[str, list[dict[str, str]]],
    pair_scope: str = "cross-host",
) -> tuple[dict[str, str], dict[str, str]]:
    """Select the first deterministic shared-network pair for one scope."""
    if pair_scope not in {"cross-host", "cross-node"}:
        raise ValueError(f"unsupported pair scope: {pair_scope}")
    for network in sorted(by_network):
        endpoints = by_network[network]
        if pair_scope == "cross-host":
            groups: dict[str, list[dict[str, str]]] = {}
            for endpoint in endpoints:
                groups.setdefault(endpoint["managementGroup"], []).append(endpoint)
            for left_group, right_group in itertools.combinations(sorted(groups), 2):
                return groups[left_group][0], groups[right_group][0]
        else:
            for left, right in itertools.combinations(endpoints, 2):
                if left["node"] != right["node"]:
                    return left, right
    description = "cross-hypervisor" if pair_scope == "cross-host" else "cross-node"
    raise RuntimeError(
        f"no ready {description} router pair sharing a Multus network was found"
    )


def validateNadMtus(
    kubeconfig: Path,
    namespace: str,
    expected_mtu: int,
    request_timeout: int,
) -> dict[str, Any]:
    """Require every namespace NAD to carry the configured MTU."""
    response = kubectl(
        kubeconfig,
        [
            "-n",
            namespace,
            "get",
            "network-attachment-definitions.k8s.cni.cncf.io",
            "-o",
            "json",
        ],
        request_timeout=request_timeout,
    )
    payload = json.loads(response.stdout)
    items = payload.get("items") if isinstance(payload, dict) else []
    records = items if isinstance(items, list) else []
    mismatches = []
    for item in records:
        if not isinstance(item, dict):
            continue
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        spec = item.get("spec") if isinstance(item.get("spec"), dict) else {}
        name = str(metadata.get("name") or "")
        raw_config = spec.get("config")
        try:
            config = json.loads(raw_config) if isinstance(raw_config, str) else {}
        except json.JSONDecodeError:
            mismatches.append({"name": name, "error": "invalid JSON config"})
            continue
        actual_mtu = int(config.get("mtu") or 0) if isinstance(config, dict) else 0
        if actual_mtu != expected_mtu:
            mismatches.append({"name": name, "mtu": actual_mtu or None})
    if not records:
        raise RuntimeError(f"no NetworkAttachmentDefinitions found in {namespace}")
    if mismatches:
        raise RuntimeError(
            f"{len(mismatches)}/{len(records)} NetworkAttachmentDefinitions do not "
            f"use MTU {expected_mtu}; first mismatches: {mismatches[:5]}"
        )
    return {
        "networkAttachmentDefinitions": len(records),
        "expectedMtu": expected_mtu,
        "mismatches": 0,
    }


def selectNodeMtuSamples(
    by_network: dict[str, list[dict[str, str]]],
) -> dict[str, dict[str, str]]:
    """Select one deterministic router secondary interface per K3s node."""
    candidates: dict[str, list[dict[str, str]]] = {}
    seen: set[tuple[str, str, str]] = set()
    for endpoints in by_network.values():
        for endpoint in endpoints:
            key = (endpoint["node"], endpoint["pod"], endpoint["ip"])
            if key in seen:
                continue
            seen.add(key)
            candidates.setdefault(endpoint["node"], []).append(endpoint)
    return {
        node: sorted(
            endpoints,
            key=lambda item: (item["network"], item["pod"], item["ip"]),
        )[0]
        for node, endpoints in sorted(candidates.items())
    }


def requireIperf(
    kubeconfig: Path,
    namespace: str,
    pair: tuple[dict[str, str], dict[str, str]],
    request_timeout: int,
) -> None:
    """Fail quickly unless both deterministic endpoints contain iperf3."""
    missing = []
    for endpoint in pair:
        check = execPod(
            kubeconfig,
            namespace,
            endpoint["pod"],
            ["sh", "-lc", "command -v iperf3 >/dev/null 2>&1"],
            request_timeout=request_timeout,
            check=False,
        )
        if check.returncode != 0:
            missing.append(endpoint["pod"])
    if missing:
        raise RuntimeError(
            "iperf3 is missing from deterministic endpoint(s): "
            + ", ".join(missing)
        )


def readInterfaceMtu(
    kubeconfig: Path,
    namespace: str,
    endpoint: dict[str, str],
    expected_mtu: int,
    request_timeout: int,
) -> dict[str, Any]:
    """Resolve the selected IP's runtime interface and require its MTU."""
    multus_interface = endpoint.get("interface", "")
    response = execPod(
        kubeconfig,
        namespace,
        endpoint["pod"],
        ["ip", "-j", "address"],
        request_timeout=request_timeout,
    )
    payload = json.loads(response.stdout)
    matches = []
    if isinstance(payload, list):
        for link in payload:
            if not isinstance(link, dict):
                continue
            addresses = (
                link.get("addr_info")
                if isinstance(link.get("addr_info"), list)
                else []
            )
            if any(
                isinstance(address, dict)
                and str(address.get("local") or "") == endpoint["ip"]
                for address in addresses
            ):
                matches.append(link)
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one runtime interface with IP {endpoint['ip']} in "
            f"{endpoint['pod']}, found {len(matches)}"
        )
    interface = str(matches[0].get("ifname") or "")
    mtu = int(matches[0].get("mtu") or 0)
    if not interface or mtu <= 0:
        raise RuntimeError(
            f"runtime interface metadata is incomplete for IP {endpoint['ip']} "
            f"in {endpoint['pod']}"
        )
    if expected_mtu and mtu != expected_mtu:
        raise RuntimeError(
            f"{endpoint['pod']} interface {interface} MTU is {mtu}, expected {expected_mtu}"
        )
    return {
        "interface": interface,
        "multusReportedInterface": multus_interface or None,
        "mtu": mtu,
        "expectedMtu": expected_mtu or None,
    }


def parsePing(output: str) -> dict[str, Any]:
    """Extract packet loss and RTT values from iputils ping output."""
    loss_match = re.search(r"([\d.]+)% packet loss", output)
    rtt_match = re.search(
        r"=\s*([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)\s*ms",
        output,
    )
    result: dict[str, Any] = {"raw": output.strip()}
    if loss_match:
        result["packetLossPercent"] = float(loss_match.group(1))
    if rtt_match:
        result["rttMs"] = {
            "min": float(rtt_match.group(1)),
            "avg": float(rtt_match.group(2)),
            "max": float(rtt_match.group(3)),
            "mdev": float(rtt_match.group(4)),
        }
    return result


def pingDirection(
    kubeconfig: Path,
    namespace: str,
    source: dict[str, str],
    target: dict[str, str],
    count: int,
    timeout: int,
    request_timeout: int,
) -> dict[str, Any]:
    """Run one direct secondary-address ping direction."""
    response = execPod(
        kubeconfig,
        namespace,
        source["pod"],
        [
            "ping",
            "-I",
            source["ip"],
            "-c",
            str(count),
            "-W",
            str(timeout),
            target["ip"],
        ],
        request_timeout=request_timeout,
    )
    return parsePing(response.stdout)


def mtuPingDirection(
    kubeconfig: Path,
    namespace: str,
    source: dict[str, str],
    target: dict[str, str],
    count: int,
    timeout: int,
    mtu: int,
    request_timeout: int,
) -> dict[str, Any]:
    """Run an IPv4 DF ping whose complete IP packet equals the expected MTU."""
    payload_bytes = mtu - 28
    if payload_bytes < 0:
        raise RuntimeError(f"MTU {mtu} is too small for an IPv4 ICMP probe")
    response = execPod(
        kubeconfig,
        namespace,
        source["pod"],
        [
            "ping",
            "-I",
            source["ip"],
            "-M",
            "do",
            "-s",
            str(payload_bytes),
            "-c",
            str(count),
            "-W",
            str(timeout),
            target["ip"],
        ],
        request_timeout=request_timeout,
    )
    result = parsePing(response.stdout)
    result["ipPacketBytes"] = mtu
    result["payloadBytes"] = payload_bytes
    return result


def warmupDirection(
    kubeconfig: Path,
    namespace: str,
    source: dict[str, str],
    target: dict[str, str],
    timeout: int,
    request_timeout: int,
) -> dict[str, Any]:
    """Warm ARP/FDB state without mixing cold-start loss into RTT samples."""
    started = time.monotonic()
    deadline = started + timeout
    attempts = 0
    last_output = ""
    while time.monotonic() < deadline:
        attempts += 1
        response = execPod(
            kubeconfig,
            namespace,
            source["pod"],
            [
                "ping",
                "-I",
                source["ip"],
                "-c",
                "1",
                "-W",
                "2",
                target["ip"],
            ],
            request_timeout=request_timeout,
            check=False,
        )
        last_output = response.stdout
        if response.returncode == 0:
            return {
                "success": True,
                "attempts": attempts,
                "durationSeconds": round(time.monotonic() - started, 3),
            }
        time.sleep(1)
    raise RuntimeError(
        f"dataplane warmup timed out after {timeout}s for "
        f"{source['pod']} -> {target['pod']}: {last_output.strip()}"
    )


def iperfDirection(
    kubeconfig: Path,
    namespace: str,
    source: dict[str, str],
    target: dict[str, str],
    duration: int,
    port: int,
    request_timeout: int,
) -> dict[str, Any]:
    """Run one TCP iperf3 direction and return compact throughput metrics."""
    execPod(
        kubeconfig,
        namespace,
        target["pod"],
        ["sh", "-lc", f"pkill -x iperf3 >/dev/null 2>&1 || true; iperf3 -s -1 -D -p {port}"],
        request_timeout=request_timeout,
    )
    time.sleep(1)
    response = execPod(
        kubeconfig,
        namespace,
        source["pod"],
        [
            "iperf3",
            "-c",
            target["ip"],
            "-B",
            source["ip"],
            "-J",
            "-t",
            str(duration),
            "-P",
            "1",
            "-p",
            str(port),
        ],
        request_timeout=max(request_timeout, duration + 30),
    )
    payload = json.loads(response.stdout)
    end = payload.get("end") if isinstance(payload.get("end"), dict) else {}
    received = end.get("sum_received") if isinstance(end.get("sum_received"), dict) else {}
    sent = end.get("sum_sent") if isinstance(end.get("sum_sent"), dict) else {}
    bits_per_second = float(received.get("bits_per_second") or sent.get("bits_per_second") or 0)
    if bits_per_second <= 0:
        raise RuntimeError(
            f"iperf3 returned no positive throughput for {source['pod']} -> {target['pod']}"
        )
    return {
        "bitsPerSecond": bits_per_second,
        "megabitsPerSecond": bits_per_second / 1_000_000,
        "retransmits": int(sent.get("retransmits") or 0),
        "seconds": float(received.get("seconds") or sent.get("seconds") or duration),
    }


def stopIperf(
    kubeconfig: Path,
    namespace: str,
    pods: set[str],
    request_timeout: int,
) -> None:
    """Best-effort cleanup of probe iperf3 servers."""
    for pod in sorted(pods):
        execPod(
            kubeconfig,
            namespace,
            pod,
            ["sh", "-lc", "pkill -x iperf3 >/dev/null 2>&1 || true"],
            request_timeout=request_timeout,
            check=False,
        )


def parseArgs() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True, type=Path)
    parser.add_argument("--config-k3s", required=True, type=Path)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ping-count", type=int, default=5)
    parser.add_argument("--ping-timeout", type=int, default=2)
    parser.add_argument("--warmup-timeout", type=int, default=30)
    parser.add_argument("--iperf-duration", type=int, default=5)
    parser.add_argument("--iperf-port", type=int, default=5201)
    parser.add_argument("--expected-mtu", type=int, default=0)
    parser.add_argument("--request-timeout", type=int, default=120)
    parser.add_argument(
        "--pair-scope",
        choices=("cross-host", "cross-node"),
        default="cross-host",
        help="require endpoints on different management /24s or only different K3s nodes",
    )
    return parser.parse_args()


def main() -> int:
    """Run the deterministic cross-node workload dataplane check."""
    args = parseArgs()
    if min(
        args.ping_count,
        args.ping_timeout,
        args.warmup_timeout,
        args.iperf_duration,
        args.iperf_port,
        args.request_timeout,
    ) < 1:
        raise SystemExit("all numeric probe arguments must be positive")
    if args.expected_mtu < 0:
        raise SystemExit("--expected-mtu must be zero or positive")

    started = time.time()
    result: dict[str, Any] = {
        "success": False,
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "namespace": args.namespace,
        "selection": {
            "method": (
                "lexicographically-first-shared-network-cross-management-/24"
                if args.pair_scope == "cross-host"
                else "lexicographically-first-shared-network-cross-k3s-node"
            ),
            "pairScope": args.pair_scope,
            "iperf3IsSelectionFilter": False,
            "requiresIperf3AfterSelection": True,
            "random": False,
        },
    }
    selected_pods: set[str] = set()
    try:
        groups = nodeManagementGroups(loadYaml(args.config_k3s))
        endpoints = readyRouterEndpoints(
            args.kubeconfig,
            args.namespace,
            groups,
            args.request_timeout,
        )
        if args.expected_mtu:
            result["nadMtuValidation"] = validateNadMtus(
                args.kubeconfig,
                args.namespace,
                args.expected_mtu,
                args.request_timeout,
            )
            node_samples = selectNodeMtuSamples(endpoints)
            missing_nodes = sorted(set(groups) - set(node_samples))
            if missing_nodes:
                raise RuntimeError(
                    "no ready router secondary interface found on K3s node(s): "
                    + ", ".join(missing_nodes)
                )
            result["nodeMtuSamples"] = {
                node: {
                    "pod": endpoint["pod"],
                    "network": endpoint["network"],
                    "ip": endpoint["ip"],
                    **readInterfaceMtu(
                        args.kubeconfig,
                        args.namespace,
                        endpoint,
                        args.expected_mtu,
                        args.request_timeout,
                    ),
                }
                for node, endpoint in node_samples.items()
            }
        left, right = selectPair(endpoints, args.pair_scope)
        selected_pods = {left["pod"], right["pod"]}
        result["pair"] = {"left": left, "right": right}
        requireIperf(
            args.kubeconfig,
            args.namespace,
            (left, right),
            args.request_timeout,
        )
        result["interfaces"] = {
            "left": readInterfaceMtu(
                args.kubeconfig,
                args.namespace,
                left,
                args.expected_mtu,
                args.request_timeout,
            ),
            "right": readInterfaceMtu(
                args.kubeconfig,
                args.namespace,
                right,
                args.expected_mtu,
                args.request_timeout,
            ),
        }
        result["warmup"] = {
            "leftToRight": warmupDirection(
                args.kubeconfig,
                args.namespace,
                left,
                right,
                args.warmup_timeout,
                args.request_timeout,
            ),
            "rightToLeft": warmupDirection(
                args.kubeconfig,
                args.namespace,
                right,
                left,
                args.warmup_timeout,
                args.request_timeout,
            ),
        }
        result["ping"] = {
            "leftToRight": pingDirection(
                args.kubeconfig,
                args.namespace,
                left,
                right,
                args.ping_count,
                args.ping_timeout,
                args.request_timeout,
            ),
            "rightToLeft": pingDirection(
                args.kubeconfig,
                args.namespace,
                right,
                left,
                args.ping_count,
                args.ping_timeout,
                args.request_timeout,
            ),
        }
        if args.expected_mtu:
            result["mtuDfPing"] = {
                "leftToRight": mtuPingDirection(
                    args.kubeconfig,
                    args.namespace,
                    left,
                    right,
                    args.ping_count,
                    args.ping_timeout,
                    args.expected_mtu,
                    args.request_timeout,
                ),
                "rightToLeft": mtuPingDirection(
                    args.kubeconfig,
                    args.namespace,
                    right,
                    left,
                    args.ping_count,
                    args.ping_timeout,
                    args.expected_mtu,
                    args.request_timeout,
                ),
            }
        result["iperfTcp"] = {
            "leftToRight": iperfDirection(
                args.kubeconfig,
                args.namespace,
                left,
                right,
                args.iperf_duration,
                args.iperf_port,
                args.request_timeout,
            ),
            "rightToLeft": iperfDirection(
                args.kubeconfig,
                args.namespace,
                right,
                left,
                args.iperf_duration,
                args.iperf_port + 1,
                args.request_timeout,
            ),
        }
        result["success"] = True
        print(
            f"[cross-host-workload] PASS network={left['network']} "
            f"{left['pod']}@{left['node']} <-> {right['pod']}@{right['node']}"
        )
        print(
            "[cross-host-workload] TCP Mbps "
            f"{result['iperfTcp']['leftToRight']['megabitsPerSecond']:.2f}/"
            f"{result['iperfTcp']['rightToLeft']['megabitsPerSecond']:.2f}"
        )
        return 0
    except (
        json.JSONDecodeError,
        RuntimeError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as error:
        result["error"] = str(error)
        if isinstance(error, subprocess.CalledProcessError):
            result["stdout"] = error.stdout
            result["stderr"] = error.stderr
        print(f"[cross-host-workload] FAIL: {error}")
        return 1
    finally:
        if selected_pods:
            stopIperf(
                args.kubeconfig,
                args.namespace,
                selected_pods,
                args.request_timeout,
            )
        result["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        result["durationSeconds"] = round(time.time() - started, 2)
        writeJson(args.output, result)


if __name__ == "__main__":
    raise SystemExit(main())
