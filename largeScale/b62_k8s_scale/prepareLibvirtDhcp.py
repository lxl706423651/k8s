#!/usr/bin/env python3
"""Prepare libvirt DHCP reservations before b62 KVM VM creation.

Inputs:
- configKvmOvn.yaml with explicit nodes.

Outputs:
- Updated libvirt network DHCP host reservations.
- Filtered dnsmasq lease status file for matching b62 nodes.

Side effects:
- Calls `virsh net-update` and edits `/var/lib/libvirt/dnsmasq/<bridge>.status`.
  This is intentionally run before k8sTools creates VMs so dnsmasq has all
  static reservations loaded before guests send their first DHCP request.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import yaml


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run one command."""
    print("+ " + " ".join(cmd), flush=True)
    result = subprocess.run(cmd, text=True, capture_output=True)
    if result.stdout:
        print(result.stdout, end="", flush=True)
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr, flush=True)
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd, output=result.stdout, stderr=result.stderr)
    return result


def loadConfig(path: Path) -> dict[str, Any]:
    """Load kvmOvn YAML config."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"Invalid YAML root: {path}")
    return data


def networkName(config: dict[str, Any]) -> str:
    """Return the libvirt network name from config."""
    kvm = config.get("kvm") if isinstance(config.get("kvm"), dict) else {}
    return str(kvm.get("network") or "default")


def kvmSettings(config: dict[str, Any]) -> dict[str, str]:
    """Return KVM network settings from config."""
    kvm = config.get("kvm") if isinstance(config.get("kvm"), dict) else {}
    network = str(kvm.get("network") or "default")
    return {
        "network": network,
        "bridge": str(kvm.get("networkBridge") or ""),
        "cidr": str(kvm.get("networkCidr") or ""),
        "gateway": str(kvm.get("networkGateway") or ""),
        "dhcpStart": str(kvm.get("dhcpStart") or ""),
        "dhcpEnd": str(kvm.get("dhcpEnd") or ""),
    }


def nodes(config: dict[str, Any]) -> list[dict[str, str]]:
    """Return explicit node reservation records."""
    values = config.get("nodes") or []
    result: list[dict[str, str]] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        result.append(
            {
                "name": str(item["name"]),
                "ip": str(item["ip"]),
                "mac": str(item["mac"]).lower(),
            }
        )
    if not result:
        raise SystemExit("config has no explicit nodes")
    return result


def netXml(network: str) -> ET.Element:
    """Return parsed libvirt network XML."""
    result = run(["virsh", "-c", "qemu:///system", "net-dumpxml", network])
    return ET.fromstring(result.stdout)


def ensureNetwork(settings: dict[str, str]) -> None:
    """Define and start the dedicated b62 libvirt network when needed."""
    network = settings["network"]

    def isActive(info_stdout: str) -> bool:
        for line in info_stdout.splitlines():
            if line.strip().lower().startswith("active:"):
                return line.split(":", 1)[1].strip().lower() == "yes"
        return False

    info = run(["virsh", "-c", "qemu:///system", "net-info", network], check=False)
    if info.returncode != 0:
        if not all(settings[key] for key in ("bridge", "cidr", "gateway", "dhcpStart", "dhcpEnd")):
            raise SystemExit(f"libvirt network {network} does not exist and network settings are incomplete")
        cidr = ipaddress.ip_network(settings["cidr"], strict=False)
        gateway = ipaddress.ip_address(settings["gateway"])
        if gateway not in cidr:
            raise SystemExit(f"gateway {gateway} is outside {cidr}")
        xml = f"""<network>
  <name>{network}</name>
  <forward mode='nat'/>
  <bridge name='{settings["bridge"]}' stp='on' delay='0'/>
  <ip address='{gateway}' netmask='{cidr.netmask}'>
    <dhcp>
      <range start='{settings["dhcpStart"]}' end='{settings["dhcpEnd"]}'/>
    </dhcp>
  </ip>
</network>
"""
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".xml", delete=False) as handle:
            handle.write(xml)
            xml_path = handle.name
        try:
            run(["virsh", "-c", "qemu:///system", "net-define", xml_path])
            run(["virsh", "-c", "qemu:///system", "net-autostart", network])
        finally:
            Path(xml_path).unlink(missing_ok=True)

    info = run(["virsh", "-c", "qemu:///system", "net-info", network], check=False)
    if not isActive(info.stdout):
        start = run(["virsh", "-c", "qemu:///system", "net-start", network], check=False)
        if start.returncode != 0:
            info = run(["virsh", "-c", "qemu:///system", "net-info", network], check=False)
            if not isActive(info.stdout):
                raise subprocess.CalledProcessError(start.returncode, start.args, output=start.stdout, stderr=start.stderr)


def bridgeName(root: ET.Element, network: str) -> str:
    """Return the bridge backing a libvirt network."""
    bridge = root.find("bridge")
    if bridge is not None and bridge.get("name"):
        return str(bridge.get("name"))
    if network == "default":
        return "virbr0"
    return network


def hostXml(name: str, ip: str, mac: str) -> str:
    """Return a libvirt DHCP host XML fragment."""
    return f"<host mac='{mac}' name='{name}' ip='{ip}'/>"


def deleteMatchingHosts(network: str, root: ET.Element, records: list[dict[str, str]], prefix: str) -> None:
    """Delete stale host reservations that match b62 names/IPs/MACs."""
    names = {record["name"] for record in records}
    ips = {record["ip"] for record in records}
    macs = {record["mac"] for record in records}
    for host in root.findall(".//host"):
        name = host.get("name") or ""
        ip = host.get("ip") or ""
        mac = (host.get("mac") or "").lower()
        if name.startswith(prefix) or name in names or ip in ips or mac in macs:
            fragment = hostXml(name, ip, mac)
            run(["virsh", "-c", "qemu:///system", "net-update", network, "delete", "ip-dhcp-host", fragment, "--live", "--config"], check=False)


def addHosts(network: str, records: list[dict[str, str]]) -> None:
    """Add current b62 DHCP reservations."""
    for record in records:
        fragment = hostXml(record["name"], record["ip"], record["mac"])
        run(["virsh", "-c", "qemu:///system", "net-update", network, "add", "ip-dhcp-host", fragment, "--live", "--config"])


def cleanLeaseStatus(bridge: str, records: list[dict[str, str]], prefix: str) -> None:
    """Filter matching entries from dnsmasq lease status."""
    status_path = Path("/var/lib/libvirt/dnsmasq") / f"{bridge}.status"
    if not status_path.exists():
        print(f"lease status not found: {status_path}")
        return
    backup_path = status_path.with_name(f"{status_path.name}.bak.b62-{time.strftime('%Y%m%d_%H%M%S')}")
    backup_path.write_bytes(status_path.read_bytes())
    names = {record["name"] for record in records}
    ips = {record["ip"] for record in records}
    macs = {record["mac"] for record in records}
    data = json.loads(status_path.read_text(encoding="utf-8") or "[]")
    filtered = [
        item
        for item in data
        if not (
            str(item.get("hostname", "")).startswith(prefix)
            or item.get("hostname") in names
            or item.get("ip-address") in ips
            or str(item.get("mac-address", "")).lower() in macs
        )
    ]
    status_path.write_text(json.dumps(filtered, indent=2) + "\n", encoding="utf-8")
    print(f"filtered {len(data) - len(filtered)} stale DHCP leases from {status_path}")


def parseArgs() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--prefix", default="seedemu-b62-")
    parser.add_argument("--settle-seconds", type=float, default=3.0)
    return parser.parse_args()


def main() -> int:
    """CLI entrypoint."""
    args = parseArgs()
    config = loadConfig(args.config)
    settings = kvmSettings(config)
    ensureNetwork(settings)
    network = networkName(config)
    records = nodes(config)
    root = netXml(network)
    bridge = bridgeName(root, network)
    deleteMatchingHosts(network, root, records, args.prefix)
    cleanLeaseStatus(bridge, records, args.prefix)
    addHosts(network, records)
    print(f"prepared {len(records)} DHCP reservations on network {network} bridge {bridge}")
    time.sleep(args.settle_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
