# 2026-06-29 B63 Placement Experiment Results

本文汇总 b63 deployment 已收集到的 placement 实验结果，重点比较 `optimized` placement 相比 `network-only`、`resource-only`、`pod-count-balanced`、`hypergraph` 和 `kubernetes-default-scheduler` 的静态指标与动态 ping/iperf 指标。

## 数据范围

静态评估使用每个规模下可用的代表性 `optimized` run 的 `output/placement_report.json`。这些 report 都包含同一拓扑下多个算法的 `staticValidation`，因此可以直接比较各算法的静态目标函数和 macvlan/vlan 相关指标。

动态评估只使用实际部署并完成 dataplane 测量的 run。1078 规模有多算法动态实测；4954 规模有 `optimized` 与 `kubernetes-default-scheduler` 动态实测；7001 和 9955 目前只有 `optimized` 动态实测，default scheduler 的对应 run 未完整通过 BIRD/FIB/dataplane 阶段。

| 规模 | 代表性 optimized run | 静态评估 | 动态评估 | 备注 |
|---:|---|---|---|---|
| 1078 | `1078_20260624_161753` | PASS | PASS | 同规模另有 network-only/resource-only/pod-count/hypergraph/default 动态实测 |
| 1897 | `1897_20260625_113126` | PASS | PASS | 仅 optimized 动态有效 |
| 3083 | `3083_20260625_183501` | PASS | PASS | 排除较早失败 run `3083_20260625_122845` |
| 4954 | `4954_20260627_154613_12vm_optimized` | PASS | PASS | default scheduler 动态 run `4954_20260627_185334_12vm_kubernetes-default-scheduler_registry_pull` PASS |
| 7001 | `7001_20260628_153715_12vm_optimized` | PASS | PASS | default scheduler run 在 `verify-after-start-bird` 前后失败，无有效 dataplane |
| 9955 | `9955_20260629_055258_36vm_optimized_full` | PASS | PASS | 36VM 跑通；12VM/18VM run 未完整通过 dataplane |

所有代表性 optimized report 的 `resourceRequestsInjected=false`，因此这些结果对应“默认不注入 Pod CPU/memory requests，只做 placement 分区”的设计。

## 指标解释

静态指标中，越低越好：

| 指标 | 含义 |
|---|---|
| objective | b63 综合目标函数，包含 network、resource、pod pressure、VLAN parent pressure 等组件 |
| networkCostRatio | 归一化网络代价，越接近 0 表示跨节点/跨网络暴露越少 |
| span | `spanningNetworkCount`，跨多个 K8s node 的 SeedEMU 网络数量 |
| VLAN total | 所有 K8s node 上需要创建/承载的 macvlan/vlan parent 总数 |
| VLAN max/node | 单个 K8s node 上最大的 VLAN parent 数，反映最坏节点压力 |
| fanout | `weightedSpanningFanout`，加权跨节点网络 fanout |

动态指标中，goodput 和 fairness 越高越好；RTT、failure rate 和 packet loss 越低越好。

## Optimized 静态绝对指标

| 规模 | run | objective | networkCostRatio | span | VLAN total | VLAN max/node | fanout |
|---:|---|---:|---:|---:|---:|---:|---:|
| 1078 | `1078_20260624_161753` | 30.59 | 0.157 | 292 | 1357 | 526 | 584 |
| 1897 | `1897_20260625_113126` | 15.82 | 0.199 | 468 | 2427 | 541 | 2662 |
| 3083 | `3083_20260625_183501` | 16.01 | 0.189 | 646 | 3962 | 353 | 5254 |
| 4954 | `4954_20260627_154613_12vm_optimized` | 17.20 | 0.169 | 945 | 6090 | 564 | 6830 |
| 7001 | `7001_20260628_153715_12vm_optimized` | 18.02 | 0.145 | 1215 | 8482 | 740 | 9212 |
| 9955 | `9955_20260629_055258_36vm_optimized_full` | 21.21 | 0.192 | 1767 | 12628 | 404 | 21226 |

结论：随着规模从 1078 增长到 9955，`optimized` 的 VLAN total 和 fanout 会随 Pod/网络规模上升，但 networkCostRatio 始终维持在约 `0.145-0.199`。这说明分区部署在更大规模下仍然能避免 default scheduler 那种接近全局扩散的网络放置。

## Optimized 相比各方法的静态降幅

下表中的百分比表示 `optimized` 相比对应方法降低了多少；负数表示该单项指标上 `optimized` 更高。注意，单项 fanout 更低不一定代表算法整体更好，尤其在 macvlan/vlan 下还必须同时控制单节点 VLAN parent 峰值和负载压力。

| 规模 | 对比方法 | objective 降低 | ratio 降低 | span 降低 | VLAN total 降低 | VLAN max 降低 | fanout 降低 |
|---:|---|---:|---:|---:|---:|---:|---:|
| 1078 | network-only | 2.6% | -36.5% | -28.6% | -3.8% | -19.3% | 32.7% |
| 1078 | resource-only | 47.4% | 75.8% | 66.4% | 32.1% | 36.0% | 84.2% |
| 1078 | pod-count-balanced | 61.3% | 79.1% | 71.3% | 37.1% | 27.2% | 85.9% |
| 1078 | hypergraph | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% |
| 1078 | kubernetes-default-scheduler | 53.8% | 77.5% | 69.9% | 35.3% | 38.6% | 84.9% |
| 1897 | network-only | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% |
| 1897 | resource-only | 72.3% | 59.0% | 64.7% | 28.0% | 21.4% | 49.9% |
| 1897 | pod-count-balanced | 84.0% | 75.9% | 74.4% | 38.3% | 31.9% | 65.9% |
| 1897 | hypergraph | 86.1% | 29.5% | 42.8% | 9.9% | 32.8% | -62.7% |
| 1897 | kubernetes-default-scheduler | 83.9% | 75.9% | 74.4% | 38.3% | 31.8% | 65.8% |
| 3083 | network-only | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% |
| 3083 | resource-only | 88.5% | 64.6% | 70.2% | 30.2% | 26.8% | 48.9% |
| 3083 | pod-count-balanced | 93.6% | 79.5% | 78.5% | 40.9% | 37.7% | 65.7% |
| 3083 | hypergraph | 97.7% | 27.0% | 42.5% | 4.4% | 79.4% | -133.9% |
| 3083 | kubernetes-default-scheduler | 93.6% | 79.4% | 78.5% | 41.1% | 37.7% | 66.0% |
| 4954 | network-only | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% |
| 4954 | resource-only | 86.9% | 70.2% | 72.4% | 30.9% | 24.0% | 53.4% |
| 4954 | pod-count-balanced | 92.8% | 81.8% | 80.1% | 41.3% | 35.5% | 67.9% |
| 4954 | hypergraph | 97.1% | 61.2% | 66.2% | 19.4% | 78.9% | -22.2% |
| 4954 | kubernetes-default-scheduler | 92.8% | 81.8% | 80.1% | 41.4% | 35.9% | 68.3% |
| 7001 | network-only | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% |
| 7001 | resource-only | 85.6% | 73.4% | 74.5% | 30.8% | 28.2% | 52.5% |
| 7001 | pod-count-balanced | 92.2% | 84.5% | 81.8% | 41.3% | 39.0% | 68.2% |
| 7001 | hypergraph | 95.1% | 69.7% | 72.6% | 23.8% | 70.9% | -4.0% |
| 7001 | kubernetes-default-scheduler | 92.2% | 84.5% | 81.8% | 41.3% | 39.1% | 68.1% |
| 9955 | network-only | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% | 0.0% |
| 9955 | resource-only | 95.3% | 71.1% | 74.2% | 30.6% | 31.2% | 44.0% |
| 9955 | pod-count-balanced | 97.4% | 80.2% | 81.0% | 41.1% | 33.4% | 59.4% |
| 9955 | hypergraph | 98.9% | 64.5% | 73.7% | 21.3% | 88.6% | -58.1% |
| 9955 | kubernetes-default-scheduler | 97.3% | 80.3% | 81.1% | 41.0% | 37.1% | 59.3% |

### 静态结果分析

1. 相比 default scheduler，`optimized` 在所有规模上都显著降低网络扩散。`networkCostRatio` 降幅稳定在 `75.9%-84.5%`，`span` 降幅从 `69.9%` 增长到约 `81%`，`VLAN total` 在 3083 以上规模稳定降低约 `41%`。

2. `pod-count-balanced` 与 default scheduler 的静态行为非常接近。它能让 Pod 数量更均匀，但会把同一 SeedEMU 网络扩散到更多 K8s node，导致 macvlan/vlan parent 数和跨节点网络数很高。

3. `resource-only` 能改善资源均衡，但没有主动控制网络局部性。它比 default scheduler 好一些，但在 4954、7001、9955 上的 `networkCostRatio` 仍分别为 `0.569`、`0.544`、`0.665`，明显高于 optimized 的 `0.169`、`0.145`、`0.192`。

4. `hypergraph` 在部分规模上 fanout 单项更低，所以表里 fanout 降幅出现负数；但它会造成非常严重的单节点 VLAN parent 峰值。例如 9955 规模下 hypergraph 的 `VLAN max/node=3554`，而 optimized 只有 `404`。在 macvlan/vlan CNI 下，这种单节点 VLAN parent 压力是实际部署风险，因此 hypergraph 不能只看 fanout。

5. 1897 及以上规模里，`optimized` 与 `network-only` 的静态结果完全一致。这说明在当前“不注入资源 requests”的实验设定下，网络局部性最优解本身已经满足资源/Pod 压力约束；如果后续显式打开资源约束，`optimized` 可能会与 `network-only` 拉开差异。

## 动态实测: 1078 多算法对比

1078 是目前唯一完成多种 placement 方法实际部署和 dataplane 测量的规模。该轮 stress 最高并发为 48，尚未包含后续新增的 64 并发项。

| 方法 | ping ok/total | ping loss avg | RTT avg ms | TCP ok/total | TCP agg Gbps | TCP fairness | stress48 Gbps | fairness48 | loaded RTT48 ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| optimized | 72/72 | 0.00% | 0.450 | 24/24 | 213.48 | 0.820 | 136.59 | 0.494 | 10.580 |
| network-only | 72/72 | 0.00% | 0.495 | 24/24 | 132.93 | 0.681 | 145.02 | 0.301 | 20.526 |
| resource-only | 72/72 | 0.00% | 0.899 | 24/24 | 133.70 | 0.612 | 180.10 | 0.280 | 42.215 |
| pod-count-balanced | 72/72 | 0.00% | 1.066 | 24/24 | 92.59 | 0.582 | 60.73 | 0.185 | 55.879 |
| hypergraph | 72/72 | 0.00% | 0.488 | 24/24 | 204.99 | 0.805 | 128.30 | 0.452 | 14.255 |
| kubernetes-default-scheduler | 72/72 | 0.00% | 0.985 | 24/24 | 128.91 | 0.601 | 124.60 | 0.221 | 35.925 |

1078 的动态结果说明：`optimized` 不一定在每个单项 goodput 上最高。例如 `resource-only` 的 stress48 goodput 更高，但 fairness 只有 `0.280`，loaded RTT 达到 `42.215 ms`。`optimized` 的价值体现在更均衡的综合表现：baseline TCP goodput 最高、fairness 最高，并且 loaded RTT 明显低于 resource-only、pod-count-balanced 和 default scheduler。

## 动态实测: 4954 optimized 对比 default scheduler

4954 是目前较大规模里唯一同时跑通 optimized 和 default scheduler full-flow dataplane 的规模。

| 指标 | optimized | default scheduler | optimized 改善 |
|---|---:|---:|---:|
| ping ok/total | 151/192 | 151/192 | 相同 |
| ping avg RTT | 2.137 ms | 2.834 ms | 低 24.6% |
| ping p95 RTT | 1.575 ms | 4.524 ms | 低 65.2% |
| baseline TCP ok/total | 35/48 | 35/48 | 相同 |
| baseline TCP agg goodput | 356.02 Gbps | 281.27 Gbps | 高 26.6% |
| baseline TCP fairness | 0.946 | 0.777 | 高 21.7% |

| stress 并发 | optimized Gbps | default Gbps | goodput 提升 | optimized fairness | default fairness | optimized loaded RTT | default loaded RTT |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 8 | 38.89 | 25.70 | 51.3% | 0.936 | 0.673 | 0.452 ms | 2.117 ms |
| 16 | 94.69 | 68.84 | 37.6% | 0.840 | 0.675 | 0.750 ms | 3.156 ms |
| 32 | 151.85 | 87.41 | 73.7% | 0.745 | 0.530 | 1.113 ms | 6.891 ms |
| 48 | 194.70 | 115.29 | 68.9% | 0.573 | 0.488 | 1.856 ms | 9.165 ms |
| 64 | 232.92 | 121.49 | 91.7% | 0.536 | 0.441 | 1.950 ms | 11.297 ms |

4954 的动态结果最直观地支持 placement 优化有效：同样 deterministic pair plan 下，optimized 在所有 stress 并发上 goodput 更高、fairness 更高、loaded RTT 更低。64 并发时 goodput 提升 `91.7%`，loaded RTT 从 `11.297 ms` 降到 `1.950 ms`。

## 动态实测: optimized 随规模变化

| 规模 | run | ping ok/total | ping fail | loss avg | RTT avg ms | TCP ok/total | TCP agg Gbps | TCP fairness | 最高 stress | stress Gbps | stress fairness | loaded RTT ms |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1078 | `1078_20260624_161753` | 72/72 | 0.000 | 0.00% | 0.450 | 24/24 | 213.48 | 0.820 | 48 | 136.59 | 0.494 | 10.580 |
| 1897 | `1897_20260625_113126` | 155/192 | 0.193 | 19.27% | 0.440 | 39/48 | 306.92 | 0.909 | 64 | 156.41 | 0.299 | 17.515 |
| 3083 | `3083_20260625_183501` | 156/192 | 0.188 | 18.75% | 3.160 | 36/48 | 273.62 | 0.884 | 64 | 177.25 | 0.397 | 12.938 |
| 4954 | `4954_20260627_154613_12vm_optimized` | 151/192 | 0.214 | 21.35% | 2.137 | 35/48 | 356.02 | 0.946 | 64 | 232.92 | 0.536 | 1.950 |
| 7001 | `7001_20260628_153715_12vm_optimized` | 147/192 | 0.234 | 23.44% | 5.130 | 40/48 | 399.94 | 0.955 | 64 | 179.55 | 0.594 | 6.117 |
| 9955 | `9955_20260629_055258_36vm_optimized_full` | 144/192 | 0.250 | 25.21% | 1.982 | 35/48 | 328.30 | 0.975 | 64 | 303.50 | 0.776 | 2.143 |

注意：9955 使用 36VM 跑通，而 4954/7001 是 12VM 结果，因此跨规模动态指标同时受到 placement 和 VM 资源容量影响，不能简单当作单变量扩展曲线。更合理的解读是：在更大 VM 资源下，9955 optimized 能完整跑通 BIRD/FIB/dataplane，并在 64 并发 stress 下达到 `303.50 Gbps`、fairness `0.776`、loaded RTT `2.143 ms`。

## 全流程耗时

| 规模 | 方法 | run | status | compile | placement | build | deploy | start-bird | start-kernel | dataplane |
|---:|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| 1078 | optimized | `1078_20260624_161753` | PASS | 5.4s | 42.8s | 79.2s | 1280.4s | 83.8s | 174.4s | 未记录 |
| 1897 | optimized | `1897_20260625_113126` | PASS | 22.9s | 210.1s | 247.4s | 384.8s | 23.0s | 140.8s | 未记录 |
| 3083 | optimized | `3083_20260625_183501` | PASS | 125.5s | 133.9s | 244.5s | 699.4s | 51.5s | 383.6s | 未记录 |
| 4954 | optimized | `4954_20260627_154613_12vm_optimized` | PASS | 664.6s | 323.7s | 278.5s | 1074.5s | 196.3s | 649.4s | 1180.5s |
| 4954 | default scheduler | `4954_20260627_185334_12vm_kubernetes-default-scheduler_registry_pull` | PASS | 659.4s | 321.1s | 46283.1s | 3685.2s | 753.8s | 223.6s | 1176.5s |
| 7001 | optimized | `7001_20260628_153715_12vm_optimized` | PASS | 479.1s | 646.7s | 330.0s | 2042.5s | 179.9s | 905.7s | 1315.0s |
| 9955 | optimized | `9955_20260629_055258_36vm_optimized_full` | PASS | 948.8s | 3854.9s | 627.6s | 30813.3s | 36.7s | 1255.0s | 1587.0s |

4954 default scheduler 的 build 阶段耗时 `46283.1s`，主要来自早期 registry-pull build 流程；后续已经为 default scheduler 增加 all-node preload build 路径，因此这个耗时不应再作为 placement 算法本身的性能结论。

9955 optimized 的 deploy 阶段耗时 `30813.3s`，主要来自当前保守的 batch deploy 参数和压力检查。该阶段保证了最终稳定跑通，但后续如果要系统化跑矩阵，需要考虑增大 batch 或做自适应 batch。

## 失败/缺失数据说明

| 规模 | 方法 | run | 结果 | 对本文影响 |
|---:|---|---|---|---|
| 3083 | optimized | `3083_20260625_122845` | FAIL，虽有 dataplane 文件，但 start-kernel/verify-after-start-bird 未完整通过 | 不纳入主动态表 |
| 4954 | default scheduler | `4954_20260627_173908_12vm_kubernetes-default-scheduler`、`4954_20260627_181411_12vm_kubernetes-default-scheduler` | FAIL at build | 不纳入动态对比 |
| 7001 | default scheduler | `7001_20260628_173730_12vm_kubernetes-default-scheduler` | FAIL at verify-after-start-bird | 只有静态 report，无法做有效动态对比 |
| 9955 | optimized 12VM/18VM | `9955_20260628_205158_12vm_optimized`、`9955_20260629_025902_18vm_optimized_podcidr22fixed` | 未通过 start-BIRD 验证 | 不纳入动态对比 |
| 9955 | default scheduler | `9955_20260628_173550_12vm_kubernetes-default-scheduler` | 未形成有效 full-flow dataplane | 无法与 9955 optimized 做动态对比 |

## 总结

1. 静态层面，`optimized` 在所有规模上相对 default scheduler 都有稳定优势。大规模下最关键的 macvlan/vlan 指标改善很明显：3083、4954、7001、9955 的 VLAN total 均降低约 `41%`，networkCostRatio 降低约 `79%-85%`，span 降低约 `78%-82%`。

2. 动态层面，4954 的真实对照最有说服力：`optimized` 在 stress 8 到 64 并发下均显著高于 default scheduler，64 并发 goodput 提升 `91.7%`，loaded RTT 降低约 `82.7%`。这说明静态减少跨节点网络扩散后，实际 ping/iperf 指标也同步改善。

3. 1078 多算法动态对比显示，单纯追求某个 goodput 单项可能会误导结论。`resource-only` 的 stress48 goodput 最高，但 fairness 和 loaded RTT 很差；`optimized` 的优势是综合 goodput、fairness、RTT 和静态网络压力的平衡。

4. `hypergraph` 不能直接作为最终 baseline 替代方案。它在部分 fanout 指标上看起来更低，但会制造严重单节点 VLAN parent 峰值，尤其 9955 规模达到 `3554`，远高于 optimized 的 `404`。在 macvlan/vlan CNI 下，这会直接转化为节点侧网络对象压力和部署风险。

5. 9955/36VM 已经证明 optimized 能在更大规模下完成 full-flow，包括 compile、placement、build、deploy、start-BIRD、FIB write、验证和 ping/iperf dataplane。后续需要补齐 7001/9955 的 default scheduler 动态 baseline，才能在这两个规模上做严格的动态同规模对照。

