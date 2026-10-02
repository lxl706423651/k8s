# SeedEMU Kubernetes 大规模优化与 Placement 设计

本文基于 `../seed-emulator-k8s-new/docs/designs/k8sdesign.md` 的原生
Kubernetes 设计，总结 `largeScale/b62_k8s_scale` 为大规模运行所做的工程优化、
`largeScale/b63_deployment` 对 placement 所做的算法与评估优化，以及当前全流程各
阶段的核心契约、实现方式、产物、通过条件和实测耗时。

文档中的“当前流程”以现有脚本和实验产物为准，而不是只按早期设计文档推断。主要
证据来源包括：

- `seedemu/k8sTools/README.md`
- `seedemu/k8sTools/resources/setup/README.md`
- `largeScale/b62_k8s_scale/largeScaleK8sDesign.md`
- `largeScale/b62_k8s_scale/runFullExperiment.py`
- `largeScale/b63_deployment/0624design.md`
- `largeScale/b63_deployment/0629experimentResult.md`
- `largeScale/b63_deployment/runB63LargeExperiment.py`
- `largeScale/b63_deployment/runPlacementExperiment.py`
- b62/b63 已通过实验的 `summary.json` 和 `placement_report.json`

## 1. 设计基线

原生 SeedEMU Kubernetes 设计把系统拆成三个相互独立的契约：

1. **Compile contract**：把 render 完成的 SeedEMU emulation 转换成 Kubernetes
   manifest、镜像构建上下文和镜像元数据。它只解释仿真拓扑，不依赖某个固定集群。
2. **Build-cluster contract**：根据 YAML 创建或准备 KVM/物理机、K3s、registry、
   Multus 和 secondary-network fabric，输出 `configK3s.yaml`、`kubeconfig.yaml`
   和 inventory。
3. **Running contract**：消费 compile 产物和 cluster 配置，完成镜像构建、分发、
   workload 部署和 Ready 等待。

K3s primary CNI 和 SeedEMU 网络彼此分离：

- Pod 的 `eth0` 是 K3s 管理网络，用于 Kubernetes 控制、镜像和普通 Pod 通信。
- SeedEMU 的 AS 内网、IX LAN 和 cross-connect 通过 Multus secondary interface
  加入 Pod。
- Router Pod 仍运行真实 BIRD，并维护自己的接口、ARP、RIB 和 Linux FIB。

因此，大规模优化的目标不是改变 SeedEMU 网络语义，而是降低 Kubernetes 承载这些
语义时的集群、CNI、镜像、API Server 和节点本地压力。

## 2. 从原生流程到 b62、b63

| 版本 | 核心能力 | Placement | 网络后端 | 验证与评估 |
| --- | --- | --- | --- | --- |
| 原生设计/B61 | `compile -> build cluster -> up` 三层解耦 | 主要依赖普通 Kubernetes 调度或简单固定映射 | 默认 OVN+OVS，支持 macvlan 方向 | 以 Pod Ready 为主 |
| b62 | 把一次 `up` 拆成可恢复、可计时、可限流的大规模阶段 | compile 内执行确定性 by-AS hard placement | 大规模默认 macvlan+VLAN，保留 bridge+VLAN 和 OVN+OVS | 增加 BIRD、FIB、reconvergence 和阶段日志 |
| b63 | 在 b62 阶段框架上把 placement 独立出来 | 多目标、fabric-aware、容量感知的 greedy placement，并比较多个 baseline | 默认 macvlan+VLAN，多 trunk 扩展 VLAN 空间 | `placement_report.json` 同时记录静态、部署动态、routing 和 ping/iperf |

b62 解决的是“大规模流程能稳定运行”的问题；b63 进一步解决的是“同一拓扑应如何
放到多个 K3s node，才能减少跨节点网络扩散和节点热点”的问题。

## 3. 当前整体流程

### 3.1 b62 实际自动流程

```text
assignment.yaml
  -> render-assignment
  -> destroy-existing-cluster
  -> prepare-libvirt-dhcp
  -> build-cluster
  -> ensure-all-nodes-schedulable
  -> clean
  -> preflight
  -> compile + by-AS hard placement
  -> build
  -> deploy
  -> wait-ready
  -> start-bird
  -> verify-after-start-bird
  -> start-kernel
  -> verify-after-fib-write
  -> reconvergence
```

`runFullExperiment.py` 默认执行 reconvergence，但成功后不自动销毁集群，方便继续
检查现场。

### 3.2 b63 当前实际自动流程

```text
configkvm_b63.yaml / vm_matrix_configs/*.yaml
  -> optional destroy-existing-cluster
  -> build-cluster
  -> ensure-master-schedulable
  -> clean
  -> compile unplaced manifest
  -> placement
  -> preflight
  -> build
  -> deploy + dynamic sampler
  -> wait-ready
  -> start-bird
  -> verify-after-start-bird
  -> start-kernel
  -> verify-after-fib-write
  -> dataplane ping/iperf
```

b63 的 `running/runReconvergence.sh` 和 `running/measureReconvergence.py` 已从 b62
保留，但 `runB63LargeExperiment.py` 当前没有调用它。因此：

- b62 的 `reconvergence(s)` 是默认全流程实测阶段。
- b63 当前 `summary.json` 中没有 `reconvergence`，不能把它当作已自动执行。
- b63 当前新增并默认执行的是 post-FIB `dataplane` 阶段。

如果后续要形成包含两类动态指标的统一流程，建议先执行无故障
`dataplane ping/iperf`，再执行会改变 BIRD 状态的 `reconvergence`，最后增加一次
恢复后的 BIRD/FIB gate，避免故障实验污染正常数据面测量。

## 4. b62 的大规模工程优化

### 4.1 固定总资源下的多 VM K3s

b62 用 `assignment.yaml` 统一描述规模、VM 数、总资源和网络参数，并由
`renderAssignmentConfig.py` 生成显式 KVM 节点列表及 resource plan。

当前实验约束为：

- 总计 `300 vCPU`、`600 GiB` memory。
- 每个 VM 使用 `80 GB` disk。
- master 预留较多控制面资源，剩余 CPU/memory 在 worker 间确定性分配。
- master 默认解除 control-plane taint，也参与 workload 调度。

集群针对高密度 Pod 做了以下调优：

- 扩大 K3s `cluster-cidr`，并校验 node PodCIDR 是否能容纳目标 VM 数和
  `maxPods=4000`。
- 使用 `host-gw` primary flannel，避免不必要的 primary overlay 成本。
- 提高 `user.max_net_namespaces`、文件描述符、进程数、inotify、PID、邻居表、
  conntrack、socket buffer 和 `netdev_max_backlog` 等上限。
- 为 K3s、containerd、Docker 设置 systemd limit，并增大 `cni0` bridge hash。
- 使用持久 Ubuntu cloud image、SeedEMU image 和 Helm cache，避免每轮重新下载。
- 输出稳定的 `configK3s.yaml`、`kubeconfig.yaml` 和
  `cluster.inventory.yaml`，供后续 SSH、preload、node-local 操作和销毁使用。

### 4.2 从 OVN+OVS 转向 VLAN-backed fabric

OVN+OVS 把每个 SeedEMU L2 network 映射成 logical switch/Subnet，并为每个
secondary interface 创建 logical port 和 OVS port。该模型语义完整，但大规模时会
同时压到 Kube-OVN controller、OVN NB/SB、northd、ovn-controller、
ovs-vswitchd 和 CNI ADD。

b62 的默认主路径改为 `macvlan+VLAN`：

- 每个 SeedEMU L2 network 分配一个 VLAN。
- Pod 的 secondary macvlan interface 挂到 `ensX.<vlan>`。
- VLAN 保留独立广播域，避免纯 macvlan 共用 parent 时的二层泄漏。
- 不为每个网络创建 OVN logical switch，显著降低控制面和 OVS 写入压力。

b62 同时保留：

- `bridge+VLAN`：多一层 host Linux bridge，性能略低但便于抓包、FDB 观测、
  `tc` 和故障注入。
- `OVN+OVS`：作为 SDN fabric 与控制面扩展性 baseline，而不是当前最大规模的默认
  运行方式。

单个 802.1Q trunk 只有 `1..4094` 的 VLAN 空间，因此大拓扑使用多个 trunk。b62
配置示例使用 `ens2`、`ens3` 两个 trunk；b63 的 48VM 最大规模配置扩展到
`ens2..ens5` 四个 trunk。可承载网络数仍受 `trunk 数量 x VLAN 空间` 约束。

### 4.3 by-AS hard placement

b62 在 compile 阶段读取实际 Ready、可调度 node，统计每个 ASN 对应的 Pod 数，按
“较大 AS 优先”顺序把整个 AS 放入仍有足够 Pod slot 的 node。

结果作为：

```yaml
nodeSelector:
  kubernetes.io/hostname: <selected-node>
```

写入 manifest。该方法的收益是：

- 同一 AS 默认不被随意拆散。
- build 阶段可以提前知道每个 node 需要哪些镜像。
- deploy 阶段可以提前知道每个 node 需要哪些 VLAN parent。
- 每轮映射确定且可重放。

它的限制是只以 ASN 和 Pod slot 为主要单位，没有显式联合优化 IX/peer/intra-AS
网络权重、异构 CPU/memory、VLAN parent 峰值和跨节点 fan-out。这正是 b63
placement 要补齐的部分。

### 4.4 快速镜像构建与按 node 分发

b62 把原生 `k8sTools.py up` 中的 build/deploy 拆开：

- compile 产物上传到 master/registry node 后在其本地构建。
- 使用本地 registry 和 Docker/buildx cache。
- 利用 SeedEMU 镜像高度相似的特点生成 fast image archive，复用公共层。
- 根据 hard placement 生成 `node_image_refs/`。
- 每个 K3s node 只导入自己将运行的 workload image archive。
- deploy 使用 `IfNotPresent`，避免大规模启动时集中拉取。

这样把“镜像是否准备好”和“Pod/CNI 是否创建成功”拆成两个独立问题，也避免 registry
pull 成为 placement 实验的主要干扰变量。

### 4.5 Placement-aware VLAN 准备和批量 deploy

在 macvlan+VLAN/bridge+VLAN 下，deploy 在创建 Pod 前解析最终 manifest：

1. 读取 workload 的 Multus networks。
2. 读取固定 `kubernetes.io/hostname`。
3. 计算每个 node 实际需要的 VLAN parent。
4. 仅在对应 node 创建 `ensX.<vlan>`；bridge 模式再创建 per-network bridge。
5. 按 warm-up batch 和普通 batch 逐步创建 controller。
6. 每个 batch 前后检查 Pending、Creating、NotReady、Failed 和单 node Creating。

这比“所有 node 创建所有 VLAN”显著减少 host 网络对象数量，也使 placement 降低
VLAN fan-out 的静态收益直接转化为 deploy 阶段的实际收益。

### 4.6 Node-local BIRD/FIB 操作

数千次 `kubectl exec` 会把压力集中到 API Server 和 kubelet。b62 因此把
start-bird 和 start-kernel 改为：

- 按 Pod 所在 node 分组。
- 所有 node 并发。
- 每个 node 内默认串行处理。
- SSH 到 VM 后用 `crictl` 查找 container，再用 `nsenter` 进入其 namespace。
- 每个目标之间加入短延迟，并在节点完成后执行 load gate。
- 操作设计为幂等：BIRD 已运行时记录 skipped，而不是重复启动。

start-kernel 把 BIRD kernel export 独立到协议收敛之后，避免 Pod 启动、路由协议建立和
海量 FIB 写入同时冲击节点。

### 4.7 防同步扫描和确定性验证

kernel export 使用：

- `export all`
- `scan time = 6000s` 基准
- `120s` jitter

长 scan interval 和 jitter 避免数千个 BIRD kernel protocol 同时周期扫描。

验证不全量 `kubectl exec` 所有 Pod，而是：

- 先等待所有 VM 的 `load1` 低于阈值。
- 从 ASN 排序最前的 10 个不同 AS 中各选一个 brd Pod。
- 每个 AS 固定选择 router ID 最小的 brd Pod。
- BIRD gate 检查 `birdc show protocols` 和 `birdc show route count`。
- FIB gate 检查 `ip route | wc -l > BIRD network count`。

这是一种低成本、可重复的健康抽样，不等价于全拓扑逐路由正确性证明。

### 4.8 阶段化、可恢复和可审计

b62 每轮生成：

```text
runs/<timestamp>_<scale>_w<workers>/
  summary.json
  <stage>.log
  output/
  node_image_refs/
  start_bird_summary.json
  verify_after_start_bird_summary.json
  start_bird_kernel_summary.json
  verify_after_fib_write_summary.json
  reconvergence_summary.json
  loadAverage.log
```

每个阶段都有开始时间、结束时间、秒级耗时、退出码和独立日志。失败后可以从某个阶段
手动恢复，而不必每次把 cluster、compile、build 和 deploy 全部重做。

## 5. b63 Placement 优化

### 5.1 独立 cluster 和独立 placement 阶段

b63 不复用 b62 cluster。每轮 cluster 必须由 b63 自己的
`configkvm_b63.yaml` 或 `vm_matrix_configs/*.yaml` 构造，持久输出为：

```text
configK3s-b63.yaml
kubeconfig-b63.yaml
inventory-b63.yaml
```

b63 compile 先生成不带 hard placement 的：

```text
output/k8s.raw.yaml
output/k8s.source.yaml
```

随后 `running/placement.sh` 单独运行算法，输出：

```text
output/k8s.yaml
output/network_weights.yaml
output/placement_report.json
```

这种拆分允许同一 compile 产物运行 optimized、network-only、resource-only、
pod-count-balanced、hypergraph 和 Kubernetes default scheduler 等方法。

### 5.2 网络权重与资源估计

一个 SeedEMU network 被视为连接多个 workload endpoint 的 hyperedge。默认权重为：

| Network 类型 | 权重 |
| --- | ---: |
| IX | 10.0 |
| 跨 AS peer | 5.0 |
| intra-AS | 2.0 |
| host/access | 0.5 |

设 workload `p` 连接网络的权重和为 `q_p`，当 manifest 没有显式 request 时，静态
估计为：

```text
cpu_p = 0.05 + 0.015 * q_p
memory_p = role-based estimate
```

memory 默认估计约为 host `128 MiB`、router `256 MiB`、border router `320 MiB`、
route server `384 MiB`；未识别的 role 使用 `192 MiB` fallback。

这些数值默认只参与 placement 评分和静态报告，不会写入 Pod
`resources.requests`。当前代表性实验均为：

```text
resourceRequestsInjected = false
```

只有显式使用 `--inject-resource-requests` 时才做 Kubernetes 资源准入实验。默认行为
保持与原 SeedEMU 部署一致：只约束 Pod 放置，不限制 Pod 可使用的 CPU/memory。
因此静态 `dominantUtilization > 1` 表示“估计需求超过模型容量”，不表示 Kubernetes
一定会拒绝或限制该 Pod；实际资源压力必须结合 `dynamicValidation` 判断。

### 5.3 当前实际目标函数

当前代码中的报告目标不是早期设计里的简单二项式，而是四项：

```text
J =
  alpha * C_network
  + beta * C_resource
  + w_pod * C_pod_pressure
  + w_vlan * C_vlan_parent_pressure
```

默认参数为：

```text
alpha = 1
beta = 1
w_pod = 8
w_vlan = 4
podCapSlack = 1.0
vlanParentCapSlack = 1.5
capacityBalanceRatio = 1.0
capacityHardCapPenalty = 100000
```

网络代价支持三种模式。设一个 network 的 endpoint 总数为 `n`，在各 node 上的数量为
`n_j`：

```text
ratio             = (occupiedNodeCount - 1) / (n - 1)
pair              = crossNodeEndpointPairs / allEndpointPairs
endpoint-exposure = (n - max(n_j)) / n
```

每个 network 的代价先乘 IX/peer/intra-AS/host 权重，再归一化。默认主优化使用
`ratio`。

资源项为：

```text
C_resource =
  0.7 * maxNodeDominantUtilization
  + 0.3 * average(CV(cpuUtilization), CV(memoryUtilization))
```

Pod pressure 和 VLAN-parent pressure 都按 node 目标 share 计算。目标 share 可以混合
均分与 CPU/memory capacity；当前 `capacityBalanceRatio=1.0` 表示完全按容量。每项
pressure 同时考虑最大目标比、CV 和超 hard-cap 程度：

```text
C_pressure = maxRatio + 0.25 * CV + 10 * hardCapExcess
```

greedy 搜索评分还额外加入 overload penalty、近似 hard-cap penalty 和确定性微小
tie-break。`placement_report.json.objective` 记录前述四个可解释组件；搜索期 penalty
用于阻止产生不可接受的 Pod 或 VLAN parent 集中。

### 5.4 确定性增量 greedy

算法流程为：

1. 按 network importance `q_p`、资源估计和 workload 名称确定性排序。
2. 对每个 workload 尝试所有候选 node。
3. 只增量重算该 workload 影响的 network、候选 node 资源、Pod count 和 VLAN
   parent count。
4. 选择评分最小的 node。
5. 可选执行确定性 single-move local improvement；大规模 runner 默认
   `improvement-passes=0`。
6. 用 `nodeSelector` 或显式 node name 写回最终 manifest，默认使用
   `nodeSelector`。

生成完整多算法静态报告时，`optimized` 还会在 optimized greedy 和已生成 baseline
候选中，先按 Pod hard-cap excess、再按四项目标选择最优候选。因此某些规模中
`optimized` 可能和 `network-only` 完全相同。`--selected-only` 大规模运行则直接生成
所选算法，不额外计算和择优其他 baseline。

`--fast-greedy-score` 避免每个候选位置都重算整个拓扑。15000 规模 optimized-only
实验的 placement 因使用 `--selected-only --fast-greedy-score` 只耗时 `272.73s`；
9955 的多 baseline 报告生成耗时 `3854.89s`。因此 placement 时间不能只按拓扑规模
比较，还取决于是否生成所有 baseline、是否启用增量评分和 improvement passes。

### 5.5 Baseline 的公平比较

| 方法 | 核心行为 |
| --- | --- |
| `optimized` | 联合优化网络、资源、Pod pressure 和 VLAN parent pressure |
| `network-only` | 只最小化 network cut |
| `resource-only` | 只优化估计 CPU/memory |
| `pod-count-balanced` | 只均衡 Pod 数 |
| `hypergraph` | 以共享 hyperedge affinity 聚合 workload，再用 Pod 数打破平局 |
| `kubernetes-default-scheduler` | 静态报告用 least-allocated 近似；动态实验真正移除 hard placement，由 Kubernetes 调度 |

动态 default scheduler baseline 没有 nodeSelector，因此无法生成精确 per-node image
列表。为避免把慢速 registry pull 误当作调度性能，当前使用
`buildBaselineAllNodePreload.sh` 把全部 workload image 预分发到所有 node，再让
Kubernetes 自由调度。它会增加 baseline 的预分发量，但消除了 image availability
对 deploy/Ready 的混淆。

### 5.6 静态与动态评估

`placement_report.json` 的核心结构为：

- `metadata`：算法、fabric、目标参数和是否注入 resource requests。
- `placements`：workload 到 node 的映射；selected-only 运行可能只含选中算法。
- `staticValidation`：各算法 objective、network cost、资源、Pod/VLAN pressure、
  secondary NIC、跨节点网络和 per-node 指标。
- `dynamicValidation`：deploy/wait-ready 期间 node CPU/memory、接口计数和最终 Pod
  状态。
- `routingValidation`：BIRD 和 FIB gate 摘要。
- `dataplaneValidation`：确定性 ping/iperf 报告和摘要。
- `b63Workflow`：每个阶段的命令、日志、耗时、状态和退出码。

macvlan+VLAN 下最重要的静态指标是：

- `networkCosts.ratio/pair/endpoint-exposure`
- `spanningNetworkCount`
- `weightedSpanningFanout`
- `vlanParentTotal`
- `vlanParentMaxPerNode`
- `vlanParentCv`
- `secondaryNicEstimate`
- `nodeCrossNetworkPressure`

OVS port 估计字段只保留兼容性，不应作为 macvlan+VLAN 的主要结论。

### 5.7 Placement 效果证据

4954/12VM 是目前 optimized 与 default scheduler 同规模、同 pair plan 的最完整动态
对照：

| 静态指标 | optimized | default scheduler | optimized 改善 |
| --- | ---: | ---: | ---: |
| network cost ratio | 0.169 | 0.928 | 降低 81.8% |
| spanning networks | 945 | 4737 | 降低 80.1% |
| VLAN parent total | 6090 | 10392 | 降低 41.4% |
| VLAN max/node | 564 | 880 | 降低 35.9% |

对应动态结果为：

- baseline ping 平均 RTT 降低 `24.6%`，p95 RTT 降低 `65.2%`。
- baseline TCP aggregate goodput 提高 `26.6%`。
- baseline Jain fairness 从 `0.777` 提高到 `0.946`。
- stress 64 并发 goodput 提高 `91.7%`。
- stress 64 loaded RTT 从 `11.297 ms` 降到 `1.950 ms`，降低约 `82.7%`。

这说明减少跨 node L2 fan-out 不只是改善静态分区分数，也能降低真实 underlay 竞争和
排队。

9955/36VM optimized 已通过 compile、placement、deploy、BIRD、FIB 和 dataplane；
15000/48VM optimized-only 也完成全流程。但 15000 没有同配置 baseline，其
`networkCostRatio=0.606`、`VLAN max/node=1179`，只能证明最大规模可运行，不能单独
证明相对算法优势。

## 6. 各核心阶段的详细契约

### 6.1 build-cluster(s)

**输入**

- b62：`assignment.yaml` 渲染出的 `configKvmOvn.yaml`。
- b63：`configkvm_b63.yaml` 或 VM matrix YAML。

**核心操作**

- 创建 KVM disk、cloud-init、management NIC 和额外 VLAN trunk NIC。
- 调整 VM 高密度系统限制。
- 安装 K3s server/agents，配置 primary flannel、PodCIDR、`maxPods`。
- 在 master 准备本地 registry，并给所有 K3s node 配置 mirror。
- 安装 Multus 和 macvlan/static CNI plugin。
- `fabric.type=macvlan-vlan` 时不安装 workload Kube-OVN/OVS。
- 生成 inventory、kubeconfig 和可供 destroy 使用的 cluster metadata。
- 解除 master taint，使其在配置允许时参与 placement。

**输出与 gate**

```text
configK3s*.yaml
kubeconfig*.yaml
inventory*.yaml
build-cluster.log
```

所有 node 必须 Ready，registry 和 Multus 必须可用。该阶段耗时主要随 VM 数增加，而
不是随 SeedEMU topology size 线性增加。

### 6.2 compile(s)

**输入**

- `real_topology_<scale>.txt`，最大规模使用无后缀 `real_topology.txt` 的快照。
- `assignment.pkl`、SeedEMU 源码、namespace 和 CNI/trunk 参数。

**核心操作**

- 构造真实拓扑，render SeedEMU layers。
- 当前路由协议参数为 OSPF `hello=10`、`dead=40`、`tick=3`，iBGP/eBGP
  `holdTime=36000`、`keepaliveTime=60`。
- Transit router 镜像加入 `iperf3`。
- 为每个 SeedEMU L2 network 分配 trunk/VLAN，并生成 Multus NAD。
- 生成 workload manifest、镜像 build context 和 `build_images.sh`。
- b62 在此阶段同时生成 by-AS nodeSelector。
- b63 明确使用无 placement 模式，保留 raw/source manifest 给下一阶段。

**输出与 gate**

```text
output/k8s.raw.yaml        # b63
output/k8s.source.yaml     # b63
output/k8s.yaml            # b62 或 b63 placement 后
output/build_images.sh
output/images.*
```

manifest 必须可解析，所有 network/VLAN/trunk 映射必须有效，镜像元数据必须完整。

### 6.3 placement(s)，b63 新增

**输入**

- b63 raw manifest。
- `inventory-b63.yaml`、`configK3s-b63.yaml` 或 KVM config 中的 node 容量。
- 算法、权重、cap 和是否包含 master 等参数。

**核心操作**

- 建立 workload-network hypergraph。
- 估算 workload CPU/memory 和 secondary NIC。
- 运行 optimized 或 baseline。
- 对 selected placement 计算静态指标。
- 默认写入 hostname nodeSelector，但不注入 resource requests。

**输出与 gate**

```text
output/k8s.yaml
output/network_weights.yaml
output/placement_report.json
placement.log
```

每个 workload 必须有合法 node，最终 report 必须标明 fabric、hard-cap excess 和
resource request 注入状态。Pod/VLAN cap 是搜索期近似 hard penalty，不是 Kubernetes
admission gate；有 excess 时必须在报告和后续动态验证中解释。

### 6.4 build(s)

**输入**

- 最终 deploy manifest、`build_images.sh`、registry 和 inventory。
- hard placement，或 default scheduler 的 all-node 模式。

**核心操作**

- 上传 compile 产物到 master。
- 通过 Docker/buildx 和 fast archive 构建镜像。
- 生成 `image_refs.txt` 和 `node_image_refs/`。
- hard-placement 方法只向目标 node 导入所需 archive。
- default scheduler 方法向所有 node 导入全部 workload image。
- 失败操作带 registry/preload retry 和 backoff。

**输出与 gate**

```text
build.log
build_remote.log
image_refs.txt
node_image_refs/
preload_<node>.log
```

所有目标 node 必须具备其 workload 镜像。当前 b63 常用并发为 build `12`、
preload node `4`、单 node image `2`，实际值应以该轮 `summary.json.command` 为准。

### 6.5 deploy(s)

**输入**

- 最终 `output/k8s.yaml`。
- placement、inventory、Kubeconfig 和 deploy pressure 参数。

**核心操作**

- 从 manifest 推导 node-scoped VLAN parent，只在需要的 node 创建。
- 按资源类型拆分 manifest，先应用 namespace/NAD 等静态对象，再应用 controller。
- 使用小 batch warm-up，随后按普通 batch 创建。
- 每批前后检查 Pending、Creating、Running-NotReady、Failed、node Ready 和
  max Creating per node。
- b63 同时由 `DynamicSampler` 采集 node CPU/memory 和 fabric interface counters。

**输出与 gate**

```text
deploy.log
output/deploy_batches/
placement_report.json.dynamicValidation
```

batch size 不是固定常数：b62 4954 代表配置为 `80`，b63 runner 默认 `10`，15000
成功 run 使用 `50`。因此分析 deploy 时间时必须同时报告 batch、sleep、VM 数和
pressure limits。

### 6.6 wait-ready(s)

**核心操作**

- 周期读取 namespace 中的 Pod。
- 分别统计 total、Running、Ready、Failed 和 router-like (`r/brd/rs`) 状态。
- 只有所有目标 workload Running/Ready 才允许进入 routing 阶段。

这一步把 Kubernetes/CNI 问题和 BIRD 协议问题分开。若 deploy 已等待了大部分 Pod，
该阶段可以很短；因此 wait-ready 时间不能单独代表整体部署速度。

### 6.7 start-bird(s)

**核心操作**

- 选择 Running 的 router-like SeedEMU workload。
- 按 node 分组，所有 node 并行、node 内默认串行。
- 通过 SSH、`crictl`、`nsenter` 在 container namespace 中启动 BIRD。
- 默认目标间隔 `0.1s`，支持 batch、node concurrency、timeout 和 load gate。
- 已运行的 BIRD 被幂等跳过。

**输出与 gate**

```text
start_bird.log
start_bird_targets.json
start_bird_summary.json
```

所有目标必须成功启动或确认已运行。该阶段只证明进程状态，不证明 BGP/OSPF 已收敛。

### 6.8 verify-bird(s)

该阶段在脚本和 JSON 中名为 `verify-after-start-bird`。

**核心操作**

- 对所有 VM 执行 load gate。
- 选取前 10 个 ASN，每个 ASN 固定取最小 router ID 的 brd Pod。
- 检查 `birdc show protocols` 和 `birdc show route count`。
- 要求存在 BGP，协议状态无 error/fail/unreachable/no-route/refused/timeout/inactive，
  且 route/network count 有效。
- 默认允许 `300s` 协议收敛重试窗口。

**输出与 gate**

```text
verify_after_start_bird.log
verify_after_start_bird_targets.json
verify_after_start_bird_summary.json
```

这是 start-kernel 的强制前置 gate。

### 6.9 start-kernel(s)

**核心操作**

- 使用和 start-bird 相同的 node-local 执行模型。
- 在每个 router-like container 写入 `/etc/bird/conf/kernel.conf`。
- 配置 `export all`、长 scan interval 和 jitter。
- reload BIRD，使 RIB 写入 Linux FIB。
- 每 node 完成后执行 load gate。

**输出与 gate**

```text
start_bird_kernel.log
start_bird_kernel_targets.json
start_bird_kernel_summary.json
```

该阶段的成本随路由数量、每 node router 数和内核邻居/路由表压力增长。15000 run 中
它耗时 `6951.79s`，已经是 deploy 之后最大的单阶段瓶颈。

### 6.10 verify-kernel(s)

该阶段在脚本和 JSON 中名为 `verify-after-fib-write`。

**核心操作**

- 对所有 VM 执行 load gate。
- 使用与 verify-bird 相同的 10-AS 确定性样本。
- 比较 `ip route | wc -l` 和 `birdc show route count` 的 network count。
- 当前 pass 条件为 `Linux FIB route count > BIRD network count`。

**输出与 gate**

```text
verify_after_fib_write.log
verify_after_fib_write_targets.json
verify_after_fib_write_summary.json
```

该检查证明 kernel export 产生了预期方向的 FIB 增长，但不证明每条路由的
next-hop、AS path 和可达性都正确。ping/iperf 是其后的数据面补充。

### 6.11 reconvergence(s)

**当前 b62 行为**

- 使用 10 个固定 Pod name prefix 作为 chaos targets。
- 使用 4 个固定 Pod name prefix 作为 route-count verify targets。
- 记录 baseline `ip route | wc -l`。
- 对 chaos target 执行 `birdc down`。
- 等待 load gate 后，至少一个 verify target route count 减少或总数减少即判定故障侧
  收敛。
- 恢复 BIRD 后，至少一个 verify target route count 增加或总数增加即判定恢复侧
  收敛。
- 默认每个 phase timeout 为 `1800s`。

**输出**

```text
reconvergence.log
reconvergence_targets.json
reconvergence_summary.json
```

当前实现是低成本的 route-count 方向检查，并且默认 target prefix 与特定拓扑命名
绑定。若要接入 b63 多规模默认流程，应先改成基于 topology hash 的确定性 target plan，
并区分 daemon failure 与单 protocol/link failure。

### 6.12 dataplane，b63 补充动态阶段

虽然它不在用户给出的十阶段名称中，但它是当前 b63 默认 full-flow 的必要部分。

- 必须在 verify-kernel PASS 后执行。
- pair plan 只由 topology、固定 seed 和版本决定，不由某个 placement 反向选 pair。
- v2 默认最多 `96` pair、`192` direction。
- 覆盖 `intra_as_link`、`ebgp_ix_peer`、`cross_as_multihop`。
- 执行 baseline ping/TCP，以及 `8,16,32,48,64` 并发、每级 3 轮的 stress TCP。
- 记录 loaded RTT/loss、goodput、Jain fairness、flow p10/p25/median、failure rate、
  endpoint/node appearance 和 start skew。

主要输出为：

```text
dataplane/pair_plan_*.json
dataplane/latency_throughput_report.json
dataplane/latency_throughput_summary.json
dataplane/stress_rounds.json
```

不同算法必须复用同一 pair plan。raw aggregate goodput 不能单独排序，应优先联合解释
loaded RTT、loss、fairness、低分位 flow goodput 和 endpoint/node 偏斜。

## 7. 代表性阶段耗时

下表均来自状态为 PASS 的 `summary.json`，单位为秒。`verify-bird` 对应
`verify-after-start-bird`，`verify-kernel` 对应 `verify-after-fib-write`。

| Stage | b62 4954/12VM | b63 4954/12VM optimized | b63 9955/36VM optimized | b63 15000/48VM optimized |
| --- | ---: | ---: | ---: | ---: |
| build-cluster(s) | 694.39 | 766.89 | 1885.30 | 2467.39 |
| compile(s) | 655.53 | 664.59 | 948.84 | 1430.21 |
| placement(s) | compile 内完成 | 323.68 | 3854.89 | 272.73 |
| build(s) | 311.50 | 278.52 | 627.61 | 945.22 |
| deploy(s) | 1290.87 | 1074.52 | 30813.32 | 18582.70 |
| wait-ready(s) | 200.11 | 375.12 | 7.21 | 74.85 |
| start-bird(s) | 89.99 | 196.27 | 36.67 | 454.52 |
| verify-bird(s) | 7.87 | 17.97 | 16.95 | 124.60 |
| start-kernel(s) | 158.16 | 649.35 | 1255.01 | 6951.79 |
| verify-kernel(s) | 8.86 | 11.10 | 22.12 | 116.26 |
| reconvergence(s) | 33.42 | 未接入 b63 orchestrator | 未接入 b63 orchestrator | 未接入 b63 orchestrator |
| dataplane(s) | 非 b62 默认阶段 | 1180.46 | 1586.95 | 2303.93 |

完整记录还包括 destroy、clean、preflight、master schedulable 等阶段：

| Run | 已记录 PASS stages 总和 | 说明 |
| --- | ---: | --- |
| b62 4954/12VM | `3480.21s` | full-flow 墙钟 `3480.80s`，无 dataplane |
| b63 4954/12VM | `5619.80s` | 含 cluster rebuild 和 dataplane |
| b63 9955/36VM | `41271.57s` | deploy 使用保守参数，占绝对主导 |
| b63 15000/48VM | `34041.55s` | 约 9.46 小时已记录阶段时间 |

这些数值不能直接解释成“规模越大一定越慢”：

- VM 数会影响 build-cluster、并行 BIRD/FIB 和每 node Pod 密度。
- deploy batch、sleep、pressure gate 会显著影响 deploy 时间。
- placement 是否 selected-only、是否 fast greedy、是否生成所有 baseline 会改变
  placement 时间。
- wait-ready 只统计 deploy 返回后的尾部等待，deploy 本身可能已经等待大部分 Pod。
- 15000 比 9955 的 deploy 更快，主要因为 48VM 和更积极的 batch 配置，不代表拓扑
  工作量更小。

## 8. 各阶段之间的关键依赖

```text
build-cluster
  produces kubeconfig + inventory + registry + Multus
      |
      v
compile
  produces raw manifest + image contexts + network/VLAN mapping
      |
      v
placement (b63)
  produces final nodeSelector manifest + placement_report static section
      |
      +------------------------+
      |                        |
      v                        v
build                      deploy
uses node->image map       uses node->VLAN-parent map
      |                        |
      +-----------+------------+
                  v
              wait-ready
                  v
              start-bird
                  v
              verify-bird
                  v
              start-kernel
                  v
              verify-kernel
                  |
          +-------+--------+
          v                v
      dataplane       reconvergence
      (b63 current)   (b62 current)
```

最重要的依赖是：**placement 必须在 build 和 deploy 之前固定**。否则 per-node image
preload 和 node-scoped VLAN parent 都无法准确准备。唯一例外是 Kubernetes default
scheduler baseline，它通过 all-node preload 和 deploy 时的安全 fallback 保留调度
自由度。

## 9. 当前结论与尚需补齐的部分

1. b62 的主要贡献是把原生 Kubernetes workflow 改造成一个在 4954/9955 级别可运行、
   可限流、可恢复、可验证的大规模系统。关键不是某一个参数，而是
   VLAN-backed fabric、hard placement、fast archive、per-node preload、
   placement-aware deploy、node-local routing 操作和 load-gated verification 的组合。

2. b63 把 b62 的 by-AS heuristic 提升为可解释的多目标 placement。当前实际目标已把
   Pod pressure 和 VLAN-parent pressure 纳入，而不再只是 network/resource 二项式。

3. 默认不注入 CPU/memory requests 是正确的实验边界。静态资源估计用于算法比较，
   不应让 Kubernetes admission 因人为 request 把原本可运行的 SeedEMU Pod 卡住。
   资源约束应作为单独变量显式开启。

4. 4954 的同规模动态对照已经证明 optimized 减少 VLAN/network fan-out 后，loaded
   RTT、fairness 和并发 goodput 同时改善。相比只看 objective，这是一条更完整的
   “静态 placement -> host fabric -> 数据面性能”证据链。

5. 9955 和 15000 证明了 optimized 的可运行性，但 15000 只有 optimized-only，
   不能据此声称相对 baseline 最优。15000 的高 network ratio、VLAN max/node 和
   master 负载也说明目标函数仍需加强 control-plane 节点惩罚或 workload cap。

6. b63 尚未把 reconvergence 接回默认 orchestrator。下一步应先把固定 Pod prefix
   改成 topology-derived deterministic target plan，再在
   `runB63LargeExperiment.py` 中增加阶段、routing report 回填和恢复后验证。

7. 当前 verify-bird/verify-kernel 是确定性抽样 gate，而不是全量正确性证明。正式论文
   结论应同时保留 sampled protocol/FIB gate、deterministic ping/iperf、失败率和
   run-to-run repetition。

8. 当前最大瓶颈已经从 OVN/OVS control plane 转移为大规模 batch deploy、单 node
   VLAN/Pod 压力和 start-kernel FIB 写入。后续优化应优先考虑自适应 deploy batch、
   master placement cap、按 node 的 FIB 写入速率控制及更细的内存容量模型。
