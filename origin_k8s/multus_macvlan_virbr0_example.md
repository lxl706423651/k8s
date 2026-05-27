# Multus + macvlan + virbr0 跨节点通信示例

本文用当前 12-node K3s 集群中的一个真实例子说明：KVM VM 之间如何通过宿主机 `virbr0` 连成同一个二层网络，以及 SEED Pod 如何通过 Multus + macvlan 加入仿真的网络。

示例选用两个跨节点 Pod：

| Pod | Kubernetes node | Pod eth0 | 仿真接口 | 仿真 IP |
| --- | --- | --- | --- | --- |
| `as2brd-r100-10.100.0.2-7bf4dbc79c-wrgmq` | `seed-k3s-worker9` | `10.42.3.212` | `net_100_101` | `10.2.0.254/24` |
| `as2brd-r101-10.101.0.2-6bfb57c455-6b99s` | `seed-k3s-worker3` | `10.42.73.174` | `net_100_101` | `10.2.0.253/24` |

它们共享的仿真网络是：

```text
NetworkAttachmentDefinition: net-2-net-100-101
prefix: 10.2.0.0/24
master interface: ens2
CNI type: macvlan
mode: bridge
IPAM: static
```

## 1. VM 之间如何连到同一个二层网络

当前 KVM 使用 libvirt 的 `default` 网络。宿主机上这个网络对应 Linux bridge `virbr0`：

```bash
virsh net-dumpxml default
```

关键结构类似：

```xml
<network>
  <name>default</name>
  <forward mode='nat'/>
  <bridge name='virbr0' stp='on' delay='0'/>
  <ip address='192.168.122.1' netmask='255.255.255.0'/>
</network>
```

也就是说，所有接入 libvirt `default` 网络的 VM，都会在宿主机侧生成一个 `vnetX` tap 设备，并挂到 `virbr0` 上。当前例子里：

```bash
virsh domiflist seed-k3s-worker9
virsh domiflist seed-k3s-worker3
```

输出对应关系是：

```text
seed-k3s-worker9 -> host vnet50 -> virbr0
seed-k3s-worker3 -> host vnet44 -> virbr0
```

可以用下面命令确认：

```bash
ip -br link show virbr0
bridge link show | egrep 'vnet44|vnet50'
```

当前结果类似：

```text
virbr0 UP
vnet44 master virbr0 state forwarding
vnet50 master virbr0 state forwarding
```

在 VM 内部，宿主机的 `vnetX` 对应 VM 看到的 `ens2`：

```bash
ssh -i /home/lxl/.ssh/id_ed25519 ubuntu@192.168.122.119 "ip -br addr show ens2"
ssh -i /home/lxl/.ssh/id_ed25519 ubuntu@192.168.122.113 "ip -br addr show ens2"
```

示例：

```text
seed-k3s-worker9  ens2  192.168.122.119/24
seed-k3s-worker3  ens2  192.168.122.113/24
```

所以 VM 层的二层路径是：

```text
seed-k3s-worker9:ens2
  -> host:vnet50
  -> host:virbr0
  -> host:vnet44
  -> seed-k3s-worker3:ens2
```

如果不用 libvirt，等价 Linux bridge 操作大概如下。注意：这一组命令是在**宿主机**上执行，不是在 K3s VM 里执行。因为 `virbr0`、`vnet44`、`vnet50` 都是宿主机网络命名空间里的设备：

```bash
# 执行位置：KVM 宿主机。
# 作用：创建宿主机侧 Linux bridge，模拟 libvirt default network 的二层承载。
sudo ip link add name virbr0 type bridge
sudo ip addr add 192.168.122.1/24 dev virbr0
sudo ip link set virbr0 up

# 执行位置：KVM 宿主机。
# 作用：把 QEMU/libvirt 创建的 VM tap 设备挂到宿主机 bridge 上。
# vnet44/vnet50 通常由 qemu/libvirt 自动创建；这里仅展示等价挂载动作。
sudo ip link set vnet44 master virbr0
sudo ip link set vnet50 master virbr0
sudo ip link set vnet44 up
sudo ip link set vnet50 up
```

实际项目中不需要手工执行这些命令，libvirt 会根据 `default` network 和 VM XML 自动完成。VM 内部看不到 `vnet44`、`vnet50`、`virbr0`；VM 内部只看到自己的网卡，例如 `seed-k3s-worker9` 里看到的是 `ens2`。

## 2. Multus 如何让 Pod 多接一个仿真网卡

Kubernetes 默认只给 Pod 一个主网卡 `eth0`。这个网卡走 K3s/Flannel/CNI 网络，地址是 `10.42.x.x`，用于 Kubernetes 管理流量，例如 `kubectl exec`、Service、Pod 到 Pod 默认通信。

SEED 仿真网络不依赖 `eth0`。它通过 Multus 给 Pod 额外挂载第二、第三、第四个接口，例如：

```text
net_100_101
ix100
net_100_105
```

这些接口来自 Kubernetes 的 `NetworkAttachmentDefinition`。本例里的 NAD 是：

```bash
kubectl --kubeconfig /home/lxl/k8s/output/kubeconfigs/seedemu-k3s.yaml \
  -n seedemu-k3s-real-topo \
  get network-attachment-definitions.k8s.cni.cncf.io net-2-net-100-101 -o yaml
```

核心内容：

```yaml
apiVersion: k8s.cni.cncf.io/v1
kind: NetworkAttachmentDefinition
metadata:
  name: net-2-net-100-101
  namespace: seedemu-k3s-real-topo
  annotations:
    org.seedsecuritylabs.seedemu.meta.prefix: 10.2.0.0/24
spec:
  config: '{"cniVersion": "0.3.1", "type": "macvlan", "master": "ens2", "mode": "bridge", "ipam": {"type": "static"}}'
```

字段含义：

| 字段 | 含义 |
| --- | --- |
| `type: macvlan` | 让 CNI 在 Pod netns 中创建 macvlan 网卡。 |
| `master: ens2` | macvlan 的 lower device 是当前 node VM 的 `ens2`。 |
| `mode: bridge` | 同一个 lower device 以及外部二层网络上的 macvlan endpoint 可以互相通信。 |
| `ipam: static` | IP 不由 CNI 自动分配，而是由 Pod annotation 指定。 |

等价 Linux 命令可以理解成：

```bash
# 在 node 上创建一个挂在 ens2 下的 macvlan 设备。
sudo ip link add link ens2 name net_100_101 type macvlan mode bridge

# 把接口移动到 Pod 的 network namespace。
sudo ip link set net_100_101 netns <pod-netns>

# 在 Pod netns 里配置静态 IP。
sudo ip -n <pod-netns> addr add 10.2.0.254/24 dev net_100_101
sudo ip -n <pod-netns> link set net_100_101 up
```

真实环境中这些动作由 kubelet 调 Multus，再由 Multus 调 macvlan CNI 插件完成。

## 3. Pod annotation 如何指定网络和 IP

编译生成的 `k8s.yaml` 会在 Pod template annotation 里写入要加入哪些 NAD，以及每个接口的静态 IP。

`as2brd-r100` 片段：

```yaml
k8s.v1.cni.cncf.io/networks: >
  [
    {"name": "net-ix-ix100", "ips": ["10.100.0.2/24"]},
    {"name": "net-2-net-100-101", "ips": ["10.2.0.254/24"]},
    {"name": "net-2-net-100-105", "ips": ["10.2.2.254/24"]}
  ]
```

`as2brd-r101` 片段：

```yaml
k8s.v1.cni.cncf.io/networks: >
  [
    {"name": "net-ix-ix101", "ips": ["10.101.0.2/24"]},
    {"name": "net-2-net-100-101", "ips": ["10.2.0.253/24"]},
    {"name": "net-2-net-101-102", "ips": ["10.2.1.254/24"]}
  ]
```

两者都加入了同一个 NAD：

```text
net-2-net-100-101
```

因此它们在仿真网络上处于同一个二层网段：

```text
10.2.0.0/24
```

## 4. Pod 里实际看到的接口

查看 `as2brd-r100`：

```bash
kubectl --kubeconfig /home/lxl/k8s/output/kubeconfigs/seedemu-k3s.yaml \
  -n seedemu-k3s-real-topo \
  exec as2brd-r100-10.100.0.2-7bf4dbc79c-wrgmq -- ip -br addr
```

结果：

```text
eth0           10.42.3.212/20
ix100          10.100.0.2/24
net_100_101    10.2.0.254/24
net_100_105    10.2.2.254/24
dummy0         10.0.0.1/32
```

查看 `as2brd-r101`：

```bash
kubectl --kubeconfig /home/lxl/k8s/output/kubeconfigs/seedemu-k3s.yaml \
  -n seedemu-k3s-real-topo \
  exec as2brd-r101-10.101.0.2-6bfb57c455-6b99s -- ip -br addr
```

结果：

```text
eth0           10.42.73.174/20
ix101          10.101.0.2/24
net_100_101    10.2.0.253/24
net_101_102    10.2.1.254/24
dummy0         10.0.0.2/32
```

这里要区分两类接口：

| 接口 | 来源 | 用途 |
| --- | --- | --- |
| `eth0` | K3s/Flannel/CNI | Kubernetes 管理网络。 |
| `net_100_101` | Multus + macvlan | SEED 仿真链路。 |
| `ix100/ix101` | Multus + macvlan | SEED IX/仿真网络。 |
| `dummy0` | 容器内部配置 | loopback-like router ID/控制面地址。 |

## 5. 跨节点通信路径

当 `as2brd-r100` ping `10.2.0.253` 时：

```bash
kubectl --kubeconfig /home/lxl/k8s/output/kubeconfigs/seedemu-k3s.yaml \
  -n seedemu-k3s-real-topo \
  exec as2brd-r100-10.100.0.2-7bf4dbc79c-wrgmq -- ping -c 2 10.2.0.253
```

验证结果：

```text
2 packets transmitted, 2 received, 0% packet loss
```

数据路径是：

```text
Pod A: as2brd-r100
  net_100_101 10.2.0.254
  mac 66:97:ed:5c:6a:cc
        |
        v
Node A: seed-k3s-worker9
  ens2 192.168.122.119
        |
        v
Host side tap:
  vnet50
        |
        v
Host bridge:
  virbr0
        |
        v
Host side tap:
  vnet44
        |
        v
Node B: seed-k3s-worker3
  ens2 192.168.122.113
        |
        v
Pod B: as2brd-r101
  net_100_101 10.2.0.253
  mac 62:07:87:bc:94:4b
```

可以从宿主机的 FDB 看到 macvlan 产生的 Pod MAC 已经被 `virbr0` 学到：

```bash
bridge fdb show br virbr0 | egrep '66:97:ed:5c:6a:cc|62:07:87:bc:94:4b|vnet44|vnet50'
```

示例输出：

```text
66:97:ed:5c:6a:cc dev vnet50 master virbr0
62:07:87:bc:94:4b dev vnet44 master virbr0
```

这说明：

```text
10.2.0.254 对应的 Pod MAC 在 vnet50 后面，也就是 worker9 后面。
10.2.0.253 对应的 Pod MAC 在 vnet44 后面，也就是 worker3 后面。
```

## 6. ARP 和二层转发过程

以 `as2brd-r100` 访问 `10.2.0.253` 为例：

1. `as2brd-r100` 发现 `10.2.0.253` 与自己 `10.2.0.254/24` 在同一网段。
2. Linux 从 `net_100_101` 发 ARP：谁是 `10.2.0.253`？
3. 这个 ARP 帧从 Pod 的 macvlan 接口进入 worker9 的 `ens2`。
4. VM 的 `ens2` 对应宿主机 `vnet50`，帧进入 `virbr0`。
5. `virbr0` 广播该 ARP 到同一 bridge 上的其他 vnet，包括 `vnet44`。
6. worker3 的 `ens2` 收到该帧，macvlan 将其送到 `as2brd-r101` 的 `net_100_101`。
7. `as2brd-r101` 回 ARP reply：`10.2.0.253` 的 MAC 是 `62:07:87:bc:94:4b`。
8. 后续 ICMP 包按 FDB 学到的 MAC 直接从 `vnet50` 转发到 `vnet44`。

这条流量不需要 Flannel 封装，也不是通过 Pod `eth0` 的 `10.42.x.x` 地址通信。

## 7. 与 Flannel/eth0 的关系

Pod 的 `eth0` 仍然存在：

```text
as2brd-r100 eth0 10.42.3.212
as2brd-r101 eth0 10.42.73.174
```

它用于 Kubernetes 默认 Pod 网络。比如 `kubectl exec`、K8s 控制面访问、默认 Pod-to-Pod 通信，会使用 `eth0` 和 K3s/Flannel 的路由。

但是 SEED 仿真链路使用的是：

```text
net_100_101 10.2.0.254/24
net_100_101 10.2.0.253/24
```

也就是 Multus/macvlan 创建的额外接口。仿真路由表中可以看到 BIRD 也在使用这些接口：

```bash
kubectl --kubeconfig /home/lxl/k8s/output/kubeconfigs/seedemu-k3s.yaml \
  -n seedemu-k3s-real-topo \
  exec as2brd-r100-10.100.0.2-7bf4dbc79c-wrgmq -- ip route
```

示例：

```text
10.2.0.0/24 dev net_100_101 proto kernel scope link src 10.2.0.254
10.0.0.2 via 10.2.0.253 dev net_100_101 proto bird
10.101.0.0/24 via 10.2.0.253 dev net_100_101 proto bird
```

## 8. 重要限制和理解点

当前所有 NAD 都使用：

```json
"master": "ens2"
```

因此所有 SEED 仿真二层网络最终都落在同一个底层二层承载上：

```text
VM ens2 -> host vnetX -> virbr0
```

NAD 名字不同、IP prefix 不同，并不等价于底层二层隔离。也就是说：

```text
net-2-net-100-101
net-150-net0
net-ix-ix100
```

这些逻辑网络都通过 `ens2/virbr0` 传帧。它们主要靠不同 IP prefix 和仿真路由逻辑区分。如果两个实验在同一个 namespace 或同一个底层二层域里复用相同 IP，可能出现 ARP 冲突或非预期通信。

如果将来要做更强隔离，可以考虑：

```text
不同实验使用不同 parent interface
不同实验使用不同 libvirt bridge
不同实验使用 VLAN
不同实验使用不同 K8s cluster/node pool
```

## 9. 常用排查命令

查看 node：

```bash
kubectl --kubeconfig /home/lxl/k8s/output/kubeconfigs/seedemu-k3s.yaml get nodes -o wide
```

查看 Pod 调度在哪个 node：

```bash
kubectl --kubeconfig /home/lxl/k8s/output/kubeconfigs/seedemu-k3s.yaml \
  -n seedemu-k3s-real-topo get pods -o wide
```

查看 NAD：

```bash
kubectl --kubeconfig /home/lxl/k8s/output/kubeconfigs/seedemu-k3s.yaml \
  -n seedemu-k3s-real-topo get network-attachment-definitions
```

查看某个 Pod 的 Multus 网络状态：

```bash
kubectl --kubeconfig /home/lxl/k8s/output/kubeconfigs/seedemu-k3s.yaml \
  -n seedemu-k3s-real-topo \
  get pod as2brd-r100-10.100.0.2-7bf4dbc79c-wrgmq \
  -o jsonpath='{.metadata.annotations.k8s\.v1\.cni\.cncf\.io/network-status}'
```

查看 Pod 内接口：

```bash
kubectl --kubeconfig /home/lxl/k8s/output/kubeconfigs/seedemu-k3s.yaml \
  -n seedemu-k3s-real-topo \
  exec as2brd-r100-10.100.0.2-7bf4dbc79c-wrgmq -- ip -br addr
```

查看 VM 接到哪个宿主机 tap：

```bash
virsh domiflist seed-k3s-worker9
virsh domiflist seed-k3s-worker3
```

查看 tap 是否挂在 `virbr0`：

```bash
bridge link show | egrep 'vnet44|vnet50'
```

查看 `virbr0` 学到的 Pod macvlan MAC：

```bash
bridge fdb show br virbr0 | egrep '66:97:ed:5c:6a:cc|62:07:87:bc:94:4b'
```

验证仿真链路：

```bash
kubectl --kubeconfig /home/lxl/k8s/output/kubeconfigs/seedemu-k3s.yaml \
  -n seedemu-k3s-real-topo \
  exec as2brd-r100-10.100.0.2-7bf4dbc79c-wrgmq -- ping -c 2 10.2.0.253
```
