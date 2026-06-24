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

## 推荐选择

当前对仿真器最务实的选择如下：

1. 默认大规模性能实验：`macvlan+VLAN`  
   它已经跑通 4954，deploy 更快、CNI 压力更低，适合作为大规模实验的高吞吐路径。

2. 需要调试、论文证据、故障注入：`bridge+VLAN`  
   它也跑通 4954，性能略慢但仍可接受。host 侧有 per-network Linux bridge，
   可以直接列出 VLAN parent 和 Pod veth 端口成员，更适合抓包、定位、故障注入和
   论文中的结构展示。

3. 研究 OVN 控制面或追求 SDN 语义：`OVN+OVS`  
   它的隔离语义最标准，但当前不适合作为 4954 规模频繁实验的主路径。若继续使用，
   必须限制每节点 CNI 并发，增加 worker，优化 placement，并考虑拆分 OVNDB/控制面。

最终建议：论文中可以把 `OVN+OVS` 作为语义完整但控制面成本高的 baseline，
把 `macvlan+VLAN` 作为高性能方案，把 `bridge+VLAN` 作为可观测、可控制、可解释的
方案。三者对比可以说明：我们不是简单牺牲隔离换性能，而是在保留二层隔离的前提下，
用 VLAN-backed 设计绕开了 OVN+OVS 在大规模仿真下的控制面和 CNI ADD 长尾。
