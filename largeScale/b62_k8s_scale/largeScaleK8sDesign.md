# 大规模 K8s Design 设计说明

本文总结当前 `b62_k8s_scale` 在大规模 SeedEMU/K3s 实验中的整体设计。重点解释
`compile`、`build`、`deploy`、`start-bird`、`verify-after-start-bird`、
`start-kernel`、`verify-after-fib-write`、`reconvergence` 这些阶段的职责、
输入输出和设计原因。

## 总体思路

当前设计把 Kubernetes/K3s 当作大规模仿真的运行底座，而不是改变 SeedEMU 本身的
网络语义。SeedEMU 中的 AS 内部网络、IX peering LAN、cross-connect 仍然被视为
仿真二层广播域；router/brd/rs 容器仍然运行 BIRD，并在容器内维护 Linux 接口、
ARP、RIB、BIRD routing table 和 Linux FIB。

K8s 层主要负责以下事情：

- 给数千到上万个 SeedEMU 节点提供可调度的容器运行环境；
- 为每个仿真网络创建对应的 Multus secondary interface；
- 用确定性 placement 控制每个 Pod 落在哪个 VM node；
- 用本地 registry、fast image archive 和 per-node preload 降低镜像构建/分发成本；
- 用分阶段启动和 load gate 避免 BIRD/kernel export 在高负载下产生误判；
- 用固定 AS 样本做可重复验证，避免每轮都全量扫描所有 Pod。

当前 full workflow 由 `runExperiment.sh` 调用 `runFullExperiment.py` 编排。默认流程是：

```text
build-cluster
preflight
compile
build
deploy
wait-ready
start-bird
verify-after-start-bird
start-kernel
verify-after-fib-write
reconvergence
```

`test-bgp` 和 `destroy-cluster-after-pass` 当前仍保留为代码路径，但不纳入默认全流程。
默认实验跑完后保留 VM/K3s cluster，便于人工进入 Pod、node 或 host 侧继续分析。

## 网络后端设计

当前实现支持三种模式并存：

| 模式 | 每个 SeedEMU 网络如何落地 | 当前定位 |
| --- | --- | --- |
| `macvlan+VLAN` | 每个仿真网络映射到一个 VLAN parent，例如 `ens2.<vlan>` 或 `ens3.<vlan>`，Pod 通过 macvlan 接入 | 默认大规模高吞吐路由实验 |
| `bridge+VLAN` | 每个仿真网络映射到 VLAN parent，同时 host 侧创建 per-network Linux bridge，Pod 通过 veth 接入 | 可观测、抓包、故障注入、论文证据展示 |
| `OVN+OVS` | 每个仿真网络映射为 Kube-OVN Subnet / OVN logical switch，Pod 通过 Kube-OVN/OVS 接入 | SDN fabric、OVN/OVS 控制面和 CNI ADD 压力实验 |

当前 `assignment.yaml` 默认走 `macvlan+VLAN`：

```yaml
networking:
  backend: macvlan
  cniType: macvlan
  localLinkCniType: macvlan
  attachedCniType: macvlan
  macvlanVlanMode: true
```

选择这个默认值的原因是：它保留了每个仿真网络独立二层广播域的语义，又避免了
OVN+OVS 在大规模下的大量 logical switch、OVNDB mutation 和 OVS add-port 长尾。
`bridge+VLAN` 性能略慢于 macvlan，但 host 侧可直接看到 bridge、VLAN parent、
Pod veth 和 FDB，更适合调试与论文证据。`OVN+OVS` 语义上最接近 SDN fabric，
但在 4954/9955 这种规模下不是默认主路径，更适合作为单独研究对象。

## Compile 阶段

`compile.sh` 的目标是把真实拓扑和当前 K3s node 状态编译成可部署的 Kubernetes
manifest。

主要输入：

- `assignment.yaml` 中的 topology size、namespace、source root、network backend；
- `real_topology_<size>.txt` 和 `assignment.pkl`；
- 当前 K3s cluster 的 Ready node 列表；
- `cluster.inventory.yaml` 和 `kubeconfig.yaml`。

主要设计：

1. 使用 by-AS hard placement  
   `compile` 会读取当前 Ready node 集合，然后调用 placement helper 生成确定性的
   by-AS hard placement。生成结果会写入 workload 的 `kubernetes.io/hostname`
   nodeSelector。

2. placement 必须在 compile 阶段固定  
   这样后续 `build` 可以知道每个 node 需要哪些镜像，`deploy` 可以知道每个 node
   需要哪些 VLAN parent。如果不固定 placement，大规模环境下会出现 preload 不准、
   VLAN parent 缺失或者 deploy 阶段临时补救成本过高的问题。

3. 根据 backend 输出 runtime manifest  
   VLAN-backed 模式使用 `output/k8s.yaml`。OVN+OVS 模式使用
   `output/k8s.kube-ovn.yaml`，其中包含 Kube-OVN VPC/Subnet/NAD 等对象。

主要输出：

- `output/k8s.yaml` 或 `output/k8s.kube-ovn.yaml`;
- `output/build_images.sh`;
- `placement_expected.json`;
- `placement_plan.json`;
- 后续 build/deploy 使用的 manifest 和 image plan。

## Build 阶段

`build.sh` 的目标是把 compile 产物构建为 K3s 可直接使用的镜像，并尽量减少大规模
部署时的镜像拉取压力。

主要输入：

- `output/k8s.yaml` 或 `output/k8s.kube-ovn.yaml`;
- `output/build_images.sh`;
- hard placement 产生的 workload 到 node 的映射；
- master 上的本地 registry。

主要设计：

1. 在 master/registry node 上远端构建  
   控制端把 compile 产物上传到 master，master 本地执行构建和 registry push。这样
   registry、本地 Docker cache 和 K3s 节点处在同一个 cluster 环境里，减少外部依赖。

2. 使用 fast image archive  
   大量 SeedEMU workload 镜像高度相似。fast archive 路径会复用基础层，只为每个
   node 生成需要的镜像集合，避免像传统 `docker compose build` 那样重复构建大量近似镜像。

3. per-node preload  
   build 阶段会根据 hard placement 生成 `node_image_refs/`，每个 node 只 preload
   自己会运行的 Pod 镜像。这样 deploy 时 kubelet 不需要临时从 registry 拉大量镜像，
   也降低了 registry 和网络压力。

主要输出：

- `build.log` 和 `build_remote.log`;
- `image_refs.txt`;
- `node_image_refs/`;
- 每个 node 的 preload 日志；
- 可被 deploy 阶段直接使用的本地镜像。

## Deploy 阶段

`deploy.sh` 的目标是在不压垮 CNI 和 API server 的前提下，把 compiled manifest
部署到目标 namespace。

### VLAN-backed deploy

在 `macvlan+VLAN` 和 `bridge+VLAN` 下，deploy 的关键不是创建 Kube-OVN Subnet，
而是在 Pod CNI ADD 前准备好 host 侧 VLAN parent。

设计逻辑：

1. 从 manifest 解析每个 workload 的 Multus annotation 和固定 nodeSelector；
2. 推导每个 node 实际需要哪些 VLAN parent；
3. 只在需要的 node 上创建 `ensX.<vlan>`，而不是每台 VM 都创建所有 VLAN；
4. 如果是 `bridge+VLAN`，还会创建 per-network Linux bridge，并把 VLAN parent
   接入对应 bridge；
5. 然后按 batch apply workload，监控 pending、creating、notReady、failed 等压力。

这种设计保留 VLAN 二层隔离，同时避免无意义地在每个 node 创建几千个 VLAN。

### OVN+OVS deploy

在 `OVN+OVS` 下，deploy 会为每个仿真网络创建 Kube-OVN Subnet / OVN logical
switch，并等待 Subnet ready。Pod 创建阶段会走 Kube-OVN/OVS CNI ADD。

大规模实验中，OVN+OVS 的主要压力有两类：

- 控制面压力：大量 Subnet/logical switch/static route/policy 写入 OVNDB；
- 节点本地压力：每个 Pod secondary interface 需要写 OVS，容易触发 `ovs-vsctl`
  timeout 或 add-port 长尾。

因此 deploy 对 OVN 模式使用保守的 node-stream 节流：每个 node 同时只允许少量
active CNI ADD，通过更多 node 横向提高总吞吐，而不是在单 node 上并发写 OVS。

## Wait-ready 阶段

`wait-ready.sh` 是 deploy 和 start-bird 之间的边界。只有当目标 namespace 中的
workload Pod 全部 Running/Ready 后，才进入 BIRD 阶段。

这个阶段的设计意义是把“Pod/CNI 是否创建成功”和“BIRD 是否收敛成功”分开。如果
Pod 还在 Creating 或 NotReady，就不应该进入 BIRD 验证，否则会把 CNI 问题误判为
BGP/OSPF 问题。

## Start-bird 阶段

`start_bird.sh` 的目标是在所有 router-like Pod 内启动 BIRD。

目标对象：

- `seedemu.io/workload=seedemu`;
- role 为 `r`、`brd`、`rs` 等 router-like Pod；
- phase 为 Running。

设计逻辑：

1. 按 Pod 所在 node 分组；
2. 所有 node 并发处理；
3. 单个 node 内串行处理 Pod；
4. 每个 Pod 之间保留短间隔；
5. 通过 SSH 到 VM，在 node 本地使用 `crictl` 找容器，再用 `nsenter` 进入容器
   namespace 启动 BIRD；
6. 每个 node 完成后等待该 node load 降到阈值以下。

为什么不用纯 `kubectl exec` 启动所有 BIRD：

- 4954/9955 规模下，数千次 `kubectl exec` 会给 API server、kubelet 和网络带来额外压力；
- node-local 路径把操作分散到各 VM，本质上是每台 VM 自己处理本地容器；
- 节点并发、节点内串行的模型更接近之前 macvlan 跑通过的逻辑，也更容易控制 load。

当前 start-bird 的核心参数在 `start_bird.sh` 顶部，例如：

- `BIRD_START_DELAY_SECONDS=0.1`;
- `BIRD_LOAD_THRESHOLD=80`;
- `BIRD_PARALLEL_PER_NODE=1`;
- `BIRD_NODE_TIMEOUT_SECONDS=36000`。

## Verify-bird 阶段

`verifyStartBird.sh` / `verifyStartBird.py` 的目标是验证 BIRD 协议层是否正常启动，
而不是扫描所有 Pod。

设计逻辑：

1. 先读取 `cluster.inventory.yaml`，对所有 VM 做 load gate；
2. 等待每个 VM 的 `load1` 小于阈值；
3. 从前 10 个不同 AS 中，每个 AS 选择一个固定 brd Pod；
4. 选择规则是该 AS 中 router id 最小的 brd Pod，这样每次实验样本稳定；
5. 对每个样本执行：

```bash
birdc show protocols
birdc show route count
```

验证要求：

- BGP protocols 必须存在；
- 协议状态不能出现明显异常，例如 error、fail、unreachable、no route、refused、
  timeout、inactive 等；
- 如果短时间内协议还在收敛，verify 会按配置的等待窗口重试；
- 最终结果写入 `verify_after_start_bird_summary.json` 和
  `verify_after_start_bird_targets.json`。

这个设计的核心取舍是：用固定样本保证每轮实验可重复，用 load gate 避免高负载时误判，
用协议重试窗口容忍 BGP/OSPF 短暂收敛时间。

## Start-kernel 阶段

`start_bird_kernel.sh` 的目标是在 BIRD 已经启动并基本收敛后，开启 BIRD 到 Linux
kernel FIB 的导出。

设计逻辑与 start-bird 一致：

- node-local SSH；
- node 上用 `crictl` 找容器；
- `nsenter` 进入容器 namespace；
- 所有 node 并发；
- node 内串行；
- 每个 Pod 之间保留短间隔；
- 完成后等待 node load 降到阈值以下。

在每个 router-like 容器里，脚本会写入或更新：

```text
/etc/bird/conf/kernel.conf
```

并 reload BIRD。当前 kernel export 设计包含：

- `KERNEL_EXPORT_MODE=all`;
- `KERNEL_SCAN_BASE_SECONDS=6000`;
- `KERNEL_SCAN_JITTER_SECONDS=120`;
- `KERNEL_SWITCH_DELAY_SECONDS=0.3`;
- `KERNEL_LOAD_THRESHOLD=80`。

长 scan interval 和 jitter 的目的，是避免几千个 BIRD kernel protocol 同步扫描内核
路由表，造成周期性负载尖峰。

## Verify-kernel 阶段

`verifyFibRoutes.sh` / `verifyFibRoutes.py` 的目标是确认 start-kernel 后，BIRD
路由确实被写入 Linux FIB。

设计逻辑：

1. 先对所有 VM 做 load gate；
2. 从前 10 个不同 AS 中，每个 AS 选择一个固定 brd Pod；
3. 样本选择规则同样是该 AS 中 router id 最小的 brd Pod；
4. 对每个样本执行：

```bash
ip route | wc -l
birdc show route count
```

pass 条件是：

```text
ip route count > bird route network count
```

这里的含义是：开启 kernel export 后，Linux FIB 中应包含更多由 BIRD 写入或保留的
路由条目。这个检查不是证明全拓扑每一条 route 都完全正确，而是作为大规模实验中的
低成本稳定检查，验证 kernel 写入阶段确实生效。

输出文件：

- `verify_after_fib_write_summary.json`;
- `verify_after_fib_write_targets.json`;
- `verify_after_fib_write.log`。

## Reconvergence 阶段

`reconvergence` 阶段用于在 `verify-after-fib-write` 通过之后，测量故障注入后路由
是否发生可观测收敛，以及故障恢复后路由是否恢复。这个阶段参考
`/home/lxl/k8s/lxl/09_reconvegence_12node.py` 的思路实现。该文件本身是 12-node
运行环境 wrapper，实际逻辑委托给 `/home/lxl/k8s/lxl/09_reconvegence.py`。

### 参考脚本的核心思路

`09_reconvegence.py` 的设计不是全量扫描全部 Pod，而是固定少量、可重复的故障点和
验证点：

1. 固定 chaos targets  
   默认选择 10 个指定前缀的 brd/IX Pod 作为故障注入对象。脚本通过 Pod name
   prefix 在当前 namespace 中解析实际 Pod 名，避免每次实验随机选点。

2. 固定 verify targets  
   默认选择 4 个指定前缀的 brd Pod 作为路由变化观测点。验证不要求全网逐 Pod
   检查，而是看这些固定样本上的 Linux FIB 是否出现方向正确的变化。

3. 采集 baseline route count  
   故障前在 verify Pods 内执行：

```bash
ip route | wc -l
```

   这些数值作为故障后和恢复后的比较基线。

4. 故障注入  
   对 chaos Pods 执行：

```bash
birdc down
```

   这是 daemon 级故障模型：目标不是只关闭某个 BGP/OSPF protocol，而是让该 Pod
   内的 BIRD 进程进入停止状态。因此它和普通的 `birdc configure` reload 语义不同。

5. 故障侧收敛判定  
   脚本先等待 chaos Pods 的 BIRD 状态变为非运行状态，再等待所有 node 的 load
   降到阈值以下，然后重新采集 verify Pods 的 `ip route | wc -l`。如果至少一个
   verify Pod 的 route count 减少，或者总 route count 出现减少，就认为故障侧已经
   出现可观测收敛。

6. 恢复侧收敛判定  
   如果启用 auto recover，脚本会尝试让 chaos Pods 中的 BIRD 恢复运行，然后等待
   BIRD 状态为 running、等待 node load 下降，再采集 verify Pods 的 route count。
   如果恢复后的 route count 相比故障后增加，就认为恢复侧出现可观测收敛。

7. 输出结构化结果  
   参考脚本会写 `reconvegence.log` 和 `reconvegence_summary.json`，其中包含
   baseline、post-failure、post-recovery 的 route count 和 diff。

### 在 b62 大规模流程中的设计约束

大规模 `b62_k8s_scale` 的 reconvergence 阶段应保留上述“固定 chaos/verify 样本 +
load gate + route count 方向变化”的思想，但需要和当前 start-bird/start-kernel
运行语义保持一致：

- 如果故障模型是 daemon 级 `birdc down`，恢复时不能裸执行 `bird` 后就结束。因为
  start-kernel 阶段已经写入 `/etc/bird/conf/kernel.conf`，而 `bird.conf` 可能已经
  通过 `include "/etc/bird/conf/*.conf";` 加载该文件。恢复逻辑必须复用 start-bird
  的 include 规则，避免同时存在 wildcard include 和显式
  `include "/etc/bird/conf/kernel.conf";` 导致同一个 kernel protocol 被加载两次。
- start-kernel 的正常设计仍然是“写 `kernel.conf`，然后执行 `birdc configure` /
  `birdc 'reload kernel'`”。这个设计没有改变。reconvergence recovery 只有在
  `birdc down` 让 BIRD daemon 停止后，才需要处理进程重启问题。
- 如果实验语义想严格保持“只改配置并 `birdc configure`”，故障注入就不应该使用
  daemon 级 `birdc down`，而应该改成 protocol 级故障，例如关闭或 disable 指定
  BGP/OSPF protocol，再通过 enable/configure 恢复。这样 BIRD daemon 始终存活，
  recovery 就不需要重新启动 BIRD。
- 因此当前建议区分两类 reconvergence 测试：
  - daemon failure reconvergence：使用 `birdc down`，恢复必须按 start-bird 的启动
    约束重新拉起 BIRD，并保持 kernel include 去重；
  - protocol/config reconvergence：不关闭 BIRD daemon，只改变协议状态或配置，恢复
    使用 `birdc configure` / protocol enable，更贴近“修改配置触发收敛”的语义。

### 当前大规模判定标准

`reconvergence` 不替代 `verify-after-start-bird` 和 `verify-after-fib-write`。
它只回答一个更窄的问题：在已经确认 BIRD 和 Linux FIB 都工作的前提下，注入少量
BIRD 故障后，固定观测点的 Linux FIB 是否出现减少；恢复后，固定观测点的 Linux
FIB 是否出现增加。

这种判定是有意设计成低成本的：

- 不全量检查 4954/9955 个 Pod；
- 不要求每条 BGP session 都在该阶段重新验证；
- 先等 load 下降，再采样 route count，减少高负载瞬间读数带来的误判；
- 使用固定 Pod prefix，保证不同 VM 数量实验之间的样本尽可能一致。

## 设计取舍

当前设计的核心取舍如下：

1. 默认使用 `macvlan+VLAN`，因为它在保留二层隔离的同时，最适合 4954/9955 规模的
   高吞吐路由实验。
2. 保留 `bridge+VLAN`，因为它提供 host 侧 per-network bridge，便于抓包、FDB
   观测、`tc` 故障注入和论文证据展示。
3. 保留 `OVN+OVS`，因为它适合研究 SDN fabric、Kube-OVN/OVS 控制面和 CNI ADD
   长尾问题，但不作为当前大规模默认路径。
4. 使用 by-AS hard placement，使 build、deploy、VLAN parent 准备、per-node preload
   都具有确定性。
5. start-bird 和 start-kernel 都采用 node-local `crictl`/`nsenter`，避免数千次
   `kubectl exec` 把压力集中到 API server。
6. verify 阶段采用 load-gated 10-AS 固定样本，牺牲全量扫描覆盖率，换取稳定、
   可重复、低成本的大规模实验验证。
7. reconvergence 阶段采用固定 chaos/verify 样本和 route count 方向变化，避免把
   故障收敛测试退化成全量 BGP/FIB 扫描；同时必须区分 daemon 级故障恢复和
   protocol/config 级恢复，避免恢复逻辑破坏 start-kernel 的 `kernel.conf` 语义。

这些设计共同服务于一个目标：在不牺牲 SeedEMU 二层网络语义和 BIRD 路由协议行为的
前提下，让大规模 K8s 运行路径足够稳定、可复现、可调试。
