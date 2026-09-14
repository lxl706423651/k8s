# B62 多宿主 macvlan+VLAN 实验设计与 4954 验证

本文记录 B62 如何用最小 YAML 构造多物理机、多 KVM VM 的 K3s 集群，并在
4954 规模上运行完整 SeedEMU 流程。重点是把 K3s 管理网络与 SeedEMU 二层数据面
分离，通过 VLAN 保持网络隔离，并通过 VXLAN 把同一 VLAN 广播域延伸到不同
hypervisor。

## 1. 目标与边界

本次实验要验证：

1. `configMultiHostKvmMacvlan.yaml` 能独立描述两台 hypervisor、30 台 VM、K3s、
   registry、主 CNI 和两条数据面 trunk。
2. 4954 规模的 SeedEMU 网络能够完成
   `build-cluster -> compile -> build -> deploy -> wait-ready -> BIRD -> FIB ->
   reconvergence`。
3. 同一个 SeedEMU 二层网络的 VLAN tag 能跨物理宿主传递，且不同 VLAN 不共享
   广播域。
4. 实际 workload 中共享同一 Multus 网络的跨物理宿主 router Pod 能完成双向
   ping 和 iperf3。
5. 配置和代码不再用 `kvmOvn` 等名称把基础设施类型误写成网络后端。
6. 所有实验数据目录使用“规模+时间戳”命名，并保留逐阶段日志和摘要。

不变的实验条件：

- 拓扑输入、编译器和 4954 workload 与 B62 既有实验保持一致。
- 总资源上限为 300 vCPU、600 GiB memory，每台 VM 80 GiB disk。
- master 参与 workload 调度。
- SeedEMU secondary network 使用 macvlan+VLAN；不安装 Kube-OVN/OVS。

## 2. 配置入口

唯一的多宿主基础设施输入是：

```text
configMultiHostKvmMacvlan.yaml
```

关键配置如下：

| 项目 | 配置 |
| --- | --- |
| infrastructure kind | `multiHostKvm` |
| cluster | `seedemu-b62-multihost-w29` |
| hypervisor | `amd=10.202.236.88`，`idc=10.202.191.39` |
| VM 分配 | amd 24 台，idc 6 台，共 30 台 |
| master | 48 vCPU，96 GiB，80 GiB disk |
| worker | 29 x 8 vCPU，29 x 17 GiB，80 GiB disk/VM |
| 总量 | 280 vCPU，589 GiB，30 x 80 GiB disk |
| 管理子网 | `10.80.1.0/24`、`10.80.2.0/24` |
| 管理网跨宿主 | routed subnet + VXLAN，VNI 4280，UDP 4790 |
| K3s primary CNI | Flannel VXLAN |
| SeedEMU fabric | Multus + macvlan + VLAN |
| SeedEMU secondary MTU | 1400 |
| trunk0 | VM `ens3`，VLAN 1..4094，fabric VNI 6200 |
| trunk1 | VM `ens4`，VLAN 1..4094，fabric VNI 6201 |

这里必须使用 Flannel VXLAN，而不能沿用单宿主实验的 `host-gw`。两组 VM 管理地址
位于不同 routed `/24`，Flannel `host-gw` 要求 node IP 二层直连，无法正确安装
跨子网 PodCIDR 的直接下一跳。Flannel VXLAN 只承载 Kubernetes primary Pod 网络；
SeedEMU 数据面仍走独立的 `ens3/ens4` macvlan+VLAN。

`fabric.mtu: 1400` 是数据面正确性参数，不是性能调优参数。若 secondary interface
保留 1500 MTU，内层 1500-byte IP packet 再叠加 Ethernet、802.1Q 和 fabric
VXLAN header 后会超过物理网 1500 MTU。小 ping 仍可能成功，但较大的 TCP segment
会形成 PMTU black hole。编译阶段会把该值写进所有 macvlan NAD，运行时校验还会
检查实际 Pod interface，而不只检查 YAML。

## 3. 网络结构

每台 VM 有三个 virtio NIC：

```text
ens2: K3s 管理地址、SSH、API Server、registry、Flannel
ens3: SeedEMU trunk0
ens4: SeedEMU trunk1
```

每台物理机上，管理面和数据面分别处理：

```text
VM ens2
  -> host-local routed libvirt network
  -> hypervisor management VXLAN
  -> peer hypervisor routed libvirt network

VM ens3/ens4 with 802.1Q tag
  -> unnumbered libvirt bridge
  -> one flood-mode VXLAN per trunk
  -> peer hypervisor bridge
  -> peer VM ens3/ens4
```

数据面 VXLAN 不终止 VLAN。Linux bridge 在未开启 VLAN filtering 时透明转发 tagged
frame，因此 `(trunk, VLAN ID)` 唯一标识一个 SeedEMU 二层广播域。两个 trunk
可承载 8188 个 VLAN-backed network，覆盖 4954 编译结果中的 4763 个 secondary
network。

## 4. 命名调整

基础设施创建方式与 secondary CNI 是两个正交维度：

- `kind: kvm`：单宿主 KVM。
- `kind: physical`：已有物理节点。
- `kind: multiHostKvm`：多宿主 KVM。
- `fabric.type`：独立选择 `ovn`、`macvlan-vlan`、`bridge-vlan` 等后端。

旧值 `kvmOvn`、`physicalOvn`、`multiHostKvmOvn` 仅作为兼容别名保留，并产生
deprecation warning。新配置、生成元数据、销毁类型、README 和 B62 活跃脚本均使用
中性名称。历史故障文档中用于描述真实 OVN 实验的词不做机械替换。

## 5. 自动检查

`multiHostKvm/prepareKvmHypervisors.py` 在创建 VM 前检查：

- SSH、免密 `sudo`、libvirt、Docker、`ip` 和 `bridge` 命令；
- routed subnet、gateway、VM IP/MAC、VNI、端口和接口名是否冲突；
- VLAN trunk 是否使用 `ens3` 或更后的专用 NIC；
- 每个 hypervisor 的 libvirt network、bridge、VXLAN 和静态路由。

VM 创建后、K3s 安装前，
`multiHostKvm/validateMultiHostKvmFabric.py` 在确定性的跨宿主 VM 对上，对每条
trunk：

1. 使用该 trunk 的 `vlanEnd` 创建临时 VLAN parent。
2. 在 VLAN parent 上创建临时 macvlan。
3. 使用 `198.18.0.0/15` 中的独立 `/30` 做双向 ping。
4. 两个方向都发送 1372-byte ICMP payload，并设置 DF；这对应 1400-byte IP
   packet，用于检测 fabric VXLAN 路径的 MTU 黑洞。
5. 再让两端使用相邻但不同的 VLAN 和相同 `/30`，此时 ping 必须失败。
6. 正向连通、MTU 或负向隔离条件任一不满足即终止 build。
7. 无论通过或失败都删除临时接口。

这项检查同时覆盖额外 VM NIC、VLAN tag、libvirt bridge、VXLAN FDB 和 macvlan
收发路径，比只检查 bridge/VXLAN 对象存在更强。

## 6. 完整流程

统一入口：

```bash
cd /home/lxl/k8s/largeScale/b62_k8s_scale
./runMultiHostMacvlanExperiment.sh
```

默认输出：

```text
runs/4954_<YYYYmmdd_HHMMSS>_multihost_macvlan_vlan/
```

也可显式指定目录：

```bash
RUN_DIR="$(pwd)/runs/4954_$(date +%Y%m%d_%H%M%S)_multihost_macvlan_vlan"
./runMultiHostMacvlanExperiment.sh "${RUN_DIR}"
```

脚本固定使用以下互不冲突的持久 cluster 文件：

```text
configK3sMultiHostKvmMacvlan.yaml
kubeconfigMultiHostKvmMacvlan.yaml
clusterMultiHostKvmMacvlan.inventory.yaml
```

完整阶段：

```text
render-assignment                 # 显式 cluster config 时记录 SKIPPED
destroy-existing-cluster          # 仅销毁同一 multi-host 记录的旧 cluster
build-cluster
  prepare hypervisors
  create 30 VMs
  validate cross-host VLAN+macvlan fabric
  tune VM limits
  install K3s/Multus/registry
ensure-all-nodes-schedulable
validate-cross-host-k3s             # fixed-node Pod-IP ping in both directions
clean
preflight
compile                           # 4954 + deterministic by-AS placement
build                             # build/push/preload workload images
deploy                            # node-aware VLAN parent + batched apply
wait-ready
start-bird
verify-after-start-bird
start-kernel
verify-after-fib-write
validate-cross-host-workload          # deterministic shared-NAD ping + iperf3
reconvergence
```

`validateCrossHostWorkload.py` 不使用随机 Pod 对。它先排除 IX route-server Pod，
再按 NAD 名、management `/24`、Pod 名和 secondary IP 排序，选择第一个满足以下
条件的 pair：

1. 两个 ready router Pod 共享同一个 Multus NAD。
2. 两个 Pod 所在 K3s node 的管理地址属于不同 `/24`，即位于不同 hypervisor。
3. pair 是上述候选中的字典序第一项。

pair 确定后才检查两个容器是否包含 `iperf3`；缺失时直接失败，不会偷偷换 pair。
验证器还要求全部 4763 个 NAD 使用预期 MTU，并在 30 个 K3s node 上各固定抽取
一个真实 secondary interface 检查 MTU。动态测量依次执行双向邻居/FDB 预热、
5-packet 普通 ping、1400-byte DF ping 和双向 5 秒 TCP iperf3。结果写入
`cross_host_workload_summary.json`；pair、IP、node、NAD、接口、MTU、RTT、loss、
吞吐和 retransmit 均被记录。

## 7. 手动分阶段执行

只构造 cluster：

```bash
python3 k8sTools.py build \
  --input configMultiHostKvmMacvlan.yaml \
  --config-k3s configK3sMultiHostKvmMacvlan.yaml \
  --kubeconfig kubeconfigMultiHostKvmMacvlan.yaml \
  --inventory clusterMultiHostKvmMacvlan.inventory.yaml \
  --keep-temp
```

设置后续阶段环境并逐步运行：

```bash
RUN_DIR="$(pwd)/runs/4954_$(date +%Y%m%d_%H%M%S)_multihost_macvlan_vlan"
mkdir -p "${RUN_DIR}"
cp assignment.yaml "${RUN_DIR}/assignment.yaml"

export B62_CLUSTER_INPUT_CONFIG="$(pwd)/configMultiHostKvmMacvlan.yaml"
export B62_CONFIG_K3S_PATH="$(pwd)/configK3sMultiHostKvmMacvlan.yaml"
export B62_KUBECONFIG_PATH="$(pwd)/kubeconfigMultiHostKvmMacvlan.yaml"
export B62_INVENTORY_PATH="$(pwd)/clusterMultiHostKvmMacvlan.inventory.yaml"

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
python3 validateCrossHostWorkload.py \
  --kubeconfig "${B62_KUBECONFIG_PATH}" \
  --config-k3s "${B62_CONFIG_K3S_PATH}" \
  --namespace seedemu-b62-mvlan-vlan-4954 \
  --expected-mtu 1400 \
  --output "${RUN_DIR}/cross_host_workload_summary.json"
./runReconvergence.sh "${RUN_DIR}"
```

销毁 workload namespace：

```bash
export KUBECONFIG="$(pwd)/kubeconfigMultiHostKvmMacvlan.yaml"
kubectl delete namespace seedemu-b62-mvlan-vlan-4954 --wait=false
```

销毁整个 multi-host cluster、VM、libvirt network、管理 VXLAN、fabric VXLAN 和路由：

```bash
python3 k8sTools.py destroy \
  -d configK3sMultiHostKvmMacvlan.yaml \
  --keep-temp
```

## 8. 通过条件与证据

| 层次 | 通过条件 | 证据 |
| --- | --- | --- |
| Hypervisor | 两端 routed network、2 个 bridge、3 个 VXLAN 均存在 | build log、`ip -d link`、`virsh net-list` |
| VM | 30/30 可 SSH，NIC 为 ens2/ens3/ens4 | build log、inventory |
| 跨宿主管理网 | 本地 master 与远端 worker 双向可达 | ping、K3s node Ready |
| 跨宿主数据面 | 两条 trunk 的 VLAN+macvlan 双向 ping 和 1400-byte DF packet 通过 | `fabric-preflight.log`、`fabric-mtu-validation.log` |
| K3s | 30/30 node Ready，master 可调度 | kubeconfig、node snapshot |
| 跨宿主 primary CNI | 两个固定 node 的临时 Pod 双向 Pod-IP ping 和 1400-byte DF packet | `k3s_cross_host_summary.json` |
| Compile | 4954 workload 和 4763 network 可完整生成 | compile log、output manifest |
| Build | 所需 image 均进入 registry 并 preload | build log、node image refs |
| Deploy | workload 全部创建，VLAN parent 无失败 | deploy log |
| Ready | namespace 中所有目标 Pod Running/Ready | wait-ready log |
| Routing | 确定性 10-AS 样本 BIRD protocol healthy | verify summary |
| FIB | 确定性样本 Linux FIB 大于 BIRD network count | FIB summary |
| secondary MTU | 4763/4763 NAD 正确，30/30 node 的运行时接口抽样正确 | `cross_host_workload_summary.json` |
| workload 跨宿主数据面 | 固定 shared-NAD router pair 双向 ping、DF ping 和 TCP iperf3 成功 | `cross_host_workload_summary.json` |
| 动态 | reconvergence 阶段成功并输出统计 | reconvergence summary |

每轮必须保留：

```text
summary.json
loadAverage.log
<stage>.log
output/
node_image_refs/
fabric-preflight.log
k3s_cross_host_summary.json
start_bird_summary.json
verify_after_start_bird_summary.json
start_bird_kernel_summary.json
verify_after_fib_write_summary.json
cross_host_workload_summary.json
reconvergence_summary.json
```

## 9. 4954 实测结果

运行目录：

```text
/home/lxl/k8s/largeScale/b62_k8s_scale/runs/4954_20260727_202729_multihost_macvlan_vlan
```

最终状态为 `PASS`，运行区间为
`2026-07-27 20:27:29 +0800` 至 `2026-07-27 23:03:52 +0800`，包含现场诊断和
恢复的墙钟时间为 2 小时 36 分 23 秒。该结果是“保留失败证据并修复后通过”，不是
一次无干预的 clean run；`summary.json` 保留了全部失败和恢复 stage。

### 9.1 基础设施结果

| 检查 | 结果 |
| --- | --- |
| 配置解析 | 2 hypervisors、30 VMs、2 fabric trunks |
| 资源计划 | 280 vCPU、589 GiB，符合 300/600 上限 |
| VM 管理连通 | master 到远端 worker 0% loss，平均约 1.21 ms |
| trunk0 VLAN+macvlan | 双向通过，平均约 1.56/0.71 ms |
| trunk1 VLAN+macvlan | 双向通过，平均约 1.16/0.75 ms |
| trunk0/trunk1 MTU | 1400-byte DF packet 双向通过 |
| trunk0/trunk1 VLAN 隔离 | VLAN 4094 无法到达 VLAN 4093，符合预期 |
| K3s 跨宿主 primary CNI | master 与远端 worker24 的 Pod 双向通过 |
| K3s 跨宿主 MTU | 1400-byte DF packet 双向通过，0% loss |
| 临时探针清理 | 已清理 |

### 9.2 最终接受阶段

同一逻辑阶段若发生过恢复，表中同时保留首次结果和最终接受结果：

| 阶段 | 最终状态 | 本轮观测耗时 |
| --- | --- | ---: |
| build-cluster | PASS | 1662.78 s |
| ensure-all-nodes-schedulable | PASS | 2.50 s |
| clean | PASS | 1.75 s |
| preflight | PASS | 23.63 s |
| compile | PASS | 初次 651.07 s；MTU 修复重编译 655.71 s |
| build | PASS | 初次 498.31 s；software layer 修复重建 311.38 s |
| deploy | RECOVERED | 首次 234.45 s 后失败；干净续跑 1248.00 s |
| wait-ready | PASS | 初次 68.58 s；全量 CNI 重建 331.03 s |
| start-bird | PASS | 最终 50.64 s，含 30 s host-load gate |
| verify-after-start-bird | PASS | 最终 9.15 s，10/10 样本 |
| start-kernel | PASS | 最终 74.62 s，4954/4954 |
| verify-after-fib-write | PASS | 最终 9.50 s，10/10 样本 |
| validate-cross-host-workload | PASS | 最终 53.04 s |
| reconvergence | PASS | 最终 34.96 s |

这里不能把各项恢复耗时简单相加后当作下一次 clean run 预算。build 使用了已有
registry/cache，deploy 是中断后的续跑；可靠的时间基准是该轮完整墙钟时间和每个
stage 的原始日志。

### 9.3 静态与动态结果

| 指标 | 结果 |
| --- | --- |
| workload | 4954/4954 Running/Ready，30/30 node Ready |
| 静态 placement | 1086 个 AS 分配到 30 node；每节点 165..166 Pod；master=166 Pod |
| NAD MTU | 4763/4763 为 1400，mismatch=0 |
| node runtime MTU | 30/30 固定样本为 1400 |
| 固定 pair | AS1269 r10 `1.10.4.245`@worker1 与 AS1273 r10 `1.10.4.249`@worker24，共享 `net-ix-ix10` |
| 普通 ping | 0% loss；平均 RTT 0.411/0.421 ms |
| 1400-byte DF ping | 0% loss；平均 RTT 0.936/0.552 ms |
| TCP iperf3 | 882.53/894.09 Mbps，5 s，单流 |
| TCP retransmit | 3078/170；存在方向差异，应在性能实验中重复采样，不能由本轮单样本得出 placement 优势 |
| reconvergence baseline | 4/4 verify Pod protocol healthy |
| 故障侧收敛 | 0.83 s，4 个观测 Pod 中 3 个路由数下降，总路由差 -14 |
| 恢复侧收敛 | 14.84 s，总路由差恢复到 0，4/4 protocol healthy |
| reconvergence 总阶段 | 34.70 s |

本轮动态测试的目的，是证明跨宿主 macvlan+VLAN 数据面和 BIRD/FIB 流程可运行。
它只有一个 placement 和一个固定 pair，没有 baseline、重复轮次或置信区间，因此
不能单独用于比较 placement 算法性能。

### 9.4 发现与修复

1. 原草稿 YAML 的 `cni:macvlan`、`VlanMode` 不是有效结构，已改成显式
   `fabric.type`、`cni` 和 `vlanTrunks`。
2. 原 multi-host 管理 VXLAN 只提供三层 VM subnet 路由，不能承载 tagged
   secondary L2，已增加每条 trunk 独立的 bridge+VXLAN flood domain。
3. routed 管理子网不适用 Flannel `host-gw`，已改为 Flannel VXLAN。
4. 原 infrastructure kind 强制写入 OVN，已把 kind 与 fabric 解耦。
5. 旧摘要会沿用 `assignment.yaml` 的单宿主 worker 数，已改为从显式 cluster
   config 记录实际 29 workers。
6. 远端 hypervisor 原先重复生成 K3s/SeedEMU Docker tar；现仅准备 VM cloud
   image，K3s bootstrap image 仍由控制端统一 preload 到全部 30 个 node。
7. fast image archive 原先只模拟通用 `mkdir/touch/chmod`，静默忽略 Dockerfile 中
   安装 `iperf3` 的 `RUN`。现按有效 pre-COPY `RUN` 构建共享 software-layer
   variant；无法安全模拟的 post-COPY `RUN` 会 fail fast。
8. secondary interface 原为 MTU 1500。小 ping 成功但 iperf3 建连后发送 0 byte，
   MSS 1448 且持续重传。根因是 VLAN+fabric VXLAN 封装超过 1500 underlay；现统一
   设置 1400，并用 DF ping、全部 NAD 和每节点运行时接口三层检查。
9. Multus `network-status` 报告的是重命名前接口 `net1`，SeedEMU 容器内实际接口为
   `ix10`。校验器现按 secondary IP 从 `ip -j address` 解析真实接口。
10. 更新 NAD 或用相同 tag 替换镜像不会自动改变现有 Pod。本轮最终删除并重建全部
    4954 workload Pod，避免新旧 MTU 和镜像混用。
11. 首次 deploy 失败不是集群容量问题，而是在活跃 Bash 读取脚本时原地编辑造成
    进程内容不一致。失败 stage 被保留，后续从静态语法正确的脚本续跑成功。以后
    不允许在 stage 执行期间修改其脚本。

截至 2026-07-27 本记录完成时，cluster 和 namespace 曾保持运行以便复核。
2026-07-28 已完成 parent VLAN 采集、namespace clean 和 30 台 VM/fabric destroy；
终态与证据见 `0728ExperimentDesign.md`。
