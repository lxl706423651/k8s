#!/usr/bin/env python3
"""Compile a parameterized two-router-per-AS star topology for 0918E1.

Inputs: positional AS count m plus optional output/platform/RS flags.
Outputs: Docker Compose build contexts and topology.json under the selected output directory.
Side effects: replaces only the selected compiler output directory.
Context: run from the repository checkout; generated output is later copied into one VM.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
from pathlib import Path
from typing import Any

import yaml


SCRIPT_DIR = Path(__file__).resolve().parent


def findRepoRoot(start: Path) -> Path:
    """Find the nearest SeedEMU source root above this experiment."""
    for candidate in (start, *start.parents):
        if (candidate / "setup.py").is_file() and (candidate / "seedemu").is_dir():
            return candidate
    raise RuntimeError(f"cannot find SeedEMU repository root above {start}")


REPO_ROOT = findRepoRoot(SCRIPT_DIR)
sys.path = [entry for entry in sys.path if entry != str(REPO_ROOT)]
sys.path.insert(0, str(REPO_ROOT))

from seedemu.compiler import Docker, Platform
from seedemu.core import Emulator
from seedemu.layers import Base, Ebgp, Ibgp, Ospf, Routing


IX_ID = 100
FIRST_ASN = 64512
IX_NETWORK = ipaddress.ip_network("10.100.0.0/23")
IX_ROUTE_SERVER_ADDRESS = ipaddress.ip_address("10.100.1.254")
PAIR_SUPERNET = ipaddress.ip_network("172.16.0.0/12")
EXTERNAL_IX_NETWORK_NAME = "e1-0918e1-external-ix"
MAX_AS_COUNT = 508


def parseArgs() -> argparse.Namespace:
    """Parse the required AS count and optional compiler settings."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("as_count", type=int, metavar="m", help="number of ASes and iperf pairs")
    parser.add_argument("--output-dir", type=Path, help="compiler output directory")
    parser.add_argument("--platform", choices=("amd64", "arm64"), default="amd64")
    parser.add_argument(
        "--without-rs-peering",
        action="store_true",
        help="keep the IX star but omit route-server BGP sessions",
    )
    parser.add_argument(
        "--external-ix",
        action="store_true",
        help="attach every Border service to the shared external IX Docker network",
    )
    return parser.parse_args()


def buildStarTopology(
    asCount: int, enableRsPeering: bool = True, enableExternalIx: bool = False
) -> tuple[Emulator, dict[str, Any]]:
    """Create m ASes, each with a border/peer router pair and one IX attachment."""
    if not 1 <= asCount <= MAX_AS_COUNT:
        raise ValueError(f"as_count must be in 1..{MAX_AS_COUNT}")

    emulator = Emulator()
    base = Base()
    ebgp = Ebgp()
    # Docker reserves the first usable address as the bridge gateway. Keep the
    # route server at the far end of /23 so 508 AS border addresses remain available.
    base.createInternetExchange(
        IX_ID,
        str(IX_NETWORK),
        create_rs=True,
        rsAddress=str(IX_ROUTE_SERVER_ADDRESS),
    )

    pairs: list[dict[str, Any]] = []
    for index in range(asCount):
        asn = FIRST_ASN + index
        pairNetwork = ipaddress.ip_network(
            (int(PAIR_SUPERNET.network_address) + index * 8, 29)
        )
        pairPrefix = str(pairNetwork)
        borderAddress = str(pairNetwork.network_address + 2)
        peerAddress = str(pairNetwork.network_address + 3)
        ixAddress = str(IX_NETWORK.network_address + index + 2)

        autonomousSystem = base.createAutonomousSystem(asn)
        autonomousSystem.createNetwork("pairnet", pairPrefix)
        border = autonomousSystem.createRouter("border")
        peer = autonomousSystem.createRouter("peer")
        border.joinNetwork("pairnet", borderAddress).joinNetwork(f"ix{IX_ID}", ixAddress)
        peer.joinNetwork("pairnet", peerAddress)

        # The server is persistent; the guest runner starts one controlled client per AS.
        border.addSoftware("iperf3 iputils-ping")
        peer.addSoftware("iperf3 iputils-ping")
        peer.appendStartCommand("iperf3 -s -D -p 5201 --logfile /tmp/iperf3-server.log")
        if enableRsPeering:
            ebgp.addRsPeer(IX_ID, asn, routerName="border")

        pairs.append(
            {
                "index": index,
                "asn": asn,
                "clientNode": "border",
                "serverNode": "peer",
                "clientAddress": borderAddress,
                "serverAddress": peerAddress,
                "ixAddress": ixAddress,
                "port": 5201,
            }
        )

    emulator.addLayer(base)
    emulator.addLayer(Routing())
    emulator.addLayer(ebgp)
    emulator.addLayer(Ibgp())
    emulator.addLayer(Ospf())
    emulator.render()
    manifest: dict[str, Any] = {
        "schemaVersion": 1,
        "asCount": asCount,
        "iperfPairCount": asCount,
        "routerCount": asCount * 2,
        "ixId": IX_ID,
        "routeServerAddress": str(IX_ROUTE_SERVER_ADDRESS),
        "routeServerPeering": enableRsPeering,
        "trafficDirection": "border-to-peer",
        "externalIx": enableExternalIx,
        "pairs": pairs,
    }
    return emulator, manifest


def addComposeServices(outputDir: Path, manifest: dict[str, Any]) -> None:
    """Resolve stable compiler service names from generated Compose labels."""
    composePath = outputDir / "docker-compose.yml"
    compose = yaml.safe_load(composePath.read_text(encoding="utf-8"))
    byNode: dict[tuple[int, str], str] = {}
    routeServerService: str | None = None
    for serviceName, service in (compose.get("services") or {}).items():
        labels = service.get("labels") or {}
        if str(labels.get("org.seedsecuritylabs.seedemu.meta.role")) == "Route Server":
            routeServerService = str(serviceName)
        rawAsn = labels.get("org.seedsecuritylabs.seedemu.meta.asn")
        nodeName = labels.get("org.seedsecuritylabs.seedemu.meta.nodename")
        if rawAsn is None or nodeName is None:
            continue
        byNode[(int(rawAsn), str(nodeName))] = str(serviceName)

    for pair in manifest["pairs"]:
        asn = int(pair["asn"])
        pair["clientService"] = byNode[(asn, pair["clientNode"])]
        pair["serverService"] = byNode[(asn, pair["serverNode"])]
    if manifest.get("externalIx"):
        externalNetwork = str(manifest.get("externalIxNetworkName", EXTERNAL_IX_NETWORK_NAME))
        compose.setdefault("networks", {})[externalNetwork] = {
            "external": True,
            "name": externalNetwork,
        }
        for pair in manifest["pairs"]:
            client = compose["services"][pair["clientService"]]
            client.setdefault("networks", {})[externalNetwork] = {}
    if routeServerService is None:
        raise ValueError("generated Compose is missing the IX route-server service")
    manifest["routeServerService"] = routeServerService
    if manifest.get("externalIx"):
        manifest["externalIxNetworkName"] = EXTERNAL_IX_NETWORK_NAME
    composePath.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")


def createSharedRuntimeCompose(outputDir: Path, manifest: dict[str, Any]) -> None:
    """Replace per-node image commits with one runtime image and bind-mounted node files."""
    composePath = outputDir / "docker-compose.yml"
    compose = yaml.safe_load(composePath.read_text(encoding="utf-8"))
    services: dict[str, dict[str, Any]] = compose["services"]
    dummyNames = [
        name
        for name, service in services.items()
        if isinstance(service.get("build"), dict) and "dummies/" in str(service["build"].get("dockerfile", ""))
    ]
    if len(dummyNames) != 1:
        raise ValueError(f"expected one SeedEMU router dummy image, found {dummyNames}")
    dummyName = dummyNames[0]
    runtimeService = "0918e1_runtime"
    runtimeImage = "0918e1-router-runtime:latest"
    runtimeDir = outputDir / "0918e1_runtime"
    runtimeDir.mkdir(parents=True, exist_ok=True)
    (runtimeDir / "Dockerfile").write_text(
        "\n".join(
            [
                f"FROM {dummyName}",
                "ARG DEBIAN_FRONTEND=noninteractive",
                "RUN apt-get update && apt-get install -y --no-install-recommends iperf3 && rm -rf /var/lib/apt/lists/*",
                "RUN mkdir -p /usr/share/doc/bird2/examples/ && touch /usr/share/doc/bird2/examples/bird.conf",
                'CMD ["/bin/true"]',
                "",
            ]
        ),
        encoding="utf-8",
    )

    optimized: dict[str, dict[str, Any]] = {}
    for serviceName, service in services.items():
        optimized[serviceName] = service
        if serviceName == dummyName:
            optimized[runtimeService] = {
                "build": "./0918e1_runtime",
                "image": runtimeImage,
                "depends_on": [dummyName],
            }
        build = service.get("build")
        if not isinstance(build, str) or not build.startswith("./"):
            continue
        nodeDir = outputDir / build
        dockerfile = nodeDir / "Dockerfile"
        if not dockerfile.is_file():
            continue
        mounts: list[str] = []
        for line in dockerfile.read_text(encoding="utf-8").splitlines():
            if not line.startswith("COPY "):
                continue
            _, source, destination = line.split(maxsplit=2)
            mounts.append(f"{build}/{source}:{destination}")
        service.pop("build", None)
        service["image"] = runtimeImage
        service["depends_on"] = [runtimeService]
        service["volumes"] = [*(service.get("volumes") or []), *mounts]
        service["working_dir"] = "/"
        service["command"] = ["/bin/bash", "/start.sh"]

    compose["services"] = optimized
    composePath.write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")
    manifest["composeBuildMode"] = "shared-runtime-bind-mounts"
    manifest["sharedRuntimeImage"] = runtimeImage


def main() -> int:
    """Render and compile the requested star topology."""
    args = parseArgs()
    outputDir = args.output_dir or Path("builds") / f"as-{args.as_count:04d}"
    if not outputDir.is_absolute():
        outputDir = (SCRIPT_DIR / outputDir).resolve()
    outputDir.parent.mkdir(parents=True, exist_ok=True)
    platform = Platform.AMD64 if args.platform == "amd64" else Platform.ARM64

    emulator, manifest = buildStarTopology(
        args.as_count,
        enableRsPeering=not args.without_rs_peering,
        enableExternalIx=args.external_ix,
    )
    compiler = Docker(platform=platform, internetMapEnabled=False, etherViewEnabled=False)
    emulator.compile(compiler, str(outputDir), override=True)
    addComposeServices(outputDir, manifest)
    createSharedRuntimeCompose(outputDir, manifest)
    (outputDir / "topology.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": "PASS",
                "asCount": args.as_count,
                "routerCount": args.as_count * 2,
                "iperfPairCount": args.as_count,
                "routeServerPeering": not args.without_rs_peering,
                "outputDir": str(outputDir),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
