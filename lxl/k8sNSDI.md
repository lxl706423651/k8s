# Kubernetes 上大规模仿真网络部署的工程问题与贡献总结

本文档面向论文写作，不是操作说明。重点是总结我们在 K3s/Kubernetes 上承载大规模仿真网络时遇到的真实工程障碍，以及为让系统真正可运行所做的系统化改造。核心结论是：**Kubernetes 并不是一个对这类工作负载开箱即用的控制平面**。当仿真网络规模从几百扩展到数千乃至近万路由节点时，问题不再只是“把 YAML 提交给 API server”，而是围绕虚拟化、镜像分发、CNI、控制面状态管理、存储和故障恢复的一整套工程体系。

## 1. 为什么这件事不简单

我们要部署的不是常规微服务，而是一个大规模、强状态、强网络依赖、每个实例都携带复杂网络配置的仿真网络。每个 Pod 不是简单的 stateless container，而是：

- 带 BIRD / kernel routing 行为的网络节点；
- 依赖 Multus/Flannel 等多网络组件；
- 需要按拓扑精确落到指定节点；
- 对节点侧 veth、bridge、FDB、netns、CNI 调用链都施加高压。

这导致 Kubernetes 在这个场景中暴露出很多平时不显著的系统瓶颈：

- VM 层资源和 libvirt/KVM 默认设置不适合高密度 Pod；
- K3s 默认 kubelet/containerd/systemd limit 不足以承载近万级 Pod；
- 构建与镜像分发链路不能靠普通 `docker build && kubectl apply`；
- 节点侧 CNI bridge 容量会先于 CPU/内存成为瓶颈；
- 一旦大规模 deploy 失败，control-plane 自身会被 namespace 残留状态拖垮；
- `clean` 并不总能“删干净”，错误的强制 finalize 还会制造幽灵资源；
- 宿主机磁盘、libvirt autostart、registry 容器生命周期等底层问题会直接反映为 Kubernetes 层面的不稳定。

因此，真正让系统可运行的工作，并不是“把拓扑翻译成 YAML”，而是围绕这个工作负载重新设计一整套部署与恢复流程。

## 2. 我们的核心工程贡献

### 2.1 基于 KVM + K3s 的高密度实验底座重构

我们没有把问题停留在 Kubernetes 资源定义层，而是先重构了宿主机和虚拟机底座，使其达到可承载大规模网络仿真的状态。主要工作包括：

- 重建 KVM + K3s 的标准化一键流程，统一 VM 创建、K3s 安装、Multus 安装、registry 初始化；
- 从 3 节点扩展到 9 节点、再扩展到 12 节点集群；
- 将节点 inventory 和环境配置从固定 3 节点/9 节点脚本，演化为可由 inventory 动态驱动的版本；
- 对 worker VM 统一提升为更高规格，如 `24 vCPU / 48 GiB RAM`；
- 对新增节点采用独立磁盘路径，避免继续挤占根分区；
- 开启所有 VM 的 `libvirt autostart`，防止节点掉线后长期不自动恢复。

这部分贡献体现的是：**Kubernetes 之上的实验平台，首先是一个虚拟化系统工程问题**。没有可靠的 VM 生命周期管理，Kubernetes 层面的任何调优都不稳固。

### 2.2 系统性解除 VM、systemd、kubelet、containerd 的默认限制

我们在大规模实验中发现，默认 Linux/K3s 配置在文件描述符、进程数、网络命名空间、邻居表、bridge hash 等方面都过于保守，会在高密度网络场景下提前失效。我们为此补了专门的资源限制解除流程，具体包括：

- `/etc/security/limits.conf` 中的 `nofile` / `nproc` 提升；
- `systemd` 服务级别的 `LimitNOFILE` / `LimitNPROC` 调整；
- `k3s` / `k3s-agent` / `containerd` 的 systemd override；
- `sysctl` 中对：
  - `user.max_net_namespaces`
  - `net.ipv4.neigh.default.gc_thresh*`
  - `net.core.netdev_max_backlog`
  - `net.core.optmem_max`
  等参数的提升；
- `cni0/bridge/hash_max` 的主动调大与持久化；
- 针对 `k3s-agent` 与 runtime 的高密度 kubelet 参数配置。

这一部分的贡献不是单一“调大参数”，而是形成了一个可自动下发到新增 worker 的体系，使扩容节点不会回退到默认、不可承载的状态。

### 2.3 重新设计 build / push / preload 流程，而不是依赖默认镜像路径

在实验中，镜像构建和镜像分发不是附属问题，而是主瓶颈之一。我们踩到了多类问题：

- 仅在 master 本地 `docker build` 成功，并不代表其他节点可以真正使用这些镜像；
- 在 `preload` 模式下，如果远端只 build 不 push，worker 侧从 registry 拉取时会出现大量 `not found`；
- registry 容器掉线后，build 会在 push 阶段直接失败；
- BuildKit 并发过高时会触发 snapshot/extraction 异常；
- 相同 tag 的缓存行为和节点本地已有镜像，会掩盖镜像分发链路是否真的工作。

为此我们将这条链路显式拆成三层：

1. **compile** 只生成产物，不假定镜像已经可用；
2. **build** 必须在 master 本地 build 后显式 push 到 registry；
3. **preload** 必须按 `k8s.yaml` 计算每节点镜像集合，再由各节点从 registry 拉取自己需要的镜像。

在实现上，我们做了这些关键改动：

- 远端构建阶段固定使用 `registry` 发布模式，避免“build 成功但 registry 里没有镜像”；
- 每次 compile 后根据 `nodeSelector["kubernetes.io/hostname"]` 生成 `images_<node>.txt`；
- preload 阶段按 node 粒度并发，而不是把全量镜像推给所有节点；
- 限制 preload 并发度，例如每次最多 `3` 个节点，避免 registry 和 containerd 被拉爆；
- 支持仅重跑 preload、仅补指定节点 preload，而不必整轮重 build。

这一点在论文里值得强调：**Kubernetes 上大规模拓扑实验的镜像分发，不能简单等价于“镜像已在 registry 中”**。需要一个 node-aware 的、和最终拓扑绑定的镜像装载机制。

### 2.4 重写 deploy 逻辑：从“提交 YAML”变成“受控的、按节点平衡的对象输入”

默认的 `kubectl apply -f k8s.yaml` 在这个场景下不可接受。原因是：

- 大量 controller 一次性进入 API server，会瞬时触发大规模 Pod 创建；
- Pod 创建和调度是并发的，脚本层面串行提交并不能限制节点侧的启动洪峰；
- workload 并不均匀，如果只按文件顺序提交，很容易在前几批就把某几个节点打满；
- 一旦有少量节点失稳，controller 会补 replacement Pod，namespace 状态迅速被污染。

我们因此实现了新的 `deploy-batched` 机制，核心贡献包括：

- 将单一 manifest 拆分为 `CRD / foundation / services / controllers` 四层；
- foundation、service 先行，controller 后行；
- controller 先按 `nodeSelector` 分桶，再按节点 round-robin 顺序提交；
- 引入 warmup batch，使前几批先低速探路；
- 引入 batch size / sleep / pressure budget；
- 在批次前后基于 `Pending / ContainerCreating / Running but not Ready / Failed` 做 backpressure；
- 增加 node monitor，对节点 load、内存、top 进程做持续采样。

这部分贡献的意义在于：**对这类网络仿真工作负载，deploy 本身必须是一个调度节流器，而不是一个简单的资源提交器。**

### 2.5 补齐 K3s + Multus 的兼容层，而不是修改业务 YAML 规避

我们遇到的一个典型问题是：Pod 全部卡在 `ContainerCreating`，原因并不是镜像拉取失败，而是 Multus 在 K3s 环境下查找 kubeconfig 路径不兼容。具体表现为：

- K3s 的实际文件路径在 `/var/lib/rancher/k3s/agent/etc/cni/net.d/multus.d/...`
- 但部分调用链仍然按传统路径 `/etc/cni/net.d/multus.d/...` 查找；
- 缺失兼容桥接后，所有 Pod 的 sandbox 创建都会失败。

我们的处理原则是：

- 不改业务 YAML；
- 不把路径写死在业务逻辑里；
- 而是在每个节点上恢复兼容桥接路径：
  - `/etc/cni/net.d/multus.d -> /var/lib/rancher/k3s/agent/etc/cni/net.d/multus.d`

这类问题说明：**Kubernetes 发行版差异（例如 K3s）对 CNI 路径假设的影响，会在大规模工作负载下被集中放大。**

### 2.6 构建集群恢复体系，而不是把失败当成“一次性脏状态”

在数千级和近万级部署中，我们多次遇到一种典型故障模式：

1. 部分节点掉成 `NotReady`；
2. 原有 Pod 卡在 `Terminating`；
3. controller 补出 replacement Pod；
4. `wait-ready` 中的总 Pod 数超过目标值；
5. control-plane 被海量 namespace 残留状态拖死；
6. `clean` 又因为 API 过慢或 namespace finalize 异常而清不干净。

我们最终形成了一个专门的恢复体系，而不是依赖简单的 `clean && redeploy`：

- 强制回收本地残留脚本、`wait-ready`、后台 SSH build 链；
- 对节点做 VM 级恢复：`reboot`，必要时 `destroy + start`；
- 对 master 的 `k3s state.db` 做定向修复，删除特定 namespace 的海量历史键；
- 重新创建/删除 namespace 以回收“幽灵资源”；
- 对 `clean` 脚本进行增强，使其能处理：
  - namespace 对象还在；
  - namespace 消失但 namespaced 资源残留；
  - finalize 后 control-plane 仍被历史 revision 拖慢
  等复杂状态。

最终，我们把这套恢复流程固化成脚本，例如：

- `repair_k3s_state_db_seedemu_namespace.py`
- `15_recover_stuck_deploy_9node.sh`

对 NSDI 论文而言，这一点的价值在于：**Kubernetes control-plane 在极端工作负载下需要显式的 state hygiene 和恢复工具链，否则实验平台不可反复使用。**

### 2.7 发现并量化节点侧 CNI 容量边界，而不是仅凭 CPU/内存做资源判断

一个关键发现是：在 7001 和 9955 规模下，失败并不首先表现为 CPU/MEM 不足，而是表现为节点侧网络栈容量先失效。我们现场抓到的直接错误是：

- `FailedCreatePodSandBox`
- `plugin type="multus" failed (add)`
- `plugin type="flannel" failed (add)`
- `failed to connect "veth..." to bridge cni0: exchange full`

这说明根因不是“资源不够”这个泛泛概念，而是：

- `veth` 接入 `cni0` 失败；
- bridge/FDB/cni 交换表达到边界；
- 新 Pod 被卡在 `ContainerCreating`；
- 当所有节点同时接近相同上限时，整体 `Running` 数量会停在一个稳定但错误的平台值。

我们为此补了专门的现场采样与证据链：

- `check_cni0_bridge_state_9node.sh`
- `check_cni0_bridge_state_12node.sh`
- `16_run_deploy_with_monitor_*`

监测内容包括：

- `cni0/bridge/hash_max`
- `bridge fdb show br cni0 | wc -l`
- `ip link show type veth | wc -l`
- `k3s-agent`、`k3s` 日志
- `dmesg`
- 节点 load / free / top / vmstat

这部分贡献体现的是：**在大规模网络仿真场景中，CNI bridge 容量是一个一等公民瓶颈，不能只靠常规的 CPU/RAM 指标判断系统健康。**

### 2.8 处理宿主机存储与 libvirt 层故障，而不是把所有问题归咎于 Kubernetes

我们还遇到了一个容易被误判的问题：宿主机根分区写满后，libvirt 会将 VM 自动暂停为 `paused (I/O error)`。表现上看像是：

- `kubectl` 无法访问；
- SSH 断开；
- 节点莫名其妙 `NotReady`；
- 似乎是 control-plane 崩溃。

但真正根因在于：

- VM 磁盘、实验结果目录、拓扑目录都挤在根分区；
- QEMU 写 guest 磁盘失败；
- libvirt 自动暂停 VM。

我们的解决方案包括：

- 将大体量目录如 `topology`、实验日志迁移到 `/data`；
- 将新增 worker 的磁盘直接创建在 `/data` 上；
- 显式监测宿主机磁盘，而不是只看 guest 内部状态。

这说明：**在 KVM + Kubernetes 的实验平台里，宿主机存储管理直接决定 Kubernetes 的可用性。**

## 3. 一套真正可用的流程，不是默认流程

基于上述问题，我们最后形成的稳定流程，已经不是原始的：

```text
compile -> build -> deploy -> wait
```

而是：

1. **预检查**  
   验证 VM、K3s、registry、Multus、磁盘、节点 Ready 状态；

2. **资源限制解除**  
   先调整 VM、systemd、kubelet、containerd、sysctl、bridge 参数；

3. **compile**  
   生成新的 `k8s.yaml` 和 node-aware 镜像清单；

4. **build + push**  
   在 master build，并显式 push 到 registry；

5. **preload**  
   各节点按自己的镜像清单、有限并发从 registry 拉取；

6. **deploy-batched**  
   按节点平衡、带 backpressure 地提交 controller；

7. **wait-ready + monitor**  
   同时采样 CNI/节点/API 指标，判断是真收敛还是伪收敛；

8. **recover / clean**  
   当 cluster 进入坏状态时，不是反复硬删，而是按 VM、namespace、state.db 三级恢复。

这说明我们做的贡献不是单个优化点，而是一整条**可反复执行、可调试、可恢复的实验运行链**。

## 4. 论文中值得强调的贡献点

如果需要在 NSDI 论文中突出“我们做了什么，而不是只陈述踩坑”，建议重点写成下面几类贡献。

### Contribution A: Kubernetes for large-scale network emulation requires nontrivial systems support

论文应明确指出：我们证明了在 K3s/Kubernetes 上承载数千到近万级仿真网络，并不是简单的容器编排问题，而是涉及：

- 虚拟化资源配置；
- 节点系统限制；
- 镜像分发与节点局部性；
- CNI/Multus 兼容；
- 节点桥接容量边界；
- control-plane 状态污染与恢复；
- 宿主机存储管理；
- 故障归因与自动化修复。

### Contribution B: A node-aware image distribution pipeline for large topologies

我们将镜像分发从全量 push/pull，改造成与最终拓扑绑定的 node-aware preload 机制。这个机制显著降低了：

- registry 瞬时压力；
- 节点不必要镜像装载；
- 重新 build 的频率；
- 单次实验的恢复成本。

### Contribution C: A controlled deployment mechanism with backpressure and node balancing

我们将 deploy 从“提交 YAML”改造成“受控对象输入”，其意义是：

- 避免 control-plane 和 kubelet 同时遭遇启动洪峰；
- 降低 workload 向单个节点倾斜的概率；
- 让 deploy 成为系统稳定性的一部分，而不是稳定性的破坏者。

### Contribution D: A practical failure diagnosis and recovery framework for K3s under extreme churn

我们并没有停留在“失败了，重建集群”，而是构建了：

- 故障现场采样；
- namespace 残留检测；
- VM 级恢复；
- state.db 定向修复；
- control-plane 再上线验证

这使实验平台具备了迭代能力，而不仅是一次性 demo 能力。

## 5. 对论文论述的建议

写作时建议避免把这些问题写成“实现细节琐碎问题”，而应该把它们上升为一个系统结论：

> Kubernetes provides a convenient declarative control plane, but large-scale network emulation stresses parts of the stack that are mostly irrelevant in conventional cloud-native workloads: bridge/FDB limits, per-node network namespace churn, registry locality, namespace-state hygiene, and VM-host storage coupling. Making the system work at scale therefore requires coordinated changes across the hypervisor, the guest OS, the container runtime, the CNI layer, and the deployment pipeline.

中文可概括为：

> Kubernetes 提供的是声明式控制平面，但大规模仿真网络真正施压的是 cloud-native 工作负载中通常不敏感的层面：节点桥接容量、网络命名空间 churn、镜像局部性、namespace 状态卫生、以及 VM 与宿主机存储耦合。因此，要让系统在该场景下稳定工作，必须跨 hypervisor、guest OS、container runtime、CNI 和 deployment pipeline 进行协同改造。

## 6. 建议在论文中强调的负面发现

论文里不应只强调“我们成功部署了多少规模”，还应强调下面这些负面发现，因为它们恰恰体现了系统贡献：

- 单纯增加 deploy batch 并不能线性加速大规模部署，反而会放大 control-plane 和节点侧 churn；
- CPU 和内存利用率不高，并不意味着系统没有接近失效边界；
- 在多网络 Pod 场景下，CNI bridge/FDB/veth 容量可能比计算资源更早成为瓶颈；
- 强制 finalize namespace 如果使用不当，会引入幽灵资源和长期 control-plane 污染；
- 重新 build 全量镜像的成本过高，因此需要 node-aware preload 和 selective recovery；
- 宿主机磁盘管理错误会直接表现为 Kubernetes 不稳定，而不是显式的“磁盘不足”错误。

## 7. 一句话版本

如果需要在摘要或贡献列表里压缩成一句话，可以写成：

> We built a practical K3s-based execution substrate for large-scale network emulation by systematically addressing VM resource limits, registry-aware image distribution, node-balanced staged deployment, Multus/K3s compatibility, CNI bridge capacity bottlenecks, and control-plane recovery under extreme namespace churn.

## 8. 给 Reviewer 的更直接表述：为什么 Kubernetes 在这里并不“简单”

如果 reviewer 直觉上认为，“既然最终工作负载被表达成 Pod 和 YAML，那么 Kubernetes 只是一个现成的执行底座”，我们需要明确反驳这一点。对这个场景而言，Kubernetes 解决的只是**对象声明与基础调度**，而真正困难的部分恰恰发生在 Kubernetes 默认抽象之外。

首先，我们部署的不是常规 stateless service，而是大量带显式拓扑约束、强网络状态、强节点局部性的仿真网络节点。每个 Pod 的启动都伴随镜像装载、多个网络接口的建立、路由协议进程的初始化以及节点本地 bridge/veth/FDB 状态的变化。因此，系统是否能够跑通，取决于一整条跨层调用链是否稳定：KVM/libvirt、guest OS、systemd、containerd、kubelet、CNI、Multus、API server、sqlite/kine。这里任意一层的默认假设过于保守，都会在规模放大后表现为整个实验平台不可用。

其次，Kubernetes 在云原生场景中常被假定为“可弹性恢复”的控制平面，但在本工作负载下，大规模失败并不会自动收敛，反而会制造新的系统负担。节点一旦短暂失稳，旧 Pod 会卡在 `Terminating`，controller 会补出 replacement Pod，namespace 对象数继续膨胀，最终把 control-plane 本身拖慢甚至拖死。换句话说，这里的困难不在于“某个 Pod 没起来”，而在于**失败会通过控制面反馈回系统自身，形成放大回路**。这要求我们不仅设计 deploy 流程，还必须设计 clean、recover、state hygiene 和故障归因流程。

再次，很多关键瓶颈并不是 reviewer 直觉中的 CPU 或内存。我们实际遇到的上限往往首先体现在节点侧网络容量，例如 `cni0` bridge、veth 挂接、FDB/exchange 表、Multus 与 Flannel 的调用链。这意味着仅凭常规 cluster metrics 很容易误判系统状态：节点看起来还有 CPU 和 RAM，但新的 Pod 已经无法完成 sandbox 创建。也正因为如此，单纯“增加机器规格”并不能自动解决问题，必须同时调整 bridge/hash/FDB/netns 相关参数，并重构 deploy 节奏与每节点负载分布。

最后，本工作的工程复杂性还体现在：为了让平台可重复使用，我们不能接受“失败后整集群重建”这种粗暴方式。我们需要能够在 registry 掉线、namespace 清理不彻底、state.db 被残留 revision 拖慢、VM 因宿主机 I/O 问题暂停、或 worker 节点网络栈失稳时，做出低成本、定点、可验证的修复。这也是为什么本文的主要工程贡献不是某一个脚本，而是一套跨层协同的执行与恢复机制。

因此，我们希望 reviewer 理解：在大规模仿真网络这个场景里，Kubernetes 不是一个“直接拿来部署”的黑盒平台，而是一个必须经过系统性改造、约束和修复之后，才能稳定承载实验的可编排控制面。本文的价值，正是在于把这些原本隐性的工程难点显式化，并把它们转化为可操作、可复现、可扩展的系统方法。
