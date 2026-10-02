# B63 面向性能的全局放置实验

这套实验把 Pod-to-Node 映射作为唯一自变量。每个变体都重新创建隔离集群，使用相同的
SeedEMU 拓扑、镜像、容器启动参数、路由配置、K3s/CNI 和数据面采样计划。所有仿真
容器同时在线，路由器仍逐跳转发；映射在部署前写入 manifest，运行期间不迁移 Pod。

代码正确性与研究假设分别验收。流程 PASS 只表示预声明步骤完成；没有观察到内部拐点、
模型没有优于基线、流量测试失败或节点在压力下停顿，都保留为实验结果。

## 一键入口

在本目录和 `seedpy310` 环境中执行：

```bash
conda activate seedpy310

# E1：214 Pod、4 个小资源 VM，生成映射曲线和标定数据。
python -u runPerformanceStudy.py \
  --config research_configs/configE1Topology214FourVm.yaml \
  --run-dir experiments/research_e1_topology214

# E3 小规模独立验证：1078 Pod、4 VM；训练数据仍只来自 E1。
python -u runPerformanceStudy.py \
  --config research_configs/configE3Topology1078FourVm.yaml \
  --run-dir experiments/research_e3_topology1078

# E3 大规模独立验证：4954 Pod、48 VM。
python -u runPerformanceStudy.py \
  --config research_configs/configE3Topology4954FortyEightVm.yaml \
  --run-dir experiments/research_e3_topology4954_48vm
```

运行目录必须是新的目录。入口串行运行变体，每个变体使用新建集群并在结束后销毁。
`study.continueAfterVariantFailure: true` 时，某个映射的真实停顿会记为 FAIL，清理其隔离
集群后仍继续其余预声明变体；批次最终状态仍为 FAIL，不会把失败改写成成功。

## E1：动机实验

E1 配置冻结五个映射：`locality-only`、三个不同静态争用权重的
`tradeoff-static` 和 `capacity-only`。映射固定后，在同一个集群内依次施加
0/10/25/50 millicores/Pod 的外生 CPU 工作量。每个负载点重新检查 BIRD 协议和 Linux
FIB，再对固定逻辑方向测量普通 RTT、基线 TCP、4/8/16 并发 TCP、负载下 RTT、失败率
和 Jain 公平性。

`locality-only` 是确定性的顺序贪心启发式，并不声称求得全局最小切边。E1 的机械拐点
判定按跨机加权端点对比例排序；只有性能惩罚最小值位于两个端点之间时才记为内部拐点。
曲线存在不代表必然有拐点。

## E2：轻量模型与部署产物

每个观测点的性能惩罚定义为：

```text
log(负载 RTT) - log(stress 聚合有效吞吐)
  - 2 × log(max(1 - TCP 失败率, 10^-6))
```

测量脚本只能从成功探针得到 RTT、从完成流得到吞吐，因此用成功概率同时放大有效 RTT、
缩小有效吞吐。这样不会因大量流失败后只剩一个低 RTT 样本而得到更优分数。原始 RTT、
吞吐和失败率始终单列保留；综合惩罚只用于预声明模型和机械比较。

部署前特征包括：

- 固定拓扑在候选映射下的跨机加权端点对比例；
- 每个 Pod 的拓扑角色和网络连接；
- VM 的 CPU、内存和实时 `status.allocatable.pods`；
- 配置的外生 CPU 工作量；
- 超过 contention knee 后的 CPU/内存二次惩罚和。

`fitPerformanceModel.py` 仅使用配置中 `calibrationLoadsMillicores` 指定的 E1 样本拟合带
极小 ridge 项的线性模型。holdout 负载不参与拟合。模型文件保存训练行、输入哈希、
系数、R² 和训练拓扑规模；E3 配置在运行前固定模型路径及哈希。

`performance-model` 用拟合系数评价每次静态放置的通信增量和争用增量。CPU 是可超配的
性能信号，不是拒绝必要容器的硬上限。硬准入只检查 live kubelet Pod 容量、现存系统
Pod 占用、内存安全和配置 trunk 上真实 VLAN ID 容量，没有额外的人为统一 Pod 上限。

每个变体输出可审核的部署材料：

```text
output/placement_report.json       # 完整 Pod-to-Node 映射、特征和准入证据
output/k8s.yaml                    # 已写入静态落点的可部署 manifest
node_image_refs/*.txt              # 按最终落点生成的逐节点镜像预置清单
node_image_refs/summary.json       # 预置清单计数及缺失 selector 检查
placement_static_summary.json      # manifest、静态映射和重算指标门控
placement_actual_summary.json      # 部署后的逐 Pod 实际落点/Ready 门控
```

## E3：独立验证和基线

E3 不重新拟合模型，比较以下三个部署前映射：

- `crescent`：依据加权拓扑边的确定性指数竞赛顺序合并连通分量，再按内存和真实准入
  映射到主机；多接入网络使用等总权重的环投影。
- `crescent-resource-aware`：使用同一合并框架，在分量生成和主机映射时加入 CPU 需求
  信号；CPU 不作为硬拒绝条件。
- `performance-model`：使用 E1 冻结模型联合评价通信和预测争用。

前两个是对 Crescent 思路的 SeedEMU/Kubernetes **适配基线**，不是 Crescent 官方源码
复现。参考：[NSDI 2024 论文页](https://www.usenix.org/conference/nsdi24/presentation/gao-zhaoyu)、
[论文 PDF](https://anubhavnidhi.github.io/papers/nsdi24-gao-zhaoyu.pdf)。

所有变体复用相同 pair-plan 版本、拓扑哈希、随机种子和逻辑方向。分析器用
`sampleSignature` 检查样本；只有 `MATCH` 的行才适合逐方向因果比较。主要产物为：

```text
study_runs.json                    # 冻结协议、代码哈希、每轮状态和清理状态
implementation_snapshot/           # 本批次实际使用的全部实现文件及哈希清单
host_environment_before.json        # 物理机容量、负载、存储及已有 libvirt 资源
host_environment_after.json         # 全部变体和清理结束后的同口径快照
study_metrics.csv                  # 每个映射/负载点的静态和动态指标
study_analysis.json                # 拐点、预测误差和独立验证机械判定
study_report.md                    # 人可读结果表
locality_contention_curve.png/.svg
load_response_curve.png/.svg
```

负载注入程序在每个现有 Pod 的主容器内运行固定 duty-cycle Python 工作线程，不重启
容器。`load_application.json` 对每个 Pod 记录成功、失败、节点和重试；
`load_application_host_metrics.json` 从后续 4954 批次起覆盖负载注入阶段的节点 CPU、
内存和 fabric 计数。负载应用失败时也会立即写出 `summary.partial.json`。

## 当前已完成结果

`experiments/research_e1_topology214_20260918` 的五个变体全部完成，固定方向签名
`MATCH`。机械判定在 0/10/25/50m 四个负载点均未观察到内部拐点。标定模型的 R² 为
0.32675，通信系数为 0.98107，争用系数为 -1.39903；这个负争用系数和较弱拟合必须视为
E1 对当前轻量模型的反证信号，不能解释成争用带来普遍收益。

对应图表：

- `experiments/research_e1_topology214_20260918/locality_contention_curve.png`
- `experiments/research_e1_topology214_20260918/load_response_curve.png`
- `experiments/research_e1_topology214_20260918/study_report.md`
- `experiments/research_e1_topology214_20260918/performance_model.json`

1078 和 4954 的最终结论以各自 `study_runs.json`、`study_analysis.json` 和原始节点日志为准；
本文档不会因期望的研究方向而覆盖自动报告。
