from __future__ import annotations
from seedemu.core import Registry, ScopedRegistry, Network, Interface, Graphable, Emulator, Layer, Router
from seedemu.core.enums import NodeRole
from typing import Tuple, List, Dict
from enum import Enum
from ._bgp_metadata import (
    BGP_COMMUNITY_CUSTOMER,
    BGP_COMMUNITY_PEER,
    BGP_COMMUNITY_PROVIDER,
    BGP_EXPORT_ALL,
    BGP_EXPORT_LOCAL_AND_CUSTOMER,
    install_router_bgp_session,
)

EbgpFileTemplates: Dict[str, str] = {}

class PeerRelationship(Enum):
    """!
    @brief Relationship between peers.
    """

    ## Provider: a side: export everything, b side: export only customer's and
    ## own prefixes
    Provider = "Provider"

    ## Peer: a side & b side: only export customer's and own prefixes.
    Peer = "Peer"

    ## Unfiltered: no filter on both sides
    Unfiltered = "Unfiltered"

class Ebgp(Layer, Graphable):
    """!
    @brief The Ebgp (eBGP) layer.

    This layer enable eBGP peering in InternetExchange.
    """

    __peerings: Dict[Tuple[int, int, int], PeerRelationship]
    __rs_peers: List[Tuple[int, int]]
    __xc_peerings: Dict[Tuple[int, int], PeerRelationship]

    def __init__(self):
        """!
        @brief Ebgp layer constructor.
        """
        super().__init__()
        self.__peerings = {}
        self.__xc_peerings = {}
        self.__rs_peers = []
        self.addDependency('Routing', False, False)

    def __recordPeer(
        self,
        node: Router,
        name: str,
        localAddress: str,
        localAsn: int,
        peerAddress: str,
        peerAsn: int,
        importCommunity: str = None,
        bgpPref: int = None,
        exportPolicy: str = BGP_EXPORT_ALL,
        nextHopSelf: bool = True,
        routeServerClient: bool = False,
    ) -> None:
        install_router_bgp_session(
            node,
            {
                "name": name,
                "kind": "ebgp",
                "local_address": str(localAddress),
                "local_asn": localAsn,
                "peer_address": str(peerAddress),
                "peer_asn": peerAsn,
                "import_community": importCommunity,
                "local_pref": bgpPref,
                "export_policy": exportPolicy,
                "next_hop_self": nextHopSelf,
                "route_server_client": routeServerClient,
            },
        )

    def __createPeer(self, nodeA: Router, nodeB: Router, addrA: str, addrB: str, rel: PeerRelationship) -> None:

        rsNode: Router = None
        routerA: Router = None
        routerB: Router = None

        # The Ebgp layer records BGP intent only. Routing decides later whether
        # those sessions become BIRD protocol blocks or FRR configuration.
        for node in [nodeA, nodeB]:
            if node.getRegistryInfo()[1] == 'rs':
                # getRole() would be BorderRouter not RouteServer here
                rsNode = node
                continue

            if routerA == None: routerA = node
            elif routerB == None: routerB = node

        assert routerA != None, 'both nodes are RS node. cannot setup peering.'
        assert routerA != routerB, 'cannot peer with oneself.'

        if rsNode != None:
            self.__recordPeer(
                rsNode,
                'p_as{}'.format(routerA.getAsn()),
                addrA,
                rsNode.getAsn(),
                addrB,
                routerA.getAsn(),
                nextHopSelf=False,
                routeServerClient=True,
            )

            self.__recordPeer(
                routerA,
                'p_rs{}'.format(rsNode.getAsn()),
                addrB,
                routerA.getAsn(),
                addrA,
                rsNode.getAsn(),
                importCommunity=BGP_COMMUNITY_PEER,
                bgpPref=20,
                exportPolicy=BGP_EXPORT_LOCAL_AND_CUSTOMER,
            )

            return

        if rel == PeerRelationship.Peer:
            self.__recordPeer(routerA, 'p_as{}'.format(routerB.getAsn()), addrA, routerA.getAsn(), addrB, routerB.getAsn(), BGP_COMMUNITY_PEER, 20, BGP_EXPORT_LOCAL_AND_CUSTOMER)
            self.__recordPeer(routerB, 'p_as{}'.format(routerA.getAsn()), addrB, routerB.getAsn(), addrA, routerA.getAsn(), BGP_COMMUNITY_PEER, 20, BGP_EXPORT_LOCAL_AND_CUSTOMER)

        if rel == PeerRelationship.Provider:
            self.__recordPeer(routerA, 'c_as{}'.format(routerB.getAsn()), addrA, routerA.getAsn(), addrB, routerB.getAsn(), BGP_COMMUNITY_CUSTOMER, 30, BGP_EXPORT_ALL)
            self.__recordPeer(routerB, 'u_as{}'.format(routerA.getAsn()), addrB, routerB.getAsn(), addrA, routerA.getAsn(), BGP_COMMUNITY_PROVIDER, 10, BGP_EXPORT_LOCAL_AND_CUSTOMER)

        if rel == PeerRelationship.Unfiltered:
            self.__recordPeer(routerA, 'x_as{}'.format(routerB.getAsn()), addrA, routerA.getAsn(), addrB, routerB.getAsn(), BGP_COMMUNITY_CUSTOMER, 30, BGP_EXPORT_ALL)
            self.__recordPeer(routerB, 'x_as{}'.format(routerA.getAsn()), addrB, routerB.getAsn(), addrA, routerA.getAsn(), BGP_COMMUNITY_PROVIDER, 10, BGP_EXPORT_ALL)

    def getName(self) -> str:
        return "Ebgp"

    def addPrivatePeering(self, ix: int, a: int, b: int, abRelationship: PeerRelationship = PeerRelationship.Peer) -> Ebgp:
        """!
        @brief Setup private peering between two ASes in IX.

        @param ix IXP id.
        @param a First ASN.
        @param b Second ASN.
        @param abRelationship (optional) A and B's relationship. If set to
        PeerRelationship.Provider, A will export everything to B, if set to
        PeerRelationship.Peer, A will only export own and customer prefixes to
        B. Default to Peer.

        @throws AssertionError if peering already exist.

        @returns self, for chaining API calls.
        """
        assert (ix, a, b) not in self.__peerings, '{} <-> {} already peered at IX{}'.format(a, b, ix)
        assert (ix, b, a) not in self.__peerings, '{} <-> {} already peered at IX{}'.format(b, a, ix)
        assert abRelationship == PeerRelationship.Peer or abRelationship == PeerRelationship.Provider or abRelationship == PeerRelationship.Unfiltered, 'unknown peering relationship {}'.format(abRelationship)

        self.__peerings[(ix, a, b)] = abRelationship

        return self

    def addPrivatePeerings(self, ix: int, a_asns: List[int], b_asns: List[int], abRelationship: PeerRelationship = PeerRelationship.Peer) -> Ebgp:
        """!
        @brief Setup private peering between two sets of ASes in IX.

        @param ix IXP id.
        @param a_asns First set of ASNs.
        @param b_asns Second set of ASNs.
        @param abRelationship (optional) A and B's relationship. If set to
        PeerRelationship.Provider, A will export everything to B, if set to
        PeerRelationship.Peer, A will only export own and customer prefixes to
        B. Default to Peer.

        @throws AssertionError if peering already exist.

        @returns self, for chaining API calls.
        """
        for a in a_asns:
            for b in b_asns:
                self.addPrivatePeering(ix, a, b, abRelationship)

        return self

    def getPrivatePeerings(self) -> Dict[Tuple[int, int, int], PeerRelationship]:
        """!
        @brief Get private peerings.

        @returns dict, where key is tuple of (ix, asnA, asnB) and value is peering relationship.
        """
        return self.__peerings

    def addCrossConnectPeering(self, a: int, b: int, abRelationship: PeerRelationship = PeerRelationship.Peer) -> Ebgp:
        """!
        @brief add cross-connect peering.

        @param a First ASN.
        @param b Second ASN.
        @param abRelationship (optional) A and B's relationship. If set to
        PeerRelationship.Provider, A will export everything to B, if set to
        PeerRelationship.Peer, A will only export own and customer prefixes to
        B. Default to Peer.

        @throws AssertionError if peering already exist.

        @returns self, for chaining API calls.
        """
        assert (a, b) not in self.__xc_peerings, '{} <-> {} already configured as XC peer'.format(a, b)
        assert (b, a) not in self.__xc_peerings, '{} <-> {} already configured as XC peer'.format(b, a)
        assert abRelationship == PeerRelationship.Peer or abRelationship == PeerRelationship.Provider or abRelationship == PeerRelationship.Unfiltered, 'unknown peering relationship {}'.format(abRelationship)

        self.__xc_peerings[(a, b)] = abRelationship

        return self

    def getCrossConnectPeerings(self) -> Dict[Tuple[int, int], PeerRelationship]:
        """!
        @brief get cross-connect peerings.

        @returns dict,  where key is tuple of (asnA, asnB) and value is peering relationship.
        """
        return self.__xc_peerings

    def addRsPeer(self, ix: int, peer: int) -> Ebgp:
        """!
        @brief Setup RS peering for an AS.

        @param ix IXP id.
        @param peer Participant ASN.

        @throws AssertionError if peering already exist.

        @returns self, for chaining API calls.
        """
        assert (ix, peer) not in self.__rs_peers, '{} already peered with RS at IX{}'.format(peer, ix)

        self.__rs_peers.append((ix, peer))

        return self

    def addRsPeers(self, ix: int, peers: List[int]):
        """!
        @brief Setup RS peering for list of ASes.

        @param ix IXP id.
        @param peers List of participant ASNs.

        @throws AssertionError if some peering already exist.

        @returns self, for chaining API calls.
        """
        for peer in peers:
            self.addRsPeer(ix, peer)

        return self

    def getRsPeers(self) -> List[Tuple[int, int]]:
        """!
        @brief Get RS peers.

        @returns list of tuple of (ix, peerAsn)
        """
        return self.__rs_peers

    def configure(self, emulator: Emulator) -> None:
        reg = emulator.getRegistry()

        for (ix, peer) in self.__rs_peers:
            ix_reg = ScopedRegistry('ix', reg)
            p_reg = ScopedRegistry(str(peer), reg)

            ix_net: Network = ix_reg.get('net', 'ix{}'.format(ix))
            ix_rs: Router = ix_reg.get('rs', 'ix{}'.format(ix))
            rs_ifs = ix_rs.getInterfaces()
            assert len(rs_ifs) == 1, '??? ix{} rs has {} interfaces.'.format(ix, len(rs_ifs))
            rs_if = rs_ifs[0]

            p_rnodes: List[Router] = p_reg.getByType('brdnode')
            p_ixnode: Router = None
            p_ixif: Interface = None
            for node in p_rnodes:
                if p_ixnode != None: break
                for iface in node.getInterfaces():
                    if iface.getNet() == ix_net:
                        p_ixnode = node
                        p_ixif = iface
                        break

            assert p_ixnode != None, 'cannot resolve peering: as{} not in ix{}'.format(peer, ix)
            self._log("adding peering: {} as {} (RS) <-> {} as {}".format(rs_if.getAddress(), ix, p_ixif.getAddress(), peer))

            self.__createPeer(ix_rs, p_ixnode, rs_if.getAddress(), p_ixif.getAddress(), PeerRelationship.Peer)

        for (a, b), rel in self.__xc_peerings.items():
            a_reg = ScopedRegistry(str(a), reg)
            b_reg = ScopedRegistry(str(b), reg)

            a_router: Router = None
            b_router: Router = None

            a_addr: str = None
            b_addr: str = None

            hit = False

            for node in a_reg.getByType('brdnode'):
                router: Router = node
                for (peername, peerasn), (localaddr, _, _) in router.getCrossConnects().items():
                    if peerasn != b: continue
                    if not b_reg.has('brdnode', peername): continue

                    hit = True
                    a_router = node
                    b_router = b_reg.get('brdnode', peername)

                    a_addr = str(localaddr.ip)
                    (b_ifaddr, _, _) = b_router.getCrossConnect(a, a_router.getName())
                    b_addr = str(b_ifaddr.ip)

                    break
                if hit: break

            assert hit, 'cannot find XC to configure peer AS{} <--> AS{}'.format(a, b)

            self._log("adding XC peering: {} as {} <-({})-> {} as {}".format(a_addr, a, rel, b_addr, b))

            self.__createPeer(a_router, b_router, a_addr, b_addr, rel)

        for (ix, a, b), rel in self.__peerings.items():
            ix_reg = ScopedRegistry('ix', reg)
            a_reg = ScopedRegistry(str(a), reg)
            b_reg = ScopedRegistry(str(b), reg)

            ix_net: Network = ix_reg.get('net', 'ix{}'.format(ix))
            a_rnodes: List[Router] = a_reg.getByType('rnode')
            b_rnodes: List[Router] = b_reg.getByType('rnode')

            a_ixnode: Router = None
            a_ixif: Interface = None
            for node in a_rnodes:
                if a_ixnode != None: break
                for iface in node.getInterfaces():
                    if iface.getNet() == ix_net:
                        a_ixnode = node
                        a_ixif = iface
                        break

            assert a_ixnode != None, 'cannot resolve peering: as{} not in ix{}'.format(a, ix)

            b_ixnode: Router = None
            b_ixif: Interface = None
            for node in b_rnodes:
                if b_ixnode != None: break
                for iface in node.getInterfaces():
                    if iface.getNet() == ix_net:
                        b_ixnode = node
                        b_ixif = iface
                        break

            assert b_ixnode != None, 'cannot resolve peering: as{} not in ix{}'.format(b, ix)

            self._log("adding IX peering: {} as {} <-({})-> {} as {}".format(a_ixif.getAddress(), a, rel, b_ixif.getAddress(), b))

            self.__createPeer(a_ixnode, b_ixnode, a_ixif.getAddress(), b_ixif.getAddress(), rel)

    def render(self, emulator: Emulator) -> None:
        pass

    def _doCreateGraphs(self, emulator: Emulator):
        # creates the following:
        # - ebgp peering, all ASes in one graph
        # - ebgp peering, one for each ix
        # mlpa peer (i.e., via rs): dashed line
        # private peer: solid line

        full_graph = self._addGraph('All Peering Sessions', False)

        ix_list = set()
        for (i, _) in self.__rs_peers: ix_list.add(i)
        for (i, _, _), _ in self.__peerings.items(): ix_list.add(i)
        for ix in ix_list:
            self._log('Creating RS peering sessions graph for IX{}...'.format(ix))
            ix_graph = self._addGraph('IX{} Peering Sessions'.format(ix), False)

            mesh_ases = set()

            for (i, a) in self.__rs_peers:
                if i == ix: mesh_ases.add(a)

            self._log('IX{} RS-mesh: {}'.format(ix, mesh_ases))

            while len(mesh_ases) > 0:
                a = mesh_ases.pop()
                if not full_graph.hasVertex('AS{}'.format(a), 'IX{}'.format(ix)):
                    full_graph.addVertex('AS{}'.format(a), 'IX{}'.format(ix))
                if not ix_graph.hasVertex('AS{}'.format(a), 'IX{}'.format(ix)):
                    ix_graph.addVertex('AS{}'.format(a), 'IX{}'.format(ix))
                for b in mesh_ases:
                    if not full_graph.hasVertex('AS{}'.format(b), 'IX{}'.format(ix)):
                        full_graph.addVertex('AS{}'.format(b), 'IX{}'.format(ix))
                    if not ix_graph.hasVertex('AS{}'.format(b), 'IX{}'.format(ix)):
                        ix_graph.addVertex('AS{}'.format(b), 'IX{}'.format(ix))

                    full_graph.addEdge('AS{}'.format(a), 'AS{}'.format(b), 'IX{}'.format(ix), 'IX{}'.format(ix), style = 'dashed', alabel = 'R', blabel= 'R')
                    ix_graph.addEdge('AS{}'.format(a), 'AS{}'.format(b), 'IX{}'.format(ix), 'IX{}'.format(ix), style = 'dashed', alabel = 'R', blabel= 'R')

        for (i, a, b), rel in self.__peerings.items():
            self._log('Creating private peering sessions graph for IX{} AS{} <-> AS{}...'.format(i, a, b))

            ix_graph = self._addGraph('IX{} Peering Sessions'.format(i), False)

            if not full_graph.hasVertex('AS{}'.format(a), 'IX{}'.format(i)):
                full_graph.addVertex('AS{}'.format(a), 'IX{}'.format(i))
            if not ix_graph.hasVertex('AS{}'.format(a), 'IX{}'.format(i)):
                ix_graph.addVertex('AS{}'.format(a), 'IX{}'.format(i))

            if not full_graph.hasVertex('AS{}'.format(b), 'IX{}'.format(i)):
                full_graph.addVertex('AS{}'.format(b), 'IX{}'.format(i))
            if not ix_graph.hasVertex('AS{}'.format(b), 'IX{}'.format(i)):
                ix_graph.addVertex('AS{}'.format(b), 'IX{}'.format(i))

            if rel == PeerRelationship.Peer:
                full_graph.addEdge('AS{}'.format(a), 'AS{}'.format(b), 'IX{}'.format(i), 'IX{}'.format(i), alabel = 'P', blabel= 'P')
                ix_graph.addEdge('AS{}'.format(a), 'AS{}'.format(b), 'IX{}'.format(i), 'IX{}'.format(i), alabel = 'P', blabel= 'P')

            if rel == PeerRelationship.Provider:
                full_graph.addEdge('AS{}'.format(a), 'AS{}'.format(b), 'IX{}'.format(i), 'IX{}'.format(i), alabel = 'U', blabel = 'C')
                ix_graph.addEdge('AS{}'.format(a), 'AS{}'.format(b), 'IX{}'.format(i), 'IX{}'.format(i), alabel = 'U', blabel = 'C')

            if rel == PeerRelationship.Unfiltered:
                full_graph.addEdge('AS{}'.format(a), 'AS{}'.format(b), 'IX{}'.format(i), 'IX{}'.format(i), alabel = 'X', blabel= 'X')
                ix_graph.addEdge('AS{}'.format(a), 'AS{}'.format(b), 'IX{}'.format(i), 'IX{}'.format(i), alabel = 'X', blabel= 'X')

        # todo: XC peering graphs

        es = list(full_graph.vertices.values())
        while len(es) > 0:
            a = es.pop()
            for b in es:
                if a.name == b.name:
                    full_graph.addEdge(a.name, b.name, a.group, b.group, style = 'dotted', alabel = 'I', blabel= 'I')


    def print(self, indent: int) -> str:
        out = ' ' * indent
        out += 'EbgpLayer:\n'

        indent += 4
        for (i, a) in self.__rs_peers:
            out += ' ' * indent
            out += 'IX{}: RS <-> AS{}\n'.format(i, a)

        for (i, a, b), rel in self.__peerings.items():
            out += ' ' * indent
            out += 'IX{}: AS{} <--({})--> AS{}\n'.format(i, a, rel, b)


        return out
