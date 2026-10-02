# E1-A 单 VM Pod 数量扫描

## 架构

E1-A pilot 使用独立的 2 VM K3s：2 vCPU/8 GiB master 只承担控制面，4 vCPU/8 GiB worker 承担 canary 和变化数量的背景 Pod。正式阶段可使用同一 master 加五个相同 worker，每个实验点仍独占一台 worker，按五点 wave 并行。它不创建 SEED 拓扑或辅助网络，因此只回答同一 VM 上批量增加固定工作 Pod 时的本地争用代价，不回答跨 VM 通信收益。

每个 Pod 从 ConfigMap 挂载同一个静态编译的 `e1aWorkload`。所有 Pod 先等待 worker hostPath 中的 epoch 文件；当全部容器 Ready 后，编排器一次写入 worker 的绝对时间，Pod 再按固定相位和绝对到达表开始。过载不会让任务到达率自动下降，超出有界队列的任务会进入 `dropped`，结束时未完成任务进入 `pending_at_end`。

## 文件

| 文件 | 类型 | 作用 |
|---|---|---|
| `research_configs/configE1aMarginalPods.yaml` | 用户输入 | 专属 VM/K3s、工作画像、pilot/formal 点集和清理策略。 |
| `e1aWorkload.c` | 原生负载源文件 | 标定固定工作单元并执行绝对到达时间表。 |
| `collectE1aNodeMetrics.py` | 可执行采样器 | 在 worker 侧读取 CPU、run queue、PSI、steal、cgroup 和内存计数器。 |
| `executeE1aExperiment.py` | 全流程入口 | 建群、标定、冻结点序、运行、分析和按清单清理。 |
| `analyzeE1aExperiment.py` | 离线分析入口 | 聚合重复、计算边际变化、描述性分段拟合并输出图和中文报告。 |
| `validateE1aResults.py` | 结果验证入口 | 核对冻结计划、150 点完整性、任务计数、采样、限流和按清单清理。 |
| `E1_DESIGN_PROPOSAL.md` | 设计依据 | E1-A/E1-B 的研究边界和验收口径。 |

## 配置字段

`kind`、`clusterName`、`ssh`、`defaults`、`master`、`workers`、`kvm`、`fabric`、`registry`、`k3s` 和 `tuning` 由 `k8sTools.py build` 消费。E1-A 使用 `fabric.type: none`，只保留 K3s 主网络。

`e1a.workload` 是固定工作画像：背景/canary 到达率、参考 CPU 时间、deadline、有界积压时间和一致的 requests。`backgroundWorkUnits`、`canaryWorkUnits` 留空时，编排器在空载 worker 内标定一次并冻结到实验目录。

`e1a.pilot` 定义探索性预热、测量时间和预先声明的 Loaded/Idle N 点。`e1a.formal` 定义正式时长、粗点、拐点邻域规则、重复 block 数和随机顺序 seed。`e1a.measurement` 定义采样间隔、按时率阈值及 20% 工程参考线。`e1a.pinning` 只固定本实验新建 VM 的 vCPU；`e1a.cleanup.destroyClusterAfterRun` 控制完整成功后是否销毁专属集群。

## 阶段流

完整流程：

```bash
python3 executeE1aExperiment.py \
  --config research_configs/configE1aMarginalPods.yaml \
  --phase all
```

五个同构 worker 的正式 block 可以按同步 wave 并行执行。每个 point 仍独占
一台 4 vCPU/8 GiB worker；同一 wave 先等待全部 Pod Ready，再同时发布 epoch，
避免其他 worker 的部署过程混入测量窗口：

```bash
python3 executeE1aExperiment.py \
  --config research_configs/configE1aMarginalPodsParallel.yaml \
  --run-dir experiments/e1_marginal_pods/<formal_parallel_run> \
  --phase formal
```

并行配置通过 `e1a.pilotReference.runDir` 固定 pilot 拐点文件，通过
`e1a.workload.backgroundWorkUnits/canaryWorkUnits` 固定同一外生工作量；
`e1a.formal.parallelism` 不能超过专属 worker 数。`e1a.pinning.workerPcpuSets`
必须为每台 worker 提供一组互不重叠的 pCPU。

先跑 pilot 并保留同一集群，审核数据后继续 formal：

```bash
python3 executeE1aExperiment.py \
  --config research_configs/configE1aMarginalPods.yaml \
  --run-dir experiments/e1_marginal_pods/<run_id> \
  --phase pilot \
  --keep-cluster

python3 executeE1aExperiment.py \
  --config research_configs/configE1aMarginalPods.yaml \
  --run-dir experiments/e1_marginal_pods/<run_id> \
  --phase formal \
  --reuse-cluster
```

离线重算：

```bash
python3 analyzeE1aExperiment.py experiments/e1_marginal_pods/<run_id>

python3 validateE1aResults.py \
  experiments/e1_marginal_pods/<run_id> \
  --check-cleanup
```

实验目录保存 `config.yaml`、`design_frozen.md`、环境快照、负载源码/二进制、pilot 标定、冻结点序、逐点原始记录、聚合 CSV、拐点 JSON、PNG/PDF 曲线、中文报告和清理清单。正式结果默认五个独立 block；只有 pilot 或少于五个 block 时必须标作探索性结果。

## 状态语义

- `success`：计数守恒，canary 和背景工作均达到预设按时率。
- `workload_overload`：计数仍有效，但出现迟到、丢弃或结束积压；这是 E1-A 的研究结果。
- `admission_failed`：真实 kubelet/CNI/资源准入未在上限内完成。
- `harness_invalid`：日志数量、任务计数或测量链路不完整。
- `infrastructure_failure`、`safety_stopped`：基础设施失败或预先定义的安全条件停止。

CPU 饱和本身不会触发安全停止。分析始终保留全部有效点；分段拟合不明确时报告“无明确拐点”。
