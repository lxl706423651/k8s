# B62 与 B63 中文运行说明

> 2026-09-14 后续更新：B62 当前 assignment 已改为 1078，显式单机配置改为
> 6 VM（298 vCPU、596 GiB），内存使用 `memoryGiB`，单机 wrapper 的目录规模
> 前缀已动态读取。单机入口现已只读 `configSingleHostKvmMacvlan.yaml`，不用再修改
> assignment；每轮的 assignment 是自动快照。本文下方部分 4954/30-VM 数值是之前配置；当前启动与 48-VM 切换
> 请以 [单机配置说明](b62_k8s_scale/configSingleHostKvmMacvlan.md) 为准。B63 已改为直接复用 B62 流程，当前入口见第 4 节和 [B63 README](b63_deployment/README.md)。

核对日期：2026-09-14。本文结合 [0720 优化设计](../0720k8sOptimizationScaleDesign.md)
与当前 `largeScale` 脚本编写；命令按当前代码核对，本文编写时没有实际启动实验。
以下目录均相对于仓库根目录。先进入本机仓库 `/home/lxl/k8s`，再执行对应的 `cd`。

## 1. 两个例子的区别

| 项目 | B62 | B63 |
| --- | --- | --- |
| 目的 | 大规模网络部署、路由与数据面验收 | 在大规模部署基础上评估 placement 优化 |
| 全流程入口 | `b62_k8s_scale/runFullExperiment.py` | `b63_deployment/runB63LargeExperiment.py` |
| placement | compile 时生成确定性的 by-AS hard placement | 复用 B62 compile，随后替换 placement，并验证实际落点 |
| 主要配置 | `assignment.yaml`，也可叠加显式单机/多机 cluster YAML | `configSingleHostKvmMacvlan.yaml`，包含 placement 算法字段 |
| 仿真数据面 | Multus secondary macvlan+VLAN | Multus secondary macvlan+VLAN |
| 静态结果 | `placement_plan.json` 等 | `output/placement_report.json` |
| 动态验证 | 阶段性能采样、路由/FIB、确定性跨节点或跨宿主 ping/iperf、reconvergence | 复用 B62 全部动态验证，增加逐 Pod 落点核对 |
| 成功后 | 保留集群，需要手动销毁 | 保留集群，需要手动销毁 |

B62 自带确定性的 by-AS placement。B63 的默认调度器可单独做静态基线，
共享主流程只接受硬 placement，以支持按节点镜像预加载。
另外，primary CNI 与仿真 secondary CNI 不同：B62 显式单机/多机配置的
primary 是 Flannel VXLAN，不能据此说仿真数据面不是 macvlan+VLAN。
B63 新输入使用 `kind: kvm` 和 `fabric.type: macvlan-vlan`，
不是使用 OVN/OVS 数据面。

## 2. 启动前准备

这些例子会创建真实 VM，不是轻量演示。先确认 CPU、内存、磁盘、IP/MAC、网络名
及 registry endpoint 没有冲突。资源充足时，两例可以使用独立 VM、网络、存储和运行目录并行运行。VM 配置内存不是宿主机全部内存需求，还要留出宿主系统、
QEMU、Docker 构建及文件缓存的空间。

控制端需要可工作的 `python3`/PyYAML、Docker/Buildx、libvirt/KVM、`virsh`、
`kubectl`、SSH、Ansible、Helm 等工具，以及所需的非交互 sudo 权限。
编译脚本优先使用 `$HOME/anaconda3/envs/seedpy310`；应确认该环境已安装本仓库
SeedEMU 所需依赖。不要把整套脚本改用 `sudo` 启动，否则 HOME、SSH 和 Conda
路径可能变化。

默认拓扑目录为 `$HOME/seed-emulator/topology`，需要：

```text
real_topology_4954.txt     # 下文 B62 示例
real_topology_1897.txt     # 下文 B63 示例
assignment.pkl
output_4954/              # 当前 B62 额外依赖的 canonical DockerCompiler 输出
```

**当前 B62 与 0720 文档的一个重要差异：** `compile.sh` 会调用
`adaptCanonicalOutput.py`，从 `experiment.canonicalOutputDir` 读取 canonical
构建上下文；未配置时取 `<topologyDir>/output_<规模>`。其中必须有
`docker-compose.yml`、对应节点的 Dockerfile、启动脚本和 BIRD/kernel 配置，
节点数及地址必须与本次拓扑一致。它不是本轮 K8s 的输出目录，也不是由本轮脚本
自动生成；缺失时需要先准备对应的 DockerCompiler 编译产物。
B63 现在直接复用这一步 canonical 覆盖；1078 示例需要 `output_1078/`。

本次只读检查确认上述两个拓扑文件、`assignment.pkl`、`output_4954` 目录和
`seedpy310` Python 存在，但没有逐项验证 canonical 内容或检查当前集群健康。
B62 单机配置指定的 `/data/lxl/k8sTools/base/jammy-server-cloudimg-amd64.img`
当前不存在；setup 支持从缓存复用或下载，但仍需确认目录权限、磁盘和网络可用。

## 3. 启动 B62

### 3.1 先选择基础设施配置

| 入口 | 当前规模 / VM 数 | VM 资源 | 配置来源 |
| --- | --- | --- | --- |
| `runExperiment.sh` | 4954 / 6 VM | 总预算 300 vCPU、600 GiB，每 VM 80 GB | `assignment.yaml`，当前 `workerCount: 5` |
| `runSingleHostMacvlanExperiment.sh` | 1078 / 6 VM，单宿主 | 合计 298 vCPU、596 GiB，每 VM 80 GB | 只需 `configSingleHostKvmMacvlan.yaml` |
| `runMultiHostMacvlanExperiment.sh` | 4954 / 30 VM，多宿主 | 同上，24 VM + 6 VM | `configMultiHostKvmMacvlan.yaml` |

当前单机配置是 master 48 vCPU、96 GiB，5 个 worker 各 50 vCPU、100 GiB；
多机仍是 master 48 vCPU、96 GiB，29 个 worker 各 8 vCPU、17 GiB。
master 也参与调度。`ens3`、`ens4` 是数据面 trunk，各支持 VLAN 1..4094，MTU 1400。
4954 拓扑有 4763 个 secondary network，不能只配置一条 VLAN trunk。

单机 wrapper 现在只读 `configSingleHostKvmMacvlan.yaml`：`workers.count` 决定 worker
数量，`experiment.topologySize` 决定规模，namespace 自动生成。每轮的 assignment
是自动生成的内部快照。多机 wrapper 仍使用 cluster YAML 加 `assignment.yaml` 的旧接口。
单机切换 48 VM / 4954 只需修改同一 YAML，见[配置指南](b62_k8s_scale/configSingleHostKvmMacvlan.md)。

多宿主配置目前指定 `amd=10.202.236.88` 为本地控制宿主，`idc=10.202.191.39`
为远端宿主。换环境时必须修改 IP、SSH 用户/密钥、工作目录与管理网段，并保证
远端免密 SSH/sudo 和 VXLAN underlay 可用，不能直接照搬这些地址。

### 3.2 完整启动命令

先选一个入口，**下面三个命令是三选一，不要连续执行**。
这些入口默认会先销毁对应生成配置所记录的旧集群，执行前必须核对配置归属。

```bash
cd largeScale/b62_k8s_scale

# A. 使用当前 assignment 的 6 VM 配置。
bash ./runExperiment.sh --assignment ./assignment.yaml

# B. 单宿主 30 VM 的 macvlan+VLAN 完整实验。
bash ./runSingleHostMacvlanExperiment.sh

# C. 多宿主 30 VM 的 macvlan+VLAN 完整实验。
bash ./runMultiHostMacvlanExperiment.sh
```

若要明确记录本轮目录，例如选择单宿主入口，可用下面这一条替代上面的 B：

```bash
RUN_DIR="$(pwd)/runs/4954_$(date +%Y%m%d_%H%M%S)_singlehost_macvlan_vlan"
bash ./runSingleHostMacvlanExperiment.sh "$RUN_DIR"
```

不传目录时两个 wrapper 也会自动生成规模+时间戳目录。
更换规模时修改 assignment 的 `experiment.topologySize`、namespace，准备对应
canonical 输出，并相应修改本轮目录名。两个 wrapper 的默认目录前缀写死了 4954，
不能仅修改 assignment 后还沿用这个目录名。
`--skip-existing-destroy` 只跳过旧集群销毁，**不是复用已有集群的开关**，后续仍会 build。

### 3.3 实际执行顺序与文件

| 阶段 | 文件 / 行为 |
| --- | --- |
| 准备配置 | 默认路线调用 `renderAssignmentConfig.py`；显式 cluster 路线跳过生成并保存输入快照 |
| 旧集群清理 | 默认路线 `destroyCluster.sh`；显式路线 `k8sTools.py destroy` |
| build-cluster | `k8sTools.py build`，创建 VM/K3s、registry 和 CNI；之后 `ensureAllNodesSchedulable.sh` |
| 多宿主预验证 | setup 验证 VLAN fabric；建好 K3s 后运行 `validateMultiHostK3s.py` |
| clean / preflight | `clean.sh`、`preflight.sh` |
| compile | `compile.sh` -> `seed_k8s_plan_real_topology_by_as.py` -> 仓库 `examples/kubernetes/real_topology_k3s_compile.py` -> `adaptCanonicalOutput.py` |
| build | `build.sh`，快速构建镜像并按 placement 预加载到目标节点 |
| deploy / wait-ready | `deploy.sh`、`wait-ready.sh`，准备 VLAN parent、分批部署并等待 Ready |
| start-bird / verify-bird | `start_bird.sh`、`verifyStartBird.sh` |
| start-kernel / verify-kernel | `start_bird_kernel.sh`、`verifyFibRoutes.sh` |
| 数据面验收 | `validateCrossHostWorkload.py`，单宿主 cross-node、多宿主 cross-host |
| reconvergence | `runReconvergence.sh` -> `measureReconvergence.py` |

数据面验收确定性选择共享 NAD 且位于不同节点/宿主的 router pair，检查 MTU、
双向 ping 和 TCP iperf3。这是连通性验收，不等价于 B63 多层次并发性能评估。
当前独立 `test.py` 并未自动执行；这不代表 BIRD/FIB 验证被跳过。

### 3.4 看状态和结果

单宿主入口生成 `configK3sSingleHostKvmMacvlan.yaml`、
`kubeconfigSingleHostKvmMacvlan.yaml`、`clusterSingleHostKvmMacvlan.inventory.yaml`。
多宿主入口对应替换为 `MultiHost`；默认 assignment 路线生成
`configK3s.yaml`、`kubeconfig.yaml`、`cluster.inventory.yaml`。

```bash
# 单宿主例子；RUN_DIR 使用本轮实际目录，不要指向旧实验。
kubectl --kubeconfig ./kubeconfigSingleHostKvmMacvlan.yaml get nodes -o wide
kubectl --kubeconfig ./kubeconfigSingleHostKvmMacvlan.yaml \
  -n seedemu-b62-mvlan-vlan-4954 get pods -o wide
jq '{status, cluster, stages}' "$RUN_DIR/summary.json"
```

查看 `placement_plan.json`、`canonical_output_summary.json`、
`performance_metrics.csv`、`performance_summary.json`、
`verify_after_start_bird_summary.json`、`verify_after_fib_write_summary.json`、
`cross_node_workload_summary.json`（多宿主为 `cross_host_workload_summary.json`）
及 `reconvergence_summary.json`。B62 没有统一使用 B63 的 `placement_report.json` 格式。

## 4. 启动 B63

新主入口只有 B63 的 placement 和落点检查，其余阶段直接调用 B62。
默认 `configSingleHostKvmMacvlan.yaml` 配置 1078 规模、6 VM，所有 VM 为
24 GiB，master 16 vCPU、五台 worker 各 8 vCPU；总计 56 vCPU / 144 GiB。
VM 名称为 `b63-sh6-*`，管理网段 `10.83.0.0/24`，使用两条独立 fabric 网络。

```bash
cd largeScale/b63_deployment
python3 runB63LargeExperiment.py \
  --config configSingleHostKvmMacvlan.yaml \
  --run-dir experiments/1078-6vm
```

可用 `--prepare-only` 先做无资源副作用的配置检查，检查与真实实验使用不同运行目录。
所有生成集群状态、kubeconfig、inventory、配置快照、manifest 和报告都放在本轮
运行目录。入口不自动销毁已有集群；不要使用 B62 或历史 B63 的状态文件。

阶段为：建群 → clean/preflight/compile → B63 placement → build/preload/deploy
→ wait-ready → B63 verify-placement → start-bird/协议检查 → kernel/FIB 检查
→ 跨节点 ping/iperf → reconvergence。默认不注入 resources.requests。

```bash
kubectl --kubeconfig experiments/1078-6vm/kubeconfig.yaml get nodes -o wide
kubectl --kubeconfig experiments/1078-6vm/kubeconfig.yaml \
  -n seedemu-b63-sh6-1078 get pods -o wide
```

修改规模、分区算法和基础设施参数请修改该 YAML。最终落点以
`output/placement_report.json` 及 `placement_actual_summary.json` 为准；
`placement_plan.json` / `placement_expected.json` 是 B62 编译阶段保留的基线。

已有 placement 产物时可用 `--resume-from build --run-dir <原目录>` 恢复构建及之后
阶段；已部署运行也支持 `start-kernel` / `reconvergence` 恢复。
历史 `--deploy-*` 等 CLI、旧 VM matrix/default-scheduler 动态入口不属于当前共享
流程。配置字段和文件角色见 [B63 README](b63_deployment/README.md)。

## 5. 只创建集群，不启动仿真

这与“全流程实验”不同。下面两段各自从仓库根目录开始，按需求选择其一：

```bash
cd largeScale/b62_k8s_scale
python3 ./k8sTools.py build \
  --input ./configSingleHostKvmMacvlan.yaml \
  --config-k3s ./configK3sSingleHostKvmMacvlan.yaml \
  --kubeconfig ./kubeconfigSingleHostKvmMacvlan.yaml \
  --inventory ./clusterSingleHostKvmMacvlan.inventory.yaml
```

```bash
cd largeScale/b63_deployment
python3 ./k8sTools.py build \
  --input ./configSingleHostKvmMacvlan.yaml \
  --config-k3s ./experiments/cluster-only/configK3s.yaml \
  --kubeconfig ./experiments/cluster-only/kubeconfig.yaml \
  --inventory ./experiments/cluster-only/inventory.yaml
```

`k8sTools.py build` 创建基础设施；实验中的 `build.sh` 构建/分发 workload 镜像。
`k8sTools.py up` 可以在已有 cluster 和编译输出上 build images + deploy，但不会替代
B63 placement、BIRD 启动、kernel 导出及上述完整测量。因此本说明使用全流程入口，
不把 `up` 当作完整实验命令。

## 6. 结束实验：清理 namespace 与销毁 VM

先确认本轮进程已经结束，保留 run-dir 和生成的 `configK3s` 文件，后者含有销毁
VM 所需元数据。以下示例使用原生 `clean`，它从本轮 manifest 识别 namespace，
发起异步删除；复杂残留的诊断/清理可使用例子自己的 `clean.sh`。
下面是破坏性操作，只能针对本轮的路径与配置执行。

B62 单宿主，在 B62 目录且 `RUN_DIR` 已设为本轮目录时：

```bash
python3 ./k8sTools.py clean \
  -f "$RUN_DIR/output" -k ./kubeconfigSingleHostKvmMacvlan.yaml
kubectl --kubeconfig ./kubeconfigSingleHostKvmMacvlan.yaml \
  wait --for=delete namespace/seedemu-b62-mvlan-vlan-4954 --timeout=1800s
python3 ./k8sTools.py destroy -d ./configK3sSingleHostKvmMacvlan.yaml
```

多宿主替换为 `kubeconfigMultiHostKvmMacvlan.yaml` 和
`configK3sMultiHostKvmMacvlan.yaml`；assignment 默认路线使用 `kubeconfig.yaml`
和 `configK3s.yaml`。若改了 namespace，等待命令也需同步修改。

B63，在 B63 目录且 `RUN_DIR` 已设为本轮目录时：

```bash
bash ../b62_k8s_scale/clean.sh "$RUN_DIR"
python3 ./k8sTools.py destroy -d "$RUN_DIR/configK3s.yaml"
```

clean 不释放 VM；destroy 才销毁所记录的集群及 VM 资源。两个全流程入口成功后都
不会自动 destroy，旧 shell 注释或兼容参数不改变当前实际行为。

## 7. 参考文件

- [0720 大规模优化设计](../0720k8sOptimizationScaleDesign.md)：整体架构与优化原因。
- [B62 README](b62_k8s_scale/README.md)：当前默认 assignment 流程与单阶段脚本。
- [0728 单机/多机设计](b62_k8s_scale/0728ExperimentDesign.md)：基础设施细节和历史清理证据，不代表当前 VM 状态。
- [B63 README](b63_deployment/README.md)：完整 CLI 和手动分阶段参数。
- [B63 测量设计](b63_deployment/0624design.md)：确定性 pair 选择和各类测量的实验目的。
- [原生 setup 调用链](../seedemu/k8sTools/resources/setup/README.md)：build/destroy 内部文件职责。
