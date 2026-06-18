#!/usr/bin/env python3
# encoding: utf-8

from __future__ import annotations

import argparse
from pathlib import Path
import sys


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from seedemu.compiler import Docker, Platform
from seedemu.core import Binding, Emulator, Filter
from seedemu.layers import Base, Ebgp, Ibgp, Ospf, PeerRelationship, Routing
from seedemu.services import WebService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a mixed BIRD/FRR BGP control-plane example.")
    parser.add_argument("legacy_platform", nargs="?", choices=["amd", "arm"])
    parser.add_argument("--platform", choices=["amd", "arm"])
    parser.add_argument("--output", default=str(SCRIPT_DIR / "output"))
    parser.add_argument("--dumpfile")
    parser.add_argument("--override", dest="override", action="store_true", default=True)
    parser.add_argument("--no-override", dest="override", action="store_false")
    parser.add_argument("--skip-render", dest="render", action="store_false", default=True)
    args = parser.parse_args()
    args.platform = args.platform or args.legacy_platform or "amd"
    return args


def resolve_platform(name: str) -> Platform:
    return Platform.AMD64 if name == "amd" else Platform.ARM64


def build_emulator() -> Emulator:
    emu = Emulator()

    base = Base()
    routing = Routing()
    ospf = Ospf()
    ibgp = Ibgp()
    ebgp = Ebgp()
    web = WebService()

    base.createInternetExchange(100)
    base.createInternetExchange(101)

    as2 = base.createAutonomousSystem(2)
    as2.createNetwork("net0")
    as2.createRouter("r1").joinNetwork("net0").joinNetwork("ix100")
    as2.createRouter("r2", routingBackend="frr").joinNetwork("net0").joinNetwork("ix101")

    as151 = base.createAutonomousSystem(151)
    as151.createNetwork("net0")
    as151.createRouter("router0", routingBackend="frr").joinNetwork("net0").joinNetwork("ix100")
    as151.createHost("web").joinNetwork("net0")

    as152 = base.createAutonomousSystem(152)
    as152.createNetwork("net0")
    as152.createRouter("router0").joinNetwork("net0").joinNetwork("ix101")
    as152.createHost("web").joinNetwork("net0")

    web.install("web151")
    emu.addBinding(Binding("web151", filter=Filter(nodeName="web", asn=151)))
    web.install("web152")
    emu.addBinding(Binding("web152", filter=Filter(nodeName="web", asn=152)))

    ebgp.addPrivatePeering(100, 2, 151, abRelationship=PeerRelationship.Provider)
    ebgp.addPrivatePeering(101, 2, 152, abRelationship=PeerRelationship.Provider)

    emu.addLayer(base)
    emu.addLayer(routing)
    emu.addLayer(ospf)
    emu.addLayer(ibgp)
    emu.addLayer(ebgp)
    emu.addLayer(web)
    return emu


def main() -> int:
    args = parse_args()
    emu = build_emulator()

    if args.dumpfile:
        emu.dump(args.dumpfile)
        print("Saved A12 emulator to {}".format(args.dumpfile))
        return 0

    if args.render:
        emu.render()

    output_dir = Path(args.output).resolve()
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    emu.compile(Docker(platform=resolve_platform(args.platform)), str(output_dir), override=args.override)
    print("Generated A12 Docker output in {}".format(output_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
