# 0918E1：星型拓扑共享 Sink VM 饱和实验

## 当前实验（2026-09-19）

当前主批次为 `results/run-20260919-vm-sink-full4`：9 台 16 vCPU / 32 GiB
源 VM 并发承担 60、80、100、110、120、130、140、150、160 AS，另用一台同配置
VM 作为 Sink。每个 AS 对应一个 Border 到 Sink 的默认 TCP iperf3 对，持续 30 秒，
重复 10 次。所有 Border 容器直接通过共享 libvirt L2 网络
`seedemu-0918e1-xvm` 的 macvlan 接入 Sink `10.250.254.254`；IX route-server
只保留为拓扑/BGP 控制面，不参与数据面转发，也没有中心 IX 转发容器。

60–150 AS 的全 pair 预检和 10/10 正式重复均通过。160 AS 在 160 对中只有 134
对能建立连接，剩余 26 对经两次重试仍报 `No route to host`，因此不把 160 AS
填入有效曲线。相同 AS160 拓扑单独运行时 160/160 预检、10/10 重复均通过，说明
异常由 9 个源 VM 同时占用共享 L2/Sink 条件触发。汇总和异常分析见
`results/RESULTS.md`。

60–150 AS 的平均每对带宽约 5–6 Mbps，负载下 RTT 约 8 ms，曲线仍有震荡而非
单调下降/上升；共享 Sink 的直接表现是总吞吐增加、丢包约 6%，并在 160 AS
出现 pair 建连硬失败。这是并发共享网络的容量异常，而不是现有 pair 路径跳数
变长。下面的“最新复核轮次”是历史的同 AS `border -> peer` 口径，不与当前
Sink VM 曲线混合。

## 资源 profile 边际收益实验（2026-09-20）

为了估算“把更多 AS/pod 放入同一 VM 能减少多少跨机流量、又付出多少 VM 内性能代价”，
新增了五组源 VM profile；Sink VM 统一为 32 vCPU / 64 GiB，所有规模仍为
60、80、100、110、120、130、140、150、160 AS，每个规模目标 10 次重复、每个
Border 到 Sink 使用不设速 TCP iperf3。只有 preflight 全通过且正式重复达到 10/10
才进入完整性能曲线。

| Profile | 源 VM | 10/10 最后成功规模 | 首个硬失败 | 总带宽峰值（AS） | 结果 |
| --- | --- | ---: | ---: | ---: | --- |
| p08m32 | 8 vCPU / 32 GiB | 140 | 150 | 917.04 Mbps（140） | `run-20260920-benefit-recheck-p08m32` |
| p08m16 | 8 vCPU / 16 GiB | 150 | 160 | 817.24 Mbps（150） | `run-20260919-benefit-p08m16` |
| p16m16 | 16 vCPU / 16 GiB | 150 | 160 | 807.67 Mbps（130） | `run-20260919-benefit-p16m16` |
| p16m32 | 16 vCPU / 32 GiB | 150 | 160 | 763.60 Mbps（150） | `run-20260919-benefit-control-p16m32` |
| p32m64 | 32 vCPU / 64 GiB | 150 | 160 | 811.89 Mbps（150） | `run-20260919-benefit-p32m64` |

结果显示“硬可运行边界”和“吞吐拐点”是两个不同指标。p16m16 在 AS130 达到
807.67 Mbps 后，AS140 降至 671.13 Mbps；p16m32 在 AS120→130 总带宽下降
60.27 Mbps 后又恢复；各 profile 的带宽均存在震荡，不能假设单调曲线。AS160
在五组 profile 均未通过共享 L2/Sink preflight（失败为 `No route to host`），
因此不填入有效带宽/时延曲线。p32m64 的 AS150 首次预检有 13 个失败，但第二次
预检全部恢复并完成 10/10；这类“重试恢复”会记录在 `summary.json`，不能和持续
失败混淆。

边际收益模型位于 `results/benefit-profiles-final/`，默认把每个成功 AS 的实测
Sink 吞吐作为“若不共置则可能产生的跨机流量”代理，并可用
`--default-demand-mbps` 或 `--weights` 改成业务需求/网络权重。权重模板为
`weights.template.csv`。模型对不完整重复和硬失败规模不计入可用边际分数，同时
输出独立的 `unsafeScales`，避免把非单调噪声误报成唯一边界。

## 历史：同 AS pairnet 复核（2026-09-19）

历史批次为 `results/run-20260919-003418`：9 台 16 vCPU / 32 GiB VM 并发承担
60、80、100、110、120、130、140、150、160 AS；每个 AS 对应一个
`border -> peer` 的默认 TCP iperf3 对，持续 30 秒，重复 10 次。每个规模正式运行
前还会并发执行一次 5 秒全 pair iperf preflight；9 个规模均为 `PASS`，说明启动时
所有配置的容器、路由器和 iperf 路径都可用。详细结果见
`results/RESULTS.md` 和该批次的 `curve_report.md`。

正式重复中，60、80、100、110、140 AS 达到 10/10 完整重复；120、130、150、160
AS 分别为 8/10、9/10、9/10、9/10。缺失对均是进程返回 0 但输出为空，解析为
`invalid JSON: Expecting value`；同一批次 ping 无丢包，preflight/BGP 均通过，因而更
像高 CPU 负载下 Docker exec/iperf 控制输出调度异常，而不是基础拓扑在启动时失效。
这些规模被标记为质量降级，不能把中位数当成“10 次完整重复”的结论。

自动规则在 120 AS 报告首个吞吐拐点：110→120 的总吞吐下降约 8.2%，同时 CPU 均值
约 93.7%。但平均每对带宽和 RTT 均不单调，故这不是已经证明的平滑容量曲线；它是
在高 CPU 条件下的首个局部退化点。所有规模实验容器和自定义网络均已清理，VM
保留运行以便复测。

注意：iperf 的 `border -> peer` 目的地址位于同一 AS 的 `pairnet`，不经过 IX route
server。增加 AS 增加的是并发 pair 和容器/进程数，不是现有 pair 的路径跳数；因此
RTT 不会仅由拓扑规模决定性地单调上升。

## 当前拓扑与测量口径

每个规模使用一台独立、同规格 VM（16 vCPU、32 GiB 内存、40 GiB 磁盘）。拓扑是共同
IX100 的星型结构；每个 AS 有 `border` 与 `peer` 两台路由器，`border` 再接入 IX，
并默认与 route server 建立 eBGP RS peer。当前数据面由 `border` 直接向远端 Sink
VM 的 `10.250.254.254` 发起 iperf，Sink VM 为每个源 AS 监听独立端口；流量经过
共享的 libvirt L2/macvlan，不经过 IX route-server 容器。

每组 iperf 使用默认的不设速 TCP（不传 `-b`），持续 30 秒；同一规模并行运行 10 次。
iperf 负载期间，同一批 pair 并行 ping，因此 RTT 是负载下时延。VM 内直接采样
`/proc/stat`、`/proc/meminfo` 和 `/proc/net/dev`。

主要结果口径：

- 平均每对带宽：所有成功 iperf 对的平均接收吞吐。
- 总带宽：所有成功 iperf 对接收吞吐之和。
- 平均/p95 时延：所有 pair 在 iperf 负载期的 ping RTT 样本。
- VM CPU/内存：负载阶段的均值、p95/峰值。
- 估算减少跨机流量：成功 iperf 对的实测总吞吐；是假设这些流若不共置都会跨机的
  反事实估计，不是物理交换机或 NIC 的直接节省量。

`analyzeVmMatrix.py` 的默认拐点规则写在 `experiment.yaml`：相对前两个低规模基线，
平均每对带宽下降至少 10% 且平均 RTT 增加至少 20%；或者总带宽较上一规模下降至少
5% 且 VM 平均 CPU 不低于 85%。分布异常采用每规模重复值的 Tukey 1.5×IQR 规则。

## 文件角色

| 文件 | 作用 |
| --- | --- |
| `experiment.yaml` | 当前规模、10 次重复、preflight、VM 资源、SSH 与分析门限。 |
| `createStarTopology.py` | `python3 createStarTopology.py m` 生成 m AS / m pair 的 Compose。 |
| `provisionVmMatrix.py` | 创建或检查 libvirt NAT 网络和同规格 VM。 |
| `executeVmMatrix.py` | 编译各规模，上传并在 VM 上并行执行，再下载结果。 |
| `runGuestExperiment.py` | preflight、并发 iperf/ping、CPU/内存采集和单规模汇总。 |
| `analyzeVmMatrix.py` | 汇总 CSV/JSON/Markdown/PNG，计算分布和拐点。 |
| `destroyVmMatrix.py` | 以显式参数清理本实验 VM、磁盘和网络。 |
| `builds/` | 生成缓存，不纳入结果曲线。 |
| `state/` | cloud image、qcow2、cloud-init 与 libvirt network XML。 |
| `results/` | 每批原始数据、日志、汇总和曲线。 |

## 复现实验

```bash
# 只编译当前 9 个规模，不访问 VM
python3 executeVmMatrix.py --compile-only --compile-workers 4

# 并发运行当前矩阵；默认完成后 compose down
python3 executeVmMatrix.py --run-only

# 对已有批次重新生成汇总、曲线和分布表
python3 analyzeVmMatrix.py --run-dir results/run-20260919-vm-sink-full4
```

标准 smoke compile：

```bash
python3 ../../../seedemu/testing/cli.py compile example.yaml \
  --artifact-dir results/validation/example-compile
```

执行器以后台作业和哨兵文件回报退出码，高负载时 SSH 短暂不可用不会直接终止实验；
结果下载会重试。实验容器/网络清理在 VM 侧完成，VM 本身不会被自动销毁。

## 历史批次

此前的 16 vCPU / 32 GiB、100..300 AS、3 次重复不设速批次保留在
`results/run-20260918-235440`；旧的 60..160 AS 同 AS `pairnet` 批次为
`results/run-20260919-003418`。早期 4 vCPU / 8 GiB 和限速/不设速批次也保留在
`results/` 中，不与当前共享 Sink VM、10 次重复主结论混用。
