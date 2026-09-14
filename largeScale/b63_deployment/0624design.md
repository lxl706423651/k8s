# B63 Placement 实验方案 2026-06-24

## 目标

B63 在 B62 大规模 SeedEMU/K3s 实验流程上只改变 workload placement，不复用
B62 已生成的 cluster。每次实验都从 `configkvm_b63.yaml` 构造 b63 自己的
KVM/K3s cluster，运行目录统一为：

```text
experiments/<scale>_<YYYYmmdd_HHMMSS>/
```

核心产物是：

```text
output/placement_report.json
```

该报告需要同时包含静态评估和部署后的动态评估。

## B62 流程抽象

B62 的完整阶段可以拆成三层：

1. Cluster 层：渲染配置、清理旧 cluster、创建 KVM/K3s、输出
   `configK3s.yaml`、`kubeconfig.yaml`、`cluster.inventory.yaml`。
2. Workload 层：clean、preflight、compile、build、deploy、wait-ready。
3. Routing 层：start-bird、verify-after-start-bird、start-kernel、
   verify-after-fib-write、可选 reconvergence/BGP test。

B63 保留这个阶段结构，但把运行脚本放在 `running/`，并把 b62 的
assignment/env 模式改为由 `runB63LargeExperiment.py` 给每个脚本传入显式参数。

## CNI 变化对目标函数的影响

B62 早期 OVN/OVS 模式的主要风险是：

- Kube-OVN controller/IPAM 处理大量 Subnet、logical switch、logical port；
- OVS/OVN daemon 和 CNI ADD 对并发非常敏感；
- 每个 secondary interface 对应 OVS/OVN 端口和控制面状态。

B63 当前改为 macvlan+VLAN 后，瓶颈变化为：

- 不再把 OVS logical port 数作为主要解释指标；
- 每个 SeedEMU L2 network 对应一个 VLAN parent，例如 `ens2.100`；
- 一个网络如果被 placement 分散到多个 K3s node，就需要在多个 node 上创建同一
  VLAN parent，并产生跨 VM/underlay 的同 VLAN 流量；
- Multus/macvlan CNI 仍要为每个 Pod secondary NIC 做 ADD，因此 secondary NIC
  总数仍然重要；
- Linux host 上的 VLAN subinterface 数、节点资源负载和 BIRD/FIB 稳定性比
  OVN Subnet Ready 更能说明效果。

结论：核心 placement 目标不需要推倒重来，仍然应最小化网络割裂并均衡资源。
但是评估指标必须修改：OVN/OVS port/control-plane 指标只能作为兼容字段，主指标
应改成 macvlan+VLAN 的 fan-out 和节点压力。

## Placement 目标函数

当前 b63 placement 引擎保留两类目标：

```text
objective = alpha * networkCost + beta * resourceCost
```

`networkCost` 支持三种模式：

- `ratio`：一个网络跨越的节点数相对 endpoint 数的比例。
- `pair`：跨节点 endpoint pair 占总 pair 的比例。
- `endpoint-exposure`：不在最大节点分区内的 endpoint 比例。

`resourceCost` 关注：

- dominant utilization 最大值；
- CPU/memory 利用率 CV，用于衡量节点间负载倾斜。

这些 CPU/memory 数值默认只作为 placement 目标函数和报告指标，不写入 Kubernetes
`resources.requests`。这样完整部署只约束分区/节点放置，保持与既有 SeedEMU
部署方式一致；只有显式开启 `--inject-resource-requests` 时，才把估计值写入
manifest，让 Kubernetes scheduler 做额外的资源准入实验。

macvlan+VLAN 下新增报告指标，但默认不直接加入 objective，原因是这些指标大多是
network cut 的派生量。若后续发现 VLAN parent 数比流量割裂更直接影响失败率，可以
把 `vlanParentTotal` 或 `weightedSpanningFanout` 加入 objective 的第三项：

```text
objective = alpha * networkCost + beta * resourceCost + gamma * fabricCost
```

当前建议先通过实验确认 `fabricCost` 与动态失败率/ready time 的相关性，再决定
是否引入 `gamma`。

## 静态评估指标

`placement_report.json.staticValidation` 对每个算法/baseline 输出：

- `objective`：当前算法优化目标。
- `networkCosts.ratio/pair/endpoint-exposure`：三种网络割裂度。
- `resourceCost.dominantMax/dominantAverage/capacityWeightedSkew`：资源负载。
- `perNode.*.podCount`：每节点 workload 数。
- `perNode.*.secondaryNicEstimate`：每节点 secondary NIC 数。
- `perNode.*.vlanParentEstimate`：每节点需要的 VLAN parent 数。
- `fabricMetrics.spanningNetworkCount`：跨节点的 SeedEMU network 数。
- `fabricMetrics.weightedSpanningFanout`：加权跨节点 fan-out。
- `fabricMetrics.vlanParentTotal`、`vlanParentMaxPerNode`、`vlanParentCv`：
  VLAN parent 总量、峰值和倾斜度。
- `nodeCrossNetworkPressure`：节点参与跨节点网络的加权压力估计。

这些指标用于回答：placement 是否减少跨节点 L2 fan-out，是否降低某些 VM 上的
VLAN parent/NIC/Pod 集中度，是否没有造成资源倾斜。

## 动态评估指标

完整部署后，`placement_report.json.dynamicValidation` 记录：

- deploy 开始、Ready 时间、结束时间；
- final workload state：Pod 数、Ready Pod 数、phase 分布、实际 node 分布；
- 每个 K3s node 的 CPU/memory average/peak；
- SeedEMU fabric 相关接口吞吐：`ens2.<vlan>`、macvlan、bridge、OVN/VXLAN
  兼容接口；
- `interfaceFamiliesObserved`：区分 `vlan-parent`、`macvlan`、`bridge`、
  `ovn-overlay`、`vxlan-overlay`；
- deploy error 或 kubectl 采样错误。

Routing 层在 `summary.json` 和 `placement_report.json.routingValidation` 中记录：

- `verify_after_start_bird_summary.json`：BIRD 协议健康；
- `verify_after_fib_write_summary.json`：BIRD 写入 Linux FIB 后的路由数量检查；
- 可选 `reconvergence_summary.json`：故障恢复时间。

## 时延与吞吐主动测量

`examples/kubernetes/real_topology_k3s_compile.py` 已在 transit router 上加入
`routers[ix].addSoftware("iperf3")`，因此可以把 router pod 本身作为主动测量端点。
该测量的目标不是替代 BIRD/FIB 正确性验证，而是在 placement 之间提供更直观的
动态特征：端到端 RTT、丢包、TCP/UDP 吞吐、抖动、重传和跨节点 underlay 代价。

测量必须在 `verify-after-fib-write` 之后执行。原因是 ping/iperf 走的是容器 Linux
FIB；如果 BIRD 尚未收敛或 kernel export 尚未完成，测到的是路由未就绪，而不是
placement 对 dataplane 性能的影响。

0625 起，`runB63LargeExperiment.py` 的完整流程默认把该测量作为
`verify-after-fib-write` 后的 `dataplane` stage 执行；只有显式传
`--skip-dataplane` 才跳过。这样每个全流程 artifact 都应同时包含 static、
deploy/routing dynamic 和 ping/iperf dataplane dynamic 三类指标。

端点选择原则：

- 不使用 Kubernetes Pod IP 作为目标地址，因为 Pod IP 主要反映 K3s/CNI 管理网络，
  不能代表 SeedEMU emulated Internet dataplane。
- 使用 router 的 SeedEMU 接口地址，例如 IX LAN、transit internal link 或 stub
  接入网络上的地址。地址可通过 `kubectl exec <router-pod> -- ip -j addr` 获取，
  并过滤掉 `eth0`/Kubernetes 管理地址。
- 当前代码至少保证 transit AS router 有 `iperf3`。如果后续希望 stub router 也参与
  iperf 测量，应在 `makeStubAsWithHosts()` 创建的 stub router 上同样
  `addSoftware("iperf3")`。

### Pair 采样设计

为了公平比较不同 placement，同一规模、同一拓扑下应使用同一个 canonical pair set。
pair set 只由拓扑决定，不由某一次 placement 的节点分配决定；每次实验部署完成后，
再根据实际 pod-to-node 结果给 pair 打标签：

- `nodeRelation`: `same-node` 或 `cross-node`。
- `asRelation`: `same-as` 或 `cross-as`。
- `topologyRelation`: `same-l2`、`same-as-internal-link`、`ebgp-neighbor`、
  `multi-hop-routed`。
- `fabricRelation`: 是否跨 K3s node、是否跨 VLAN parent、是否跨 IX LAN。

建议 canonical pair set 分层抽样：

- same-AS internal router pair：同一 transit AS 内、直接 internal link 相连的 router
  pair，用于观察同一个 AS 被 placement 切开后的代价。
- same-AS non-neighbor pair：同一 AS 内但不是直接 internal link 的 router pair，
  用于观察 AS 内多跳路径。
- cross-AS eBGP neighbor pair：在同一 IX 上有 eBGP peering 的 pair，用于观察跨
  AS 直连业务。
- cross-AS multi-hop pair：AS path 长度大于 1 的 pair，用于观察真实跨域路径。
- placement-diagnostic supplement：如果某个算法在 canonical set 中缺少
  `same-node` 或 `cross-node` 样本，可以额外补充诊断 pair；这类 pair 只能用于解释
  单次部署，不应和 canonical aggregate 混在一起作为主结论。

### 可复现 Pair 生成规则

pair 生成必须 deterministic。禁止每次运行用无 seed 的随机抽样。建议生成一次
`pair_plan_<scale>_<topology_hash>.json`，后续所有算法直接复用该文件；如果拓扑、
assignment 或 pair 生成参数变化，才生成新的 pair plan。

输入：

- `real_topology_<scale>.txt`：IX、transit AS、AS-AS-IX edge、IX-IX-transit edge。
- `assignment.pkl`：真实 AS/IX 到 SeedEMU ASN/prefix 的映射。
- `rr_plan.json`：每个 transit AS 的 router/exchange 列表和 internal link。
- `output/k8s.raw.yaml` 或 compiled manifest：router pod 命名和 namespace。

固定参数：

```text
pair_seed = 20260624
pair_plan_version = 2
pairs_per_stratum = 32
max_pairs_total = 96
directions = both
```

0625 起主动测量默认使用 v2 pair plan。v2 相比 v1 的变化：

- canonical pair set 扩大到最多 96 pair，每个 stratum 默认 32 pair。
- 默认 stratum 包含 `intra_as_link`、`ebgp_ix_peer`、`cross_as_multihop`，覆盖 AS
  内链路、IX/eBGP 直连和跨 AS 多跳路径。
- pair 生成时加入 per-endpoint appearance cap，初始 cap 为 2；如果某个 topology
  stratum 天然有 hub endpoint，才按 deterministic 规则放宽 cap。pair plan 会记录
  `selectionSummary.endpointAppearanceCv`，用于判断 pair set 本身是否偏斜。
- stress round 内同一 unordered pair 默认最多出现一个方向，避免同一 pair 的双向
  iperf 在同一轮把局部路径放大。full-duplex 只作为单独诊断，不混入主 aggregate
  结论。
- 每个 stress round 记录 `selection.nodeEndpointAppearanceCv`、
  `selection.bidirectionalPairCount` 和 `selection.endpointAppearance`。如果某轮 node
  endpoint appearance 明显偏斜，则该轮 raw aggregate Mbps 只能作为辅助说明，主排序
  应看 latency/fairness/loss/quality score。

候选 pair 的排序必须完全确定：

```text
sort_key = (
  topologyRelation,
  min(srcAsn, dstAsn),
  max(srcAsn, dstAsn),
  min(srcRouterId, dstRouterId),
  max(srcRouterId, dstRouterId),
  sharedIxOrLinkId,
  srcLogicalName,
  dstLogicalName
)
```

抽样方式用 deterministic hash，而不是运行时随机：

```text
score = sha256(
  f"{pair_seed}|{pair_plan_version}|{scale}|{topology_hash}|"
  f"{topologyRelation}|{srcAsn}|{srcRouter}|{dstAsn}|{dstRouter}"
).hexdigest()
```

每个 stratum 内按 `(score, sort_key)` 升序取前 `pairs_per_stratum` 个。这样即使候选
集合很大，每次生成结果也完全一致；同时又不会总是偏向最小 ASN 或最小 router ID。
`topology_hash` 使用 `real_topology_<scale>.txt`、`assignment.pkl`、`rr_plan.json`
的内容 hash，写入 pair plan。运行时如果 hash 不匹配，必须拒绝复用旧 pair plan。

pair ID 也必须稳定：

```text
pairId = "{topologyRelation}-{rank:03d}-{short_hash}"
```

pair plan 最少包含：

```json
{
  "metadata": {
    "scale": 1078,
    "pairSeed": 20260624,
    "pairPlanVersion": 2,
    "topologyHash": "...",
    "pairsPerStratum": 12,
    "maxPairsTotal": 72,
    "directions": "both"
  },
  "pairs": [
    {
      "pairId": "same-as-internal-link-001-ab12cd34",
      "topologyRelation": "same-as-internal-link",
      "srcAsn": 1602,
      "srcRouter": "r12",
      "dstAsn": 1602,
      "dstRouter": "r23",
      "sharedNetwork": "net_12_23",
      "measurementIps": {
        "srcToDst": "10.x.y.z",
        "dstToSrc": "10.x.y.w"
      }
    }
  ]
}
```

生成规则细节：

- same-AS internal router pair：从 `rr_plan.json.internal_links` 取同一 transit AS 的
  direct router link，按 deterministic hash 选。
- same-AS non-neighbor pair：同一 transit AS 的 router 两两组合，排除 internal link
  直接邻居；优先选择最短路径长度为 2 或 3 的 pair。
- same-L2 IX pair：同一 IX LAN 上的 transit router 两两组合，排除同一 AS 的同一
  router；目标 IP 使用共享 IX LAN 的对端接口 IP。
- cross-AS eBGP neighbor pair：从 `as_as_ix_edges` 中选同一 IX 上存在 eBGP peering
  的两端 router。
- cross-AS multi-hop pair：选择没有直接 eBGP peering、但 BIRD 可达的 AS pair；优先
  选择 AS path 长度 2-4 的 pair，避免路径过长导致测量失败难以解释。

每个 pair 固定方向和反向都测，形成 direction samples。主分析聚合时可以同时保留
direction-level 结果和 pair-level average。部署后再用实际 `pod -> K3s node` 结果
补充 `same-node/cross-node` 标签；这个标签不能反过来影响 canonical pair 选择。

如果 canonical pair set 在某个算法下 `same-node` 或 `cross-node` 样本太少，可以生成
`diagnostic_pair_plan.json`。诊断 pair 允许基于实际 placement 补足样本，但必须单独
报告，不能混入主 `pair_plan` 的 aggregate 结论。

### 一到两小时内的执行预算

1078 规模建议主实验控制在 `60-90` 分钟内完成，最多不超过 `120` 分钟。测量分成两层：

- low-interference baseline：低并发测单流路径质量，避免测量流量本身影响结果。
- high-concurrency stress：多组 iperf 同时启动，主动制造共享 fabric/underlay 竞争。
  这层更能体现不同 placement 在 VLAN parent fan-out、跨 node 流量和热点节点上的差异。

默认参数：

```text
canonical_pairs = 96
direction_samples = 192
ping_per_direction = 10 packets
baseline_tcp_iperf_directions = 48
baseline_tcp_iperf_duration = 8s
baseline_tcp_iperf_concurrency = 4
stress_concurrency_levels = [8, 16, 32, 48]
stress_repetitions_per_level = 3
stress_iperf_duration = 15s
stress_cooldown_between_rounds = 10s
loaded_ping_canary_directions = 16
udp_iperf_pairs = 12
udp_iperf_duration = 8s
cooldown_between_batches = 5s
```

预计时间：

- pair 解析、地址发现、标签补全：`2-5` 分钟。
- ping：192 个 direction，每个约 `2-4` 秒；并发 `8-12`，总计约 `3-8` 分钟。
- baseline TCP iperf：24 个 pair 双向或 48 个 direction，8 秒一条；并发 `4`，
  总计约 `4-8` 分钟。
- high-concurrency TCP stress：`8/16/32/48` 并发，每级 3 轮，每轮 15 秒，轮间冷却
  10 秒，总计约 `12-25` 分钟；如果前四级没有明显 overload，可单独追加 `64` 并发
  作为诊断，不作为默认主指标。
- loaded ping：stress 期间对 16 个 canary direction 每秒 ping 一次，用于测拥塞时延和
  抖动；与 stress 同时运行，不单独显著增加时间。
- UDP iperf：12 个 pair 抽样，8 秒一条；并发 `1-2`，总计约 `2-6` 分钟。
- 前后 node/interface 采样、失败重试和 JSON 汇总：`5-10` 分钟。

单算法 dataplane 测量预算约 `25-55` 分钟。若加上 deploy、BIRD/FIB 阶段，单个算法
完整动态实验预计 `50-100` 分钟。比较 `optimized` 与 `default scheduler` 两个算法时，
建议分两次 run，而不是同一时间在一个 cluster 上并行部署。

执行调度：

- Ping 可批次并发，默认 `ping_concurrency=12`。Ping 流量很小，适合快速覆盖所有
  direction；但同一个源 pod 同时最多发起 `1` 个 ping，避免本地排队。
- baseline TCP iperf 使用并发 `4`，仍限制同一个源 pod 同时最多一个 client。
- high-concurrency stress 使用固定并发层级。每一轮从 canonical direction set 中按
  deterministic hash 选择 `C` 条 direction：

```text
stressScore = sha256(
  f"{pair_seed}|stress|{concurrency}|{repetition}|{pairId}|{direction}"
)
```

  按 `stressScore` 排序取前 `C` 条，但同一 pair 在同一轮最多出现一个方向，且同一
  endpoint 在一轮内默认最多出现 2 次；如果候选不足，按 deterministic 规则逐步放宽
  endpoint cap，并在 round summary 中记录 `relaxedEndpointCap`。不同算法使用完全相同
  canonical pair plan；round 选择只使用 pair/direction ID，不使用运行时 node 标签。
- stress round 内先在所有目标 pod 启动 `iperf3 -s -1 -J -p <port>`，确认 server 就绪
  后，在 `1-2` 秒窗口内同时启动 client。记录每个 client 的实际 start/end timestamp
  和 start skew；start skew 超过 `2s` 的 round 标记为 `skewed`。
- stress round 不按运行时 K3s node 反向挑选 pair，否则不同算法会测到不同逻辑路径。
  但是每轮必须记录 `activeEndpointsByNode`、`nodeEndpointAppearanceCv` 和
  `bidirectionalPairCount`。如果 raw aggregate Mbps 与这些偏斜指标强相关，主结论必须
  降低 aggregate 的权重。
- 如果某个并发层级导致任一 worker `load1 > 1.5 * vCPU`、Pod NotReady、或 iperf
  failure rate 超过 `20%`，该层级之后的更高 optional level 不再执行；当前层级结果
  仍保留为 stress failure/overload 证据。
- UDP iperf 只抽样，默认批次大小 `1-2`。如果 UDP loss 大于 `5%`，不自动提高带宽，
  只记录结果；另开压力实验再调大 `-b`。
- 每个批次前后记录 node load 和 host interface counters。若任一 K3s node
  `load1 > 0.8 * vCPU` 或 CPU 使用率高于 `85%`，baseline 阶段等待最多 `120` 秒；
  stress 阶段不等待到完全空闲，只记录 starting load，因为 stress 本身就是要观察
  contention。但如果节点已经 NotReady，则暂停并标记该轮无效。
- 失败重试只做一次。重试仍失败的 direction 标记为 failure，不无限重试。
- 所有命令按 pair plan 顺序派生稳定执行顺序：`executionOrder =
  sha256(pairId|direction|testType|pair_seed)` 排序。不同算法使用完全相同顺序。

输出文件：

```text
pair_plan_<scale>_<topology_hash>.json
latency_throughput_report.json
latency_throughput_summary.json
ping_raw/
iperf_raw/
stress_round_plan.json
stress_raw/
```

### Ping 指标

对每个方向执行：

```text
ping -c 10 -i 0.2 -W 1 <dst_seedemu_ip>
```

记录：

- `rttMinMs/rttAvgMs/rttMaxMs/rttMdevMs`；
- `rttP50Ms/rttP95Ms/rttP99Ms`，从单包 RTT 明细计算；
- `packetLossPct`、超时次数；
- 按 `nodeRelation/asRelation/topologyRelation` 聚合的 median、p95 和 loss。

Ping 的主用途是比较时延和尾延迟。对 placement 来说，重点看：

- same-AS pair 被放到 `cross-node` 后，RTT/p95 是否明显恶化；
- cross-AS eBGP neighbor pair 在 `same-node` 和 `cross-node` 下的差异；
- default scheduler 是否因为随机切分 L2/AS 结构造成更高尾延迟。

### iperf3 指标

对每个 pair 使用目标 router 启动一次性 server：

```text
iperf3 -s -1 -J -p <port>
```

源 router 执行 TCP 测量：

```text
iperf3 -c <dst_seedemu_ip> -J -t <duration> -P 1 -p <port>
```

可选 UDP 测量用于抖动和丢包：

```text
iperf3 -u -b 100M -c <dst_seedemu_ip> -J -t 10 -p <port>
```

记录：

- TCP：sender/receiver goodput、retransmits、cwnd、duration、是否失败；
- UDP：jitter、lostPercent、outOfOrder、receiver bitrate；
- 双向性：至少抽样执行 reverse direction；高价值 pair 可执行 `--bidir`，但主指标
  不建议默认双向并发，以免测量本身造成强干扰；
- fairness：并发多流场景下计算 Jain fairness index，作为额外压力测试指标。

iperf 分成两类结果解释：

- baseline iperf：低并发，用于比较单流路径质量和不同 topology stratum 的基础
  throughput。
- stress iperf：高并发，用于比较 placement 在共享 fabric、跨 node 流量和热点节点
  下的 aggregate throughput、loaded latency、failure rate 和 fairness。

两类结果必须分开聚合，不能把 stress round 的结果混进 baseline median。主结论可以
分别报告“低干扰路径性能”和“高并发压力性能”。

`aggregate throughput` 不能单独作为 placement 排序依据。1078 第一轮结果显示，某些
baseline 会因为少数 same-node/eBGP flow 极快而获得更高 raw aggregate Mbps，但同时
loaded RTT、loss 或 Jain fairness 明显变差。因此主结论按下面优先级解释：

1. `loadedPingAvgRttMs.p95/avg` 与 `loadedPingLossPct`；
2. `fairness` 与 `flowMbps.p10/p25/median`；
3. `qualityScore = aggregateGoodputMbps * fairness * (1 - lossPct/100) / loadedRttMs`；
4. 分组结果：`same-node/cross-node`、`intra_as_link/ebgp_ix_peer/cross_as_multihop`；
5. 最后才看 raw `aggregateGoodputMbps`。

### 新增动态指标

建议新增 `latency_throughput_report.json`，并在 `placement_report.json` 中加入摘要
引用。该报告包含：

- `pairPlan`：pair ID、src/dst router、ASN、router ID、目标 SeedEMU IP、拓扑标签。
- `placementLabels`：每个 pair 的实际 K3s node、`same-node/cross-node`、
  VLAN/fabric 标签。
- `pingResults`：每个方向的 RTT/loss 明细和聚合。
- `iperfResults`：TCP/UDP goodput、jitter、loss、retransmit。
- `stressResults`：每个 concurrency level/repetition 的并发 goodput、loaded ping、
  failure rate、start skew 和 active endpoint 分布。
- `aggregateByStratum`：按 `nodeRelation`、`asRelation`、`topologyRelation`、
  `algorithm` 聚合的 median/p95。
- `nodeLoadDuringTest`：测量期间每个 K3s node 的 CPU/memory/load average。
- `hostInterfaceDelta`：测量前后 `ens2.<vlan>`、macvlan、bridge 统计差值，用于估算
  underlay bytes 和 dropped/error packets。

可以从这些原始数据派生新的动态特征：

- `rttCrossNodePenalty = medianRTT(cross-node) - medianRTT(same-node)`。
- `throughputCrossNodePenalty = medianGoodput(same-node) / medianGoodput(cross-node)`。
- `tailLatencyRatio = p95RTT / medianRTT`。
- `underlayAmplification = hostVlanBytesDelta / iperfPayloadBytes`。
- `tcpRetransmitRate = retransmits / transferredMB`。
- `lossOrTimeoutRate`：ping/UDP 丢包和执行失败的统一比例。
- `pathAsLength`：从 `birdc show route <dst>` 中解析 AS_PATH 长度，辅助解释同一
  placement 下不同拓扑 pair 的差异。
- `stressAggregateGoodputMbps`：同一 stress round 内所有成功 TCP 流的 goodput 求和。
- `stressPerFlowFairness`：同一 stress round 内 TCP goodput 的 Jain fairness index。
- `loadedRttInflation = medianRTT(during_stress) / medianRTT(before_stress)`。
- `stressFailureRate`：同一并发层级下 iperf 启动失败、超时或 TCP 连接失败比例。
- `nodeHotspotScore`：stress round 中每个 K3s node active endpoint 数和接口 bytes
  的最大值/均值比，用于解释 placement 是否制造热点。

## Baseline

建议至少比较以下 baseline：

- `optimized`：b63 默认，网络割裂和资源均衡共同优化。
- `network-only`：只优化网络割裂，验证网络目标本身的贡献。
- `resource-only`：只优化 CPU/memory，验证资源均衡与网络代价的冲突。
- `pod-count-balanced`：只均衡 Pod 数，作为最简单工程 baseline。
- `hypergraph`：按网络 hyperedge affinity 的泛化 baseline。
- `kubernetes-default-scheduler`：静态报告 baseline 以及独立动态 baseline。动态
  baseline 不走 b63 per-node image preload 流程，而是用
  `runDefaultSchedulerBaseline.py` 复制 compile output、移除 hard placement 和
  `resources.requests`，再调用 `k8sTools.py up` 让 Kubernetes 自行调度。之后复用
  BIRD/FIB 验证脚本补齐 routing 指标。
- `b62-by-as-hard`：历史 by-AS hard placement，可通过 b62 run 的
  `placement_plan.json`/`summary.json` 做横向对比，或后续在 b63 placement 引擎中
  增加 legacy by-AS 算法。

## 实验矩阵

推荐矩阵：

- 规模：`1078`、`1897`、`3083`；3083 以上必须先验证 BIRD/FIB 收敛能力，再扩展
  baseline 动态对比。
- 算法：`optimized`、`network-only`、`resource-only`、`pod-count-balanced`、
  `hypergraph`。
- network cost mode：先用 `ratio`，再抽样对比 `pair` 和 `endpoint-exposure`。
- 每个组合至少重复 3 次，比较 deploy ready time 和 routing 验证稳定性。
- 时延/吞吐：每个算法使用相同 canonical pair set；每个 pair 做 ping 和 TCP iperf，
  抽样做 UDP iperf；测试顺序随机但 seed 固定。

每次 run 目录必须包含：

```text
summary.json
output/placement_report.json
output/network_weights.yaml
deploy.log
wait-ready.log
start_bird_summary.json
verify_after_start_bird_summary.json
start_bird_kernel_summary.json
verify_after_fib_write_summary.json
latency_throughput_report.json
loadAverage.log
```

如果 routing gate 在 `verify-after-start-bird` 或 `start-kernel` 失败，后续
`start_bird_kernel_summary.json` / `verify_after_fib_write_summary.json` 可以不存在，但
必须在 `summary.json` 和 `placement_report.json.routingValidation` 中显式记录
`status: NOT_RUN`、失败阶段和原因。3083 第一轮按这个规则记录。

控制变量：

- 同一规模必须使用同一 topology、assignment、compiler image、CNI、cluster
  config、Kubernetes 版本和 `configkvm_b63.yaml`。
- 每次只部署一个算法的 workload；default scheduler baseline 需先清理 optimized
  workload，或使用独立 b63 cluster。
- 所有算法默认不写 Kubernetes `resources.requests`，除非专门做 scheduler resource
  constraint 实验。
- 测量前必须确认 Pod Ready、BIRD 协议健康、FIB 验证通过。
- pair plan、随机 seed、iperf 时长、并发度、测试顺序、post-deploy 等待时间在所有
  算法之间保持一致。
- 每轮测量前后采集 node load 和接口计数器；如果某次测量期间节点 CPU/load 明显异常，
  标记为 contaminated sample，不直接进入主结论。

## 0625 大规模第一轮结果

资源配置参照 B62 `assignment.yaml` 的 `300 vCPU / 600 GiB / 80 GB disk` 预算。b63
实际 cluster 是 1 master + 5 workers：master `48 vCPU/96 GiB/80 GB`，每个 worker
`50 vCPU/100 GiB/80 GB`，总计 `298 vCPU/596 GiB`，默认只把 5 个 worker 用于
workload placement。

`1897` optimized 在 `experiments/1897_20260625_113126/` 完整通过：

- Pod 实际分布为 worker1-5 `379/379/379/380/380`，control-plane 上为 0。
- 静态 optimized objective `15.82`，VLAN parent total `2427`；对应
  `pod-count-balanced`/`kubernetes-default-scheduler` 静态 baseline 的 VLAN parent
  total 约 `3935/3933`，说明优化目标明显减少 macvlan+VLAN fan-out。
- Routing 阶段 `start-bird`、`verify-after-start-bird`、`start-kernel`、
  `verify-after-fib-write` 都通过。
- Dataplane v2 采集 96 pair/192 direction，其中 cross-node 148、same-node 44；
  ping 成功 `155/192`，baseline TCP 成功 `39/48`。stress `8/16/32/48/64` 并发
  均已采集，可用于后续与 baseline 的同 pair plan 对比。

`3083` optimized 在 `experiments/3083_20260625_122845/` 部署层通过，但 routing 层未
通过：

- Pod 实际分布为 worker1-5 `617/617/616/617/616`，control-plane 上为 0。
- 静态 optimized objective `15.65`，VLAN parent total `3696`；静态 baseline 中
  `pod-count-balanced`/`kubernetes-default-scheduler` 为 `6259/6249`，`resource-only`
  为 `5397`，说明 placement 仍显著降低 VLAN fan-out。
- `start-bird` 续跑确认 `3083/3083` 个 BIRD 进程存在，但
  `verify-after-start-bird` 在 10 个采样 AS 上失败，主要表现为 iBGP RR session
  停在 `Passive/OpenConfirm`、连接超时或 no-route。`start-kernel` 两次尝试未完成，
  因此没有 `verify-after-fib-write`。
- Dataplane 仍已采集，用于诊断：ping 成功 `88/192`、failure rate `54.17%`；
  baseline TCP iperf 成功 `15/48`、failure rate `68.75%`。这些 failure rate 主要
  反映 routing/FIB 未健康，不应作为干净的 placement dataplane 性能结论。

这轮结果对实验方案的影响：

- 1897 可以作为当前资源配置下的“完整可用规模”，适合继续跑 optimized 与 baseline 的
  动态对比。
- 3083 适合先做 routing 收敛专项优化；在 BIRD/FIB gate 未通过前，dataplane 指标
  只能用于解释可达性和控制面压力，主排序应暂停。
- Routing load gate 不能再使用固定 `80` 作为硬等待条件。大规模下应显式设置
  `BIRD_LOAD_THRESHOLD`、`VERIFY_START_BIRD_LOAD_THRESHOLD`、
  `KERNEL_LOAD_THRESHOLD`、`VERIFY_FIB_LOAD_THRESHOLD`，并使用有限
  `*_LOAD_MAX_WAIT_SECONDS` 记录高负载后继续验证，避免流程永久卡住。

## 0625 12VM/Master 参与后的 3083 复跑

按后续资源约束，b63 cluster 已改成 12 VM：1 master + 11 workers，每台
`25 vCPU / 50 GiB / 80 GB disk`，总计 `300 vCPU / 600 GiB`。master 默认参与
workload placement，只有显式传 `--exclude-master` 才排除 control-plane node。

`examples/kubernetes/real_topology_k3s_compile.py` 同时通过 SeedEMU API 固定协议参数：

- OSPF：`hello=10`、`dead=40`、`tick=3`。
- iBGP/eBGP：`holdTime=36000`、`keepaliveTime=60`。

在该配置下，`3083` optimized 于 `experiments/3083_20260625_183501/` 完整通过：

- 完整阶段 `clean/compile/placement/preflight/build/deploy/wait-ready/start-bird/
  verify-after-start-bird/start-kernel/verify-after-fib-write` 全部 `PASS`。
- Pod 动态状态为 `3083/3083` Running/Ready；节点分布为 master 和 worker1-10 各
  `257`，worker11 为 `256`。
- placement 默认仍不注入 Kubernetes `resources.requests`，只写 hard placement。
- 静态 optimized VLAN parent total 为 `3962`；同一静态报告中
  `pod-count-balanced` 为 `6708`，`kubernetes-default-scheduler` 为 `6722`，
  `resource-only` 为 `5674`。这说明 12VM 下优化目标仍显著降低 macvlan+VLAN
  fan-out。
- `verify-after-start-bird` 抽样 `10/10` 个 AS representatives 通过；`start-kernel`
  将 `3083/3083` 个 router 目标写入 Linux FIB；`verify-after-fib-write` 抽样
  `10/10` 个 AS representatives 通过。
- 已在同一 artifact 下补跑 dataplane v2，并复用
  `pair_plan_3083_9f6e118305f37617_seed20260624_v2.json`：ping `156/192`
  direction 成功，overall median RTT `0.2495 ms`，cross-node median RTT
  `0.316 ms`；baseline TCP iperf `36/48` direction 成功，aggregate goodput
  `273617 Mbps`、Jain fairness `0.884`。stress `8/16/32/48/64` 并发各完成
  3 轮，平均 aggregate goodput 分别约 `36.8/67.7/102.2/136.8/177.3 Gbps`，
  平均 loaded RTT 分别约 `2.35/4.00/6.69/10.29/12.94 ms`。
- 成功 run 使用的 routing load gate 为：`BIRD_LOAD_THRESHOLD=200`，
  `VERIFY_START_BIRD_LOAD_THRESHOLD=250`，`KERNEL_LOAD_THRESHOLD=250`，
  `VERIFY_FIB_LOAD_THRESHOLD=250`，各阶段 `*_LOAD_MAX_WAIT_SECONDS=30`。

该复跑改变了 3083 的实验状态：5-worker 第一轮的 dataplane 失败数据只能作为
routing 未健康时的诊断样本；后续 3083 的 placement 性能比较应以
`3083_20260625_183501` 这类 BIRD/FIB gate 全通过的 run 为基础，再复用同一
deterministic pair plan 测 ping/iperf。

## 判定标准

一个 placement 方案有效，需要同时满足：

- 静态上优于 baseline：更低的 network cut、VLAN parent fan-out 或更低的资源倾斜；
- 动态上不牺牲可部署性：Ready time 降低或不变，Failed/NotReady Pod 更少；
- 路由层正确：BIRD 协议验证和 FIB route-count 验证通过；
- dataplane 性能更好：同一 canonical pair set 下，RTT median/p95 更低或不恶化，
  TCP/UDP goodput 更高或不恶化，loss/jitter/retransmit 更少；
- 跨节点惩罚更小：same-AS、eBGP-neighbor、multi-hop strata 中的
  `rttCrossNodePenalty`、`throughputCrossNodePenalty` 和 `underlayAmplification`
  不高于 default scheduler；
- 稳定性更好：节点 CPU/memory peak、接口吞吐峰值和 API timeout 数量没有恶化；
- 可复现：相同规模和算法多次 run 的结论一致。
