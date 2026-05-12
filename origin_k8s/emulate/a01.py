#!/usr/bin/env python3
# encoding: utf-8
from __future__ import annotations

import os
import sys
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


def parse_platform(raw: str) -> Platform:
    value = (raw or "amd64").strip().lower()
    if value in {"amd", "amd64", "x86_64"}:
        return Platform.AMD64
    if value in {"arm", "arm64", "aarch64"}:
        return Platform.ARM64
    raise ValueError(f"Unsupported platform: {raw}")


IMAGE_REGISTRY_PREFIX = "seedemu"
OUTPUT_DIR = Path("./output")


def run(dumpfile = None):
    os.chdir(Path(__file__).resolve().parent)

    ###############################################################################
    # Set the platform information
    if dumpfile is None:
        script_name = os.path.basename(__file__)

        if len(sys.argv) == 1:
            platform = Platform.AMD64
        elif len(sys.argv) == 2:
            if sys.argv[1].lower() == 'amd':
                platform = Platform.AMD64
            elif sys.argv[1].lower() == 'arm':
                platform = Platform.ARM64
            else:
                print(f"Usage:  {script_name} amd|arm")
                sys.exit(1)
        else:
            print(f"Usage:  {script_name} amd|arm")
            sys.exit(1)

    ###############################################################################
    # Create the base layer
    base  = Base()

    # Create two Internet Exchanges, where BGP routers peer with one another.
    base.createInternetExchange(100)
    base.createInternetExchange(101)

    ###############################################################################
    # Create and configure a transit autonomous system 

    as2 = base.createAutonomousSystem(2)

    # Create 3 internal networks
    as2.createNetwork('net0')
    as2.createNetwork('net1')
    as2.createNetwork('net2')

    # Create four routers and link them in a linear structure:
    # ix100 <--> r1 <--> r2 <--> r3 <--> r4 <--> ix101
    # r1 and r4 are BGP routers because they are connected to Internet exchanges
    as2.createRouter('r1').joinNetwork('net0').joinNetwork('ix100')
    as2.createRouter('r2').joinNetwork('net0').joinNetwork('net1')
    as2.createRouter('r3').joinNetwork('net1').joinNetwork('net2')
    as2.createRouter('r4').joinNetwork('net2').joinNetwork('ix101')

    ###############################################################################
    # Create and set up the stub AS (AS-151)

    as151 = base.createAutonomousSystem(151)

    # Create an internal network and a router
    as151.createNetwork('net0')
    as151.createRouter('router0').joinNetwork('net0').joinNetwork('ix100')

    # Create a host node 
    as151.createHost('host0').joinNetwork('net0')


    ###############################################################################
    # Create and set up the stub AS (AS-152)
    as152 = base.createAutonomousSystem(152)
    as152.createNetwork('net0')
    as152.createRouter('router0').joinNetwork('net0').joinNetwork('ix101')
    as152.createHost('host0').joinNetwork('net0')


    ###############################################################################
    # Create and set up the stub AS (AS-153)
    as153 = base.createAutonomousSystem(153)
    as153.createNetwork('net0')
    as153.createRouter('router0').joinNetwork('net0').joinNetwork('ix101')
    as153.createHost('host0').joinNetwork('net0')



    ###############################################################################
    # Create the EBGP layer, conduct peering
    ebgp    = Ebgp()

    # Peer AS-2 with ASes 151, 152, and 153 (AS-2 is the Internet service provider)
    ebgp.addPrivatePeering(100, 2, 151, abRelationship = PeerRelationship.Provider)
    ebgp.addPrivatePeering(101, 2, 152, abRelationship = PeerRelationship.Provider)
    ebgp.addPrivatePeering(101, 2, 153, abRelationship = PeerRelationship.Provider)

    # Peer AS-152 and AS-153 (as equal peers for mutual benefit)
    ebgp.addPrivatePeering(101, 152, 153, abRelationship = PeerRelationship.Peer)


    ###############################################################################
    # Add all the necessary layers 
    emu  = Emulator()

    emu.addLayer(base)
    emu.addLayer(Routing())  
    emu.addLayer(ebgp)
    emu.addLayer(Ibgp())
    emu.addLayer(Ospf())

    if dumpfile is not None:
        ###############################################################################
        # Save the emulation if needed (can be reused by other emulation)
        # Must be called before the rendering
        emu.dump(dumpfile)
        ###############################################################################
    else:
        ###############################################################################
        # Render the emulation
        emu.render()
        PLATFORM = Platform.AMD64
        NAMESPACE= "seedemu-k3s-a01"
        compiler = NativeKubernetesCompiler(
            platform=PLATFORM,
            image_registry_prefix=IMAGE_REGISTRY_PREFIX,
            namespace=NAMESPACE
        )
        emu.compile(compiler, str(OUTPUT_DIR), override=True)

if __name__ == "__main__":
    run()
