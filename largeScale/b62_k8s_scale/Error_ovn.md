# B62 4954 OVN/OVS 与 eBGP 异常分析

本文记录 B62 4954 规模实验中出现的 eBGP 邻接异常、Kube-OVN/OVS
部署阻塞现象、逐步排查过程、最终判断以及后续改进方式。重点是区分
“BIRD 协议未启动或配置错误”和“secondary network 数据面不通/OVS
扩展性瓶颈”这两类问题。

## 1. 问题背景

B62 4954 规模实验需要在 K3s 上部署 4954 个 SeedEMU router-like Pod。
每个 Pod 通过 Multus 挂载额外网络接口，BIRD 在这些接口上建立 OSPF、
iBGP、eBGP，并在 `start-kernel` 阶段把 BIRD RIB 中的路由导出到 Linux
kernel。

实验中先后测试过两类网络路径：

- `macvlan + Kube-OVN IPAM`：NetworkAttachmentDefinition 使用
  `type: macvlan`，master 为 VM 内的 `ens2`，Kube-OVN 只负责 IPAM 和
  Subnet/IP CR 管理。
- `pure Kube-OVN attached network`：NetworkAttachmentDefinition 使用
  `type: kube-ovn`，每个 SeedEMU link 对应 Kube-OVN Subnet/OVN logical
  switch/OVS port，数据面走 Kube-OVN/OVS。

这两个模式产生的故障现象不同，不能混为一个根因。

## 2. 早期 eBGP 异常现象

在一次 4954 运行中，BIRD 已经启动，部分路由也能看到，但抽样发现部分
eBGP 邻接没有起来。例如：

- 某些协议处于 `Connect` 或 `Active`，而不是 `Established`。
- `as1269brd-r2` 到 `as3506brd-r2` 的邻接仍为 `Connect`，并且对端地址
  `1.2.13.178` ping 不通。
- 另一个检查中，AS1850 到 AS1824 的 kernel route 存在，但同 IX 上的 TCP
  建连测试双向失败。
- 同一个 AS1850 还能连接另一个已经 `Established` 的 eBGP 邻居，说明
  AS1850 的 BIRD 进程和接口并非全局不可用。

这说明异常不是“所有 BIRD 没启动”，也不是单纯的 BGP timer 问题。更直接的
证据是：在直连邻居 TCP 建连之前，底层接口连通性已经不稳定。因此优先怀疑
secondary interface 数据面，而不是先调 `ospf hello/dead` 或 BGP 参数。

## 3. 当时排除过的可能性

### 3.1 不是 BIRD 进程未启动

`start-bird` 日志显示 BIRD 可以在大量 Pod 中启动；后续 4954 成功运行里
也记录到 `start-bird` 启动了 `4954/4954` 个 BIRD 进程。因此 eBGP 不通
不能简单归因于 BIRD 没有启动。

### 3.2 不只是 BIRD 协议参数问题

问题发生在直连 IX 邻居之间的 ping/TCP 建连阶段。如果两个 Pod 在同一个
IX 网络上已经无法互通，则即使调整 OSPF tick、hello、dead 或 BGP keepalive，
也无法让 TCP session 建起来。因此这些协议 timer 不是第一修复点。

### 3.3 早期 kernel.conf 处理确实带来过干扰，但不是全部问题

早期脚本曾把 `/etc/bird/conf/kernel.conf` 移成
`kernel.conf.disabled-before-start-kernel`，导致 `start-bird` 后 kernel
protocol 没有按预期加载。这个问题后来已经修复：`start-bird` 不再移走
`kernel.conf`，`start-kernel` 负责切换导出策略并 reload BIRD。

但是，kernel.conf 问题只能解释“kernel route 没导出”或 route-count 测试在
`start-kernel` 前失败，不能解释同 IX 邻居底层 ping/TCP 不通。因此它是一个
需要修的脚本问题，但不是 OVN/OVS 数据面异常的完整解释。

## 4. 对 pure Kube-OVN attached 模式的复测现象

为了判断是否应该从 macvlan 切换到真正的 OVN/OVS 数据面，我们把
`assignment.yaml` 中的 attached 网络切到 `kube-ovn`。这次实验没有跑到
`start-bird`，而是在 deploy 阶段被阻塞。

当时的关键现象如下：

- 4954 规模会生成约 4763 个 Kube-OVN `Subnet` CR。
- OVN NB 中会出现约 4765 个 logical switch、4774 个 logical switch port、
  4777 条 logical router policy、4764 条 static route。
- 脚本已经看到 Subnet CR 全部 `Ready`，但 `kube-ovn-controller` 和
  `ovn-northd` 仍在处理大量 route/policy 工作。
- 即使把 workload 创建节流到很保守的程度，第一个 workload Pod 仍卡在
  `ContainerCreating`。
- kubelet 事件反复出现：
  - `ovs interface <id>_net1_h is not ready after 30s`
  - `ovs-vsctl --timeout=30 ... signal: alarm clock`
  - `add nic to ovs failed context canceled by timeout`
- OVS 侧可见 transient interface 的 `ofport` 为空，`ovs-vswitchd` 出现长
  poll interval 和 RCU block，说明 `br-int` 上 add-port/端口就绪路径压力
  很高。

这些现象说明 pure Kube-OVN attached 模式的失败点发生在 Pod secondary
interface 加入 OVS/OVN 数据面的路径上，而不是 BIRD 阶段。

## 5. 逐步论证

### 5.1 为什么不是普通 deploy 并发太高

当时已经把 workload deploy 调得很保守：

- `DEPLOY_BATCH_SIZE=6`
- warmup 为 `12 x 1`
- `DEPLOY_MAX_CREATING_PODS=0`
- `ovn.cniOvsVsctlConcurrency=1`
- Subnet 全部 Ready 后还等待了 900 秒 cooldown

在这种设置下，第一个 workload Pod 仍然无法完成 CNI ADD。这说明问题不是
“一次创建太多 Pod”，而是 OVN/OVS 在已经存在几千个 logical object 的情况下，
单个 Pod 的 secondary interface add-port 操作也无法在 Kube-OVN 固定的 30 秒
窗口内稳定完成。

### 5.2 为什么不是 Subnet CR 参数错误

Subnet CR 能够创建并进入 Ready；问题出现在 Pod CNI ADD 时的 OVS interface
就绪检查。也就是说，Kube-OVN 控制面对象不是语法错误或字段错误，而是在大规模
对象数量下，控制面和 OVS 数据面处理速度无法满足 CNI 同步操作的 timeout。

### 5.3 为什么 controller 参数优化没有解决

曾在 live cluster 中临时关闭部分非核心 controller 功能，例如 LB、NP、EIP/SNAT
相关功能，并重启 controller。随后删除卡住的第一个 Pod 让它重建，但新 Pod 仍然
卡在同一个 OVS add-port 路径上。

这说明问题不是某一个非核心 controller 功能导致的，而更接近于 pure Kube-OVN
attached 设计在本拓扑规模下产生了过多 OVN/OVS 对象，导致端口创建和就绪路径
超过 timeout。

## 6. 最终定论

最终判断分两层：

1. 早期 eBGP `Connect/Active` 的直接现象是某些 secondary network 数据面不通，
   不是 BIRD 没启动，也不是优先由 OSPF/BGP timer 导致。
2. pure Kube-OVN attached 模式在 4954 规模下的直接阻塞点是 OVS/OVN add-port
   和 interface ready 路径：几千个 SeedEMU link 被映射成几千个 OVN logical
   switch/route/policy 后，Kube-OVN/OVS 无法在 30 秒 CNI 操作窗口内稳定创建
   workload secondary interface。

因此，本轮 4954 实验不继续使用 pure OVN+OVS attached 数据面作为主路径。

## 7. 最终采用的改进方式

为了让 4954 规模 BIRD 实验先完整跑通，最终采用：

```yaml
networking:
  backend: kube-ovn
  cniType: macvlan
  cniMasterInterface: ens2
  localLinkCniType: macvlan
  attachedCniType: macvlan
```

这个方案的含义是：

- Multus secondary interface 由 `macvlan` 创建。
- Kube-OVN 保留为 IPAM/non-primary CNI 管理组件。
- 不再为每个 SeedEMU link 创建 pure OVN overlay datapath 上的逻辑端口路径。
- 避免 4763 个 SeedEMU 网络全部落到 OVS `br-int` add-port 路径上。

后续成功运行证明这个方案可以完成当前 BGP/RIB/kernel route 层面的实验：

- deploy 创建 `4954/4954` 个 Pod，0 Failed。
- `start-bird` 启动 `4954/4954` 个 BIRD 进程。
- `start-kernel` 切换 `4954/4954` 个 Pod。
- `test_kernel.sh` 抽样 10 个不同 AS 的 BRD Pod，全部满足
  `ip route | wc` 数量大于 `birdc show route count` 中的 network 数量。

## 8. 这个改进方式能否实现每个网络的二层隔离

不能严格实现。

当前 `macvlan + Kube-OVN IPAM` 方案能让每个 SeedEMU link 获得独立的 IP
前缀和独立的 NetworkAttachmentDefinition 配置，但它不是严格的“每个网络一个
独立二层广播域”。原因是：

- `macvlan` 的 lower device 都是 VM 内的同一个 `ens2`。
- `mode: bridge` 下，同一 lower device 上的 macvlan 端口共享底层二层承载。
- Kube-OVN 在这里主要负责 IPAM，Subnet 状态类似 `SetNonOvnSubnetSuccess`，
  不负责为每个 link 提供 OVN logical switch 级别的隔离。
- 因此不同 SeedEMU network 之间主要靠不同 IP 前缀、路由策略和 BIRD 控制面来
  维持实验语义，而不是靠 OVS/OVN logical switch 做严格二层隔离。

如果实验目标必须要求“每个 SeedEMU link 都是严格隔离的二层网络”，当前最终方案
不满足这个要求。可选方向包括：

- 修复 pure Kube-OVN attached 模式的规模瓶颈，让每个 link 仍映射到独立 OVN
  logical switch，但这需要解决 Kube-OVN/OVS 大量 logical object 下的 add-port
  timeout 和 northd/controller 处理压力。
- 使用 VLAN 或独立 Linux bridge 为每个 SeedEMU network 提供隔离，但 4763 个
  network 会带来宿主机、VM、CNI 和管理复杂度上的新压力。
- 对拓扑进行聚合或分片，减少每个 Kube-OVN VPC/OVS 实例承载的 logical switch
  数量，例如多 VPC、多集群、分批实验或多物理宿主机拆分。

因此，当前最终方案的准确定位是：它是为了跑通 4954 规模 BGP/route convergence
实验的实用方案，不是严格 per-network L2 isolation 的最终方案。

## 9. 后续建议

- 如果论文或报告需要强调二层隔离，不能把当前 macvlan 路径描述成严格 OVN
  per-link isolation。
- 如果只关注 BGP 会话、RIB 收敛和 kernel route 导出，当前方案已经能支撑
  4954 规模实验。
- 如果后续必须回到 pure OVN+OVS，需要先做小规模到中规模的递增测试，记录
  logical switch 数量、OVS add-port 耗时、`ovs-vsctl` timeout、`ovn-northd`
  CPU、`kube-ovn-controller` queue，以及第一个 Pod 到第 N 个 Pod 的 CNI ADD
  延迟曲线。

## 10. 2026-06-15 pure OVN+OVS 递增测试记录

本次新增了两个 profile 工具：

- `collectOvnMetrics.py`：采集目标 namespace Pod 状态、Kube-OVN Subnet/IP/VPC
  数量、OVN NB/SB logical object 数量、每个 OVS 节点的 `ovs-vsctl` 查询耗时、
  kube-system 中 OVN/OVS 组件 CPU/内存、`kube-ovn-controller` workqueue、CNI
  ADD latency counter，以及相关事件/日志中的 timeout。
- `runOvnScaleProfile.py`：按指定规模生成 pure Kube-OVN assignment，执行
  clean/preflight/compile/build/deploy，并在 deploy 期间持续采样。

测试命令：

```bash
cd /home/lxl/k8s/largeScale/b62_k8s_scale
python3 runOvnScaleProfile.py --scales 1897 2599 3083 4192 --metrics-interval 20 --clean-after --stop-on-fail
```

### 10.1 1897 规模结果

运行目录：

```text
/home/lxl/k8s/largeScale/b62_k8s_scale/runs/ovn_profile_20260615_130537_1897_w5
```

阶段耗时：

| 阶段 | 结果 | 耗时 |
| --- | --- | --- |
| clean | PASS | 2.35s |
| preflight | PASS | 5.35s |
| compile | PASS | 23.32s |
| build | PASS | 101.72s |
| deploy | INTERRUPTED | 432.32s |

deploy 被手动中断的原因：这轮运行已经连续多轮卡在 Kube-OVN Subnet wait
阶段，没有进入 workload Pod 创建阶段。这个中断不是 36000s timeout 触发，也不
等同于证明 1897 规模 pure OVN+OVS 永远不能跑通；它只能说明“本轮配置/本轮集群
状态下，Subnet reconcile 在观察窗口内异常停滞”。

关键指标：

| 指标 | 观测值 |
| --- | --- |
| 目标 namespace | `seedemu-b62-ovn-1897` |
| Kube-OVN Subnet CR | 1876 |
| Subnet processed | 21 |
| Subnet Ready | 18 |
| Pod 数量 | 0 |
| IP CR 数量 | 0 |
| OVN NB `Logical_Switch` | 20 |
| OVN NB `Logical_Switch_Port` | 26 |
| OVN NB `Logical_Router_Static_Route` | 978 |
| OVN SB `Port_Binding` | 46 |
| `kube-ovn-controller` `AddSubnet` queue | 1873 |
| `kube-ovn-controller` `UpdateSubnetStatus` queue | 747 |
| 近期 controller 慢 OVN NB 操作 | 9 次 |
| 慢 OVN NB 操作最大耗时 | 2013ms |
| `ovs-vsctl` 查询最大耗时 | 10ms |
| `ovs-vsctl` timeout 样本 | 0 |
| CNI ADD 增量样本 | 0 |
| OVS add-port 日志样本 | 0 |
| CNI/OVS 失败事件 | 0 |

组件 CPU 量级：

- `kube-ovn-controller` 约 600m。
- `ovn-central` 约 400m 到 520m。
- 每个 `ovs-ovn` Pod 约 40m 到 60m。

controller 日志中的典型现象：

```text
ovn-nb operations took too long: insert Logical_Router_Static_Route + mutate Logical_Router ... in 565ms~2013ms
```

### 10.2 本轮结论与停止原因

1897 规模的 pure OVN+OVS 没有到达 Pod CNI ADD 阶段，因此本轮没有出现
`ovs-vsctl add-port timeout`，也没有第一个 Pod 到第 N 个 Pod 的 CNI ADD 延迟
曲线。它更早地卡在：

```text
Kube-OVN Subnet CR -> OVN logical switch / logical router static route / subnet status
```

也就是说，在 pure OVN+OVS 模式下，每个 SeedEMU link 映射为 Kube-OVN Subnet
后，会快速形成大量 `AddSubnet` 和 `UpdateSubnetStatus` 队列。controller 对同一个
VPC/logical router 持续插入 static route 和更新 logical object，导致 Subnet
processed/Ready 进展极慢。此时 Pod 数量为 0，所以不是 Pod batch size、BIRD、
start-kernel 或 OVS add-port 阶段导致。

我停止这轮测试的直接原因是：

- deploy 已经应用完 1876 个 Subnet CR。
- `kube-ovn-subnets` wait 阶段从 `processed=18 ready=15` 只推进到
  `processed=21 ready=18`，随后连续多轮不再推进。
- `kube-ovn-controller` 的 `AddSubnet` queue 仍有 1873，`UpdateSubnetStatus`
  queue 继续累积到 747。
- Pod 数量为 0，说明还没有进入 workload/CNI 阶段。

因此当时选择中断，是为了避免在一个明显无进展的状态下继续等到 36000s。这个判断
是“当前 run 异常，需要先定位原因”，不是“1897 规模理论上不能跑通”。如果之前
1897 规模确实在 pure OVN+OVS 下完整跑通过，那么这次结果更应该被解释为一次
回归/状态差异：需要对比当时的 assignment、deploy wait 策略、Kube-OVN 参数、
controller worker/feature gates、集群重建状态和 Subnet CR 模板，而不能直接下
最终规模结论。

因此，本轮没有继续直接跑 2599、3083、4192。更合理的下一步是先复现“之前能跑通
的 1897 配置”，确认本轮卡住是否由配置差异、等待逻辑差异或当前集群状态造成。

### 10.3 为什么它和之前 4954 pure OVN 现象不完全一样

之前 4954 pure OVN 曾经到达过 Pod CNI ADD 阶段，并出现 OVS interface not ready
和 `ovs-vsctl` timeout。1897 这轮在更早阶段停住，原因是这次完整记录了 Subnet
wait 阶段，并且等待逻辑要求 1876 个 Subnet 都被 controller 处理完成。

两个现象并不矛盾：

- Subnet 数量较大时，第一层瓶颈是 controller 把 Subnet 转成 OVN logical object
  和 static route。
- 即便 Subnet eventually Ready，第二层瓶颈仍会是 Pod CNI ADD 时的 OVS/OVN
  add-port 和 interface ready。

所以 pure OVN+OVS 的限制不是单点，而是两段式的：

1. 大量 Subnet/route/logical switch 对 controller 和 OVN NB/northd 形成压力。
2. 大量 logical object 存在后，Pod secondary interface add-port 又对 OVS/OVN
   数据面形成压力。

### 10.4 下一步想解开 pure OVN+OVS 限制的方向

优先级最高的不是继续调大 timeout，而是减少单个 OVN 控制面实例里需要同步的
logical object 数量和单个 logical router 上的 route mutation 压力。

可选方向：

- **拓扑分片**：把一个 4954/4192/3083 规模实验拆成多个 Kube-OVN VPC、多个
  ovn-central/OVS 域，甚至多个 K3s 集群。目标是降低单个 controller queue 和
  单个 logical router 的 static route 数量。
- **路由聚合**：如果 SeedEMU link 地址分配允许聚合，减少每个 Subnet 对 logical
  router 插入一条 static route 的需求。当前日志表明 static route insert/mutate
  是明显慢点。
- **减少 per-link Subnet 数量**：不要把每条点到点 link 都直接映射成独立
  Kube-OVN Subnet。可以考虑把只用于 BGP 邻接的 link 做成更轻量的数据面对象，
  或按 AS/区域聚合后再在容器内部用路由语义区分。
- **控制器并发和限速分开调**：降低 `DEPLOY_SUBNET_BATCH_SIZE` 只会让 API 写入更
  平滑，但不能降低总工作量；提高 controller worker 也可能被同一个 logical
  router 的 OVN NB transaction 串行化限制。需要单独做参数矩阵，而不能只看
  deploy batch size。
- **预创建和分阶段稳定**：可以先只创建 Subnet/VPC，等待 OVN DB 和 northd 完全
  稳定，再创建 Pod。这有利于定位阶段瓶颈，但不从根本上降低规模压力。
- **回到 macvlan/VLAN/bridge 数据面**：如果实验目标主要是 BGP/RIB/kernel route，
  当前 macvlan 方案是已验证可跑通的实用路径；如果目标必须是严格二层隔离，则要
  用可扩展的 VLAN/bridge/OVN 分片设计替代单个 pure OVN 域。

### 10.5 对二层隔离的影响

理论上，pure OVN+OVS 中“每个 SeedEMU network 对应一个独立 OVN logical switch”
可以实现每个网络的二层隔离；但本轮结果说明，把 1876 个以上 logical switch/route
集中放进单个 Kube-OVN/VPC/ovn-central 域时，控制面已经不可接受地慢。

因此，想同时满足“严格二层隔离”和“几千网络规模”，下一步不能只使用单一
Kube-OVN VPC 承载所有 link，而应转向分片：

- 多 VPC 或多 logical router 分摊 static route mutation。
- 多 ovn-central/集群分摊 northd 和 OVSDB 压力。
- 或用 VLAN/Linux bridge 为不同网络提供隔离，并通过外部编排管理大量网络对象。

这类分片方案仍可以实现二层隔离，但隔离边界不再是“一个单体 pure OVN 域内塞入
所有 SeedEMU link”，而是“多个受控二层域/OVN 域共同承载拓扑”。

## 11. 2026-06-15 1078 规模 clean rebuild 后的 pure OVN+OVS 观察

本轮按“先重建环境，再从较小规模开始看 Subnet 行为”的思路执行：

```bash
python3 runOvnScaleProfile.py --scales 1078 --metrics-interval 10 --rebuild-first --clean-after --stop-on-fail
```

运行目录：

```text
/home/lxl/k8s/largeScale/b62_k8s_scale/runs/ovn_profile_20260615_143633_1078_w5
```

### 11.1 环境重建结果

这次不是复用旧的 K3s/OVN 状态，而是先销毁已有集群并重建 6 台 KVM VM、K3s、
Multus、Kube-OVN 和 OVS。关键阶段耗时如下：

- `destroy-existing-cluster`: 50.69s
- `prepare-libvirt-dhcp`: 8.97s
- `build-cluster`: 477.77s
- `compile`: 10.90s
- `build`: 65.15s
- `deploy`: 人工中断，已运行 1666.70s

中断后我执行了 `clean.sh`，用 127s 删除了 `seedemu-b62-ovn-1078` namespace、
1 个 Pod、1065 个 NAD、1065 个 Subnet、3 个 IP 和 1 个 VPC。清理后 6 个节点仍
为 Ready，Multus、`kube-ovn-controller`、`ovn-central`、`ovs-ovn` 和
`kube-ovn-cni` 均为 Running。

### 11.2 Subnet 阶段的实际变化

1078 规模下，本轮生成了 1065 个 Kube-OVN Subnet。和 1897 异常 run 不同，这次
Subnet 阶段最终能完全收敛：

- 14:47:34 CST 左右：`processed=12 ready=9`，随后进入短暂平台期。
- 14:49:10 CST 左右：`processed=15 ready=12`，`AddSubnet` queue 达到 1065。
- 14:52:35 CST 左右：开始明显推进，`processed=82 ready=79`。
- 14:53:59 CST 左右：`processed=589 ready=586`。
- 14:55:25 CST 左右：`processed=1065 ready=1065`。

这说明 clean rebuild 后，1078 规模的 Subnet reconcile 本身不是硬性失败点；它会
先积压，再由 controller/northd 集中消化。对应的资源峰值是：

- `kube-ovn-controller` CPU 峰值约 `1439m`。
- `ovn-central` CPU 峰值约 `2029m`。
- master 上的 `ovs-ovn` Pod CPU 峰值约 `1152m`。
- `AddSubnet` workqueue depth 峰值为 `1065`。

Subnet 完成后 OVN DB 规模大致为：

- `Logical_Switch`: 1067
- `Logical_Switch_Port`: 1076
- `Logical_Router_Static_Route`: 1066
- `Logical_Router_Policy`: 1079
- `Port_Binding`: 2143

### 11.3 后续 Pod/CNI 阶段的新现象

在 Subnet 全部 Ready 后，脚本按当前策略 cooldown 900s，然后开始创建 workload
Pod。第一个 Pod 曾经出现 CNI/OVS add-port 不稳定：

- `FailedCreatePodSandBox`: 3 次
- `ovs interface ... is not ready after 30s`: 2 次
- `ovs-vsctl --timeout=30 ... add-port br-int ... signal: alarm clock`: 1 次
- CNI 日志中记录到 10 条 `ovs-vsctl` timeout 相关日志，最长约 `30035ms`

我中断 profile 后再次查看，那个 Pod 已经通过重试变为 Running。因此这次 1078 的
结论不是“第一个 Pod 永久失败”，而是：

1. 1078 clean rebuild 后 Subnet 能收敛到 1065/1065 Ready。
2. Subnet 收敛以后，瓶颈转移到 Pod CNI ADD 阶段的 OVS add-port/interface ready
   延迟。
3. 该延迟在 1078 规模上可能通过 kubelet/CNI 重试恢复，但已经出现 30s 级别
   `ovs-vsctl` 超时；继续放大到 1897/2599/3083 时，很可能同时受到 Subnet
   reconcile 和 Pod CNI ADD 两段压力影响。

### 11.4 对下一步的判断

这次 1078 run 给出的信息比之前 1897 异常 run 更清楚：pure OVN+OVS 的限制不是
单纯的 Subnet 创建速度，而是“Subnet/route/logical switch 控制面收敛”加上
“Pod secondary interface add-port 数据面接入”两段压力。

下一步如果继续递增，建议不要直接跳到 2599 或 3083，而是先在 1078 到 1897 之间
增加一个中间点，并记录：

- Subnet 从 0 到全 Ready 的耗时。
- `AddSubnet` queue 清空时间。
- `ovn-central` 和 master `ovs-ovn` CPU 峰值。
- 第一个 Pod CNI ADD 的失败次数和最终 Running 时间。
- 是否出现 `ovs-vsctl --timeout=30`。

如果目标是解开 pure OVN+OVS 限制，而不是只确认失败点，仍然需要优先考虑分片：
多 VPC、多 logical router、多 ovn-central/集群，或者 VLAN/Linux bridge 隔离。单个
OVN 控制域里承载每条 SeedEMU link 一个 logical switch，虽然语义上能提供二层隔离，
但控制面和 OVS 接入路径都会随 logical object 数量快速变重。

## 12. 2026-06-15 OVN 资源加量后的 1078/1897 对照实验

本轮为了判断“给 OVN/Kube-OVN 更多资源是否能直接缓解 pure OVN+OVS 限制”，新建了
实验专用配置：

```text
/home/lxl/k8s/largeScale/b62_k8s_scale/assignment_ovn_tuned.yaml
```

它没有覆盖原始 `assignment.yaml`。主要调整包括：`northdThreads=8`、
`controllerWorkerNum=24`、`kube-ovn-controller` 资源提升到 request `4000m/2Gi`、
limit `16 CPU/8Gi`，`ovn-central` 提升到 request `4000m/4Gi`、limit
`24 CPU/16Gi`，`ovs-ovn` 提升到 request `2000m/4Gi`、limit `12 CPU/16Gi`，
`kube-ovn-cni` 提升到 request `1000m/1Gi`、limit `6 CPU/4Gi`。

### 12.1 1078 tuned 结果

命令：

```bash
python3 runOvnScaleProfile.py --assignment assignment_ovn_tuned.yaml --scales 1078 --namespace-template 'seedemu-b62-ovn-tuned-{scale}' --metrics-interval 10 --rebuild-first --clean-after --stop-on-fail
```

运行目录：

```text
/home/lxl/k8s/largeScale/b62_k8s_scale/runs/ovn_profile_20260615_155916_1078_w5
```

阶段结果：

- `destroy-existing-cluster`: 40.83s
- `prepare-libvirt-dhcp`: 8.96s
- `build-cluster`: 481.89s
- `compile`: 10.85s
- `build`: 62.22s
- `deploy`: 人工中断于 578.18s；中断点在 Subnet 已完成后进入 900s cooldown，
  尚未创建 workload Pod。

Subnet 现象和未 tuned 的 1078 很接近：

- 共 1065 个 Subnet。
- 早期长时间停在 `processed=14 ready=12` 左右。
- 之后开始批量追平，最终 `processed=1065 ready=1065`。
- 完成快照中 OVN DB 约为：`Logical_Switch=1067`、
  `Logical_Switch_Port=1073`、`Logical_Router_Static_Route=1066`、
  `Logical_Router_Policy=1079`、`Port_Binding=2140`。
- 采样峰值：`AddSubnet` queue 约 `1065`，`kube-ovn-controller` 约 `1473m`，
  `ovn-central` 约 `2193m`。

关键判断：1078 下即使给 controller/central 很高 limit，实际 CPU 也只用到约 1.5
核和 2.2 核，说明瓶颈不主要是 CPU limit 不够，而是 Subnet/VPC/OVN DB 的处理路径
本身存在明显前置串行成本。

### 12.2 1897 tuned 结果

命令：

```bash
python3 runOvnScaleProfile.py --assignment assignment_ovn_tuned.yaml --scales 1897 --namespace-template 'seedemu-b62-ovn-tuned-{scale}' --metrics-interval 10 --clean-after --stop-on-fail
```

运行目录：

```text
/home/lxl/k8s/largeScale/b62_k8s_scale/runs/ovn_profile_20260615_162744_1897_w5
```

阶段结果：

- `clean`: 1.69s
- `preflight`: 3.24s
- `compile`: 23.41s
- `build`: 81.26s
- `deploy`: 人工中断于 998.36s；中断时还在等待 Subnet，尚未创建 workload Pod。

这次有一个重要修正：1897 不是永久卡死。它的真实过程是：

1. 1876 个 Subnet 很快被创建出来。
2. 随后长时间停在 `processed=13 ready=12`，`AddSubnet` queue 接近 `1874`。
3. 这段时间 `Logical_Router_Static_Route` 从几百条持续增长到 `1877`，而
   `Logical_Switch` 长时间停在十几个。
4. 当 static route 基本写完后，Subnet 状态开始快速追平。
5. deploy 主进程被我中断后，kube-ovn-controller 仍在后台继续处理 CR，最终
   `processed=1876 ready=1876`。

最终完成快照：

- `Subnet`: `processed=1876 ready=1876 error=0`
- `Logical_Switch`: `1878`
- `Logical_Switch_Port`: `1884`
- `Logical_Router_Static_Route`: `1877`
- `Logical_Router_Policy`: `1890`
- `Port_Binding`: `3762`
- `ovs-vsctl` 查询最大耗时约 `9ms`
- 无 workload Pod，因此没有 CNI ADD 延迟样本，也没有 OVS add-port timeout 样本。

采样峰值：

- `AddSubnet` queue 约 `1876`
- `kube-ovn-controller` 约 `1482m`
- `ovn-central` 约 `1125m`

关键判断：1897 tuned 下也没有出现 controller/central CPU 接近新 limit 的情况。
真正慢的是单 VPC 下大量 Subnet 触发的 static route mutation 和后续 logical switch
创建/状态同步。资源加量能防止过低 limit 卡住，但不能根本消除这个单 VPC/单 OVNDB
对象模型的规模成本。

### 12.3 1500 中间点未执行

尝试 1500 作为中间规模时，preflight 失败：

```text
Required file not found: /data/lxl/seed-emulator/topology/real_topology_1500.txt
```

当前可用拓扑规模包含 `1078, 1897, 2599, 3083, 4192, 4954` 等，因此后续中间点应从
已有拓扑文件中选择，例如 1078 -> 1897 -> 2599。

### 12.4 对改善方向的判断

本轮回答了两个问题：

1. **只给 OVN/Kube-OVN 加 CPU/内存不是核心解法。** 1078 和 1897 tuned 期间，
   `kube-ovn-controller` 和 `ovn-central` CPU 都远低于新 limit，但 Subnet 阶段仍有
   长平台期。
2. **平台期主要来自单 VPC route/OVNDB 对象构建。** 1897 中最明显：先把
   `Logical_Router_Static_Route` 写到 1877 左右，之后 `Logical_Switch` 和 Subnet
   ready 才开始快速推进。

因此下一步优先级应是：

- **拆 VPC/拆 logical router。** 减少单个 VPC 上的 static route 列表和单 router
  mutation 压力。拆分后 `controllerWorkerNum` 才更可能发挥并发作用。
- **分片 OVN 域或多集群。** 如果必须保持每个 SeedEMU network 一个独立二层域，
  单个 ovn-central/OVN DB 承载几千个 logical switch 会越来越慢，分片是更稳的方向。
- **减少 logical switch/Subnet 数。** 如果实验语义允许，可合并部分网络对象或做路由
  聚合；但这会影响“每个网络独立二层隔离”的语义，需要单独确认。
- **继续保留分阶段部署。** 先创建 Subnet/VPC，等 route/switch 完成，再创建 Pod。
  这不能减少总开销，但能避免 Pod CNI ADD 和 Subnet reconcile 混在一起，便于定位。
- **Pod/CNI ADD 仍需单独压测。** 本轮 tuned 1897 没进入 Pod 阶段；之前 1078 clean
  rebuild 已出现过首个 Pod 的 `ovs-vsctl --timeout=30` 和 interface-not-ready 重试。
  所以后续要在 Subnet 完成后专门记录第一个 Pod 到第 N 个 Pod 的 CNI ADD 延迟曲线。

最终结论：pure OVN+OVS 可以实现每个网络独立 logical switch 的二层隔离语义，但在
单 VPC、单 logical router、单 OVNDB 域中承载上千到几千个 SeedEMU link 时，控制面
会先在 static route/OVNDB mutation 上变慢，随后还会在 Pod CNI ADD/OVS add-port
阶段继续承压。要扩大规模，方向应是分片和减少单域对象数量，而不是单纯把资源上限
继续调大。

## 13. 2026-06-15 针对 OVN 瓶颈的 1/2/4 类实验设计

用户确认可以尝试以下方向：

1. 升级 Kube-OVN 版本。
2. 关闭当前实验不需要的 Kube-OVN 功能。
4. 对 VPC/logical router 做分片。

第 3 类“减少 Subnet 数量”暂不作为默认方向。原因是 SeedEMU 语义中每个 link/network
基本对应一个独立二层广播域；Kube-OVN 文档也把 Subnet 定义为 IP 和网络配置的基本
单元。若把多个 SeedEMU link 合并到同一个 Subnet/logical switch，会改变原本每条链路
独立二层隔离的语义。因此现阶段保持 Subnet 数量不变，只改变这些 Subnet 进入 OVN 的
组织方式。

参考依据：

- Kube-OVN release 页面显示当前 latest 为 `v1.16.2`，发布日期为 `2026-06-04`。
- Kube-OVN performance tuning 文档建议使用最新版本以获得更好的默认性能。
- Kube-OVN setup options 文档明确说明可以关闭 LB、NetworkPolicy、EIP/SNAT 来减少
  `kube-ovn-controller` 在大规模集群中的开销。
- Kube-OVN VPC 文档说明每个 VPC 映射为一个 OVN logical router，且 VPC 的
  `namespaces` 为空时表示所有 namespace 可使用该 VPC。
- Kube-OVN issue #5312 记录过大规模 Subnet 下 policy route 逐条添加导致 Subnet ready
  变慢的现象，并说明 v1.14.0 后相关路径有性能改善。

本轮代码侧改动：

- `seedemu/k8sTools/resources/setup/manageK3sConfig.py`
  - 增加 `ovn.enableLb`、`ovn.enableNp`、`ovn.enableEipSnat`、`ovn.enableNatGw`、
    `ovn.enableLbSvc`、`ovn.enableExternalVpc`、`ovn.checkGateway` 的读取。
- `seedemu/k8sTools/resources/setup/ovn/installKubeOvnFabric.py`
  - Helm 安装 Kube-OVN 时把上述开关传入 chart。
- `seedemu/k8sTools/resources/running/manageK8sManifest.py`
  - 增加 `SEED_KUBE_OVN_VPC_SHARDS` 支持。
  - `vpcShards=1` 保持原行为：一个 namespace 一个 VPC。
  - `vpcShards>1` 时为同一 namespace 生成多个 VPC，并用稳定 hash 把 Subnet 分散到
    多个 VPC/logical router。
  - 分片 VPC 的 `spec.namespaces` 置空，按 Kube-OVN 语义允许 namespace 使用多个 VPC。
- `largeScale/b62_k8s_scale/lib.sh`
  - 从 assignment 读取 `ovn.vpcShards` 并导出给 manifest renderer。
  - Kube-OVN runtime manifest 每次都重新渲染，避免改分片数后复用旧
    `k8s.kube-ovn.yaml`。
- `largeScale/b62_k8s_scale/runOvnScaleProfile.py`
  - 新增 `--ovn-chart-version`、`--disable-extra-ovn-features`、`--vpc-shards`。

### 13.1 非破坏性渲染验证

用已有 1078 规模 `k8s.yaml` 离线渲染 `vpcShards=4`：

- `Vpc`: 4
- `Subnet`: 1065
- `NetworkAttachmentDefinition`: 1065
- `Deployment`: 1078
- Subnet 分布约为 `237/263/296/269`。
- `validate-manifest` 未发现重复资源。

这说明分片逻辑本身不会减少 Subnet 数量，也不会破坏每个 Subnet/NAD/provider 的一一
对应关系。

### 13.2 1078 pure OVN + vpcShards=4 中间结果

执行命令：

```bash
cd /home/lxl/k8s/largeScale/b62_k8s_scale
python3 runOvnScaleProfile.py \
  --assignment assignment_ovn_tuned.yaml \
  --scales 1078 \
  --namespace-template 'seedemu-b62-ovn-shard4-{scale}' \
  --vpc-shards 4 \
  --metrics-interval 15 \
  --no-stop-on-fail
```

运行目录：

```text
/home/lxl/k8s/largeScale/b62_k8s_scale/runs/ovn_profile_20260615_221817_1078_w5
```

阶段中间结果：

- `render-assignment`: 0.05s
- `clean`: 1.79s
- `preflight`: 4.96s
- `compile`: 10.83s
- `build`: 63.37s
- `deploy`: 仍在运行

实际 manifest：

- `Vpc`: 4
- `Subnet`: 1065
- `NetworkAttachmentDefinition`: 1065
- `Deployment`: 1078
- 四个 VPC 上的 Subnet 分布为 `245/278/261/281`。

Subnet 阶段观察：

- 所有 1065 个 Subnet 已创建。
- `wait_for_kube_ovn_subnets` 从 `processed=133 ready=130` 逐步推进到
  `processed=1065 ready=1065 error=0`。
- 采样时 `AddSubnet` 和 `AddOrUpdateVpc` 队列归零。
- `ovn-central` 约 `580m CPU / 519Mi`。
- `kube-ovn-controller` 约 `1525m CPU / 128Mi`。
- `Logical_Switch`: 1067。
- `Logical_Router_Static_Route`: 1066。
- `ovs-vsctl` 查询没有 timeout，节点侧 OVS interface 查询约 `9-10ms`。

当前状态：

- deploy 已进入 `DEPLOY_POST_SUBNET_COOLDOWN_SECONDS=900` 的 cooldown。
- workload Pod 尚未开始创建，因此还没有 CNI ADD latency 和 OVS add-port 耗时样本。
- 该中间结果已经证明：VPC 分片不会阻止 Subnet ready；下一步要等 Pod 阶段验证
  多 VPC 多 secondary interface 是否能正常 CNI ADD。

### 13.3 后续实验顺序

建议按以下顺序继续，避免把变量混在一起：

1. 完成当前 `vpcShards=4` 的 1078 Pod 阶段，确认 CNI ADD 是否正常。
2. 重建 Kube-OVN 时先只关闭无关功能，仍用 v1.15.12，比较 Subnet 阶段和 Pod 阶段。
3. 再切到 v1.16.2，比较同一规模、同一分片数、同一功能开关下的差异。
4. 如果 1078 稳定，再按 `1897 -> 2599 -> 3083 -> 4192` 递增。

### 13.4 1078 pure OVN + vpcShards=4 的 Pod 阶段结论

运行目录：

```text
/home/lxl/k8s/largeScale/b62_k8s_scale/runs/ovn_profile_20260615_221817_1078_w5
```

该轮使用 4 个 VPC shard，但未关闭 Kube-OVN 的额外功能。Subnet 阶段可以收敛：

- `Subnet`: `1065/1065 Ready`
- `Vpc`: 4
- `Logical_Switch`: 1067
- `Logical_Router_Static_Route`: 1066
- Subnet 阶段未观察到 `ovs-vsctl` 查询 timeout。

但是 cooldown 后进入 Pod 阶段时，前两个 Pod 即出现 CNI ADD 问题：

- Pod 状态停留在 `ContainerCreating`。
- events 中出现 `failed_create_pod_sandbox`、`ovs_interface_not_ready`、
  `ovs_vsctl_timeout`。
- CNI 日志中 `ovs add-port` 样本最大约 `30034ms`，即触发了
  `ovs-vsctl --timeout=30`。
- 当时 workload Pod 只有 2 个，说明问题不是 Pod 批量并发过大造成的，而是单个
  secondary Kube-OVN interface 进入 OVS/OVN 路径时已经可能超过 30s。

因此，VPC sharding 能让 1078 的 Subnet 阶段完成，但没有单独解决 Pod CNI ADD
阶段的 OVS port ready/`ovs-vsctl` timeout 问题。

### 13.5 1078 pure OVN + feature-off + vpcShards=4 的结论

运行命令：

```bash
cd /home/lxl/k8s/largeScale/b62_k8s_scale
python3 runOvnScaleProfile.py \
  --assignment assignment_ovn_tuned.yaml \
  --scales 1078 \
  --namespace-template 'seedemu-b62-ovn-featureoff-shard4-{scale}' \
  --vpc-shards 4 \
  --disable-extra-ovn-features \
  --metrics-interval 15 \
  --rebuild-first \
  --no-stop-on-fail
```

运行目录：

```text
/home/lxl/k8s/largeScale/b62_k8s_scale/runs/ovn_profile_20260615_225357_1078_w5
```

该轮重建了 K3s/Kube-OVN/OVS，使用 Kube-OVN `v1.15.12`，并关闭：

- `ovnEnableLb=false`
- `ovnEnableNp=false`
- `ovnEnableEipSnat=false`
- `ovnEnableNatGw=false`
- `ovnEnableLbSvc=false`
- `ovnEnableExternalVpc=false`
- `ovnCheckGateway=false`

阶段耗时：

- `destroy-existing-cluster`: 42.68s
- `prepare-libvirt-dhcp`: 8.90s
- `build-cluster`: 490.54s
- `clean`: 1.82s
- `preflight`: 3.06s
- `compile`: 11.15s
- `build`: 69.26s
- `deploy`: 人工停止于第 1 个 workload Pod CNI 失败后。

Subnet 阶段结果：

- `Subnet`: `1065/1065 Ready`
- `Vpc`: 4
- `Logical_Switch`: 1067
- `Logical_Router_Static_Route`: 1066
- `Logical_Switch_Port`: 1076
- `SB Port_Binding`: 2143
- 事件计数在 Subnet 阶段为 0。

Pod 阶段结果：

- 第 1 个 Pod `as1528brd-r3-1.3.5.248-...` 调度到 master。
- 默认网络 `eth0` 可以被 Multus 添加。
- secondary network `net-ix-ix3` 失败：
  - 第一次失败为 `ovs interface ..._net1_h is not ready after 30s`。
  - 第二次失败为 `ovs-vsctl --timeout=30 --may-exist add-port br-int ...`
    触发 `signal: alarm clock`。
- 停止后采样：
  - `failed_create_pod_sandbox=2`
  - `ovs_interface_not_ready=1`
  - `ovs_vsctl_timeout=1`
  - `add_nic_to_ovs_failed=1`
  - CNI 日志 `ovs_add_port_log_max_ms=30031`
  - CNI 日志 `ovs_vsctl_timeout_logs=6`

该轮清理结果：

- namespace `seedemu-b62-ovn-featureoff-shard4-1078` 已删除。
- 匹配的 `ip.kubeovn.io`、`subnet.kubeovn.io`、`vpc.kubeovn.io` 均已删除。

结论：

- 关闭 LB/NetworkPolicy/EIP-SNAT/NAT-GW/LB-SVC/External-VPC/Gateway check 等
  功能不能消除本实验的核心瓶颈。
- Subnet 阶段的核心对象数量仍然和 SeedEMU 网络数量近似线性对应：
  每个网络仍会产生 Subnet、Logical Switch、相关 static route/port binding。
- Pod 阶段的失败仍集中在 secondary Kube-OVN interface 的 OVS add-port/port ready
  路径，和未关闭功能时的失败模式一致。
- 因此第 2 类优化只能作为减少无关控制面负载的辅助措施，不能作为解开 pure
  OVN+OVS 限制的主要方案。下一步应测试 Kube-OVN v1.16.2，并继续考虑更彻底的
  OVS add-port 超时、OVN/CNI 串行路径、或 VPC/OVNDB 分片方案。

### 13.6 1078 pure OVN + v1.16.2 + feature-off + vpcShards=4 的结论

运行命令：

```bash
cd /home/lxl/k8s/largeScale/b62_k8s_scale
python3 runOvnScaleProfile.py \
  --assignment assignment_ovn_tuned.yaml \
  --scales 1078 \
  --namespace-template 'seedemu-b62-ovn-v1162-featureoff-shard4-{scale}' \
  --vpc-shards 4 \
  --disable-extra-ovn-features \
  --ovn-chart-version v1.16.2 \
  --metrics-interval 15 \
  --rebuild-first \
  --no-stop-on-fail
```

运行目录：

```text
/home/lxl/k8s/largeScale/b62_k8s_scale/runs/ovn_profile_20260615_232825_1078_w5
```

该轮重建了 K3s/Kube-OVN/OVS，并成功安装 Kube-OVN `v1.16.2`：

- Kube-OVN chart 已缓存到
  `/home/lxl/k8s/largeScale/b62_k8s_scale/base_image/helm/charts/kube-ovn-v1.16.2.tgz`。
- Kube-OVN 镜像 tar 已缓存到
  `/home/lxl/k8s/largeScale/b62_k8s_scale/base_image/helm/kube-ovn_v1.16.2.tar`。
- 后续同版本重跑可复用本地缓存，不需要重新联网拉取该 chart/image。

阶段耗时：

- `destroy-existing-cluster`: 42.23s
- `prepare-libvirt-dhcp`: 8.80s
- `build-cluster`: 528.75s
- `clean`: 1.72s
- `preflight`: 3.01s
- `compile`: 11.00s
- `build`: 67.47s
- `deploy`: 人工停止于第 1 个 workload Pod CNI 失败后。

Subnet 阶段结果：

- `Subnet`: `1065/1065 Ready`
- `Vpc`: 4
- `Logical_Switch`: 1067
- `Logical_Router_Static_Route`: 1066
- `Logical_Switch_Port`: 1076
- `SB Port_Binding`: 2143
- Subnet 阶段没有 Kube-OVN error。

Pod 阶段结果：

- 第 1 个 Pod 仍是 `as1528brd-r3-1.3.5.248-...`，调度到 master。
- 默认网络 `eth0` 添加成功。
- secondary network `net-ix-ix3` 仍然失败：
  - 第一次失败为 `ovs interface ..._net1_h is not ready after 30s`。
  - 第二次失败为 `ovs-vsctl --timeout=30 --may-exist add-port br-int ...`
    触发 `signal: alarm clock`。
- 后续 retry 中该 Pod 曾继续添加 `net1` 和 `net2`，说明失败并非永久性资源缺失，
  而是 OVS/OVN add-port/port-ready 路径在高对象数量下出现明显长尾。
- 停止后采样：
  - `failed_create_pod_sandbox=2`
  - `ovs_interface_not_ready=1`
  - `ovs_vsctl_timeout=1`
  - `add_nic_to_ovs_failed=1`
  - CNI 日志 `ovs_add_port_log_max_ms=30033`
  - CNI 日志 `ovs_vsctl_timeout_logs=5`

该轮清理结果：

- namespace `seedemu-b62-ovn-v1162-featureoff-shard4-1078` 已删除。
- 匹配的 `ip.kubeovn.io`、`subnet.kubeovn.io`、`vpc.kubeovn.io` 均已删除。
- 当前 6 个 K3s 节点均为 Ready，Multus、Kube-OVN CNI、OVS、ovn-central 组件为
  Running。`kube-ovn-controller` 出现过 1 次重启，但清理后当前为 Running。

结论：

- Kube-OVN v1.16.2 可以正常安装并完成 1078 规模的 Subnet reconcile。
- v1.16.2 没有消除本实验的 pure OVN attached CNI ADD 长尾；第 1 个 Pod 仍能触发
  `ovs-vsctl --timeout=30`。
- 结合 13.4、13.5、13.6，可以基本排除“只升级 Kube-OVN 版本”“只关闭无关功能”
  或“只把 Subnet 分到多个 VPC”能单独解决问题。
- 下一步应把重点放在：
  1. 降低单节点 `ovs-vsctl add-port` 串行压力或提高超时阈值；
  2. 研究 Kube-OVN CNI daemon 的 OVS 操作并发/队列；
  3. 评估是否需要多 OVNDB/多 Kube-OVN 控制平面，而不仅是单集群内多个 VPC；
  4. 或继续使用当前已跑通的 `macvlan + Kube-OVN IPAM` 作为大规模实验路径，同时把
     pure OVN+OVS 作为独立优化课题。

### 13.7 针对 CNI ADD 30s 长尾的第一轮修复假设

时间：2026-06-16 10:14 CST。

上一轮 v1.16.2 + feature-off + vpcShards=4 的关键证据是：Subnet 已经全部 Ready，
但第一个 workload Pod 的 secondary Kube-OVN interface 在 OVS add-port/port-ready
路径触发 `ovs-vsctl --timeout=30` 和 `interface is not ready after 30s`。因此本轮
不再只调外层 deploy timeout，而是直接降低 CNI/OVS 写入压力并提高 Kube-OVN CNI
内部调用 `ovs-vsctl` 的有效正超时时间。

本轮代码侧改动：

- `runOvnScaleProfile.py` 默认 pure OVN profile 使用
  `ovn.cniOvsVsctlConcurrency=1`、`ovn.cniOvsVsctlTimeoutSeconds=180`，
  并写入 `placement.excludeControlPlane=true`。
- `seed_k8s_plan_real_topology_by_as.py` 支持从 assignment 派生的 placement 控制，
  compile 阶段会把业务 AS 放到 worker，避免第一个 workload Pod 落到运行
  `ovn-central`/monitor 的 master。
- Kube-OVN install patch 会在 `kube-ovn-cni` 的 `/usr/local/sbin` 生成
  `ovs-vsctl` wrapper。该 wrapper 只把小于配置值的正 timeout 参数提升到配置值，
  例如把内部写死的 `--timeout=30` 提升到 180 秒；`--timeout=0` 等无超时语义不会被改。
- `deploy.sh` 在 `attachedCniType=kube-ovn` 时切换为更保守的提交节奏：
  batch size 5、前 20 批单 Pod warmup、批间 10 秒，并记录
  `max_creating_per_node`/`creating_by_node`。

这轮修复的预期：

- 如果之前的失败主要是“OVS add-port 需要 30 秒以上但最终可完成”，提高内部
  `ovs-vsctl` timeout 应该能让第一个 Pod 通过 CNI ADD。
- 如果失败主要是 OVS/OVN datapath 永久无法就绪，则 wrapper 只能把失败推迟到
  180 秒，后续仍会出现 `interface not ready` 或 kubelet sandbox 失败。
- 避开 master 和单并发不会减少 OVN logical switch/static route 数量，但能减少
  control-plane 节点的本地 OVS 写入和 kubelet/CNI 瞬时压力。

下一轮验证命令计划：

```bash
cd /home/lxl/k8s/largeScale/b62_k8s_scale
python3 runOvnScaleProfile.py \
  --assignment assignment_ovn_tuned.yaml \
  --scales 1078 \
  --namespace-template 'seedemu-b62-ovn-timeout180-shard4-{scale}' \
  --vpc-shards 4 \
  --disable-extra-ovn-features \
  --ovn-chart-version v1.16.2 \
  --cni-ovs-vsctl-concurrency 1 \
  --cni-ovs-vsctl-timeout-seconds 180 \
  --metrics-interval 15 \
  --rebuild-first \
  --no-stop-on-fail
```

该验证需要真正重建 K3s/Kube-OVN 并部署 workload；结论以 run directory 中的
`summary.json`、`deploy.log`、events、CNI 日志和 `ovn_metrics_summary.json` 为准。


## 2026-06-16 13:56 CST pure OVN scale 1078 profile

- Run directory: `/home/lxl/k8s/largeScale/b62_k8s_scale/runs/ovn_profile_20260616_101529_1078_w5`.
- Namespace: `seedemu-b62-ovn-timeout180-shard4-1078`.
- Profile knobs: chart=`v1.16.2`, disable_extra_ovn_features=`True`, vpc_shards=`4`, ovs_vsctl_concurrency=`1`, ovs_vsctl_timeout_seconds=`180`, exclude_control_plane=`True`.
- Overall status: `PASS`.
- Key stages: deploy=PASS(12324.6s), wait-ready=PASS(1.26s), clean-after-profile=PASS(324.04s).
- Final Pods: `{'counts': {'Pending': 2, 'Running': 1076}, 'latency': {'count': 1078, 'max': 415.0, 'min': 0.0, 'p50': 21.0, 'p90': 22.0, 'p99': 142.0}, 'ready_latency': {'count': 1078, 'max': 415.0, 'min': 0.0, 'p50': 21.0, 'p90': 22.0, 'p99': 142.0}, 'total': 1078}`.
- Final OVN DB counts: `{'nb_logical_router_policy': {'count': 1079, 'elapsed_ms': 23, 'rc': 0, 'stderr': ''}, 'nb_logical_router_static_route': {'count': 1066, 'elapsed_ms': 18, 'rc': 0, 'stderr': ''}, 'nb_logical_switch': {'count': 1067, 'elapsed_ms': 33, 'rc': 0, 'stderr': ''}, 'nb_logical_switch_port': {'count': 3885, 'elapsed_ms': 122, 'rc': 0, 'stderr': ''}, 'sb_port_binding': {'count': 4952, 'elapsed_ms': 156, 'rc': 0, 'stderr': ''}}`.
- Max workqueue depth: `{'AddIP': 0.0, 'AddIPPool': 0.0, 'AddIptablesDnatRule': 0.0, 'AddIptablesEip': 0.0, 'AddIptablesFip': 0.0, 'AddIptablesSnatRule': 0.0, 'AddNamespace': 0.0, 'AddNode': 0.0, 'AddOrUpdateCSR': 0.0, 'AddOrUpdatePod': 5.0, 'AddOrUpdateVMIMigration': 0.0, 'AddOrUpdateVpc': 3.0, 'AddOrUpdateVpcEgressGateway': 0.0, 'AddOrUpdateVpcNatGw': 0.0, 'AddOvnDnatRule': 0.0, 'AddOvnEip': 0.0, 'AddOvnFip': 0.0, 'AddOvnSnatRule': 0.0, 'AddQoSPolicy': 0.0, 'AddService': 0.0, 'AddSubnet': 994.0, 'AddVirtualIP': 0.0, 'AddVlan': 0.0, 'DeleteIP': 0.0, 'DeleteIPPool': 0.0, 'DeleteIptablesDnatRule': 0.0, 'DeleteIptablesEip': 0.0, 'DeleteIptablesFip': 0.0, 'DeleteIptablesSnatRule': 0.0, 'DeleteNode': 0.0, 'DeleteOvnDnatRule': 0.0, 'DeleteOvnEip': 0.0, 'DeleteOvnFip': 0.0, 'DeleteOvnSnatRule': 0.0, 'DeleteQoSPolicy': 0.0, 'DeleteSecurityGroup': 0.0, 'DeleteService': 0.0, 'DeleteSubnet': 0.0, 'DeleteVM': 0.0, 'DeleteVirtualIP': 0.0, 'DeleteVlan': 0.0, 'DeleteVpc': 0.0, 'DeleteVpcEgressGateway': 0.0, 'DeleteVpcNatGw': 0.0, 'InitVpcNatGw': 0.0, 'ResetIptablesEip': 0.0, 'ResetOvnEip': 0.0, 'SyncSecurityGroupPorts': 0.0, 'SyncVirtualPort': 0.0, 'UpdateEndpointSlice': 10.0, 'UpdateIP': 0.0, 'UpdateIPPoolStatus': 0.0, 'UpdateIptablesDnatRule': 0.0, 'UpdateIptablesEip': 0.0, 'UpdateIptablesFip': 0.0, 'UpdateIptablesSnatRule': 0.0, 'UpdateNode': 0.0, 'UpdateOvnDnatRule': 0.0, 'UpdateOvnEip': 0.0, 'UpdateOvnFip': 0.0, 'UpdateOvnSnatRule': 0.0, 'UpdatePodSecurity': 0.0, 'UpdateQoSPolicy': 0.0, 'UpdateSecurityGroup': 0.0, 'UpdateService': 0.0, 'UpdateSubnetStatus': 0.0, 'UpdateVirtualIP': 0.0, 'UpdateVirtualParents': 0.0, 'UpdateVlan': 0.0, 'UpdateVpcDnat': 0.0, 'UpdateVpcEip': 0.0, 'UpdateVpcFloatingIp': 0.0, 'UpdateVpcSnat': 0.0, 'UpdateVpcStatus': 0.0, 'UpdateVpcSubnet': 0.0}`.
- Max `ovs-vsctl` elapsed: `34 ms`; timeout/error samples: `0`.
- Pod sandbox lifecycle latency summary: `{'count': 1078, 'max': 415.0, 'min': 0.0, 'p50': 21.0, 'p90': 22.0, 'p99': 142.0}`.
- CNI ADD delta count max per node: `564.0`; average CNI ADD latency across reported nodes: `31.5526` seconds.
- Event counters: `{'add_nic_to_ovs_failed': 0, 'failed_create_pod_sandbox': 0, 'ovs_interface_not_ready': 0, 'ovs_vsctl_timeout': 0}`.

### 13.8 复用已清理集群时的 CNI ready-wait 长尾

时间：2026-06-16 14:32 CST。

在上一轮 clean rebuild 的 1078 pure OVN profile PASS 后，为了保留 namespace
继续跑 BIRD/kernel，我尝试不重建 K3s/Kube-OVN，直接复用已清理后的集群重新部署
`seedemu-b62-ovn-timeout180-shard4-1078-keep`。这轮被主动中断，原因是第一批 Pod
进入 CNI ADD 后出现很长的重试链：

- 第 1 个 Pod `as1479brd-r13-1.13.5.199-...` 第一次在 `net1` 失败：
  `ovs interface ..._net1_h is not ready after 30s`。
- 同一个 Pod 第二次 retry 时 `ovs-vsctl --may-exist add-port` 实际执行约 36.7s
  但没有 `alarm clock`，说明 `ovs-vsctl` wrapper 已经把内部 30s timeout
  提升后生效；随后失败点转移到 Kube-OVN daemon 自身的 interface-ready 30s 等待。
- 该 Pod 第三次 retry 后能 Running；第 2 个 Pod 也重复了 `net1`、`net2`
  各一次 `interface not ready after 30s` 的模式。
- deploy 的压力门槛设置为 `creating<=0`，因此每个 Pod 都要等待 retry 自恢复。
  如果持续这种模式，1078 个 Pod 的部署时间会变得不可接受。

本轮不是 permanent CNI ADD failure，而是复用集群状态下的 CNI ready-wait 长尾。
它和之前的 `ovs-vsctl --timeout=30 signal: alarm clock` 不同：后者已经被 wrapper
缓解，前者是 Kube-OVN daemon 在添加端口后等待 OVS/OVN datapath/网关可用的内部
30s 阈值。为了避免把这个状态带入 BIRD/kernel 验证，本轮中断于 deploy 约
1759s，只创建到 3 个 Pod，然后用 `clean.sh` 清掉 namespace 和匹配的
Kube-OVN CR。

当前处理策略：

- 对需要保留 namespace 并继续 BIRD/kernel 的 1078 验证，改为
  `--rebuild-first --no-clean-after`，先做 cold K3s/Kube-OVN rebuild，再部署并保留
  workload。
- 如果 clean rebuild 后第一批 Pod 仍然反复触发 `interface not ready after 30s`，
  下一步应研究 Kube-OVN daemon 源码中的 interface-ready/gateway-ready 等待参数，
  或对 kube-ovn-cni 启动逻辑做更深的 wrapper/patch，而不是继续只调外层 deploy
  batch size。


## 2026-06-16 19:10 CST pure OVN scale 1078 profile

- Run directory: `/home/lxl/k8s/largeScale/b62_k8s_scale/runs/ovn_profile_20260616_160915_1078_w5`.
- Namespace: `seedemu-b62-ovn-ifskip-1078-keep`.
- Profile knobs: chart=`v1.16.2`, image_tag=`v1.16.2-seed-ifskip`, disable_extra_ovn_features=`True`, vpc_shards=`4`, ovs_vsctl_concurrency=`1`, ovs_vsctl_timeout_seconds=`180`, interface_ready_timeout_seconds=`0`, exclude_control_plane=`True`.
- Overall status: `PASS`.
- Key stages: deploy=PASS(9993.82s), wait-ready=PASS(1.14s).
- Final Pods: `{'counts': {'Running': 1078}, 'latency': {'count': 1078, 'max': 99.0, 'min': 6.0, 'p50': 20.0, 'p90': 21.0, 'p99': 46.0}, 'ready_latency': {'count': 1078, 'max': 99.0, 'min': 6.0, 'p50': 20.0, 'p90': 21.0, 'p99': 46.0}, 'total': 1078}`.
- Final OVN DB counts: `{'nb_logical_router_policy': {'count': 1079, 'elapsed_ms': 22, 'rc': 0, 'stderr': ''}, 'nb_logical_router_static_route': {'count': 1066, 'elapsed_ms': 23, 'rc': 0, 'stderr': ''}, 'nb_logical_switch': {'count': 1067, 'elapsed_ms': 32, 'rc': 0, 'stderr': ''}, 'nb_logical_switch_port': {'count': 3885, 'elapsed_ms': 116, 'rc': 0, 'stderr': ''}, 'sb_port_binding': {'count': 4952, 'elapsed_ms': 141, 'rc': 0, 'stderr': ''}}`.
- Max workqueue depth: `{'AddIP': 0.0, 'AddIPPool': 0.0, 'AddIptablesDnatRule': 0.0, 'AddIptablesEip': 0.0, 'AddIptablesFip': 0.0, 'AddIptablesSnatRule': 0.0, 'AddNamespace': 0.0, 'AddNode': 0.0, 'AddOrUpdateCSR': 0.0, 'AddOrUpdatePod': 5.0, 'AddOrUpdateVMIMigration': 0.0, 'AddOrUpdateVpc': 3.0, 'AddOrUpdateVpcEgressGateway': 0.0, 'AddOrUpdateVpcNatGw': 0.0, 'AddOvnDnatRule': 0.0, 'AddOvnEip': 0.0, 'AddOvnFip': 0.0, 'AddOvnSnatRule': 0.0, 'AddQoSPolicy': 0.0, 'AddService': 0.0, 'AddSubnet': 947.0, 'AddVirtualIP': 0.0, 'AddVlan': 0.0, 'DeleteIP': 0.0, 'DeleteIPPool': 0.0, 'DeleteIptablesDnatRule': 0.0, 'DeleteIptablesEip': 0.0, 'DeleteIptablesFip': 0.0, 'DeleteIptablesSnatRule': 0.0, 'DeleteNode': 0.0, 'DeleteOvnDnatRule': 0.0, 'DeleteOvnEip': 0.0, 'DeleteOvnFip': 0.0, 'DeleteOvnSnatRule': 0.0, 'DeleteQoSPolicy': 0.0, 'DeleteSecurityGroup': 0.0, 'DeleteService': 0.0, 'DeleteSubnet': 0.0, 'DeleteVM': 0.0, 'DeleteVirtualIP': 0.0, 'DeleteVlan': 0.0, 'DeleteVpc': 0.0, 'DeleteVpcEgressGateway': 0.0, 'DeleteVpcNatGw': 0.0, 'InitVpcNatGw': 0.0, 'ResetIptablesEip': 0.0, 'ResetOvnEip': 0.0, 'SyncSecurityGroupPorts': 0.0, 'SyncVirtualPort': 0.0, 'UpdateEndpointSlice': 10.0, 'UpdateIP': 0.0, 'UpdateIPPoolStatus': 0.0, 'UpdateIptablesDnatRule': 0.0, 'UpdateIptablesEip': 0.0, 'UpdateIptablesFip': 0.0, 'UpdateIptablesSnatRule': 0.0, 'UpdateNode': 0.0, 'UpdateOvnDnatRule': 0.0, 'UpdateOvnEip': 0.0, 'UpdateOvnFip': 0.0, 'UpdateOvnSnatRule': 0.0, 'UpdatePodSecurity': 0.0, 'UpdateQoSPolicy': 0.0, 'UpdateSecurityGroup': 0.0, 'UpdateService': 0.0, 'UpdateSubnetStatus': 0.0, 'UpdateVirtualIP': 0.0, 'UpdateVirtualParents': 0.0, 'UpdateVlan': 0.0, 'UpdateVpcDnat': 0.0, 'UpdateVpcEip': 0.0, 'UpdateVpcFloatingIp': 0.0, 'UpdateVpcSnat': 0.0, 'UpdateVpcStatus': 1.0, 'UpdateVpcSubnet': 0.0}`.
- Max `ovs-vsctl` elapsed: `26 ms`; timeout/error samples: `0`.
- Pod sandbox lifecycle latency summary: `{'count': 1078, 'max': 99.0, 'min': 6.0, 'p50': 20.0, 'p90': 21.0, 'p99': 46.0}`.
- CNI ADD delta count max per node: `0.0`; average CNI ADD latency across reported nodes: `None` seconds.
- Event counters: `{'add_nic_to_ovs_failed': 0, 'failed_create_pod_sandbox': 0, 'ovs_interface_not_ready': 0, 'ovs_vsctl_timeout': 0}`.

### 13.9 1078 ifskip 运行结论

时间：2026-06-16 19:55 CST。

本轮在 ChatGPT/Codex 前端出现 `stream disconnected before completion` 后继续检查本地
进程，确认实验没有被中断。`runOvnScaleProfile.py` 已正常退出，最终状态为
`PASS`。这说明前端 stream 断连只是交互链路问题，不是 K3s/Kube-OVN/Pod 创建失败。

本轮有效结论：

- `v1.16.2-seed-ifskip` 镜像中跳过 Kube-OVN daemon 的
  `ovn-installed=true` interface-ready wait 后，1078 规模 pure OVN+OVS 可以完整部署。
- `ovs-vsctl` wrapper、`--ovs-vsctl-concurrency=1`、worker-only placement、每节点
  一个 creating Pod 的 deploy pacing 组合没有再触发历史上的三类错误：
  `FailedCreatePodSandBox=0`、`ovs_interface_not_ready=0`、`ovs_vsctl_timeout=0`。
- wait-ready 阶段 0s 即观察到 1078/1078 Pod Running and Ready，说明 deploy 结束时
  workload 已经完成启动。
- 最终 OVN DB 规模为 1067 logical switches、1066 static routes、1079 router
  policies、3885 logical switch ports、4952 southbound port bindings。
- 最终 Pod ready/lifecycle 延迟分布为 p50=20s、p90=21s、p99=46s、max=99s，
  明显优于之前 timeout180 no-ifskip 复用/冷启动场景中的长尾。

这个改动没有改变每个 SeedEMU 网络对应独立 Kube-OVN Subnet/logical switch 的语义，
因此二层隔离仍由不同 Subnet/logical switch 提供。需要注意的是，跳过
interface-ready wait 只是让 CNI ADD 不等待 OVN datapath 显式标记 `ovn-installed=true`；
它不会把不同网络合并，但可能让 Pod 在刚启动的极短窗口内先于 datapath 完全收敛。
本轮最终 `empty_ofport=0`、所有 Pod Ready，未观察到残留未安装端口。
### 13.10 deploy 阶段耗时优化记录

2026-06-16 22:57 CST 对 1078 规模 ifskip 成功轮次的 `deploy.log` 和 `ovn_metrics_summary.json` 做复盘，结论是本轮 9993.82s 的 deploy 耗时主要来自脚本层人为节流，而不是 OVN/OVS 本身卡死：

- Pod 生命周期延迟很低：最终 1078 个 Pod 全部 Running/Ready，Pod ready latency p50=20s、p90=21s、p99=46s、max=99s。
- `ovs-vsctl` 不是瓶颈：最大记录耗时 26ms，timeout/error samples 为 0。
- CNI 错误未出现：`FailedCreatePodSandBox=0`、`ovs_interface_not_ready=0`、`ovs_vsctl_timeout=0`。
- deploy 脚本旧节奏过保守：pure OVN 时 batch size 被降为 5，warmup 为 20 个单 Pod batch，每批后要求 `creating=0`，每批之间再 sleep 10s；1078 个 controller 被拆为 232 批。
- 另有固定等待：Subnet 全部 Ready 后仍强制 cooldown 900s。

因此本次优化不改变二层隔离语义，不减少 Subnet/logical switch 数量，也不合并网络；仍然保持每个 SeedEMU 网络对应独立的 Kube-OVN Subnet/logical switch。优化只改变资源提交节奏：

- controller batch size: `5 -> 40`
- warmup: `20 x 1 -> 2 x 5`
- batch sleep: `10s -> 0s`
- pressure poll interval: `10s -> 5s`
- total creating window: `0 -> 40`
- per-node creating window: `0 -> 10`
- pending/notReady window: `8 -> 80`
- subnet batch size: `100 -> 250`
- subnet batch sleep: `2s -> 0s`
- post-subnet cooldown: `900s -> 0s`

预期效果：1078 个 controller 的提交批次数从 232 降到约 29 批。因为 `cniOvsVsctlConcurrency=1` 仍保留，单节点 OVS 写入仍由 Kube-OVN CNI 本地串行化；脚本层只允许有限 in-flight Pod，不会一次性把全部 1078 个 Pod 压给 CNI。

风险点：如果后续放大到 2599/4954 后 `FailedCreatePodSandBox`、`ovs_vsctl_timeout` 或 `empty_ofport` 再出现，优先把 `DEPLOY_MAX_CREATING_PODS` 从 40 降到 20，或把 `DEPLOY_MAX_CREATING_PODS_PER_NODE` 从 10 降到 5；不应先恢复 900s cooldown，因为本轮指标显示 Subnet Ready 后固定空等并没有提供可观测收益。

### 13.11 2026-06-16 deploy 提速实测修正

13.10 中基于指标提出的 `batch=40`/提高 in-flight Pod 窗口只是静态推断，
后续实测证明这个方向在当前 pure OVN+OVS 路径上不安全。即使
`kube-ovn-cni` 已经带有 `ovs-vsctl` wrapper、`--ovs-vsctl-concurrency=1`
和 180s 最小 timeout，只要让同一 worker 同时有 2 个 workload Pod 进入
多网卡 CNI ADD，就会重新触发 OVS 写路径 timeout。

本次连续测试结果：

- `runs/ovn_profile_20260616_230729_1078_w5`：
  `batch=40`、较大的 creating 窗口。Subnet 全部 Ready 后进入 controller
  阶段，在第 5 批附近出现约 130 个 ContainerCreating、单节点约 26 个
  Creating，并出现 `add nic to ovs failed context canceled by timeout`。
  本轮被中断并清理。
- `runs/ovn_profile_20260616_231731_1078_w5`：
  `batch=10`、creating 窗口降到 20、单节点窗口降到 4。第 4 批时仍到达
  约 30 个 ContainerCreating、单节点约 6 个 Creating，并复现同类
  `FailedCreatePodSandBox`/`add nic to ovs failed` 事件。本轮被中断并清理。
- `runs/ovn_profile_20260616_232715_1078_w5`：
  回到 `batch=5`、`creating<=0`、`creatingPerNode<=0`。前三批没有
  `FailedCreatePodSandBox`，说明这个节奏稳定；但每批等待所有 CNI ADD
  完成，吞吐较低。本轮作为稳定性确认被中断并清理。
- `runs/ovn_profile_20260616_234008_1078_w5`：
  试探浅流水线：`batch=5`、允许上一批最多 `creating<=5`、
  `creatingPerNode<=1`。这会让下一批提交后短时间形成每个 worker 2 个
  ContainerCreating、总共 10 个 ContainerCreating。结果在第 2 批立刻
  复现 `FailedCreatePodSandBox`，事件中明确包含
  `add nic to ovs failed context canceled by timeout`。本轮被中断并清理。

因此当前结论修正为：

- 不能通过增大 controller batch size 或允许每节点 2 个 in-flight CNI ADD
  来安全缩短 deploy。
- 当前稳定边界应保持：`DEPLOY_BATCH_SIZE=5`、
  `DEPLOY_MAX_CREATING_PODS=0`、`DEPLOY_MAX_CREATING_PODS_PER_NODE=0`，
  也就是每轮最多每个 worker 新建 1 个 Pod，并等待它们全部脱离
  ContainerCreating 后再发下一批。
- 可以保留的安全提速只有不增加 CNI 并发的部分：
  `DEPLOY_SUBNET_BATCH_SIZE=250`、`DEPLOY_SUBNET_BATCH_SLEEP_SECONDS=0`、
  `DEPLOY_POST_SUBNET_COOLDOWN_SECONDS=0`、`DEPLOY_BATCH_SLEEP_SECONDS=0`、
  `DEPLOY_PRESSURE_CHECK_SECONDS=5`。

这次现象也说明，瓶颈不主要是 CPU 或内存。失败时 `kubectl top nodes`
显示各 worker CPU 约 3%，master 约 6%，但 CNI ADD 仍然在 OVS/OVN 写路径
上 timeout。更合理的后续优化方向不是继续提高 Pod 提交并发，而是：

- 在 Kube-OVN daemon/OVS 写路径内部做更强的本地队列化，保证同一节点同一时刻
  只有一个 workload Pod 的多网卡 add-port 流程运行；
- 或者拆分 OVN/OVS 控制面，把不同规模分片落到不同 OVNDB/OVS 写路径；
- 或者改变仿真部署语义，减少每个 Pod 的 secondary interface 数量或减少
  需要 Kube-OVN CNI ADD 的网络对象数量。该方向会触及二层隔离语义，不能作为
  默认优化。

当前 `deploy.sh` 已回退到稳定限速，同时保留 Subnet 批量、去掉 900s cooldown
等安全优化。最新清理后没有残留 `seedemu-b62-*` workload namespace。

## 2026-06-17 09:52：gateway-check-skip + node-stream maxActive=2 仍然失败

本轮实验目录：

`runs/ovn_profile_20260617_094157_1078_w5`

关键参数：

- `--deploy-skip-kube-ovn-gateway-check`
- `--deploy-controller-apply-mode node-stream`
- `--deploy-node-stream-max-active-per-node 2`
- `--deploy-subnet-batch-size 250`
- `--deploy-post-subnet-cooldown-seconds 0`
- `--cni-ovs-vsctl-concurrency 1`
- `--cni-ovs-vsctl-timeout-seconds 180`
- `--interface-ready-timeout-seconds 0`

实验现象：

- compile 通过，耗时约 11.02s。
- build 通过，耗时约 71.78s。
- 1065 个 Kube-OVN Subnet 全部进入 ready，说明本轮没有卡在 subnet ready。
- subnet 阶段 `ovn-central` CPU 峰值约 2.2 核，`kube-ovn-controller`
  约 1.1 核到 1.7 核，`AddSubnet` queue 深度一度接近 1000，说明 subnet
  阶段本身仍是明显的 OVN 控制面负载。
- 进入 controller node-stream 后，每个 worker 被限制为最多 2 个 active
  not-yet-Running Pod。实际观测为 5 个 worker 上各 2 个
  `ContainerCreating`，总计 10 个。
- 即使只有这个并发度，仍立即出现大量
  `FailedCreatePodSandBox`，错误核心是
  `add nic to ovs failed context canceled by timeout`。
- 采样时事件计数已经达到 `add_nic_to_ovs_failed=35`、
  `failed_create_pod_sandbox=36`，手工事件统计为 39 条相关 timeout 事件。

结论：

`gateway-check-skip` 能消除一部分 gateway ARP/MAC resolve 等待，但它不能
解决当前 pure OVN+OVS 的核心问题：同一 worker 上只要有 2 个 workload Pod
同时进行多网卡 CNI ADD，就足以触发 OVS add-port 路径 timeout。因此
`maxActivePerNode=2` 在当前 w5 环境下不安全，不能作为 1078 规模 deploy 的
可用优化参数。

下一步：

继续测试 `node-stream maxActivePerNode=1` + `gateway-check-skip`。该方案保留
跳过 gateway check 的收益，但恢复同一 worker 同一时间只推进一个未 Running
Pod 的限制，用于验证是否能稳定跑完整个 1078 deploy，以及相对旧的严格串行方案
是否有实际耗时改善。

## 2026-06-17 12:05：gateway-check-skip + node-stream maxActive=1 跑通 1078

本轮实验目录：

`runs/ovn_profile_20260617_095318_1078_w5`

关键参数：

- `--deploy-skip-kube-ovn-gateway-check`
- `--deploy-controller-apply-mode node-stream`
- `--deploy-node-stream-max-active-per-node 1`
- `--deploy-subnet-batch-size 250`
- `--deploy-post-subnet-cooldown-seconds 0`
- `--cni-ovs-vsctl-concurrency 1`
- `--cni-ovs-vsctl-timeout-seconds 180`
- `--interface-ready-timeout-seconds 0`

阶段耗时：

- compile：10.85s
- build：70.08s
- deploy：5894.99s
- wait-ready：32.48s
- clean-after-profile：295.71s

最终状态：

- profile 总状态：`PASS`
- `wait-ready.log` 确认 1078/1078 个 Pod 全部 Running and Ready。
- 1065/1065 个 Kube-OVN Subnet 全部 ready。
- final events：
  - `add_nic_to_ovs_failed=0`
  - `failed_create_pod_sandbox=0`
  - `ovs_interface_not_ready=0`
  - `ovs_vsctl_timeout=0`
- clean-after 后 namespace `seedemu-b62-ovn-gwskip-ns1-1078` 和匹配的
  Kube-OVN IP/Subnet/VPC 资源全部删除。

与旧稳定基线对比：

- 旧稳定基线：
  `runs/ovn_profile_20260616_160915_1078_w5`
  deploy 耗时 9993.82s。
- 本轮：
  deploy 耗时 5894.99s。
- deploy 阶段减少约 4098.83s，约 41%。

结论：

当前 w5 pure OVN+OVS 环境下，`maxActivePerNode=1` 是可用的稳定 deploy
边界；`maxActivePerNode=2` 已经在前 10 个 Pod 左右复现大量
`add nic to ovs failed context canceled by timeout`，因此不应继续测试
`maxActivePerNode=3`。本轮提速来自两部分：保留每节点单 active CNI ADD 的
安全边界，同时去掉固定 900s subnet cooldown、提高 subnet apply 批量，并通过
`activation_strategy` 跳过 Kube-OVN gateway check 等待。它没有破坏 pure
OVN+OVS 的二层隔离模型，因为每个网络仍然是独立 Kube-OVN Subnet/logical
switch，Pod 仍通过对应 NAD 接入各自网络；改动只影响创建节奏和启动检查路径。

下一步更可能有效的优化方向：

1. 增加 worker 数量。保持 `maxActivePerNode=1`，把每个 worker 需要串行处理的
   Pod 数减少。例如从 w5 增加到 w11，理论上 controller/CNI 阶段应接近按每节点
   Pod 数下降，但 subnet/OVNDB 阶段不会等比例下降。
2. 优化 Kube-OVN/OVS 本地写路径。即使脚本限流为 1，CNI 内部仍可以做更明确的
   node-local add-port 队列、失败重试和超时观测，避免 kubelet 重试时制造额外长尾。
3. 拆分 OVN 控制面或进一步分片 VPC/logical router。subnet 阶段仍会产生大量
   logical switch、static route、logical router policy mutation，后续更大规模时
   单 OVNDB/单控制面仍可能成为主要瓶颈。


## 2026-06-17 11:39 CST pure OVN scale 1078 profile

- Run directory: `/home/lxl/k8s/largeScale/b62_k8s_scale/runs/ovn_profile_20260617_095318_1078_w5`.
- Namespace: `seedemu-b62-ovn-gwskip-ns1-1078`.
- Profile knobs: chart=`v1.16.2`, image_tag=`v1.16.2-seed-ifskip`, disable_extra_ovn_features=`True`, vpc_shards=`4`, ovs_vsctl_concurrency=`1`, ovs_vsctl_timeout_seconds=`180`, interface_ready_timeout_seconds=`0`, exclude_control_plane=`True`.
- Overall status: `PASS`.
- Key stages: deploy=PASS(5894.99s), wait-ready=PASS(32.48s), clean-after-profile=PASS(295.71s).
- Final Pods: `{'counts': {'Pending': 1, 'Running': 1077}, 'latency': {'count': 1078, 'max': 131.0, 'min': 0.0, 'p50': 20.0, 'p90': 21.0, 'p99': 104.0}, 'ready_latency': {'count': 1078, 'max': 131.0, 'min': 0.0, 'p50': 20.0, 'p90': 21.0, 'p99': 104.0}, 'total': 1078}`.
- Final OVN DB counts: `{'nb_logical_router_policy': {'count': 1079, 'elapsed_ms': 24, 'rc': 0, 'stderr': ''}, 'nb_logical_router_static_route': {'count': 1066, 'elapsed_ms': 24, 'rc': 0, 'stderr': ''}, 'nb_logical_switch': {'count': 1067, 'elapsed_ms': 34, 'rc': 0, 'stderr': ''}, 'nb_logical_switch_port': {'count': 3885, 'elapsed_ms': 121, 'rc': 0, 'stderr': ''}, 'sb_port_binding': {'count': 4952, 'elapsed_ms': 152, 'rc': 0, 'stderr': ''}}`.
- Max workqueue depth: `{'AddIP': 0.0, 'AddIPPool': 0.0, 'AddIptablesDnatRule': 0.0, 'AddIptablesEip': 0.0, 'AddIptablesFip': 0.0, 'AddIptablesSnatRule': 0.0, 'AddNamespace': 0.0, 'AddNode': 0.0, 'AddOrUpdateCSR': 0.0, 'AddOrUpdatePod': 1.0, 'AddOrUpdateVMIMigration': 0.0, 'AddOrUpdateVpc': 3.0, 'AddOrUpdateVpcEgressGateway': 0.0, 'AddOrUpdateVpcNatGw': 0.0, 'AddOvnDnatRule': 0.0, 'AddOvnEip': 0.0, 'AddOvnFip': 0.0, 'AddOvnSnatRule': 0.0, 'AddQoSPolicy': 0.0, 'AddService': 0.0, 'AddSubnet': 1045.0, 'AddVirtualIP': 0.0, 'AddVlan': 0.0, 'DeleteIP': 0.0, 'DeleteIPPool': 0.0, 'DeleteIptablesDnatRule': 0.0, 'DeleteIptablesEip': 0.0, 'DeleteIptablesFip': 0.0, 'DeleteIptablesSnatRule': 0.0, 'DeleteNode': 0.0, 'DeleteOvnDnatRule': 0.0, 'DeleteOvnEip': 0.0, 'DeleteOvnFip': 0.0, 'DeleteOvnSnatRule': 0.0, 'DeleteQoSPolicy': 0.0, 'DeleteSecurityGroup': 0.0, 'DeleteService': 0.0, 'DeleteSubnet': 0.0, 'DeleteVM': 0.0, 'DeleteVirtualIP': 0.0, 'DeleteVlan': 0.0, 'DeleteVpc': 0.0, 'DeleteVpcEgressGateway': 0.0, 'DeleteVpcNatGw': 0.0, 'InitVpcNatGw': 0.0, 'ResetIptablesEip': 0.0, 'ResetOvnEip': 0.0, 'SyncSecurityGroupPorts': 0.0, 'SyncVirtualPort': 0.0, 'UpdateEndpointSlice': 10.0, 'UpdateIP': 0.0, 'UpdateIPPoolStatus': 0.0, 'UpdateIptablesDnatRule': 0.0, 'UpdateIptablesEip': 0.0, 'UpdateIptablesFip': 0.0, 'UpdateIptablesSnatRule': 0.0, 'UpdateNode': 0.0, 'UpdateOvnDnatRule': 0.0, 'UpdateOvnEip': 0.0, 'UpdateOvnFip': 0.0, 'UpdateOvnSnatRule': 0.0, 'UpdatePodSecurity': 0.0, 'UpdateQoSPolicy': 0.0, 'UpdateSecurityGroup': 0.0, 'UpdateService': 0.0, 'UpdateSubnetStatus': 0.0, 'UpdateVirtualIP': 0.0, 'UpdateVirtualParents': 0.0, 'UpdateVlan': 0.0, 'UpdateVpcDnat': 0.0, 'UpdateVpcEip': 0.0, 'UpdateVpcFloatingIp': 0.0, 'UpdateVpcSnat': 0.0, 'UpdateVpcStatus': 0.0, 'UpdateVpcSubnet': 0.0}`.
- Max `ovs-vsctl` elapsed: `28 ms`; timeout/error samples: `0`.
- Pod sandbox lifecycle latency summary: `{'count': 1078, 'max': 131.0, 'min': 0.0, 'p50': 20.0, 'p90': 21.0, 'p99': 104.0}`.
- CNI ADD delta count max per node: `0.0`; average CNI ADD latency across reported nodes: `None` seconds.
- Event counters: `{'add_nic_to_ovs_failed': 0, 'failed_create_pod_sandbox': 0, 'ovs_interface_not_ready': 0, 'ovs_vsctl_timeout': 0}`.


## 2026-06-17 13:22 CST pure OVN scale 1078 profile

- Run directory: `/home/lxl/k8s/largeScale/b62_k8s_scale/runs/ovn_profile_20260617_120845_1078_w11`.
- Namespace: `seedemu-b62-ovn-gwskip-ns1-w11-1078`.
- Profile knobs: chart=`v1.16.2`, image_tag=`v1.16.2-seed-ifskip`, disable_extra_ovn_features=`True`, vpc_shards=`4`, ovs_vsctl_concurrency=`1`, ovs_vsctl_timeout_seconds=`180`, interface_ready_timeout_seconds=`0`, exclude_control_plane=`True`.
- Overall status: `PASS`.
- Key stages: deploy=PASS(2995.74s), wait-ready=PASS(1.25s), clean-after-profile=PASS(367.56s).
- Final Pods: `{'counts': {'Pending': 1, 'Running': 1077}, 'latency': {'count': 1078, 'max': 144.0, 'min': 0.0, 'p50': 19.0, 'p90': 21.0, 'p99': 120.0}, 'ready_latency': {'count': 1078, 'max': 144.0, 'min': 0.0, 'p50': 19.0, 'p90': 21.0, 'p99': 120.0}, 'total': 1078}`.
- Note: the Final Pods line above is the deploy-stage metrics sample; the following `wait-ready` stage confirmed `1078/1078` Pods Running and Ready.
- Final OVN DB counts: `{'nb_logical_router_policy': {'count': 1091, 'elapsed_ms': 17, 'rc': 0, 'stderr': ''}, 'nb_logical_router_static_route': {'count': 1066, 'elapsed_ms': 25, 'rc': 0, 'stderr': ''}, 'nb_logical_switch': {'count': 1067, 'elapsed_ms': 38, 'rc': 0, 'stderr': ''}, 'nb_logical_switch_port': {'count': 3891, 'elapsed_ms': 125, 'rc': 0, 'stderr': ''}, 'sb_port_binding': {'count': 4958, 'elapsed_ms': 148, 'rc': 0, 'stderr': ''}}`.
- Max workqueue depth: `{'AddIP': 0.0, 'AddIPPool': 0.0, 'AddIptablesDnatRule': 0.0, 'AddIptablesEip': 0.0, 'AddIptablesFip': 0.0, 'AddIptablesSnatRule': 0.0, 'AddNamespace': 0.0, 'AddNode': 0.0, 'AddOrUpdateCSR': 0.0, 'AddOrUpdatePod': 1.0, 'AddOrUpdateVMIMigration': 0.0, 'AddOrUpdateVpc': 3.0, 'AddOrUpdateVpcEgressGateway': 0.0, 'AddOrUpdateVpcNatGw': 0.0, 'AddOvnDnatRule': 0.0, 'AddOvnEip': 0.0, 'AddOvnFip': 0.0, 'AddOvnSnatRule': 0.0, 'AddQoSPolicy': 0.0, 'AddService': 0.0, 'AddSubnet': 1050.0, 'AddVirtualIP': 0.0, 'AddVlan': 0.0, 'DeleteIP': 0.0, 'DeleteIPPool': 0.0, 'DeleteIptablesDnatRule': 0.0, 'DeleteIptablesEip': 0.0, 'DeleteIptablesFip': 0.0, 'DeleteIptablesSnatRule': 0.0, 'DeleteNode': 0.0, 'DeleteOvnDnatRule': 0.0, 'DeleteOvnEip': 0.0, 'DeleteOvnFip': 0.0, 'DeleteOvnSnatRule': 0.0, 'DeleteQoSPolicy': 0.0, 'DeleteSecurityGroup': 0.0, 'DeleteService': 0.0, 'DeleteSubnet': 0.0, 'DeleteVM': 0.0, 'DeleteVirtualIP': 0.0, 'DeleteVlan': 0.0, 'DeleteVpc': 0.0, 'DeleteVpcEgressGateway': 0.0, 'DeleteVpcNatGw': 0.0, 'InitVpcNatGw': 0.0, 'ResetIptablesEip': 0.0, 'ResetOvnEip': 0.0, 'SyncSecurityGroupPorts': 0.0, 'SyncVirtualPort': 0.0, 'UpdateEndpointSlice': 10.0, 'UpdateIP': 0.0, 'UpdateIPPoolStatus': 0.0, 'UpdateIptablesDnatRule': 0.0, 'UpdateIptablesEip': 0.0, 'UpdateIptablesFip': 0.0, 'UpdateIptablesSnatRule': 0.0, 'UpdateNode': 0.0, 'UpdateOvnDnatRule': 0.0, 'UpdateOvnEip': 0.0, 'UpdateOvnFip': 0.0, 'UpdateOvnSnatRule': 0.0, 'UpdatePodSecurity': 0.0, 'UpdateQoSPolicy': 0.0, 'UpdateSecurityGroup': 0.0, 'UpdateService': 0.0, 'UpdateSubnetStatus': 0.0, 'UpdateVirtualIP': 0.0, 'UpdateVirtualParents': 0.0, 'UpdateVlan': 0.0, 'UpdateVpcDnat': 0.0, 'UpdateVpcEip': 0.0, 'UpdateVpcFloatingIp': 0.0, 'UpdateVpcSnat': 0.0, 'UpdateVpcStatus': 3.0, 'UpdateVpcSubnet': 0.0}`.
- Max `ovs-vsctl` elapsed: `17 ms`; timeout/error samples: `0`.
- Pod sandbox lifecycle latency summary: `{'count': 1078, 'max': 144.0, 'min': 0.0, 'p50': 19.0, 'p90': 21.0, 'p99': 120.0}`.
- CNI ADD delta count max per node: `254.0`; average CNI ADD latency across reported nodes: `7.5217` seconds.
- Event counters: `{'add_nic_to_ovs_failed': 0, 'failed_create_pod_sandbox': 0, 'ovs_interface_not_ready': 0, 'ovs_vsctl_timeout': 0}`.

### 结论：如何减轻 pure OVN+OVS 的 CNI 创建压力

这轮 w11 结果和前面的 w5 对比说明，最有效的方向不是简单提高全局
`kubectl apply` 并发，而是把“每个节点的本地 OVS 写压力”控制住，再通过增加
worker 数摊薄总 Pod 数。w5 下 `nodeStreamMaxActivePerNode=2` 在第一批 Pod
就复现大量 `add nic to ovs failed context canceled by timeout`；w5 下
`nodeStreamMaxActivePerNode=1` 能稳定通过但 deploy 需要 `5894.99s`；
w11 下仍保持 `nodeStreamMaxActivePerNode=1`，deploy 降到 `2995.74s`，且
`add_nic_to_ovs_failed=0`、`failed_create_pod_sandbox=0`、`ovs_vsctl_timeout=0`。

当前最可能有效的减压办法，按优先级是：

1. 增加 worker 数量，但每个 worker 继续限制为单 active CNI ADD。这样每台 VM
   的 OVS add-port 是串行的，避免本地 `br-int` 写入堆积；总吞吐则通过更多
   worker 横向摊开。w11 相比 w5 的 1078 deploy 时间从 `5894.99s` 降到
   `2995.74s`，证明这个方向有效。
2. 保持 `cniOvsVsctlConcurrency=1` 和较高 `ovs-vsctl` timeout。这个参数解决的
   是 CNI 内部本地 OVS 写并发和 30s 超时问题；它不是为了提速，而是为了稳定。
3. 继续跳过非必要的 Kube-OVN gateway/interface-ready 等待。当前使用
   `activation_strategy=rarp`、`interfaceReadyTimeoutSeconds=0` 和
   `skipKubeOvnGatewayCheck=true`，避免把等待逻辑变成 CNI ADD 长尾。
4. subnet 创建可以分批提交，但不要误认为 subnet 分批能解决 Pod CNI 长尾。subnet
   阶段的主要压力在 kube-ovn-controller/OVNDB；Pod 创建阶段的主要压力在每个
   worker 本地 OVS/CNI。两者要分别限流。
5. 对更大规模，应该考虑拆分 OVN 控制面、拆分 VPC/logical router 或多集群分片。
   这属于架构级改造，主要解决单 OVNDB 下大量 logical switch/static route/router
   policy mutation 的串行成本；它不能替代每节点 OVS 写限流，但能缓解更高规模时的
   控制面瓶颈。

注意：增加 worker 数量本身不会破坏二层隔离。只要每个仿真网络仍然对应独立的
Kube-OVN Subnet/logical switch，Pod 通过对应 NAD 接入该网络，二层隔离语义仍由
OVN logical switch 保证。这里改变的是 Pod 被调度到更多 worker，以及 CNI ADD
的节奏，不改变网络模型。


## 2026-06-17 14:42 CST pure OVN scale 1078 profile

- Run directory: `/home/lxl/k8s/largeScale/b62_k8s_scale/runs/ovn_profile_20260617_134406_1078_w11`.
- Namespace: `seedemu-b62-ovn-w11-noskip-1078`.
- Profile knobs: chart=`v1.16.2`, image_tag=`v1.16.2-seed-ifskip`, disable_extra_ovn_features=`True`, vpc_shards=`4`, ovs_vsctl_concurrency=`1`, ovs_vsctl_timeout_seconds=`180`, interface_ready_timeout_seconds=`0`, exclude_control_plane=`True`.
- Overall status: `PASS`.
- Key stages: deploy=PASS(3027.68s), wait-ready=PASS(1.08s), clean-after-profile=PASS(350.82s).
- Final Pods: `{'counts': {'Running': 1075}, 'latency': {'count': 1075, 'max': 125.0, 'min': 6.0, 'p50': 19.0, 'p90': 21.0, 'p99': 118.0}, 'ready_latency': {'count': 1075, 'max': 125.0, 'min': 6.0, 'p50': 19.0, 'p90': 21.0, 'p99': 118.0}, 'total': 1075}`.
- Final OVN DB counts: `{'nb_logical_router_policy': {'count': 1091, 'elapsed_ms': 22, 'rc': 0, 'stderr': ''}, 'nb_logical_router_static_route': {'count': 1066, 'elapsed_ms': 23, 'rc': 0, 'stderr': ''}, 'nb_logical_switch': {'count': 1067, 'elapsed_ms': 21, 'rc': 0, 'stderr': ''}, 'nb_logical_switch_port': {'count': 3888, 'elapsed_ms': 123, 'rc': 0, 'stderr': ''}, 'sb_port_binding': {'count': 4955, 'elapsed_ms': 150, 'rc': 0, 'stderr': ''}}`.
- Max workqueue depth: `{'AddIP': 0.0, 'AddIPPool': 0.0, 'AddIptablesDnatRule': 0.0, 'AddIptablesEip': 0.0, 'AddIptablesFip': 0.0, 'AddIptablesSnatRule': 0.0, 'AddNamespace': 0.0, 'AddNode': 0.0, 'AddOrUpdateCSR': 0.0, 'AddOrUpdatePod': 2.0, 'AddOrUpdateVMIMigration': 0.0, 'AddOrUpdateVpc': 3.0, 'AddOrUpdateVpcEgressGateway': 0.0, 'AddOrUpdateVpcNatGw': 0.0, 'AddOvnDnatRule': 0.0, 'AddOvnEip': 0.0, 'AddOvnFip': 0.0, 'AddOvnSnatRule': 0.0, 'AddQoSPolicy': 0.0, 'AddService': 0.0, 'AddSubnet': 1051.0, 'AddVirtualIP': 0.0, 'AddVlan': 0.0, 'DeleteIP': 0.0, 'DeleteIPPool': 0.0, 'DeleteIptablesDnatRule': 0.0, 'DeleteIptablesEip': 0.0, 'DeleteIptablesFip': 0.0, 'DeleteIptablesSnatRule': 0.0, 'DeleteNode': 0.0, 'DeleteOvnDnatRule': 0.0, 'DeleteOvnEip': 0.0, 'DeleteOvnFip': 0.0, 'DeleteOvnSnatRule': 0.0, 'DeleteQoSPolicy': 0.0, 'DeleteSecurityGroup': 0.0, 'DeleteService': 0.0, 'DeleteSubnet': 0.0, 'DeleteVM': 0.0, 'DeleteVirtualIP': 0.0, 'DeleteVlan': 0.0, 'DeleteVpc': 0.0, 'DeleteVpcEgressGateway': 0.0, 'DeleteVpcNatGw': 0.0, 'InitVpcNatGw': 0.0, 'ResetIptablesEip': 0.0, 'ResetOvnEip': 0.0, 'SyncSecurityGroupPorts': 0.0, 'SyncVirtualPort': 0.0, 'UpdateEndpointSlice': 10.0, 'UpdateIP': 0.0, 'UpdateIPPoolStatus': 0.0, 'UpdateIptablesDnatRule': 0.0, 'UpdateIptablesEip': 0.0, 'UpdateIptablesFip': 0.0, 'UpdateIptablesSnatRule': 0.0, 'UpdateNode': 0.0, 'UpdateOvnDnatRule': 0.0, 'UpdateOvnEip': 0.0, 'UpdateOvnFip': 0.0, 'UpdateOvnSnatRule': 0.0, 'UpdatePodSecurity': 0.0, 'UpdateQoSPolicy': 0.0, 'UpdateSecurityGroup': 0.0, 'UpdateService': 0.0, 'UpdateSubnetStatus': 0.0, 'UpdateVirtualIP': 0.0, 'UpdateVirtualParents': 0.0, 'UpdateVlan': 0.0, 'UpdateVpcDnat': 0.0, 'UpdateVpcEip': 0.0, 'UpdateVpcFloatingIp': 0.0, 'UpdateVpcSnat': 0.0, 'UpdateVpcStatus': 0.0, 'UpdateVpcSubnet': 0.0}`.
- Max `ovs-vsctl` elapsed: `23 ms`; timeout/error samples: `1`.
- Pod sandbox lifecycle latency summary: `{'count': 1075, 'max': 125.0, 'min': 6.0, 'p50': 19.0, 'p90': 21.0, 'p99': 118.0}`.
- CNI ADD delta count max per node: `254.0`; average CNI ADD latency across reported nodes: `7.4978` seconds.
- Event counters: `{'add_nic_to_ovs_failed': 0, 'failed_create_pod_sandbox': 6, 'ovs_interface_not_ready': 0, 'ovs_vsctl_timeout': 0}`.


## 2026-06-17 15:43 CST pure OVN scale 1078 profile

- Run directory: `/home/lxl/k8s/largeScale/b62_k8s_scale/runs/ovn_profile_20260617_144310_1078_w11`.
- Namespace: `seedemu-b62-ovn-w11-subnet50-1078`.
- Profile knobs: chart=`v1.16.2`, image_tag=`v1.16.2-seed-ifskip`, disable_extra_ovn_features=`True`, vpc_shards=`4`, ovs_vsctl_concurrency=`1`, ovs_vsctl_timeout_seconds=`180`, interface_ready_timeout_seconds=`0`, exclude_control_plane=`True`.
- Overall status: `PASS`.
- Key stages: deploy=PASS(3118.5s), wait-ready=PASS(1.33s), clean-after-profile=PASS(389.03s).
- Final Pods: `{'counts': {'Pending': 1, 'Running': 1077}, 'latency': {'count': 1078, 'max': 132.0, 'min': 0.0, 'p50': 19.0, 'p90': 21.0, 'p99': 126.0}, 'ready_latency': {'count': 1078, 'max': 132.0, 'min': 0.0, 'p50': 19.0, 'p90': 21.0, 'p99': 126.0}, 'total': 1078}`.
- Final OVN DB counts: `{'nb_logical_router_policy': {'count': 1091, 'elapsed_ms': 22, 'rc': 0, 'stderr': ''}, 'nb_logical_router_static_route': {'count': 1066, 'elapsed_ms': 23, 'rc': 0, 'stderr': ''}, 'nb_logical_switch': {'count': 1067, 'elapsed_ms': 33, 'rc': 0, 'stderr': ''}, 'nb_logical_switch_port': {'count': 3891, 'elapsed_ms': 111, 'rc': 0, 'stderr': ''}, 'sb_port_binding': {'count': 4958, 'elapsed_ms': 143, 'rc': 0, 'stderr': ''}}`.
- Max workqueue depth: `{'AddIP': 0.0, 'AddIPPool': 0.0, 'AddIptablesDnatRule': 0.0, 'AddIptablesEip': 0.0, 'AddIptablesFip': 0.0, 'AddIptablesSnatRule': 0.0, 'AddNamespace': 0.0, 'AddNode': 0.0, 'AddOrUpdateCSR': 0.0, 'AddOrUpdatePod': 2.0, 'AddOrUpdateVMIMigration': 0.0, 'AddOrUpdateVpc': 3.0, 'AddOrUpdateVpcEgressGateway': 0.0, 'AddOrUpdateVpcNatGw': 0.0, 'AddOvnDnatRule': 0.0, 'AddOvnEip': 0.0, 'AddOvnFip': 0.0, 'AddOvnSnatRule': 0.0, 'AddQoSPolicy': 0.0, 'AddService': 0.0, 'AddSubnet': 1031.0, 'AddVirtualIP': 0.0, 'AddVlan': 0.0, 'DeleteIP': 0.0, 'DeleteIPPool': 0.0, 'DeleteIptablesDnatRule': 0.0, 'DeleteIptablesEip': 0.0, 'DeleteIptablesFip': 0.0, 'DeleteIptablesSnatRule': 0.0, 'DeleteNode': 0.0, 'DeleteOvnDnatRule': 0.0, 'DeleteOvnEip': 0.0, 'DeleteOvnFip': 0.0, 'DeleteOvnSnatRule': 0.0, 'DeleteQoSPolicy': 0.0, 'DeleteSecurityGroup': 0.0, 'DeleteService': 0.0, 'DeleteSubnet': 0.0, 'DeleteVM': 0.0, 'DeleteVirtualIP': 0.0, 'DeleteVlan': 0.0, 'DeleteVpc': 0.0, 'DeleteVpcEgressGateway': 0.0, 'DeleteVpcNatGw': 0.0, 'InitVpcNatGw': 0.0, 'ResetIptablesEip': 0.0, 'ResetOvnEip': 0.0, 'SyncSecurityGroupPorts': 0.0, 'SyncVirtualPort': 0.0, 'UpdateEndpointSlice': 10.0, 'UpdateIP': 0.0, 'UpdateIPPoolStatus': 0.0, 'UpdateIptablesDnatRule': 0.0, 'UpdateIptablesEip': 0.0, 'UpdateIptablesFip': 0.0, 'UpdateIptablesSnatRule': 0.0, 'UpdateNode': 0.0, 'UpdateOvnDnatRule': 0.0, 'UpdateOvnEip': 0.0, 'UpdateOvnFip': 0.0, 'UpdateOvnSnatRule': 0.0, 'UpdatePodSecurity': 0.0, 'UpdateQoSPolicy': 0.0, 'UpdateSecurityGroup': 0.0, 'UpdateService': 0.0, 'UpdateSubnetStatus': 0.0, 'UpdateVirtualIP': 0.0, 'UpdateVirtualParents': 0.0, 'UpdateVlan': 0.0, 'UpdateVpcDnat': 0.0, 'UpdateVpcEip': 0.0, 'UpdateVpcFloatingIp': 0.0, 'UpdateVpcSnat': 0.0, 'UpdateVpcStatus': 1.0, 'UpdateVpcSubnet': 0.0}`.
- Max `ovs-vsctl` elapsed: `18 ms`; timeout/error samples: `0`.
- Pod sandbox lifecycle latency summary: `{'count': 1078, 'max': 132.0, 'min': 0.0, 'p50': 19.0, 'p90': 21.0, 'p99': 126.0}`.
- CNI ADD delta count max per node: `254.0`; average CNI ADD latency across reported nodes: `7.5538` seconds.
- Event counters: `{'add_nic_to_ovs_failed': 0, 'failed_create_pod_sandbox': 3, 'ovs_interface_not_ready': 0, 'ovs_vsctl_timeout': 0}`.

## 2026-06-17 15:45 CST 1078 deploy optimization sweep conclusion

本轮只评估 deploy 是否能稳定完成，并保持 pure OVN+OVS 语义。核心结论是：
CNI 创建压力的关键边界在“单 worker 本地 OVS 写入并发”，不是单纯的全局 Pod
数量。增加 worker 有明显收益，但前提是每个 worker 上仍然只允许 1 个 active
CNI ADD；把单节点并发提高到 2，即使在 w11 环境下也会迅速复现 add-port 超时类
失败。

对比结果：

| 配置 | 结果 | deploy 耗时 | 关键现象 |
| --- | --- | ---: | --- |
| w5, `maxActivePerNode=1`, gateway skip, subnet batch 250 | PASS | 5894.99s | 0 个 OVS/CNI 失败事件 |
| w11, `maxActivePerNode=1`, gateway skip, subnet batch 250 | PASS | 2995.74s | 0 个 OVS/CNI 失败事件 |
| w11, `maxActivePerNode=2`, gateway skip, subnet batch 250 | 中断于失败证据充分后 | 606.82s 时中断 | `add_nic_to_ovs_failed=233`, `failed_create_pod_sandbox=241`, CNI 日志中 `ovs-vsctl` timeout=322 |
| w11, `maxActivePerNode=1`, 不跳过 gateway check, subnet batch 250 | PASS | 3027.68s | 总耗时只比 skip 慢约 32s，说明 gateway skip 在该稳定配置下收益很小 |
| w11, `maxActivePerNode=1`, gateway skip, subnet batch 50 | PASS | 3118.5s | 比 batch 250 慢约 123s，减小 subnet batch 没有改善 CNI 压力 |

减轻 CNI 创建压力最可能有效的具体办法，按优先级如下：

1. 增加 worker 数量，同时保持 `nodeStreamMaxActivePerNode=1`。w11 相比 w5
   将 1078 deploy 从 `5894.99s` 降到 `2995.74s`，这是本轮最明确的收益。
   原因是每台 worker 需要串行处理的 Pod 数减少，但每台机器本地 OVS add-port
   仍然不并发。
2. 限制 CNI/OVS 本地写入并发。继续使用 `cniOvsVsctlConcurrency=1`，
   `cniOvsVsctlTimeoutSeconds=180`，并在 deploy 层用
   `nodeStreamMaxActivePerNode=1` 控制每个节点的 active CNI ADD。w11 下
   `maxActivePerNode=2` 仍失败，说明这个边界不能简单放宽。
3. 保持 worker-only placement，避免控制面节点参与大量仿真 Pod CNI ADD。
   当前 `excludeControlPlane=true` 是合理的；master 留给 apiserver、Kube-OVN、
   OVNDB、northd 等控制面组件更稳。
4. subnet 分批提交可以避免一次性 API 压力过大，但它主要影响
   kube-ovn-controller/OVNDB 的 Subnet 队列，不直接解决 Pod CNI ADD 长尾。
   1078 下 batch 250 比 batch 50 更快且同样稳定，因此当前建议保留
   `deploySubnetBatchSize=250`。
5. 跳过非必要 gateway/interface-ready 等待可作为辅助优化，但不是主因。本轮
   w11 下 skip 与 no-skip 的 deploy 差距只有约 32s；保留 skip 的理由是减少无用
   等待路径和更大规模时的长尾风险。
6. 若继续扩大到 2599/4954，应优先做 worker 数量曲线，例如 w11 -> w15/w16，
   并保持每节点 active=1。若仍卡在 subnet/OVNDB 阶段，则下一步才是拆分
   OVN 控制面、拆分 VPC/logical router、或者多集群/多 OVNDB 分片。

这些优化不改变二层隔离语义。只要每个仿真网络仍生成独立 Kube-OVN
Subnet/logical switch，Pod 仍通过对应 NAD 接入该 logical switch，那么隔离仍由
OVN logical switch 保证；增加 worker 和限制创建节奏只改变创建路径，不改变网络
模型。


## 2026-06-17 19:54 CST pure OVN scale 1078 profile

- Run directory: `/home/lxl/k8s/largeScale/b62_k8s_scale/runs/ovn_profile_20260617_182338_1078_w24`.
- Namespace: `seedemu-b62-ovn-w24-curve-1078`.
- Profile knobs: chart=`v1.16.2`, image_tag=`v1.16.2-seed-ifskip`, disable_extra_ovn_features=`True`, vpc_shards=`4`, ovs_vsctl_concurrency=`1`, ovs_vsctl_timeout_seconds=`180`, interface_ready_timeout_seconds=`0`, exclude_control_plane=`True`.
- Overall status: `PASS`.
- Key stages: deploy=PASS(3043.72s), wait-ready=PASS(32.28s), clean-after-profile=PASS(378.23s).
- Final Pods: `{'counts': {'Running': 1077}, 'latency': {'count': 1077, 'max': 133.0, 'min': 6.0, 'p50': 20.0, 'p90': 46.0, 'p99': 130.0}, 'ready_latency': {'count': 1077, 'max': 133.0, 'min': 6.0, 'p50': 20.0, 'p90': 46.0, 'p99': 130.0}, 'total': 1077}`.
- Final OVN DB counts: `{'nb_logical_router_policy': {'count': 1117, 'elapsed_ms': 24, 'rc': 0, 'stderr': ''}, 'nb_logical_router_static_route': {'count': 1066, 'elapsed_ms': 22, 'rc': 0, 'stderr': ''}, 'nb_logical_switch': {'count': 1067, 'elapsed_ms': 32, 'rc': 0, 'stderr': ''}, 'nb_logical_switch_port': {'count': 3901, 'elapsed_ms': 123, 'rc': 0, 'stderr': ''}, 'sb_port_binding': {'count': 4970, 'elapsed_ms': 164, 'rc': 0, 'stderr': ''}}`.
- Max workqueue depth: `{'AddIP': 0.0, 'AddIPPool': 0.0, 'AddIptablesDnatRule': 0.0, 'AddIptablesEip': 0.0, 'AddIptablesFip': 0.0, 'AddIptablesSnatRule': 0.0, 'AddNamespace': 0.0, 'AddNode': 0.0, 'AddOrUpdateCSR': 0.0, 'AddOrUpdatePod': 3.0, 'AddOrUpdateVMIMigration': 0.0, 'AddOrUpdateVpc': 3.0, 'AddOrUpdateVpcEgressGateway': 0.0, 'AddOrUpdateVpcNatGw': 0.0, 'AddOvnDnatRule': 0.0, 'AddOvnEip': 0.0, 'AddOvnFip': 0.0, 'AddOvnSnatRule': 0.0, 'AddQoSPolicy': 0.0, 'AddService': 0.0, 'AddSubnet': 1059.0, 'AddVirtualIP': 0.0, 'AddVlan': 0.0, 'DeleteIP': 0.0, 'DeleteIPPool': 0.0, 'DeleteIptablesDnatRule': 0.0, 'DeleteIptablesEip': 0.0, 'DeleteIptablesFip': 0.0, 'DeleteIptablesSnatRule': 0.0, 'DeleteNode': 0.0, 'DeleteOvnDnatRule': 0.0, 'DeleteOvnEip': 0.0, 'DeleteOvnFip': 0.0, 'DeleteOvnSnatRule': 0.0, 'DeleteQoSPolicy': 0.0, 'DeleteSecurityGroup': 0.0, 'DeleteService': 0.0, 'DeleteSubnet': 0.0, 'DeleteVM': 0.0, 'DeleteVirtualIP': 0.0, 'DeleteVlan': 0.0, 'DeleteVpc': 0.0, 'DeleteVpcEgressGateway': 0.0, 'DeleteVpcNatGw': 0.0, 'InitVpcNatGw': 0.0, 'ResetIptablesEip': 0.0, 'ResetOvnEip': 0.0, 'SyncSecurityGroupPorts': 0.0, 'SyncVirtualPort': 0.0, 'UpdateEndpointSlice': 10.0, 'UpdateIP': 0.0, 'UpdateIPPoolStatus': 0.0, 'UpdateIptablesDnatRule': 0.0, 'UpdateIptablesEip': 0.0, 'UpdateIptablesFip': 0.0, 'UpdateIptablesSnatRule': 0.0, 'UpdateNode': 0.0, 'UpdateOvnDnatRule': 0.0, 'UpdateOvnEip': 0.0, 'UpdateOvnFip': 0.0, 'UpdateOvnSnatRule': 0.0, 'UpdatePodSecurity': 0.0, 'UpdateQoSPolicy': 0.0, 'UpdateSecurityGroup': 0.0, 'UpdateService': 0.0, 'UpdateSubnetStatus': 0.0, 'UpdateVirtualIP': 0.0, 'UpdateVirtualParents': 0.0, 'UpdateVlan': 0.0, 'UpdateVpcDnat': 0.0, 'UpdateVpcEip': 0.0, 'UpdateVpcFloatingIp': 0.0, 'UpdateVpcSnat': 0.0, 'UpdateVpcStatus': 2.0, 'UpdateVpcSubnet': 0.0}`.
- Max `ovs-vsctl` elapsed: `20 ms`; timeout/error samples: `0`.
- Pod sandbox lifecycle latency summary: `{'count': 1077, 'max': 133.0, 'min': 6.0, 'p50': 20.0, 'p90': 46.0, 'p99': 130.0}`.
- CNI ADD delta count max per node: `0.0`; average CNI ADD latency across reported nodes: `None` seconds.
- Event counters: `{'add_nic_to_ovs_failed': 0, 'failed_create_pod_sandbox': 0, 'ovs_interface_not_ready': 0, 'ovs_vsctl_timeout': 0}`.


## 2026-06-17 21:44 CST pure OVN scale 1078 profile

- Run directory: `/home/lxl/k8s/largeScale/b62_k8s_scale/runs/ovn_profile_20260617_200657_1078_w32`.
- Namespace: `seedemu-b62-ovn-w32-curve-1078`.
- Profile knobs: chart=`v1.16.2`, image_tag=`v1.16.2-seed-ifskip`, disable_extra_ovn_features=`True`, vpc_shards=`4`, ovs_vsctl_concurrency=`1`, ovs_vsctl_timeout_seconds=`180`, interface_ready_timeout_seconds=`0`, exclude_control_plane=`True`.
- Overall status: `PASS`.
- Key stages: deploy=PASS(3005.81s), wait-ready=PASS(32.3s), clean-after-profile=PASS(334.8s).
- Final Pods: `{'counts': {'Running': 1078}, 'latency': {'count': 1078, 'max': 142.0, 'min': 6.0, 'p50': 20.0, 'p90': 91.0, 'p99': 130.0}, 'ready_latency': {'count': 1078, 'max': 142.0, 'min': 6.0, 'p50': 20.0, 'p90': 91.0, 'p99': 130.0}, 'total': 1078}`.
- Final OVN DB counts: `{'nb_logical_router_policy': {'count': 1133, 'elapsed_ms': 22, 'rc': 0, 'stderr': ''}, 'nb_logical_router_static_route': {'count': 1066, 'elapsed_ms': 22, 'rc': 0, 'stderr': ''}, 'nb_logical_switch': {'count': 1067, 'elapsed_ms': 31, 'rc': 0, 'stderr': ''}, 'nb_logical_switch_port': {'count': 3912, 'elapsed_ms': 117, 'rc': 0, 'stderr': ''}, 'sb_port_binding': {'count': 4979, 'elapsed_ms': 128, 'rc': 0, 'stderr': ''}}`.
- Max workqueue depth: `{'AddIP': 0.0, 'AddIPPool': 0.0, 'AddIptablesDnatRule': 0.0, 'AddIptablesEip': 0.0, 'AddIptablesFip': 0.0, 'AddIptablesSnatRule': 0.0, 'AddNamespace': 0.0, 'AddNode': 0.0, 'AddOrUpdateCSR': 0.0, 'AddOrUpdatePod': 8.0, 'AddOrUpdateVMIMigration': 0.0, 'AddOrUpdateVpc': 3.0, 'AddOrUpdateVpcEgressGateway': 0.0, 'AddOrUpdateVpcNatGw': 0.0, 'AddOvnDnatRule': 0.0, 'AddOvnEip': 0.0, 'AddOvnFip': 0.0, 'AddOvnSnatRule': 0.0, 'AddQoSPolicy': 0.0, 'AddService': 0.0, 'AddSubnet': 1051.0, 'AddVirtualIP': 0.0, 'AddVlan': 0.0, 'DeleteIP': 0.0, 'DeleteIPPool': 0.0, 'DeleteIptablesDnatRule': 0.0, 'DeleteIptablesEip': 0.0, 'DeleteIptablesFip': 0.0, 'DeleteIptablesSnatRule': 0.0, 'DeleteNode': 0.0, 'DeleteOvnDnatRule': 0.0, 'DeleteOvnEip': 0.0, 'DeleteOvnFip': 0.0, 'DeleteOvnSnatRule': 0.0, 'DeleteQoSPolicy': 0.0, 'DeleteSecurityGroup': 0.0, 'DeleteService': 0.0, 'DeleteSubnet': 0.0, 'DeleteVM': 0.0, 'DeleteVirtualIP': 0.0, 'DeleteVlan': 0.0, 'DeleteVpc': 0.0, 'DeleteVpcEgressGateway': 0.0, 'DeleteVpcNatGw': 0.0, 'InitVpcNatGw': 0.0, 'ResetIptablesEip': 0.0, 'ResetOvnEip': 0.0, 'SyncSecurityGroupPorts': 0.0, 'SyncVirtualPort': 0.0, 'UpdateEndpointSlice': 10.0, 'UpdateIP': 0.0, 'UpdateIPPoolStatus': 0.0, 'UpdateIptablesDnatRule': 0.0, 'UpdateIptablesEip': 0.0, 'UpdateIptablesFip': 0.0, 'UpdateIptablesSnatRule': 0.0, 'UpdateNode': 0.0, 'UpdateOvnDnatRule': 0.0, 'UpdateOvnEip': 0.0, 'UpdateOvnFip': 0.0, 'UpdateOvnSnatRule': 0.0, 'UpdatePodSecurity': 0.0, 'UpdateQoSPolicy': 0.0, 'UpdateSecurityGroup': 0.0, 'UpdateService': 0.0, 'UpdateSubnetStatus': 0.0, 'UpdateVirtualIP': 0.0, 'UpdateVirtualParents': 0.0, 'UpdateVlan': 0.0, 'UpdateVpcDnat': 0.0, 'UpdateVpcEip': 0.0, 'UpdateVpcFloatingIp': 0.0, 'UpdateVpcSnat': 0.0, 'UpdateVpcStatus': 0.0, 'UpdateVpcSubnet': 0.0}`.
- Max `ovs-vsctl` elapsed: `36 ms`; timeout/error samples: `0`.
- Pod sandbox lifecycle latency summary: `{'count': 1078, 'max': 142.0, 'min': 6.0, 'p50': 20.0, 'p90': 91.0, 'p99': 130.0}`.
- CNI ADD delta count max per node: `0.0`; average CNI ADD latency across reported nodes: `None` seconds.
- Event counters: `{'add_nic_to_ovs_failed': 0, 'failed_create_pod_sandbox': 2, 'ovs_interface_not_ready': 0, 'ovs_vsctl_timeout': 0}`.


## 2026-06-17 23:47 CST pure OVN scale 1078 profile

- Run directory: `/home/lxl/k8s/largeScale/b62_k8s_scale/runs/ovn_profile_20260617_214511_1078_w48`.
- Namespace: `seedemu-b62-ovn-w48-curve-1078`.
- Profile knobs: chart=`v1.16.2`, image_tag=`v1.16.2-seed-ifskip`, disable_extra_ovn_features=`True`, vpc_shards=`4`, ovs_vsctl_concurrency=`1`, ovs_vsctl_timeout_seconds=`180`, interface_ready_timeout_seconds=`0`, exclude_control_plane=`True`.
- Overall status: `PASS`.
- Key stages: deploy=PASS(3051.77s), wait-ready=PASS(32.44s), clean-after-profile=PASS(378.02s).
- Final Pods: `{'counts': {'Pending': 1, 'Running': 1077}, 'latency': {'count': 1078, 'max': 148.0, 'min': 0.0, 'p50': 20.0, 'p90': 107.0, 'p99': 133.0}, 'ready_latency': {'count': 1078, 'max': 148.0, 'min': 0.0, 'p50': 20.0, 'p90': 107.0, 'p99': 133.0}, 'total': 1078}`.
- Final OVN DB counts: `{'nb_logical_router_policy': {'count': 1165, 'elapsed_ms': 21, 'rc': 0, 'stderr': ''}, 'nb_logical_router_static_route': {'count': 1066, 'elapsed_ms': 21, 'rc': 0, 'stderr': ''}, 'nb_logical_switch': {'count': 1067, 'elapsed_ms': 33, 'rc': 0, 'stderr': ''}, 'nb_logical_switch_port': {'count': 3928, 'elapsed_ms': 106, 'rc': 0, 'stderr': ''}, 'sb_port_binding': {'count': 4995, 'elapsed_ms': 151, 'rc': 0, 'stderr': ''}}`.
- Max workqueue depth: `{'AddIP': 0.0, 'AddIPPool': 0.0, 'AddIptablesDnatRule': 0.0, 'AddIptablesEip': 0.0, 'AddIptablesFip': 0.0, 'AddIptablesSnatRule': 0.0, 'AddNamespace': 0.0, 'AddNode': 0.0, 'AddOrUpdateCSR': 0.0, 'AddOrUpdatePod': 16.0, 'AddOrUpdateVMIMigration': 0.0, 'AddOrUpdateVpc': 3.0, 'AddOrUpdateVpcEgressGateway': 0.0, 'AddOrUpdateVpcNatGw': 0.0, 'AddOvnDnatRule': 0.0, 'AddOvnEip': 0.0, 'AddOvnFip': 0.0, 'AddOvnSnatRule': 0.0, 'AddQoSPolicy': 0.0, 'AddService': 0.0, 'AddSubnet': 1037.0, 'AddVirtualIP': 0.0, 'AddVlan': 0.0, 'DeleteIP': 0.0, 'DeleteIPPool': 0.0, 'DeleteIptablesDnatRule': 0.0, 'DeleteIptablesEip': 0.0, 'DeleteIptablesFip': 0.0, 'DeleteIptablesSnatRule': 0.0, 'DeleteNode': 0.0, 'DeleteOvnDnatRule': 0.0, 'DeleteOvnEip': 0.0, 'DeleteOvnFip': 0.0, 'DeleteOvnSnatRule': 0.0, 'DeleteQoSPolicy': 0.0, 'DeleteSecurityGroup': 0.0, 'DeleteService': 0.0, 'DeleteSubnet': 0.0, 'DeleteVM': 0.0, 'DeleteVirtualIP': 0.0, 'DeleteVlan': 0.0, 'DeleteVpc': 0.0, 'DeleteVpcEgressGateway': 0.0, 'DeleteVpcNatGw': 0.0, 'InitVpcNatGw': 0.0, 'ResetIptablesEip': 0.0, 'ResetOvnEip': 0.0, 'SyncSecurityGroupPorts': 0.0, 'SyncVirtualPort': 0.0, 'UpdateEndpointSlice': 10.0, 'UpdateIP': 0.0, 'UpdateIPPoolStatus': 0.0, 'UpdateIptablesDnatRule': 0.0, 'UpdateIptablesEip': 0.0, 'UpdateIptablesFip': 0.0, 'UpdateIptablesSnatRule': 0.0, 'UpdateNode': 0.0, 'UpdateOvnDnatRule': 0.0, 'UpdateOvnEip': 0.0, 'UpdateOvnFip': 0.0, 'UpdateOvnSnatRule': 0.0, 'UpdatePodSecurity': 0.0, 'UpdateQoSPolicy': 0.0, 'UpdateSecurityGroup': 0.0, 'UpdateService': 0.0, 'UpdateSubnetStatus': 0.0, 'UpdateVirtualIP': 0.0, 'UpdateVirtualParents': 0.0, 'UpdateVlan': 0.0, 'UpdateVpcDnat': 0.0, 'UpdateVpcEip': 0.0, 'UpdateVpcFloatingIp': 0.0, 'UpdateVpcSnat': 0.0, 'UpdateVpcStatus': 3.0, 'UpdateVpcSubnet': 0.0}`.
- Max `ovs-vsctl` elapsed: `19 ms`; timeout/error samples: `0`.
- Pod sandbox lifecycle latency summary: `{'count': 1078, 'max': 148.0, 'min': 0.0, 'p50': 20.0, 'p90': 107.0, 'p99': 133.0}`.
- CNI ADD delta count max per node: `0.0`; average CNI ADD latency across reported nodes: `None` seconds.
- Event counters: `{'add_nic_to_ovs_failed': 0, 'failed_create_pod_sandbox': 8, 'ovs_interface_not_ready': 0, 'ovs_vsctl_timeout': 0}`.

## 2026-06-17 23:52 CST worker 数量 24/32/48 递增结论

本轮按用户要求继续测试 `worker_num=24/32/48`，配置保持 pure OVN+OVS：
`nodeStreamMaxActivePerNode=1`、`cniOvsVsctlConcurrency=1`、
`cniOvsVsctlTimeoutSeconds=180`、`interfaceReadyTimeoutSeconds=0`、
`vpcShards=4`、Subnet batch 250、跳过 Kube-OVN gateway check。三轮都能完整
deploy 1078 并通过 wait-ready，说明当前稳定性边界是成立的。

对比结果：

| worker 数 | build-cluster | deploy | wait-ready | clean-after | 结果 |
| ---: | ---: | ---: | ---: | ---: | --- |
| 11 | 880.05s | 2995.74s | 1.25s | 367.56s | PASS |
| 24 | 1729.74s | 3043.72s | 32.28s | 378.23s | PASS |
| 32 | 2204.34s | 3005.81s | 32.30s | 334.80s | PASS |
| 48 | 3315.12s | 3051.77s | 32.44s | 378.02s | PASS |

结论是：从 w11 继续增加到 w24/w32/w48 并没有继续显著缩短 deploy。原因不是
OVS/CNI 失败，而是当前 placement 产生了稳定的热点节点长尾。三轮的 node-stream
计划都显示相同热点：`worker1=86`、`worker10=64`、`worker11=64`、
`worker12=50`、`worker13=42`。由于每个节点只能有 1 个 active CNI ADD，
deploy 后半段会被 `worker1` 的 86 个 Pod 串行 CNI ADD 决定；新增 worker 只减少
了低负载节点的 Pod 数，对这些热点没有帮助。w48 的实际过程也验证了这一点：
大部分节点结束后，最后只剩 `worker1` 从 65/86 一直串行推进到 86/86。

事件层面没有复现之前的 OVS add-port 硬失败。w24 事件计数为 0；w32 有 2 个、
w48 有 8 个 `FailedCreatePodSandBox`，但事件内容是 Multus 查询 apiserver 的
`context deadline exceeded`，后续均自动重试并 Running；`add_nic_to_ovs_failed`、
`ovs_interface_not_ready`、`ovs_vsctl_timeout` 都是 0。w48 的 wait-ready 确认
1078/1078 Pods Running and Ready。

本轮还修复了两个与 worker 扩展相关的环境问题：

1. `k3s.clusterCidr=10.42.0.0/16` 配合 `/20` 每节点 PodCIDR 只能分配 16 个节点，
   对 w24/w32/w48 不够。已改为 `10.48.0.0/12`，并在
   `renderAssignmentConfig.py` 加入 CIDR 容量校验，后续 worker 数不足会提前报错，
   不再等到 K3s/Flannel 阶段才表现为节点无 PodCIDR。
2. 不同 worker 数反复重建时，旧 libvirt 网络会留下 `virbrb62wXX` 桥并导致
   `Network is already in use by interface`。已在 `prepareLibvirtDhcp.py` 中加入
   同前缀 stale sibling network 清理逻辑，w48 启动时已自动清理旧 w32 网络并继续。

本轮对 k8sTools 也做了一个必要修复：Multus bootstrap 镜像现在会在 Ansible
安装 K3s 前复制到 `/var/lib/rancher/k3s/agent/images`，避免 worker 启动时走外网
拉取；同时把 worker K3s agent 安装任务的 `creates` 从 agent 目录改成
`/etc/systemd/system/k3s-agent.service`，否则预加载镜像目录会让 Ansible 误判
K3s agent 已安装。

下一步优化方向不应是单纯把 worker 数继续加到 64 或更高。更有效的是先修复
placement，让热点节点更均衡，例如按计划 Pod 数或预估 CNI 接口数做 worker 分配；
或者在不触发 OVS 超时的前提下，只对热点节点做自适应小并发，但这需要谨慎验证，
因为全局 `maxActivePerNode=2` 已经明确不安全。
