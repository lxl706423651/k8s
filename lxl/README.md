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

## KVM + K3s 重建主流程（推荐使用 kvm_quickstart.sh）

### 概述

在新服务器上重新建立 KVM 和 K3s，推荐优先使用仓库主入口：

- `scripts/kvm_quickstart.sh up`

这个脚本会自动串起来执行：

- `prereq`：安装宿主机依赖、启用 libvirt、检查当前用户 libvirt 访问
- `scripts/kvm_lab.sh up`：创建 3 台 KVM 虚拟机
- `scripts/setup_k3s_cluster.sh`：在 3 台 VM 上安装 K3s、Multus、私有 registry，并做最终校验

底层仍然是两个核心脚本配合：

| 脚本 | 位置 | 用途 |
|------|------|------|
| `kvm_lab.sh` | `scripts/kvm_lab.sh` | 创建/管理KVM虚拟机 (3节点: master + 2 worker) |
| `setup_k3s_cluster.sh` | `scripts/setup_k3s_cluster.sh` | 在虚拟机上安装K3s集群 |
| `kvm_quickstart.sh` | `scripts/kvm_quickstart.sh` | 一键完成 prereq + VM 创建 + K3s 安装 |

### 使用方法

```bash
cd ~/k8s

# 1. 宿主机首次执行前，确认默认 SSH key 可用于 VM
test -f ~/.ssh/id_ed25519
test -f ~/.ssh/id_ed25519.pub

# 2. 一键安装
./scripts/kvm_quickstart.sh up

# 3. 验证
source output/kvm_lab/k3s_vm_env.sh
export KUBECONFIG=/home/lxl/k8s/output/kubeconfigs/seedemu-k3s.yaml
kubectl get nodes -o wide
./scripts/kvm_quickstart.sh status
```

### 分步执行方法

如果只想重建其中一段，可以按下面顺序执行：

```bash
cd ~/k8s

# 1. 只装宿主机依赖
./scripts/kvm_quickstart.sh prereq

# 2. 重建 VM
./scripts/kvm_lab.sh down
./scripts/kvm_lab.sh up

# 3. 加载 VM 环境变量
source output/kvm_lab/k3s_vm_env.sh

# 4. 安装或重装 K3s
./scripts/setup_k3s_cluster.sh
```

### 新服务器上的推荐执行顺序

```bash
cd ~/k8s
source scripts/env_seedemu.sh

# 宿主机检查
nproc
free -h
test -e /dev/kvm && ls -l /dev/kvm
virsh -c qemu:///system list --all

# 一键安装
./scripts/kvm_quickstart.sh up

# 安装后验证
export KUBECONFIG=/home/lxl/k8s/output/kubeconfigs/seedemu-k3s.yaml
kubectl get nodes -o wide
./scripts/kvm_quickstart.sh status
```

### 可选资源覆盖

仓库当前 `scripts/kvm_lab.sh` 默认给 3 台 VM 分配非常大的资源；如果要在别的服务器上重建，可以在执行前覆盖：

```bash
export SEED_KVM_DISK_GB=200
export SEED_K3S_MASTER_VCPUS=12
export SEED_K3S_WORKER1_VCPUS=12
export SEED_K3S_WORKER2_VCPUS=12
export SEED_K3S_MASTER_MEMORY_MB=32768
export SEED_K3S_WORKER1_MEMORY_MB=65536
export SEED_K3S_WORKER2_MEMORY_MB=65536
./scripts/kvm_quickstart.sh up
```

### 已有三节点 KVM 资源重配

如果当前 3 台 VM 已经存在，需要把资源调整到下面这组规格：

- `seed-k3s-master`：`64 vCPU / 120 GiB / 400 GiB`
- `seed-k3s-worker1`：`32 vCPU / 60 GiB / 200 GiB`
- `seed-k3s-worker2`：`32 vCPU / 60 GiB / 200 GiB`

可以直接执行：

```bash
cd ~/k8s/lxl
./resize_kvm_cluster_resources.sh
```

脚本位置：

- `/home/lxl/k8s/lxl/resize_kvm_cluster_resources.sh`

这个脚本的行为：

- 先用 `virsh` 关闭现有 VM
- 持久化修改 vCPU 和内存
- 对磁盘只做扩容，不做缩容
- 如果磁盘变大，会在 guest 内自动扩 root filesystem
- 最后重新启动 VM 并等待 SSH 恢复

注意：

- 如果当前 worker 磁盘已经大于 `200G`，脚本会跳过缩盘，只打印 warning
- 这是有意设计的，因为 `qcow2` 在线/离线缩盘都不适合作为自动化默认行为

### 从 3 节点扩成 9 节点 K3s 集群

如果要在现有 `master + worker1 + worker2` 基础上，再新增 6 台 worker：

- `seed-k3s-worker3`
- `seed-k3s-worker4`
- `seed-k3s-worker5`
- `seed-k3s-worker6`
- `seed-k3s-worker7`
- `seed-k3s-worker8`

每台新增 worker 的规格固定为：

- `32 vCPU`
- `60 GiB RAM`
- `200 GiB disk`

执行脚本：

```bash
cd ~/k8s/lxl
./add_k3s_workers_6.sh
```

脚本位置：

- `/home/lxl/k8s/lxl/add_k3s_workers_6.sh`

这个脚本做的事情：

- 复用 `scripts/kvm_lab.sh` 的 KVM 命名、磁盘目录、cloud-init 方式创建 VM
- 按固定地址创建：
  - `worker3 -> 192.168.122.113`
  - `worker4 -> 192.168.122.114`
  - `worker5 -> 192.168.122.115`
  - `worker6 -> 192.168.122.116`
  - `worker7 -> 192.168.122.117`
  - `worker8 -> 192.168.122.118`
- 通过读取现有 master 的 `/var/lib/rancher/k3s/server/node-token`
- 按 `setup_k3s_cluster.sh` 中 worker 的 join 参数安装 `k3s-agent`
- 安装 `containernetworking-plugins`
- 补齐 `/opt/cni/bin` 的 `macvlan`、`ipvlan`、`static`
- 补齐 `/etc/cni/net.d/multus.d -> /var/lib/rancher/k3s/agent/etc/cni/net.d/multus.d` 的兼容链接
- 自动对新增 worker 应用与 `seed_k8s_ultimate_all.sh` 等价的资源限制解除：
  - 放宽 `/etc/security/limits.conf` 的 `nofile` / `nproc`
  - 写入 `/etc/sysctl.d/99-k8s-ultimate.conf`
  - 放宽 `k3s-agent` / `containerd` 的 systemd limits
  - 强化 `/etc/rancher/k3s/config.yaml` 中的 `kubelet-arg`
- 等待新节点 `Ready`
- 输出一个 9 节点 inventory 和一个扩展 env 文件

运行完成后，产物包括：

- `/home/lxl/k8s/configs/clusters/seedemu-k3s-9node.yaml`
- `/home/lxl/k8s/output/kvm_lab/k3s_vm_env_9node.sh`

建议完成后执行：

```bash
export KUBECONFIG=/home/lxl/k8s/output/kubeconfigs/seedemu-k3s.yaml
kubectl get nodes -o wide
source /home/lxl/k8s/output/kvm_lab/k3s_vm_env_9node.sh
```

如果后续脚本需要显式使用 9 节点 inventory，可以再导出：

```bash
export SEED_CLUSTER_INVENTORY_PATH=/home/lxl/k8s/configs/clusters/seedemu-k3s-9node.yaml
export SEED_CLUSTER_INVENTORY=seedemu-k3s-9node
```

### 本次实际安装结果

- KVM 虚拟机：`seed-k3s-master`、`seed-k3s-worker1`、`seed-k3s-worker2` 全部成功创建并运行
- K3s 集群：3 个节点全部 `Ready`
- kubeconfig：`/home/lxl/k8s/output/kubeconfigs/seedemu-k3s.yaml`

### 虚拟机资源查看

```bash
cd ~/k8s/lxl
./check_resources
```

输出示例：
```
--- master (192.168.122.110) ---
  内存: 64Gi
  CPU核数: 12
  虚拟磁盘: 200GB
  podCIDR: 10.42.16.0/20
  maxPods: 4k

--- worker1 (192.168.122.111) ---
  内存: 64Gi
  CPU核数: 12
  虚拟磁盘: 200GB
  podCIDR: 10.42.32.0/20
  maxPods: 4k

--- worker2 (192.168.122.112) ---
  内存: 64Gi
  CPU核数: 12
  虚拟磁盘: 200GB
  podCIDR: 10.42.48.0/20
  maxPods: 4k
```

### PodCIDR 收敛到 /20

如果节点的 `podCIDR` 出现不一致，或者曾经被错误脚本改成了 `/24`，现在统一使用：

```bash
cd ~/k8s/lxl
./update-podcidr.sh
```

这个脚本现在的行为是：

- 在 master 上清理冲突的 `kube-controller-manager-arg`
- 只保留 `node-cidr-mask-size-ipv4=20`
- 先重建 master，再依次重建 worker1、worker2
- 每个节点都会等待重新 `Ready`
- 每个节点都会校验 `podCIDR` 已经收敛到 `/20`

执行完成后，验证命令：

```bash
export KUBECONFIG=/home/lxl/k8s/output/kubeconfigs/seedemu-k3s.yaml
kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\tPodCIDR: "}{.spec.podCIDR}{"\n"}{end}'
```

本次实际结果：

```text
seed-k3s-master   PodCIDR: 10.42.16.0/20
seed-k3s-worker1  PodCIDR: 10.42.32.0/20
seed-k3s-worker2  PodCIDR: 10.42.48.0/20
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

# 1.5 检查配置资源
./check_resources 

若是配置资源有问题 ./update-podcidr.sh 和 
# 2. 进入 lxl 目录进行后续操作
cd lxl
source ./env.sh
./preflight  # 验证集群状态
./compile   # 编译拓扑
./build     # 构建镜像
./deploy    # 部署到集群
```

---

## 本次安装遇到的问题与解决方法

### 问题 1：virt-install 创建第一台 VM 时权限被拒绝

现象：

```text
ERROR Cannot access storage file '/home/lxl/k8s/output/kvm_lab/disks/seed-k3s-master.qcow2' ... Permission denied
```

以及后续：

```text
ERROR Cannot access backing file '/home/lxl/k8s/output/kvm_lab/base/jammy-server-cloudimg-amd64.img' ... Permission denied
```

根因：

- `scripts/kvm_lab.sh` 把磁盘和 backing image 放在仓库目录 `~/k8s/output/kvm_lab`
- 真正启动虚拟机的是 `libvirt-qemu` 用户
- `/home/lxl`、`/home/lxl/k8s`、`/home/lxl/k8s/output/kvm_lab/...` 这条路径默认不是 `libvirt-qemu` 可穿越/可读
- 所以 `virt-install` 能看到路径字符串，但 hypervisor 无法实际打开 qcow2 和 backing image

解决方法：

- 给 `libvirt-qemu` 补 ACL，使其能访问 `SEED_KVM_STORAGE_DIR` 整棵路径
- 本次实际修复的是给 `/home/lxl`、`/home/lxl/k8s`、`/home/lxl/k8s/output/kvm_lab` 递归加 `libvirt-qemu` 的 `rx`

示例：

```bash
setfacl -m u:libvirt-qemu:rx /home/lxl /home/lxl/k8s
setfacl -R -m u:libvirt-qemu:rx /home/lxl/k8s/output/kvm_lab
find /home/lxl/k8s/output/kvm_lab -type d -exec setfacl -d -m u:libvirt-qemu:rx {} +
```

更稳妥的长期方案：

- 把 `SEED_KVM_STORAGE_DIR` 改到 `/var/lib/libvirt/images/...` 这类 libvirt 原生可访问路径
- 或在每台新服务器初始化时，显式给仓库输出目录补 `libvirt-qemu` ACL

### 问题 2：quickstart 最后一步 registry 校验失败

现象：

```text
failed to do request: Head "https://192.168.122.110:5000/...": http: server gave HTTP response to HTTPS client
```

根因：

- `scripts/setup_k3s_cluster.sh` 会在 master 上启动本地 Docker registry，地址是 `192.168.122.110:5000`
- 仓库原本只给 K3s/containerd 写了 `/etc/rancher/k3s/registries.yaml`
- 但最后的 `[7/7] Validating registry pull chain` 里，先在 master 上用 `docker push` 推测试镜像
- 主节点 Docker daemon 没有把 `192.168.122.110:5000` 标成 insecure registry，于是默认按 HTTPS 访问明文 HTTP registry，导致失败

解决方法：

- 已在仓库 [README.md](/home/lxl/k8s/lxl/README.md) 对应流程说明中记录
- 已在仓库 [k3s-install.yml](/home/lxl/k8s/ansible/k3s-install.yml) 中加入 Docker daemon 配置：
  - 写入 `/etc/docker/daemon.json`
  - 配置 `"insecure-registries": ["{{ seed_registry_host }}:{{ seed_registry_port }}"]`
  - 配置变化后重启 Docker

这次修复后，重新执行：

```bash
source /home/lxl/k8s/output/kvm_lab/k3s_vm_env.sh
./scripts/setup_k3s_cluster.sh
```

最终成功输出：

```text
pod/registry-self-check condition met
registry-ok
K3s cluster is ready.
```

### 问题 3：apt update 偶发访问 Docker 源握手失败

现象：

```text
W: Failed to fetch https://download.docker.com/linux/ubuntu/dists/noble/InRelease  Could not handshake
```

说明：

- 这个问题在本次安装中出现过，但没有阻断 `kvm_quickstart.sh`
- 因为脚本实际只缺 `qemu-kvm`，而系统已能从已有索引继续完成安装
- 真正导致安装失败的不是这个 warning，而是前面的 libvirt 路径权限和 Docker insecure registry 配置

处理建议：

- 如果 warning 不影响 `apt install`，可以继续往下看真实报错
- 如果某台服务器上它升级成阻断错误，再单独检查外网、代理、证书或 Docker apt 源

### 问题 4：214 个业务 Pod 全部卡在 ContainerCreating

现象：

- `deploy-batched` 完成后，namespace 中 214 个业务 Pod 长时间停留在 `ContainerCreating`
- `kubectl describe pod` 事件持续出现 `FailedCreatePodSandBox`
- 代表性报错如下：

```text
plugin type="multus" failed (add): stat /etc/cni/net.d/multus.d/multus.kubeconfig: no such file or directory
```

排查结论：

- 不是镜像拉取失败
- 不是所有节点都没有安装 Multus
- `kube-system` 中 3 个节点上的 `kube-multus-ds` 都是 `1/1 Running`
- K3s + Multus 的实际 kubeconfig 文件存在于：

```text
/var/lib/rancher/k3s/agent/etc/cni/net.d/multus.d/multus.kubeconfig
```

- 但业务 Pod 创建 sandbox 时，Multus CNI 实际去读取的是：

```text
/etc/cni/net.d/multus.d/multus.kubeconfig
```

- 三台节点上缺少这个兼容路径，导致每次 `ADD` 网络时都报 `stat ... no such file or directory`

根因：

- K3s 的 CNI 目录在 `/var/lib/rancher/k3s/agent/etc/cni/net.d`
- 但部分 Multus 调用链仍然按传统路径 `/etc/cni/net.d/...` 查找 kubeconfig
- 当前机器上缺少从传统路径到 K3s 实际路径的兼容桥接
- 这不是新的临时 workaround，而是仓库原本就应该保证的兼容处理：
  - `scripts/setup_k3s_cluster.sh` 中有 `ensure_multus_kubeconfig_bridge()`
  - `ansible/k3s-install.yml` 中也有创建 `/etc/cni/net.d/multus.d` 兼容路径的任务

规范修复方式：

- 按仓库原设计恢复兼容桥接路径
- 不去修改业务 YAML，也不去硬编码新的 Multus 查找逻辑
- 在每个节点上确保：

```text
/etc/cni/net.d/multus.d -> /var/lib/rancher/k3s/agent/etc/cni/net.d/multus.d
```

手动修复示例：

```bash
for host in 192.168.122.110 192.168.122.111 192.168.122.112; do
  ssh -i ~/.ssh/id_ed25519 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null ubuntu@"$host" \
    'sudo -n mkdir -p /etc/cni/net.d && \
     sudo -n rm -rf /etc/cni/net.d/multus.d && \
     sudo -n ln -s /var/lib/rancher/k3s/agent/etc/cni/net.d/multus.d /etc/cni/net.d/multus.d && \
     sudo -n test -f /etc/cni/net.d/multus.d/multus.kubeconfig'
done
```

修复结果：

- 修复前：214 个业务 Pod 全部 `ContainerCreating`
- 修复后：214 个业务 Pod 全部进入 `Running`

验证命令：

```bash
export KUBECONFIG=/home/lxl/k8s/output/kubeconfigs/seedemu-k3s.yaml

kubectl -n seedemu-k3s-real-topo get pods --no-headers | awk '{print $3}' | sort | uniq -c
kubectl -n kube-system get pods -o wide | grep multus
```

### 问题 5：部分 `net_*` 链路前缀没有被外部 AS 学到

现象：

- 路由 Pod 的 BIRD 已经启动，iBGP / OSPF 也已经建立
- 同一个 AS 内部可以看到某些 `net_*` 前缀
- 但跨 AS 测试时，部分 `net_*` 地址无法互 ping
- 代表性现象：
  - `6.159.12.0/24` 在 AS1439 内部存在
  - 但在 AS1277 / AS2010 的路由表中是 `Network not found`
  - `6.159.7.0/24` 却可以被外部 AS 学到

根因：

- 不是 `kernel protocol` 的 `import none` 导致的
- 也不是 BIRD 没有学到这些内部链路前缀
- 真正原因在于 eBGP 外宣策略依赖 `bgp_large_community`
- 原始实现中，只有边界路由器本地 `t_direct` 的直连前缀会被打上 `LOCAL_COMM`
- 非边界路由器上的内部 `net_*` 链路前缀虽然会通过 OSPF / iBGP 传播到边界路由器，但不会自动带上 `LOCAL_COMM`
- 而边界 eBGP 的导出条件是：

```text
export where bgp_large_community ~ [LOCAL_COMM, CUSTOMER_COMM]
```

- 结果就是：
  - 边界路由器自己的直连 `net_*` 前缀可以外宣
  - 内部路由器产生、经 OSPF 传播过来的 `net_*` 前缀不会继续外宣

为什么这会导致 ping 不通：

- 这里测试的目标不是普通 Pod 的 `eth0` 地址，而是路由 Pod 上各个 `net_*` 接口的地址
- 这些地址对应的其实是 AS 内部或跨路由器点到点链路前缀
- ping 能否成功依赖双向路径都成立：
  - 去程需要源 AS 能学到目标 `net_*` 前缀
  - 回程也需要目标 AS 能学到源 `net_*` 前缀
- 一旦某一侧没有把这些内部链路前缀继续通过 eBGP 外宣出去，另一侧就会出现：
  - `birdc show route for <net_前缀>` 返回 `Network not found`
  - `ip route get <net_地址>` 回退到 `eth0` 或默认路由
  - 最终表现为跨 AS 的 `net_*` 地址只能部分互通，或者单向不通

为什么这是“内部链路前缀”问题：

- `net_*` 前缀很多并不是边界路由器自己直接连出来的业务前缀
- 它们常常先在某个内部路由器上产生，再通过 OSPF / iBGP 传播到本 AS 的边界路由器
- 对边界路由器而言，这类前缀已经不属于 `t_direct` 的“本地直连”
- 因此边界路由器虽然已经在 `master4` 里看到了这些前缀，但默认并不会把它们自动变成可外宣的 `LOCAL_COMM` 路由
- 所以问题不是“BIRD 没学到”，而是“学到了但没有资格被 eBGP 导出”

为什么不是修改 `kernel import none -> import all`：

- `kernel import all` 只会把 Linux 内核路由重新导回 BIRD
- 它不会自动给这些前缀补 `LOCAL_COMM / CUSTOMER_COMM`
- 所以不能解决 eBGP export policy 的放行条件
- 反而可能把默认路由、宿主相关路由或其他不需要的内核路由再灌回 BIRD，污染控制面

仓库修复：

- 已在 [Ebgp.py](/home/lxl/k8s/seedemu/layers/Ebgp.py) 中修改边界路由器的 BGP bootstrap 逻辑
- 原来只有：
  - `t_direct -> t_bgp`
  - 给直连前缀打 `LOCAL_COMM`
- 现在新增：
  - `master4 -> t_bgp`
  - 只匹配 `source = RTS_OSPF && net.len = 24` 的前缀
  - 给这些 OSPF 学到的 `/24` 链路前缀也打上 `LOCAL_COMM`

为什么加上这一步后就能 ping 通：

- 这一步的作用不是重新学习路由，而是把“已经通过 OSPF 学到、但原本不会外宣”的内部链路前缀补上外宣资格
- 边界路由器在 `master4` 里本来就能看到这些 `source = RTS_OSPF` 的 `/24` 前缀
- 新增 `master4 -> t_bgp` 这条 pipe 之后，这些前缀会被送入 `t_bgp`
- 进入 `t_bgp` 的同时会补上 `LOCAL_COMM`
- 一旦带上 `LOCAL_COMM`，它们就满足原来的 eBGP 导出条件：

```text
export where bgp_large_community ~ [LOCAL_COMM, CUSTOMER_COMM]
```

- 这样外部 AS 就能通过 eBGP 学到这些之前缺失的 `net_*` 前缀
- 当源和目的两侧都能对称地学到对方的内部链路前缀后，去程和回程路径才能同时成立
- 所以补上这一步之后，原先因为“返回路由缺失”导致的跨 AS `net_*` ping 不通问题才会收敛

修改点：

- 文件：[Ebgp.py](/home/lxl/k8s/seedemu/layers/Ebgp.py)
- 在 `__createPeer()` 中，对参与 eBGP 的边界路由器新增了一条 `addTablePipe('master4', 't_bgp', ...)`
- 过滤逻辑是：
  - 只接受 `RTS_OSPF`
  - 只接受 `/24`
  - 给前缀补 `LOCAL_COMM`
  - 保持现有 eBGP export policy 不变

设计原因：

- 当前生成拓扑中：
  - `net_*` 点到点链路前缀使用 `/24`
  - `ix*` 前缀通常是 `/16`
  - loopback 是 `/32`
- 因此用 `source = RTS_OSPF && net.len = 24` 可以较稳定地把内部链路前缀外宣出去，同时避免把 loopback 和 IX 前缀一并放大外宣

部署时的一个额外陷阱：生成物是新的，但运行中的 Pod 仍可能是旧配置

- 后续排查中发现，`compile` 生成的 `bird.conf` 已经包含了新的 `master4 -> t_bgp` 逻辑，但 live Pod 里的 `/etc/bird/bird.conf` 仍然是旧版本
- 这不是因为 `Ebgp.py` 修改失效，而是因为部署时容器镜像没有真正更新到运行节点
- 根因通常是这几个条件同时出现：
  - 镜像名一直复用同一个 `:latest`
  - manifest 中 `imagePullPolicy` 使用 `IfNotPresent`
  - 某个节点本地已经缓存了旧的同名镜像
- 在这种情况下，Kubernetes 会直接复用节点上的旧镜像，而不会重新从 registry 拉取新 build 的内容
- 结果就是：
  - 实验目录里的生成物是新的
  - registry 里的镜像可能也是新的
  - 但真正启动起来的 Pod 仍然跑着旧的 `bird.conf`
  - 于是看起来像“代码已经改了，但路由现象完全没变”

如何识别这是镜像缓存问题：

- 对比实验目录中的生成 `bird.conf` 与 live Pod 中的 `/etc/bird/bird.conf`
- 如果前者已经包含 `master4 -> t_bgp`，后者却没有，基本可以确定是镜像更新没有真正落到运行 Pod
- 同时可以检查：
  - Pod 的 `image`
  - Pod 的 `imageID`
  - Pod 的 `imagePullPolicy`

如何避免这个问题：

- 更稳妥的方式是每轮实验使用唯一镜像 tag，而不是一直复用 `:latest`
- 如果暂时继续使用 `:latest`，则应把 `imagePullPolicy` 设为 `Always`
- 这样每次 Pod 启动时都会强制向 registry 拉取镜像，避免继续复用节点本地缓存的旧镜像
- 只有确认运行中的 Pod 真正吃到了新镜像，前面的 `Ebgp.py` 修复才会在现网路由行为中体现出来

`peer` / `provider-customer` 关系下为什么会出现“同一个 AS 里有的地址能 ping，有的不能 ping”

- 参考文件：
  - [Ebgp.py](/home/lxl/seed-emulator/seedemu/layers/Ebgp.py)
  - [transit_as.py](/home/lxl/seed-emulator/examples/basic/A01_transit_as/transit_as.py)
- 在这个例子里，AS2 的边界路由器是连接 `ix100` 的 `r1`
- `10.2.0.253` 所在的 `net0` 是 `r1` 的直连前缀
- `10.2.1.254` 所在的 `net1` 不是 `r1` 的直连前缀，而是 AS2 内部通过 OSPF / iBGP 传播到边界路由器的前缀

为什么 `peer` 下 `10.2.0.253` 能通但 `10.2.1.254` 不通：

- 当前 `Ebgp.py` 的 BGP bootstrap 逻辑里，只有 `t_direct -> t_bgp` 会给前缀补 `LOCAL_COMM`
- 对边界路由器 `r1` 而言：
  - `net0` 属于本地直连前缀，所以会被打上 `LOCAL_COMM`
  - `net1` 属于内部学来的前缀，默认不会自动带上 `LOCAL_COMM`
- 而 `Peer` 关系的导出条件是：

```text
export where bgp_large_community ~ [LOCAL_COMM, CUSTOMER_COMM]
```

- 所以：
  - `10.2.0.0/24` 因为带有 `LOCAL_COMM`，可以对 peer 外宣
  - `10.2.1.0/24` 虽然边界路由器已经学到，但因为没有 `LOCAL_COMM / CUSTOMER_COMM`，不会继续对 peer 外宣
- 结果就是 AS151 可以学到 `10.2.0.0/24`，但学不到 `10.2.1.0/24`

为什么把和 AS2 有关的关系改成 `Provider` 后就都通了：

- 在 `Ebgp.py` 中，`Provider` 关系对应的是：
  - A 侧对 B 侧 `export all`
  - B 侧只导出 `[LOCAL_COMM, CUSTOMER_COMM]`
- 如果调用形式是 `addPrivatePeering(ix, 2, 151, Provider)`，那么 AS2 这一侧就是 provider
- provider 侧对 customer 侧会 `export all`
- 这样 AS2 边界路由器已经学到的内部前缀也会一并发给 AS151
- 所以像 `10.2.1.0/24` 这种原本在 `peer` 关系下不会被放行的内部链路前缀，在 `provider-customer` 关系下也能被学到

进一步的坑：customer AS 自己如果也有“内部非直连前缀”，同样可能导不出去

- `provider-customer` 并不等于“customer 内所有前缀天然都能被 provider 学到”
- 如果 customer AS 的某个前缀正好是边界路由器直连的，那么它通常会进入 `t_direct`，并被打上 `LOCAL_COMM`，于是可以正常向 provider 外宣
- 但如果 customer AS 内部也做成了多跳结构，例如：
  - 边界路由器只直连 `net0`
  - 另一个内部路由器再挂一个 `net1`
  - 主机地址实际落在这个内部 `net1` 上
- 那么这个 `net1` 到达 customer 边界路由器时，同样只是 OSPF / iBGP 学来的内部前缀
- 在当前这版 `Ebgp.py` 下，它默认也不会被自动补上 `LOCAL_COMM`
- 因此它也可能无法满足：

```text
export where bgp_large_community ~ [LOCAL_COMM, CUSTOMER_COMM]
```

- 结果就是：
  - customer 的边界直连前缀可以正常导出
  - customer 的内部非直连前缀也可能像前面 AS2 的 `10.2.1.0/24` 一样，学得到但导不出去

这类问题的统一判断方法：

- 不要只看“前缀属于哪个 AS”
- 更关键的是看“这个前缀到达边界路由器时，是不是被当作可外宣的 `LOCAL_COMM / CUSTOMER_COMM` 前缀”
- 如果它只是内部传播到边界，但没有被补社区属性，那么：
  - 在 `peer` 下通常不会导出
  - 在 `customer -> provider` 方向也同样可能不会导出
- 只有在边界路由器上给这类内部前缀补上合适的 community，或者放宽 export policy，它们才会被继续外宣

后续动作：

- 修改代码后需要重新 `compile -> build -> deploy -> start-bird -> start-kernel`
- 再重新验证这些之前学不到的 `net_*` 前缀是否已经进入外部 AS 的 BIRD 路由表
- 重点验证：

```bash
birdc show route for 6.159.12.0/24 all
birdc show route for 8.218.4.0/24 all
```

### 问题 6：`clean` 后 API 持续很慢，`k3s` 一直卡在 `activating (start)`

现象：

- 某一轮大规模实验在 `deploy` 后因为 Multus 问题、`ContainerCreating`、`SandboxChanged` 等异常状态积压了大量对象
- 执行 `clean` 后，namespace 进入 `Terminating`
- 即使节点仍然全部 `Ready`，API 仍长期很卡
- `k3s` 反复重启或 master 重启后，`systemctl status k3s` 仍长期停留在：

```text
Active: activating (start)
```

- `kubectl get --raw=/readyz?verbose` 长期失败，典型失败项包括：

```text
[-]etcd failed
[-]etcd-readiness failed
[-]informer-sync failed
[-]poststarthook/rbac/bootstrap-roles failed
```

- `journalctl -u k3s` 中大量出现：

```text
Slow SQL
http: Handler timeout
```

- 且慢查询反复命中：

```text
/registry/pods/seedemu-k3s-real-topo/...
```

根因：

- 不是 worker 节点故障
- 不是磁盘满
- 不是简单的 “k3s 进程没起来”
- 真正问题是 master 上的 K3s sqlite/kine 数据库 `state.db` 被这一轮实验 namespace 的历史 revision 和 event 键拖慢了
- 本次实际排查结果：
  - `state.db` 约 `378MB`
  - `kine` 总记录数 `130920`
  - 其中 `seedemu-k3s-real-topo` 相关记录 `129378`
  - `compact_rev` 明显落后
  - 其中绝大多数 backlog 都属于本次实验 namespace

这意味着：

- 单纯重复 `clean`
- 单纯 `systemctl restart k3s`
- 单纯重启 master VM

都不一定能恢复 control-plane，因为问题不在进程生命周期，而在 datastore 中这批历史键已经把恢复链路拖住了。

本次实际修复流程：

1. 停掉 master 上的 `k3s`
2. 备份 `state.db`
3. 检查 `kine` 表中该 namespace 的历史键体量
4. 定向删除：

```text
/registry/%seedemu-k3s-real-topo%
```

5. 对 sqlite 执行 `VACUUM`
6. 重新启动 `k3s`
7. 验证 `/readyz` 和 `kubectl get nodes`

本次实际修复结果：

- 清理前：
  - `before_total=130920`
  - `before_ns_rows=129378`
  - `before_ns_after_compact=124389`
- 清理后：
  - `after_total=1542`
  - `after_ns_rows=0`
  - `state.db` 从约 `378MB` 收缩到约 `3.3MB`
- 修复后：
  - `k3s.service -> active (running)`
  - `/readyz -> passed`
  - 9 个 node 全部恢复 `Ready`
  - 旧 namespace 不再存在

修复脚本：

- Multus 兼容桥接修复：
  - [ensure_multus_bridge_9node.sh](/home/lxl/k8s/lxl/ensure_multus_bridge_9node.sh)
- K3s sqlite/kine 定向清理脚本：
  - [repair_k3s_state_db_seedemu_namespace.py](/home/lxl/k8s/lxl/repair_k3s_state_db_seedemu_namespace.py)

推荐修复命令顺序：

```bash
# 1. 先停掉 master 上的 k3s
ssh -i ~/.ssh/id_ed25519 ubuntu@192.168.122.110 'sudo -n systemctl stop k3s'

# 2. 备份 state.db
ssh -i ~/.ssh/id_ed25519 ubuntu@192.168.122.110 \
  'sudo -n mkdir -p /var/lib/rancher/k3s/server/db/backup_manual && \
   sudo -n cp -a /var/lib/rancher/k3s/server/db/state.db /var/lib/rancher/k3s/server/db/backup_manual/state.db.$(date +%Y%m%d_%H%M%S)'

# 3. 上传并执行定向清理脚本
scp -i ~/.ssh/id_ed25519 ~/k8s/lxl/repair_k3s_state_db_seedemu_namespace.py \
  ubuntu@192.168.122.110:/tmp/repair_k3s_state_db_seedemu_namespace.py
ssh -i ~/.ssh/id_ed25519 ubuntu@192.168.122.110 \
  'sudo -n python3 /tmp/repair_k3s_state_db_seedemu_namespace.py'

# 4. 重启 k3s
ssh -i ~/.ssh/id_ed25519 ubuntu@192.168.122.110 'sudo -n systemctl start k3s'

# 5. 验证 control-plane 恢复
export KUBECONFIG=/home/lxl/k8s/output/kubeconfigs/seedemu-k3s.yaml
kubectl get --raw=/readyz?verbose
kubectl get nodes -o wide
kubectl get ns
```

下次再碰到这种情况时的建议：

1. 不要反复重跑 `clean`
   - 第二次、第三次重入通常只会让 API 更慢

2. 先判断是不是 datastore 已经被单轮实验残留拖慢
   - 看：

```bash
ssh -i ~/.ssh/id_ed25519 ubuntu@192.168.122.110 'sudo -n systemctl status k3s --no-pager -l | sed -n "1,40p"'
export KUBECONFIG=/home/lxl/k8s/output/kubeconfigs/seedemu-k3s.yaml
kubectl get --raw=/readyz?verbose
ssh -i ~/.ssh/id_ed25519 ubuntu@192.168.122.110 'sudo -n journalctl -u k3s --since "5 min ago" --no-pager | tail -n 120'
```

3. 如果持续出现下面这些信号：
   - `k3s` 长时间 `activating (start)`
   - `/readyz` 长期卡在 `etcd` / `informer-sync`
   - `journalctl` 里持续 `Slow SQL`
   - 慢查询持续命中某个已删 namespace

   就不要继续依赖“重启 k3s / 重启 master VM”作为主修复手段，而应直接转向 sqlite/kine 定向清理。

4. 做数据库修复前一定先备份 `state.db`
   - 不要在 `k3s` 运行时直接改库

5. 重新开始实验前，建议固定执行：

```bash
cd /home/lxl/k8s/lxl
source ./env_9node.sh
./ensure_multus_bridge_9node.sh
```

这样可以先避免 Multus 兼容路径问题再次把业务 Pod 卡在 `ContainerCreating`，从源头减少后续 control-plane 被异常对象拖慢的概率。

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

## 故障处理：deploy 过程中 `worker1 NotReady`

### 现象

- `deploy` 过程中 Running Pod 数量停在某个值附近，不再继续明显增长
- 同时大量 Pod 长时间停留在 `Pending`
- `kubectl get nodes -o wide` 显示 `seed-k3s-worker1` 变成 `NotReady`
- 即使继续等待，最近事件中仍然持续出现新的 `FailedCreatePodSandBox`

### 为什么可以判断这不是“只是慢”，而是节点已经出问题

最直接的证据来自：

```bash
export KUBECONFIG=/home/lxl/k8s/output/kubeconfigs/seedemu-k3s.yaml
kubectl describe node seed-k3s-worker1
```

如果看到下面这类信息，就说明节点已经不是单纯繁忙，而是 kubelet 失联：

- `Ready: Unknown`
- `Reason: NodeStatusUnknown`
- `Message: Kubelet stopped posting node status`
- `Taints:`
  - `node.kubernetes.io/unreachable:NoExecute`
  - `node.kubernetes.io/unreachable:NoSchedule`

辅助证据：

```bash
kubectl get nodes -o wide
kubectl -n ${SEED_NAMESPACE} get pods --field-selector=status.phase=Pending --no-headers | head -n 40
kubectl -n ${SEED_NAMESPACE} get events --field-selector type=Warning --sort-by=.lastTimestamp | tail -n 80
```

这次实际出现的典型 warning 包括：

- `FailedCreatePodSandBox`
- `plugin type="multus" failed`
- `plugin type="flannel" failed`
- `failed to connect ... to bridge cni0: exchange full`

这说明 deploy 期间 CNI / bridge 侧已经顶满，新的 Pod sandbox 无法继续创建。

### 这次为什么会发展成 `worker1 NotReady`

- deploy 期间创建 Pod 的速度过快
- Multus / Flannel 在 worker1 上大量创建 veth 与 bridge 连接
- `cni0` / bridge 侧出现 `exchange full`
- Pod sandbox 持续创建失败
- 节点网络与 kubelet / containerd 状态一起恶化
- 最终 kubelet 停止正常上报 node status
- 控制面将该节点标记为 `NotReady / unreachable`

### 当时的正确处理顺序

#### 1. 不要继续等 deploy 自己收敛

- 一旦已经出现 `worker1 NotReady + FailedCreatePodSandBox + exchange full`
- 继续等待通常收益不高
- 正确做法是先停止当前 deploy，不再继续提交新对象

#### 2. 先执行 clean

- 先删 controllers，再删 namespace
- 如果 namespace 卡在 `Terminating`，继续检查：

```bash
kubectl get namespace ${SEED_NAMESPACE} -o yaml
```

如果已经看到：

- `pods=0, deploy=0, sts=0, ds=0, jobs=0`
- 但 namespace 仍然 `Terminating`
- `spec.finalizers` 里还挂着 `kubernetes`

说明已经进入“内容基本删空，但 finalizer 没退出”的阶段。

#### 3. 强制 finalize 卡住的 namespace

本次实际采用的是直接清空 namespace finalizers：

```bash
tmp=$(mktemp)
KUBECONFIG=/home/lxl/k8s/output/kubeconfigs/seedemu-k3s.yaml kubectl get namespace seedemu-k3s-real-topo -o json > "$tmp"
python3 - <<'PY' "$tmp"
import json, sys
p=sys.argv[1]
with open(p,'r',encoding='utf-8') as f:
    data=json.load(f)
data.setdefault('spec', {})['finalizers']=[]
with open(p,'w',encoding='utf-8') as f:
    json.dump(data,f)
PY
KUBECONFIG=/home/lxl/k8s/output/kubeconfigs/seedemu-k3s.yaml kubectl replace --raw /api/v1/namespaces/seedemu-k3s-real-topo/finalize -f "$tmp"
rm -f "$tmp"
```

也可以使用当前的 `clean` 脚本配合：

```bash
export SEED_CLEAN_FORCE_FINALIZER_CLEANUP=true
./clean
```

#### 4. 再修复 `worker1`

先确认 VM 本身还活着，不要一上来就假设是 K3s 逻辑问题：

```bash
virsh -c qemu:///system list --all
virsh -c qemu:///system domstate seed-k3s-worker1
virsh -c qemu:///system domifaddr seed-k3s-worker1
```

这次的实际情况是：

- VM 仍然 `running`
- 但一度 `No route to host`
- `domifaddr` 也拿不到地址

随后采用的最小恢复动作是：

```bash
virsh -c qemu:///system reboot seed-k3s-worker1
```

#### 5. 重启后检查 guest 网络与 `k3s-agent`

```bash
ssh -o StrictHostKeyChecking=no ubuntu@192.168.122.111 \
  'hostname; ip addr show; sudo -n systemctl is-active k3s-agent || true'
```

这次恢复后可以看到：

- `ens2` 重新拿回了 `192.168.122.111/24`
- `k3s-agent` 从 `activating` 最终恢复到 `active (running)`

再进一步确认：

```bash
ssh -o StrictHostKeyChecking=no ubuntu@192.168.122.111 \
  'sudo -n systemctl status k3s-agent --no-pager; echo ---; sudo -n journalctl -u k3s-agent -n 120 --no-pager'
```

#### 6. 只有节点重新 `Ready` 之后，才允许重新 deploy

最终以这条命令为准：

```bash
kubectl get nodes -o wide
```

本次修复完成后的状态是：

- `seed-k3s-master: Ready`
- `seed-k3s-worker1: Ready`
- `seed-k3s-worker2: Ready`

### 经验结论

- `worker1 NotReady` 不等于“继续等等就能恢复”
- 当它和 `FailedCreatePodSandBox`、`exchange full` 同时出现时，应视为 deploy 已进入硬阻塞
- 正确顺序是：
  - 停 deploy
  - clean
  - 必要时强制 finalize namespace
  - 修复 `worker1`
  - 等节点回到 `Ready`
  - 再重新 deploy

---

## 大规模部署补充：`maxPods=4000` 且 `PodCIDR=/20` 仍然可能先撞到 `cni0` bridge 上限

### 结论

- `maxPods` 只表示 kubelet 调度层面允许该节点最多接收多少个 Pod
- `PodCIDR=/20` 只表示 Flannel / K3s 给该节点分配了足够多的 Pod IP
- 这两个都不等于 Linux 网络实现层真的能稳定承载这么多 Pod
- 对本实验环境，真实更早出现的瓶颈是：
  - `flannel`
  - `cni0` bridge
  - `veth` 数量
  - bridge FDB / hash 压力
  - Multus 额外网卡数量

因此，即使节点配置已经显示：

```text
maxPods: 4k
podCIDR: /20
```

仍然可能在单节点只有约 `1000` 个 Pod 时，就先出现：

- `FailedCreatePodSandBox`
- `plugin type="multus" failed`
- `plugin type="flannel" failed`
- `failed to connect "veth..." to bridge cni0: exchange full`

### 为什么会这样

- `maxPods` 和 `PodCIDR` 只解决“能不能调度、能不能分配 Pod IP”
- 但每个 Pod 真正落地时，还要经过 `cbr0 -> flannel -> cni0 -> veth`
- 本项目里每个 Pod 不仅有默认主网卡，还常常有多个 Multus 附加 `net_*` 接口
- 所以“每增加 1 个 Pod”带来的网络对象数量，明显高于普通单网卡 K8s 集群
- 当 bridge FDB、veth、哈希桶压力过高时，就会先在底层网络实现处失败，而不是先碰到 `maxPods`

### 这次 3083 规模实验中的实际证据

在这次 `3083` 规模实验中，三台节点都已经配置为：

- `maxPods: 4k`
- `PodCIDR: /20`

但在每台节点大约 `1027-1028` 个 Pod 时，仍然出现了 `ContainerCreating` 长时间不收敛。对应的直接证据是：

- 三台节点都持续报 `failed to connect "veth..." to bridge cni0: exchange full`
- 三台节点的 `cni0` bridge `hash_max` 都是 `4096`
- 三台节点的 `cni0` FDB 项都已经达到 `5300+`
- 三台节点的 `veth` 数量都已经达到 `5640+`

这说明真实先撞上的不是 Pod 数量许可，而是 `cni0` / bridge / veth 侧的承载能力。

### 验证命令

#### 1. 先看节点声明出来的理论容量

```bash
export KUBECONFIG=/home/lxl/k8s/output/kubeconfigs/seedemu-k3s.yaml
kubectl get nodes -o wide
kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\tPodCIDR: "}{.spec.podCIDR}{"\n"}{end}'
cd ~/k8s/lxl
./check_resources
```

#### 2. 统计每个节点当前承载了多少 Pod，以及有多少 Pod 卡在 `ContainerCreating`

```bash
export KUBECONFIG=/home/lxl/k8s/output/kubeconfigs/seedemu-k3s.yaml
kubectl -n seedemu-k3s-real-topo get pods -o wide | \
  awk 'NR>1 {count[$7]++; if ($3=="ContainerCreating") stuck[$7]++} END {for (n in count) printf "%s\ttotal=%d\tcreating=%d\n", n, count[n], stuck[n]+0}' | sort
```

#### 3. 列出具体卡住的 Pod 以及它们分布在哪些节点

```bash
export KUBECONFIG=/home/lxl/k8s/output/kubeconfigs/seedemu-k3s.yaml
kubectl -n seedemu-k3s-real-topo get pods -o wide | \
  awk '$3=="ContainerCreating" {print $1"\t"$7}' | sort
```

#### 4. 看 namespace 最近事件，确认是不是 `exchange full`

```bash
export KUBECONFIG=/home/lxl/k8s/output/kubeconfigs/seedemu-k3s.yaml
kubectl -n seedemu-k3s-real-topo get events --sort-by=.lastTimestamp | tail -n 120
```

如果看到这类报错：

- `FailedCreatePodSandBox`
- `plugin type="multus" failed`
- `plugin type="flannel" failed`
- `failed to connect "veth..." to bridge cni0: exchange full`

就说明不是镜像问题，也不是单纯调度慢，而是主 CNI bridge 已经顶满。

#### 5. 在各节点上直接看 `cni0` 的 bridge 参数、FDB 数量和 `veth` 数量

```bash
ssh -i ~/.ssh/id_ed25519 -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no ubuntu@192.168.122.110 \
  'cat /sys/class/net/cni0/bridge/hash_max; bridge fdb show br cni0 | wc -l; ip link show type veth | wc -l'

ssh -i ~/.ssh/id_ed25519 -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no ubuntu@192.168.122.111 \
  'cat /sys/class/net/cni0/bridge/hash_max; bridge fdb show br cni0 | wc -l; ip link show type veth | wc -l'

ssh -i ~/.ssh/id_ed25519 -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no ubuntu@192.168.122.112 \
  'cat /sys/class/net/cni0/bridge/hash_max; bridge fdb show br cni0 | wc -l; ip link show type veth | wc -l'
```

本次实测看到的是：

- `hash_max = 4096`
- `FDB ≈ 5300+`
- `veth ≈ 5640+`

#### 6. 如需进一步确认 kubelet / k3s 本身也在重复报同一个错误

```bash
ssh -i ~/.ssh/id_ed25519 -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no ubuntu@192.168.122.110 \
  'sudo -n journalctl -u k3s --no-pager -n 40'

ssh -i ~/.ssh/id_ed25519 -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no ubuntu@192.168.122.111 \
  'sudo -n journalctl -u k3s-agent --no-pager -n 40'

ssh -i ~/.ssh/id_ed25519 -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=no ubuntu@192.168.122.112 \
  'sudo -n journalctl -u k3s-agent --no-pager -n 40'
```

### 如何评估“单节点到底能放多少 Pod”

不要只看：

- `maxPods`
- `PodCIDR`

对于当前这种 `flannel + multus + 多附加接口` 的场景，更应该盯住：

- 单节点 Pod 数
- `bridge fdb show br cni0 | wc -l`
- `ip link show type veth | wc -l`
- 最近事件里是否已经出现 `exchange full`

这次实测中：

- 每个节点到 `1027-1028` 个 Pod 左右时开始出现失败
- 因此这个值只能视为“已经碰到边界”的危险值，不能视为可稳定运行值
- 更保守的单节点目标应先控制在 `700-900` Pod 范围内，再逐步压测

经验上应把：

- 第一次出现 `exchange full` 时的 Pod 数

视为“硬边界附近”，然后至少再向下留出 `10%-20%` 余量，作为后续稳定运行的单节点容量目标。

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
   - 对于 K3s，还需要保证兼容路径存在：
     ```bash
     /etc/cni/net.d/multus.d -> /var/lib/rancher/k3s/agent/etc/cni/net.d/multus.d
     ```
   - 如果缺少这条桥接，业务 Pod 会反复出现 `FailedCreatePodSandBox`，并长期停留在 `ContainerCreating`
