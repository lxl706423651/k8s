# 单宿主 KVM macvlan+VLAN 配置

可直接执行的配置文件是：

```text
/home/lxl/k8s/largeScale/b62_k8s_scale/configSingleHostKvmMacvlan.yaml
```

本文解释当前单机 YAML。2026-09-14 配置调整为 6 VM、1078 拓扑验收；多机配置
仍保留 30 VM，两者目前不是同资源对照组。单机不需要 hypervisor 间的管理
VXLAN、fabric VXLAN 和静态路由。

当前单机入口只需要维护这一个 YAML；不再读取仓库中的 `assignment.yaml`。
VM、fabric、拓扑、placement、deploy 和验证配置来自同一文件。每轮生成的
`runs/<规模>_<时间戳>_singlehost_macvlan_vlan/assignment.yaml` 是供旧阶段脚本
读取的内部快照，不是第二个用户配置文件，不需要手工编辑。

## 1. 资源与网络

| 项目 | 配置 |
| --- | --- |
| infrastructure kind | `kvm` |
| hypervisor | 当前控制机，本地 `qemu:///system` |
| VM | 1 master + 5 workers |
| master | 48 vCPU、96 GiB、80 GiB disk |
| worker | 50 vCPU、100 GiB、80 GiB disk |
| 总资源 | 298 vCPU、596 GiB、6 x 80 GiB virtual disk |
| 管理网络 | `seedemu-b62-singlehost-w5`，`10.81.0.0/24` |
| 管理 bridge | `virbrb62sh` |
| K3s primary CNI | Flannel VXLAN |
| SeedEMU secondary CNI | Multus + macvlan + VLAN |
| trunk0 | libvirt `brb62sh0` -> VM `ens3`，VLAN 1..4094 |
| trunk1 | libvirt `brb62sh1` -> VM `ens4`，VLAN 1..4094 |
| secondary MTU | 1400 |

每台 VM 的 NIC 顺序固定为：

```text
ens2: K3s 管理地址、SSH、registry、Flannel
ens3: SeedEMU trunk0
ens4: SeedEMU trunk1
```

`kvm/prepareKvmNetworks.py` 会在 `k8sTools.py build` 内自动创建专用管理 NAT
网络及两条无 IP 的 L2 trunk 网络。单机数据面不经过 VXLAN，带 VLAN tag 的帧
只在同一物理机的 libvirt bridge 上交换。

## 2. 参数说明

### 2.1 VM 计划

```yaml
kind: kvm
clusterName: seedemu-b62-singlehost-w5

ssh:
  user: ubuntu
  key: ~/.ssh/id_ed25519

defaults:
  masterName: seed-k3s-master
  workerNamePrefix: seed-k3s-worker
  ipPrefix: 10.81.0
  masterIpStart: 10
  workerIpStart: 11
  macPrefix: 52:54:00:66:10
  masterMacStart: 16
  workerMacStart: 17

master:
  vcpus: 48
  memoryGiB: 96
  diskGb: 80

workers:
  count: 5
  vcpus: 50
  memoryGiB: 100
  diskGb: 80
```

`defaults` 让 compact `master/workers` 计划仍得到可复现的 VM 名、IP 和 MAC。
如果目标名称、IP 或 MAC 已被其他 VM 占用，KVM planner 会拒绝冲突或选择下一个
可用值；因此实验前应销毁旧的同名集群。

### 2.2 libvirt 网络与磁盘

```yaml
kvm:
  network: seedemu-b62-singlehost-w5
  networkBridge: virbrb62sh
  networkCidr: 10.81.0.0/24
  networkGateway: 10.81.0.1
  dhcpStart: 10.81.0.10
  dhcpEnd: 10.81.0.239
  storageDir: /data/lxl/k8sTools/b62-singlehost-w5
  diskDir: /data/lxl/k8sTools/b62-singlehost-w5/disks
  cloudInitDir: /data/lxl/k8sTools/b62-singlehost-w5/cloud-init
  baseImagePath: /data/lxl/k8sTools/base/jammy-server-cloudimg-amd64.img
  extraNetworks:
    - name: seedemu-b62-singlehost-w5-fabric0
      bridge: brb62sh0
      model: virtio
      trunk: trunk0
      masterInterface: ens3
    - name: seedemu-b62-singlehost-w5-fabric1
      bridge: brb62sh1
      model: virtio
      trunk: trunk1
      masterInterface: ens4
```

管理网络必须提供完整的 `networkBridge/networkCidr/networkGateway/dhcpStart/
dhcpEnd`，否则新网络不存在时构建会拒绝继续。`extraNetworks` 的顺序决定 guest
NIC 顺序，不能交换或省略 `ens3/ens4` 的映射。

### 2.3 CNI、K3s 与 tuning

```yaml
fabric:
  type: macvlan-vlan
  mtu: 1400
  vlanTrunks:
    - name: trunk0
      masterInterface: ens3
      vlanStart: 1
      vlanEnd: 4094
    - name: trunk1
      masterInterface: ens4
      vlanStart: 1
      vlanEnd: 4094

cni:
  cniType: macvlan
  localLinkCniType: macvlan
  attachedCniType: macvlan
  defaultMasterInterface: ens3

registry:
  port: 5000

k3s:
  version: v1.29.15+k3s1
  clusterCidr: 10.48.0.0/12
  serviceCidr: 10.43.0.0/16
  flannelBackend: vxlan
  nodeCidrMaskSizeIpv4: 20
  maxPods: 4000
  kubeletRegistryQps: 100
  kubeletRegistryBurst: 20

tuning:
  userMaxNetNamespaces: 65536
```

`fabric.vlanTrunks` 是编译与部署阶段的逻辑 VLAN 分配源；
`kvm.extraNetworks` 是 VM 网卡和 libvirt bridge 的物理承载，两处 trunk 名和
`masterInterface` 必须一致。

### 2.4 实验参数

```yaml
experiment:
  topologySize: 1078
  topologyDir: ~/seed-emulator/topology
```

| 字段 | 必填 / 默认 | 消费者与含义 |
| --- | --- | --- |
| `experiment.topologySize` | 必填，正整数 | `runFullExperiment.py`：拓扑规模、日志前缀 |
| `workers.count` | 必填，非负整数 | 建 VM；自动生成内部 `experiment.workerCount`，不要重复填写 |
| `experiment.topologyDir` | 默认 `~/seed-emulator/topology` | compile：拓扑数据目录 |
| `experiment.sourceRoot` | 默认自动定位本仓库 | compile：SeedEMU 源码根目录 |
| `experiment.canonicalOutputDir` | 默认 `<topologyDir>/output_<topologySize>` | compile：canonical 输出 |
| `experiment.name` | 默认 `b62-macvlan-vlan-<规模>` | 实验标识，可覆盖 |
| `experiment.namespace` | 默认 `seedemu-b62-mvlan-vlan-<规模>` | deploy/clean 的 namespace，可覆盖 |
| `placement` | 默认 master 参与、每 node 保留 20 Pod 容量 | compile/lib：沿用原字段格式 |
| `deploy` | 省略字段使用 `deploy.sh` 默认值 | 本 YAML 已迁移此前有效的批次与压力阈值，例 `batchSize: 80` |
| `verification` | 省略使用原 runner 默认值 | BIRD/FIB 验证，默认 load1 < 50、FIB 覆盖全部 AS |
| `reconvergence` | 省略使用原 runner 默认值 | 故障目标和重收敛检查，原字段格式不变 |

experiment 的相对路径相对于输入 YAML 所在目录解析。namespace/name 若显式覆盖，
更换规模时需要自行保证命名正确；使用默认值则无需同步修改。
此单文件入口支持当前 compact `master/workers` 单机格式；旧显式 nodes、多机入口
继续使用原接口，不自动迁移。现有 `resources` 总预算不会替代或自动重分配 VM 资源。

## 3. 构建与销毁

当前 YAML 使用 `kvm.prepareImageCache: false`，跳过建机前的重复缓存生成，
不跳过 K3s 镜像导入或安装。本轮先用 `docker load -i` 将现有 `base_image/image-cache`
中的 Multus/CoreDNS/metrics-server/local-path tar 导入主机 Docker；安装阶段实际
从主机 Docker 导出临时 tar 再导入 VM。仅填写缓存目录不等于自动导入主机镜像。
迁移机器时也应先导入所需 tar，或恢复缓存生成并保证公网可达。Ubuntu cloud image
可从 `legacyBaseImagePath` 复用。首次外网 Multus 拉取超时的失败记录保留在本轮日志中。

只构造 YAML 指定的 K3s cluster：

```bash
cd /home/lxl/k8s/largeScale/b62_k8s_scale
python3 k8sTools.py build \
  --input configSingleHostKvmMacvlan.yaml \
  --config-k3s configK3sSingleHostKvmMacvlan.yaml \
  --kubeconfig kubeconfigSingleHostKvmMacvlan.yaml \
  --inventory clusterSingleHostKvmMacvlan.inventory.yaml \
  --keep-temp
```

运行本 YAML 指定规模的全流程（当前 1078）：

```bash
./runSingleHostMacvlanExperiment.sh
```

只检查配置与资源汇总，不创建 VM、不访问集群、不部署 workload：

```bash
./runSingleHostMacvlanExperiment.sh --prepare-only
```

`--prepare-only` 生成的 summary 状态是 PREPARED，不是实验 PASS。实际启动使用
新的实验目录；不能从该空集群预检目录 `--resume-from start-kernel`。

默认实验目录：

```text
runs/<topologySize>_<YYYYmmdd_HHMMSS>_singlehost_macvlan_vlan/
```

销毁前先清理 workload namespace，再销毁 K3s、VM、管理网络和两个 trunk 网络：

```bash
RUN_DIR=/home/lxl/k8s/largeScale/b62_k8s_scale/runs/<本轮目录>
./clean.sh "${RUN_DIR}"

python3 k8sTools.py destroy \
  -d configK3sSingleHostKvmMacvlan.yaml \
  --keep-temp
```

新单文件流程不需要设置 B62 环境变量，手动阶段从本轮 `assignment.yaml.runtime`
读取 cluster/kubeconfig/inventory 路径。旧双文件实验目录仍按旧方式传入上下文。
生成的 `cluster-input.yaml` 保存本轮基础设施快照；修改原 YAML 不影响已有实验：

```bash
./runSingleHostMacvlanExperiment.sh "$RUN_DIR" --resume-from start-kernel
```

续跑读取本轮快照而非原输入；不会重新建机。日志目录不可被新实验覆盖。
wrapper 仍不会自动 clean/destroy，必须按上面的两步收尾。
仅重跑故障恢复阶段可使用 `--resume-from reconvergence`；它会重新注入故障，
不是补算原来已停止采样的恢复时间，也不会重建 VM 或重复部署。

单机物理宿主必须能容纳当前配置的 596 GiB guest memory，并预留宿主开销。若宿主可用内存不足，不应通过
swap 或过量 memory overcommit 强行运行，应减少 VM 数或调整每 VM 内存后重新
形成独立实验组。

## 4. 内存单位和 48 VM 配置

`master.memoryGiB`、`workers.memoryGiB` 是推荐的内存字段（配置 VM 时需要给出内存，
或由 API 使用默认值）；`memoryGb` 是同义兼容拼写，按本项目 VM 单位约定使用
二进制 GiB，不是十进制 GB。`10.5` 表示 10752 MiB。旧 `memoryMb`/`memory_mb`
仍按 MiB 解释。不要同时填写不同数值的多个内存字段；非正数、无穷值和不能精确
转换为整数 MiB 的数值会报错。原生 API、单机/多机 setup 和 B62 静态汇总共用
`seedemu/k8sTools/resources/setup/normalizeResources.py`；内部生成文件仍保存 MiB。

切换为 48 VM、4954 拓扑时，先确认上轮 VM 已销毁，再修改本 YAML 的资源：

```yaml
master:
  vcpus: 48
  memoryGiB: 96
  diskGb: 80
workers:
  count: 47
  vcpus: 5
  memoryGiB: 10.5
  diskGb: 80
```

总计 283 vCPU、589.5 GiB、48 x 80 GiB 虚拟磁盘，在 300 vCPU/600 GiB 预算内。
如果只把 count 改成 47 而保留当前每 worker 50 vCPU/100 GiB，会严重超预算。
`clusterName` 和网络/目录名中的 `w5` 只是名字，不决定节点数；可保持不变，或在
旧集群完整销毁后统一改为 `w47`，不能混用两套生成配置或同名 bridge。

同一个 `configSingleHostKvmMacvlan.yaml` 中再修改拓扑字段，其他参数不必同步：

```yaml
experiment:
  topologySize: 4954
  topologyDir: ~/seed-emulator/topology
```

需要 `real_topology_4954.txt`、`assignment.pkl`、`output_4954/` canonical 输出。
现有管理 IP 范围足够容纳 .10 master 和 .11..57 workers；现有两条 VLAN trunk
容量为 8188，足够容纳 4954 拓扑的 4763 个 secondary network，但仍需检查地址冲突。
启动命令不变。无需修改 `assignment.yaml`、namespace、日志名称或生成的 K3s 配置。

重收敛测试未指定目标时，从当前拓扑中按 AS 数字编号升序选取最多 10 个至少有两个
router 的 AS，关闭每个 AS 中 router 数字编号最大的 BIRD；在前 4 个 AS 的最小编号
router 上观测路由数量变化。选择不依赖 Pod 随机后缀、VM 位置或运行结果。
本单机 YAML 中显式设置的 `reconvergence.chaosTargetPrefixes` 和
`verifyTargetPrefixes` 优先，切换规模后不存在的显式目标会报错，不会静默替换。
故障侧仍记录 4 个观测点 FIB 数量首次下降时间。恢复侧改为监测被重启的 10 个
router 的 `birdc show route count` 总路由数（不是 FIB 数，也不是唯一前缀数）：

- 从发出第一个 BIRD 重启命令前开始计时，包括顺序重启命令耗时。
- 新 BIRD 的起始 RIB 按 0 计；首次正路由数代表重启后的增长，避免首次采样时已恢复而永远等不到增长。
- 每 5 秒采样；命令耗时超过周期时记录实际采样时间，不伪造固定间隔。
- 每个 router 必须连续两次与上一轮相等，即至少三次相同读数才确认稳定。
  例如 10/15/20 秒都是 N，则 20 秒确认、收敛时间回记 10 秒。
- 任意增减都会重新计算稳定段；空值、命令失败或 0 路由不能算稳定。
  已确认的 router 仍继续采样，后续变化会撤销之前的确认。
- 全体收敛时间取 10 个 router 最终稳定段起点的最大值。协议恢复与 VM load1 < 50
  是独立通过条件；未就绪继续采样，超过 `reconvergence.phaseTimeoutSeconds`
  （默认 1800）才失败，不再因为第一次 BGP 未 Established 就立即失败。

`recovery_samples.jsonl` 逐轮保留读数、协议和 VM load；`recovery_stability.json`
记录每个 router 的稳定起点/确认时间；`reconvergence_summary.json.recoveryStability`
汇总同一结果。`recoveryConvergenceSeconds` 是稳定起点，`stabilityConfirmedSeconds`
是两轮确认完成时间，`validationCompletedSeconds` 包括最终协议/负载检查等待。
这是固定 10 个 router 的采样收敛判据，不保证未采样路由器或全部路由内容完全稳定；
也不能与旧“首次 FIB 上升”时间直接比较。
