# test workflow

This directory provides a standalone 12-node flow. You do not need to `source`
`env_12node.sh`, and you do not need to pass compile/build/deploy parameters by hand.

## 1. Create a new experiment directory

Recommended naming convention:

```bash
mkdir -p /home/lxl/k8s/lxl/logs/$(date +%Y%m%d_%H%M%S)_1078
```

Example:

```bash
mkdir -p /home/lxl/k8s/lxl/logs/20260506_132500_1078
```

The workflow derives topology size from the experiment directory suffix `_*SIZE`.
For example, `..._1078` means `SEED_TOPOLOGY_SIZE=1078`.

If you do not want to encode the size in the directory name, create a file named
`topology_size` inside the experiment directory:

```bash
mkdir -p /home/lxl/k8s/lxl/logs/my-exp
printf '1078\n' > /home/lxl/k8s/lxl/logs/my-exp/topology_size
```

## 2. Run the full flow

Set the experiment directory once:

```bash
EXPERIMENT_DIR=/home/lxl/k8s/lxl/logs/20260506_132500_1078
cd /home/lxl/k8s/lxl/test
```

Then run the stages in order:

```bash
./preflight.sh  "${EXPERIMENT_DIR}"
./compile.sh    "${EXPERIMENT_DIR}"
./build.sh      "${EXPERIMENT_DIR}"
./deploy.sh     "${EXPERIMENT_DIR}"
./wait-ready.sh "${EXPERIMENT_DIR}"
./start_bird.sh "${EXPERIMENT_DIR}"
./verify_bird.sh "${EXPERIMENT_DIR}"
./start_bird_kernel.sh "${EXPERIMENT_DIR}"
./verify_bird_kernel.sh "${EXPERIMENT_DIR}"
```

Do not skip `compile.sh`. `build.sh` consumes `${EXPERIMENT_DIR}/output/k8s.yaml`
from the last compile. If placement logic, scheduling mode, or compile code changed,
you must rerun `./compile.sh` for that experiment directory before `./build.sh`.

Meaning of each stage:

- `preflight.sh`: checks kubeconfig, inventory, registry reachability from all nodes, kube-system health, topology inputs, and namespace baseline.
- `compile.sh`: captures the current Ready node set, generates by-AS hard placement, runs `examples/kubernetes/real_topology_k3s_compile.py`, and writes compile artifacts into `${EXPERIMENT_DIR}/output`.
- `build.sh`: uploads compile artifacts to the master, uses `BuildKit=1`, runs remote image builds, generates per-node image refs, and preloads images with 3-node concurrency.
- `deploy.sh`: creates the namespace, splits the manifest, and submits controllers in batched round-robin order with pressure/backpressure control.
- `wait-ready.sh`: polls pod readiness until all pods in the namespace are Running and Ready.
- `start_bird.sh`: starts BIRD inside all router-like `seedemu` pods and waits for node load to stabilize.
- `verify_bird.sh`: verifies `birdc show status` across router-like pods with multi-node concurrency.
- `start_bird_kernel.sh`: rewrites `/etc/bird/conf/kernel.conf` inside router-like pods, reloads kernel export mode, and waits for node load to stabilize.
- `verify_bird_kernel.sh`: verifies kernel protocol state across router-like pods with multi-node concurrency.

After a successful deploy, do not run `deploy.sh` again against the same live namespace.
`deploy.sh` is intentionally not a rolling update script for an already-running experiment.
For a new run of the same namespace, clean first:

```bash
./clean.sh "${EXPERIMENT_DIR}"
```

## 3. Useful outputs

Each stage writes a log file into the experiment directory:

- `${EXPERIMENT_DIR}/preflight.log`
- `${EXPERIMENT_DIR}/compile.log`
- `${EXPERIMENT_DIR}/build.log`
- `${EXPERIMENT_DIR}/deploy.log`
- `${EXPERIMENT_DIR}/wait-ready.log`
- `${EXPERIMENT_DIR}/start_bird.log`
- `${EXPERIMENT_DIR}/verify_bird.log`
- `${EXPERIMENT_DIR}/start_bird_kernel.log`
- `${EXPERIMENT_DIR}/verify_bird_kernel.log`
- `${EXPERIMENT_DIR}/clean.log`

Important generated artifacts:

- `${EXPERIMENT_DIR}/output/k8s.yaml`
- `${EXPERIMENT_DIR}/output/build_images.sh`
- `${EXPERIMENT_DIR}/node_image_refs/`
- `${EXPERIMENT_DIR}/build_remote.log`
- `${EXPERIMENT_DIR}/start_bird_targets.json`
- `${EXPERIMENT_DIR}/start_bird_summary.json`
- `${EXPERIMENT_DIR}/verify_bird_targets.json`
- `${EXPERIMENT_DIR}/verify_bird_summary.json`
- `${EXPERIMENT_DIR}/start_bird_kernel_targets.json`
- `${EXPERIMENT_DIR}/start_bird_kernel_summary.json`
- `${EXPERIMENT_DIR}/verify_bird_kernel_targets.json`
- `${EXPERIMENT_DIR}/verify_bird_kernel_summary.json`

## 4. If the cluster is unhealthy before running

If some workers are `NotReady`, or the master Docker/registry is unhealthy, repair
the cluster first:

```bash
cd /home/lxl/k8s/lxl/test
./repair-cluster.sh
```

You can also repair specific workers:

```bash
./repair-cluster.sh seed-k3s-worker2 seed-k3s-worker4
```

## 5. Compile 架构说明（中文）

这一套 K8s 流程的核心，不是把一个普通服务 YAML 扔给 Kubernetes，而是把
“真实拓扑数据 + AS 到节点的放置计划 + SeedEmu 的 Kubernetes compiler”结合起来，
生成可构建、可分节点 preload、可分批 deploy 的完整产物。

### 5.1 拓扑数据如何和 K8s compiler 结合

`test/compile.sh` 是 standalone compile 入口。它本身不直接生成 YAML，而是做三件事：

- 确定实验上下文，例如 `EXPERIMENT_DIR`、`topology_size`、`output/` 目录。
- 基于当前 Ready 节点集合生成 placement mapping，也就是“每个 ASN 应该落到哪个 Kubernetes node”。
- 调用真正的 Python 编译入口 `examples/kubernetes/real_topology_k3s_compile.py`。

这个 Python 编译入口会读取：

- `real_topology_<SIZE>.txt`
- `assignment.pkl`
- 环境变量中的 `SEED_NODE_LABELS_JSON`
- 环境变量中的 `SEED_SCHEDULING_STRATEGY` / `SEED_PLACEMENT_MODE`

然后在内存里构建 SeedEmu 的 `Emulator` / `Base` / `Routing` / `Ebgp` / `Ospf` 等对象，
把真实拓扑转成 SeedEmu 的网络节点、路由器、IX、链路和服务，最后交给
`seedemu.compiler.KubernetesCompiler` 输出 Kubernetes 相关产物。

### 5.2 `/home/lxl/k8s/seedemu/compiler/Kubernetes.py` 的作用

`/home/lxl/k8s/seedemu/compiler/Kubernetes.py` 是真正的 K8s compiler 实现。
它的职责不是“规划谁去哪台节点”，而是“把已经构建好的 SeedEmu 节点编译成 K8s 产物”。

它主要负责：

- 为每个 SeedEmu 节点生成 Deployment/Pod 模板。
- 生成容器镜像构建目录和 `build_images.sh`。
- 生成 `k8s.yaml`。
- 根据调度策略给 Pod 写入：
  - `nodeSelector`
  - `affinity`
  - `topologySpreadConstraints`

这里最关键的一点是：

- `BY_AS` 是软约束，主要生成 `podAffinity`，不会生成硬 `nodeSelector`。
- `BY_AS_HARD` 才会根据 `SEED_NODE_LABELS_JSON` 为每个 ASN 生成硬 `nodeSelector`。

而我们现在这套 standalone workflow 之所以能做：

- per-node image preload
- controllers 按 node bucket 分批 deploy

前提就是 compile 产物里必须有硬 `nodeSelector`。所以当前 `test/` 流程要求使用
`by_as_hard` 或 `custom`，其中推荐 `by_as_hard`。

### 5.3 `seed_k8s_plan_real_topology_by_as.py` 如何参与 compile

`/home/lxl/k8s/lxl/seed_k8s_plan_real_topology_by_as.py` 不是 compiler 本体，
而是 `compile.sh` 在正式进入 compiler 之前调用的“放置规划器”。

它读取三类输入：

- 拓扑文件 `real_topology_<SIZE>.txt`
- ASN 分配文件 `assignment.pkl`
- 当前集群节点状态 `kubectl get nodes -o json`

它会做这些事情：

- 统计每个 ASN 大概会产生多少 Pod。
- 读取当前 Ready 且可调度的 Kubernetes 节点。
- 为每个节点预留一部分 pod budget，避免把节点塞满。
- 按 `by-AS` 思路，把 ASN 分配到不同 worker/master 上。

它的输出有两个文件：

- `placement_expected.json`
  - 给 compiler 使用
  - 本质上是 `ASN -> nodeSelector labels` 的映射
  - 例如 `{"1277": {"kubernetes.io/hostname": "seed-k3s-master"}}`
- `placement_plan.json`
  - 给人看和排障用
  - 记录每个节点被分配了哪些 ASN、每个 ASN 预计多少 pod

之后 `test/compile.sh` 会把 `placement_expected.json` 的内容塞进环境变量
`SEED_NODE_LABELS_JSON`，再调用 Python compile 入口。也就是说：

- `seed_k8s_plan_real_topology_by_as.py` 决定“某个 ASN 应该去哪台 K8s 节点”
- `Kubernetes.py` 负责把这个结果真正写进 `k8s.yaml` 的 `nodeSelector`

### 5.4 `generate_node_image_refs.py` 如何与 compile 结合

`/home/lxl/k8s/lxl/test/generate_node_image_refs.py` 不参与“生成 manifest”，
它参与的是“消费 compile 产物”。

它的输入是：

- compile 产物 `output/k8s.yaml`
- 节点列表 `seed-k3s-master`, `seed-k3s-worker1` ... `seed-k3s-worker11`

它会扫描 `k8s.yaml` 中每个 workload 的：

- `spec.template.spec.nodeSelector`
- `containers[].image`

然后输出：

- `node_image_refs/images_<node>.txt`
  - 每个节点自己需要 preload 的镜像列表
- `node_image_refs/summary.json`
  - 汇总统计
  - 也能看出是否存在 `missing_selector`

所以它和 compile 的关系是：

- compile 负责产出带 `nodeSelector` 的 `k8s.yaml`
- `generate_node_image_refs.py` 再从这个 `k8s.yaml` 倒推出“每台节点该拉哪些镜像”

如果 compile 产物里没有硬 `nodeSelector`，它就无法知道镜像应该给哪台节点，
结果就是所有节点 `image_count = 0`，最后 preload 全部 `skip`。

### 5.5 Compile 后的产物分别有什么作用

`compile.sh` 结束后，`${EXPERIMENT_DIR}/output/` 里最重要的产物包括：

- `k8s.yaml`
  - 最终的 Kubernetes manifest
  - `deploy.sh` 会基于它拆分 `00-crds`、`01-foundation`、`02-services`、`03-controllers`
  - `generate_node_image_refs.py` 也会读取它

- `build_images.sh`
  - 由 compiler 生成的镜像构建脚本
  - `build.sh` 会把它上传到 master 上执行
  - 其中包含真正的 `docker build` / push 逻辑

- `images.txt`
  - compile 阶段已知的镜像引用列表
  - 可作为 build 的辅助输入

- 各镜像的 build context 目录
  - `build_images.sh` 会在这些目录上执行构建

此外，实验目录根下还会有：

- `placement_expected.json`
  - compile 输入
- `placement_plan.json`
  - placement 调试和审计用
- `nodes.ready.json`
  - 记录当次 compile 看到的 Ready 节点快照
- `node_image_refs/`
  - build 前后生成，供 per-node preload 使用

### 5.6 Compile 里的 “Generating by-AS placement mapping” 是什么

这一步现在属于 `compile.sh`，本质上就是在真正进入 compiler 之前运行
`seed_k8s_plan_real_topology_by_as.py`，把这次实验所需的放置计划先算出来。

它的作用有三层：

- 基于当前集群节点视图，判断该 topology 在当前节点集合下如何分配。
- 把 `ASN -> kubernetes node` 的映射固定下来，供后续 compile 使用。
- 把 placement 结果落盘，供后续 compiler、build、排障使用。

可以把它理解成：

- `preflight` 负责“先看当前集群和输入条件是否允许继续执行”
- `compile` 负责“根据当前节点状态计算放置计划，并把它正式编译进 `k8s.yaml`”

### 5.7 是不是每次执行前都需要 preflight

建议是：每次正式执行一个实验目录前，都先跑一次 `preflight.sh`。

原因是它不只是做格式检查，还会检查与当前集群状态直接相关的内容：

- 当前哪些节点是 Ready
- registry 从所有节点是否可达
- kube-system 是否健康
- 目标 namespace 是否已存在

而 placement mapping 现在属于 compile 阶段，因此真正需要每次重新生成 placement 的是 `compile.sh`。

严格来说，如果下面这些条件都没有变化，理论上可以复用上一次 compile 生成的
placement 和 `k8s.yaml`：

- 集群节点集合没变
- Ready 状态没变
- inventory 没变
- topology 没变
- placement 策略没变

但工程上不建议这样做。因为这套实验的核心瓶颈就在“每节点 pod 密度”和当前节点健康状态，
而 placement mapping 又直接依赖 compile 时刻的 Ready 节点快照。为了避免 stale planning，推荐流程始终是：

```bash
./preflight.sh  "${EXPERIMENT_DIR}"
./compile.sh    "${EXPERIMENT_DIR}"
./build.sh      "${EXPERIMENT_DIR}"
./deploy.sh     "${EXPERIMENT_DIR}"
./wait-ready.sh "${EXPERIMENT_DIR}"
```

如果只是单纯重跑 `build.sh`，而 `compile` 逻辑、placement 策略或 `nodeSelector` 语义已经改过，
那就必须先重新跑 `compile.sh`，不能直接复用旧的 `output/k8s.yaml`。
