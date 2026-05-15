# setup 目录说明

## Up 后常用 kubectl 命令

当前默认 namespace 是 `seedemu-k3s-real-topo`，默认 kubeconfig 是：

```bash
export KUBECONFIG=/home/lxl/k8s/origin_k8s/setup/seedemu-k3s.kubeconfig.yaml
export NS=seedemu-k3s-real-topo
```

查看所有 Pod：

```bash
kubectl -n "$NS" get pods
kubectl -n "$NS" get pods -o wide
```

统计 Pod 状态：

```bash
kubectl -n "$NS" get pods --no-headers | awk '{c[$3]++} END{for (s in c) print s, c[s]}'
```

查看所有 Deployment：

```bash
kubectl -n "$NS" get deploy
kubectl -n "$NS" get deploy -o wide
```

查看某个 Pod 的详细信息和事件：

```bash
kubectl -n "$NS" describe pod <pod-name>
```

进入某个 Pod：

```bash
kubectl -n "$NS" exec -it <pod-name> -- bash
```

如果容器里没有 `bash`，用：

```bash
kubectl -n "$NS" exec -it <pod-name> -- sh
```

按名字模糊查找 Pod，然后进入第一个匹配项：

```bash
POD=$(kubectl -n "$NS" get pods --no-headers | awk '/as150h-host-0/{print $1; exit}')
kubectl -n "$NS" exec -it "$POD" -- bash
```

在 Pod 里直接执行命令，例如 ping 另一个仿真地址：

```bash
kubectl -n "$NS" exec "$POD" -- ping -c 3 10.151.0.254
```

查看 Pod 日志：

```bash
kubectl -n "$NS" logs <pod-name>
```

查看当前集群节点：

```bash
kubectl get nodes -o wide
```

`/home/lxl/k8s/origin_k8s/setup` 是原生 K8s/K3s 实验流程里的基础设施初始化层。它负责创建 KVM VM、打开 VM 限制、把这些 VM 组装成 K3s 集群、准备 registry 和 bootstrap 镜像，并输出 running 阶段需要读取的 kubeconfig/env 文件。

边界要清楚：`setup` 不负责编译仿真拓扑，也不负责部署仿真 workload。compile 在 `../emulate`，build/up/clean 在 `../running`。`running` 只通过 `setup/seedemu-k3s.env.sh` 读取 registry 地址和 kubeconfig 路径。

## 推荐流程

```bash
cd /home/lxl/k8s/origin_k8s/setup

# 0. 可选：提前准备大文件和镜像缓存。大文件不需要上传到仓库。
./prepare_setup_assets.sh kvm_template.yaml

# 1. 清理上一轮由 resolved plan 记录的新 VM 和 setup 输出。
./clean_kvm_from_resolved.sh kvm_template.yaml

# 2. 根据 kvm_template.yaml 创建 KVM VM。
./kvm_from_yaml.sh kvm_template.yaml

# 3. 对新 VM 打开高密度实验需要的 OS 限制。
./unlock_vm_limits_from_yaml.sh kvm_template.yaml

# 4. 把这些 VM 配置成 K3s 集群，并准备 registry / bootstrap 镜像。
./k3s_from_yaml.sh kvm_template.resolved-nodes.tsv
```

完成后会生成：

- `seedemu-k3s.kubeconfig.yaml`：宿主机 `kubectl` 访问新 K3s API server 的 kubeconfig。
- `seedemu-k3s.env.sh`：`../running/Makefile` 默认读取的 registry/kubeconfig 信息。
- `seedemu-k3s.inventory.yaml`：解释性集群 inventory，记录节点、registry、K3s 参数。

然后运行 workload：

```bash
cd /home/lxl/k8s/origin_k8s/emulate
source /home/lxl/anaconda3/etc/profile.d/conda.sh
conda activate seedpy310
python mini_internet_k8s_native_compile.py

cd /home/lxl/k8s/origin_k8s/running
make preflight
make build
make up
```

## 目录作用

| 路径 | 类型 | 作用 |
| --- | --- | --- |
| `base/` | 目录 | Ubuntu cloud image 的存放位置。当前 `base/jammy-server-cloudimg-amd64.img` 可以是实际镜像，也可以是指向已有基础镜像的符号链接，避免把大文件提交或上传。 |
| `cloud-init/` | 目录 | `kvm_from_yaml.sh` 为每台 VM 生成的 cloud-init `user-data.yaml` 和 `meta-data.yaml`。用于设置 hostname、SSH 用户、sudo 权限、authorized key。 |
| `image-cache/` | 目录 | `prepare_setup_assets.sh` 或 `k3s_from_yaml.sh` 在宿主机准备的镜像 tar 缓存。用于把 `registry:2`、Multus、SEED base/router 等镜像导入 VM，避免 fresh VM 自己从公网拉镜像。 |
| `tmp/` | 目录 | 临时文件目录，主要给 K3s Ansible inventory/playbook 临时副本使用。 |
| `__pycache__/` | 目录 | Python 解释器自动生成的 bytecode 缓存，无需手工维护。 |

## 配置与产物文件

| 文件 | 作用 | 谁读取 | 谁生成/修改 |
| --- | --- | --- | --- |
| `kvm_template.yaml` | 最小 KVM 模板。只需要描述 master/worker 的 vCPU、内存、磁盘和 worker 数量。 | `kvm_from_yaml.sh`、`unlock_vm_limits_from_yaml.sh`、`clean_kvm_from_resolved.sh` | 用户编辑 |
| `kvm_template.resolved-nodes.tsv` | 实际创建出来的 VM 清单，包含 `name role ip mac vcpus memory_mb disk_gb`。这是后续清理和 K3s 初始化的关键输入。 | `unlock_vm_limits_from_yaml.sh`、`k3s_from_yaml.sh`、`clean_kvm_from_resolved.sh` | `kvm_from_yaml.sh` |
| `k3s_template.yaml` | 显式选择 VM 组成 K3s 集群的模板，适合不用 resolved TSV、手工指定已有 VM 时使用。 | `k3s_from_yaml.sh`、`k3s_config.py` | 用户编辑 |
| `seedemu-k3s.kubeconfig.yaml` | K3s kubeconfig，宿主机和 running 阶段用它访问 API server。 | `kubectl`、`../running/Makefile`、`make preflight/build/up` | `k3s_from_yaml.sh` |
| `seedemu-k3s.env.sh` | setup 向 running 暴露的最小环境文件。当前主要包含 `SEED_REGISTRY_HOST`、`SEED_REGISTRY_PORT`、`SEED_OUTPUT_KUBECONFIG`、节点 IP。 | `../running/Makefile` | `k3s_from_yaml.sh` 通过 `k3s_config.py write-env-file` 生成 |
| `seedemu-k3s.inventory.yaml` | 解释性集群 inventory，记录 cluster name、K3s CIDR/max-pods、registry、节点列表和资源。 | 人读、后续调试/扩展 | `k3s_from_yaml.sh` 通过 `k3s_config.py write-cluster-inventory` 生成 |

注意：`seedemu-k3s.env.sh` 不保存镜像常量。镜像 bootstrap 是 setup 内部行为，写死在 `k3s_from_yaml.sh` 中，避免 running 和 setup 过度耦合。

## 脚本与代码

### `prepare_setup_assets.sh`

作用：提前准备 setup 阶段需要的大文件和 Docker 镜像 tar 缓存。这个脚本不创建 VM、不安装 K3s，只做本地资产准备，适合新机器初始化或避免把大文件上传进仓库。

用法：

```bash
cd /home/lxl/k8s/origin_k8s/setup
./prepare_setup_assets.sh kvm_template.yaml
```

它会调用：

- `cluster_config.py <yaml> kvm-env`：读取 Ubuntu cloud image 的 URL、目标路径、legacy 复用路径。
- 宿主机 Docker：准备并 `docker save` bootstrap 镜像到 `image-cache/`。

Ubuntu cloud image 默认下载地址来自 `cluster_config.py`：

- `jammy`：`https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img`
- `noble`：`https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img`

实际目标路径默认是：

- `base/jammy-server-cloudimg-amd64.img`
- 或 `base/noble-server-cloudimg-amd64.img`

如果 `/home/lxl/k8s/output` 或 legacy 路径下已经有同名 cloud image，脚本会在 `setup/base/` 下创建符号链接复用；否则从上述 Ubuntu cloud image URL 联网下载。

Docker 镜像准备策略是：

- 先用 `docker image inspect <image>` 检查宿主机本地是否已有镜像。
- 如果本地没有，对通用公网镜像执行 `docker pull`，Docker Hub 镜像会优先尝试原始地址，失败后尝试 `docker.m.daocloud.io` 镜像源。
- 对 SEED base/router 镜像，若宿主机存在 `/home/lxl/seed-emulator/docker_images/multiarch/seedemu-base` 和 `seedemu-router`，则本地 `DOCKER_BUILDKIT=1 docker build`；否则再尝试 pull。
- 最后把需要传入 VM 的镜像 `docker save` 成 tar，放入 `image-cache/`。

它会准备这些 Docker 镜像 tar：

- `registry:2`
- `ghcr.io/k8snetworkplumbingwg/multus-cni:snapshot`
- `ubuntu:20.04`
- `handsonsecurity/seedemu-multiarch-base:buildx-latest`
- `handsonsecurity/seedemu-multiarch-router:buildx-latest`

这些 tar 会放在：

```bash
/home/lxl/k8s/origin_k8s/setup/image-cache/
```

两个 compiler 使用的稳定 hash tag 不再单独保存为 tar：

- `98a2693c996c2294358552f48373498d:latest`
- `39e016aa9e819f203ebc1809245a5818:latest`

它们会由源镜像在宿主机和 master Docker 中通过 `docker tag` 生成，因此 `image-cache/` 可以不包含这两个 tar。

注意：`k3s_from_yaml.sh` 内部仍然有同等镜像准备逻辑，所以即使没有提前运行 `prepare_setup_assets.sh`，完整 setup 流程也可以自己准备。但显式提前准备更适合复现、排查网络下载问题，以及确认哪些大文件不应该上传。

### `cluster_config.py`

这是 KVM 阶段的 YAML 解析器。它读取 `kvm_template.yaml`，结合当前 libvirt 里已有 VM、DHCP reservation 和 DHCP lease，计算不会冲突的新 VM 名字、IP 和 MAC。

主要子命令：

- `kvm-env`：输出 KVM 创建阶段需要的 shell 变量，例如 libvirt network、disk dir、cloud-init dir、SSH key、base image 路径。
- `nodes-tsv`：输出计划创建的节点 TSV。若传入 `--existing-tsv`，会自动避开已有名字/IP/MAC。
- `write-inventory`、`write-env`、`write-ansible-inventory`：保留的扩展子命令，目前 KVM 主流程主要使用 `kvm-env` 和 `nodes-tsv`。

被调用关系：

- `kvm_from_yaml.sh` 调用 `cluster_config.py <yaml> kvm-env` 和 `cluster_config.py <yaml> nodes-tsv --existing-tsv <file>`。
- `unlock_vm_limits_from_yaml.sh` 调用 `cluster_config.py <yaml> kvm-env`，并在没有 resolved TSV 时调用 `nodes-tsv`。
- `clean_kvm_from_resolved.sh` 调用 `cluster_config.py <yaml> kvm-env`，用来取得实际 disk/cloud-init/network 默认路径。

### `kvm_from_yaml.sh`

作用：创建或启动 KVM VM，但不安装 K3s。

输入：

- 默认输入：`kvm_template.yaml`
- 可显式传入其他 KVM YAML，例如 `./kvm_from_yaml.sh kvm.local.yaml`

内部调用/读取：

- 调用 `cluster_config.py ... kvm-env` 获取 KVM 默认参数。
- 调用 `virsh list --all --name`、`virsh net-dumpxml`、`virsh net-dhcp-leases` 收集已有 VM、DHCP reservation、DHCP lease。
- 调用 `cluster_config.py ... nodes-tsv --existing-tsv ...` 生成不冲突的节点计划。
- 使用 `curl` 下载 Ubuntu cloud image；若 `/home/lxl/k8s/output` 或 legacy 路径已有同名镜像，则创建符号链接复用。
- 使用 `qemu-img` 创建 qcow2 overlay disk。
- 使用 `virt-install` 创建 VM。
- 使用 `virsh net-update` 写入静态 DHCP host reservation。
- 使用 `ssh` 等待每台 VM 可登录。

产物：

- `<输入名>.resolved-nodes.tsv`，例如 `kvm_template.resolved-nodes.tsv`。
- `cloud-init/<vm-name>/user-data.yaml` 和 `meta-data.yaml`。
- `/data/lxl/k8s/origin_k8s/setup/disks/<vm-name>.qcow2`。
- libvirt domain 和 DHCP reservation。

产物用途：

- resolved TSV 是后续 `unlock_vm_limits_from_yaml.sh`、`k3s_from_yaml.sh`、`clean_kvm_from_resolved.sh` 的共同依据。
- cloud-init 文件只在 VM 首次创建时使用。
- qcow2 disk 保存 VM 状态；删除 disk 就等价于删除 VM 内部 K3s/Docker/containerd 状态。

### `unlock_vm_limits_from_yaml.sh`

作用：对 resolved TSV 里的 VM 打开高密度网络实验需要的 OS 限制。这个脚本只依赖 SSH，不依赖 K3s。

输入：

- 默认/常用：`kvm_template.yaml`
- 它会优先读取同名 resolved TSV，例如 `kvm_template.resolved-nodes.tsv`。

内部调用/读取：

- 调用 `cluster_config.py ... kvm-env` 获取 SSH 用户和 key。
- 读取 resolved TSV，拿到 VM name/IP。
- 通过 SSH 在每台 VM 上写入 limits/sysctl/systemd drop-in。

主要修改 VM 内部：

- `/etc/security/limits.conf`
- `/etc/sysctl.d/99-seed-vm-limits.conf`
- `/etc/systemd/system.conf.d/99-seed-vm-limits.conf`
- `/etc/systemd/user.conf.d/99-seed-vm-limits.conf`
- 若服务已存在，则为 `k3s`、`k3s-agent`、`containerd`、`docker` 写 systemd drop-in。
- `/usr/local/sbin/seed-vm-cni0-hashmax.sh`
- `/etc/systemd/system/seed-vm-cni0-hashmax.service`

产物用途：

- 提高 nofile/nproc/netns/neigh/conntrack/backlog 等上限。
- 预置未来 K3s 创建 `cni0` 后的 bridge `hash_max` 调整。
- 降低大规模 Pod/Multus/veth 场景下的系统限制干扰。

### `k3s_config.py`

这是 K3s 阶段的节点/配置解析器。它不创建 VM，只把 TSV/YAML 节点清单转换成 Ansible inventory、cluster inventory 和 env 文件。

支持输入：

- `--nodes-tsv kvm_template.resolved-nodes.tsv`
- `--config k3s_template.yaml`

主要子命令：

- `env`：输出 `k3s_from_yaml.sh` 所需的 shell 变量。
- `nodes-tsv`：标准化输出 K3s 节点 TSV。
- `write-ansible-inventory --output <file>`：生成给 Ansible playbook 使用的 inventory。
- `write-cluster-inventory`：生成 `seedemu-k3s.inventory.yaml`。
- `write-env-file`：生成 `seedemu-k3s.env.sh`。

默认 K3s 参数：

- `cluster_name=seedemu-k3s`
- `version=v1.28.5+k3s1`
- `cluster_cidr=10.42.0.0/16`
- `service_cidr=10.43.0.0/16`
- `node_cidr_mask_size_ipv4=20`
- `max_pods=4000`
- registry 默认在 master IP 的 `5000` 端口。

### `k3s_from_yaml.sh`

作用：把已有 VM 组装成 K3s 集群，并准备 running/build 所需的基础设施。

支持三种输入模式：

```bash
./k3s_from_yaml.sh
./k3s_from_yaml.sh kvm_template.resolved-nodes.tsv
./k3s_from_yaml.sh k3s_template.yaml
```

- 无参数：自动读取当前 libvirt VM 和 DHCP lease，按名字判断 master/worker；如果有多个 master-like VM 会报错。
- TSV：按 resolved TSV 组建集群，当前推荐方式。
- YAML：按 `k3s_template.yaml` 里显式列出的 VM 组建集群。

内部调用/读取：

- 调用 `k3s_config.py env` 读取 master IP、registry port、kubeconfig 输出路径等。
- 调用 `k3s_config.py nodes-tsv` 获取节点列表。
- 调用 `k3s_config.py write-ansible-inventory` 生成临时 Ansible inventory。
- 调用 `/home/lxl/k8s/ansible/k3s-install.yml` 安装 K3s、Multus、macvlan CNI、registry mirror 配置和节点 label。
- 使用宿主机 Docker 准备 bootstrap 镜像，再通过 `scp` 传入 VM。
- 使用 master VM 的 Docker 启动 registry。
- 使用 K3s containerd 导入 Multus 镜像。
- 使用 SSH 对节点追加 K3s/kubelet 高密度运行参数。

setup 阶段写死准备的关键镜像：

- `registry:2`
- `ghcr.io/k8snetworkplumbingwg/multus-cni:snapshot`
- `ubuntu:20.04`
- `handsonsecurity/seedemu-multiarch-base:buildx-latest`
- `handsonsecurity/seedemu-multiarch-router:buildx-latest`

这些镜像采用“宿主机准备 -> `docker save` -> `scp` -> VM `docker load` 或 `k3s ctr images import`”的方式，避免 fresh VM 在 setup/build 关键路径上从公网拉镜像。

两个稳定 hash tag：

- `98a2693c996c2294358552f48373498d:latest`
- `39e016aa9e819f203ebc1809245a5818:latest`

不需要作为独立 tar 传输；master 收到 source base/router 镜像后直接 `docker tag` 生成。

产物：

- `seedemu-k3s.kubeconfig.yaml`
- `seedemu-k3s.env.sh`
- `seedemu-k3s.inventory.yaml`
- `image-cache/*.tar`
- master VM 上运行的 `registry` 容器。
- 每个 K3s node 上导入的 Multus image。
- master Docker 中导入的 build base images。

产物用途：

- `seedemu-k3s.env.sh` 被 `../running/Makefile` include，用来决定 `REGISTRY_PREFIX` 和 `KUBECONFIG`。
- `seedemu-k3s.kubeconfig.yaml` 被 `kubectl`、`make preflight`、`make up` 使用。
- master registry 被 `make build` 推送 workload 镜像，以及 `make up` 时 K3s/containerd 拉镜像使用。
- build base images 保证 fresh master 第一次 `make build` 不再因为缺少 `ubuntu:20.04`、`98a269...`、`39e016...` 而失败。

### `clean_kvm_from_resolved.sh`

作用：按 resolved TSV 精确清理本轮创建的 VM 和 setup 输出，方便重走完整流程。

输入：

```bash
./clean_kvm_from_resolved.sh kvm_template.yaml
# 或
./clean_kvm_from_resolved.sh kvm_template.resolved-nodes.tsv
```

内部调用/读取：

- 若传入 YAML，会调用 `cluster_config.py ... kvm-env` 获取实际 network/disk/cloud-init 路径。
- 读取 `<输入名>.resolved-nodes.tsv`，不硬编码 VM 名字、IP、MAC。
- 调用 `virsh destroy/undefine` 删除 domain。
- 调用 `virsh net-update ... delete ip-dhcp-host` 删除 DHCP reservation。
- 修改 `/var/lib/libvirt/dnsmasq/virbr0.status` 去掉 stale DHCP lease。
- 删除 cloud-init 目录、qcow2 disk、resolved TSV。
- 删除 stale setup 输出：`seedemu-k3s.*`。

清理范围：

- KVM domain
- qcow2 disk
- cloud-init 目录
- libvirt DHCP reservation
- dnsmasq stale lease
- resolved TSV
- `seedemu-k3s.kubeconfig.yaml`
- `seedemu-k3s.inventory.yaml`
- `seedemu-k3s.env.sh`

不会清理：

- `../emulate/output`
- `../running` 文件
- 宿主机 Docker 镜像
- 旧集群或其他 kubeconfig 指向的 namespace
- `image-cache/` 里的镜像 tar 缓存

## 当前最小 KVM YAML

```yaml
master:
  vcpus: 12
  memory_mb: 10240
  disk_gb: 80

workers:
  count: 2
  vcpus: 6
  memory_mb: 10240
  disk_gb: 80
```

字段说明：

| 字段 | 是否必需 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `master.vcpus` | `master` 存在时必需 | 无 | master VM vCPU 数 |
| `master.memory_mb` | `master` 存在时必需 | 无 | master VM 内存，单位 MiB |
| `master.disk_gb` | `master` 存在时必需 | 无 | master VM 磁盘，单位 GiB |
| `workers.count` | `workers` 存在时必需 | 无 | worker VM 数量 |
| `workers.vcpus` | `workers.count > 0` 时必需 | 无 | 每个 worker 的 vCPU 数 |
| `workers.memory_mb` | `workers.count > 0` 时必需 | 无 | 每个 worker 的内存，单位 MiB |
| `workers.disk_gb` | `workers.count > 0` 时必需 | 无 | 每个 worker 的磁盘，单位 GiB |
| `ssh.user` | 可选 | `ubuntu` | VM 登录用户，同时写入 cloud-init |
| `ssh.key` | 可选 | `~/.ssh/id_ed25519` | 私钥路径；其公钥会写入 cloud-init |
| `kvm.network` | 可选 | `default` | libvirt network |
| `kvm.disk_dir` | 可选 | `/data/lxl/k8s/origin_k8s/setup/disks` | VM qcow2 磁盘目录 |
| `kvm.cloud_init_dir` | 可选 | `setup/cloud-init` | cloud-init 文件目录 |
| `kvm.storage_dir` | 可选 | `setup` | 临时/元数据目录 |
| `kvm.ubuntu_series` | 可选 | `jammy` | Ubuntu cloud image 系列 |
| `kvm.allow_existing` | 可选 | `false` | 是否允许复用显式同名 VM |

## 命名、IP、MAC 规则

`kvm_from_yaml.sh` 会先读取已有 libvirt VM、DHCP reservation 和 lease，然后自动避开冲突：

- 如果已有 `seed-k3s-master`，新 master 会变成 `seed-k3s-master2`、`seed-k3s-master3` 等。
- worker 会从已有最大 `seed-k3s-worker<N>` 继续递增。
- IP 和 MAC 也会沿着已有值继续递增。
- 实际结果必须以 `*.resolved-nodes.tsv` 为准，不要假设模板里的第一个 master 一定叫某个固定名字。

## 与 running 的连接点

`setup` 和 `running` 的连接点只有 setup 产物，而不是脚本互调：

- `setup` 生成 `seedemu-k3s.env.sh`。
- `../running/Makefile` include 这个 env 文件。
- Makefile 从 env 文件派生：
  - `REGISTRY_PREFIX=$(SEED_REGISTRY_HOST):$(SEED_REGISTRY_PORT)`
  - `KUBECONFIG=$(SEED_OUTPUT_KUBECONFIG)`

因此用户通常不需要手工 export `KUBECONFIG` 或 `REGISTRY_PREFIX`。如果 `seedemu-k3s.env.sh` 不存在，running 会退回 Makefile 里的默认值。

## 常见检查命令

```bash
# 查看 resolved VM 计划
cat /home/lxl/k8s/origin_k8s/setup/kvm_template.resolved-nodes.tsv

# 查看新集群节点
KUBECONFIG=/home/lxl/k8s/origin_k8s/setup/seedemu-k3s.kubeconfig.yaml kubectl get nodes -o wide

# 查看 Multus
KUBECONFIG=/home/lxl/k8s/origin_k8s/setup/seedemu-k3s.kubeconfig.yaml kubectl -n kube-system get pods -l name=multus -o wide

# 查看 registry 是否可达
curl -fsS http://192.168.122.122:5000/v2/

# 查看 running 实际会使用哪个 kubeconfig/registry
cd /home/lxl/k8s/origin_k8s/running
make -n preflight | grep -E 'KUBECONFIG=|REGISTRY_PREFIX='
```
