# E1-B Pod 放置数量与通信/性能取舍

本次任务是把 E1-B 正式实验结果整理成一张 tradeoff 图：横坐标为目标 worker VM 上放置的 SEED Pod 数，纵向分别展示跨机带宽减少量、UDP 交付率和背景任务按时处理速度。图标题中标明了实验资源配置：4 个 worker VM，每个 worker 为 4 vCPU / 8 GiB，拓扑规模为 214 Pod。

图文件：

- [e1b_pod_placement_tradeoff.png](experiments/e1_marginal_pods/20260920_tradeoff_plot/e1b_pod_placement_tradeoff.png)
- [e1b_pod_placement_tradeoff.pdf](experiments/e1_marginal_pods/20260920_tradeoff_plot/e1b_pod_placement_tradeoff.pdf)
- [e1b_pod_placement_tradeoff.csv](experiments/e1_marginal_pods/20260920_tradeoff_plot/e1b_pod_placement_tradeoff.csv)

数据来自 E1-B formal2 主序列 `pi00 → pi04`。`matched-count-target054-low-locality` 是同 Pod 数控制点，不放入这张单调 Pod 数曲线。跨机带宽减少量按冻结的 16 条固定流计算，每条流方向的 offered payload 为 25 Mbps，因此从 16 条跨 worker 方向降到 4 条时，跨机 offered bandwidth 减少 300 Mbps。

| 映射 | 目标 VM Pod 数 | 跨机带宽减少 | UDP 交付率 | 按时任务处理速度 |
|---|---:|---:|---:|---:|
| `pi00` | 44 | 0 Mbps | 0.840 | 2136.9 jobs/s |
| `pi01` | 54 | 75 Mbps | 0.772 | 2138.6 jobs/s |
| `pi02` | 64 | 150 Mbps | 0.534 | 2128.4 jobs/s |
| `pi03` | 74 | 225 Mbps | 0.365 | 1850.7 jobs/s |
| `pi04` | 84 | 300 Mbps | 0.437 | 1303.0 jobs/s |

结果说明：随着更多 Pod 被共置到目标 VM，固定流的跨机通信持续减少，通信局部性收益是明确存在的；但 UDP 交付率从 0.840 降到 0.365–0.437，背景任务按时处理速度也从约 2137 jobs/s 降到约 1303 jobs/s。也就是说，继续共置可以节省跨机带宽，但当目标 VM 上的 Pod 数超过约 64–74 这个区间后，资源争用开始主导整体服务质量。

图中阴影区标出 `pi02 → pi03` 的转折区域：跨机带宽仍在减少，但任务按时处理速度开始明显下降，UDP 交付率也进入低位。这支持我们的 motivation：放置算法不能只最小化切边或跨机带宽，还需要估计一次共置带来的边际资源争用代价。

注意：`pi04` 的 formal UDP workload 只有 4 个有效重复，图中任务处理速度按这 4 个有效点计算；无效重复没有被填补或当作有效结果。
