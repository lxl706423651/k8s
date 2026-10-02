# 面向 Pod 放置的边际收益模型

这个模型回答一个局部决策问题：

> 将 Pod 集合 \(S\) 放到候选 VM \(h\) 后，减少了多少重要通信的跨 VM 流量？同时给目标 VM 增加了多少 CPU、内存和网络争用？两者合在一起是否值得这次共置？

模型只使用部署前可以得到的信息来计算候选放置的特征；实测吞吐、RTT、交付率和任务按时率用于标定系数和做独立验证。它不会改变 Kubernetes、CNI、内核或路由器配置，也不把“Pod 数相同”当作目标。

## 1. 通信收益

对每条逻辑网络流 \(e=(u,v)\)，提前给出 offered rate \(r_e\) 和重要性权重 \(w_e\)。一个放置 \(\pi\) 下的加权跨 VM 流量是：

\[
C(\pi)=\sum_{e\in E} w_e r_e I[\pi(u)\ne\pi(v)].
\]

把 Pod 集合 \(S\) 从当前放置 \(\pi\) 加入候选 VM 后，通信边际收益为：

\[
\Delta C(S\rightarrow h)=C(\pi)-C(\pi_{S\rightarrow h}).
\]

`weightedCrossMbpsSaved` 就是这个量，单位是“重要性加权 Mbps”。如果某条网络比另一条重要两倍，只需把它的 `importanceWeight` 设为 2；模型不会把所有边当成等价边。

模型还保留未加权的跨机 Mbps、跨机边数、加权跨机比例和每条流的贡献，方便解释一次 move 究竟节省了哪些通信。

## 2. VM 争用代价

每个 VM profile 至少提供 CPU 容量、内存容量和网络容量：

\[
u^{cpu}_h=\frac{L^{cpu}_h}{K^{cpu}_h},\quad
u^{mem}_h=\frac{L^{mem}_h}{K^{mem}_h},\quad
u^{net}_h=\frac{L^{net}_h}{K^{net}_h}.
\]

在 profile 的 knee 以上使用软惩罚：

\[
\Phi_h =
s^{cpu}_h [u^{cpu}_h-\kappa^{cpu}_h]_+^2+
s^{mem}_h [u^{mem}_h-\kappa^{mem}_h]_+^2+
s^{net}_h [u^{net}_h-\kappa^{net}_h]_+^2.
\]

其中 \(s\) 是不同 VM 性能画像的敏感度，\(\kappa\) 是通过 E1-A 或独立标定实验得到的 knee。模型同时输出每个 VM 的最大利用率和均值，因此可以区分“整体负载增加”和“一个 VM 被集中压满”。

Pod 的 CPU、内存和网络需求可以分别加权：

```json
{
  "pod-a": {
    "cpuCores": 0.20,
    "memoryMiB": 128,
    "networkMbps": 12,
    "cpuWeight": 1.0,
    "memoryWeight": 2.0
  }
}
```

这表示 Pod 的内存压力在模型中的重要性是普通 Pod 的两倍。权重用于建模优先级，不会替代 Kubernetes 的实际资源请求和准入检查。

## 3. 性能响应

模型不把所有性能指标预先合成一个不可解释的分数，而是分别拟合以下响应：

- TCP aggregate goodput；
- UDP 接收速率和交付率；
- UDP/TCP 背景任务按时率；
- UDP/TCP canary P99 RTT；
- 后续可加入 PSI、run queue 或丢包率。

每个响应都是相对基线的变化。例如：

\[
\delta T = \frac{T(\pi_{S\rightarrow h})}{T(\pi)}-1.
\]

吞吐、交付率和按时率的正值表示改善；RTT 在写入响应时反向取符号，因此正值也表示改善。这样既可以看到“总带宽变好了但任务服务质量变差”的情况，也可以看到平均流速、尾延迟和任务按时率之间的冲突。

当前实现用带岭项的线性模型作为第一版：

\[
\hat y_m =
\beta_{m,0}+
\beta_{m,C} C+
\beta_{m,CPU}\Phi^{CPU}+
\beta_{m,MEM}\Phi^{MEM}+
\beta_{m,NET}\Phi^{NET}+
\beta_{m,\max CPU}u^{cpu}_{max}+
\beta_{m,\max MEM}u^{mem}_{max}+
\beta_{m,\max NET}u^{net}_{max}.
\]

它的作用是给出可审计的局部估计，不是声称真实系统一定服从线性关系。输出包含样本数、参数数、训练误差和留一法误差；当样本不足时会明确标记 `underDetermined`。

## 4. 净边际分数

最终由实验者指定通信目标和性能目标的权重：

\[
\text{Score}(S\rightarrow h)
=\lambda_C\Delta C
+\lambda_P\sum_m q_m\Delta y_m.
\]

`q_m` 可以把 UDP 交付率、TCP goodput、P99 RTT 和背景按时率放在同一个研究问题里；不同论文实验可以使用不同的 \(q_m\)，而原始响应仍然保留。正分表示在当前权重下候选放置更有利。

分数只用于排序和边界判断，不能越过硬约束。候选放置仍然必须满足实际 kubelet/CNI 准入上限、内存可用量、镜像和网络配置等条件。

## 5. 现有 E1-B 回放

下面的命令使用冻结的 214-Pod E1-B 映射、16 条固定流和正式测量结果：

```bash
python3 marginalBenefitModel.py replay-e1b \
  --output-dir experiments/e1_marginal_pods/20260920_marginal_model/e1b_replay
```

可以用一个 JSON 或 CSV 指定网络重要性。例如：

```json
{
  "default": 1.0,
  "flowWeights": {
    "e1b-00-7b3d574f3b90a46f-a": 3.0
  },
  "stratumWeights": {
    "ebgp_ix_peer": 2.0
  }
}
```

回放产出：

- `marginal_benefit_dataset.json`：所有映射的通信、VM 资源和观测指标；
- `marginal_benefit_model.json`：每个性能响应的系数和误差；
- `marginal_benefit_replay.png`：通信收益、争用特征和动态响应曲线；
- `marginal_benefit_report_zh.md`：数据边界和解释。

对一个候选 move 的离线评分可以复用同一个模型：

```bash
python3 marginalBenefitModel.py evaluate \
  --input research_configs/marginalBenefitExample.json \
  --model experiments/e1_marginal_pods/20260920_marginal_model/e1b_replay_v2/marginal_benefit_model.json \
  --profile-overrides experiments/e1_marginal_pods/20260920_vm_profile_calibration_analysis_v3/node_profile_overrides.json \
  --output experiments/e1_marginal_pods/20260920_marginal_model/example_score.json
```

输入的 `nodes` 可以只写 `"profile": "cpu-weak"`，再由
`node_profile_overrides.json` 补充容量、knee 和 provisional sensitivity；也可以显式给出
节点容量来覆盖 profile 默认值。

这组 E1-B 数据只有一个 VM 规格（4 vCPU/8 GiB），所以只能验证“特征能够解释当前局部性梯度”，不能直接当成跨规格模型。

## 6. 不同 VM profile 的标定矩阵

要估计 VM 性能的影响，至少需要三类 profile，并在相同工作负载、相同 Pod 资源和相同流权重下重复：

| profile | CPU | 内存 | 目的 |
|---|---:|---:|---|
| `cpu-weak` | 2 vCPU | 8 GiB | 放大 CPU 争用 |
| `balanced` | 4 vCPU | 8 GiB | 与 E1-A/E1-B 对照 |
| `cpu-strong` | 8 vCPU | 8 GiB | 检查 CPU 容量是否推迟 knee |
| `memory-weak` | 4 vCPU | 4 GiB | 放大内存压力 |
| `memory-strong` | 4 vCPU | 16 GiB | 检查内存容量的边界 |

每个 profile 使用同一组局部负载点，例如 `N={0,20,40,60,80,100}`，至少 3 次重复；每次先做空闲对照，再逐步加入固定工作 Pod。需要保存：

- VM 规格、实际 allocatable CPU/内存和 kubelet Pod 准入上限；
- 每个 Pod 的 CPU、内存和网络需求；
- 节点 CPU、memory PSI、run queue、网络速率；
- TCP/UDP goodput、平均/尾延迟、交付率和任务按时率。

由这些点分别估计 `cpuSensitivity`、`memorySensitivity`、`networkSensitivity` 和三个 knee。E2 的训练集和 E3 的拓扑/工作负载必须分开，不能用 E3 的实测结果反向调参。

本轮 pilot 已实际完成四个 profile 的短扫描：

| profile | 结果 |
|---|---|
| 2 vCPU/8 GiB | N=0 正常，N=20 起稳定过载 |
| 4 vCPU/4 GiB | N=0–40 正常，N=60 起稳定过载 |
| 8 vCPU/8 GiB | 测试到 N=80 没有过载，属于右删失观测 |
| 4 vCPU/16 GiB | N=0 基线已经无效，只保留审计记录，不进入系数拟合 |

这些结果说明 profile 需要进入模型，但它们仍是 pilot 级别的边界观测；正式建模应对每个有效 profile 做重复实验，并单独验证 CPU 与内存效应。

复现实验入口：

```bash
python3 runVmProfileCalibration.py \
  --profiles cpu-weak balanced cpu-strong memory-weak memory-strong \
  --phase all \
  --repetitions 3 \
  --output-dir experiments/e1_marginal_pods/20260920_vm_profile_calibration_formal

python3 analyzeVmProfileCalibration.py \
  --input-dir experiments/e1_marginal_pods/20260920_vm_profile_calibration_formal \
  --output-dir experiments/e1_marginal_pods/20260920_vm_profile_calibration_formal/analysis
```

## 7. 解释边界

如果一次 move 的 `weightedCrossMbpsSaved` 为正而 `weightedPerformanceDelta` 为负，说明它确实节省了重要跨机通信，但增加的争用代价抵消了局部性收益。若通信收益和服务质量都为正，则该 move 是稳健候选。若两者符号相反，应该报告 Pareto 前沿和不确定性，而不是通过修改权重制造“最优”结论。
