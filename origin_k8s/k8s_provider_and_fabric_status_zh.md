# K8sPre 当前状态与多物理机网络方案评估

本文记录当前 SeedEMU/K8sPre 在两类环境中的实际状态：

- 已经比较成熟的 KVM VM 方式
- 正在评估的两台真实物理机方式

核心结论：KVM 和物理机应该在“节点准备阶段”解耦，但在 `configK3s.yaml` 之后复用同一套 K3s 构建、registry、running、build/up 流程。SeedEMU 仿真网络需要单独抽象成 fabric backend，不能混在 K3s 安装逻辑里。

## 1. 当前 KVM 方案实际情况

当前已验证的主路径是：

```text
kvm.yaml
  -> create KVM VMs
  -> tune VM OS limits
  -> generate configK3s.yaml
  -> build K3s cluster
  -> write kubeconfig / inventory
  -> running make preflight/build/up
```

KVM 场景的特点：

- VM 由脚本创建，名字、IP、MAC、磁盘路径都可控。
- VM 默认位于 libvirt/KVM 管理的虚拟网络中。
- K3s 节点本质上是 VM，不是物理宿主机。
- 过去的 9node/12node 实验都是这种模式。
- SeedEMU 的 Multus/macvlan 父接口默认沿用了 KVM 场景中的接口命名，例如 `ens2`。

KVM 阶段应该保留为独立 provider：

- `createKvmVms.sh`：只负责创建 VM。
- `destroyKvmVms.sh`：只负责清理本轮 VM、磁盘、cloud-init、DHCP reservation。
- `prepareHostAssets.sh`：主要是 KVM cloud image 和镜像缓存准备。
- `manageKvmConfig.py`：只解析 KVM 创建配置和生成 VM 状态。

KVM 阶段最终产物应该是统一的 `configK3s.yaml`。一旦进入 `configK3s.yaml`，后续 K3s 构建流程不应该关心节点来自 KVM 还是物理机。

## 2. 当前两台真实物理机情况

当前真实物理机配置文件：

```text
/home/lxl/k8s/origin_k8s/test/configK3s.yaml
```

节点现状：

```text
amd:
  role: master
  ip: 10.202.236.88
  user: lxl
  key: /home/lxl/.ssh/id_ed25519
  current primary interface: eno8403

idc:
  role: worker
  ip: 10.202.191.39
  user: seed
  key: /home/lxl/.ssh/id_ed25519
  current primary interface: enp101s0f1np1
```

已确认：

- `amd` 本机没有安装宿主机级 K3s。
- `idc` 物理机没有安装宿主机级 K3s。
- 之前已有 K3s 集群是在 KVM VM 中，不是在这两台物理宿主机系统中。
- `idc` 已经配置为可使用 `/home/lxl/.ssh/id_ed25519` 免密登录。
- `idc` 上 `sudo -n` 可用。
- Ansible 对 `amd` local master 和 `idc` worker 的非破坏性 `ping` 验证通过。

已修改 `seedemu.k8spre` 以支持：

- `nodes[].connection: local|ssh`
- 当 master IP 属于本机且 `ssh.user` 等于当前用户时，自动识别为 local master
- local master 不再 SSH-to-self，而是本地执行 registry、镜像导入、kubeconfig 读取等操作
- running 阶段按每个 node 独立解析 SSH user/key/connection，不再假设所有节点使用 master 的账号

## 3. 当前面临的核心问题

### 3.1 K3s 控制集群可以构造，但默认网络参数不适合物理机

两台机器管理网络如下：

```text
amd: 10.202.236.88/24 via eno8403
idc: 10.202.191.39/24 via enp101s0f1np1
```

两者通过网关三层互通：

```text
amd -> idc: via 10.202.236.1
idc -> amd: via 10.202.191.1
```

因此真实物理机场景下，K3s 的 flannel backend 不建议继续使用 `host-gw`。`host-gw` 更适合节点处于同一二层网络的场景。跨三层物理机更适合使用 `vxlan` 作为 K3s Pod `eth0` 网络。

### 3.2 SeedEMU 的 Multus/macvlan 仿真网络仍缺少统一二层 fabric

当前 `cni.defaultMasterInterface` 缺省为：

```yaml
cni:
  defaultMasterInterface: ens2
```

但现场真实接口是：

```text
amd: eno8403
idc: enp101s0f1np1
```

并且两者不在同一个 `/24`，是三层路由互通。直接把 macvlan 挂到这些管理口上，并不能得到一个可靠的跨节点仿真二层网络。

对于 SeedEMU 仿真网络，必须额外准备一个所有 K3s 节点都可见、且语义一致的父接口，例如：

```text
br-seedemu
```

然后配置：

```yaml
cni:
  defaultMasterInterface: br-seedemu
```

### 3.3 不能把 KVM 和物理机流程完全复制成两套

如果复制两套 K3s 安装逻辑，会导致：

- registry 初始化逻辑重复
- K3s 参数和 tuning 逻辑重复
- running/preflight/build/up 逻辑重复
- 后续修复容易漏一套

更合理的边界是：

```text
KVM provider / Physical provider
  -> produce configK3s.yaml
  -> common K3s builder
  -> common running flow
```

## 4. 推荐架构

建议把系统拆成三个层次。

### 4.1 Node Provider

负责“节点从哪里来”。

```text
provider: kvm
  - create VM
  - allocate name/ip/mac
  - create disk/cloud-init
  - tune VM limits
  - generate configK3s.yaml

provider: physical
  - read existing machine list
  - prepare SSH key access
  - validate sudo / required tools / network
  - optionally prepare fabric interface
  - use user-written configK3s.yaml
```

### 4.2 Common K3s Builder

只消费 `configK3s.yaml`。

```text
configK3s.yaml
  -> manageK3sConfig.py
  -> temporary Ansible inventory
  -> applyK3sCluster.sh
  -> ansible/k3s-install.yml
  -> registry / bootstrap images / kubeconfig / inventory
```

这一层应该同时支持：

- KVM VM 节点
- 真实物理机节点
- local master
- remote SSH master
- worker 使用不同 SSH user/key

### 4.3 Simulation Fabric

负责 SeedEMU 仿真接口挂在哪个二层网络上。

建议作为单独配置：

```yaml
fabric:
  type: physical-vlan
  bridgeName: br-seedemu
  parentInterface: enoX
  vlanId: 3000
```

或者：

```yaml
fabric:
  type: ovs-vxlan
  bridgeName: br-seedemu
  vni: 3000
```

K3s/Multus 只消费最终结果：

```yaml
cni:
  defaultMasterInterface: br-seedemu
```

## 5. 几种网络方案建议

### 5.1 physical-vlan

这是大规模物理机最推荐方案。

结构：

```text
all physical nodes
  -> same dedicated VLAN / L2 broadcast domain
  -> local br-seedemu
  -> Multus/macvlan parent = br-seedemu
```

优点：

- 性能最好
- 节点侧逻辑简单
- 不需要 overlay tunnel
- 最接近 KVM 虚拟二层语义

缺点：

- 需要交换机/VLAN 权限
- 交换机需要允许大量 MAC、ARP、广播和未知单播
- 大规模时仍需要注意 L2/FDB 压力

当前现场状态：

- `amd` 有一个空闲口 `eno8303`，但无 carrier。
- `idc` 有多个空闲口，但多数无 carrier。
- 目前没有确认存在可用的同一二层专用链路。

因此此方案需要先准备交换机 VLAN 或专用线缆/端口。

### 5.2 Linux bridge + VXLAN

适合 2-5 台小规模测试。

结构：

```text
br-seedemu
  -> vxlan device
  -> management IP network
```

优点：

- 不需要交换机 VLAN 权限
- 可以跨三层管理网络
- Linux 原生命令即可实现

缺点：

- 多节点时容易变成 full mesh
- N 台机器需要大量 tunnel 或复杂脚本
- 广播复制、FDB 学习、故障排查会变复杂
- 不建议作为几十台物理机的默认方案

结论：可以作为当前两台机器的 PoC，但不建议作为大规模长期方案。

### 5.3 OVS VXLAN

OVS 可以用 VXLAN/Geneve tunnel 把多个物理机连成逻辑二层。

适合：

- 需要比原生 Linux VXLAN 更好的可观测性和管理能力
- 小到中等规模 overlay
- 未来可能接 OVN 控制面

优点：

- 比裸 bridge+vxlan 更适合演进
- 可以观察 OVS bridge、port、flow、FDB
- 后续能接 OVN

缺点：

- 仍然是 overlay，有封装开销
- 如果没有控制面，多节点仍需要管理 tunnel
- 运维复杂度高于 Linux bridge

结论：可以作为“无 VLAN 权限时”的中间方案，但几十台物理机不建议只靠手写 OVS VXLAN full mesh。

### 5.4 OVN

适合几十台物理机并且没有 VLAN 权限的长期方案。

结构：

```text
OVN central
  -> ovn-controller on each node
  -> OVS integration bridge
  -> logical switch / logical port
```

优点：

- 更适合多节点管理
- 不需要手写 full mesh VXLAN
- 能管理逻辑交换机、逻辑端口、tunnel
- 后续可支持多实验隔离

缺点：

- 引入 OVN central，复杂度明显增加
- 调试门槛更高
- 性能低于 physical VLAN

结论：如果未来目标是几十台物理机且无法配置交换机/VLAN，OVN 是更合理的长期 overlay 方向。

## 6. 建议的演进路线

### 阶段一：保持 KVM provider 稳定

继续保留当前 KVM 流程：

```text
writeKvmInstallScripts()
installKvmVms()
```

KVM 只负责创建 VM 和生成 `configK3s.yaml`。

### 阶段二：新增 physical provider

新增：

```text
writePhysicalNodeScripts()
preparePhysicalNodes()
validatePhysicalNodes()
```

职责：

- 检查 SSH key
- 可选引导免密
- 检查 `sudo -n`
- 检查 Docker/K3s 前置条件
- 检查网络接口
- 不安装 K3s
- 不修改 KVM 资源

### 阶段三：统一 K3s builder

保留并强化：

```text
writeK3sBuildScripts()
buildK3sCluster()
```

它只读取 `configK3s.yaml`，支持 KVM 和物理机。

### 阶段四：新增 fabric backend

先实现：

```text
fabric.type = physical-vlan
fabric.type = linux-vxlan
```

后续再考虑：

```text
fabric.type = ovs-vxlan
fabric.type = ovn
```

### 阶段五：running 完全复用

running 不应该关心节点来源。

```text
writeRunningScripts()
make preflight
make build
make up
make clean
```

只依赖：

- kubeconfig
- registry
- compile output
- node access info

## 7. 当前行动建议

短期：

1. 不要直接在两台物理机上跑大规模实验。
2. 先决定是否能拿到交换机 VLAN/专用二层。
3. 如果能拿到 VLAN，优先实现 `physical-vlan + br-seedemu`。
4. 如果拿不到 VLAN，用 `linux-vxlan` 或 `ovs-vxlan` 做两台机器 PoC。
5. K3s 物理机场景建议把 flannel backend 改为 `vxlan`。

中期：

1. 把 KVM provider 和 physical provider 在 API 上拆开。
2. 继续复用 common K3s builder。
3. 增加 fabric backend 配置。
4. 文档中明确：K3s Pod 网络和 SeedEMU 仿真网络是两层不同网络。

长期：

1. 大规模物理机优先争取 physical VLAN。
2. 无 VLAN 权限时再评估 OVN。
3. 不建议几十台机器用手写 Linux bridge + VXLAN full mesh 作为长期方案。

## 8. 两台物理机 Linux VXLAN + bridge PoC 结果

测试时间：2026-05-20。

测试目标：

- 验证 `amd` 和 `idc` 是否能通过现有三层管理网络建立 Linux VXLAN。
- 验证 VXLAN-backed bridge 是否能承载一个统一二层接口。
- 验证 macvlan 挂到该 bridge 上是否能跨节点通信，模拟 Multus/macvlan 后续使用方式。

测试使用的临时资源：

```text
bridge: brseed0
vxlan: vxseed0
macvlan: macseed0
vni: 4242
dstport: 4789

amd underlay:
  ip: 10.202.236.88
  dev: eno8403

idc underlay:
  ip: 10.202.191.39
  dev: enp101s0f1np1
```

### 8.1 bridge IP 测试

临时配置：

```text
amd brseed0: 172.31.252.1/30
idc brseed0: 172.31.252.2/30
```

结果：

```text
amd -> idc: 3/3 packets received, 0% loss
idc -> amd: 3/3 packets received, 0% loss
```

结论：两台物理机之间的三层管理网络可以承载 Linux VXLAN，VXLAN-backed bridge 本身可通信。

### 8.2 macvlan-on-bridge 测试

临时配置：

```text
amd macseed0@brseed0: 172.31.253.1/30
idc macseed0@brseed0: 172.31.253.2/30
```

结果：

```text
amd -> idc macvlan: 3/3 packets received, 0% loss
idc -> amd macvlan: 3/3 packets received, 0% loss
```

结论：macvlan 挂在 VXLAN-backed Linux bridge 上，在两台服务器之间可以通信。这说明“`br-seedemu` 作为 Multus/macvlan 父接口，底层由 Linux VXLAN 跨物理机互联”的设计在当前两台机器上是可行的。

### 8.3 清理结果

测试结束后已删除两台机器上的所有临时接口：

```text
macseed0
brseed0
vxseed0
```

清理验证结果：

```text
amd: macseed0/brseed0/vxseed0 do not exist
idc: macseed0/brseed0/vxseed0 do not exist
```

### 8.4 经验和注意事项

第一次测试失败的原因不是 VXLAN 不通，而是接口名过长：

```text
vxlan-seedemu-test
```

Linux 网络接口名长度限制通常是 15 字符，因此改用短名：

```text
brseed0
vxseed0
macseed0
```

如果后续做成正式脚本，接口名必须控制在 15 字符以内。

### 8.5 对架构建议的影响

两台物理机 PoC 结果支持短期方案：

```yaml
fabric:
  type: linux-vxlan
  bridgeName: br-seedemu
  vni: 4242
```

并让 K3s/Multus 消费：

```yaml
cni:
  defaultMasterInterface: br-seedemu
```

但是该结论只说明两台机器可行，不代表几十台机器适合裸 Linux VXLAN full mesh。对于几十台物理机，仍建议优先：

1. `physical-vlan`
2. 如果没有 VLAN 权限，再评估 `OVN`
3. `linux-vxlan` 作为小规模 PoC 或临时方案
