# 0918E1 工作记录

本文件只记录本目录的实现与验证；实际矩阵的逐命令、原始输出和结果由每个
`results/run-*/matrix.json`、`vm-as-*.log` 及子目录保存。

## 2026-09-18

- 明确实验口径：m AS = 2m routers = m 个单向并发 iperf pair；共同 IX100；默认 RS peering。
- 固定 10 台同规格 VM：4 vCPU / 8 GiB / 40 GiB，规模 60..150、步长 10。
- 添加参数化编译、libvirt provisioning、VM 侧采集、并行矩阵与曲线/拐点分析脚本。
- 添加 `example.yaml`，使 2 AS smoke topology 可由 `seedemu/testing` 标准流程验证。
- 所有编译缓存、VM 状态和实验结果路径均限制在 `0918E1/` 下。
- `python3 executeVmMatrix.py --compile-only --compile-workers 4`：60..150 AS 十个规模均编译通过。
- `python3 provisionVmMatrix.py`：创建并启动 `e1-0918-as060`..`e1-0918-as150`，每台 4 vCPU / 8 GiB / 40 GiB。
- Docker Hub 拉取超时后，改为从宿主机缓存导出并向各 VM 预载 `handsonsecurity/seedemu-router:2.0`。
- 将编译结果优化为共享 `0918e1-router-runtime:latest`，避免每个路由器重复构建镜像。
- 定位并修复两项启动问题：Docker 29 与 Compose v1 创建容器卡住；IX bridge 网关与 RS 同占 `.1`。VM 自动安装 Compose v2，RS 固定到 `10.100.0.254`。
- 60 AS 短验证：121 个容器全部启动，60/60 iperf 成功，汇总 PASS，清理成功。
- `run-20260918-152503`：60..140 AS 完成；150 AS 的 SSH 控制连接在高负载下中断。该批保留为辅助重复性证据。
- VM 作业改为脱离 SSH 的后台运行并以退出码文件轮询，结果下载加入重试。
- `run-20260918-153956`：60..130 AS 完成 3 次重复；140 AS 在第 3 次、150 AS 在第 1 次以 137 被系统杀死；两台 VM 的实验容器均已恢复清理。
- 分析结论：完整规模内每对吞吐约 99.96–99.98 Mbps，总吞吐随规模线性增长，未观察到带宽/RTT 性能拐点；硬容量边界位于 130 完整成功与 140 首次失败之间。
- `python3 analyzeVmMatrix.py --run-dir results/run-20260918-153956`：生成 CSV、JSON、Markdown 与 PNG 曲线，并将失败规模从性能曲线排除。
- 按用户修正移除 iperf3 `-b 100M` 和 `traffic.targetMbpsPerPair`；跨机流量反事实估算改用实测总吞吐。
- 2 AS 不设速 smoke：TCP 平均每对约 11.3 Gbps、合计约 22.6 Gbps，确认没有 100 Mbps 限制。
- `run-20260918-162011`：60..130 AS 均完成 3 次不设速重复，所有规模的 iperf 成功对数等于 AS 数。
- 不设速曲线在 120 AS 首次同时越过平均每对带宽下降 10% 与平均 RTT 上升 20% 门限；总吞吐仍持续增长。
- 140 AS 首轮一个 Docker exec 在原 75 秒窗口内超时；150 AS 以 137 被系统杀死。两台 VM 均已清理实验容器。
- 后续运行将 `traffic.commandGraceSeconds` 设为 300，避免满载 VM 的调度延迟被误判为网络失败。

## 2026-09-19

- 以新矩阵替换实验 VM：11 台统一为 16 vCPU / 32 GiB / 40 GiB，规模为 100..300、步长 20。
- 精确销毁旧 10 台 4 vCPU / 8 GiB VM 及其 qcow2/cloud-init；历史结果和专用 libvirt 网络保留。
- 为 300 AS 扩展 IX 到 `10.100.0.0/23`，RS 使用 `10.100.1.254`；AS 内 `/29` 改用整数地址递增，消除第 257 个 AS 的 IPv4 八位组越界。
- `python3 executeVmMatrix.py --compile-only --compile-workers 4`：100..300 AS 十一个规模全部编译通过。
- `python3 provisionVmMatrix.py`：11 台新 VM 全部创建并完成 cloud-init/Docker 就绪检查；libvirt XML 验证为 16 vCPU / 33554432 KiB。
- `run-20260918-235440`：11 台 VM 并发执行，100..300 AS 全部完成 3 次不设速 TCP 重复并回传结果，批次状态 PASS。
- 自动分析在 100..300 AS 未检测到配置的带宽/时延拐点或完整运行边界；总吞吐总体增至约 871.2 Gbps。
- 300 AS 的平均 RTT 中位数升至约 0.238 ms、丢包约 1.22%，三次 iperf 成功对数为 282/288/297，显示尾部可靠性退化。
- 100..280 AS 的 RS BGP 会话全部 Established；300 AS 为 296/300，缺少 4 条会话。
- 实验结束后逐台确认 Docker 容器数和自定义网络数均为 0；11 台 VM 保持运行，供后续实验复用。
- `seedemu/testing` 标准 smoke compile 使用更新后的生成器通过，产物位于 `results/validation/example-16cpu-32g`。

## 2026-09-19：60–160 AS、10 次重复复核

- 将当前复核矩阵改为 60、80、100、110、120、130、140、150、160 AS；继续复用
  16 vCPU / 32 GiB / 40 GiB 的前九台 VM，保持每个 AS 一对不设速 TCP iperf。
- 为每个规模增加全 pair、5 秒并发 iperf preflight；只有 preflight 全部通过才进入正式
  重复。新增 `trafficQuality`、`iperfComplete` 和失败 ASN 字段，避免把空输出误当作
  成功流。
- `python3 executeVmMatrix.py --compile-only --compile-workers 4`：9 个规模全部编译通过。
- `python3 executeVmMatrix.py --run-only`：批次 `run-20260919-003418`，9 台 VM 并发，
  9/9 preflight PASS，9/9 VM 作业返回 PASS，所有规模各尝试 10 次正式重复。
- 60、80、100、110、140 AS 完成 10/10；120、130、150、160 AS 分别完成 8/10、
  9/10、9/10、9/10。缺失 iperf 对均为 return code 0 但空 JSON，解析错误为
  `invalid JSON: Expecting value`；所有重复 ping 丢包为 0%，BGP 检查均为全 Established。
- 120 AS 被自动分析标为首个局部吞吐拐点：110→120 总吞吐下降约 8.2%，VM CPU
  均值约 93.7%；但 60–160 AS 的平均每对带宽和 RTT 整体仍呈振荡，不是单调容量曲线。
- 分布分析：60 AS 第 7 次带宽为 IQR 异常；120/130 的 RTT 分别在第 3/9 次为 IQR
  异常；100 AS 的重复带宽标准差最高但无单点离群。结果写入 `results/RESULTS.md`、
  `results/run-20260919-003418/curve_report.md` 和 `scale_metrics.csv`。
- 逐台 SSH 检查 192.168.191.60–.68：Docker 容器数和自定义网络数均为 0；VM 保留运行。
- `python3 -m py_compile 0918E1/*.py` 和标准 smoke compile 均通过。
- 复核拓扑代码确认 iperf 目标是同 AS `pairnet` 上的 peer 地址，IX/RS 不在该数据路径；
  因此 AS 数增加不等于单条路径增加跳数，RTT 非单调是拓扑口径与 CPU/调度竞争共同导致。

## 2026-09-19：共享 Sink VM 跨 VM 流量复核

- 按用户调整实验口径：取消 IX 中心转发容器，所有 Border 容器通过各源 VM 的
  macvlan 直接接入共享 libvirt L2 网络 `seedemu-0918e1-xvm`，目标为 Sink VM
  `10.250.254.254`。每个 AS 使用独立 Sink 端口；IX route-server 只保留控制面。
- 当前配置固定为 16 vCPU / 32 GiB VM，规模 60、80、100、110、120、130、140、150、
  160 AS，每个规模 10 次、不设速 TCP、30 秒正式负载，5 秒全 pair preflight。
- 为满足“先证明 iperf 对可用”，`runGuestExperiment.py` 增加 `--preflight-retries`；
  当前 YAML 配置为 2。失败 pair 会按 attempt-01/02/03 保存原始 JSON，最终摘要记录
  `failedAsns`、`recoveredAsns` 和每次尝试的成功数，不会将持续失败静默算成成功。
- 修改执行器将 Sink VM 配置为每个 pair 启动一个 `iperf3 -s -D`，并按源 VM 的
  `sourceIndex` 分配端口区间；实验结束清理 Sink iperf 进程和临时地址。外部 Docker
  macvlan 网络仍在 VM 侧按源 VM 创建并在 `finally` 中删除。
- 验证命令：

  ```text
  python3 -m py_compile 0918E1/*.py
  python3 0918E1/loadExperiment.py 0918E1/experiment.yaml
  python3 0918E1/executeVmMatrix.py --compile-only --compile-workers 4
  ```

  三项均通过；9 个规模的 topology/Compose 编译缓存可复用。
- 交叉 VM smoke 批次 `run-20260919-vm-sink-smoke2`：AS60 单 VM 60/60 preflight、
  10/10 重复，平均每对约 155.30 Mbps、平均 RTT 约 9.954 ms，证明 Sink VM 和共享
  macvlan 路径可工作。
- 首次完整并发批次 `run-20260919-vm-sink-full3`：60–150 AS 通过；AS160 的 26/160
  pair 报 `No route to host`。修正预检端口监听检查和字段提取后，Sink 可确认启动
  1050 个监听端口；失败不是 Sink 缺少端口。
- 启动 AS160 隔离批次：

  ```text
  python3 0918E1/executeVmMatrix.py --run-only --only-as 160 --skip-build \
    --run-id run-20260919-vm-sink-as160-isolated
  ```

  结果为 160/160 preflight、10/10 正式重复，平均每对 44.82 Mbps、总吞吐
  7171.67 Mbps、平均 RTT 6.483 ms、丢包 0.08%。因此 AS160 拓扑和单 VM iperf 对
  正常，失败只在多源 VM 共同使用 Sink/L2 时出现。
- 增加 preflight 重试后执行最终并发批次：

  ```text
  python3 0918E1/executeVmMatrix.py --run-only --skip-build \
    --run-id run-20260919-vm-sink-full4
  ```

  60–150 AS 均一次 preflight 通过并完成 10/10 正式重复。AS160 第 1 次仍为 134/160，
  第 2、3 次对剩余 26 对均为 0/26，未出现恢复型 pair，因此该规模被标记为并发
  硬失败，不进入有效带宽/时延曲线。
- `python3 analyzeVmMatrix.py --run-dir results/run-20260919-vm-sink-full4` 生成
  `scale_metrics.csv`、`curve_summary.json`、`curve_report.md` 和 `curve.png`。有效
  曲线显示 60–150 AS 平均每对约 5–6 Mbps、负载 RTT 约 8 ms、丢包约 6%，总吞吐
  随 pair 数增加但有震荡；未观察到单调带宽下降或 RTT 单调上升，160 AS 首先表现为
  pair 建连失败。
- 清理核验：源 VM `.60`–`.68` Docker 容器/外部网络均为 0；Sink `.69` 的 iperf3
  进程为 0，`10.250.254.254` 临时地址已删除；VM 本身保留运行。

## 2026-09-20：资源 profile 边际收益建模实验

- 用户确认继续执行“把 AS/pod 共置到 VM 后，比较避免跨机流量与 VM 内性能代价”的实验。
  保持 Border→Sink、不经过 IX 中心转发容器、TCP iperf 不加 `-b`；每个规模目标
  10 次、每次 30 秒，并统一 Sink 为 32 vCPU / 64 GiB。
- 新增 `applyVmResources.py`，只按 `experiment.yaml` 中声明的域名修改 vCPU/内存；
  新增 `profileSweep.yaml` 和 `runProfileSweep.py`，可以按 profile 自动切换资源、
  执行矩阵、分析结果。为控制重跑增加 `--rerun-existing`。
- `runGuestExperiment.py` 增加 CPU steal、CPU/memory PSI、可用内存和 swap 采样；
  `analyzeVmMatrix.py` 将这些字段写入 `scale_metrics.csv`。
- 实际执行命令：

  ```text
  python3 0918E1/runProfileSweep.py --only-profile p08m16 \
    --run-prefix run-20260919-benefit
  python3 0918E1/runProfileSweep.py \
    --only-profile p08m32 --only-profile p16m16 --only-profile p32m64 \
    --run-prefix run-20260919-benefit
  python3 0918E1/runProfileSweep.py --only-profile p16m32 \
    --rerun-existing --run-prefix run-20260919-benefit-control
  python3 0918E1/runProfileSweep.py --only-profile p08m32 \
    --run-prefix run-20260920-benefit-recheck
  ```

- 完整 profile 结果：
  - `p08m32`（补跑最终采用）：AS60–140 为 10/10；AS150 为 130/150
    preflight、AS160 为 154/160，剩余 pair 三次均失败；最后完整规模 140。
  - `p08m16`：AS60–150 为 10/10；AS160 为 134/160，剩余 26 对重试失败。
  - `p16m16`：AS60–150 为 10/10；AS160 为 134/160，剩余 26 对重试失败。
  - `p16m32`（统一 Sink 的控制重跑）：AS60–150 为 10/10；AS160 为
    134/160，剩余 26 对重试失败。
  - `p32m64`：AS60–150 为 10/10；AS150 首次预检 137/150、第二次 13/13
    恢复；AS160 为 148/160，剩余 12 对重试失败。
- 关键局部退化：p16m16 的总带宽 AS130=807.67 Mbps、AS140=671.13 Mbps；
  p16m32 的 AS120=759.21 Mbps、AS130=698.94 Mbps；p08m32 的 AS140=917.04
  Mbps 后 AS150 进入 preflight 硬失败。各曲线仍有震荡，不能拟合为严格单调下降。
- 新增 `modelPlacementBenefit.py`：输入多个 `profile=runDir`，计算
  `weightedAvoidedCrossVmMbps`、总带宽/平均带宽/RTT/丢包/CPU/内存边际变化和
  可配置惩罚分数；不完整重复或硬失败规模不产生可用边际分数。默认模型和显式
  5 Mbps/AS 模型分别写入：

  ```text
  python3 0918E1/modelPlacementBenefit.py \
    --run p08m32=results/run-20260920-benefit-recheck-p08m32 \
    --run p08m16=results/run-20260919-benefit-p08m16 \
    --run p16m16=results/run-20260919-benefit-p16m16 \
    --run p16m32=results/run-20260919-benefit-control-p16m32 \
    --run p32m64=results/run-20260919-benefit-p32m64 \
    --out-dir 0918E1/results/benefit-profiles-final

  python3 0918E1/modelPlacementBenefit.py ... \
    --default-demand-mbps 5.0 \
    --out-dir 0918E1/results/benefit-profiles-demand5
  ```

- 新增 `weights.template.csv`，支持后续按 ASN 填入 `weight,demandMbps`；分析器忽略
  空行/注释行。修正 `modelPlacementBenefit.py`：安全判断要求完整 10/10，分开输出
  连续安全边界、最后安全规模和非单调 `unsafeScales`，失败规模的边际分数为 NA。
- 验证：

  ```text
  python3 -m py_compile 0918E1/*.py
  python3 0918E1/loadExperiment.py 0918E1/experiment.yaml
  python3 0918E1/modelPlacementBenefit.py --run p16m32=results/run-20260919-benefit-control-p16m32 \
    --weights 0918E1/weights.template.csv --default-demand-mbps 5 \
    --out-dir 0918E1/results/benefit-weight-smoke
  ```

  均通过。最终清理核验：`.60`–`.68` 的 Docker 容器数和外部网络数均为 0；`.69`
  的 iperf3 进程为 0，`10.250.254.254` 临时地址为 0。
- 将 `profileSweep.yaml` 的 p16m32 `existingRun` 指向统一 Sink 配置的
  `run-20260919-benefit-control-p16m32`，并再次执行 `py_compile`、配置加载和
  profile dry-run；均通过。补跑结束后宿主机 load average 已回落至约 2.87，
  无 D 状态进程。
