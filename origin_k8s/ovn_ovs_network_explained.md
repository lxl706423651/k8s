# KVM + K3s + Multus + Kube-OVN/OVS 网络原理说明

## 1. 完善后的写作 Prompt

请面向 Kubernetes 和 Linux 网络初学者，解释当前 SeedEMU KVM+K3s+Kube-OVN 实验中 Pod secondary network 的实现原理。说明应覆盖：

- 从宿主机 `virbr0`、KVM VM 网卡 `ens2`、K3s 默认 Pod 网络 `eth0/cni0` 到 SeedEMU 仿真接口 `net_101_102` 的分层关系。
- Kubernetes、Multus、NetworkAttachmentDefinition、Kube-OVN CNI、Kube-OVN Controller、OVN Central、OVS `br-int`、OVN Geneve tunnel 分别负责什么。
- CNI 在创建一个带 secondary interface 的 Pod 时，如何创建 veth、把 host-side veth 接到 OVS、把 pod-side veth 放进 Pod network namespace。
- 两个不同 node 上的 Pod，例如 `as2brd-r101` 和 `as2brd-r102`，如何通过同一个仿真网络 `net-2-net-101-102` 通信。
- 为什么不同仿真网络之间能实现二层隔离；同时解释为什么普通 IP ping 可能因为 SeedEMU/BIRD 三层路由而可达，这不等于二层隔离失效。
- 用 Linux bridge 和 VLAN 进行类比，但要明确 OVN 不是简单地给每个网络配置一个真实 VLAN ID，而是在同一个 OVS `br-int` 上用 OVN logical switch/logical datapath/OpenFlow/Geneve metadata 实现逻辑隔离。
- 给出当前真实实验中的具体命令、对象名、接口名、IP、OVN logical switch 名称，帮助读者验证。
- 最后给出核心理解题和答案，用来检查读者是否真正理解。

## 2. 当前真实实验对象

本文以当前已经跑通的 KVM+OVN 实验为例。

本轮文档中的命令已经替换为当前新集群的真实路径和对象名，核心值如下：

```text
kubeconfig: /home/lxl/k8s/origin_k8s/test/k8spre-kvm-ovn-example/setup/seedemu-k3s.kubeconfig.yaml
namespace:  seedemu-k3s-real-topo
src pod:    as2brd-r101-10.101.0.2-5db48ff988-bl7mg
dst pod:    as2brd-r102-10.102.0.2-89cf896fd-tzprk
observer:   as3brd-r103-10.103.0.3-7dcfb9749f-nr67m
ovn pod:    ovn-central-58fc6f9d79-rmggk
ovs pod:    ovs-ovn-4cjcq
```

集群节点：

```bash
kubectl --kubeconfig /home/lxl/k8s/origin_k8s/test/k8spre-kvm-ovn-example/setup/seedemu-k3s.kubeconfig.yaml get nodes -o wide
```

当前结果中的关键节点是：

```text
seed-k3s-master2    192.168.122.122
seed-k3s-worker12   192.168.122.123
seed-k3s-worker13   192.168.122.124
```

宿主机的 KVM/libvirt 默认二层网络：

```bash
ip -o -4 addr show virbr0
bridge link | grep virbr0
```

当前可以看到：

```text
virbr0  192.168.122.1/24
vnet108 master virbr0
vnet109 master virbr0
vnet110 master virbr0
```

这表示宿主机上有一个 Linux bridge `virbr0`，KVM VM 的 tap 设备，例如 `vnet108`、`vnet109`、`vnet110`，挂在这个 bridge 上。VM 里的 `ens2` 就通过这些 tap 设备接入 `192.168.122.0/24`。

VM 内部的 underlay 网卡：

```bash
ssh -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=no ubuntu@192.168.122.122 'ip -o -4 addr show scope global'
ssh -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=no ubuntu@192.168.122.123 'ip -o -4 addr show scope global'
ssh -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=no ubuntu@192.168.122.124 'ip -o -4 addr show scope global'
```

当前结果中：

```text
seed-k3s-master2:
  ens2  192.168.122.122/24
  cni0  10.42.0.1/20
  ovn0  100.64.0.2/16

seed-k3s-worker12:
  ens2  192.168.122.123/24
  cni0  10.42.32.1/20
  ovn0  100.64.0.3/16

seed-k3s-worker13:
  ens2  192.168.122.124/24
  cni0  10.42.16.1/20
  ovn0  100.64.0.4/16
```

三个网络层次要分清：

- `ens2` 是 VM 的 underlay 网卡，节点之间靠它互通。
- `eth0` 是每个 Pod 的 Kubernetes 默认网卡，属于 K3s 默认 Pod 网络，用于 kubelet、kubectl exec、service discovery 等基础能力。
- `net_101_102`、`ix101` 这类接口是 SeedEMU 仿真网络接口，由 Multus + Kube-OVN 作为 secondary network 创建。

示例 Pod：

```bash
kubectl --kubeconfig /home/lxl/k8s/origin_k8s/test/k8spre-kvm-ovn-example/setup/seedemu-k3s.kubeconfig.yaml \
  -n seedemu-k3s-real-topo get pods -o wide | grep -E 'as2brd-r101|as2brd-r102'
```

当前结果：

```text
as2brd-r101-...   Pod eth0 IP 10.42.16.19   node seed-k3s-worker13
as2brd-r102-...   Pod eth0 IP 10.42.0.27    node seed-k3s-master2
```

进入 `as2brd-r101` 看接口：

```bash
kubectl --kubeconfig /home/lxl/k8s/origin_k8s/test/k8spre-kvm-ovn-example/setup/seedemu-k3s.kubeconfig.yaml \
  -n seedemu-k3s-real-topo exec as2brd-r101-10.101.0.2-5db48ff988-bl7mg -- \
  ip -o addr
```

可以看到：

```text
eth0          10.42.16.19/20
dummy0        10.0.0.2/32
ix101         10.101.0.2/24
net_100_101   10.2.0.253/24
net_101_102   10.2.1.254/24
```

这里 `eth0` 是 Kubernetes 默认网络，`net_101_102` 才是 SeedEMU 中 AS2 r101 到 r102 的仿真链路。

## 3. 先用 Linux Bridge 理解最底层

Linux bridge 可以理解成一台软件交换机。

最简单的单机 Docker 或手工网络模型是：

```bash
sudo ip link add br-demo type bridge
sudo ip link set br-demo up

sudo ip link add veth-a type veth peer name veth-a-br
sudo ip link add veth-b type veth peer name veth-b-br

sudo ip link set veth-a-br master br-demo
sudo ip link set veth-b-br master br-demo
sudo ip link set veth-a-br up
sudo ip link set veth-b-br up
```

如果把 `veth-a` 放进容器 A，把 `veth-b` 放进容器 B，那么 A 和 B 就在同一个二层广播域中：

- A 发 ARP，B 能收到。
- A 发广播，B 能收到。
- bridge 学习 MAC 地址，把单播包转发到正确端口。

如果你想隔离两个网络，可以有两种常见做法。

第一种：创建两个 bridge：

```bash
sudo ip link add br-net-a type bridge
sudo ip link add br-net-b type bridge
```

挂在 `br-net-a` 上的容器和挂在 `br-net-b` 上的容器默认不会互相收到二层广播。

第二种：同一个 bridge 上使用 VLAN：

```bash
sudo ip link set br-demo type bridge vlan_filtering 1
sudo bridge vlan add dev veth-a-br vid 101 pvid untagged
sudo bridge vlan add dev veth-b-br vid 102 pvid untagged
```

这时同一个物理 bridge 上，VLAN 101 和 VLAN 102 是两个不同二层广播域。可以把它理解成同一个交换机被切成多个逻辑交换机。

OVN/OVS 的思路和这个很像，但不是简单地给每条仿真链路配置一个 Linux VLAN ID。它更像是：

- 所有真实端口都接到一台强大的软件交换机 OVS `br-int`。
- OVN 在控制面中创建很多 logical switch。
- OVS 通过 OpenFlow 和 Geneve metadata 区分每个 logical switch。
- 每个 logical switch 就像一个独立 bridge 或一个独立 VLAN 广播域。

## 4. 各组件分别负责什么

### 4.1 Kubernetes API Server

Kubernetes API Server 保存这些对象：

- Pod / Deployment
- Namespace
- NetworkAttachmentDefinition，简称 NAD
- Kube-OVN 的 CRD，例如 `Subnet`、`Vpc`

它本身不负责转发数据包，只保存期望状态。

### 4.2 Kubelet 和 Container Runtime

每个 node 上的 kubelet 负责真正创建 Pod sandbox。

创建 Pod 时，它会调用 CNI 插件：

- 默认 CNI 创建 Pod 的 `eth0`。
- Multus 作为 meta CNI，再根据 Pod annotation 调用 secondary CNI。

### 4.3 Multus

Multus 的作用可以理解成“多网卡调度器”。

普通 Kubernetes Pod 只有一个 `eth0`。SeedEMU 的 router Pod 需要很多接口，例如：

```text
eth0
ix101
net_100_101
net_101_102
```

Pod manifest 中会有类似 annotation：

```yaml
k8s.v1.cni.cncf.io/networks: '[{"name":"net-ix-ix101"},{"name":"net-2-net-100-101"},{"name":"net-2-net-101-102"}]'
```

Multus 看到这个 annotation 后，会去读取对应 NAD，然后调用 NAD 中指定的 CNI 插件。

### 4.4 NAD

NAD 是 Kubernetes 中描述“额外网络”的对象。

当前 `net-2-net-101-102` 的 NAD 是：

```yaml
apiVersion: k8s.cni.cncf.io/v1
kind: NetworkAttachmentDefinition
metadata:
  name: net-2-net-101-102
  namespace: seedemu-k3s-real-topo
spec:
  config: |
    {
      "cniVersion": "0.3.1",
      "type": "kube-ovn",
      "server_socket": "/run/openvswitch/kube-ovn-daemon.sock",
      "provider": "net-2-net-101-102.seedemu-k3s-real-topo.ovn"
    }
```

关键字段：

- `type: kube-ovn` 表示这个 secondary network 交给 Kube-OVN CNI 创建。
- `provider: net-2-net-101-102.seedemu-k3s-real-topo.ovn` 是这个网络在 Kube-OVN/OVN 中的唯一名字。

### 4.5 Kube-OVN Subnet 和 VPC

Kube-OVN 用 CRD 描述逻辑网络。

当前 `net-2-net-101-102` 对应的 Subnet 是：

```yaml
kind: Subnet
metadata:
  name: subnet-63cead70-seedemu-k3s-real-topo-net-2-net-101-102
spec:
  cidrBlock: 10.2.1.0/24
  gateway: 10.2.1.1
  provider: net-2-net-101-102.seedemu-k3s-real-topo.ovn
  vpc: vpc-f3d0a3be-seedemu-k3s-real-topo
```

意思是：

- 这个仿真网络的 IP 段是 `10.2.1.0/24`。
- 它属于 provider `net-2-net-101-102.seedemu-k3s-real-topo.ovn`。
- 它属于实验 VPC `vpc-f3d0a3be-seedemu-k3s-real-topo`。

注意：同一个 VPC 下的多个 subnet 可以通过三层路由互通，取决于配置和 Pod 内的路由/BIRD。因此“二层隔离”不等于“所有 IP 都 ping 不通”。

### 4.6 OVS

OVS 是每个 node 上真正转发包的软件交换机。

当前 worker node 上的 OVS：

```bash
kubectl --kubeconfig /home/lxl/k8s/origin_k8s/test/k8spre-kvm-ovn-example/setup/seedemu-k3s.kubeconfig.yaml \
  -n kube-system exec ovs-ovn-4cjcq -- ovs-vsctl show
```

可以看到：

```text
Bridge br-int
  Port d37da5ba_net3_h
    Interface d37da5ba_net3_h
  Port ovn-6aa8e9-0
    Interface ovn-6aa8e9-0
      type: geneve
      options: {local_ip="192.168.122.124", remote_ip="192.168.122.122"}
```

解释：

- `br-int` 是 integration bridge，所有 Pod secondary network 的 host-side veth 都会接到这里。
- `d37da5ba_net3_h` 是 `as2brd-r101` 的 `net_101_102` 在 host 侧对应的 veth。
- `ovn-6aa8e9-0` 是连到另一个 node 的 Geneve tunnel 端口。

Pod 内看到的接口是：

```text
net_101_102@if124
  link/ether 36:72:b9:9c:20:d0
  alias d37da5ba_net3_c
```

host 侧 OVS 接口带有 external IDs：

```text
name: d37da5ba_net3_h
external_ids:
  iface-id=as2brd-r101-...net-2-net-101-102.seedemu-k3s-real-topo.ovn
  ip="10.2.1.254"
  pod_name=as2brd-r101-...
  pod_namespace=seedemu-k3s-real-topo
  vendor=kube-ovn
```

这个 `iface-id` 是 OVS 本地接口和 OVN logical port 之间的绑定关系。

获取这些信息可以直接运行下面的命令。

先看 Pod 内 `net_101_102` 的真实接口名、MAC 和 alias：

```bash
kubectl --kubeconfig /home/lxl/k8s/origin_k8s/test/k8spre-kvm-ovn-example/setup/seedemu-k3s.kubeconfig.yaml \
  -n seedemu-k3s-real-topo exec as2brd-r101-10.101.0.2-5db48ff988-bl7mg -- \
  ip -d link show net_101_102
```

也可以只看这个接口的 IP：

```bash
kubectl --kubeconfig /home/lxl/k8s/origin_k8s/test/k8spre-kvm-ovn-example/setup/seedemu-k3s.kubeconfig.yaml \
  -n seedemu-k3s-real-topo exec as2brd-r101-10.101.0.2-5db48ff988-bl7mg -- \
  ip -o addr show net_101_102
```

再看 host 侧 OVS interface 的 `external_ids`：

```bash
kubectl --kubeconfig /home/lxl/k8s/origin_k8s/test/k8spre-kvm-ovn-example/setup/seedemu-k3s.kubeconfig.yaml \
  -n kube-system exec ovs-ovn-4cjcq -- \
  ovs-vsctl --columns=name,external_ids list Interface d37da5ba_net3_h
```

如果你不确定当前 Pod 对应哪个 host-side veth，可以用下面这组命令自动从 Pod interface 的 alias 推导出来。这里 `_c` 表示 container side，替换成 `_h` 就是 host side：

```bash
K=/home/lxl/k8s/origin_k8s/test/k8spre-kvm-ovn-example/setup/seedemu-k3s.kubeconfig.yaml
NS=seedemu-k3s-real-topo
POD=as2brd-r101-10.101.0.2-5db48ff988-bl7mg
IFACE=net_101_102

HOST_IFACE="$(
  kubectl --kubeconfig "$K" -n "$NS" exec "$POD" -- ip -d link show "$IFACE" \
    | awk '/alias / {print $2}' \
    | sed 's/_c$/_h/'
)"

echo "$HOST_IFACE"
```

如果你也不确定这个 Pod 在哪个 node 上、该 node 对应哪个 `ovs-ovn-*` Pod，可以继续自动查：

```bash
NODE="$(
  kubectl --kubeconfig "$K" -n "$NS" get pod "$POD" \
    -o jsonpath='{.spec.nodeName}'
)"

OVS_POD="$(
  kubectl --kubeconfig "$K" -n kube-system get pods -o wide --no-headers \
    | awk -v node="$NODE" '$1 ~ /^ovs-ovn-/ && $7 == node {print $1; exit}'
)"

echo "node=$NODE"
echo "ovs_pod=$OVS_POD"
echo "host_iface=$HOST_IFACE"
```

最后用自动得到的 `OVS_POD` 和 `HOST_IFACE` 查询 OVS 绑定信息：

```bash
kubectl --kubeconfig "$K" -n kube-system exec "$OVS_POD" -- \
  ovs-vsctl --columns=name,external_ids list Interface "$HOST_IFACE"
```

### 4.7 OVN

OVN 是 OVS 的控制平面。可以把它理解成“集中设计逻辑网络，然后下发规则给各 node 上的 OVS”。

OVN 里有几个核心对象：

- Logical Switch：逻辑交换机，类似一个独立 bridge 或 VLAN。
- Logical Switch Port：逻辑交换机上的端口，对应一个 Pod 的一个接口。
- Chassis：一个 node，例如 `seed-k3s-master2`、`seed-k3s-worker12` 或 `seed-k3s-worker13`。
- Port Binding：某个 logical port 当前在哪个 chassis 上。

查看 logical switch：

```bash
kubectl --kubeconfig /home/lxl/k8s/origin_k8s/test/k8spre-kvm-ovn-example/setup/seedemu-k3s.kubeconfig.yaml \
  -n kube-system exec ovn-central-58fc6f9d79-rmggk -- \
  ovn-nbctl --columns=name list logical_switch | grep net-2-net-101-102
```

可以看到：

```text
subnet-63cead70-seedemu-k3s-real-topo-net-2-net-101-102
```

查看这个 logical switch 上有哪些端口：

```bash
kubectl --kubeconfig /home/lxl/k8s/origin_k8s/test/k8spre-kvm-ovn-example/setup/seedemu-k3s.kubeconfig.yaml \
  -n kube-system exec ovn-central-58fc6f9d79-rmggk -- \
  ovn-nbctl lsp-list subnet-63cead70-seedemu-k3s-real-topo-net-2-net-101-102
```

当前结果：

```text
as2brd-r101-10.101.0.2-5db48ff988-bl7mg.seedemu-k3s-real-topo.net-2-net-101-102.seedemu-k3s-real-topo.ovn
as2brd-r102-10.102.0.2-89cf896fd-tzprk.seedemu-k3s-real-topo.net-2-net-101-102.seedemu-k3s-real-topo.ovn
subnet-63cead70-...-vpc-f3d0a3be-seedemu-k3s-real-topo
```

这说明 `net-2-net-101-102` 这个二层网络中只有 r101、r102 相关端口和一个逻辑网关端口。

## 5. CNI 创建 OVS+OVN 网络时发生了什么

以 `as2brd-r101` 创建 `net_101_102` 为例。

第一步，kubelet 创建 Pod sandbox，并调用默认 CNI 创建 `eth0`：

```text
Pod eth0 = 10.42.16.19/20
```

这是 Kubernetes 默认网络，不是 SeedEMU 仿真链路。

第二步，Multus 读取 Pod annotation：

```yaml
k8s.v1.cni.cncf.io/networks: '[{"name":"net-ix-ix101"},{"name":"net-2-net-100-101"},{"name":"net-2-net-101-102"}]'
```

第三步，Multus 找到 `net-2-net-101-102` 这个 NAD，发现它的 CNI 类型是 `kube-ovn`。

第四步，Kube-OVN CNI 通过：

```text
/run/openvswitch/kube-ovn-daemon.sock
```

请求本 node 上的 Kube-OVN daemon 创建接口。

第五步，Kube-OVN daemon 做类似下面的事情。注意这是等价理解，不是逐字执行的原始命令：

```bash
# 创建一对 veth。
ip link add d37da5ba_net3_h type veth peer name d37da5ba_net3_c

# host 侧接入 OVS br-int。
ovs-vsctl add-port br-int d37da5ba_net3_h

# 写入 OVN 识别这个端口所需的 external_ids。
ovs-vsctl set Interface d37da5ba_net3_h \
  external_ids:iface-id=as2brd-r101-10.101.0.2-5db48ff988-bl7mg.seedemu-k3s-real-topo.net-2-net-101-102.seedemu-k3s-real-topo.ovn \
  external_ids:ip=10.2.1.254 \
  external_ids:pod_name=as2brd-r101-10.101.0.2-5db48ff988-bl7mg \
  external_ids:pod_namespace=seedemu-k3s-real-topo

# pod 侧移入 Pod network namespace，并改名为 net_101_102。
ip link set d37da5ba_net3_c netns <pod-netns>
ip -n <pod-netns> link set d37da5ba_net3_c name net_101_102

# 在 Pod 内配置 IP 和 MAC。
ip -n <pod-netns> addr add 10.2.1.254/24 dev net_101_102
ip -n <pod-netns> link set net_101_102 up
```

第六步，Kube-OVN controller 和 OVN central 确保 OVN 里存在：

```text
Logical Switch:
  subnet-63cead70-seedemu-k3s-real-topo-net-2-net-101-102

Logical Switch Port:
  as2brd-r101-...net-2-net-101-102...
  address: 36:72:b9:9c:20:d0 10.2.1.254
```

第七步，每个 node 上的 `ovn-controller` 从 OVN Southbound DB 读取逻辑网络，给本地 OVS `br-int` 下发 OpenFlow 规则。

因此，真正发包时不是 Linux bridge 在普通学习转发，而是：

```text
Pod veth -> OVS br-int -> OVN 逻辑流表 -> 本地端口或 Geneve tunnel
```

## 6. 两个不同 node 的 Pod 如何通信

示例：

```text
as2brd-r101
  node: seed-k3s-worker13
  eth0: 10.42.16.19
  net_101_102: 10.2.1.254

as2brd-r102
  node: seed-k3s-master2
  eth0: 10.42.0.27
  net_101_102: 10.2.1.253
```

两个 Pod 在不同 node 上，但都接入同一个 OVN logical switch：

```text
subnet-63cead70-seedemu-k3s-real-topo-net-2-net-101-102
```

通信路径如下。

第一步，`as2brd-r101` 发包：

```bash
kubectl --kubeconfig /home/lxl/k8s/origin_k8s/test/k8spre-kvm-ovn-example/setup/seedemu-k3s.kubeconfig.yaml \
  -n seedemu-k3s-real-topo exec as2brd-r101-10.101.0.2-5db48ff988-bl7mg -- \
  ping -c 3 10.2.1.253
```

包从 Pod 内的 `net_101_102` 出去：

```text
as2brd-r101 net_101_102
  IP 10.2.1.254
  MAC 36:72:b9:9c:20:d0
```

第二步，包通过 veth 到达 worker node 的 OVS：

```text
Pod side:  net_101_102
Host side: d37da5ba_net3_h
Bridge:    br-int
```

第三步，worker node 的 OVS/OVN 判断：

```text
目的 IP/MAC 属于 net-2-net-101-102 这个 logical switch
目的 logical port 在 seed-k3s-master2 上
```

第四步，worker node 用 Geneve tunnel 封装原始二层帧：

```text
外层 underlay:
  src node IP: 192.168.122.124
  dst node IP: 192.168.122.122
  out dev: ens2

内层 overlay:
  src pod IP: 10.2.1.254
  dst pod IP: 10.2.1.253
  logical switch: net-2-net-101-102
```

可以把它想象成：

```text
把一封写给 10.2.1.253 的信装进一个快递盒。
快递盒外面写的是从 192.168.122.124 发到 192.168.122.122。
到了目标 node 后，拆开快递盒，再把里面的原始二层帧交给目标 Pod。
```

第五步，underlay 路径经过 KVM/libvirt 网络：

```text
seed-k3s-worker13 ens2
  -> QEMU tap，例如 vnet110
  -> host virbr0
  -> QEMU tap，例如 vnet108
  -> seed-k3s-master2 ens2
```

第六步，master node 的 OVS 收到 Geneve 包：

```text
Geneve tunnel port -> br-int -> target host-side veth -> Pod net_101_102
```

最后，`as2brd-r102` 在 `net_101_102` 上收到包。

## 7. 为什么能实现二层隔离

当前每个 SeedEMU 仿真网络都被编译成独立的 Kube-OVN provider/subnet/logical switch。

例如：

```text
net-2-net-101-102
  provider: net-2-net-101-102.seedemu-k3s-real-topo.ovn
  subnet:   subnet-63cead70-seedemu-k3s-real-topo-net-2-net-101-102
  cidr:     10.2.1.0/24

net-3-net-103-105
  provider: net-3-net-103-105.seedemu-k3s-real-topo.ovn
  subnet:   subnet-796e9bae-seedemu-k3s-real-topo-net-3-net-103-105
  cidr:     10.3.2.0/24
```

它们在 OVN 中是两个不同 logical switch：

```text
subnet-63cead70-seedemu-k3s-real-topo-net-2-net-101-102
subnet-796e9bae-seedemu-k3s-real-topo-net-3-net-103-105
```

这和 Linux bridge/VLAN 的类比如下：

```text
Linux bridge 多 bridge 模型:
  br-net-2-101-102 只接 r101 和 r102
  br-net-3-103-105 只接 r103 和 r105

VLAN 模型:
  VLAN 20102 只接 r101 和 r102
  VLAN 31035 只接 r103 和 r105

OVN 模型:
  logical switch net-2-net-101-102 只接 r101 和 r102 的相关 logical port
  logical switch net-3-net-103-105 只接 r103 和 r105 的相关 logical port
```

虽然在每个 node 上它们的 host-side veth 都插在同一个 OVS `br-int` 上，但 OVS 不会把它们当成一个普通大二层网桥。OVN 下发的 OpenFlow 规则会给每个包打上逻辑网络上下文，类似“这个包属于 logical datapath A”，另一个网络的端口属于 logical datapath B。

因此：

- 同一个 logical switch 内，广播可以被同网端口看到。
- 不同 logical switch 之间，二层广播不会互相泛洪。
- 这就是 per-network L2 isolation。

## 8. 实际隔离验证

### 8.1 同一网络内广播能被看到

在 `as2brd-r102` 的 `net_101_102` 上抓包：

```bash
kubectl --kubeconfig /home/lxl/k8s/origin_k8s/test/k8spre-kvm-ovn-example/setup/seedemu-k3s.kubeconfig.yaml \
  -n seedemu-k3s-real-topo exec as2brd-r102-10.102.0.2-89cf896fd-tzprk -- \
  timeout 8 tcpdump -i net_101_102 -nne 'icmp or arp'
```

同时在 `as2brd-r101` 发二层广播 ping：

```bash
kubectl --kubeconfig /home/lxl/k8s/origin_k8s/test/k8spre-kvm-ovn-example/setup/seedemu-k3s.kubeconfig.yaml \
  -n seedemu-k3s-real-topo exec as2brd-r101-10.101.0.2-5db48ff988-bl7mg -- \
  ping -b -c 3 -W 1 -I net_101_102 10.2.1.255
```

抓包结果能看到：

```text
36:72:b9:9c:20:d0 > ff:ff:ff:ff:ff:ff
10.2.1.254 > 10.2.1.255: ICMP echo request
```

这说明同一个 `net-2-net-101-102` 内广播可见。

### 8.2 不同网络的 Pod 看不到这个广播

在不属于 `net-2-net-101-102` 的 `as3brd-r103` 上抓所有接口：

```bash
kubectl --kubeconfig /home/lxl/k8s/origin_k8s/test/k8spre-kvm-ovn-example/setup/seedemu-k3s.kubeconfig.yaml \
  -n seedemu-k3s-real-topo exec as3brd-r103-10.103.0.3-7dcfb9749f-nr67m -- \
  timeout 8 tcpdump -i any -nne 'icmp and (host 10.2.1.254 or host 10.2.1.255)'
```

同时重复上面的广播 ping。结果是：

```text
0 packets captured
0 packets received by filter
0 packets dropped by kernel
```

这说明二层广播没有跨 logical switch 泄露。

### 8.3 为什么普通 ping 另一个网段可能成功

从 `as2brd-r101` 普通 ping `10.3.2.254`：

```bash
kubectl --kubeconfig /home/lxl/k8s/origin_k8s/test/k8spre-kvm-ovn-example/setup/seedemu-k3s.kubeconfig.yaml \
  -n seedemu-k3s-real-topo exec as2brd-r101-10.101.0.2-5db48ff988-bl7mg -- \
  ping -c 2 10.3.2.254
```

它可能成功，因为 Pod 内 BIRD 已经安装了三层路由：

```text
10.3.2.0/24 via 10.2.0.254 dev net_100_101 proto bird
```

这表示包不是在二层直接发到 `net-3-net-103-105`，而是先发给下一跳 router，再通过 SeedEMU 仿真拓扑路由过去。

如果强制从错误的二层接口发：

```bash
kubectl --kubeconfig /home/lxl/k8s/origin_k8s/test/k8spre-kvm-ovn-example/setup/seedemu-k3s.kubeconfig.yaml \
  -n seedemu-k3s-real-topo exec as2brd-r101-10.101.0.2-5db48ff988-bl7mg -- \
  ping -I net_101_102 -c 2 -W 1 10.3.2.254
```

当前结果是：

```text
2 packets transmitted, 0 received, 100% packet loss
```

所以要区分：

- 二层隔离：广播、ARP、同一二层网内直接交换帧不会跨 logical switch。
- 三层可达：如果路由表允许，IP 包可以被 router 转发到另一个网段。

## 9. 和 VLAN 的准确类比

如果你熟悉 VLAN，可以这样理解。

传统交换机中：

```text
VLAN 101:
  port1, port2

VLAN 102:
  port3, port4
```

即使 port1 和 port3 插在同一台交换机上，它们也不是同一个二层广播域。port1 的广播不会发到 port3。

当前 OVN 中：

```text
logical switch net-2-net-101-102:
  as2brd-r101/net_101_102
  as2brd-r102/net_101_102

logical switch net-3-net-103-105:
  as3brd-r103/net_103_105
  as3brd-r105/net_103_105
```

它们也不是同一个二层广播域。

区别是：

- VLAN 是交换机数据帧里常见的 802.1Q tag。
- OVN 不要求你在物理交换机上配置 VLAN。
- OVN 在 OVS 内部用 logical datapath、logical port、OpenFlow 和 Geneve metadata 表达“这个包属于哪个逻辑网络”。

所以它更像“软件定义的 VLAN/bridge”，但不是直接依赖物理 VLAN。

## 10. 常用排查命令

查看 Pod 分布：

```bash
kubectl --kubeconfig /home/lxl/k8s/origin_k8s/test/k8spre-kvm-ovn-example/setup/seedemu-k3s.kubeconfig.yaml \
  -n seedemu-k3s-real-topo get pods -o wide
```

查看 NAD：

```bash
kubectl --kubeconfig /home/lxl/k8s/origin_k8s/test/k8spre-kvm-ovn-example/setup/seedemu-k3s.kubeconfig.yaml \
  -n seedemu-k3s-real-topo get nad
```

查看某个 NAD：

```bash
kubectl --kubeconfig /home/lxl/k8s/origin_k8s/test/k8spre-kvm-ovn-example/setup/seedemu-k3s.kubeconfig.yaml \
  -n seedemu-k3s-real-topo get nad net-2-net-101-102 -o yaml
```

查看 Kube-OVN subnet：

```bash
kubectl --kubeconfig /home/lxl/k8s/origin_k8s/test/k8spre-kvm-ovn-example/setup/seedemu-k3s.kubeconfig.yaml get subnet
```

查看 Pod 内接口：

```bash
kubectl --kubeconfig /home/lxl/k8s/origin_k8s/test/k8spre-kvm-ovn-example/setup/seedemu-k3s.kubeconfig.yaml \
  -n seedemu-k3s-real-topo exec as2brd-r101-10.101.0.2-5db48ff988-bl7mg -- ip -o addr
```

查看 Pod 路由：

```bash
kubectl --kubeconfig /home/lxl/k8s/origin_k8s/test/k8spre-kvm-ovn-example/setup/seedemu-k3s.kubeconfig.yaml \
  -n seedemu-k3s-real-topo exec as2brd-r101-10.101.0.2-5db48ff988-bl7mg -- ip route
```

查看 OVS `br-int`：

```bash
kubectl --kubeconfig /home/lxl/k8s/origin_k8s/test/k8spre-kvm-ovn-example/setup/seedemu-k3s.kubeconfig.yaml \
  -n kube-system exec ovs-ovn-4cjcq -- ovs-vsctl show
```

查看 OVN logical switch：

```bash
kubectl --kubeconfig /home/lxl/k8s/origin_k8s/test/k8spre-kvm-ovn-example/setup/seedemu-k3s.kubeconfig.yaml \
  -n kube-system exec ovn-central-58fc6f9d79-rmggk -- ovn-nbctl --columns=name list logical_switch
```

查看某个 logical switch 的端口：

```bash
kubectl --kubeconfig /home/lxl/k8s/origin_k8s/test/k8spre-kvm-ovn-example/setup/seedemu-k3s.kubeconfig.yaml \
  -n kube-system exec ovn-central-58fc6f9d79-rmggk -- \
  ovn-nbctl lsp-list subnet-63cead70-seedemu-k3s-real-topo-net-2-net-101-102
```

查看 node 间 Geneve tunnel：

```bash
kubectl --kubeconfig /home/lxl/k8s/origin_k8s/test/k8spre-kvm-ovn-example/setup/seedemu-k3s.kubeconfig.yaml \
  -n kube-system exec ovs-ovn-4cjcq -- ovs-vsctl show | grep -A4 geneve
```

你会看到类似：

```text
Interface ovn-6aa8e9-0
  type: geneve
  options: {local_ip="192.168.122.124", remote_ip="192.168.122.122"}
```

## 11. 核心理解题

### 题 1：`eth0` 和 `net_101_102` 分别是什么？

答案：

`eth0` 是 Kubernetes 默认 Pod 网络接口，当前例子里类似 `10.42.16.19/20`，用于 Kubernetes 基础通信、kubectl exec、Service 等。`net_101_102` 是 SeedEMU 仿真网络接口，当前例子里是 `10.2.1.254/24`，表示 r101 到 r102 的仿真链路。

### 题 2：Multus 的核心作用是什么？

答案：

Multus 是 meta CNI。它让一个 Pod 除了默认 `eth0` 外，还能根据 Pod annotation 和 NAD 创建额外网卡，例如 `ix101`、`net_100_101`、`net_101_102`。Multus 本身不负责最终数据转发，它负责调用真正的 secondary CNI，例如 Kube-OVN。

### 题 3：NAD 中的 `provider` 为什么重要？

答案：

`provider` 把 Kubernetes 的 NAD 和 Kube-OVN/OVN 的逻辑网络绑定起来。例如：

```text
net-2-net-101-102.seedemu-k3s-real-topo.ovn
```

它对应一个 Kube-OVN Subnet 和一个 OVN logical switch。不同 provider 通常表示不同二层网络。

### 题 4：为什么所有 host-side veth 都接到 OVS `br-int`，却不会变成一个大二层网络？

答案：

因为 `br-int` 不是普通 Linux bridge 的简单泛洪学习模式。OVN 给 OVS 下发 OpenFlow 规则，每个端口属于特定 logical switch/logical datapath。包进入 `br-int` 后会根据 logical context 转发，不同 logical switch 之间不会互相二层泛洪。

### 题 5：`net-2-net-101-102` 和 `net-3-net-103-105` 的二层隔离体现在哪里？

答案：

它们有不同 provider、不同 Kube-OVN Subnet、不同 OVN logical switch。实测中，`as2brd-r101` 在 `net_101_102` 上发送 `10.2.1.255` 广播，同网的 `as2brd-r102` 能抓到，不属于这个网络的 `as3brd-r103` 在 `tcpdump -i any` 上抓不到。

### 题 6：为什么 `as2brd-r101` 普通 ping `10.3.2.254` 可能成功，但这不代表二层隔离失败？

答案：

普通 ping 会查路由表。当前 Pod 内 BIRD 已经安装了到 `10.3.2.0/24` 的三层路由，例如经 `10.2.0.254 dev net_100_101` 转发。这个包是通过 SeedEMU 仿真路由器转发过去的，不是 `net_101_102` 的二层广播直接泄露到 `net_3_103_105`。

### 题 7：跨 node 通信时，外层 IP 和内层 IP 分别是什么？

答案：

以 `as2brd-r101` 到 `as2brd-r102` 为例：

- 内层 IP 是 Pod 仿真接口 IP：`10.2.1.254 -> 10.2.1.253`。
- 外层 IP 是 node underlay IP：`192.168.122.124 -> 192.168.122.122`。

OVS/OVN 用 Geneve tunnel 把内层二层帧封装在外层 node-to-node IP 包里。

### 题 8：Geneve tunnel 是干什么的？

答案：

Geneve tunnel 负责跨 node 承载 overlay 网络包。不同 node 上的 OVS 通过 Geneve tunnel 交换属于同一个 OVN logical switch 的二层帧。当前 worker 上能看到类似：

```text
Interface ovn-6aa8e9-0
  type: geneve
  local_ip=192.168.122.124
  remote_ip=192.168.122.122
```

### 题 9：如果不用 OVN，只把所有 Pod 用 macvlan 接到同一个 `ens2` 或 `br-seedemu`，会发生什么？

答案：

那所有 Pod 很可能处于同一个大二层广播域。不同仿真网络的 ARP/广播可能互相可见，除非你额外使用 VLAN、多个 bridge、多个 macvlan parent 或其他隔离机制。OVN 的优势是它能为每个仿真网络创建独立 logical switch，不依赖物理交换机 VLAN 配置。

### 题 10：如果两个不同仿真网络都错误地使用同一个 provider，会有什么风险？

答案：

它们可能被放进同一个逻辑网络，导致二层广播域合并，ARP/广播泄露，甚至 IP 冲突和路由行为异常。因此 provider 必须和 SeedEMU 的每个仿真网络一一对应。

## 12. 一句话总结

当前 KVM+OVN 模式下，宿主机 `virbr0` 和 VM `ens2` 只是 node-to-node underlay；Pod 的 `eth0` 是 Kubernetes 默认网络；SeedEMU 的 `net_101_102` 等接口由 Multus 调用 Kube-OVN CNI 创建，host 侧接入 OVS `br-int`，再由 OVN logical switch 和 Geneve tunnel 实现跨 node 的二层 overlay。每个 SeedEMU 仿真网络对应独立 provider/subnet/logical switch，因此实现了 per-network 二层隔离；普通跨网段 IP 可达通常来自 SeedEMU/BIRD 的三层路由，不等于二层隔离失效。
