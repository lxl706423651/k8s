#!/usr/bin/env python3
"""Load and validate the 0918E1 YAML experiment configuration.

Inputs: an experiment YAML path.
Outputs: normalized Python dictionaries for topology, traffic, VM, and result settings.
Side effects: none.
Context: imported by the 0918E1 provisioning, execution, and analysis entrypoints.
"""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any

import yaml


def loadConfig(path: Path) -> dict[str, Any]:
    """Load one YAML file and enforce the cross-stage experiment contract."""
    config_path = path.expanduser().resolve()
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"configuration must be a YAML mapping: {config_path}")

    topology = requireMapping(data, "topology")
    traffic = requireMapping(data, "traffic")
    vm = requireMapping(data, "vm")
    execution = requireMapping(data, "execution")
    requireMapping(data, "analysis")

    scales = topology.get("asCounts")
    if not isinstance(scales, list) or not scales:
        raise ValueError("topology.asCounts must be a non-empty list")
    normalized_scales = [int(value) for value in scales]
    if normalized_scales != sorted(set(normalized_scales)):
        raise ValueError("topology.asCounts must be sorted and contain no duplicates")
    if normalized_scales[0] < 1 or normalized_scales[-1] > 508:
        raise ValueError(
            "topology.asCounts values must be in 1..508; IX /23 reserves its first host for Docker and 10.100.1.254 for the RS"
        )
    topology["asCounts"] = normalized_scales

    nodes = vm.get("nodes")
    if not isinstance(nodes, list) or len(nodes) != len(normalized_scales):
        raise ValueError("vm.nodes must contain exactly one entry per topology.asCounts value")
    node_scales = [int(requireMapping(node, "", allowSelf=True).get("asCount", -1)) for node in nodes]
    if node_scales != normalized_scales:
        raise ValueError("vm.nodes asCount values must match topology.asCounts in the same order")
    names = [str(node.get("name", "")) for node in nodes]
    addresses = [str(node.get("address", "")) for node in nodes]
    if any(not name for name in names) or len(names) != len(set(names)):
        raise ValueError("vm.nodes names must be non-empty and unique")
    if any(not address for address in addresses) or len(addresses) != len(set(addresses)):
        raise ValueError("vm.nodes addresses must be non-empty and unique")

    network = requireMapping(vm, "network")
    subnet = ipaddress.ip_network(str(network["cidr"]), strict=True)
    gateway = ipaddress.ip_address(str(network["gateway"]))
    if gateway not in subnet:
        raise ValueError("vm.network.gateway must belong to vm.network.cidr")
    for address in addresses:
        if ipaddress.ip_address(address) not in subnet:
            raise ValueError(f"VM address {address} is outside {subnet}")

    externalNetwork = requireMapping(vm, "externalNetwork")
    externalSubnet = ipaddress.ip_network(str(externalNetwork["cidr"]), strict=True)
    externalGateway = ipaddress.ip_address(str(externalNetwork["gateway"]))
    if externalGateway not in externalSubnet:
        raise ValueError("vm.externalNetwork.gateway must belong to vm.externalNetwork.cidr")
    sourceIndexes = [int(node.get("sourceIndex", -1)) for node in nodes]
    if sourceIndexes != list(range(len(nodes))):
        raise ValueError("vm.nodes sourceIndex values must be contiguous and match node order")
    for node in nodes:
        if not str(node.get("xvmMac", "")):
            raise ValueError("vm.nodes.xvmMac is required for the shared external network")
    sink = requireMapping(vm, "sink")
    sinkAddress = ipaddress.ip_address(str(sink["address"]))
    if sinkAddress not in subnet:
        raise ValueError(f"VM sink address {sinkAddress} is outside {subnet}")
    sinkExternalAddress = ipaddress.ip_address(str(sink["xvmAddress"]))
    if sinkExternalAddress not in externalSubnet:
        raise ValueError(f"VM sink external address {sinkExternalAddress} is outside {externalSubnet}")

    for key in (
        "durationSeconds",
        "preflightDurationSeconds",
        "commandGraceSeconds",
        "repeats",
        "sampleIntervalSeconds",
    ):
        value = float(traffic[key])
        if value <= 0:
            raise ValueError(f"traffic.{key} must be positive")
    if int(traffic.get("preflightRetries", 0)) < 0:
        raise ValueError("traffic.preflightRetries must be non-negative")
    if str(traffic.get("mode", "")) not in {"local-pair", "vm-to-vm-sink"}:
        raise ValueError("traffic.mode must be local-pair or vm-to-vm-sink")
    if str(traffic["mode"]) == "vm-to-vm-sink":
        sinkTrafficAddress = ipaddress.ip_address(str(traffic["sinkAddress"]))
        if sinkTrafficAddress != sinkExternalAddress:
            raise ValueError("traffic.sinkAddress must match vm.sink.xvmAddress")
        if int(traffic["sinkPortBase"]) < 1024:
            raise ValueError("traffic.sinkPortBase must be >= 1024")

    if not str(execution.get("composePluginPath", "")):
        raise ValueError("execution.composePluginPath must be set")
    baseImages = execution.get("baseImages")
    if not isinstance(baseImages, list) or not baseImages:
        raise ValueError("execution.baseImages must be a non-empty list")
    for key in ("remoteRunTimeoutSeconds", "statusPollSeconds", "resultDownloadAttempts"):
        if float(execution.get(key, 0)) <= 0:
            raise ValueError(f"execution.{key} must be positive")

    data["_configPath"] = str(config_path)
    return data


def requireMapping(parent: dict[str, Any], key: str, allowSelf: bool = False) -> dict[str, Any]:
    """Return a required mapping, optionally treating parent itself as the value."""
    value: Any = parent if allowSelf and key == "" else parent.get(key)
    if not isinstance(value, dict):
        label = key or "value"
        raise ValueError(f"{label} must be a YAML mapping")
    return value


def resolvePath(config: dict[str, Any], value: str | Path) -> Path:
    """Resolve a user path relative to the directory containing experiment.yaml."""
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (Path(config["_configPath"]).parent / path).resolve()
