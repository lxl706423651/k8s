#!/usr/bin/env python3
"""Collect VLAN-backed CNI observability and isolation evidence.

The script selects one shared secondary network with two running pods, then
collects Kubernetes, pod, and host-side evidence for macvlan+VLAN or
bridge+VLAN. In bridge mode, ``--inject-bridge-down`` briefly disables the
selected node-local Linux bridge, verifies traffic fails, and restores it.
"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


DEFAULT_KUBECONFIG = Path(__file__).resolve().parent / "kubeconfig.yaml"
DEFAULT_INVENTORY = Path(__file__).resolve().parent / "cluster.inventory.yaml"
NETWORK_STATUS_ANNOTATION = "k8s.v1.cni.cncf.io/network-status"
VLAN_MASTER_ANNOTATION = "org.seedsecuritylabs.seedemu.meta.vlan-master-interface"
VLAN_BRIDGE_ANNOTATION = "org.seedsecuritylabs.seedemu.meta.vlan-bridge-interface"
VLAN_ID_ANNOTATION = "org.seedsecuritylabs.seedemu.meta.vlan-id"


@dataclass
class InventoryNode:
    name: str
    host: str
    user: str
    key: str


@dataclass
class Attachment:
    pod: str
    node: str
    network: str
    statusInterface: str
    ips: list[str]
    mac: str
    labels: dict[str, str]


def runCommand(cmd: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    """Run a local command and capture stdout/stderr."""
    try:
        return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else f"timeout after {timeout}s"
        return subprocess.CompletedProcess(cmd, 124, stdout, stderr)


def kubectl(kubeconfig: Path, namespace: str, args: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    """Run kubectl against one namespace."""
    return runCommand(
        ["kubectl", "--kubeconfig", str(kubeconfig), "-n", namespace, *args],
        timeout=timeout,
    )


def kubectlJson(kubeconfig: Path, namespace: str, args: list[str], timeout: int = 120) -> dict[str, Any]:
    """Run kubectl and parse JSON output."""
    result = kubectl(kubeconfig, namespace, [*args, "-o", "json"], timeout=timeout)
    result.check_returncode()
    return json.loads(result.stdout)


def loadInventory(path: Path) -> dict[str, InventoryNode]:
    """Load node SSH connection metadata."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    nodes: dict[str, InventoryNode] = {}
    for item in data.get("nodes", []):
        name = str(item.get("name", "")).strip()
        host = str(item.get("managementIp") or item.get("management_ip") or item.get("ip") or "").strip()
        ssh = item.get("ssh", {}) or {}
        if not name or not host:
            continue
        nodes[name] = InventoryNode(
            name=name,
            host=host,
            user=str(ssh.get("user") or item.get("sshUser") or "ubuntu"),
            key=str(Path(str(ssh.get("key") or item.get("sshKey") or "~/.ssh/id_ed25519")).expanduser()),
        )
    if not nodes:
        raise SystemExit(f"No nodes found in {path}")
    return nodes


def sshCommand(node: InventoryNode, remote: str, timeout: int | None = 30) -> subprocess.CompletedProcess[str]:
    """Run one remote shell command on a VM."""
    return runCommand(
        [
            "ssh",
            "-i",
            node.key,
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=8",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "LogLevel=ERROR",
            f"{node.user}@{node.host}",
            remote,
        ],
        timeout=timeout,
    )


def parseAttachments(pod_data: dict[str, Any], namespace: str) -> list[Attachment]:
    """Extract running secondary-network attachments from pod JSON."""
    attachments: list[Attachment] = []
    for item in pod_data.get("items", []):
        metadata = item.get("metadata", {}) or {}
        status = item.get("status", {}) or {}
        spec = item.get("spec", {}) or {}
        if status.get("phase") != "Running":
            continue
        annotation_text = (metadata.get("annotations", {}) or {}).get(NETWORK_STATUS_ANNOTATION)
        if not annotation_text:
            continue
        try:
            network_status = json.loads(annotation_text)
        except json.JSONDecodeError:
            continue
        for entry in network_status:
            if entry.get("default"):
                continue
            raw_name = str(entry.get("name", ""))
            if "/" not in raw_name:
                continue
            ns, name = raw_name.split("/", 1)
            if ns != namespace:
                continue
            ips = [str(ip) for ip in entry.get("ips", []) if str(ip)]
            if not ips:
                continue
            attachments.append(
                Attachment(
                    pod=str(metadata.get("name", "")),
                    node=str(spec.get("nodeName", "")),
                    network=name,
                    statusInterface=str(entry.get("interface", "")),
                    ips=ips,
                    mac=str(entry.get("mac", "")),
                    labels={str(k): str(v) for k, v in (metadata.get("labels", {}) or {}).items()},
                )
            )
    return attachments


def chooseSamples(attachments: list[Attachment]) -> tuple[Attachment, Attachment, Attachment]:
    """Choose sender, same-network listener, and different-network listener."""
    by_network: dict[str, list[Attachment]] = defaultdict(list)
    for item in attachments:
        by_network[item.network].append(item)

    for network in sorted(by_network):
        candidates = sorted(by_network[network], key=lambda x: (x.node, x.pod, x.ips[0]))
        for sender in candidates:
            same = next((item for item in candidates if item.pod != sender.pod and item.node != sender.node), None)
            if same is None:
                same = next((item for item in candidates if item.pod != sender.pod), None)
            if same is None:
                continue
            different = next(
                (
                    item
                    for item in sorted(attachments, key=lambda x: (x.network, x.node, x.pod))
                    if item.network != network and item.pod not in {sender.pod, same.pod}
                ),
                None,
            )
            if different is not None:
                return sender, same, different
    raise RuntimeError("could not find a usable same-network and different-network attachment sample")


def resolvePodInterface(kubeconfig: Path, namespace: str, attachment: Attachment, timeout: int) -> dict[str, Any]:
    """Resolve the actual interface name inside the pod by matching IP."""
    result = kubectl(kubeconfig, namespace, ["exec", attachment.pod, "--", "ip", "-j", "addr", "show"], timeout=timeout)
    info: dict[str, Any] = {
        "pod": attachment.pod,
        "ip": attachment.ips[0],
        "statusInterface": attachment.statusInterface,
        "returncode": result.returncode,
        "stderr": result.stderr.strip(),
        "actualInterface": None,
    }
    if result.returncode != 0:
        return info
    try:
        rows = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        info["stderr"] = str(exc)
        return info
    for row in rows:
        for addr in row.get("addr_info", []):
            if addr.get("family") == "inet" and addr.get("local") == attachment.ips[0]:
                info.update(
                    {
                        "actualInterface": row.get("ifname"),
                        "mac": row.get("address"),
                        "prefixlen": addr.get("prefixlen"),
                    }
                )
                return info
    return info


def getNad(kubeconfig: Path, namespace: str, network: str) -> dict[str, Any]:
    """Read one NetworkAttachmentDefinition and decoded config."""
    data = kubectlJson(kubeconfig, namespace, ["get", "network-attachment-definition", network])
    config_text = data.get("spec", {}).get("config", "{}")
    decoded = json.loads(config_text)
    annotations = data.get("metadata", {}).get("annotations", {}) or {}
    return {
        "name": network,
        "config": decoded,
        "annotations": annotations,
        "type": decoded.get("type"),
        "bridge": decoded.get("bridge") or annotations.get(VLAN_BRIDGE_ANNOTATION),
        "master": decoded.get("master") or annotations.get(VLAN_MASTER_ANNOTATION),
        "vlanId": annotations.get(VLAN_ID_ANNOTATION),
    }


def podPing(kubeconfig: Path, namespace: str, sender: Attachment, target_ip: str, timeout: int) -> dict[str, Any]:
    """Ping a target IP from one selected pod."""
    result = kubectl(
        kubeconfig,
        namespace,
        ["exec", sender.pod, "--", "sh", "-lc", f"ping -c 2 -W 1 {shlex.quote(target_ip)}"],
        timeout=timeout,
    )
    return {
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def podTcpdumpDuringPing(
    kubeconfig: Path,
    namespace: str,
    listener: Attachment,
    listener_iface: str,
    sender: Attachment,
    target_ip: str,
    timeout: int,
) -> dict[str, Any]:
    """Run tcpdump inside one pod while another pod pings."""
    filter_expr = f"arp and (host {sender.ips[0]} or host {target_ip})"
    tcpdump_cmd = [
        "kubectl",
        "--kubeconfig",
        str(kubeconfig),
        "-n",
        namespace,
        "exec",
        listener.pod,
        "--",
        "timeout",
        "5",
        "tcpdump",
        "-n",
        "-i",
        listener_iface,
        filter_expr,
    ]
    process = subprocess.Popen(tcpdump_cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    time.sleep(1.0)
    ping_result = podPing(kubeconfig, namespace, sender, target_ip, timeout)
    try:
        stdout, _ = process.communicate(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, _ = process.communicate()
    lines = (stdout or "").splitlines()
    arp_lines = [line for line in lines if "ARP" in line]
    return {
        "listenerPod": listener.pod,
        "listenerIface": listener_iface,
        "filter": filter_expr,
        "tcpdumpReturncode": process.returncode,
        "tcpdumpLines": lines,
        "arpLineCount": len(arp_lines),
        "ping": ping_result,
    }


def hostEvidence(node: InventoryNode, nad: dict[str, Any], mode: str) -> dict[str, Any]:
    """Collect host-side link/FDB evidence for one node and network."""
    master = str(nad.get("master") or "")
    bridge = str(nad.get("bridge") or "")
    commands: dict[str, str] = {}
    if master:
        quoted_master = shlex.quote(master)
        commands["master_sysfs"] = (
            f"if [ -d /sys/class/net/{quoted_master} ]; then "
            f"printf 'exists=1\\n'; "
            f"printf 'ifindex='; cat /sys/class/net/{quoted_master}/ifindex; "
            f"printf 'operstate='; cat /sys/class/net/{quoted_master}/operstate; "
            f"printf 'address='; cat /sys/class/net/{quoted_master}/address; "
            f"else printf 'exists=0\\n'; fi"
        )
    if mode == "bridge" and bridge:
        quoted_bridge = shlex.quote(bridge)
        commands["bridge_sysfs"] = (
            f"if [ -d /sys/class/net/{quoted_bridge} ]; then "
            f"printf 'exists=1\\n'; "
            f"printf 'ifindex='; cat /sys/class/net/{quoted_bridge}/ifindex; "
            f"printf 'operstate='; cat /sys/class/net/{quoted_bridge}/operstate; "
            f"printf 'address='; cat /sys/class/net/{quoted_bridge}/address; "
            f"else printf 'exists=0\\n'; fi"
        )
        commands["bridge_ports_sysfs"] = (
            f"if [ -d /sys/class/net/{quoted_bridge}/brif ]; then "
            f"printf 'port_count='; find /sys/class/net/{quoted_bridge}/brif -mindepth 1 -maxdepth 1 -printf '.\\n' | wc -l; "
            f"ls -1 /sys/class/net/{quoted_bridge}/brif | head -n 80; "
            f"else printf 'port_count=0\\n'; fi"
        )
    else:
        commands["macvlan_host_observation"] = (
            "printf 'macvlan_has_no_node_local_bridge_port_list=1\\n'; "
            "printf 'root_namespace_macvlan_count='; "
            "find /sys/class/net -maxdepth 1 -type l -printf '%f\\n' "
            "| while read -r iface; do [ -d \"/sys/class/net/${iface}/macvlan\" ] && echo \"${iface}\"; done "
            "| wc -l"
        )

    evidence: dict[str, Any] = {"node": node.name, "host": node.host, "commands": {}}
    for name, remote in commands.items():
        result = sshCommand(node, remote, timeout=30)
        evidence["commands"][name] = {
            "command": remote,
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
    return evidence


def hostTcpdumpDuringPing(
    node: InventoryNode,
    iface: str,
    sender: Attachment,
    target_ip: str,
    kubeconfig: Path,
    namespace: str,
    timeout: int,
) -> dict[str, Any]:
    """Run host tcpdump on a bridge/VLAN parent while a pod pings."""
    filter_expr = f"arp and (host {sender.ips[0]} or host {target_ip})"
    remote = f"sudo -n timeout 5 tcpdump -n -i {shlex.quote(iface)} {shlex.quote(filter_expr)}"
    ssh_cmd = [
        "ssh",
        "-i",
        node.key,
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=8",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        "LogLevel=ERROR",
        f"{node.user}@{node.host}",
        remote,
    ]
    process = subprocess.Popen(ssh_cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    time.sleep(1.0)
    ping_result = podPing(kubeconfig, namespace, sender, target_ip, timeout)
    try:
        stdout, _ = process.communicate(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, _ = process.communicate()
    lines = (stdout or "").splitlines()
    return {
        "node": node.name,
        "iface": iface,
        "filter": filter_expr,
        "tcpdumpReturncode": process.returncode,
        "tcpdumpLines": lines,
        "arpLineCount": sum(1 for line in lines if "ARP" in line),
        "ping": ping_result,
    }


def bridgeDownExperiment(
    node: InventoryNode,
    bridge: str,
    sender: Attachment,
    target_ip: str,
    kubeconfig: Path,
    namespace: str,
    timeout: int,
) -> dict[str, Any]:
    """Briefly disable one Linux bridge and verify ping fails, then restore it."""
    result: dict[str, Any] = {
        "node": node.name,
        "bridge": bridge,
        "before": podPing(kubeconfig, namespace, sender, target_ip, timeout),
    }
    down = sshCommand(node, f"sudo -n timeout 5 ip link set {shlex.quote(bridge)} down", timeout=20)
    time.sleep(1.0)
    result["downCommand"] = {"returncode": down.returncode, "stdout": down.stdout.strip(), "stderr": down.stderr.strip()}
    result["duringDown"] = podPing(kubeconfig, namespace, sender, target_ip, timeout)
    up = sshCommand(node, f"sudo -n timeout 5 ip link set {shlex.quote(bridge)} up", timeout=20)
    time.sleep(2.0)
    result["upCommand"] = {"returncode": up.returncode, "stdout": up.stdout.strip(), "stderr": up.stderr.strip()}
    result["afterRestore"] = podPing(kubeconfig, namespace, sender, target_ip, timeout)
    return result


def parseArgs() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--namespace", default=None)
    parser.add_argument("--kubeconfig", type=Path, default=DEFAULT_KUBECONFIG)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--sample-network", default=None)
    parser.add_argument("--inject-bridge-down", action="store_true")
    parser.add_argument("--exec-timeout", type=int, default=60)
    return parser.parse_args()


def main() -> int:
    """CLI entrypoint."""
    args = parseArgs()
    run_dir = args.run_dir.resolve()
    assignment_path = run_dir / "assignment.yaml"
    if not assignment_path.exists():
        raise SystemExit(f"missing run assignment: {assignment_path}")
    assignment = yaml.safe_load(assignment_path.read_text(encoding="utf-8")) or {}
    namespace = args.namespace or assignment.get("experiment", {}).get("namespace")
    if not namespace:
        raise SystemExit("namespace must be supplied or present in run assignment")

    out_dir = (args.output_dir or run_dir / "cni_observability").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    nodes = loadInventory(args.inventory)
    pod_data = kubectlJson(args.kubeconfig, namespace, ["get", "pods"], timeout=300)
    attachments = parseAttachments(pod_data, namespace)
    if args.sample_network:
        selected = [item for item in attachments if item.network == args.sample_network]
        if len(selected) < 2:
            raise SystemExit(f"network {args.sample_network} has fewer than two running attachments")
        sender = sorted(selected, key=lambda x: (x.node, x.pod))[0]
        same = next(
            (item for item in sorted(selected, key=lambda x: (x.node, x.pod)) if item.pod != sender.pod and item.node != sender.node),
            None,
        )
        if same is None:
            same = next((item for item in sorted(selected, key=lambda x: (x.node, x.pod)) if item.pod != sender.pod), None)
        different = next(item for item in sorted(attachments, key=lambda x: (x.network, x.node, x.pod)) if item.network != args.sample_network)
        assert same is not None
    else:
        sender, same, different = chooseSamples(attachments)

    same_nad = getNad(args.kubeconfig, namespace, sender.network)
    different_nad = getNad(args.kubeconfig, namespace, different.network)
    mode = str(same_nad.get("type") or "").lower()
    sender_if = resolvePodInterface(args.kubeconfig, namespace, sender, args.exec_timeout)
    same_if = resolvePodInterface(args.kubeconfig, namespace, same, args.exec_timeout)
    different_if = resolvePodInterface(args.kubeconfig, namespace, different, args.exec_timeout)
    if not sender_if.get("actualInterface") or not same_if.get("actualInterface") or not different_if.get("actualInterface"):
        raise SystemExit("failed to resolve selected pod interface names")

    sender_node = nodes[sender.node]
    same_node = nodes[same.node]
    different_node = nodes[different.node]
    host_iface = str(same_nad.get("bridge") if mode == "bridge" else same_nad.get("master") or "")

    summary: dict[str, Any] = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "namespace": namespace,
        "runDir": str(run_dir),
        "mode": mode,
        "selected": {
            "sender": asdict(sender),
            "sameNetworkListener": asdict(same),
            "differentNetworkListener": asdict(different),
        },
        "podInterfaces": {
            "sender": sender_if,
            "sameNetworkListener": same_if,
            "differentNetworkListener": different_if,
        },
        "nad": {
            "sameNetwork": same_nad,
            "differentNetwork": different_nad,
        },
        "sameNetworkPing": podPing(args.kubeconfig, namespace, sender, same.ips[0], args.exec_timeout),
        "differentNetworkPing": podPing(args.kubeconfig, namespace, sender, different.ips[0], args.exec_timeout),
        "podTcpdumpSameNetwork": podTcpdumpDuringPing(
            args.kubeconfig,
            namespace,
            same,
            str(same_if["actualInterface"]),
            sender,
            same.ips[0],
            args.exec_timeout,
        ),
        "podTcpdumpDifferentNetwork": podTcpdumpDuringPing(
            args.kubeconfig,
            namespace,
            different,
            str(different_if["actualInterface"]),
            sender,
            same.ips[0],
            args.exec_timeout,
        ),
        "hostEvidence": {
            "senderNode": hostEvidence(sender_node, same_nad, mode),
            "sameListenerNode": hostEvidence(same_node, same_nad, mode),
            "differentListenerNode": hostEvidence(different_node, different_nad, mode),
        },
    }

    if host_iface:
        summary["hostTcpdumpSameNetwork"] = hostTcpdumpDuringPing(
            same_node,
            host_iface,
            sender,
            same.ips[0],
            args.kubeconfig,
            namespace,
            args.exec_timeout,
        )
    if args.inject_bridge_down and mode == "bridge" and same_nad.get("bridge"):
        summary["bridgeDownExperiment"] = bridgeDownExperiment(
            sender_node,
            str(same_nad["bridge"]),
            sender,
            same.ips[0],
            args.kubeconfig,
            namespace,
            args.exec_timeout,
        )

    host_commands = summary["hostEvidence"]["senderNode"]["commands"]
    bridge_ports_stdout = host_commands.get("bridge_ports_sysfs", {}).get("stdout", "")
    bridge_ports_visible = "port_count=" in bridge_ports_stdout and "port_count=0" not in bridge_ports_stdout
    master_stdout = host_commands.get("master_sysfs", {}).get("stdout", "")
    master_visible = "exists=1" in master_stdout
    summary["checks"] = {
        "sameNetworkPingPassed": summary["sameNetworkPing"]["returncode"] == 0,
        "sameNetworkPodSawArp": summary["podTcpdumpSameNetwork"]["arpLineCount"] > 0,
        "differentNetworkPodSawNoArp": summary["podTcpdumpDifferentNetwork"]["arpLineCount"] == 0,
        "hostMasterVisible": master_visible,
    }
    if mode == "bridge":
        summary["checks"]["hostBridgePortListVisible"] = bridge_ports_visible
    if "bridgeDownExperiment" in summary:
        exp = summary["bridgeDownExperiment"]
        summary["checks"]["bridgeDownCausedFailure"] = exp["duringDown"]["returncode"] != 0
        summary["checks"]["bridgeRestoreRecovered"] = exp["afterRestore"]["returncode"] == 0

    output_path = out_dir / "cni_observability_summary.json"
    output_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(summary["checks"], indent=2, sort_keys=True))
    print(f"summary={output_path}")
    return 0 if all(summary["checks"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
