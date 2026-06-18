from __future__ import annotations

import importlib

import pytest

from seedemu.core import Binding, Emulator, Filter
from seedemu.layers import Base, Ebgp, Ibgp, Mpls, Ospf, PeerRelationship, Routing
from seedemu.layers._bgp_metadata import get_bgp_sessions, get_ospf_interface_intents
from seedemu.services import ExaBgpService


def _file_content(node, path: str) -> str:
    for file in node.getFiles():
        file_path, content = file.get()
        if file_path == path:
            return content
    return ""


def test_router_backend_default_and_legacy_rejection():
    as2 = Base().createAutonomousSystem(2)
    assert as2.createRouter("default").getRoutingBackend() == "bird"
    assert as2.createRouter("frr", routingBackend="frr").getRoutingBackend() == "frr"

    with pytest.raises(AssertionError, match="unsupported routing backend"):
        as2.createRouter("exabgp", routingBackend="exabgp")
    with pytest.raises(AssertionError, match="unsupported routing backend"):
        as2.createRouter("external", routingBackend="external")


def test_frr_bgp_layer_shim_is_not_exported():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("seedemu.layers.FrrBgp")
    assert not hasattr(importlib.import_module("seedemu.layers"), "FrrBgp")


def test_route_server_peering_renders_from_intent_in_routing_layer():
    emu = Emulator()
    base = Base()
    routing = Routing()
    ebgp = Ebgp()

    base.createInternetExchange(100)
    as150 = base.createAutonomousSystem(150)
    as150.createNetwork("net0")
    as150.createRouter("router0").joinNetwork("net0").joinNetwork("ix100")

    ebgp.addRsPeer(100, 150)

    emu.addLayer(base)
    emu.addLayer(routing)
    emu.addLayer(ebgp)
    emu.render()

    route_server = emu.getRegistry().get("ix", "rs", "ix100")
    router = emu.getRegistry().get("150", "rnode", "router0")

    rs_conf = _file_content(route_server, "/etc/bird/bird.conf")
    router_conf = _file_content(router, "/etc/bird/bird.conf")

    assert "protocol bgp p_as150" in rs_conf
    assert "rs client" in rs_conf
    assert "neighbor 10.100.0.150 as 150" in rs_conf
    assert "protocol bgp p_rs100" in router_conf
    assert "neighbor 10.100.0.100 as 100" in router_conf
    assert "bgp_large_community.add(PEER_COMM)" in router_conf


def test_frr_route_server_backend_is_explicitly_rejected():
    emu = Emulator()
    base = Base()
    routing = Routing()
    ebgp = Ebgp()

    ix = base.createInternetExchange(100)
    ix.getRouteServerNode().setRoutingBackend("frr")
    as150 = base.createAutonomousSystem(150)
    as150.createNetwork("net0")
    as150.createRouter("router0").joinNetwork("net0").joinNetwork("ix100")

    ebgp.addRsPeer(100, 150)

    emu.addLayer(base)
    emu.addLayer(routing)
    emu.addLayer(ebgp)

    with pytest.raises(NotImplementedError, match="FRR route-server nodes are not supported yet"):
        emu.render()


def test_frr_backend_renders_frr_config_for_selected_router():
    emu = Emulator()
    base = Base()
    routing = Routing()
    ospf = Ospf()
    ibgp = Ibgp()
    ebgp = Ebgp()

    base.createInternetExchange(100)
    base.createInternetExchange(101)

    as2 = base.createAutonomousSystem(2)
    as2.createNetwork("net0")
    as2.createRouter("r1").joinNetwork("net0").joinNetwork("ix100")
    as2.createRouter("r2", routingBackend="frr").joinNetwork("net0").joinNetwork("ix101")

    as151 = base.createAutonomousSystem(151)
    as151.createNetwork("net0")
    as151.createRouter("router0").joinNetwork("net0").joinNetwork("ix100")

    as152 = base.createAutonomousSystem(152)
    as152.createNetwork("net0")
    as152.createRouter("router0").joinNetwork("net0").joinNetwork("ix101")

    ebgp.addPrivatePeering(100, 2, 151, abRelationship=PeerRelationship.Provider)
    ebgp.addPrivatePeering(101, 2, 152, abRelationship=PeerRelationship.Provider)

    emu.addLayer(base)
    emu.addLayer(routing)
    emu.addLayer(ospf)
    emu.addLayer(ibgp)
    emu.addLayer(ebgp)
    emu.render()

    r1 = emu.getRegistry().get("2", "rnode", "r1")
    r2 = emu.getRegistry().get("2", "rnode", "r2")

    assert any(cmd == "bird -d" for cmd, _ in r1.getStartCommands())
    assert not any(cmd == "bird -d" for cmd, _ in r2.getStartCommands())
    assert _file_content(r2, "/etc/bird/bird.conf") == ""

    r1_bird_conf = _file_content(r1, "/etc/bird/bird.conf")
    assert r1_bird_conf.index("ipv4 table t_ospf") < r1_bird_conf.index("protocol bgp ibgp1")

    frr_conf = _file_content(r2, "/etc/frr/frr.conf")
    assert "router bgp 2" in frr_conf
    assert "neighbor 10.101.0.152 remote-as 152" in frr_conf
    assert "RM_CONNECTED_TO_BGP" in frr_conf
    assert "LC_LOCAL_OR_CUSTOMER" in frr_conf
    assert "router ospf" in frr_conf
    assert "interface net0" in frr_conf


def test_duplicate_bgp_session_names_are_preserved_with_unique_render_names():
    emu = Emulator()
    base = Base()
    routing = Routing()
    ebgp = Ebgp()

    base.createInternetExchange(100)
    base.createInternetExchange(101)

    as2 = base.createAutonomousSystem(2)
    as2.createRouter("router0").joinNetwork("ix100").joinNetwork("ix101")

    as151 = base.createAutonomousSystem(151)
    as151.createRouter("router0").joinNetwork("ix100").joinNetwork("ix101")

    ebgp.addPrivatePeering(100, 2, 151, abRelationship=PeerRelationship.Peer)
    ebgp.addPrivatePeering(101, 2, 151, abRelationship=PeerRelationship.Peer)

    emu.addLayer(base)
    emu.addLayer(routing)
    emu.addLayer(ebgp)
    emu.render()

    router = emu.getRegistry().get("2", "rnode", "router0")
    sessions = get_bgp_sessions(router)
    assert len([session for session in sessions if session["name"] == "p_as151"]) == 2
    assert len({session["render_name"] for session in sessions}) == len(sessions)

    bird_conf = _file_content(router, "/etc/bird/bird.conf")
    assert bird_conf.count("neighbor 10.100.0.151 as 151") == 1
    assert bird_conf.count("neighbor 10.101.0.151 as 151") == 1
    assert bird_conf.count("protocol bgp p_as151") == 2


def test_route_reflector_intent_renders_without_direct_ibgp_bird_writes():
    emu = Emulator()
    base = Base()
    routing = Routing()
    ospf = Ospf()
    ibgp = Ibgp()

    as2 = base.createAutonomousSystem(2)
    as2.createNetwork("net0")
    as2.createBgpCluster("10.2.0.1")
    as2.createRouter("rr").joinNetwork("net0").joinBgpCluster("10.2.0.1").makeRouteReflector()
    as2.createRouter("client").joinNetwork("net0").joinBgpCluster("10.2.0.1")

    emu.addLayer(base)
    emu.addLayer(routing)
    emu.addLayer(ospf)
    emu.addLayer(ibgp)
    emu.render()

    rr = emu.getRegistry().get("2", "rnode", "rr")
    client = emu.getRegistry().get("2", "rnode", "client")

    rr_conf = _file_content(rr, "/etc/bird/bird.conf")
    client_conf = _file_content(client, "/etc/bird/bird.conf")

    assert "protocol bgp Ibgp_rr_client_client" in rr_conf
    assert "passive yes" in rr_conf
    assert "rr client" in rr_conf
    assert "rr cluster id 10.2.0.1" in rr_conf
    assert "protocol bgp Ibgp_rr_rr" in client_conf
    assert "next hop self" in client_conf
    assert rr_conf.index("ipv4 table t_ospf") < rr_conf.index("protocol bgp Ibgp_rr_client_client")


def test_frr_route_reflector_renders_cluster_id_and_client():
    emu = Emulator()
    base = Base()
    routing = Routing()
    ospf = Ospf()
    ibgp = Ibgp()

    as2 = base.createAutonomousSystem(2)
    as2.createNetwork("net0")
    as2.createBgpCluster("10.2.0.1")
    as2.createRouter("rr", routingBackend="frr").joinNetwork("net0").joinBgpCluster("10.2.0.1").makeRouteReflector()
    as2.createRouter("client").joinNetwork("net0").joinBgpCluster("10.2.0.1")

    emu.addLayer(base)
    emu.addLayer(routing)
    emu.addLayer(ospf)
    emu.addLayer(ibgp)
    emu.render()

    rr = emu.getRegistry().get("2", "rnode", "rr")
    frr_conf = _file_content(rr, "/etc/frr/frr.conf")

    assert "router bgp 2" in frr_conf
    assert " bgp cluster-id 10.2.0.1" in frr_conf
    assert "neighbor 10.0.0.2 route-reflector-client" in frr_conf
    assert "neighbor 10.0.0.2 passive" in frr_conf


def test_mpls_masks_ospf_and_ibgp_before_intent_is_recorded():
    emu = Emulator()
    base = Base()
    routing = Routing()
    ebgp = Ebgp()
    ibgp = Ibgp()
    ospf = Ospf()
    mpls = Mpls()

    base.createInternetExchange(100)
    base.createInternetExchange(101)

    as2 = base.createAutonomousSystem(2)
    as2.createNetwork("net0")
    as2.createNetwork("net1")
    as2.createNetwork("net2")
    as2.createRouter("r1").joinNetwork("net0").joinNetwork("ix100")
    as2.createRouter("r2").joinNetwork("net0").joinNetwork("net1")
    as2.createRouter("r3").joinNetwork("net1").joinNetwork("net2")
    as2.createRouter("r4").joinNetwork("net2").joinNetwork("ix101")
    mpls.enableOn(2)

    as151 = base.createAutonomousSystem(151)
    as151.createNetwork("net0")
    as151.createRouter("router0").joinNetwork("net0").joinNetwork("ix100")

    as152 = base.createAutonomousSystem(152)
    as152.createNetwork("net0")
    as152.createRouter("router0").joinNetwork("net0").joinNetwork("ix101")

    ebgp.addPrivatePeering(100, 2, 151, abRelationship=PeerRelationship.Provider)
    ebgp.addPrivatePeering(101, 2, 152, abRelationship=PeerRelationship.Provider)

    emu.addLayer(base)
    emu.addLayer(routing)
    emu.addLayer(ebgp)
    emu.addLayer(ibgp)
    emu.addLayer(ospf)
    emu.addLayer(mpls)
    emu.render()

    r1 = emu.getRegistry().get("2", "rnode", "r1")
    r2 = emu.getRegistry().get("2", "rnode", "r2")
    r4 = emu.getRegistry().get("2", "rnode", "r4")

    assert get_ospf_interface_intents(r1) == {"active": [], "passive": []}
    assert [session for session in get_bgp_sessions(r1) if session["kind"] == "ibgp"] == []
    assert get_bgp_sessions(r2) == []

    r1_conf = _file_content(r1, "/etc/bird/bird.conf")
    r2_conf = _file_content(r2, "/etc/bird/bird.conf")
    r4_conf = _file_content(r4, "/etc/bird/bird.conf")
    assert "protocol ospf ospf1" not in r1_conf
    assert "protocol ospf ospf1" not in r2_conf
    assert "protocol bgp ibgp" not in r2_conf
    assert "protocol bgp ibgp1" in r1_conf
    assert "protocol bgp ibgp1" in r4_conf


def test_exabgp_service_renders_speaker_and_router_peer():
    emu = Emulator()
    base = Base()
    routing = Routing()
    exabgp = ExaBgpService()

    base.createInternetExchange(100)

    as2 = base.createAutonomousSystem(2)
    as2.createNetwork("net0")
    as2.createRouter("router0").joinNetwork("net0").joinNetwork("ix100")

    as180 = base.createAutonomousSystem(180)
    as180.createHost("exabgp").joinNetwork("ix100", address="10.100.0.180")

    exabgp.install("as180_exabgp") \
        .setLocalAsn(180) \
        .addPeer("router0", router_asn=2, router_relationship="customer") \
        .addAnnouncement("198.51.100.0/24")
    emu.addBinding(Binding("as180_exabgp", filter=Filter(asn=180, nodeName="exabgp")))

    emu.addLayer(base)
    emu.addLayer(routing)
    emu.addLayer(exabgp)
    emu.render()

    speaker = emu.getRegistry().get("180", "hnode", "exabgp")
    router = emu.getRegistry().get("2", "rnode", "router0")

    exabgp_conf = _file_content(speaker, "/etc/exabgp/exabgp.conf")
    assert "neighbor 10.100.0.2" in exabgp_conf
    assert "local-as 180" in exabgp_conf
    assert "peer-as 2" in exabgp_conf
    assert "198.51.100.0/24" in exabgp_conf

    bird_conf = _file_content(router, "/etc/bird/bird.conf")
    assert "protocol bgp exabgp_180" in bird_conf
    assert "neighbor 10.100.0.180 as 180" in bird_conf
    assert "bgp_large_community.add(CUSTOMER_COMM)" in bird_conf
    assert "bgp_local_pref = 30" in bird_conf


def test_exabgp_service_renders_frr_router_peer_without_bird():
    emu = Emulator()
    base = Base()
    routing = Routing()
    exabgp = ExaBgpService()

    base.createInternetExchange(100)

    as2 = base.createAutonomousSystem(2)
    as2.createNetwork("net0")
    as2.createRouter("router0", routingBackend="frr").joinNetwork("net0").joinNetwork("ix100")

    as180 = base.createAutonomousSystem(180)
    as180.createHost("exabgp").joinNetwork("ix100", address="10.100.0.180")

    exabgp.install("as180_exabgp") \
        .setLocalAsn(180) \
        .addPeer("router0", router_asn=2, router_relationship="customer") \
        .addAnnouncement("198.51.100.0/24")
    emu.addBinding(Binding("as180_exabgp", filter=Filter(asn=180, nodeName="exabgp")))

    emu.addLayer(base)
    emu.addLayer(routing)
    emu.addLayer(exabgp)
    emu.render()

    speaker = emu.getRegistry().get("180", "hnode", "exabgp")
    router = emu.getRegistry().get("2", "rnode", "router0")

    assert _file_content(router, "/etc/bird/bird.conf") == ""
    frr_conf = _file_content(router, "/etc/frr/frr.conf")
    assert "router bgp 2" in frr_conf
    assert "neighbor 10.100.0.180 remote-as 180" in frr_conf
    assert "neighbor 10.100.0.180 description exabgp_180" in frr_conf
    assert "neighbor 10.100.0.180 next-hop-self" in frr_conf

    exabgp_conf = _file_content(speaker, "/etc/exabgp/exabgp.conf")
    assert "peer-as 2" in exabgp_conf
    assert "198.51.100.0/24" in exabgp_conf
