# SEED Emulator K8s 部署流程说明

## 目录结构

```
lxl/
├── README.md                          # 本说明文件
├── env.sh                            # 环境变量配置（必须修改）
├── preflight                         # 预检查阶段
├── compile                           # 编译阶段
├── build                             # 构建阶段
├── deploy                            # 部署阶段
├── start-bird                        # 启动 bird 阶段
├── start-kernel                      # 切换到 kernel 模式阶段
├── clean                             # 清理阶段
├── seed_k8s_plan_real_topology_by_as.py  # 节点规划脚本
├── seed_k8s_start_bird0130.py       # 启动 bird 脚本
├── seed_k8s_start_bird_kernel.py   # 切换 kernel 脚本
└── scripts/                          # 依赖的脚本目录
```

---

## 快速开始

```bash
cd ~/seed-emulator-k8s/lxl

# 1. 修改环境变量配置
vim env.sh  # 修改为你实际的集群配置

# 2. 加载环境变量
source env.sh

# 3. 运行各阶段
./preflight
./compile
./build
./deploy
./start-bird
./start-kernel

# 4. 清理（如需要）
./clean
```

---

## 虚拟机重建（kvm_lab.sh + setup_k3s_cluster.sh）

### 概述

重建完整的虚拟机和K3s集群需要两个脚本配合使用：

| 脚本 | 位置 | 用途 |
|------|------|------|
| `kvm_lab.sh` | `scripts/kvm_lab.sh` | 创建/管理KVM虚拟机 (3节点: master + 2 worker) |
| `setup_k3s_cluster.sh` | `scripts/setup_k3s_cluster.sh` | 在虚拟机上安装K3s集群 |

### 使用方法

```bash
cd ~/seed-emulator-k8s

# 1. 重建虚拟机 (可选 - 如果已有VM可跳过)
# 默认配置: 50GB磁盘, 4核CPU (master), 2核CPU (worker)
# 可通过环境变量调整:
#   SEED_KVM_DISK_GB=200        # 磁盘大小 (GB)
#   SEED_K3S_MASTER_VCPUS=12    # Master CPU核数
#   SEED_K3S_WORKER1_VCPUS=12   # Worker1 CPU核数
#   SEED_K3S_WORKER2_VCPUS=12   # Worker2 CPU核数
./scripts/kvm_lab.sh down      # 停止现有VM (如需要)
./scripts/kvm_lab.sh up         # 创建VM

# 2. 安装K3s集群
./scripts/setup_k3s_cluster.sh

# 3. 验证
source output/kvm_lab/k3s_vm_env.sh
kubectl get nodes
```

### 虚拟机资源查看

```bash
cd ~/seed-emulator-k8s/lxl
./check_resources
```

输出示例：
```
--- master (192.168.122.110) ---
  内存: 31Gi
  CPU核数: 12
  虚拟磁盘: 200GB
  podCIDR: 10.42.32.0/20
  maxPods: 4k

--- worker1 (192.168.122.111) ---
  内存: 62Gi
  CPU核数: 12
  虚拟磁盘: 200GB
  podCIDR: 10.42.0.0/20
  maxPods: 4k

--- worker2 (192.168.122.112) ---
  内存: 62Gi
  CPU核数: 12
  虚拟磁盘: 200GB
  podCIDR: 10.42.16.0/20
  maxPods: 4k
```

---

## K3s 集群重建（setup_k3s_cluster.sh）

### 作用

`scripts/setup_k3s_cluster.sh` 用于在 KVM 虚拟机上全新部署一个三节点 K3s 集群（1 master + 2 workers），包含以下功能：

1. **前置检查** - 验证 ansible、ssh、ping、kubectl 等命令可用性
2. **VM 连通性验证** - 检查三个节点网络可达性和 SSH 无密码访问
3. **通过 Ansible 安装 K3s** - 在 master 和 worker 节点上安装 K3s
4. **配置私有 Registry** - 在 master 上启动 Docker registry 容器
5. **获取 kubeconfig** - 从 master 拉取集群配置文件到本地
6. **验证集群就绪** - 等待所有节点 Ready，安装 Multus CNI
7. **验证 Registry 拉取链** - 测试从集群内部能否正常拉取镜像

### 使用场景

- 首次部署 K3s 集群
- 集群损坏需要重建
- 切换 K3s 版本
- 切换 Pod/Service CIDR 范围
- 切换到高密度规模配置（seedemu-k3s-scale）

### 使用方法

```bash
# 基本用法 - 安装默认版本 K3s
cd ~/seed-emulator-k8s
source scripts/env_seedemu.sh
scripts/setup_k3s_cluster.sh

# 强制重建（删除现有集群后重新安装）
SEED_K3S_FORCE_REINSTALL=true scripts/setup_k3s_cluster.sh

# 指定 K3s 版本
SEED_K3S_VERSION=v1.28.5+k3s1 scripts/setup_k3s_cluster.sh

# 使用国内镜像源加速安装
SEED_K3S_ARTIFACT_URL=https://rancher-mirror.rancher.cn/k3s scripts/setup_k3s_cluster.sh

# 切换到高密度规模配置
SEED_CLUSTER_INVENTORY=seedemu-k3s-scale scripts/setup_k3s_cluster.sh
```

### 主要环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SEED_K3S_MASTER_IP` | 192.168.122.110 | Master 节点 IP |
| `SEED_K3S_WORKER1_IP` | 192.168.122.111 | Worker1 节点 IP |
| `SEED_K3S_WORKER2_IP` | 192.168.122.112 | Worker2 节点 IP |
| `SEED_K3S_USER` | ubuntu | SSH 用户 |
| `SEED_K3S_SSH_KEY` | ~/.ssh/id_ed25519 | SSH 私钥 |
| `SEED_K3S_VERSION` | v1.28.5+k3s1 | K3s 版本 |
| `SEED_K3S_FORCE_REINSTALL` | false | 是否强制重建 |
| `SEED_REGISTRY_HOST` | ${SEED_K3S_MASTER_IP} | Registry 主机 |
| `SEED_REGISTRY_PORT` | 5000 | Registry 端口 |

### 输出

- kubeconfig: `output/kubeconfigs/seedemu-k3s.yaml`
- 集群就绪后可直接使用 `kubectl` 操作

### 配合 lxl 工作流使用

```bash
# 1. 重建 K3s 集群
SEED_K3S_FORCE_REINSTALL=true scripts/setup_k3s_cluster.sh

# 2. 进入 lxl 目录进行后续操作
cd lxl
source ./env.sh
./preflight  # 验证集群状态
./compile   # 编译拓扑
./build     # 构建镜像
./deploy    # 部署到集群
```

---

## 环境变量说明（env.sh）

### 必须修改的变量

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `SEED_K3S_MASTER_IP` | 192.168.122.110 | K3s master IP |
| `SEED_K3S_WORKER1_IP` | 192.168.122.111 | Worker1 IP |
| `SEED_K3S_WORKER2_IP` | 192.168.122.112 | Worker2 IP |
| `SEED_K3S_SSH_KEY` | ~/.ssh/id_ed25519 | SSH 密钥路径 |
| `SEED_TOPOLOGY_SIZE` | 214 | 拓扑规模（如 1078） |
| `SEED_REAL_TOPOLOGY_DIR` | ~/lxl_topology/autocoder_test | 拓扑文件目录 |

### 可选变量（一般不需要修改）

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `SEED_K3S_USER` | ubuntu | SSH 用户 |
| `SEED_K3S_CLUSTER_NAME` | seedemu-k3s | 集群名称 |
| `SEED_NAMESPACE` | seedemu-k3s-real-topo | K8s namespace |
| `SEED_REGISTRY` | ${SEED_K3S_MASTER_IP}:5000 | 镜像仓库 |
| `SEED_CNI_TYPE` | macvlan | CNI 类型 |
| `SEED_IMAGE_DISTRIBUTION_MODE` | preload | 镜像分发模式 |
| `OUTPUT_DIR` | ~/seed-emulator-k8s/output/k8s | 输出目录 |
| `CLEAN_NAMESPACE` | true | 是否清理 namespace |

---

## 日志目录结构

日志统一存放在 `${LOG_BASE_DIR}` 目录下（默认 `~/seed-emulator-k8s/lxl/logs`）。

### 实验批次目录

每次运行脚本时会自动创建以 **时间戳 + topology_size** 命名的文件夹：

```
logs/
├── 20260329_060000_214/          # 实验批次目录
│   ├── timings.txt               # 各脚本执行时间记录
│   ├── preflight.log             # preflight 日志
│   ├── compile.log                # compile 日志
│   ├── build.log                  # build 日志（本地）
│   ├── build_remote.log           # build 日志（远程 master）
│   ├── preload_worker1.log        # worker1 镜像预加载日志
│   ├── preload_worker2.log        # worker2 镜像预加载日志
│   ├── deploy.log                 # deploy 日志
│   ├── start-bird.log             # start-bird 日志
│   ├── start-bird.json            # start-bird 结果 JSON
│   ├── start-kernel.log            # start-kernel 日志
│   └── start-kernel.json           # start-kernel 结果 JSON
├── 20260329_120000_214/
│   └── ...
```

### timings.txt 示例

```
# Experiment: 20260329_060000_214
# Created: Sun Mar 29 06:00:00 UTC 2026

[06:00:15] preflight started
[06:00:45] preflight completed
[06:00:46] compile started
[06:01:30] compile completed
[06:01:31] build started
[06:45:00] build completed
[06:45:01] deploy started
[06:46:00] deploy completed
[06:46:01] start-bird started
[06:50:00] start-bird completed
[06:50:01] start-kernel started
[06:55:00] start-kernel completed
```

### 续跑功能

如果某次脚本执行中断，可以设置 `EXPERIMENT_DIR` 环境变量继续在同一个目录下记录日志：

```bash
export EXPERIMENT_DIR=~/seed-emulator-k8s/lxl/logs/20260329_060000_214
./deploy
```

### 环境变量

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `LOG_BASE_DIR` | `~/seed-emulator-k8s/lxl/logs` | 日志根目录 |
| `EXPERIMENT_DIR` | 自动创建 | 实验批次目录（可手动指定） |

---

## 各脚本使用的环境变量

| 环境变量 | preflight | compile | build | deploy | start-bird | start-kernel | clean |
|----------|:---------:|:-------:|:-----:|:------:|:----------:|:------------:|:-----:|
| `SEED_K3S_CLUSTER_NAME` | ✅ | - | - | ✅ | ✅ | ✅ | ✅ |
| `SEED_K3S_MASTER_IP` | ✅ | ✅ | ✅ | - | - | - | - |
| `SEED_K3S_WORKER1_IP` | ✅ | - | ✅ | - | - | - | - |
| `SEED_K3S_WORKER2_IP` | ✅ | - | ✅ | - | - | - | - |
| `SEED_K3S_USER` | ✅ | - | ✅ | - | - | - | - |
| `SEED_K3S_SSH_KEY` | ✅ | - | ✅ | - | - | - | - |
| `SEED_NAMESPACE` | ✅ | ✅ | - | ✅ | ✅ | ✅ | ✅ |
| `SEED_REGISTRY` | ✅ | ✅ | - | - | - | - | - |
| `SEED_REGISTRY_HOST` | ✅ | - | - | - | - | - | - |
| `SEED_REGISTRY_PORT` | ✅ | - | - | - | - | - | - |
| `SEED_CNI_TYPE` | - | ✅ | - | - | - | - | - |
| `SEED_CNI_MASTER_INTERFACE` | - | ✅ | - | - | - | - | - |
| `SEED_SCHEDULING_STRATEGY` | - | ✅ | - | - | - | - | - |
| `SEED_IMAGE_PULL_POLICY` | - | ✅ | - | - | - | - | - |
| `SEED_IMAGE_DISTRIBUTION_MODE` | - | - | ✅ | - | - | - | - |
| `SEED_BUILD_PARALLELISM` | - | - | ✅ | - | - | - | - |
| `SEED_DOCKER_BUILDKIT` | - | - | ✅ | - | - | - | - |
| `SEED_REAL_TOPOLOGY_DIR` | ✅ | ✅ | - | - | - | - | - |
| `SEED_TOPOLOGY_SIZE` | ✅ | ✅ | - | - | - | - | - |
| `OUTPUT_DIR` | - | ✅ | ✅ | ✅ | - | - | - |
| `CLEAN_NAMESPACE` | - | - | - | ✅ | - | - | ✅ |

---

## scripts/ 目录脚本功能说明

### 本流程直接调用的脚本

| 脚本 | 功能 |
|------|------|
| `seed_k8s_plan_real_topology_by_as.py` | 根据拓扑文件生成 AS 到 Pod 的映射规划 |
| `seed_k8s_start_bird0130.py` | 在所有 router pods 中启动 bird 路由进程 |
| `seed_k8s_start_bird_kernel.py` | 将 bird 路由切换到 kernel 模式 |

### 集群初始化脚本（首次配置或重建时使用）

| 脚本 | 功能 |
|------|------|
| `setup_k3s_cluster.sh` | 在 KVM 虚拟机上全新部署三节点 K3s 集群（1 master + 2 workers） |
| `k3s_fetch_kubeconfig.sh` | 获取 K3s 集群 kubeconfig（仅首次运行） |

---

## 注意事项

1. **必须按顺序执行**（preflight → compile → build → deploy → start-bird → start-kernel）
2. **首次运行前**必须修改 `env.sh` 中的配置
3. 每个脚本会自动加载 `env.sh`，只需 `source env.sh` 一次

## 已知问题

### CNI macvlan "Link not found" 错误

如果部署时遇到如下错误：
```
Failed to create pod sandbox: rpc error: code = Unknown desc = failed to setup network for sandbox: 
plugin type="multus" failed (add): error adding container to network "net-ix-ixXX": Link not found
```

这是因为 macvlan 网络接口在 worker 节点上不存在。需要：
1. 确保所有 worker 节点都有正确的物理网卡（如 eth0, ens2）
2. 或者使用 hostLocal 模式代替 macvlan
3. 或者预先在节点上创建必要的 vlan 接口

### BuildKit 错误

如果遇到 `BuildKit is enabled but the buildx component is missing` 错误，build 脚本会自动禁用 BuildKit 并使用传统构建方式。

## 测试状态

| 阶段 | 状态 | 说明 |
|------|------|------|
| preflight | ✅ | 正常运行 |
| compile | ✅ | 正常运行 |
| build | ✅ | 正常运行（已禁用 BuildKit） |
| deploy | ✅ | 214 pods 全部 Running |
| start-bird | ✅ | 全部 214 个 pod 启动 bird 成功 |
| start-kernel | ✅ | 全部 214 个 pod 切换到 kernel 模式成功 |

## 中间产物和日志文件

> **注意**：日志已统一存放在实验批次目录 `${EXPERIMENT_DIR}` 中，请参考上方「日志目录结构」部分。

### 1. preflight

| 文件路径 | 说明 |
|----------|------|
| `/tmp/nodes.json` | kubectl get nodes 的 JSON 输出 |
| `/tmp/placement_expected.json` | **节点到 AS 的映射**（compile 阶段必须） |
| `/tmp/placement_plan.json` | 完整的放置计划（暂未使用） |
| `${EXPERIMENT_DIR}/preflight.log` | preflight 日志 |

### 2. compile

| 文件路径 | 说明 |
|----------|------|
| `${OUTPUT_DIR}/k8s.yaml` | **编译生成的 K8s 部署清单**（deploy 阶段必须） |
| `${OUTPUT_DIR}/docker-compose.yml` | Docker Compose 配置（用于 build 阶段） |
| `${OUTPUT_DIR}/images.txt` | 镜像列表（用于预加载） |
| `${OUTPUT_DIR}/build_images.sh` | 镜像构建脚本（备用） |
| `${EXPERIMENT_DIR}/compile.log` | compile 日志 |

默认 `OUTPUT_DIR=~/seed-emulator-k8s/output/k8s`

### 3. build

| 文件路径 | 说明 |
|----------|------|
| `${EXPERIMENT_DIR}/build.log` | build 日志（本地） |
| `${EXPERIMENT_DIR}/build_remote.log` | build 日志（远程 master） |
| `${EXPERIMENT_DIR}/preload_worker1.log` | Worker1 预加载日志 |
| `${EXPERIMENT_DIR}/preload_worker2.log` | Worker2 预加载日志 |
| `/tmp/seedemu-build/build.log` | 远程 master 构建原始日志 |

### 4. deploy

| 文件路径 | 说明 |
|----------|------|
| `${EXPERIMENT_DIR}/deploy.log` | deploy 日志 |

**状态查看命令**:
```bash
# 查看 Pod 状态
kubectl get pods -n ${SEED_NAMESPACE}

# 查看事件（按时间排序）
kubectl get events -n ${SEED_NAMESPACE} --sort-by='.lastTimestamp'

# 查看详细事件
kubectl describe pods -n ${SEED_NAMESPACE} <pod-name>
```

### 5. start-bird

| 文件路径 | 说明 |
|----------|------|
| `${EXPERIMENT_DIR}/start-bird.log` | start-bird 日志 |
| `${EXPERIMENT_DIR}/start-bird.json` | 启动结果 JSON |

**状态查看命令**:
```bash
# 查看 bird 进程
kubectl exec -n ${SEED_NAMESPACE} <pod-name> -- ps aux | grep bird

# 查看 bird 配置
kubectl exec -n ${SEED_NAMESPACE} <pod-name> -- cat /etc/bird/bird.conf
```

### 6. start-kernel

| 文件路径 | 说明 |
|----------|------|
| `${EXPERIMENT_DIR}/start-kernel.log` | start-kernel 日志 |
| `${EXPERIMENT_DIR}/start-kernel.json` | 切换结果 JSON |

**状态查看命令**:
```bash
# 查看 bird 进程（应显示 kernel 模式）
kubectl exec -n ${SEED_NAMESPACE} <pod-name> -- ps aux | grep bird

# 查看 bird 路由表
kubectl exec -n ${SEED_NAMESPACE} <pod-name> -- bird show protocol all
```

### 7. clean

| 文件路径 | 说明 |
|----------|------|
| `${EXPERIMENT_DIR}/clean.log` | clean 日志 |

### 8. timings.txt

| 文件路径 | 说明 |
|----------|------|
| `${EXPERIMENT_DIR}/timings.txt` | **各脚本执行时间记录** |

---

## CNI 配置关键点

根据 `scripts/setup_k3s_cluster.sh` 和 `scripts/validate_k3s_real_topology_multinode.sh`：

1. **CNI 插件安装**：所有节点需要安装 `containernetworking-plugins`（包含 macvlan, ipvlan, static）
   ```bash
   sudo apt-get install -y containernetworking-plugins
   ```

2. **CNI Master Interface**：必须与节点上的实际默认网卡匹配
   - 使用 preflight 检测：`ip -o -4 route show to default`
   - 或在 env.sh 中设置正确的 `SEED_CNI_MASTER_INTERFACE`
   - 常见问题：节点用 `ens2` 但配置用 `eth0`

3. **Multus 配置**：确保 `/etc/cni/net.d/00-multus.conf` 存在且正确