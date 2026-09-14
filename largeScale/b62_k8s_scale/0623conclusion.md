# 2026-06-23 三种网络方案对比结论

## 背景

本结论面向当前 SeedEMU/K8s 仿真器场景：一个互联网级拓扑会被编译成大量
Kubernetes workload，每个仿真二层网络需要保持广播域隔离，BIRD/OSPF/BGP
需要在容器内正常收敛，后续还要支持 start-kernel、FIB 验证和 reconvergence
测试。因此这里比较的不是通用 Kubernetes CNI，而是三种网络方案与仿真器语义、
大规模部署压力、可观测性和可控制性的匹配程度。

对比对象：

- `OVN+OVS`: 每个仿真网络映射为 Kube-OVN Subnet / OVN logical switch。
- `macvlan+VLAN`: 每个仿真网络映射为 VLAN ID，Pod macvlan 接口直接挂在
  `ensX.<vlan>` parent 上。
- `bridge+VLAN`: 每个仿真网络映射为 VLAN ID，同时在节点上创建 per-network
  Linux bridge，把 `ensX.<vlan>` 和 Pod veth 接到同一个 bridge。

## 总体结论

| 维度 | OVN+OVS | macvlan+VLAN | bridge+VLAN |
| --- | --- | --- | --- |
| 仿真二层隔离语义 | 最完整，由 OVN logical switch 保证 | 成立，由 VLAN 广播域保证 | 成立，由 VLAN + Linux bridge 保证 |
| 4954 规模可运行性 | 当前不适合直接跑完整 4954，容易卡在 Subnet/OVNDB 或 CNI ADD 长尾 | 已完整跑通 4954 | 已完整跑通 4954 |
| deploy 性能 | 1078 稳定配置约 3000s；4954 压力过大 | 4954 deploy 1358.92s | 4954 deploy 1504.93s |
| CNI 创建压力 | 高：OVNDB、logical switch、logical switch port、OVS add-port 都参与 | 低：创建 VLAN parent + macvlan 接口 | 中等：创建 VLAN parent + Linux bridge + veth enslave |
| 控制面压力 | 高，kube-ovn-controller/OVNDB/northd/OVS 都是路径的一部分 | 低，不创建 workload Subnet，不走 OVNDB | 低，不创建 workload Subnet，不走 OVNDB |
| host 侧可观测性 | 控制面对象丰富，但定位链路要跨 kubectl/ovn-nbctl/ovs-vsctl | 较弱，没有 per-network bridge port list | 最强，有 per-network Linux bridge 和端口成员列表 |
| host 侧可控制性 | 强但复杂，需操作 OVN/OVS 抽象 | 较弱，缺少集中端口对象 | 强且直观，可对 bridge/port/tc/qdisc/fdb 抓包和故障注入 |
| 论文证据展示 | 适合展示标准 SDN 控制面压力 | 适合展示高性能 VLAN-backed 方案 | 适合展示可观测、可解释、可控制的二层隔离结构 |

如果目标是“尽可能快地跑通 4954 规模实验”，当前数据支持优先选择
`macvlan+VLAN`。如果目标是“在论文和 debug 中清晰展示每个仿真二层网络的成员、
广播路径、抓包证据和故障注入位置”，`bridge+VLAN` 更合适。`OVN+OVS`
语义最完整，但在当前单 OVN 控制面/单集群路径下，大规模仿真成本过高。

## 性能结果

### VLAN-backed 4954 重建实验

本轮对 `bridge+VLAN` 和 `macvlan+VLAN` 都重新构造 VM 与 K3s cluster，
使用 32 台 VM（master + 31 worker，master 参与调度）、4954 规模、相同 batch
参数。运行目录：

- `runs/rebuild_compare_bridge_macvlan_vlan_20260623_092436_4954_w31/bridge_vlan_4954_w31`
- `runs/rebuild_compare_bridge_macvlan_vlan_20260623_092436_4954_w31/macvlan_vlan_4954_w31`

| 阶段 | bridge+VLAN | macvlan+VLAN | 结论 |
| --- | ---: | ---: | --- |
| 全流程总耗时 | 4309.22s | 4154.21s | macvlan 快 155.01s |
| 排除 destroy/network/build-cluster 后 | 2545.66s | 2417.23s | macvlan 快 128.43s |
| compile | 231.02s | 228.03s | 基本一致 |
| build | 413.87s | 388.38s | macvlan 略快 |
| deploy | 1504.93s | 1358.92s | macvlan 快 146.01s |
| wait-ready | 101.91s | 166.30s | bridge 更快 |
| start-bird | 44.77s | 36.17s | macvlan 略快 |
| verify start-bird | 113.41s | 111.45s | 基本一致 |
| start-kernel | 84.05s | 76.07s | macvlan 略快 |
| verify FIB | 9.58s | 9.55s | 一致 |
| reconvergence | 16.08s | 15.96s | 一致 |

deploy pressure：

| 指标 | bridge+VLAN | macvlan+VLAN |
| --- | ---: | ---: |
| max pending | 4 | 2 |
| max creating | 34 | 25 |
| max failed | 0 | 0 |
| max creating per node | 2 | 2 |
| deploy max load1 | 192.13 | 134.63 |
| deploy avg load1 | 91.02 | 77.76 |

这说明 VLAN-backed 两种方案都没有复现 OVN+OVS 的 CNI ADD 长尾。
macvlan+VLAN 的路径更短，少了 Linux bridge 对象和 veth enslave 操作，
因此本轮性能略优。

### OVN+OVS 观测结论

OVN+OVS 的稳定优化实验主要在 1078 规模完成。典型结果：

| 配置 | 规模 | 结果 | deploy 耗时 | 关键现象 |
| --- | ---: | --- | ---: | --- |
| w5, 每节点 active CNI ADD=1 | 1078 | PASS | 5894.99s | 稳定但非常慢 |
| w11, 每节点 active CNI ADD=1 | 1078 | PASS | 2995.74s | worker 增加后明显改善 |
| w11, 每节点 active CNI ADD=2 | 1078 | 中断 | 606.82s 时已证据充分 | `add_nic_to_ovs_failed=233`, `failed_create_pod_sandbox=241`, `ovs-vsctl` timeout=322 |
| w24/w32/w48, 每节点 active CNI ADD=1 | 1078 | PASS | 约 3000s | 继续加 worker 收益不明显，热点节点串行长尾主导 |

OVN+OVS 的压力来自两条路径：

1. Subnet/控制面路径：每个仿真网络会变成 Kube-OVN Subnet、logical switch、
   logical router static route、policy、logical switch port 等对象。大量网络会让
   kube-ovn-controller、OVNDB 和 northd 形成串行 mutation 成本。
2. Pod CNI ADD 路径：每个 Pod 接入 logical switch 时要在对应节点写 OVS，
   包括 local OVS add-port、interface readiness、logical switch port binding 等。
   实验表明单节点 active CNI ADD 提高到 2 就可能触发 `ovs-vsctl` timeout 和
   Pod sandbox 创建失败。

因此，OVN+OVS 在小规模或中等规模下语义很好，但在 4954 这种仿真规模中，
控制面和每节点 OVS 写入压力会成为主要瓶颈。它的优化方向是增加 worker、
限制每节点 CNI 并发、拆分 OVN 控制面/OVNDB/VPC，但这些优化复杂度明显高于
VLAN-backed 方案。

## 三种方案与仿真语义的关系

### OVN+OVS

OVN+OVS 与“每个仿真二层网络独立隔离”的语义最自然匹配。每个网络都可以对应一个
logical switch，Pod 通过对应 NAD 接入该 logical switch。理论上，ARP/广播域隔离、
三层路由、OVN 控制面对象都很清楚。

问题在于仿真器不是普通业务集群。SeedEMU 会生成成千上万个网络和路由器容器，
这意味着 OVN 需要维护成千上万个 logical switch、logical switch port、static route
和 policy。对 4954 规模，网络对象数量和 CNI ADD 次数都很大，单 OVNDB 和每节点
OVS add-port 串行路径很容易成为瓶颈。

适用场景：

- 小规模或中等规模；
- 需要严格贴近 SDN/OVN 控制面语义；
- 需要研究 OVN 控制面本身的瓶颈。

不适合场景：

- 需要频繁跑 4954 规模全流程；
- 需要快速部署、快速清理、快速迭代实验；
- 不希望调试 kube-ovn-controller、OVNDB、northd、ovs-vsctl 的长尾问题。

### macvlan+VLAN

macvlan+VLAN 把隔离边界从 OVN logical switch 下沉到 Linux VLAN。每个仿真二层网络
对应一个 VLAN ID，Pod 的 macvlan 接口直接挂到 `ensX.<vlan>`。同一个 VLAN 内的
Pod 处于同一广播域，不同 VLAN 的 ARP 广播互相不可见。

本轮 4954 实验证明该方案可以跑通完整流程，并且性能最好：

- 4954 deploy `1358.92s`;
- deploy max creating `25`;
- deploy max failed `0`;
- start-bird、start-kernel、FIB verify、reconvergence 全部 PASS。

它的主要优势是路径短、控制面轻、不经过 OVNDB，不需要为每个仿真网络创建
Kube-OVN Subnet/logical switch。缺点是 host 侧缺少 per-network 的集中对象：
macvlan 接口在 Pod netns 内，host root namespace 里没有一个类似 bridge port list
的结构可以直接列出“这个仿真网络在该节点上接了哪些 Pod 接口”。

适用场景：

- 追求 4954 规模实验吞吐；
- 希望保留二层隔离但避免 OVN+OVS 长尾；
- 主要关心实验是否能稳定、快速跑完。

不适合场景：

- 需要在 host 侧直接列出某个仿真二层网络的所有端口；
- 需要对某个网络端口做精细故障注入、tc/qdisc、FDB 观测；
- 论文需要非常直观地展示“一个网络内有哪些接口接入”。

### bridge+VLAN

bridge+VLAN 也用 VLAN 保证二层隔离，但在每个节点上为每个仿真网络创建一个
Linux bridge。`ensX.<vlan>` 作为 VLAN parent 接入该 bridge，Pod 通过 veth 接入
同一个 bridge。

本轮 4954 实验证明该方案也能完整跑通：

- 4954 deploy `1504.93s`;
- deploy max creating `34`;
- deploy max failed `0`;
- start-bird、start-kernel、FIB verify、reconvergence 全部 PASS。

它比 macvlan+VLAN 略慢，原因是每个网络多了 Linux bridge 对象和 veth enslave
操作。但它带来了很强的可观测性和可控制性。

## bridge+VLAN 的可观测性优势

bridge+VLAN 的核心优势是 host 侧有 per-network Linux bridge。对任意一个仿真二层网络，
我们可以直接在节点上看到：

- 对应的 VLAN parent，例如 `ens3.419`;
- 对应的 Linux bridge，例如 `br-c71549a70f47`;
- 接入该网络的 Pod veth，例如多个 `veth...`;
- bridge 端口成员列表，例如 `/sys/class/net/br-c71549a70f47/brif`;
- bridge 状态、MAC 地址、FDB、qdisc、tc、抓包点。

这使得它比 macvlan+VLAN 更适合做实验调试和论文证据展示。

具体优势：

1. 可以直接列出网络成员  
   对某个仿真网络，可以通过 `brif` 看到 VLAN parent 和所有 Pod veth。也就是说，
   我们能明确回答“这个节点上，哪些接口属于这个仿真二层网络”。

2. 抓包点清晰  
   可以在 `br-...` 上抓整个二层网络，也可以在 `ensX.<vlan>` 上抓 underlay VLAN，
   还可以在具体 `veth...` 上抓单个 Pod 接口。这个层级结构适合定位 ARP、OSPF、
   BGP 邻居、广播泄漏和路由收敛问题。

3. 故障注入对象明确  
   可以对 bridge、VLAN parent 或单个 veth 做 `tc netem`、丢包、延迟、限速、
   down/up 等操作。相比 macvlan，bridge 模式下端口对象都在 host 侧可见，
   操作和恢复更直观。

4. FDB 和二层行为可解释  
   Linux bridge 有 FDB，可以观察 MAC 学习、泛洪、端口状态。对论文来说，这比
   “Pod 里有一个 macvlan 接口挂在 parent 上”更容易展示二层网络结构。

5. 与传统网络模型更贴近  
   在解释仿真器时，可以把每个 `br-...` 看作一个节点本地的虚拟二层交换机，
   把 `ensX.<vlan>` 看作跨 VM 的 trunk 接口，把 Pod veth 看作接入端口。
   这个模型对网络读者更直观。

因此，bridge+VLAN 的定位不是“最快”，而是“可观察、可控制、可解释”。
如果论文需要展示二层隔离证据、ARP 不泄漏证据、端口成员关系、故障注入机制，
bridge+VLAN 比 macvlan+VLAN 更有优势。

## 二层隔离结论

三种方案都可以实现二层隔离，但实现机制不同：

- OVN+OVS: logical switch 隔离广播域。
- macvlan+VLAN: VLAN ID 隔离广播域。
- bridge+VLAN: VLAN ID 负责跨节点隔离，Linux bridge 负责节点内同 VLAN 端口转发。

本轮 VLAN-backed 观测结果：

| 检查项 | bridge+VLAN | macvlan+VLAN |
| --- | --- | --- |
| 同 VLAN ping | PASS | PASS |
| 同 VLAN listener Pod 看到 ARP | PASS | PASS |
| 不同 VLAN listener Pod 看不到 ARP | PASS | PASS |
| host 上 VLAN parent 可见 | PASS | PASS |
| host 上 per-network bridge port list | PASS | 不适用 |

需要强调：跨网络 ping 可能因为三层路由而成功，这不表示二层广播泄漏。判断二层隔离
要看 ARP/广播是否出现在不相关网络中。本轮不同 VLAN listener Pod 的 ARP 计数为 0，
说明 VLAN 隔离成立。

## 面向仿真器语义的模式选择

源码里的 SeedEMU 网络语义不是普通 Kubernetes service 网络，而是“仿真二层广播域”：

- `NetworkType.Local` 是 AS 内部网络，OSPF 默认会在这些接口上形成邻接；
- `NetworkType.InternetExchange` 是 IX peering LAN，eBGP/route server 依赖它；
- `NetworkType.CrossConnect` 是两个节点之间的专用二层连接；
- 每个节点通过 `joinNetwork()` 加入这些网络，BIRD/OSPF/BGP 看到的是容器内的真实
  Linux 接口、地址、ARP 和路由表。

因此选择 CNI 时，第一原则不是“哪种 Kubernetes 网络最先进”，而是它是否能正确承载
SeedEMU 的二层广播域、邻接关系、ARP 行为和故障模型。三种模式都可以保留二层隔离，
但适合的实验问题不同。

| 实验目标 | 推荐模式 | 原因 |
| --- | --- | --- |
| 大规模 BGP/OSPF 收敛、FIB 写入、reconvergence 主实验 | `macvlan+VLAN` | 最少控制面对象，CNI ADD 路径最短，已稳定跑通 4954 和 9955 规模 |
| 需要证明二层隔离、抓 ARP/OSPF/BGP 包、展示每个网络成员 | `bridge+VLAN` | host 侧有 per-network Linux bridge、VLAN parent、Pod veth、FDB 和端口列表 |
| 需要对单个仿真链路、单个端口或整个二层网络做 `tc`/丢包/延迟故障注入 | 优先 `bridge+VLAN` | 可以在 host 侧明确选择 bridge、veth、VLAN parent 作为故障注入点 |
| 只需要对单个 Pod 接口做端点级限速、延迟或丢包 | 三者都可用 | Pod 是 privileged，容器内接口可以直接使用 `tc` |
| 研究 Kubernetes/OVN/OVS 作为虚拟网络 fabric 的性能、隔离、控制面瓶颈 | `OVN+OVS` | 每个 SeedEMU 网络映射为 Kube-OVN Subnet/logical switch，可观察 OVNDB、northd、OVS 写路径 |
| 物理机之间没有统一二层 trunk、无法控制上游 VLAN，但仍需要跨节点二层虚拟网 | `OVN+OVS` | Kube-OVN/OVS 可通过 overlay/tunnel 承载 secondary L2 network，不依赖 underlay VLAN |
| 需要最高可解释性、可复现实验现象和论文图示 | `bridge+VLAN` | Linux bridge 模型接近传统交换机，证据链比 macvlan 更直观 |
| 频繁重跑大规模实验、主要关心实验吞吐 | `macvlan+VLAN` | deploy、start-bird、start-kernel 的系统压力更低 |

### `tc` 与故障注入位置

三种模式下，Pod 内都可以使用 `tc`，因为当前编译器给 workload 容器设置了
`privileged: true` 和 `capabilities: ALL`。但 `tc` 的语义取决于操作点：

- Pod 内接口上的 `tc`：三种模式都可用，适合模拟某个 router/brdnode 自己的接口
  出方向延迟、丢包、限速。这个模型最接近 SeedEMU 中“节点接口属性”的语义。
- `bridge+VLAN` host 侧 veth 上的 `tc`：适合模拟某个 Pod 接入端口的故障。host 上
  能看到 veth 与 bridge 成员关系，便于恢复和记录证据。
- `bridge+VLAN` host 侧 Linux bridge 上的 `tc`/抓包/FDB 观测：适合模拟或观察某个
  仿真二层网络整体的行为，例如广播、ARP 泄漏检查、OSPF hello 是否只在本网络出现。
- `macvlan+VLAN` host 侧：适合在 VLAN parent 上做粗粒度操作，但不适合枚举和控制
  每个 Pod 的接入端口，因为 macvlan 子接口主要在 Pod netns 内，host root namespace
  没有 per-network bridge port list。
- `OVN+OVS` host 侧：可以操作 OVS port、OVN logical port、QoS/ACL 等对象，但定位链路
  需要跨 `kubectl`、`ovn-nbctl`、`ovs-vsctl` 和 Pod netns。它适合研究 SDN fabric
  本身，不适合作为最简单的链路故障注入路径。

如果未来要把 SeedEMU 的 `Network.setDefaultLinkProperties()` 或更细粒度 link model
系统性映射到 K8s 运行时，推荐先实现两层策略：默认在 Pod 内接口加 `tc` 保证跨模式
一致；需要 host 侧证据或网络级故障时，在 `bridge+VLAN` 下额外支持 bridge/veth 级
注入。

### 关于 SDN 实验的边界

现在的 `OVN+OVS` 可以直接支持一类 SDN 相关实验，但它的含义需要说清楚：

1. 可以做的实验：把 Kube-OVN/OVS 当作承载 SeedEMU secondary networks 的 SDN fabric。
   这类实验关注 logical switch、logical router、OVNDB、northd、kube-ovn-controller、
   OVS add-port、ACL/QoS/policy、overlay 隧道、跨节点二层虚拟化和控制面扩展性。
   例如：
   - 大量 logical switch/Subnet 对 OVNDB 和 northd 的压力；
   - 每节点 OVS 写入并发对 CNI ADD 延迟的影响；
   - Kube-OVN ACL/QoS/policy 对仿真流量的影响；
   - 在没有 underlay VLAN 的多物理机环境中，用 OVN overlay 提供隔离二层网络；
   - SDN fabric 故障或控制面抖动对 BGP/OSPF 收敛的间接影响。

2. 不能直接代表的实验：SeedEMU 网络内部的“AS 路由器被 SDN 控制器直接控制”。
   当前 SeedEMU 节点仍然是 Linux router/container，控制平面主要是 BIRD/OSPF/BGP；
   OVN+OVS 只是 Kubernetes secondary network 的承载层。也就是说，使用 OVN+OVS
   不会自动把 AS 内 router 变成 OpenFlow switch，也不会自动把 BGP/OSPF 逻辑替换为
   一个仿真 SDN controller。

3. 如果未来要在 Seed 网络中增加真正的 SDN 语义，需要新增编译和运行时映射：
   - 明确哪些 SeedEMU 节点是 SDN switch、哪些是 controller；
   - 为 switch 创建可编程 datapath，例如 OVS bridge 或 OVN logical switch；
   - 建立 controller 到 switch 的控制通道，例如 OpenFlow/OVSDB 或 OVN northbound；
   - 定义流表生命周期、链路故障、控制器失联、重连和恢复语义；
   - 决定 BGP/OSPF 与 SDN 控制平面的关系，是替代、叠加，还是只控制部分二层 fabric。

基于这个边界，`OVN+OVS` 适合作为“SDN fabric baseline”和“控制面压力实验平台”。
如果论文问题是“SDN 控制面如何承载大规模仿真二层网络”，OVN+OVS 是合适对象；
如果论文问题是“互联网 AS 内部引入 SDN 控制器后，BGP/OSPF/流表如何交互”，则还需要
扩展 SeedEMU 的网络元素和编译器，不能只依赖当前 Kube-OVN CNI。

### 更具体的使用建议

1. 默认大规模路由实验使用 `macvlan+VLAN`。  
   它最符合当前主线目标：在保留二层隔离的前提下，让 4954/9955 规模尽可能稳定、
   快速地完成 deploy、start-bird、start-kernel、FIB verify 和 reconvergence。

2. 论文证据、调试和链路故障注入使用 `bridge+VLAN`。  
   如果要展示“某个仿真网络有哪些端口”“ARP 广播有没有泄漏”“某条链路被加了
   100ms delay 后收敛如何变化”，bridge+VLAN 更合适。它比 macvlan 慢一些，但 host
   侧证据链完整：VLAN parent、Linux bridge、veth、FDB、抓包点都能列出来。

3. SDN fabric、跨物理机无 VLAN trunk、OVN 控制面扩展性实验使用 `OVN+OVS`。  
   它不适合作为当前 4954 规模频繁重跑的默认路径，但适合单独作为研究对象。
   使用时必须把规模和问题定义清楚：如果研究的是 OVN/OVS 自身，应记录 logical
   switch 数量、logical port 数量、OVNDB mutation、northd CPU、kube-ovn-controller
   queue、OVS add-port latency 和 CNI ADD 长尾；如果研究的是 BGP/OSPF 本身，则
   OVN+OVS 的额外控制面压力会成为干扰变量。

4. 避免退回纯 macvlan 无 VLAN 的大规模主实验。  
   纯 macvlan 虽然快，但不能可靠表达每个 SeedEMU 网络的独立二层广播域，历史实验中
   已出现过 ARP 识别错误进而影响 BGP session 的现象。当前 VLAN-backed 方案已经证明
   可以在保留隔离的同时获得接近 macvlan 的性能，因此不需要牺牲二层语义。

最终建议：论文中可以把 `OVN+OVS` 作为 SDN fabric 语义完整但控制面成本高的 baseline，
把 `macvlan+VLAN` 作为高吞吐大规模路由实验方案，把 `bridge+VLAN` 作为可观测、
可控制、可解释的实验平台。三者对比可以说明：我们不是简单牺牲隔离换性能，而是在
保留 SeedEMU 二层网络语义的前提下，根据实验问题选择不同承载层。
