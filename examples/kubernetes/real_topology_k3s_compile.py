#!/usr/bin/env python3
# encoding: utf-8

from __future__ import annotations

import json
import os
import sys
import re
import pickle
import time
from ipaddress import IPv4Network
from typing import List, Tuple, Dict, Any

import networkx as nx

# Always prefer the repo-local seedemu package for this compile entrypoint.
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from seedemu.compiler import KubernetesCompiler, SchedulingStrategy
from seedemu.core import Binding, Filter, Emulator, Router, AutonomousSystem
from seedemu.layers import Base, Routing, Ebgp, Ibgp, Ospf, PeerRelationship, EtcHosts
from seedemu.services import TrafficService, TrafficServiceType


###############################################################################
# 数据加载
###############################################################################

def load_assignment(path: str) -> Dict[str, Dict[str, Any]]:
    with open(path, "rb") as f:
        data = pickle.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"assignment.pkl must be dict, got {type(data)}")
    return data


def load_topology_data(filename: str) -> dict:
    """从文件加载拓扑数据（兼容 key: value 格式）"""
    with open(filename, "r", encoding="utf-8") as f:
        content = f.read().strip()

    content = re.sub(r'^(\w+):', r'"\1":', content, flags=re.MULTILINE)
    lines = content.split("\n")
    lines = [line + "," for line in lines[:-1]] + [lines[-1]] if lines else []
    content = "\n".join(lines)
    content = "{" + content + "}"

    try:
        return eval(content)
    except Exception as e:
        raise ValueError(f"解析拓扑数据失败: {e}")


###############################################################################
# 环境变量
###############################################################################

def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_node_labels_json(raw: str) -> Dict[str, Dict[str, str]]:
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("SEED_NODE_LABELS_JSON must be a JSON object")
    normalized: Dict[str, Dict[str, str]] = {}
    for key, value in data.items():
        if not isinstance(value, dict):
            raise ValueError(f"SEED_NODE_LABELS_JSON['{key}'] must be an object")
        normalized[str(key)] = {str(k): str(v) for k, v in value.items()}
    return normalized


###############################################################################
# 你原脚本里的辅助函数
###############################################################################

def add_traffic(base, emu, stubAS, assignment_temp, num=10):
    etc_hosts = EtcHosts()
    traffic_service = TrafficService()

    for i in range(num):
        asnum1 = assignment_temp[stubAS[3 * i]]["asn"]
        asnum2 = assignment_temp[stubAS[3 * i + 1]]["asn"]
        asnum3 = assignment_temp[stubAS[3 * i + 2]]["asn"]

        receiver1 = f"iperf-receiver-{i}-1"
        receiver2 = f"iperf-receiver-{i}-2"

        traffic_service.install(
            receiver1,
            TrafficServiceType.IPERF_RECEIVER,
            log_file="/root/iperf3_receiver.log",
        )
        traffic_service.install(
            receiver2,
            TrafficServiceType.IPERF_RECEIVER,
            log_file="/root/iperf3_receiver.log",
        )
        traffic_service.install(
            f"iperf-generator-{i}",
            TrafficServiceType.IPERF_GENERATOR,
            log_file="/root/iperf3_generator.log",
            protocol="TCP",
            duration=36000,
            rate=0,
        ).addReceivers(hosts=[receiver1, receiver2])

        as1 = base.getAutonomousSystem(asnum1)
        as1.createHost(f"iperf-generator-{i}").joinNetwork("net0")

        as2 = base.getAutonomousSystem(asnum2)
        as2.createHost(f"iperf-receiver-{i}-1").joinNetwork("net0")

        as3 = base.getAutonomousSystem(asnum3)
        as3.createHost(f"iperf-receiver-{i}-2").joinNetwork("net0")

        emu.addBinding(
            Binding(f"iperf-generator-{i}", filter=Filter(asn=asnum1, nodeName=f"iperf-generator-{i}"))
        )
        emu.addBinding(
            Binding(receiver1, filter=Filter(asn=asnum2, nodeName=receiver1))
        )
        emu.addBinding(
            Binding(receiver2, filter=Filter(asn=asnum3, nodeName=receiver2))
        )

    emu.addLayer(traffic_service)
    emu.addLayer(etc_hosts)


def get_assignment(asn, assignment):
    t = len(assignment)
    if asn not in assignment.keys():
        assignment[asn] = {
            "asn": t + 1,
            "ipv4": f"{(t + 1) // 256}.{(t + 1) % 256}.0.0/16",
        }
    return assignment


def generate_connected_pairs(nodes, extra_edges_count=0):
    if len(nodes) < 2:
        return []
    elif len(nodes) == 2:
        return [tuple(sorted((nodes[0], nodes[1])))]

    pairs = set()
    for i in range(len(nodes)):
        pair1 = tuple(sorted((nodes[i], nodes[(i + 1) % len(nodes)])))
        pairs.add(pair1)

    for i in range(len(nodes)):
        if len(pairs) < 254:
            break
        j = int(i + 1 + len(nodes) / 2) % len(nodes)
        if i != j:
            pair2 = tuple(sorted((nodes[i], nodes[j])))
            pairs.add(pair2)

    return list(pairs)


def makeStubAsWithHosts(
    emu: Emulator,
    base: Base,
    asn: int,
    prefix: str,
    exchange: int,
    hosts_total: int,
    node_prefix: Dict[int, str],
):
    network = "net0"

    stub_as = base.createAutonomousSystem(asn)
    stub_as.createNetwork(network, prefix)

    router = stub_as.createRouter(f"r{exchange}")
    router.joinNetwork(network)
    router.joinNetwork(f"ix{exchange}", str(IPv4Network(node_prefix[exchange])[asn]))

    for counter in range(hosts_total):
        name = f"host_{counter}"
        host = stub_as.createHost(name)
        host.joinNetwork(network)


def makeTransitAs(
    base: Base,
    asn: int,
    prefix: str,
    exchanges: List[int],
    intra_ix_links: List[Tuple[int, int]],
    node_prefix: Dict[int, str],
    rrNum=0,
) -> AutonomousSystem:
    transit_as = base.createAutonomousSystem(asn)
    routers: Dict[int, Router] = {}

    for ix in exchanges:
        routers[ix] = transit_as.createRouter(f"r{ix}")
        routers[ix].joinNetwork(f"ix{ix}", str(IPv4Network(node_prefix[ix])[asn]))

    if rrNum == 1:
        rr_index = list(routers.keys())[0]
        routers[rr_index].makeRouteReflector(True)
    elif rrNum > 1:
        for i in range(rrNum):
            transit_as.createCluster(f"10.0.0.{i+1}")
        index = 0
        for _, value in routers.items():
            if index < rrNum:
                value.makeRouteReflector(True)
                value.joinBgpCluster(f"10.0.0.{index+1}")
                index += 1
            else:
                value.joinBgpCluster(f"10.0.0.{index % rrNum + 1}")
                index += 1

    subnets = list(IPv4Network(prefix).subnets(prefixlen_diff=8))
    i = 1
    for (a, b) in intra_ix_links:
        assert i < 255, "net >= 255"
        name = f"net_{a}_{b}"
        transit_as.createNetwork(name, str(subnets[i]))
        routers[a].joinNetwork(name)
        routers[b].joinNetwork(name)
        i += 1

    return transit_as


###############################################################################
# 主逻辑
###############################################################################

def run():
    topology_dir = os.environ.get("SEED_REAL_TOPOLOGY_DIR", ".")
    topology_size_raw = os.environ.get("SEED_TOPOLOGY_SIZE", "214").strip()

    try:
        topology_size = int(topology_size_raw)
    except ValueError as exc:
        raise ValueError(f"Invalid SEED_TOPOLOGY_SIZE: {topology_size_raw}") from exc

    topology_file = os.environ.get("SEED_TOPOLOGY_FILE")
    if not topology_file:
        topology_file = os.path.join(topology_dir, f"real_topology_{topology_size}.txt")

    assignment_file = os.environ.get("SEED_ASSIGNMENT_FILE")
    if not assignment_file:
        assignment_file = os.path.join(topology_dir, "assignment.pkl")

    if not os.path.isfile(topology_file):
        raise FileNotFoundError(f"未找到拓扑文件: {topology_file}")
    if not os.path.isfile(assignment_file):
        raise FileNotFoundError(f"未找到 assignment 文件: {assignment_file}")

    assignment = load_assignment(assignment_file)
    TOPOLOGY_DATA = load_topology_data(topology_file)

    node_prefix: Dict[int, str] = {}
    for _, value in assignment.items():
        node_prefix[int(value["asn"])] = str(value["ipv4"])

    emu = Emulator()
    ebgp = Ebgp()
    base = Base()

    t = time.time()

    ###############################################################################
    # 创建 IXP
    ###############################################################################
    for ixp in TOPOLOGY_DATA["ixps"]:
        prefix = assignment[ixp]["ipv4"]
        ix = assignment[ixp]["asn"]
        address = str(IPv4Network(prefix)[ix])
        ix_obj = base.createInternetExchange(ix, prefix, rsAddress=address)
        ix_obj.getPeeringLan().setDisplayName(f"IX-{ix}")
        print(f"创建IXP: {ix}")

    ###############################################################################
    # Transit AS
    ###############################################################################
    transit_info = {}
    for asn_key in TOPOLOGY_DATA["transit_asns"]:
        asn = assignment[asn_key]["asn"]
        connected_ixs = set()
        intra_links = []
        for (ix_a, ix_b, t_asn, _) in TOPOLOGY_DATA["ix_ix_transit_edges"]:
            ix_a_real = assignment[ix_a]["asn"]
            ix_b_real = assignment[ix_b]["asn"]
            t_asn_real = assignment[t_asn]["asn"]
            if t_asn_real == asn:
                connected_ixs.add(ix_a_real)
                connected_ixs.add(ix_b_real)
                intra_links.append((ix_a_real, ix_b_real))
        transit_info[asn] = (sorted(connected_ixs), intra_links)

    rr_plan_by_as = {}

    for asnumber in TOPOLOGY_DATA["transit_asns"]:
        asn = assignment[asnumber]["asn"]
        prefix = assignment[asnumber]["ipv4"]
        exchanges, _ = transit_info[asn]

        links = generate_connected_pairs(
            sorted(exchanges),
            min(2 * len(exchanges), 253 - len(exchanges)),
        )

        makeTransitAs(
            base,
            asn,
            prefix,
            exchanges,
            list(set(links)),
            node_prefix=node_prefix,
            rrNum=1,
        )

        rr_plan_by_as[str(asn)] = {
            "asn": asn,
            "router_count": len(exchanges),
            "rr_count": 1 if len(exchanges) > 0 else 0,
            "exchanges": exchanges,
            "internal_links": [f"r{a}<->r{b}" for (a, b) in list(set(links))],
        }

        print(f"创建Transit AS{asn}: 连接IXP{exchanges}")

    ###############################################################################
    # Stub AS
    ###############################################################################
    stub_ix_map = {}
    for (provider, customer, ix, rel) in TOPOLOGY_DATA["as_as_ix_edges"]:
        if rel == "-1" and customer in TOPOLOGY_DATA["stub_asns"]:
            stub_ix_map[customer] = ix

    assignment_temp = assignment.copy()

    for stub_asn in TOPOLOGY_DATA["stub_asns"]:
        assignment_temp = get_assignment(stub_asn, assignment_temp)
        asn = assignment_temp[stub_asn]["asn"]
        prefix = assignment_temp[stub_asn]["ipv4"]
        ix = assignment_temp[stub_ix_map[stub_asn]]["asn"]

        makeStubAsWithHosts(
            emu,
            base,
            asn,
            prefix,
            ix,
            hosts_total=0,
            node_prefix=node_prefix,
        )
        print(f"创建Stub AS{asn}: 连接IXP{ix}")

    ###############################################################################
    # eBGP
    ###############################################################################
    for (a, b, ix, rel) in TOPOLOGY_DATA["as_as_ix_edges"]:
        a_real = assignment_temp[a]["asn"]
        b_real = assignment_temp[b]["asn"]
        ix_real = assignment_temp[ix]["asn"]
        rel_int = int(rel)

        if rel_int == -1:
            relationship = PeerRelationship.Provider
        elif rel_int == 0:
            relationship = PeerRelationship.Peer
        else:
            raise ValueError(f"无效关系值: {rel} (仅支持-1和0)")

        ebgp.addPrivatePeerings(ix_real, [a_real], [b_real], relationship)

    ###############################################################################
    # 添加层
    ###############################################################################
    emu.addLayer(base)
    emu.addLayer(Routing())
    emu.addLayer(ebgp)
    emu.addLayer(Ibgp())
    emu.addLayer(Ospf())

    print("开始渲染仿真网络...")
    emu.render()

    ###############################################################################
    # 改成 KubernetesCompiler，输出 k3s 产物
    ###############################################################################
    registry_prefix = os.environ.get("SEED_REGISTRY", "localhost:5001")
    namespace = os.environ.get("SEED_NAMESPACE", "seedemu")
    cni_type = os.environ.get("SEED_CNI_TYPE", "bridge").strip().lower()
    cni_master_interface = os.environ.get("SEED_CNI_MASTER_INTERFACE", "eth0").strip()
    image_pull_policy = os.environ.get("SEED_IMAGE_PULL_POLICY", "Always").strip()
    scheduling_strategy = os.environ.get(
        "SEED_SCHEDULING_STRATEGY",
        SchedulingStrategy.BY_AS_HARD,
    ).strip().lower()
    if 'SEED_NODE_LABELS_FILE' in os.environ:
        with open(os.environ['SEED_NODE_LABELS_FILE'], 'r') as f:
            node_labels = json.load(f)
    else:
        node_labels_raw = os.environ.get('SEED_NODE_LABELS_JSON', '{}')
        node_labels = json.loads(node_labels_raw)
    enable_internet_map = env_bool("SEED_ENABLE_INTERNET_MAP", default=False)

    output_dir = os.environ.get("SEED_OUTPUT_DIR", "./output_k3s")

    k8s = KubernetesCompiler(
        registry_prefix=registry_prefix,
        namespace=namespace,
        use_multus=True,
        internetMapEnabled=enable_internet_map,
        scheduling_strategy=scheduling_strategy,
        node_labels=node_labels,
        cni_type=cni_type,
        cni_master_interface=cni_master_interface,
        generate_services=True,
        image_pull_policy=image_pull_policy,
    )

    if enable_internet_map:
        k8s.attachInternetMap()

    emu.compile(k8s, output_dir, override=True)

    rr_plan_path = os.path.join(output_dir, "rr_plan.json")
    with open(rr_plan_path, "w", encoding="utf-8") as f:
        json.dump(rr_plan_by_as, f, indent=2, ensure_ascii=False, sort_keys=True)

    print("=" * 72)
    print("Real topology k3s compilation complete.")
    print("=" * 72)
    print(f"Topology file: {topology_file}")
    print(f"Assignment file: {assignment_file}")
    print(f"Output directory: {output_dir}")
    print(f"Namespace: {namespace}")
    print(f"Registry prefix: {registry_prefix}")
    print(f"CNI type: {cni_type}")
    print(f"Internet Map enabled: {enable_internet_map}")
    print(f"RR plan: {rr_plan_path}")
    print(f"编译时间: {time.time() - t} 秒")


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        print(f"[real_topology_k3s_compile] ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
