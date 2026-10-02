#!/usr/bin/env python3
"""Provision the identical local libvirt VMs declared in experiment.yaml.

Inputs: experiment.yaml, an SSH public key, and an Ubuntu cloud image (downloaded if absent).
Outputs: libvirt network/VM definitions plus qcow2 and cloud-init files under state/.
Side effects: creates and starts a libvirt NAT network and all missing experiment VMs.
Context: run once on the KVM host before executeVmMatrix.py; existing domains are never replaced.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import ipaddress
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml

from loadExperiment import loadConfig, resolvePath


SCRIPT_DIR = Path(__file__).resolve().parent


def parseArgs() -> argparse.Namespace:
    """Parse provisioning and non-destructive preview options."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=SCRIPT_DIR / "experiment.yaml")
    parser.add_argument("--reuse-existing", action="store_true", help="leave declared existing VMs unchanged")
    parser.add_argument("--skip-wait", action="store_true", help="return after virt-install starts every VM")
    parser.add_argument("--dry-run", action="store_true", help="validate and print actions without changing libvirt")
    return parser.parse_args()


def runCommand(command: list[str], *, capture: bool = False, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run one provisioning command with an auditable shell-style log line."""
    print("+ " + shlex.join(command), flush=True)
    return subprocess.run(command, text=True, capture_output=capture, check=check)


def requireCommands(names: list[str]) -> None:
    """Fail before mutation when a required local executable is unavailable."""
    missing = [name for name in names if shutil.which(name) is None]
    if missing:
        raise RuntimeError("missing required commands: " + ", ".join(missing))


def ensureBaseImage(config: dict[str, Any], dryRun: bool) -> Path:
    """Return the configured cloud image, downloading it to state/base when absent."""
    vm = config["vm"]
    imagePath = resolvePath(config, vm["baseImagePath"])
    if imagePath.is_file():
        return imagePath
    if dryRun:
        print(f"would download {vm['baseImageUrl']} -> {imagePath}")
        return imagePath
    imagePath.parent.mkdir(parents=True, exist_ok=True)
    temporaryPath = imagePath.with_suffix(imagePath.suffix + ".partial")
    runCommand(["curl", "-fL", "--retry", "3", "-o", str(temporaryPath), str(vm["baseImageUrl"])])
    temporaryPath.replace(imagePath)
    return imagePath


def networkPrefixLength(cidr: str) -> int:
    """Return the prefix length used by the VM cloud-init network config."""
    return ipaddress.ip_network(cidr, strict=True).prefixlen


def createNetworkXml(network: dict[str, Any]) -> str:
    """Render the isolated experiment NAT network consumed by virsh net-define."""
    subnet = ipaddress.ip_network(str(network["cidr"]), strict=True)
    netmask = subnet.netmask
    return f"""<network>
  <name>{network['name']}</name>
  <forward mode='nat'/>
  <bridge name='{network['bridge']}' stp='on' delay='0'/>
  <ip address='{network['gateway']}' netmask='{netmask}'>
    <dhcp>
      <range start='{network['dhcpStart']}' end='{network['dhcpEnd']}'/>
    </dhcp>
  </ip>
</network>
"""


def createExternalNetworkXml(network: dict[str, Any]) -> str:
    """Render the isolated shared L2 network used by Border macvlan interfaces."""
    subnet = ipaddress.ip_network(str(network["cidr"]), strict=True)
    return f"""<network>
  <name>{network['name']}</name>
  <forward mode='none'/>
  <bridge name='{network['bridge']}' stp='on' delay='0'/>
  <ip address='{network['gateway']}' netmask='{subnet.netmask}'/>
</network>
"""


def ensureNetwork(config: dict[str, Any], dryRun: bool) -> None:
    """Create and start the dedicated libvirt network when it does not exist."""
    network = config["vm"]["network"]
    info = subprocess.run(
        ["virsh", "net-info", str(network["name"])], text=True, capture_output=True, check=False
    )
    if info.returncode == 0:
        if "Active:         yes" not in info.stdout and not dryRun:
            runCommand(["virsh", "net-start", str(network["name"])])
        print(f"libvirt network already exists: {network['name']}")
        return
    if dryRun:
        print(f"would define and start libvirt network {network['name']}")
        return
    stateDir = resolvePath(config, "state")
    stateDir.mkdir(parents=True, exist_ok=True)
    xmlPath = stateDir / "libvirtNetwork.xml"
    xmlPath.write_text(createNetworkXml(network), encoding="utf-8")
    runCommand(["virsh", "net-define", str(xmlPath)])
    runCommand(["virsh", "net-autostart", str(network["name"])])
    runCommand(["virsh", "net-start", str(network["name"])])


def ensureExternalNetwork(config: dict[str, Any], dryRun: bool) -> None:
    """Create and start the shared external IX L2 network when absent."""
    network = config["vm"]["externalNetwork"]
    info = subprocess.run(
        ["virsh", "net-info", str(network["name"])], text=True, capture_output=True, check=False
    )
    if info.returncode == 0:
        if "Active:         yes" not in info.stdout and not dryRun:
            runCommand(["virsh", "net-start", str(network["name"])])
        print(f"external network already exists: {network['name']}")
        return
    if dryRun:
        print(f"would define and start external network {network['name']}")
        return
    stateDir = resolvePath(config, "state")
    stateDir.mkdir(parents=True, exist_ok=True)
    xmlPath = stateDir / "externalNetwork.xml"
    xmlPath.write_text(createExternalNetworkXml(network), encoding="utf-8")
    runCommand(["virsh", "net-define", str(xmlPath)])
    runCommand(["virsh", "net-autostart", str(network["name"])])
    runCommand(["virsh", "net-start", str(network["name"])])


def hasDomainNetwork(domain: str, source: str) -> bool:
    """Return whether a libvirt domain already has an interface on one network."""
    result = subprocess.run(["virsh", "domiflist", domain], text=True, capture_output=True, check=False)
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 4 and fields[1] == "network" and fields[2] == source:
            return True
    return False


def ensureExternalInterface(config: dict[str, Any], node: dict[str, Any], dryRun: bool) -> None:
    """Attach the shared external-network vNIC to one VM domain."""
    network = config["vm"]["externalNetwork"]
    domain = str(node["name"])
    if hasDomainNetwork(domain, str(network["name"])):
        return
    if dryRun:
        print(f"would attach {network['name']} to {domain}")
        return
    runCommand(
        [
            "virsh",
            "attach-interface",
            "--domain",
            domain,
            "--type",
            "network",
            "--source",
            str(network["name"]),
            "--model",
            "virtio",
            "--mac",
            str(node["xvmMac"]),
            "--live",
            "--config",
        ]
    )


def cloudInitDocuments(
    config: dict[str, Any], node: dict[str, Any], publicKey: str
) -> tuple[str, str, str]:
    """Render user-data, meta-data, and static network-data for one VM node."""
    vm = config["vm"]
    network = vm["network"]
    sshUser = str(vm["ssh"]["user"])
    userData = {
        "users": [
            "default",
            {
                "name": sshUser,
                "groups": "adm,sudo",
                "shell": "/bin/bash",
                "sudo": "ALL=(ALL) NOPASSWD:ALL",
                "ssh_authorized_keys": [publicKey],
            },
        ],
        "ssh_pwauth": False,
        "package_update": True,
        "packages": ["docker.io", "docker-compose", "python3", "rsync", "iperf3"],
        "runcmd": [
            ["systemctl", "enable", "--now", "docker"],
            ["usermod", "-aG", "docker", sshUser],
        ],
    }
    metaData = {"instance-id": node["name"], "local-hostname": node["name"]}
    networkData = {
        "version": 2,
        "ethernets": {
            "ens3": {
                "match": {"macaddress": str(node["mac"]).lower()},
                "set-name": "ens3",
                "addresses": [f"{node['address']}/{networkPrefixLength(network['cidr'])}"],
                "routes": [{"to": "default", "via": str(network["gateway"])}],
                "nameservers": {"addresses": [str(network["gateway"]), "1.1.1.1"]},
            }
        },
    }
    external = vm["externalNetwork"]
    externalInterface: dict[str, Any] = {
        "match": {"macaddress": str(node["xvmMac"]).lower()},
        "set-name": str(external.get("parentInterface", "ens4")),
        "optional": True,
    }
    if node.get("xvmAddress"):
        externalInterface["addresses"] = [
            f"{node['xvmAddress']}/{networkPrefixLength(external['cidr'])}"
        ]
    networkData["ethernets"][str(external.get("parentInterface", "ens4"))] = externalInterface
    return (
        "#cloud-config\n" + yaml.safe_dump(userData, sort_keys=False),
        yaml.safe_dump(metaData, sort_keys=False),
        yaml.safe_dump(networkData, sort_keys=False),
    )


def provisionNode(
    config: dict[str, Any], node: dict[str, Any], baseImage: Path, publicKey: str, reuseExisting: bool, dryRun: bool
) -> str:
    """Create one qcow2-backed VM without replacing any existing libvirt domain."""
    name = str(node["name"])
    existing = subprocess.run(["virsh", "dominfo", name], text=True, capture_output=True, check=False)
    if existing.returncode == 0:
        if not reuseExisting:
            raise RuntimeError(f"domain already exists: {name}; use --reuse-existing or destroy it explicitly")
        print(f"reuse existing domain: {name}")
        return name
    if dryRun:
        print(f"would create {name}: {config['vm']['vcpus']} vCPU, {config['vm']['memoryGiB']} GiB")
        return name

    vm = config["vm"]
    storageDir = resolvePath(config, vm["storageDir"])
    cloudInitDir = resolvePath(config, vm["cloudInitDir"]) / name
    storageDir.mkdir(parents=True, exist_ok=True)
    cloudInitDir.mkdir(parents=True, exist_ok=True)
    diskPath = storageDir / f"{name}.qcow2"
    seedPath = storageDir / f"{name}-seed.img"
    if diskPath.exists() or seedPath.exists():
        raise RuntimeError(f"refusing to overwrite existing VM storage for {name}")

    userData, metaData, networkData = cloudInitDocuments(config, node, publicKey)
    userPath = cloudInitDir / "user-data"
    metaPath = cloudInitDir / "meta-data"
    networkPath = cloudInitDir / "network-config"
    userPath.write_text(userData, encoding="utf-8")
    metaPath.write_text(metaData, encoding="utf-8")
    networkPath.write_text(networkData, encoding="utf-8")

    runCommand(["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", str(baseImage), str(diskPath)])
    runCommand(["qemu-img", "resize", str(diskPath), f"{int(vm['diskGiB'])}G"])
    runCommand(
        [
            "cloud-localds",
            "--network-config",
            str(networkPath),
            str(seedPath),
            str(userPath),
            str(metaPath),
        ]
    )
    runCommand(
        [
            "virt-install",
            "--name",
            name,
            "--memory",
            str(int(float(vm["memoryGiB"]) * 1024)),
            "--vcpus",
            str(int(vm["vcpus"])),
            "--disk",
            f"path={diskPath},format=qcow2,bus=virtio",
            "--disk",
            f"path={seedPath},device=cdrom",
            "--os-variant",
            str(vm["osVariant"]),
            "--network",
            f"network={vm['network']['name']},model=virtio,mac={node['mac']}",
            "--network",
            f"network={vm['externalNetwork']['name']},model=virtio,mac={node['xvmMac']}",
            "--graphics",
            "none",
            "--noautoconsole",
            "--import",
        ]
    )
    return name


def sshBase(config: dict[str, Any], node: dict[str, Any]) -> list[str]:
    """Build the non-interactive SSH prefix shared by provisioning readiness checks."""
    ssh = config["vm"]["ssh"]
    keyPath = Path(str(ssh["privateKey"])).expanduser().resolve()
    return [
        "ssh",
        "-i",
        str(keyPath),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=10",
        f"{ssh['user']}@{node['address']}",
    ]


def waitForNode(config: dict[str, Any], node: dict[str, Any]) -> str:
    """Wait for cloud-init and Docker on one newly provisioned VM."""
    timeout = int(config["execution"]["sshReadyTimeoutSeconds"])
    deadline = time.monotonic() + timeout
    command = sshBase(config, node) + [
        "cloud-init status --wait >/dev/null && sudo docker info >/dev/null && echo READY"
    ]
    lastMessage = ""
    while time.monotonic() < deadline:
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        if result.returncode == 0 and "READY" in result.stdout:
            return str(node["name"])
        lastMessage = (result.stderr or result.stdout).strip()[-500:]
        time.sleep(10)
    raise TimeoutError(f"VM {node['name']} did not become ready: {lastMessage}")


def main() -> int:
    """Provision all configured nodes, then wait for them in parallel."""
    args = parseArgs()
    config = loadConfig(args.config)
    requireCommands(["virsh", "virt-install", "qemu-img", "cloud-localds", "ssh", "curl"])
    baseImage = ensureBaseImage(config, args.dry_run)
    publicKeyPath = Path(str(config["vm"]["ssh"]["publicKey"])).expanduser().resolve()
    if not publicKeyPath.is_file():
        raise FileNotFoundError(f"SSH public key not found: {publicKeyPath}")
    publicKey = publicKeyPath.read_text(encoding="utf-8").strip()
    ensureNetwork(config, args.dry_run)
    ensureExternalNetwork(config, args.dry_run)

    nodes = [*config["vm"]["nodes"], config["vm"]["sink"]]
    for node in nodes:
        provisionNode(config, node, baseImage, publicKey, args.reuse_existing, args.dry_run)
        ensureExternalInterface(config, node, args.dry_run)

    if args.dry_run or args.skip_wait:
        return 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(config["vm"]["nodes"])) as executor:
        futures = {executor.submit(waitForNode, config, node): node for node in nodes}
        for future in concurrent.futures.as_completed(futures):
            print(f"ready: {future.result()}", flush=True)
    print("all experiment VMs are ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
