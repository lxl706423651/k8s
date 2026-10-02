# B63 性能感知放置研究：实验进展、阶段成果与当前问题

更新时间：2026-09-18 16:13 CST

本文记录 B63 研究实验的实际执行状态。文中的 **PASS** 只表示对应脚本、部署或检查按定义完成，不表示研究假设已经成立。失败、缺失指标和未观察到拐点均按原始结果保留。

## 1. 研究目标与不变量

研究问题是：能否在部署前预测一次节点共置决策的边际性能影响，从而保留有收益的通信局部性，并避开集中部署引起的宿主机争用？

所有实验保持以下条件：

- 全部仿真容器同时在线；
- 对比算法使用同一虚拟拓扑、协议语义和外生负载；
- 路由器仍执行真实逐跳转发；
- Pod-to-Node 映射在部署前静态生成，不执行在线 Pod 迁移；
- 不替换内核、CNI 或转发后端；
- 节点准入检查采用集群实时报告的 `allocatable.pods`，扣除已有系统 Pod；
- Pod 数均衡和 CPU 利用率方差只作为描述性指标，不作为最终研究目标；
- 算法失败或没有出现性能反转时，不修改结果来满足假设。

## 2. 总体状态

| 阶段 | 当前状态 | 已有成果 | 尚未完成 |
|---|---|---|---|
| E1：动机发现 | **E1-A 已完成并验证；E1-B 尚未执行** | 4 vCPU/8 GiB 单 VM 固定到达工作画像；150 个正式点；Loaded/Idle 对照、重复性过渡带和曲线 | 用 topology 214 固定全部仿真 Pod，验证跨 VM 通信需求下降与本地/全局争用代价的关系 |
| E2：建模与放置 | 原型代码完成，科学结论阻塞 | 可生成训练数据、岭回归模型、静态映射、镜像预置清单和部署清单 | 现模型使用了尚未通过 E1 验收的数据，只能作为工程原型，不能作为有效研究模型 |
| E3：独立验证 | 1078 探索运行已完成；正式 E3 暂停 | 已得到三个方案的一次探索性对比 | E1、E2 合格之前不能称为独立验证；4954/48VM 已中断并清理 |

## 3. 已实现的实验代码

- [`researchPlacement.py`](./researchPlacement.py)：实现 locality-only、capacity-only、tradeoff-static、Crescent 适配基线、Crescent 资源感知增强版和 performance-model 放置。
- [`runPlacementExperiment.py`](./runPlacementExperiment.py)：生成静态映射，并根据 Kubernetes 实时 `allocatable.pods` 做准入检查。
- [`runB63LargeExperiment.py`](./runB63LargeExperiment.py)：执行建群、编译、放置、镜像预置、部署、静态验证和动态数据面测量。
- [`runLoadSweep.py`](./runLoadSweep.py)：在映射固定的情况下执行多档 Pod CPU 压力和数据面测量。
- [`applyPodCpuLoadNodeLocal.py`](./applyPodCpuLoadNodeLocal.py)：向所有目标 Pod 注入可控的节点本地 CPU 负载。
- [`performanceStudy.py`](./performanceStudy.py)：统一实验描述、指标和性能模型计算。
- [`fitPerformanceModel.py`](./fitPerformanceModel.py)：从小规模标定结果拟合部署前性能模型。
- [`analyzePerformanceStudy.py`](./analyzePerformanceStudy.py)：汇总静态/动态结果并生成图表。
- [`runPerformanceStudy.py`](./runPerformanceStudy.py)：按算法串行运行完整实验，每种算法使用新建集群并在结束后清理。
- [`executeE1aExperiment.py`](./executeE1aExperiment.py)：执行 E1-A 建群、标定、冻结点序、五 worker 分波并行、采集、分析和清理。
- [`e1aWorkload.c`](./e1aWorkload.c)：实现固定到达、固定工作单元、有界队列和任务计数守恒的原生工作负载。
- [`analyzeE1aExperiment.py`](./analyzeE1aExperiment.py)：聚合正式重复、识别重复性过渡带与描述性分段拐点，并生成 PNG/PDF。
- [`validateE1aResults.py`](./validateE1aResults.py)：验证冻结计划、逐点任务计数、节点采样、CPU 限流和实验资源清理。

E1-A 相关 12 项测试通过，覆盖固定到达任务守恒、过载不自节流、固定相位、真实 nodeName、正式点集、并行 wave 和重复性过渡带；Python 编译检查通过。

## 4. E1-A：单 VM Pod 数量扫描

Pilot：[`experiments/e1_marginal_pods/20260918_e1a`](./experiments/e1_marginal_pods/20260918_e1a/)

正式运行：[`experiments/e1_marginal_pods/20260918_e1a_formal_parallel`](./experiments/e1_marginal_pods/20260918_e1a_formal_parallel/)

正式实验使用 1 台 2 vCPU/8 GiB master 和 5 台同构的 4 vCPU/8 GiB worker。五个 worker 的 vCPU 固定到互不重叠的物理 CPU 集合；每个实验点仍独占一台 worker，同一 wave 的五个点在全部 Ready 后通过时间屏障并行启动。每个 worker 执行 30 点，总计 150 点：15 个 N、Loaded/Idle 两组、5 个重复 block。预热 30 秒、测量 120 秒。

Pilot 冻结的背景任务工作单元为 1,828,943，对应参考机器约 5 ms/任务、10 jobs/s；canary 为 36,719 工作单元、100 jobs/s。Pod 没有 CPU limit；每点都使用相同的绝对到达表、有界队列和任务计数规则。

| group | N | overload | canary P99 中位数 ms | on-time 中位数 | CPU busy 中位数 | run queue 中位数 |
|---|---:|---:|---:|---:|---:|---:|
| Loaded | 0 | 0/5 | 0.464 | 1.000 | 0.006 | 1.07 |
| Loaded | 20 | 0/5 | 6.573 | 0.9994 | 0.445 | 2.18 |
| Loaded | 40 | 0/5 | 7.343 | 0.9987 | 0.535 | 3.24 |
| Loaded | 60 | 2/5 | 9.647 | 0.9923 | 0.772 | 5.37 |
| Loaded | 70 | 5/5 | 17.931 | 0.9530 | 0.900 | 15.28 |
| Loaded | 80 | 5/5 | 14.033 | 0.9847 | 1.000 | 80.84 |
| Loaded | 100 | 5/5 | 24.416 | 0.9687 | 1.000 | 101.19 |
| Loaded | 140 | 5/5 | 56.265 | 0.9052 | 1.000 | 141.53 |
| Loaded | 200 | 5/5 | 161.312 | 0.7708 | 1.000 | 201.97 |
| Idle | 0–200 | 0/75 | 0.450–0.522 | 1.000 | 0.006–0.086 | 1.03–1.73 |

正式结果给出三种需要区分的转折：冻结过载判据在 N=60 出现混合结果，稳定正常到稳定过载的重复性区间为 `(40,70]`；CPU busy 中位数达到 99% 且背景完成率低于 99% 的首个点为 N=80；对全范围 P99 做描述性分段拟合得到 N=140，SSE 相对单直线改善 81.7%。这三者分别表示 SLA/任务失约、资源饱和和高负载区时延斜率加速，不应合并成唯一的硬 Pod 上限。

状态为 success=93、workload_overload=57，没有 admission、harness 或 infrastructure 失败。150 点均与冻结计划一一对应，canary 和背景任务全部计数守恒，每点有 120 个节点样本，cgroup CPU throttling 为 0；最大平均 CPU steal 为 0.55%，最低可用内存约 5.0 GiB。五台 worker 每台恰好承担 30 点。Idle 的 75 点全部成功，说明本轮恶化来自固定外生工作叠加，而不是 Pod 对象数量本身。

图表：[`e1a_formal_transition.png`](./experiments/e1_marginal_pods/20260918_e1a_formal_parallel/figures/e1a_formal_transition.png)、[`e1a_formal_curves.png`](./experiments/e1_marginal_pods/20260918_e1a_formal_parallel/figures/e1a_formal_curves.png)。聚合表见 [`e1a_summary.csv`](./experiments/e1_marginal_pods/20260918_e1a_formal_parallel/metrics/e1a_summary.csv)，完整验证见 [`validation.json`](./experiments/e1_marginal_pods/20260918_e1a_formal_parallel/metrics/validation.json)。本轮专属 VM、网络、磁盘和空目录均已清理；其他 VM 和已有网络未被操作。

E1-A 只建立了给定 4 vCPU/8 GiB VM 与冻结工作画像下的本地共置代价曲线。它没有改变 SEED 映射或测量跨 VM 通信，因此不能单独证明“减少跨机通信后全局性能反而恶化”；该问题留给 E1-B。

## 5. E1 第一轮 topology 214 探索实验（未通过验收）

实验目录：[`experiments/research_e1_topology214_20260918`](./experiments/research_e1_topology214_20260918/)

5 种映射都完成部署、静态验证、动态负载扫描和清理。4 档外生 CPU 负载为 0、10、25、50 millicore。抽样拓扑语义检查为 MATCH。这里的“完成”仅指第一轮流程执行完成，**E1 研究目标没有完成**。

### 5.1 静态分布

| 映射 | 4 个工作节点的 Pod 数 | 加权跨节点通信对比例 |
|---|---:|---:|
| locality-only | 76 / 76 / 62 / 0 | 0.79320 |
| tradeoff-static, weight=0.1 | 76 / 76 / 53 / 9 | 0.60424 |
| tradeoff-static, weight=10 | 55 / 54 / 55 / 50 | 0.76784 |
| tradeoff-static, weight=1000 | 53 / 53 / 54 / 54 | 0.78484 |
| capacity-only | 53 / 53 / 54 / 54 | 0.79041 |

`locality-only` 是确定性贪心实现，并不是全局最小切边算法；本轮中 weight=0.1 的跨节点通信比例更低。

### 5.2 动态结果

性能惩罚定义为：

```text
penalty = log(RTT_ms) - log(stress_goodput_Mbps)
          - 2 * log(max(success_rate, 1e-6))
```

数值越低越好。RTT 只来自成功探测，吞吐只来自完成流，因此使用成功率修正，避免高失败率的方案因为少量幸存样本而得到虚假的好分数。

| 映射 | 0m penalty | 10m penalty | 25m penalty | 50m penalty | 流失败率 |
|---|---:|---:|---:|---:|---:|
| locality-only | -8.0024 | -7.1704 | -8.1832 | -8.3743 | 31.25% |
| tradeoff 0.1 | -8.1755 | -8.0487 | -7.5863 | -7.9265 | 31.25% |
| tradeoff 10 | -7.5099 | -6.8584 | -7.1436 | -7.5738 | 31.25% |
| tradeoff 1000 | -7.1606 | -7.6292 | -7.5246 | -8.2976 | 31.25% |
| capacity-only | -7.2444 | -7.7232 | -7.6318 | -7.4204 | 31.25% |

本轮在任何一种映射内都没有检测到随负载增加先改善、后恶化的内部拐点。进一步检查原始记录后确认：固定失败方向全部来自 `cross_as_multihop` 分层。该分层随机组合不同 AS 的路由器接口地址，而这些接口前缀中有大量并未作为端到端可达前缀传播；10 对跨 AS 样本中只有 2 对双向可达，另外 8 对存在单向或双向 100% 丢包。它造成了与算法和负载无关的固定失败率，不能解释为放置性能。

此外，`locality-only` 贪心映射的加权跨节点通信比例为 0.79320，反而高于 tradeoff 0.1 的 0.60424，说明本轮算法集合没有形成可信、单调的通信局部性梯度。最高 50m/Pod 的负载区间也没有稳定跨过各映射的资源争用阈值。因此该轮只能作为工程探索记录，不能验收 E1。

图表：[`locality_contention_curve.png`](./experiments/research_e1_topology214_20260918/locality_contention_curve.png)、[`load_response_curve.png`](./experiments/research_e1_topology214_20260918/load_response_curve.png)。

## 6. E2：轻量性能模型与部署输出

当前原型模型使用第一轮未通过验收的 topology 214 探索数据中的 0、10、25 millicore 数据拟合，50 millicore 作为保留负载，不参与拟合。E1-A 的新结果尚未进入该模型，E1-B 也未完成，因此下列系数只用于定位工程问题，不能作为论文模型或后续正式 E3 的输入：

| 项目 | 数值 |
|---|---:|
| 截距 | -8.17110995 |
| 通信特征系数 | +0.98107067 |
| 争用特征系数 | -1.39903269 |
| 训练集 R² | 0.32674666 |
| 训练数据哈希 | `4c82dd7a64b2d13582a3ccdbcf3461616b6c2b010d045ae72bd00fdcfc7d47d6` |

当前模型能够在编译前输出 Pod-to-Node 映射、节点镜像预置清单和确定性部署清单。负的争用系数按原始拟合保留，没有为了符合预期而改符号。但这与研究预期相反，且 R² 只有 0.327，说明现有小规模标定数据不足以稳定识别争用代价。

## 7. E3：topology 1078 独立验证

实验目录：[`experiments/research_e3_topology1078_20260918`](./experiments/research_e3_topology1078_20260918/)

三个方案在部署后均满足 1078 个 Pod 同时在线。每个节点 `allocatable.pods=400`，扣除 2 个系统 Pod 后真实剩余准入量为 398。

### 7.1 静态结果

| 算法 | 工作节点 Pod 数 | trunk0 VLAN 数 | 加权跨节点通信对比例 |
|---|---|---|---:|
| Crescent 适配基线 | 398 / 398 / 282 | 501 / 463 / 334 | 0.15988 |
| Crescent 资源感知 | 359 / 358 / 361 | 433 / 452 / 433 | 0.17451 |
| performance-model | 398 / 398 / 282 | 530 / 520 / 270 | 0.39206 |

### 7.2 动态结果

| 算法 | 负载 | RTT 均值 (ms) | stress 聚合吞吐 (Mbps) | 流失败率 | penalty |
|---|---:|---:|---:|---:|---:|
| Crescent | 5m | 16.459 | 33663 | 18.75% | -7.2080 |
| Crescent | 15m | 17.785 | 36776 | 18.75% | -7.2190 |
| Crescent 资源感知 | 5m | 21.010 | 36988 | 21.875% | -6.9797 |
| Crescent 资源感知 | 15m | 24.573 | 34224 | 21.875% | -6.7453 |
| performance-model | 5m | 0.439* | 4146.7 | 93.75% | -3.6076 |
| performance-model | 15m | 缺失 | 缺失 | 缺失 | 缺失 |

\* 0.439 ms 只来自极少数成功探测，不能解释为整体性能更好；93.75% 的流已经失败。15m 时负载注入停顿，没有伪造或插补指标。停顿时两个 8 vCPU worker 的负载约为 8 核，第三个约为 4.5 核。

performance-model 在 5m 的预测 penalty 为 -7.8024，实测为 -3.6076，发生明显失配；它也没有优于两个基线。本轮说明集中映射可能伴随严重尾部失败和执行停顿，但单次运行不能建立统计意义上的反转曲线。

这里的 Crescent 是基于论文语义实现的适配基线，不声称是作者原始代码。参考：[USENIX NSDI '24 页面](https://www.usenix.org/conference/nsdi24/presentation/gao-zhaoyu)和[论文 PDF](https://anubhavnidhi.github.io/papers/nsdi24-gao-zhaoyu.pdf)。

## 8. 4954/48VM 首轮工程故障与修复

首轮归档目录：[`experiments/research_e3_topology4954_48vm_pod_quantity_failure_20260918`](./experiments/research_e3_topology4954_48vm_pod_quantity_failure_20260918/)

首轮完成 48 台 VM 建群（1686.92 秒）和编译（676.17 秒），随后在放置准入检查中失败：

```text
cannot read live kubelet Pod admission limits:
invalid literal for int() with base 10: '4k'
```

原因是 Kubernetes 将 `maxPods: 4000` 规范化为 Quantity 字符串 `4k`。这是代码解析错误，与研究假设无关。现已使用 Kubernetes Quantity 语义解析整数，支持 `4k`、`2Ki`，并拒绝 `500m` 这类非整数 Pod 数；相关测试已通过。首轮的 48 台 VM 和网络已清理，失败记录原样归档。

## 9. 已中断并清理的 4954/48VM 运行

实验目录：[`experiments/research_e3_topology4954_48vm_20260918`](./experiments/research_e3_topology4954_48vm_20260918/)

启动命令：

```bash
/home/lxl/anaconda3/envs/seedpy310/bin/python -u runPerformanceStudy.py \
  --config research_configs/configE3Topology4954FortyEightVm.yaml \
  --run-dir experiments/research_e3_topology4954_48vm_20260918
```

配置为 1 台 master（24 vCPU、36 GiB）和 47 台 worker（每台 6 vCPU、12 GiB），master 不承载仿真 Pod；总资源为 306 vCPU、600 GiB。依次运行 Crescent、Crescent 资源感知和 performance-model，每个算法新建并清理自己的集群。负载档位为 0、20、40 millicore；动态测量最多抽取 72 对通信端点，stress 并发为 8、16、32、48，每项重复 2 次、每次 12 秒。

该运行于 2026-09-18 12:14 CST 在 Crescent 拓扑编译阶段主动中断，原因是确认 E1 尚未完成，继续大规模 E3 不符合实验顺序。中断状态和日志已保留为 `INTERRUPTED`，不能计为算法失败。中断前建群耗时 1646.41 秒并通过；本轮 48 台 VM、K3s 和三个专用 libvirt 网络随后已完成清理验证。

## 10. 当前问题与实验风险

1. E1-A 已复现本地共置争用，但 E1-B 尚未把同一现象放入固定 topology 214 的通信局部性梯度；不能提前声称观察到全局性能反转。
2. 旧 E1 的 locality-only 贪心方案并未产生最低跨节点通信比例；E1-B 必须部署前冻结一条单调、容量可行且逐批减少通信需求的完整映射序列。
3. E1-B 的业务对必须经过真实仿真地址、路由和逐跳路径预检；旧的随机跨 AS 接口样本不可继续使用。
4. E1-A 的 `(40,70]` 过渡带只适用于本轮 4 vCPU/8 GiB 与固定工作画像。E1-B 可以将目标 worker 从约 54 Pod 扫描到 64、74、84 等预先冻结点，但不能把 70 当作人为准入上限。
5. E2 旧模型 R² 较低且争用系数为负；必须用 E1-A 的边际曲线和后续 E1-B 数据重新定义特征与标定集，旧系数不能进入正式 E3。
6. 1078 的高失败率与 15m 缺失数据仍只属于探索结果，不能插值或当作正式独立验证。
7. E1-A 有五个重复 block；旧 topology 214、1078 与算法对比仍缺少同等层次的独立重复和配对区间。
8. 主机当前还有用户保留的 `bird-rtnl-dev`、`e1-0918-as060` 至 `e1-0918-as150` 等 VM；实验清理只处理 manifest 明确拥有的资源。

## 11. 当前阶段结论

E1-A 已在正式五重复实验中证明：固定 4 vCPU/8 GiB VM 上，增加执行相同固定外生工作的 Pod 会先进入 `(40,70]` 的失约过渡区，并在 N=80 后出现 CPU 饱和、运行队列突增和背景完成率下降；相同数量的 Idle Pod 没有该现象。代码、数据完整性和清理均已验证。

这满足 E1-A 的本地争用动机，但没有验证通信局部性收益是否被争用抵消。下一步是 E1-B：在 topology 214、全部 214 个仿真 Pod 与固定业务不变的条件下，冻结逐批降低跨 worker 通信需求的映射序列，并分别报告目标 worker canary、本地资源排队、固定 offered UDP 实现量及全网 TCP/RTT。E1-B 完成前不重新拟合正式 E2，也不恢复正式 E3。
