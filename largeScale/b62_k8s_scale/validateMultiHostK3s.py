#!/usr/bin/env python3
"""Validate bidirectional primary Pod networking across K3s hypervisors.

The probe schedules one short-lived Ubuntu Pod on a local-subnet node and one
on a different management subnet, then requires Pod-IP ping to pass in both
directions. The temporary namespace is removed on every exit path.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml


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
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run kubectl with a stable kubeconfig and request timeout."""
    return subprocess.run(
        [
            "kubectl",
            "--kubeconfig",
            str(kubeconfig),
            "--request-timeout=120s",
            *args,
        ],
        input=input_text,
        text=True,
        capture_output=True,
        check=check,
    )


def selectCrossSubnetNodes(config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Select a deterministic master/worker pair on different management /24s."""
    raw_nodes = config.get("nodes")
    nodes = [node for node in raw_nodes if isinstance(node, dict)] if isinstance(raw_nodes, list) else []
    left = next(
        (
            node
            for node in nodes
            if str(node.get("role") or "").lower() in {"master", "control-plane"}
        ),
        nodes[0] if nodes else None,
    )
    if left is None:
        raise SystemExit("configK3s.yaml has no nodes")
    left_ip = ipaddress.ip_address(str(left.get("ip") or ""))
    left_network = ipaddress.ip_network(f"{left_ip}/24", strict=False)
    right = next(
        (
            node
            for node in nodes
            if str(node.get("role") or "").lower() not in {"master", "control-plane"}
            and ipaddress.ip_address(str(node.get("ip") or "")) not in left_network
        ),
        None,
    )
    if right is None:
        raise SystemExit("no worker on a different management /24 was found")
    return left, right


def probeManifest(
    namespace: str,
    left_node: str,
    right_node: str,
    probe_image: str,
) -> str:
    """Render the namespace and two fixed-node probe Pods."""
    documents: list[dict[str, Any]] = [
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": namespace},
        }
    ]
    for pod_name, node_name in (("primary-left", left_node), ("primary-right", right_node)):
        documents.append(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {"name": pod_name, "namespace": namespace},
                "spec": {
                    "nodeName": node_name,
                    "restartPolicy": "Never",
                    "containers": [
                        {
                            "name": "probe",
                            "image": probe_image,
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["sleep", "3600"],
                        }
                    ],
                },
            }
        )
    return yaml.safe_dump_all(documents, sort_keys=False)


def podIp(kubeconfig: Path, namespace: str, pod: str) -> str:
    """Return one Pod IP."""
    result = kubectl(
        kubeconfig,
        ["-n", namespace, "get", "pod", pod, "-o", "jsonpath={.status.podIP}"],
    )
    value = result.stdout.strip()
    if not value:
        raise RuntimeError(f"Pod {namespace}/{pod} has no IP")
    return value


def pingFromPod(
    kubeconfig: Path,
    namespace: str,
    pod: str,
    peer_ip: str,
    count: int,
    timeout: int,
    payload_bytes: int | None = None,
) -> str:
    """Run ping in one probe Pod and return its output."""
    ping_args = [
        "ping",
        "-c",
        str(count),
        "-W",
        str(timeout),
    ]
    if payload_bytes is not None:
        ping_args.extend(["-M", "do", "-s", str(payload_bytes)])
    ping_args.append(peer_ip)
    result = kubectl(
        kubeconfig,
        [
            "-n",
            namespace,
            "exec",
            pod,
            "--",
            *ping_args,
        ],
    )
    return result.stdout.strip()


def parseArgs() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True, type=Path)
    parser.add_argument("--config-k3s", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--namespace", default="seedemu-k3s-cross-host-smoke")
    parser.add_argument(
        "--probe-image",
        help=(
            "Probe image containing iputils ping. Defaults to the SeedEMU router "
            "image in the cluster-local registry."
        ),
    )
    parser.add_argument("--ping-count", type=int, default=5)
    parser.add_argument("--ping-timeout", type=int, default=2)
    parser.add_argument(
        "--mtu-payload-bytes",
        type=int,
        default=1372,
        help="Required unfragmented ICMP payload (1372 bytes gives a 1400-byte IP packet).",
    )
    return parser.parse_args()


def main() -> int:
    """Run the cross-host primary CNI check."""
    args = parseArgs()
    started = time.time()
    result: dict[str, Any] = {
        "success": False,
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "namespace": args.namespace,
        "kubeconfig": str(args.kubeconfig.resolve()),
        "configK3s": str(args.config_k3s.resolve()),
    }
    if args.ping_count < 1 or args.ping_timeout < 1 or args.mtu_payload_bytes < 1:
        raise SystemExit("ping count, timeout, and MTU payload must be positive")

    config = loadYaml(args.config_k3s)
    left, right = selectCrossSubnetNodes(config)
    registry = config.get("registry") if isinstance(config.get("registry"), dict) else {}
    registry_host = str(registry.get("host") or left.get("ip"))
    registry_port = int(registry.get("port", 5000))
    probe_image = args.probe_image or (
        f"{registry_host}:{registry_port}/"
        "handsonsecurity/seedemu-multiarch-router:buildx-latest"
    )
    result["nodes"] = {
        "left": {"name": str(left.get("name")), "managementIp": str(left.get("ip"))},
        "right": {"name": str(right.get("name")), "managementIp": str(right.get("ip"))},
    }
    result["probeImage"] = probe_image
    result["requiredMtuPayloadBytes"] = args.mtu_payload_bytes

    try:
        kubectl(
            args.kubeconfig,
            ["delete", "namespace", args.namespace, "--ignore-not-found", "--wait=true", "--timeout=120s"],
            check=False,
        )
        kubectl(
            args.kubeconfig,
            ["apply", "-f", "-"],
            input_text=probeManifest(
                args.namespace,
                str(left.get("name")),
                str(right.get("name")),
                probe_image,
            ),
        )
        kubectl(
            args.kubeconfig,
            [
                "-n",
                args.namespace,
                "wait",
                "--for=condition=Ready",
                "pod/primary-left",
                "pod/primary-right",
                "--timeout=180s",
            ],
        )
        left_ip = podIp(args.kubeconfig, args.namespace, "primary-left")
        right_ip = podIp(args.kubeconfig, args.namespace, "primary-right")
        left_ping = pingFromPod(
            args.kubeconfig,
            args.namespace,
            "primary-left",
            right_ip,
            args.ping_count,
            args.ping_timeout,
        )
        right_ping = pingFromPod(
            args.kubeconfig,
            args.namespace,
            "primary-right",
            left_ip,
            args.ping_count,
            args.ping_timeout,
        )
        left_mtu_ping = pingFromPod(
            args.kubeconfig,
            args.namespace,
            "primary-left",
            right_ip,
            args.ping_count,
            args.ping_timeout,
            args.mtu_payload_bytes,
        )
        right_mtu_ping = pingFromPod(
            args.kubeconfig,
            args.namespace,
            "primary-right",
            left_ip,
            args.ping_count,
            args.ping_timeout,
            args.mtu_payload_bytes,
        )
        result["podIps"] = {"left": left_ip, "right": right_ip}
        result["defaultPing"] = {
            "leftToRight": left_ping,
            "rightToLeft": right_ping,
        }
        result["mtuPing"] = {
            "payloadBytes": args.mtu_payload_bytes,
            "leftToRight": left_mtu_ping,
            "rightToLeft": right_mtu_ping,
        }
        result["success"] = True
        print(
            f"[k3s-cross-host] PASS {left['name']}({left_ip}) "
            f"<-> {right['name']}({right_ip})"
        )
        return 0
    except (subprocess.CalledProcessError, RuntimeError) as error:
        result["error"] = str(error)
        if isinstance(error, subprocess.CalledProcessError):
            result["stdout"] = error.stdout
            result["stderr"] = error.stderr
        print(f"[k3s-cross-host] FAIL: {error}")
        return 1
    finally:
        cleanup = kubectl(
            args.kubeconfig,
            ["delete", "namespace", args.namespace, "--ignore-not-found", "--wait=false"],
            check=False,
        )
        result["cleanupExitCode"] = cleanup.returncode
        result["finishedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        result["durationSeconds"] = round(time.time() - started, 2)
        writeJson(args.output, result)


if __name__ == "__main__":
    raise SystemExit(main())
