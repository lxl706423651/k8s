#!/usr/bin/env python3
from __future__ import annotations

import sys
import os
from pathlib import Path

ORIGIN_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ORIGIN_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from seedemu.core import Emulator
from seedemu.layers import Base, Routing, Ebgp, Ibgp, Ospf, PeerRelationship
from seedemu.utilities import Makers
from seedemu.compiler import Platform

from origin_k8s.native_k8s_compiler import NativeKubernetesCompiler


PLATFORM = Platform.AMD64
IMAGE_REGISTRY_PREFIX = "seedemu"
HOSTS_PER_AS = 2
OUTPUT_DIR = Path("./output")


def build_mini_internet(hosts_per_as: int) -> Emulator:
    emu = Emulator()
    ebgp = Ebgp()
    base = Base()

    ix100 = base.createInternetExchange(100)
    ix101 = base.createInternetExchange(101)
    ix102 = base.createInternetExchange(102)
    ix103 = base.createInternetExchange(103)
    ix104 = base.createInternetExchange(104)
    ix105 = base.createInternetExchange(105)

    ix100.getPeeringLan().setDisplayName("NYC-100")
    ix101.getPeeringLan().setDisplayName("San Jose-101")
    ix102.getPeeringLan().setDisplayName("Chicago-102")
    ix103.getPeeringLan().setDisplayName("Miami-103")
    ix104.getPeeringLan().setDisplayName("Boston-104")
    ix105.getPeeringLan().setDisplayName("Houston-105")

    Makers.makeTransitAs(base, 2, [100, 101, 102, 105], [(100, 101), (101, 102), (100, 105)])
    Makers.makeTransitAs(base, 3, [100, 103, 104, 105], [(100, 103), (100, 105), (103, 105), (103, 104)])
    Makers.makeTransitAs(base, 4, [100, 102, 104], [(100, 104), (102, 104)])
    Makers.makeTransitAs(base, 11, [102, 105], [(102, 105)])
    Makers.makeTransitAs(base, 12, [101, 104], [(101, 104)])

    for asn, ix in [
        (150, 100), (151, 100), (152, 101), (153, 101), (154, 102),
        (160, 103), (161, 103), (162, 103), (163, 104), (164, 104),
        (170, 105), (171, 105),
    ]:
        Makers.makeStubAsWithHosts(emu, base, asn, ix, hosts_per_as)

    ebgp.addRsPeers(100, [2, 3, 4])
    ebgp.addRsPeers(102, [2, 4])
    ebgp.addRsPeers(104, [3, 4])
    ebgp.addRsPeers(105, [2, 3])

    ebgp.addPrivatePeerings(100, [2], [150, 151], PeerRelationship.Provider)
    ebgp.addPrivatePeerings(100, [3], [150], PeerRelationship.Provider)
    ebgp.addPrivatePeerings(101, [2], [12], PeerRelationship.Provider)
    ebgp.addPrivatePeerings(101, [12], [152, 153], PeerRelationship.Provider)
    ebgp.addPrivatePeerings(102, [2, 4], [11, 154], PeerRelationship.Provider)
    ebgp.addPrivatePeerings(102, [11], [154], PeerRelationship.Provider)
    ebgp.addPrivatePeerings(103, [3], [160, 161, 162], PeerRelationship.Provider)
    ebgp.addPrivatePeerings(104, [3, 4], [12], PeerRelationship.Provider)
    ebgp.addPrivatePeerings(104, [4], [163], PeerRelationship.Provider)
    ebgp.addPrivatePeerings(104, [12], [164], PeerRelationship.Provider)
    ebgp.addPrivatePeerings(105, [3], [11, 170], PeerRelationship.Provider)
    ebgp.addPrivatePeerings(105, [11], [171], PeerRelationship.Provider)

    emu.addLayer(base)
    emu.addLayer(Routing())
    emu.addLayer(ebgp)
    emu.addLayer(Ibgp())
    emu.addLayer(Ospf())
    emu.render()
    return emu


def main() -> int:
    if len(sys.argv) > 1:
        print("Usage: mini_internet_k8s_native_compile.py", file=sys.stderr)
        return 2

    os.chdir(Path(__file__).resolve().parent)
    
    NAMESPACE= "seedemu-k3s-real-topo"
    compiler = NativeKubernetesCompiler(
        platform=PLATFORM,
        image_registry_prefix=IMAGE_REGISTRY_PREFIX,
        namespace=NAMESPACE
    )

    emu = build_mini_internet(hosts_per_as=HOSTS_PER_AS)
    emu.compile(compiler, str(OUTPUT_DIR), override=True)

    print("=" * 72)
    print("Native Kubernetes baseline compilation complete.")
    print("=" * 72)
    print("Config file: not used")
    print(f"Output directory: {Path.cwd() / OUTPUT_DIR}")
    print(f"Image registry prefix: {IMAGE_REGISTRY_PREFIX}")
    print(f"Runtime Makefile: {ORIGIN_ROOT / 'running' / 'Makefile'}")
    print("Inventory required for compile: no")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
