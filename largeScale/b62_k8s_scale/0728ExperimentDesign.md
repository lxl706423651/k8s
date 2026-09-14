# B62 单宿主与多宿主 macvlan+VLAN 实验流程

本文记录 2026-07-28 对 4954 多宿主集群的 parent VLAN 实测与销毁结果，并给出
单宿主 KVM 和多宿主 KVM 两种情况下的完整执行流程、文件调用链、静态评估和
动态 ping/iperf 验证方法。

## 1. 当前结论

1. 销毁前 30 台 VM 全部可 SSH，parent VLAN 子接口采集成功率为 30/30。
2. 每台 VM 固定有两块数据面物理 parent NIC：`ens3` 和 `ens4`。
3. 每台 VM 实际创建 215 到 254 个 `ens3.<VID>` / `ens4.<VID>` 子接口，均值
   227.8、中位数 227。
4. `seedemu-b62-mvlan-vlan-4954` namespace 已完全删除。
5. 两台 hypervisor 上属于本轮 B62 的 30 台 VM、管理网络、fabric 网络、
   VXLAN/bridge、路由和工作目录均已删除。
6. 销毁后 30 个 VM 管理 IP 均不可 SSH；本机无关的 VM 和 libvirt 网络未删除。
7. 新增单宿主可执行配置 `configSingleHostKvmMacvlan.yaml`，与多宿主配置使用
   相同的 30-VM 资源、K3s、CNI、VLAN 和 MTU 参数。

本轮清理证据目录为：

```text
/home/lxl/k8s/largeScale/b62_k8s_scale/runs/4954_20260728_105853_cleanup_destroy/
```

核心证据：

```text
cleanup_summary.json
vlan_parent_inventory_before_destroy.json
vlan_parent_inventory_before_destroy.csv
clean.log
destroy-cluster.log
destroy-remote-recovery.log
destroy-final-hypervisor-verification.log
destroy-final-management-ip-verification.log
```

## 2. 各 VM 的 parent VLAN

这里需要区分两个概念：

- 物理 parent NIC 数量：每台 VM 都是 2 个，即 `ens3` 和 `ens4`。
- parent VLAN 子接口数量：部署阶段按该节点实际承载的 Pod 创建
  `ens3.<VID>` / `ens4.<VID>`，这些子接口才是 macvlan CNI 的 master。

下表的 VID 范围是该 VM 上观测值的最小值和最大值，范围内并非连续存在。精确 VID
列表保存在 `vlan_parent_inventory_before_destroy.json`。

| VM | 管理 IP | hypervisor | 总数 | ens3 数量/VID范围 | ens4 数量/VID范围 |
| --- | --- | --- | ---: | --- | --- |
| seed-k3s-master | 10.80.1.10 | amd | 254 | 136 / 18-3772 | 118 / 28-666 |
| seed-k3s-worker1 | 10.80.1.11 | amd | 246 | 132 / 773-3618 | 114 / 67-669 |
| seed-k3s-worker2 | 10.80.1.12 | amd | 221 | 139 / 110-4091 | 82 / 50-666 |
| seed-k3s-worker3 | 10.80.1.13 | amd | 229 | 140 / 827-4059 | 89 / 29-669 |
| seed-k3s-worker4 | 10.80.1.14 | amd | 230 | 138 / 238-3859 | 92 / 36-668 |
| seed-k3s-worker5 | 10.80.1.15 | amd | 215 | 131 / 318-4051 | 84 / 38-668 |
| seed-k3s-worker6 | 10.80.1.16 | amd | 228 | 141 / 288-4066 | 87 / 45-668 |
| seed-k3s-worker7 | 10.80.1.17 | amd | 227 | 139 / 166-4024 | 88 / 168-668 |
| seed-k3s-worker8 | 10.80.1.18 | amd | 225 | 144 / 183-4079 | 81 / 177-666 |
| seed-k3s-worker9 | 10.80.1.19 | amd | 222 | 134 / 314-4036 | 88 / 16-668 |
| seed-k3s-worker10 | 10.80.1.20 | amd | 236 | 141 / 908-4011 | 95 / 155-669 |
| seed-k3s-worker11 | 10.80.1.21 | amd | 245 | 144 / 53-3991 | 101 / 72-669 |
| seed-k3s-worker12 | 10.80.1.22 | amd | 231 | 138 / 1070-4094 | 93 / 37-665 |
| seed-k3s-worker13 | 10.80.1.23 | amd | 231 | 127 / 551-3885 | 104 / 8-665 |
| seed-k3s-worker14 | 10.80.1.24 | amd | 219 | 133 / 473-4092 | 86 / 74-666 |
| seed-k3s-worker15 | 10.80.1.25 | amd | 227 | 144 / 305-4055 | 83 / 249-665 |
| seed-k3s-worker16 | 10.80.1.26 | amd | 228 | 133 / 243-3973 | 95 / 54-668 |
| seed-k3s-worker17 | 10.80.1.27 | amd | 223 | 137 / 364-3626 | 86 / 33-665 |
| seed-k3s-worker18 | 10.80.1.28 | amd | 231 | 133 / 852-3926 | 98 / 93-669 |
| seed-k3s-worker19 | 10.80.1.29 | amd | 217 | 137 / 572-4052 | 80 / 49-665 |
| seed-k3s-worker20 | 10.80.1.30 | amd | 229 | 135 / 467-3965 | 94 / 12-667 |
| seed-k3s-worker21 | 10.80.1.31 | amd | 226 | 136 / 129-4031 | 90 / 13-668 |
| seed-k3s-worker22 | 10.80.1.32 | amd | 229 | 132 / 202-4007 | 97 / 53-666 |
| seed-k3s-worker23 | 10.80.1.33 | amd | 225 | 132 / 344-4084 | 93 / 59-651 |
| seed-k3s-worker24 | 10.80.2.10 | idc | 233 | 135 / 1385-4072 | 98 / 19-667 |
| seed-k3s-worker25 | 10.80.2.11 | idc | 225 | 135 / 1-4056 | 90 / 20-660 |
| seed-k3s-worker26 | 10.80.2.12 | idc | 222 | 140 / 571-3970 | 82 / 42-662 |
| seed-k3s-worker27 | 10.80.2.13 | idc | 215 | 136 / 12-4090 | 79 / 167-665 |
| seed-k3s-worker28 | 10.80.2.14 | idc | 219 | 131 / 619-4093 | 88 / 1-666 |
| seed-k3s-worker29 | 10.80.2.15 | idc | 226 | 141 / 24-4076 | 85 / 104-668 |

汇总：

| 分组 | VM 数 | parent 总数 | 每 VM 均值 | 每 VM 范围 | ens3 总数 | ens4 总数 |
| --- | ---: | ---: | ---: | --- | ---: | ---: |
| amd | 24 | 5494 | 228.92 | 215-254 | 3276 | 2218 |
| idc | 6 | 1340 | 223.33 | 215-233 | 818 | 522 |
| 全集群 | 30 | 6834 | 227.80 | 215-254 | 4094 | 2740 |

4954 编译结果有 4763 个 secondary network。`trunk0` 提供 VLAN 1..4094，
其余 logical network 进入 `trunk1`，所以 `ens4` 的实际最大 VID 约为 669。
同一个 NAD 若被不同节点上的 Pod 使用，各节点都需要自己的 VLAN parent；反之，
未承载该 NAD 的节点不创建 parent。因此 6834 是所有节点的接口实例总数，不是
logical NAD 数量。

## 3. 清理与销毁结果

先执行：

```bash
export B62_CLUSTER_INPUT_CONFIG="$(pwd)/configMultiHostKvmMacvlan.yaml"
export B62_CONFIG_K3S_PATH="$(pwd)/configK3sMultiHostKvmMacvlan.yaml"
export B62_KUBECONFIG_PATH="$(pwd)/kubeconfigMultiHostKvmMacvlan.yaml"
export B62_INVENTORY_PATH="$(pwd)/clusterMultiHostKvmMacvlan.inventory.yaml"
./clean.sh \
  /home/lxl/k8s/largeScale/b62_k8s_scale/runs/4954_20260728_105853_cleanup_destroy
```

实际删除了：

```text
4954 Deployments
3916 ReplicaSets
4255 Pods
4763 NetworkAttachmentDefinitions
4967 Events
```

namespace 删除请求后 18 秒确认完全不存在，namespaced residual 和
cluster-scoped residual 都为 0。

随后调用：

```bash
python3 k8sTools.py destroy \
  -d configK3sMultiHostKvmMacvlan.yaml
```

本地 amd hypervisor 的 24 台 VM 首次清理通过。远端 idc 清理到 `worker28` 时，
host-local 销毁脚本返回 141。该退出码是 SIGPIPE；销毁脚本中
`virsh domblklist | awk ... exit` 在启用 `pipefail` 时可能让上游 `virsh`
收到 SIGPIPE。现已改为让 awk 消费完整输出后再打印首个 disk path，保留原有
“磁盘必须位于本次 setup diskDir”保护。

恢复操作先检查 `worker28/29` 磁盘路径均位于
`/home/seed/k8sTools/b62-multihost/idc/disks/`，再定向删除两个残留 VM、三个
libvirt 网络、管理 VXLAN、fabric bridge/link、路由和远端工作目录。

最终检查：

```text
目标 domain 残留：0
目标 libvirt network 残留：0
目标 VXLAN/bridge 残留：0
本地/远端实验工作目录残留：0
30 个 VM 管理 IP 可达数：0
```

## 4. 两种配置的共同条件

| 项目 | single-host | multi-host |
| --- | --- | --- |
| 输入 | `configSingleHostKvmMacvlan.yaml` | `configMultiHostKvmMacvlan.yaml` |
| kind | `kvm` | `multiHostKvm` |
| VM 数 | 30 | 30 |
| master | 48 vCPU、96 GiB、80 GiB | 相同 |
| workers | 29 x 8 vCPU、17 GiB、80 GiB | 相同 |
| 总资源 | 280 vCPU、589 GiB | 相同 |
| K3s primary | Flannel VXLAN | Flannel VXLAN |
| secondary CNI | macvlan+VLAN | macvlan+VLAN |
| secondary MTU | 1400 | 1400 |
| trunk | `ens3/ens4`，各 VLAN 1..4094 | 相同 |
| master 调度 | `ensureAllNodesSchedulable.sh` 去 taint | 相同 |
| 数据面跨物理机 | 无 | 每 trunk 一个 fabric VXLAN |
| 管理网跨物理机 | 无 | routed subnet + management VXLAN |

保持 Flannel VXLAN 不变是控制变量要求。虽然单宿主可以使用 `host-gw`，但改变
primary CNI 会把另一个变量带入对比。

single-host 与 multi-host 的结果不能直接作为 placement 算法的唯一因果对比，
因为物理 fabric 是否跨 VXLAN 也发生了变化。placement 方法之间的比较必须在
同一份基础设施配置、相同拓扑、相同镜像、相同 Pod pair 和相同测量参数下进行。

## 5. 自动全流程入口

### 5.1 单宿主

```bash
cd /home/lxl/k8s/largeScale/b62_k8s_scale
./runSingleHostMacvlanExperiment.sh
```

默认目录：

```text
runs/4954_<YYYYmmdd_HHMMSS>_singlehost_macvlan_vlan/
```

### 5.2 多宿主

```bash
cd /home/lxl/k8s/largeScale/b62_k8s_scale
./runMultiHostMacvlanExperiment.sh
```

默认目录：

```text
runs/4954_<YYYYmmdd_HHMMSS>_multihost_macvlan_vlan/
```

两个 wrapper 都调用 `runFullExperiment.py`。全流程成功后默认保留集群，便于读取
指标和继续诊断；不会自动销毁。实验结束后必须按第 9 节 clean + destroy。

## 6. 各阶段调用链

### 6.1 基础设施阶段

| 阶段 | 顶层调用 | single-host 实际文件 | multi-host 实际文件 | 主要输出 |
| --- | --- | --- | --- | --- |
| render-assignment | `runFullExperiment.py` | 显式 `--cluster-config`，记录 SKIPPED | 相同 | run-local `cluster-input.yaml` |
| destroy-existing | `runFullExperiment.py` -> `k8sTools.py destroy` | 按旧 `configK3sSingle...` 清理 | 按旧 `configK3sMulti...` 清理两端 | destroy log |
| prepare DHCP | `runFullExperiment.py` | compact 计划记录 SKIPPED；VM 创建前逐个 reservation | 不单独调用 | summary stage |
| build-cluster | `k8sTools.py build` -> `seedemu/k8sTools/runner.py` | `_buildKvm()` | `_buildMultiHostKvm()` | configK3s、kubeconfig、inventory |
| schedulable | `ensureAllNodesSchedulable.sh` | 去除 master/control-plane taint | 相同 | node snapshot |
| primary cross-host | `validateMultiHostK3s.py` | 不适用 | 固定跨 `/24` Pod 双向 ping/DF ping | `k3s_cross_host_summary.json` |

single-host 的 `_buildKvm()` 顺序：

```text
seedemu/k8sTools/resources/setup/kvm/prepareHostAssets.py
seedemu/k8sTools/resources/setup/kvm/prepareKvmNetworks.py
seedemu/k8sTools/resources/setup/kvm/createKvmVms.py
seedemu/k8sTools/resources/setup/kvm/tuneVmLimits.py
seedemu/k8sTools/resources/setup/applyK3sCluster.py
```

其中 `prepareKvmNetworks.py` 创建：

```text
seedemu-b62-singlehost-w29           management NAT, bridge virbrb62sh
seedemu-b62-singlehost-w29-fabric0  unnumbered L2, bridge brb62sh0
seedemu-b62-singlehost-w29-fabric1  unnumbered L2, bridge brb62sh1
```

multi-host 的 `_buildMultiHostKvm()` 顺序：

```text
seedemu/k8sTools/resources/setup/multiHostKvm/prepareKvmHypervisors.py
seedemu/k8sTools/resources/setup/multiHostKvm/createMultiHostKvmVms.py
seedemu/k8sTools/resources/setup/multiHostKvm/validateMultiHostKvmFabric.py
seedemu/k8sTools/resources/setup/kvm/tuneVmLimits.py
seedemu/k8sTools/resources/setup/applyK3sCluster.py
```

`prepareKvmHypervisors.py` 在每台 hypervisor 上创建 routed management network、
管理 VXLAN/route，以及每条 trunk 对应的 libvirt bridge 和 fabric VXLAN。
`validateMultiHostKvmFabric.py` 在 K3s 安装前验证同 VLAN 双向可达、1400-byte
DF packet 和不同 VLAN 必须隔离。

### 6.2 workload 与路由阶段

| 顺序 | stage | 顶层文件 | 进一步调用/核心实现 | 证据 |
| ---: | --- | --- | --- | --- |
| 1 | clean | `clean.sh` | `lib.sh`；批量删除 workload 和 namespace | `clean.log` |
| 2 | preflight | `preflight.sh` | kubectl node/system Pod/namespace 检查 | `preflight.log` |
| 3 | compile | `compile.sh` | `lib.sh` -> `seed_k8s_plan_real_topology_by_as.py` -> `examples/kubernetes/real_topology_k3s_compile.py` -> `seedemu/compiler/kubernetes.py` | `output/k8s.yaml`、placement JSON |
| 4 | build | `build.sh` | `generate_node_image_refs.py`、`ensure_master_registry.sh`、`fastSeedemuImageArchive.py`、远端 `build_images.sh` | build/preload logs |
| 5 | deploy | `deploy.sh` | `manageK8sManifest.py macvlan-vlan-interfaces-by-node`，再按 kind/node 批量 apply | deploy log、deploy batches |
| 6 | wait-ready | `wait-ready.sh` | kubectl rollout/pod readiness 轮询 | wait-ready log |
| 7 | start-bird | `start_bird.sh` | `start_bird_node_local.py` | start BIRD summary |
| 8 | verify-bird | `verifyStartBird.sh` | `verifyStartBird.py` | `verify_after_start_bird_summary.json` |
| 9 | start-kernel | `start_bird_kernel.sh` | `start_bird_kernel_node_local.py` | kernel start summary |
| 10 | verify-kernel | `verifyFibRoutes.sh` | `verifyFibRoutes.py` | `verify_after_fib_write_summary.json` |
| 11 | dataplane | `runFullExperiment.py` | `validateCrossHostWorkload.py` | cross-host/cross-node workload JSON |
| 12 | reconvergence | `runReconvergence.sh` | `measureReconvergence.py` | `reconvergence_summary.json` |

当前 `deploy.sh` controller 基础 batch size 为 5，前两个 warmup batch 也各为 5；
subnet batch size 为 250。`build.sh` 的 preload node concurrency 为 4，每节点
image concurrency 为 2。修改这些值会改变 API pressure 与部署时长，比较实验中
必须保持一致。

## 7. 静态与动态指标

### 7.1 静态 placement

`compile.sh` 固定使用 `by_as_hard`，从当前 Ready node 集合生成：

```text
placement_expected.json
placement_plan.json
summary.json -> staticPlacement
```

至少检查：

- 每个 AS 的目标 node；
- Pod 总数、每节点 Pod 数、min/max/mean 与 imbalance；
- master 上的 Pod 数；
- 被排除节点和 control-plane 策略；
- manifest 中 nodeSelector 与 placement plan 是否一致；
- 每节点所需 image 和 VLAN parent 数量。

### 7.2 动态 ping/iperf

两种模式都不使用随机 Pod pair：

- multi-host：在共享 NAD 的 ready non-IX router Pod 中，按
  `NAD -> management /24 -> Pod -> secondary IP` 排序，选择字典序第一个跨
  management `/24` 的 pair。
- single-host：按 `NAD -> Pod -> secondary IP` 排序，选择字典序第一个位于不同
  K3s node 的 pair。

pair 先确定，之后才检查两端是否有 `iperf3`，不会因工具缺失换 pair。测量内容：

```text
全部 NAD 的 MTU 静态检查
每个 K3s node 固定抽一个真实 secondary interface 检查运行时 MTU
双向邻居/FDB 预热
双向 5-packet ping：loss、min/avg/max/mdev RTT
双向 1400-byte IP DF ping
双向 5 秒 TCP iperf3：Mbps、retransmit
```

输出：

```text
single-host: cross_node_workload_summary.json
multi-host:  cross_host_workload_summary.json
```

这是全流程正确性与直观动态特征检查，只测一个固定 pair，不能替代 B63 的多 pair
Stress48 并发 placement 性能实验。若用于证明 placement 优越性，应在同一 cluster
架构内复用 B63 的确定性 pair 集和相同并发批次。

## 8. 手动执行

### 8.1 单宿主 build

```bash
cd /home/lxl/k8s/largeScale/b62_k8s_scale
python3 k8sTools.py build \
  --input configSingleHostKvmMacvlan.yaml \
  --config-k3s configK3sSingleHostKvmMacvlan.yaml \
  --kubeconfig kubeconfigSingleHostKvmMacvlan.yaml \
  --inventory clusterSingleHostKvmMacvlan.inventory.yaml \
  --keep-temp
```

### 8.2 多宿主 build

```bash
cd /home/lxl/k8s/largeScale/b62_k8s_scale
python3 k8sTools.py build \
  --input configMultiHostKvmMacvlan.yaml \
  --config-k3s configK3sMultiHostKvmMacvlan.yaml \
  --kubeconfig kubeconfigMultiHostKvmMacvlan.yaml \
  --inventory clusterMultiHostKvmMacvlan.inventory.yaml \
  --keep-temp
```

### 8.3 common workload stages

先创建带规模和时间戳的目录并写入 assignment：

```bash
MODE=singlehost
RUN_DIR="$(pwd)/runs/4954_$(date +%Y%m%d_%H%M%S)_${MODE}_macvlan_vlan"
mkdir -p "${RUN_DIR}"
cp assignment.yaml "${RUN_DIR}/assignment.yaml"
```

单宿主环境：

```bash
export B62_CLUSTER_INPUT_CONFIG="$(pwd)/configSingleHostKvmMacvlan.yaml"
export B62_CONFIG_K3S_PATH="$(pwd)/configK3sSingleHostKvmMacvlan.yaml"
export B62_KUBECONFIG_PATH="$(pwd)/kubeconfigSingleHostKvmMacvlan.yaml"
export B62_INVENTORY_PATH="$(pwd)/clusterSingleHostKvmMacvlan.inventory.yaml"
```

多宿主环境：

```bash
export B62_CLUSTER_INPUT_CONFIG="$(pwd)/configMultiHostKvmMacvlan.yaml"
export B62_CONFIG_K3S_PATH="$(pwd)/configK3sMultiHostKvmMacvlan.yaml"
export B62_KUBECONFIG_PATH="$(pwd)/kubeconfigMultiHostKvmMacvlan.yaml"
export B62_INVENTORY_PATH="$(pwd)/clusterMultiHostKvmMacvlan.inventory.yaml"
```

共同阶段：

```bash
export KUBECONFIG="${B62_KUBECONFIG_PATH}"
./ensureAllNodesSchedulable.sh "${RUN_DIR}"
./clean.sh "${RUN_DIR}"
./preflight.sh "${RUN_DIR}"
./compile.sh "${RUN_DIR}"
./build.sh "${RUN_DIR}"
./deploy.sh "${RUN_DIR}"
./wait-ready.sh "${RUN_DIR}"
./start_bird.sh "${RUN_DIR}"
./verifyStartBird.sh "${RUN_DIR}"
./start_bird_kernel.sh "${RUN_DIR}"
./verifyFibRoutes.sh "${RUN_DIR}"
```

单宿主 dataplane：

```bash
python3 validateCrossHostWorkload.py \
  --kubeconfig "${B62_KUBECONFIG_PATH}" \
  --config-k3s "${B62_CONFIG_K3S_PATH}" \
  --namespace seedemu-b62-mvlan-vlan-4954 \
  --expected-mtu 1400 \
  --pair-scope cross-node \
  --output "${RUN_DIR}/cross_node_workload_summary.json"
```

多宿主 dataplane：

```bash
python3 validateCrossHostWorkload.py \
  --kubeconfig "${B62_KUBECONFIG_PATH}" \
  --config-k3s "${B62_CONFIG_K3S_PATH}" \
  --namespace seedemu-b62-mvlan-vlan-4954 \
  --expected-mtu 1400 \
  --pair-scope cross-host \
  --output "${RUN_DIR}/cross_host_workload_summary.json"
```

最后：

```bash
./runReconvergence.sh "${RUN_DIR}"
```

## 9. `k8sTools.py up` 的位置

已经有 compiler output 时，可以直接执行：

```bash
python3 k8sTools.py up \
  -f "${RUN_DIR}/output" \
  -k "${B62_KUBECONFIG_PATH}" \
  -d "${B62_CONFIG_K3S_PATH}" \
  --keep-temp
```

该命令会执行通用 running 资源中的：

```text
manageRunningStage.py preflight
manageRunningStage.py build
manageRunningStage.py up
  -> 准备 node-scoped VLAN parent
  -> apply workload
  -> wait Ready
```

因此 `up` 确实能完成 image build + workload deploy + wait。它不会执行 BIRD start、
FIB write、ping/iperf 和 reconvergence。B62 正式规模实验仍分开调用
`build.sh/deploy.sh/wait-ready.sh`，原因是要分别记录阶段时间，并使用
`fastSeedemuImageArchive.py`、placement-aware preload、API pressure gate 和
可恢复 batch artifacts。default scheduler baseline 可以使用 `up`，但必须确保
其 compiler manifest 本身没有 placement nodeSelector。

## 10. 清理与销毁命令

先保留生成配置到 run directory，避免 destroy 正常删除持久 cluster output 后失去
审计证据：

```bash
cp "${B62_CONFIG_K3S_PATH}" "${RUN_DIR}/configK3s.before-destroy.yaml"
./clean.sh "${RUN_DIR}"
python3 k8sTools.py destroy \
  -d "${B62_CONFIG_K3S_PATH}" \
  --keep-temp
```

single-host destroy 应删除：

```text
30 VM
seedemu-b62-singlehost-w29
seedemu-b62-singlehost-w29-fabric0
seedemu-b62-singlehost-w29-fabric1
virbrb62sh / brb62sh0 / brb62sh1
single-host disk/cloud-init files
```

multi-host destroy 应删除：

```text
amd 24 VM + idc 6 VM
两端 routed management networks
两端 management VXLAN/routes
两端 fabric0/fabric1 libvirt networks
两端 fabric VXLAN/bridges
两端 disk/cloud-init/work directories
```

## 11. 验证状态

本次完成的是：

- 4954 多宿主运行集群的 VLAN parent 现场采集；
- namespace clean；
- 30 VM 与 multi-host fabric destroy；
- single-host YAML、自动网络准备和流程代码的静态/单元测试。

本次没有重新创建并实跑 single-host 589 GiB cluster，因为当前任务明确要求先销毁
现有集群。单宿主配置通过了 YAML/XML、资源预算、compact summary、确定性 Pod pair
选择、Python compile 和 shell syntax 检查；首次真实运行仍需按第 5.1 节执行，并以
30/30 Ready、4954 workload Ready、BIRD/FIB、cross-node ping/iperf 和
reconvergence 全部 PASS 作为最终验收。
