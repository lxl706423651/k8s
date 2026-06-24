# B62 4954 多 VM macvlan+VLAN 实验报告

更新时间：2026-06-21 12:33 CST

本文记录 B62 4954 规模在 macvlan+VLAN 网络模式下，分别使用
9/12/18/24/32 台总 VM 的全流程实验结果。本文中的“总 VM 数”指 K3s master
加 worker 的总数；本轮实验中 master 也参与运行 SeedEMU workload Pod。

## 1. 实验目标

本轮实验要验证 macvlan+VLAN 是否能在保留每条仿真链路二层隔离语义的同时，
避开 pure OVN+OVS attached network 在大规模下出现的 CNI ADD 压力。

每组 VM 数都执行完整流程：

1. 渲染 assignment 和 KVM resource plan。
2. 按目标 VM 数重建或准备 K3s/KVM 集群。
3. 编译 4954 router 拓扑。
4. 构建并预加载 router 镜像。
5. 部署全部 Kubernetes workload。
6. 等待全部 Pod Running/Ready。
7. 执行 `start-bird`。
8. 抽取 10 个固定 AS representative 验证 BIRD 协议状态。
9. 执行 `start-kernel`，切换 BIRD kernel export。
10. 抽取 10 个 AS representative 验证 Linux FIB。
11. 执行 reconvergence 阶段。

本轮 reconvergence 使用用户指定的简化判定：

- 故障侧：对选定目标执行 `birdc down`，通过 load gate 后，只要验证 Pod 的
  `ip route` 数量出现减少，就认为故障收敛已被观测到。
- 恢复侧：对选定目标执行 `birdc configure`/恢复命令，通过 load gate 后，只要
  验证 Pod 的 `ip route` 数量出现增加，就认为恢复收敛已被观测到。

## 2. 关键参数

### 2.1 网络和调度

- 网络模式：macvlan+VLAN。
- 规模：4954 个 router-like Pod。
- master 是否运行 workload：是。
- VLAN 方案：使用两条 KVM trunk 接口，避免单一 4094 VLAN ID 上限限制。

### 2.2 deploy 参数

- `deploy.batchSize=80`
- `deploy.warmupBatches=12`
- `deploy.warmupBatchSize=1`
- `deploy.postControllerApplySettleSeconds=5`
- `deploy.waitKubeOvnSubnets=false`
- `deploy.postSubnetCooldownSeconds=0`
- `deploy.maxPendingPods=160`
- `deploy.maxCreatingPods=160`
- `deploy.maxCreatingPodsPerNode=40`
- `deploy.nodeStreamMaxActivePerNode=4`

在 macvlan+VLAN 下，deploy 的主要压力不是 OVN CNI ADD，而是 workload 创建前
需要在 K3s 节点上准备 VLAN parent interface。

### 2.3 BIRD 参数

`start-bird`：

- 执行方式：node-local `nsenter`
- 节点并发：所有节点并发
- 节点内执行：串行
- 容器间隔：`0.1s`
- load 阈值：`80`

`start-kernel`：

- 执行方式：node-local `nsenter`
- 节点并发：所有节点并发
- 节点内执行：串行
- 容器间隔：`0.3s`
- load 阈值：`80`
- kernel export mode：`all`

### 2.4 reconvergence 参数

- load 阈值：`80`
- load 检查间隔：`15s`
- route 检查间隔：`10s`
- 单阶段 timeout：`1800s`
- 单次 exec timeout：`45s`

## 3. 实验目录

| 总 VM 数 | 实验目录 |
| ---: | --- |
| 9 | `runs/macvlan_vlan_vm_sweep_20260620_115812_4954/20260620_115812_4954_vm9_w8` |
| 12 | `runs/macvlan_vlan_vm_sweep_20260620_131934_4954/20260620_131934_4954_vm12_w11` |
| 18 | `runs/macvlan_vlan_vm_sweep_20260620_145720_4954/20260620_145720_4954_vm18_w17` |
| 24 | `runs/macvlan_vlan_vm_sweep_20260620_145720_4954/20260620_145720_4954_vm24_w23` |
| 32 | `runs/macvlan_vlan_vm_sweep_20260620_145720_4954/20260620_145720_4954_vm32_w31` |

## 4. 全流程阶段结果

5 组实验最终均为 `PASS`。

| 总 VM 数 | 状态 | render | destroy existing | DHCP/net | build cluster | compile | build images | deploy | wait-ready | start-bird | verify BIRD | start-kernel | verify FIB | reconvergence |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 9 | PASS | 0.06s | 19.20s | 13.49s | 545.92s | 227.59s | 344.34s | 2269.76s | 232.02s | 160.73s | 9.18s | 317.82s | 9.52s | 21.97s |
| 12 | PASS | 0.06s | 22.02s | 15.93s | 671.14s | 227.57s | 315.86s | 2697.70s | 200.23s | 191.22s | 8.97s | 304.81s | 9.33s | 63.00s |
| 18 | PASS | 0.06s | 13.02s | 20.12s | 921.49s | 232.02s | 362.15s | 3350.44s | 167.60s | 87.73s | 51.32s | 157.06s | 9.48s | 16.75s |
| 24 | PASS | 0.06s | 192.17s | 25.19s | 1130.11s | 226.68s | 374.94s | 3878.46s | 199.78s | 67.57s | 50.63s | 119.51s | 9.52s | 15.72s |
| 32 | PASS | 0.06s | 195.98s | 32.25s | 1468.90s | 227.80s | 387.54s | 4521.16s | 167.93s | 53.56s | 69.99s | 91.48s | 10.17s | 15.93s |

## 5. reconvergence 结果

| 总 VM 数 | 状态 | fault injection | failure convergence | recovery injection | recovery convergence | reconvergence 总耗时 |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 9 | PASS | 3.91s | 3.74s | 5.95s | 0.86s | 21.72s |
| 12 | PASS | 3.10s | 27.80s | 4.48s | 9.04s | 62.75s |
| 18 | PASS | 3.01s | 1.11s | 3.84s | 0.82s | 16.49s |
| 24 | PASS | 2.81s | 0.86s | 3.79s | 0.82s | 15.49s |
| 32 | PASS | 2.91s | 0.78s | 3.77s | 0.83s | 15.67s |

12 VM 是 reconvergence 的异常点，故障侧和恢复侧观测窗口明显更长，但仍满足本轮
定义的 route count 减少/增加判定。

## 6. 多 VM 实验现象

### 6.1 BIRD 阶段随 VM 数增加明显变快

`start-bird` 和 `start-kernel` 的耗时随 VM 数增加显著下降：

- `start-bird`：9 VM 为 `160.73s`，32 VM 为 `53.56s`。
- `start-kernel`：9 VM 为 `317.82s`，32 VM 为 `91.48s`。

原因和执行模型一致：所有节点并发，但节点内串行。VM 数增加后，每台 VM 上的 Pod
数量减少，因此最慢节点完成得更快。

### 6.2 deploy 随 VM 数增加变慢

deploy 从 9 VM 的 `2269.76s` 增加到 32 VM 的 `4521.16s`。这不是 workload CNI
失败导致的；32 VM 中 workload Pod 创建最终达到 `4954/4954 Running`，且最终
`Failed=0`。

主要原因是当前 macvlan+VLAN 的 VLAN parent 准备策略：

- manifest 需要 4763 个 VLAN parent interface。
- 本轮实验当时的 deploy 会在每个 K3s 节点上准备这些 VLAN parent。
- 当时实现按节点串行执行。

因此，增加 VM 数会减少 BIRD 阶段的每节点工作量，但会增加重复的 VLAN parent
准备工作。这个问题已经在后续代码中优化为按节点准备：deploy 会从 workload 的
固定 `kubernetes.io/hostname` placement 和 Multus network annotation 推导每个
节点实际需要的 VLAN parent；无法推导固定节点的 workload 会安全回退为全节点准备。
因此后续新实验的 deploy 耗时不应再直接用本节历史数据外推。

### 6.3 macvlan+VLAN 的 workload CNI ADD 是健康的

以 32 VM 为例，VLAN parent 准备完成后，workload Pod 创建阶段持续推进：

- Pod 状态主要为 Running，只存在少量短暂的 `ContainerCreating`。
- 没有出现长期 Creating 堆积。
- 没有出现 workload Pod 最终 Failed。
- `wait-ready` 确认全部 Pod Running/Ready。

这与之前 pure OVN+OVS 的 CNI ADD 长尾或 timeout 现象不同。

## 7. 为什么 OVN+OVS 下 CNI ADD 压力很重

先把术语说清楚：Kubernetes 创建 Pod 时，kubelet 会调用 CNI 插件给 Pod 创建网卡。
这个调用通常被称为 CNI ADD。对普通用户来说，它看起来只是“给容器插一张网卡”，
但在 pure OVN+OVS 模式下，这一步实际会触发一整条控制面和数据面配置链。

在 B62 这类 SeedEMU 拓扑中，每条仿真 link 都需要表达成一个独立二层网络。pure
OVN+OVS 的做法是：每条 link 对应 Kube-OVN/OVN 中的 logical network，每个 Pod
接入该 link 时，都要把一个容器接口接到宿主机 OVS `br-int` 上，并让 OVN 控制面
知道这个端口属于哪条 logical switch。

### 7.1 pure OVN+OVS 需要配置哪些对象

一个 workload Pod 的 secondary interface 在 pure OVN+OVS 下通常涉及以下对象：

| 层级 | 对象 | 作用 |
| --- | --- | --- |
| Kubernetes API | Pod、Deployment、NetworkAttachmentDefinition | 描述 Pod 需要接入哪些 attached networks。 |
| Kube-OVN CRD | Subnet、IP、VPC 相关对象 | 管理每条仿真 link 的网段、静态 IP 和地址分配状态。 |
| OVN Northbound DB | logical switch、logical switch port、logical router、static route、policy | 表达“这条 link 是一个二层交换机”“这个 Pod 接到这个交换机”“跨 link 如何路由”。 |
| OVN Southbound DB | Port_Binding、chassis binding | 把 logical port 绑定到具体 K3s 节点。 |
| 宿主机 OVS | `br-int`、OVS Interface、OpenFlow flows | 在真实节点上承载容器网卡，把报文送入 OVN/OVS 数据面。 |
| kubelet runtime | Pod sandbox、CNI ADD 状态 | kubelet 必须等 CNI 返回成功，Pod 才能从 `ContainerCreating` 进入 Running。 |

所以 pure OVN+OVS 的 CNI ADD 不是一次孤立的 `ip link add`。它需要同时满足：

1. Kube-OVN 能确认该 Pod 在目标 Subnet 中的 IP。
2. OVN NB/SB 数据库中对应 logical switch、logical port 和绑定状态存在。
3. 当前节点上的 OVS 能把容器端口写入 `br-int`。
4. `ovn-controller` 能把 OVN 逻辑状态翻译成当前节点上的 OVS 流表。
5. kubelet/CNI 能在 timeout 之前看到 interface ready。

任何一步变慢，Pod 都会停在 `ContainerCreating`，并且 kubelet 可能重试 sandbox
创建。

### 7.2 CNI ADD 的实际流程

对一个带 secondary interface 的 Pod，简化流程如下：

1. `kubectl apply` 创建 Deployment/Pod。
2. scheduler 把 Pod 分配到某个 K3s 节点。
3. 该节点 kubelet 开始创建 Pod sandbox。
4. Multus 读取 Pod annotation 和 NetworkAttachmentDefinition。
5. Multus 调用 Kube-OVN CNI delegate 给 secondary interface 做 ADD。
6. Kube-OVN CNI 创建容器侧接口和宿主机侧接口。
7. CNI 调用 `ovs-vsctl`，把宿主机侧接口加入 OVS `br-int`，并写入 `iface-id`
   等 `external_ids`。
8. Kube-OVN controller、`ovn-northd`、`ovn-controller` 协同处理 logical port、
   Port_Binding 和流表下发。
9. CNI 等待接口进入 ready 状态。
10. CNI 返回成功后，kubelet 才能继续启动容器。

这个流程中，`ovs-vsctl` 写 OVSDB、OVNDB mutation、Port_Binding/flow 下发和
interface ready 都在 Pod 创建关键路径上。它们不是后台慢慢完成的可选工作，而是
会直接决定 Pod 能不能从 `ContainerCreating` 进入 Running。

### 7.3 为什么规模上来后压力会被放大

B62 4954 规模下，pure OVN+OVS 会产生数千个 attached networks。之前 4954 pure
Kube-OVN 观测到的对象量级包括：

- 4763 个 Kube-OVN Subnet CR。
- 约 4765 个 OVN logical switch。
- 约 4774 个 logical switch port。
- 约 4777 条 logical router policy。
- 约 4764 条 static route。

这意味着控制面已经不是几十个网络对象，而是几千个网络对象。后续每个 Pod CNI
ADD 都是在这个大状态空间里继续创建和绑定端口。压力被放大的原因有四个：

1. **全局对象多。** `ovn-northd` 和 kube-ovn-controller 需要在大量 logical
   switch、port、route、policy 上做增量计算和同步。
2. **单节点 OVS 写入串行化明显。** 同一台节点上的多个 Pod 同时 CNI ADD 时，都会
   竞争本机 OVSDB、`br-int` 和 `ovs-vsctl` 路径。
3. **interface ready 有等待窗口。** 端口写进 OVS 不等于立刻可用，还要等 OVN
   binding 和本机 flow 下发完成。
4. **失败会自我放大。** 一旦某些 CNI ADD 超时，kubelet 会重试 sandbox 创建，
   新重试又会带来更多 `ovs-vsctl`、event 和 CNI 日志，进一步挤压控制面。

因此，pure OVN+OVS 下的“慢”不是单个脚本 sleep 太长，而是每个 Pod 都把工作压到
OVN/OVS 的同步路径上。当并发超过单节点 OVS 可承受能力时，系统会从“慢但推进”
变成“长时间 ContainerCreating、CNI ADD timeout、kubelet 重试、压力继续增加”。

之前 pure OVN+OVS 1078 profile 已经验证了这个边界：

| 配置 | 结果 | deploy 耗时 | 关键现象 |
| --- | --- | ---: | --- |
| w5, `nodeStreamMaxActivePerNode=1`, gateway skip, subnet batch 250 | PASS | 5894.99s | 1078 Pods ready，0 个 OVS/CNI 失败事件 |
| w11, `nodeStreamMaxActivePerNode=1`, gateway skip, subnet batch 250 | PASS | 2995.74s | 1078 Pods ready，0 个 OVS/CNI 失败事件 |
| w11, `nodeStreamMaxActivePerNode=2`, gateway skip, subnet batch 250 | 失败证据充分后中断 | 606.82s 时中断 | `add_nic_to_ovs_failed=233`，`failed_create_pod_sandbox=241`，CNI 日志中 `ovs-vsctl` timeout=322 |

结论是：pure OVN+OVS 的 CNI 压力主要由“每节点 active CNI ADD 数量”控制，而
不是单纯由全局 batch size 控制。增加 worker 有帮助，但前提是每个 worker 仍然
限制本地 OVS 写入并发。

在 4954 pure OVN+OVS 的失败记录中，也出现过更直接的数据面证据：

- kubelet event 中出现 `ovs interface ... is not ready after 30s`。
- CNI 日志中出现 `ovs-vsctl --timeout=30 ... signal: alarm clock`。
- CNI 报错 `add nic to ovs failed` 和 `context canceled by timeout`。
- OVS transient interface 存在但 `ofport` 为空，说明接口对象进入了 OVSDB，
  但没有及时完成可转发端口状态。

这些现象都指向同一个瓶颈：Pod 创建卡在本机 OVS add-port 和 OVN binding/ready
路径上，而不是卡在镜像拉取、BIRD 启动或 Kubernetes 调度本身。

## 8. OVN+OVS 与 macvlan+VLAN 的压力对比

| 维度 | pure OVN+OVS attached network | macvlan+VLAN |
| --- | --- | --- |
| 每条 link 的二层隔离 | 强。每条 link 可映射到独立 OVN logical switch。 | 强。每条 link 分配独立 VLAN，依靠 VLAN tag 隔离。 |
| CNI ADD 路径 | 重。Pod interface 创建需要写 OVS/OVN 状态并等待 ready。 | 轻。CNI 在已有 VLAN parent 上创建 macvlan interface。 |
| 主要瓶颈 | OVS add-port、`ovs-vsctl` timeout、interface ready timeout、Kube-OVN controller/OVNDB queue。 | 历史实验瓶颈是所有节点重复创建全部 VLAN parent；当前代码已改为按节点实际需要创建。 |
| 失败特征 | `ContainerCreating`、`add nic to ovs failed`、`ovs-vsctl` timeout、`interface not ready`。 | 本轮没有 workload CNI 失败；耗时主要发生在 Pod 创建前的 VLAN 准备。 |
| 扩展手段 | 限制每节点 active CNI ADD、增加 worker、调大 timeout、降低 OVS 并发、后续拆分 OVN 控制面。 | 减少重复 VLAN 准备、按节点实际需求准备 VLAN、超过 4094 VLAN 时增加 trunk。 |

所以这两类模式的压力性质不同。OVN+OVS 的 CNI ADD 压力来自每个 Pod secondary
interface 都要走 OVS/OVN 写路径；macvlan+VLAN 的主要成本转移到了确定性的
host/VLAN 预处理。

本文实验实际使用的是 macvlan+VLAN，而不是 VXLAN overlay。如果后续论文或代码中
采用“macvlan+VXLAN”这个表述，需要额外说明具体实现。低 CNI ADD 压力这一结论成立
的前提是：VLAN/VXLAN parent device 已经在宿主机侧准备好，Pod 创建时 CNI 只是在
这个 parent 上创建 macvlan 子接口，并把它放进容器 network namespace。这样 CNI
ADD 不需要为每个 Pod 写 OVN logical switch/port，不需要调用 `ovs-vsctl` 把端口
加入 `br-int`，也不需要等待 OVN Port_Binding 和 OVS flow ready。因此压力主要是
Linux 本地 netlink 操作，路径短、状态少、失败重试也不会反复放大 OVN/OVS 控制面
压力。

## 9. 为什么纯 macvlan 不够

纯 macvlan 如果没有 VLAN 隔离，不能等价于“每条 SeedEMU link 一个独立二层网络”。
这一点需要在论文里说清楚，因为它不是性能问题，而是仿真语义正确性问题。

### 9.1 SeedEMU link 对二层边界的要求

在 SeedEMU 的路由器级仿真中，一条 link 或一个 IX LAN 应当对应一个明确的二层
广播域。这个二层域内的 ARP 广播只应该被该 link 上的节点看到，不应该泄漏到其他
无关 link。也就是说：

- AS1850 和 AS1824 如果都连接在 `ix12` 上，那么它们可以在 `ix12` 上互相 ARP。
- 不在 `ix12` 上的其他容器接口，不应该收到、响应或影响这次 ARP。
- 如果另一个仿真 link 复用了同一个 IP 段结构或相近的地址模式，也不能进入这个
  ARP 广播域。

纯 macvlan 的问题在于：多个 NetworkAttachmentDefinition 可以都配置为
`type: macvlan`、`mode: bridge`，并且共用同一个 lower device，例如 `ens2`。
Kube-OVN 即使被用作 IPAM，也只负责给这些接口分配地址，不会自动为每条 attached
network 创建 OVN logical switch，也不会为每条 link 提供独立广播域。结果是，
多个本应互相隔离的 SeedEMU link 会共享同一条底层二层承载。

### 9.2 为什么 ARP 错误会导致 BGP 错误

BGP 建邻的第一步不是交换 BGP OPEN 报文，而是建立到 neighbor IP 的 TCP 连接。
如果 neighbor 在同一个直连 link 上，这条 TCP 连接依赖下面三件事：

1. Linux 路由表中有到 neighbor IP 的出接口路由。
2. ARP 能把 neighbor IP 解析成正确的 neighbor MAC。
3. 二层网络能把以该 MAC 为目的地址的帧送到正确容器接口。

如果第 1 点存在，但第 2 或第 3 点失败，BIRD 层看到的现象通常就是 BGP peer
停在 `Active`/`Connect`，并报 `Socket: No route to host`、超时或连接失败。
这类错误容易被误判为 BGP timer、OSPF 收敛或 BIRD 配置问题，但实际失败发生在
BIRD 之下的 ARP/二层转发层。

这里说的“ARP 识别错误”更准确地说是 ARP/邻居发现被错误的二层广播域污染，包括两类
结果：

- **错误解析。** 目标 IP 被解析到不属于该仿真 link 的 MAC 或接口，后续 TCP
  报文被送到错误位置。
- **解析失败。** ARP 请求在共享二层域中被丢弃、冲突或没有得到预期邻居响应，
  neighbor entry 无法进入可用状态。

无论是哪一类，BGP 都会表现为 TCP 建连失败，而不是表现为 BIRD 进程无法启动。

### 9.3 已有异常的证据链

之前一次 BGP 异常排查提供了比较完整的证据链。该异常中，
`as1850brd-r12-1.12.7.58-745bdc5949-69jxl` 的 BIRD protocol 输出中出现
`Ebgp_p_as1824 ... Active ... Socket: No route to host`，同时还有若干 iBGP
protocol 处于 `Passive`。

关键证据如下：

| 证据 | 观察结果 | 能排除什么 |
| --- | --- | --- |
| BIRD 进程状态 | `start-bird` 已成功，`birdc` 可用。 | 排除 BIRD 没启动、脚本没有执行、容器内没有 birdc。 |
| BGP 错误形态 | eBGP peer 报 `Active Socket: No route to host`。 | 问题发生在 BGP TCP 建连之前或建连阶段，不是 BGP OPEN/KEEPALIVE 参数协商失败。 |
| 内核路由 | AS1850 到 AS1824 有 `1.12.7.32 dev ix12 src 1.12.7.58`，AS1824 到 AS1850 有 `1.12.7.58 dev ix12 src 1.12.7.32`。 | 排除“完全没有到 neighbor IP 的 route”。 |
| 反向 TCP 测试 | AS1850 到 AS1824 的临时 TCP 测试失败，反向也失败。 | 说明不是单向 BIRD policy 问题，而是这对 Pod 在该 attached link 上数据面不可达。 |
| 对照邻居 | AS1850 可以连接另一个已 Established 的 eBGP 邻居 `1.12.5.2:179`。 | 排除 AS1850 的 BIRD 监听、整个容器网络栈或整个 `ix12` 相关进程全局损坏。 |
| NAD 类型 | `net-ix-ix12` 是 `type: macvlan`、`master: ens2`、`mode: bridge`；Kube-OVN 只作为 IPAM，Subnet 状态为 `SetNonOvnSubnetSuccess`。 | 说明该 attached network 不是 OVN logical switch 隔离的数据面，而是共享 lower device 的 macvlan 数据面。 |
| 接口状态 | 两个 Pod 的 `ix12` 都显示为 `macvlan mode bridge`，RX `errors/dropped` 达到几十万到百万级。 | 支持底层二层/接口数据面异常，而不是单纯协议 timer 过短。 |

另一次 pure OVN 调试中也观测到类似的“BIRD 已启动但 attached network ARP 不通”
现象：抽样 Pod 中 BGP peer 为 `Active Socket: No route to host`，OSPF 为
`Alone`，route count 接近直连路由；进一步检查发现
`1.2.4.245 -> 1.2.4.247`、`1.2.4.245 -> 1.2.0.1` 和同节点
`5.245.45.254 -> 5.245.45.253` 的 ARP 都无法解析。那一次最终定位到
OVN Port_Binding `activation-strategy=rarp` 导致 attached port `up=false`。
虽然根因不是纯 macvlan，但它进一步证明：当 ARP/二层邻居发现失败时，BIRD 层会
表现为 BGP/OSPF 不收敛，而不是表现为启动脚本失败。

### 9.4 逻辑推导

基于上面的证据，可以形成如下推导：

1. BGP peer 报 `No route to host`，说明 BIRD 试图建立 TCP 连接时，内核无法完成
   到 neighbor 的实际发送路径。
2. 双方内核路由已经存在，并且都指向预期的 `ix12` 接口，因此不能把问题简单归因
   为“没有路由”。
3. AS1850 还能连接其他已 Established 的 eBGP 邻居，因此也不能把问题归因于
   AS1850 的 BIRD 进程、监听端口或整个容器网络栈全局不可用。
4. 出问题的 attached network 使用纯 macvlan bridge，并共享 lower device `ens2`；
   Kube-OVN 在这里只做 IPAM，不提供 per-link logical switch 隔离。
5. 在这种结构下，SeedEMU link 的逻辑边界和实际 ARP 广播边界不一致；ARP 请求和
   响应可能被不属于该仿真 link 的接口影响。
6. 接口上大量 RX errors/dropped 与选择性邻接失败相吻合，说明问题更像二层转发或
   邻居发现异常，而不是 BGP/OSPF timer 设置异常。

因此，更严谨的结论是：纯 macvlan 不能保证 SeedEMU 每条 link 的二层隔离语义。
在大规模多 link 复用同一 lower device 时，ARP/邻居发现可能被共享广播域污染，
导致错误解析或解析失败；BGP 最终表现为 session 卡在 `Active`、`Connect` 或报
`No route to host`。当前已有证据足以排除 BIRD 启动失败、路由缺失和单个 AS 全局
网络栈故障，并支持“二层隔离不足导致 ARP/邻居发现异常”这一解释。

论文表述上建议避免说成“每一次都能观察到某个确定的错误 MAC”。更稳妥的说法是：
纯 macvlan 共享 lower device 破坏了仿真 link 的独立广播域假设，使 ARP/neighbor
discovery 暴露在错误的二层范围内；实验中表现为直连路由存在但 TCP/BGP 建邻失败，
并伴随 macvlan 接口大量丢包。

### 9.5 纯 macvlan 的风险总结

纯 macvlan 的风险包括：

1. 不相关的 SeedEMU link 可能暴露在同一个二层广播域中。
2. ARP 或 neighbor cache 可能被其他容器接口污染，或者无法解析到预期 neighbor。
3. 可能出现选择性、不对称的故障：某个 eBGP 邻居可达，另一个同 IX 直连邻居却
   不可达。
4. 故障会向上表现为 BGP/OSPF 不收敛，容易误导排查方向。
5. 修改 OSPF/BGP timer 不能修复底层 ARP 或 L2 隔离错误。

## 10. 为什么当前选择 macvlan+VLAN

macvlan+VLAN 是当前更适合 4954 规模全流程实验的折中方案：

- 避开 pure OVN+OVS 的 CNI ADD/OVS add-port 瓶颈。
- 通过 VLAN tag 保留每条仿真 link 的二层隔离。
- 避免纯 macvlan 共享 lower device 导致的 ARP 串扰风险。
- 已经在 9/12/18/24/32 总 VM 数下跑通 4954 规模全流程。

历史实验中的剩余问题主要是实现层面的 deploy 开销：脚本会在每个节点上准备全部
VLAN parent。当前代码已经改为按节点实际需要准备 VLAN parent。这个问题属于工程
实现优化，不是 pure macvlan 那种二层语义错误，也不是 pure OVN+OVS 那种
CNI ADD/OVS timeout 正确性风险。

## 11. 建议

1. 当前 4954 规模全流程实验以 macvlan+VLAN 作为主路径。
2. 不要把纯 macvlan 描述成严格 per-link 二层隔离方案。
3. pure OVN+OVS 仍是严格 logical switch 隔离的参考设计，但在用于 4954 规模
   BIRD/FIB 全流程前，需要继续解决 CNI/OVS 扩展性问题。
4. 后续重新跑 4954 多 VM 实验时，重点比较按节点 VLAN parent 准备后的 deploy
   耗时，确认 VM 数增加时不再因为重复准备 VLAN parent 而线性变慢。
5. 后续排查 BGP 异常时，应先检查 attached interface 的 L2 连通性、ARP/neighbor
   cache 和接口丢包，再考虑调整 OSPF/BGP timer。
