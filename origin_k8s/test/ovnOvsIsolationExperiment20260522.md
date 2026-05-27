# OVN/OVS 二层隔离实验记录

## 实验目标

验证真实物理机 `amd + idc` 上的 OVN/Kube-OVN 版本是否实现了比 `macvlan + shared bridge` 更严格的 per-network 二层隔离。

重点问题：

- 同一个仿真网络内的 ARP 广播是否能被 peer Pod 收到。
- 不同仿真网络的 Pod 是否会收到这条 ARP 广播。
- 跨物理节点时，OVN/OVS 是否仍能保持同网可达、异网隔离。

## 集群与部署结果

使用目录：

- setup: `/home/lxl/k8s/origin_k8s/test/k8spre-ovn-example/setup`
- running: `/home/lxl/k8s/origin_k8s/test/k8spre-ovn-example/running`
- kubeconfig: `/home/lxl/k8s/origin_k8s/test/k8spre-ovn-example/setup/seedemu-k3s.kubeconfig.yaml`
- namespace: `seedemu-k3s-real-topo`

执行结果：

- `./buildK3sCluster.sh` 成功构造 K3s + Kube-OVN 集群。
- `make preflight` 通过，识别 `network_backend=kube-ovn`。
- `make build` 成功使用 BuildKit/buildx 构建并推送镜像到 `10.202.236.88:5000`。
- `make up` 成功部署 `57` 个 Pod，全部 `Running`。

集群状态：

```text
amd   Ready   control-plane,master   10.202.236.88
idc   Ready   worker                 10.202.191.39
```

Kube-OVN 资源：

```text
NAD=29
Subnet=29
VPC=1
Pod Running=57
```

## 关键配置现象

`make up` 生成的是 Kube-OVN backend，不再是 macvlan NAD。

例如 `net-2-net-101-102`：

```json
{
  "type": "kube-ovn",
  "server_socket": "/run/openvswitch/kube-ovn-daemon.sock",
  "provider": "net-2-net-101-102.seedemu-k3s-real-topo.ovn"
}
```

同时它对应一个独立 Kube-OVN Subnet：

```text
provider=net-2-net-101-102.seedemu-k3s-real-topo.ovn
cidr=10.2.1.0/24
vpc=vpc-f3d0a3be-seedemu-k3s-real-topo
```

这意味着每个 SeedEMU 网络不再直接挂到同一个物理二层或同一个 Linux bridge 上，而是由 OVN logical switch / Kube-OVN Subnet 表达。

## 单节点隔离测试

测试网络：`net-2-net-101-102`

源 Pod：

```text
as2brd-r101-10.101.0.2-6bf8dbf6c9-q6wv4
interface=net_101_102
ip=10.2.1.254/24
node=amd
```

同网观察 Pod：

```text
as2brd-r102-10.102.0.2-867df9c59c-l868q
interface=net_101_102
ip=10.2.1.253/24
node=amd
```

非同网观察 Pod：

```text
as160brd-router0-10.160.0.254-6467f6cc9b-dddft
interface=net0
ip=10.160.0.254/24
node=idc

as150h-host-0-10.150.0.71-799bdc44d5-4t2qh
interface=net0
ip=10.150.0.71/24
node=amd
```

命令：

```bash
/home/lxl/k8s/origin_k8s/test/validateMacvlanArpVisibility.sh \
  --kubeconfig /home/lxl/k8s/origin_k8s/test/k8spre-ovn-example/setup/seedemu-k3s.kubeconfig.yaml \
  --namespace seedemu-k3s-real-topo \
  --source-pod as2brd-r101-10.101.0.2-6bf8dbf6c9-q6wv4 \
  --source-interface net_101_102 \
  --target-ip 10.2.1.200 \
  --observer same-subnet-peer:as2brd-r102-10.102.0.2-867df9c59c-l868q:net_101_102 \
  --observer nonprefix-router:as160brd-router0-10.160.0.254-6467f6cc9b-dddft:net0 \
  --observer nonprefix-host:as150h-host-0-10.150.0.71-799bdc44d5-4t2qh:net0 \
  --capture-seconds 12 \
  --output-dir /home/lxl/k8s/origin_k8s/test/ovn-arp-isolation-20260522-unused
```

结果：

| Observer | Interface | Saw ARP for `10.2.1.200` |
| --- | --- | --- |
| same-subnet-peer | `net_101_102` | yes |
| nonprefix-router | `net0` | no |
| nonprefix-host | `net0` | no |

结论：同一 OVN Subnet 内能看到 ARP 广播，不同 Subnet 的 Pod 看不到。

## 跨节点隔离测试

测试网络：`net-154-net0`

这个网络天然跨物理节点：

```text
seedemu-k3s-real-topo/net-154-net0
  amd: as154brd-router0, as154h-host-0
  idc: as154h-host-1
```

源 Pod：

```text
as154brd-router0-10.154.0.254-5b6fdc6f7-phgm7
interface=net0
ip=10.154.0.254/24
node=amd
```

同网跨节点观察 Pod：

```text
as154h-host-1-10.154.0.72-69fbf6686-nbh4f
interface=net0
ip=10.154.0.72/24
node=idc
```

非同网观察 Pod：

```text
as150h-host-0-10.150.0.71-799bdc44d5-4t2qh
interface=net0
ip=10.150.0.71/24
node=amd

as160brd-router0-10.160.0.254-6467f6cc9b-dddft
interface=net0
ip=10.160.0.254/24
node=idc
```

命令：

```bash
/home/lxl/k8s/origin_k8s/test/validateMacvlanArpVisibility.sh \
  --kubeconfig /home/lxl/k8s/origin_k8s/test/k8spre-ovn-example/setup/seedemu-k3s.kubeconfig.yaml \
  --namespace seedemu-k3s-real-topo \
  --source-pod as154brd-router0-10.154.0.254-5b6fdc6f7-phgm7 \
  --source-interface net0 \
  --target-ip 10.154.0.200 \
  --observer cross-node-same-subnet:as154h-host-1-10.154.0.72-69fbf6686-nbh4f:net0 \
  --observer same-node-nonprefix:as150h-host-0-10.150.0.71-799bdc44d5-4t2qh:net0 \
  --observer cross-node-nonprefix:as160brd-router0-10.160.0.254-6467f6cc9b-dddft:net0 \
  --capture-seconds 12 \
  --output-dir /home/lxl/k8s/origin_k8s/test/ovn-arp-isolation-20260522-crossnode
```

结果：

| Observer | Interface | Node | Saw ARP for `10.154.0.200` |
| --- | --- | --- | --- |
| cross-node-same-subnet | `net0` | idc | yes |
| same-node-nonprefix | `net0` | amd | no |
| cross-node-nonprefix | `net0` | idc | no |

结论：跨物理节点的同一 OVN Subnet 能收到 ARP 广播；同节点和跨节点的其他 Subnet 都没有收到。

## 总结结论

本轮 OVN/OVS 版本实现了 per-network L2 隔离。

更精确地说：

- 同一个 SeedEMU 网络被映射为一个独立 Kube-OVN Subnet / provider。
- 同一 Subnet 内的 ARP broadcast 会被 peer Pod 看到，包括跨物理节点场景。
- 不同 Subnet 的 Pod 没有看到目标 ARP broadcast。
- 这与 KVM/macvlan 共享 `ens2/virbr0` 的结果不同；KVM/macvlan 实验中非目标网络 Pod 也能看到目标 ARP，说明那种方式没有严格 per-network L2 隔离。

剩余边界：

- 本实验验证的是代表性 ARP broadcast 可见性，不等价于形式化证明所有二层帧都无法跨 Subnet。
- 当前所有 Subnet 在同一个 Kube-OVN VPC 下，L3 路由策略仍需按后续实验单独评估。
- 如果后续启用 NetworkPolicy、ACL、不同 VPC 或 provider 复用策略，隔离语义会随配置变化。
