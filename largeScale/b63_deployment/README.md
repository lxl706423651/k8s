# B63：在 B62 流程上增加 placement

B63 的主入口直接调用相邻 B62 的 `runFullExperiment.main()`，共用集群创建、
编译、canonical DockerCompiler 镜像内容、按节点镜像预加载、部署、BIRD/FIB
验证、跨节点连通性和故障恢复测试。B63 负责分区算法、静态验证、逐 Pod 落点检查，
并在最后默认调用 `running/measureDataplane.py` 测量 RTT、吞吐和多流压力性能。
两个例子复用代码，各自使用独立的集群资源和运行目录。

面向通信局部性与宿主机争用的 E1/E2/E3 协议、运行命令、模型口径和产物说明见
[RESEARCH_EXPERIMENTS.md](RESEARCH_EXPERIMENTS.md)。

旧实现复制了 `running/`，还分别维护了编译、网络参数、镜像分发和部署编排；
因此并非单纯比 B62 多一个 placement。新入口不再调用这套副本，20 个共用阶段/helper 的旧文件名已改为转发到 B62。

流程如下：

```text
B62: build-cluster → ensure-all-nodes-schedulable
     → clean → preflight → compile（含 canonical output 适配）
B63: → placement（固定分区或移除固定落点，保存 B62 基线）
B63: → verify-static-placement（离线检查分配、manifest 和静态指标）
B62: → build/preload → deploy → wait-ready
B63: → verify-placement（固定分区逐 Pod 对照；默认调度检查真实落点并统计）
B62: → start-bird → verify-after-start-bird
     → start-kernel → verify-after-fib-write
     → validate-cross-node-workload → reconvergence
B63: → dataplane（RTT、基线 TCP、8/16/32/48 并发压力测试）
```

placement 默认只修改调度约束及报告注解，不修改拓扑连接、镜像、容器启动命令，
也不注入 CPU/memory requests。master 默认参与分区。B62 原有 by-AS 分配作为
`manifest-existing` 基线保存在 placement 报告中；镜像预加载与部署读取替换后的
`output/k8s.yaml`。所有验证均是必须通过的阶段，失败不会继续启动下一阶段。

`placement.pinningMode` 默认为 `node-selector`，可选 `node-name`。两种模式都按
目标节点预加载镜像、准备 VLAN/CNI，并在 deploy 中按节点分组；`node-name` 直接指定
Pod 节点，跳过 Kubernetes scheduler。`deploy.controllerApplyMode: node-stream` 会让
各节点的提交流程并行，每节点由 `deploy.nodeStreamMaxActivePerNode` 控制尚未 Running
的 Pod 数（默认 1）；`batch` 模式则用 `deploy.batchSize`、warm-up 批次和压力阈值
控制全局提交速度。两种模式都通过控制端的 `kubectl` 向 API server 提交资源。

## 文件与输入输出

| 文件 | 角色 |
| --- | --- |
| `configSingleHostKvmMacvlan.yaml` | 新流程唯一用户输入；与 B62 同名配置采用相同结构。 |
| `runB63LargeExperiment.py` | 精简入口；调用 B62，注册 placement、落点验证和最终 dataplane 回调。 |
| `runPlacementComparison.py` | 新的单算法/四算法串行全流程入口；复用原 YAML，逐轮创建、测量、清理，失败保留现场。 |
| `collectPlacementComparison.py` | 只读汇总已有批次，输出静态/动态 CSV、逐阶段耗时与 Markdown，并核对测量方向。 |
| `runPlacementExperiment.py` | 分区算法、静态基线比较、manifest 约束写入；也可独立进行静态实验。 |
| `verifyPlacement.py` | 只读验证实际 Pod 落点、数量、Ready 状态，生成验证报告。 |
| `verifyStaticPlacement.py` | 离线静态验证器：在 build/preload 前核对分配、manifest 和重算指标；不访问集群。 |
| `k8sTools.py` | 原生 K8sTools CLI；也可针对指定运行目录销毁本次测试集群。 |
| `experiments/<run>/` | 生成输出：配置快照、集群状态、镜像/manifest、日志、验证与性能报告。 |
| `configkvm_b63.yaml`, `vm_matrix_configs/` | 历史显式节点配置；不是新入口的默认输入。 |
| `running/` | 20 个共用脚本为 B62 转发入口；shell 阶段必须传含 assignment.yaml 的本例运行目录。`measureDataplane.py` 由主流程默认调用；`lib.sh`、`placement.sh`、`buildBaselineAllNodePreload.sh` 保留为历史专用文件。 |
| `runDefaultSchedulerBaseline.py` | 解除固定落点的 helper 被新流程复用；旧 CLI 的独立 up/routing 编排保留，不用于新的串行全流程。 |
| `runVmScaleMatrix.py`, `generateVmMatrixConfigs.py` | 历史矩阵入口/配置生成器，使用旧 CLI；需迁移为新 YAML 后使用。 |
| `configK3s-b63.yaml`, `kubeconfig-b63.yaml`, `inventory-b63.yaml` | 历史集群输出；新入口不会读取或覆盖它们。 |
| `0624design.md`, `0629experimentResult.md`, `cleanup_logs/` | 历史设计、实验与清理记录（存在时保留）。 |
| `codex_worklog.md` | 修改、命令与验证记录。 |

B62 主流程提供可选 Python 回调 `after_compile`、`before_build`、`after_ready` 和
`after_workload`。`before_build` 包括从 build 恢复的路径；最后一个在完整流程或恢复流程成功后、写入最终 PASS 之前执行。
默认值均为 `None`，B62 自己运行时保持原有行为。

## 四算法串行对比（当前入口）

当前 `configSingleHostKvmMacvlan.yaml` 是 **48VM / 7001**，新脚本不修改它。
四种算法只切换 placement，沿用相同拓扑、节点资源、网络、资源请求设置和测量参数：

| `--algorithm` | 行为 |
| --- | --- |
| `optimized` | B63 优化分区，写入固定 hostname nodeSelector。 |
| `network-only` | 网络代价优先的硬分区：`alpha=1`，资源、Pod 数及 VLAN 均衡目标权重为 0；仍保留容量硬约束，写入固定 hostname nodeSelector。 |
| `pod-count-balanced` | 按 Pod 数量均衡，单副本拓扑中合格节点的数量最多相差 1，不按 CPU 容量加权。 |
| `kubernetes-default-scheduler` | 不指定每个 Pod 的目标节点，由真实 K8s default-scheduler 分配。 |
| `all`（默认） | 依次运行上述四种，顺序 optimized → network-only → 默认调度 → Pod 均衡。 |

```bash
conda activate seedpy310

# 仅检查四轮配置，不建 VM；准备目录不能再作为真实运行目录。
python3 runPlacementComparison.py --prepare-only \
  --run-dir experiments/comparison-prepare

# 一键四算法完整实验，每轮成功后销毁该轮集群（包括最后一轮）。
python3 runPlacementComparison.py \
  --config configSingleHostKvmMacvlan.yaml \
  --algorithm all --run-dir experiments/comparison-7001

# 只跑一种，默认保留集群；加 --destroy-after-pass 可成功后自动清理。
python3 runPlacementComparison.py --algorithm kubernetes-default-scheduler \
  --run-dir experiments/default-scheduler-7001
python3 runPlacementComparison.py --algorithm network-only \
  --run-dir experiments/network-only-7001
python3 runPlacementComparison.py --algorithm pod-count-balanced \
  --run-dir experiments/pod-balanced-7001
```

`--run-dir` 是批次根目录，实际运行目录为 `<批次>/<算法>/`。每轮包含
`input.yaml`、`assignment.yaml`、`configK3s.yaml`、日志和全部测量结果。
默认执行静态门控、实际落点检查、BIRD/FIB、跨节点验收、故障恢复及 dataplane；
只有显式 `--skipMeasureDataplane` 才跳过最后的测量。
批次根目录生成 `comparison_runs.json`（命令/进度/清理状态）、
`comparison_metrics.csv`、`comparison_stages.csv`、`comparison_summary.json`、`comparison.md`。
每轮开始/结束都会更新汇总，缺失指标保留为空，不当成 0。

默认调度的静态模型仍保留，但标记为 `scheduler-simulation`，**不代表真实落点**。
`verifyStaticPlacement.py` 验证部署 manifest 已解除固定落点且其他内容符合预期；
`verifyPlacement.py` 检查数量、Ready、合格节点和 scheduler，再按真实映射重算指标。
汇总中的 Pod/VLAN 使用分布来自真实映射，不拿模拟值代替。

默认调度必须在所有节点预加载全部镜像，VLAN parent 也需提前在所有可能节点配置。
B62 共享 `build.sh` 从冻结 YAML 的 `placement.algorithm` 决定全节点预加载；
其他算法仍按固定落点预加载。共享部署原已支持无固定落点时的全节点 VLAN 准备。
报告分别记录 Pod 实际使用的 VLAN（按真实落点推算）与部署配置的 VLAN，避免低估默认调度开销。
此差异也会影响 build/deploy 耗时，应与稳态吞吐、RTT 分开分析。

串行批次只销毁该轮成功生成的 `configK3s.yaml` 对应集群，不清理已有其他集群；
要求 `kvm.allowExisting: false`。实验失败或清理失败立即停止，失败集群保留。
例如恢复默认调度轮次或手动清理：

```bash
python3 runB63LargeExperiment.py \
  --run-dir experiments/comparison-7001/kubernetes-default-scheduler --resume-from build
python3 k8sTools.py destroy \
  -d experiments/comparison-7001/kubernetes-default-scheduler/configK3s.yaml --keep-temp
python3 collectPlacementComparison.py --run-dir experiments/comparison-7001
```

汇总器保留批次执行/清理状态；手动恢复成功不会自动把失败批次改成 PASS，也不会启动剩余轮次。
可用新的批次目录单独运行剩余算法。旧 `runVmScaleMatrix.py` 的参数未适配，不用于本入口。
`sampleComparison` 比较各轮逻辑端点、基线 TCP 方向和每档 stress/canary 选择；
MATCH 表示样本相同，MISMATCH 表示不能直接进行同流性能比较，INCOMPLETE 表示数据未收齐。
测量 PASS 仍不等于每条流都成功，需结合失败率。

## 历史 6 VM / 1078 配置

此前验证配置为 1 master + 5 workers（当前默认 YAML 已改为 48VM / 7001）：

| 项目 | master | 每台 worker |
| --- | --- | --- |
| vCPU | 16 | 8 |
| 内存 | 24 GiB | 24 GiB |
| 磁盘 | 80 GiB | 80 GiB |

总计 56 vCPU、144 GiB。VM 名称为 `b63-sh6-master`、`b63-sh6-worker1..5`。
管理网络为 `seedemu-b63-sh6` / `10.83.0.0/24` / `virbrb63sh6`，地址从
`10.83.0.10` 开始。独立的 fabric 网络为 `seedemu-b63-sh6-fabric0/1`，
bridge 为 `brb63s6f0/1`，VM trunk 接口为 `ens3/ens4`。
Pod CIDR 为 `10.64.0.0/12`，Service CIDR 为 `10.84.0.0/16`。
这些值与本次执行前检查到的 B62 和历史 B63 网络不同。

同一份配置不能同时创建多套同名 VM。下一次全新实验应先针对原运行目录清理本例
集群，或在另一份 YAML 中更换名称、网段、MAC、bridge 和存储目录。
入口不会自动销毁任何已有集群，也不会使用 B62 的集群状态文件。

基础镜像和 bootstrap 镜像缓存可以复用 B62；可写 VM 磁盘及 cloud-init 使用本例
自己的 `kvm.storageDir/diskDir/cloudInitDir`。存储路径是本机部署配置，可按宿主机
调整；共享框架代码不依赖这些绝对路径。

## 运行与验证

在本目录执行：

```bash
# 无副作用检查：冻结输入和资源汇总，不创建 VM。
python3 runB63LargeExperiment.py --prepare-only --run-dir experiments/current-prepare

# 完整实验：运行目录必须是新的目录。
python3 runB63LargeExperiment.py \
  --config configSingleHostKvmMacvlan.yaml \
  --run-dir experiments/current-run

# 只跳过最后的多流测量，仍执行共享流程中的单流连通性验收。
python3 runB63LargeExperiment.py \
  --run-dir experiments/current-run-no-dataplane --skipMeasureDataplane
```

默认 `dataplane` 阶段使用运行目录对应的 namespace、kubeconfig，以及固定输入
解析后的 topologySize/topologyDir，不读取历史 `kubeconfig-b63.yaml`。
普通 ping 每方向 10 包；基线 TCP 48 个方向、并发 4、每流 8 秒；压力档位为
8/16/32/48，每档重复 3 次、每流 15 秒，附带 16 个 ping 探针。
候选流对沿用测量脚本的 v2 算法和种子 20260624，最多 96 对，包含 AS 内、
IX 邻居和跨 AS 多跳三类。`--skipMeasureDataplane` 会记录零耗时 SKIPPED 阶段；
`--prepare-only` 不执行任何测量。所有 `--resume-from` 路径结束后也默认执行
dataplane，除非本次命令显式传入跳过参数。恢复时测量输出使用同一运行目录。

压测脚本异常退出会使整个流程 FAIL；正常完成时，单条路径不通等测量结果
仍按原脚本语义记录在 failureRate/missingDirections 等字段，并不自动让流程失败。
因此 PASS 表示测量完成，评估性能时还要检查失败率和每轮 successCount。
只调整这一步的测量参数时可跳过自动测量，再独立运行 `running/measureDataplane.py`。

关键产物：

```text
experiments/current-run/
  cluster-input.yaml                 # 固定路径后的集群输入快照
  assignment.yaml                    # B62 内部阶段配置，包含 runtime 文件路径
  configK3s.yaml / kubeconfig.yaml / inventory.yaml
  summary.json                       # 共用阶段、placement 与所有验证状态
  canonical_output_summary.json      # 镜像内容与 canonical output 的对应检查
  placement_expected.json / placement_plan.json  # B62 by-AS 基线，不是最终 B63 分配
  output/k8s.b62.yaml                 # B62 编译完成时的原始 placement
  output/k8s.yaml                     # B63 最终部署 manifest
  output/placement_report.json        # 选中算法、B62 基线、分配与指标
  placement_actual_summary.json       # 逐 Pod 实际 node、数量、Ready 验证
  placement_static_summary.json       # 静态 PASS/FAIL、重算指标、Pod/VLAN 均值和总体方差
  verify-static-placement.orchestrator.log
  verify_after_start_bird_summary.json
  verify_after_fib_write_summary.json
  cross_node_workload_summary.json
  reconvergence_summary.json
  dataplane.orchestrator.log         # 多流测量命令和控制台输出
  dataplane/latency_throughput_summary.json  # RTT/吞吐/stress.byLevel["48"]
  dataplane/stress_rounds.json        # 各轮逐流分布、失败率、公平性和负载下 RTT
  dataplane/stress_raw/level_48_rep_0/iperf_results.json  # 原始逐流结果
```

`summary.json` 的 `stages` 记录 dataplane 耗时/退出状态，`tests.dataplane` 保存
测量汇总。默认自动测量结束后再执行 destroy，才能保留完整的数据面结果。

静态验证默认强制执行，位于 placement 之后、镜像构建/预加载之前；
`--skipMeasureDataplane` 不会跳过静态验证。缺失分配、非法/排除节点、部署 manifest
与允许的 placement 修改不一致、或报告静态指标与重新计算结果不一致，均使流程失败。
`--resume-from build` 会再次验证保存的产物；从 start-kernel/reconvergence 恢复不重新部署，
不重复此构建前检查。`--prepare-only` 尚无编译产物，因此不运行静态验证。

重算复用现有资源估计、网络代价、VLAN 和分配评估函数，并提供 selected 与
manifest-existing 的逐节点 Pod/VLAN 数量、均值、总体方差（除以节点数）和标准差。
模型容量/pressure 分数作为估计报告，不额外引入新的硬门限；浮点比较容差为 1e-9。
此检查证明保存的分配与指标自洽，不声称该算法优于基线或证明动态吞吐可复现。

已有集群销毁后仍可独立静态验证（需要保留 assignment、cluster-input 和 output）：

```bash
python3 verifyStaticPlacement.py --run-dir experiments/current-run
```

离线重复运行分区算法时，将历史 `output/k8s.b62.yaml` 作为输入，输出到独立目录，
并复用历史 assignment 的算法参数和保存的节点容量。比较 placements、staticValidation
与生成的 manifest；忽略报告中的输出路径/创建时间。实际复现样例和命令见
`experiments/placement_static_replay_20260915/`，原实验输出不被覆盖。
本次 1078 重放一致；7001 在历史 `seedpy310`（Python 3.10.19）环境下重放一致，
但系统 Python 3.12.7 重放有 6303 个 Pod 落点变化。因此复现必须固定 Python
运行环境、输入和算法参数；静态校验通过仅说明产物自洽，不保证跨环境落点一致。

独立执行共用阶段时传入运行目录，`assignment.yaml.runtime` 会指向本例集群：

```bash
bash ../b62_k8s_scale/verifyStartBird.sh experiments/current-run
bash ../b62_k8s_scale/verifyFibRoutes.sh experiments/current-run
bash ../b62_k8s_scale/clean.sh experiments/current-run
```

`clean.sh` 删除本次 namespace。需要销毁本次 VM 时，明确提供本次状态文件：

```bash
python3 k8sTools.py destroy -d experiments/current-run/configK3s.yaml --keep-temp
```

已有编译/placement 产物的运行可用 `--resume-from build` 恢复镜像构建与后续阶段，
不会重建集群或重算分区。已完成部署的运行也可用 `--resume-from start-kernel` 或
`--resume-from reconvergence`，必须传原 `--run-dir`。

## YAML 字段

基础设施/运行参数直接由 B62 和 K8sTools 消费，完整解释见
[B62 单 YAML 配置说明](../b62_k8s_scale/configSingleHostKvmMacvlan.md)。本例没有
第二套部署参数解析器；旧入口的 `--deploy-*`、`--build-*` 等参数应迁移到 YAML
支持的相应字段，不再通过旧 CLI 设置。

| 字段组 | 必填/默认值 | 消费方 |
| --- | --- | --- |
| `kind`, `clusterName`, `defaults`, `master`, `workers`, `kvm`, `ssh` | 按模板填写；`workers.count` 为 worker 数，内存用 `memoryGiB` | B62 / K8sTools 建群 |
| `experiment.topologySize`, `experiment.topologyDir` | 规模必填；目录默认 `~/seed-emulator/topology` | B62 编译与 canonical 内容适配 |
| `experiment.name`, `experiment.namespace` | 本例显式填写，避免沿用 B62 默认 namespace | B62 编排与阶段脚本 |
| `fabric`, `cni`, `registry`, `seedemu`, `k3s`, `tuning` | 参照本例和 B62 模板 | 原生 setup / B62 running |
| `deploy` | 可选，省略字段使用 B62 默认值 | B62 `lib.sh` / `deploy.sh` |
| `placement.excludeControlPlane` | 可选，默认 `false` | B62 基线和 B63 分区 |
| `placement.nodePodReserve` | 可选，默认 `20`；只控制 B62 by-AS 基线 | B62 编译 |
| `placement.algorithm` | 可选，默认 `optimized` | B63 分区 |
| `placement.networkCostMode` | 可选，默认 `ratio`；可选 `pair` / `endpoint-exposure` | B63 分区 |
| `placement.alpha`, `placement.beta` | 可选，均默认 `1.0` | B63 网络/资源目标权重 |
| `placement.podBalanceWeight`, `placement.vlanParentBalanceWeight` | 可选，默认 `8.0` / `4.0` | B63 平衡目标 |
| `placement.podCapSlack`, `placement.vlanParentCapSlack` | 可选，默认 `1.0` / `1.5` | B63 分区容量限制 |
| `placement.capacityBalanceRatio` | 可选，默认 `1.0` | B63 异构资源平衡 |
| `placement.resourceOverloadPenalty`, `placement.capacityHardCapPenalty` | 可选，默认 `0.0` / `100000.0` | B63 评分 |
| `placement.improvementPasses` | 可选，默认 `0` | B63 局部改进轮数 |
| `placement.fastGreedyScore`, `placement.selectedOnly` | 可选，均默认 `true` | B63 增量评分/仅计算选中算法（同时记录现有 B62 分配） |
| `placement.injectResourceRequests` | 可选，默认 `false` | B63 manifest 写入 |
| `placement.pinningMode` | 可选，默认 `node-selector`；可选 `node-name` | B63 manifest 固定落点 |
| `placement.ixWeight`, `peerWeight`, `intraWeight`, `hostWeight` | 可选，默认 `10.0` / `5.0` / `2.0` / `0.5`，均位于 `placement` 下 | B63 网络权重 |

硬约束算法支持 `optimized`、`network-only`、`pod-count`、`pod-count-balanced`、
`resource-only`、`hypergraph`。共享主流程也支持 `kubernetes-default-scheduler`，
使用全节点预加载及真实调度后的落点统计。新串行入口运行上述四种对照。

本次重构不删除历史文件，重复实现改为转发；历史矩阵命令及历史报告中的旧命令不是新入口的接口。

## 本次验证记录

2026-09-14 的 `experiments/1078_6vm_shared_validation/` 完整实验已通过：
1078/1078 Pod Ready 且实际落点一致；BIRD 固定 10 AS、FIB 全部 82 AS、跨节点
ping/iperf、故障恢复及恢复后协议/FIB 复查全部通过。最终 VM load1 小于 1。
主流程保留了第一次因 registry 端口硬编码而失败的 build 记录，修复共用脚本后
从 build 阶段恢复成功。共用构建临时文件也已按运行目录隔离。
详细命令、外部 B62 清理记录与验证证据见 [工作记录](codex_worklog.md)。
