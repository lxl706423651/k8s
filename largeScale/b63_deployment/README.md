# B63：在 B62 流程上增加 placement

B63 的主入口直接调用相邻 B62 的 `runFullExperiment.main()`，共用集群创建、
编译、canonical DockerCompiler 镜像内容、按节点镜像预加载、部署、BIRD/FIB
验证、跨节点连通性和故障恢复测试。B63 负责分区算法和逐 Pod 落点检查。
两个例子复用代码，各自使用独立的集群资源和运行目录。

旧实现复制了 `running/`，还分别维护了编译、网络参数、镜像分发和部署编排；
因此并非单纯比 B62 多一个 placement。新入口不再调用这套副本，20 个共用阶段/helper 的旧文件名已改为转发到 B62。

流程如下：

```text
B62: build-cluster → ensure-all-nodes-schedulable
     → clean → preflight → compile（含 canonical output 适配）
B63: → placement（替换 hostname nodeSelector，保存 B62 基线）
B62: → build/preload → deploy → wait-ready
B63: → verify-placement（每个 Pod 的实际 node 与分区计划一致）
B62: → start-bird → verify-after-start-bird
     → start-kernel → verify-after-fib-write
     → validate-cross-node-workload → reconvergence
```

placement 默认只修改调度约束及报告注解，不修改拓扑连接、镜像、容器启动命令，
也不注入 CPU/memory requests。master 默认参与分区。B62 原有 by-AS 分配作为
`manifest-existing` 基线保存在 placement 报告中；镜像预加载与部署读取替换后的
`output/k8s.yaml`。所有验证均是必须通过的阶段，失败不会继续启动下一阶段。

## 文件与输入输出

| 文件 | 角色 |
| --- | --- |
| `configSingleHostKvmMacvlan.yaml` | 新流程唯一用户输入；与 B62 同名配置采用相同结构。 |
| `runB63LargeExperiment.py` | 精简入口；调用 B62，仅注册 placement 和落点验证回调。 |
| `runPlacementExperiment.py` | 分区算法、静态基线比较、manifest 约束写入；也可独立进行静态实验。 |
| `verifyPlacement.py` | 只读验证实际 Pod 落点、数量、Ready 状态，生成验证报告。 |
| `k8sTools.py` | 原生 K8sTools CLI；也可针对指定运行目录销毁本次测试集群。 |
| `experiments/<run>/` | 生成输出：配置快照、集群状态、镜像/manifest、日志、验证与性能报告。 |
| `configkvm_b63.yaml`, `vm_matrix_configs/` | 历史显式节点配置；不是新入口的默认输入。 |
| `running/` | 20 个共用脚本已改为 B62 转发入口；shell 阶段必须传含 assignment.yaml 的本例运行目录。`lib.sh`、`placement.sh`、`buildBaselineAllNodePreload.sh`、`measureDataplane.py` 保留为历史专用文件，新主流程无依赖。 |
| `runDefaultSchedulerBaseline.py` | 历史动态 default scheduler 实验入口；不是共享流程的一部分。 |
| `runVmScaleMatrix.py`, `generateVmMatrixConfigs.py` | 历史矩阵入口/配置生成器，使用旧 CLI；需迁移为新 YAML 后使用。 |
| `configK3s-b63.yaml`, `kubeconfig-b63.yaml`, `inventory-b63.yaml` | 历史集群输出；新入口不会读取或覆盖它们。 |
| `0624design.md`, `0629experimentResult.md`, `cleanup_logs/` | 历史设计、实验与清理记录（存在时保留）。 |
| `codex_worklog.md` | 修改、命令与验证记录。 |

B62 主流程增加了两个可选 Python 回调 `after_compile` 和 `after_ready`。
默认值均为 `None`，B62 自己运行时的阶段顺序和配置保持原有行为。

## 6 VM / 1078 配置

默认配置为 1 master + 5 workers：

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
python3 runB63LargeExperiment.py --prepare-only --run-dir experiments/prepare-1078

# 完整实验：运行目录必须是新的目录。
python3 runB63LargeExperiment.py \
  --config configSingleHostKvmMacvlan.yaml \
  --run-dir experiments/1078-6vm
```

关键产物：

```text
experiments/1078-6vm/
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
  verify_after_start_bird_summary.json
  verify_after_fib_write_summary.json
  cross_node_workload_summary.json
  reconvergence_summary.json
```

独立执行共用阶段时传入运行目录，`assignment.yaml.runtime` 会指向本例集群：

```bash
bash ../b62_k8s_scale/verifyStartBird.sh experiments/1078-6vm
bash ../b62_k8s_scale/verifyFibRoutes.sh experiments/1078-6vm
bash ../b62_k8s_scale/clean.sh experiments/1078-6vm
```

`clean.sh` 删除本次 namespace。需要销毁本次 VM 时，明确提供本次状态文件：

```bash
python3 k8sTools.py destroy -d experiments/1078-6vm/configK3s.yaml --keep-temp
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
| `placement.ixWeight`, `peerWeight`, `intraWeight`, `hostWeight` | 可选，默认 `10.0` / `5.0` / `2.0` / `0.5`，均位于 `placement` 下 | B63 网络权重 |

硬约束算法支持 `optimized`、`network-only`、`pod-count`、`pod-count-balanced`、
`resource-only`、`hypergraph`。`kubernetes-default-scheduler` 可通过
`runPlacementExperiment.py` 单独做静态对比；共享主流程依赖确定的 nodeSelector
进行按节点预加载，因此不会启动这个无固定落点的动态基线。

本次重构不删除历史文件，重复实现改为转发；历史矩阵命令及历史报告中的旧命令不是新入口的接口。

## 本次验证记录

2026-09-14 的 `experiments/1078_6vm_shared_validation/` 完整实验已通过：
1078/1078 Pod Ready 且实际落点一致；BIRD 固定 10 AS、FIB 全部 82 AS、跨节点
ping/iperf、故障恢复及恢复后协议/FIB 复查全部通过。最终 VM load1 小于 1。
主流程保留了第一次因 registry 端口硬编码而失败的 build 记录，修复共用脚本后
从 build 阶段恢复成功。共用构建临时文件也已按运行目录隔离。
详细命令、外部 B62 清理记录与验证证据见 [工作记录](codex_worklog.md)。
