# seedemu.k8spre

`seedemu.k8spre` 是 SeedEMU/K8s 预处理流程的轻量 Python 包装。它不重写 KVM、K3s、registry、running 逻辑，只负责把内置 `setup/`、`running/` 资源复制到用户目录，生成 YAML 配置和入口脚本，然后可选地执行这些入口脚本。

核心原则：用户配置写入 YAML，不要求手工 `export` 环境变量。开发者仍可以直接修改生成出来的 shell 脚本。

## 最小用法

```python
from seedemu.k8spre import K8sPre

k = K8sPre()

k.writeKvmInstallScripts("./out")
k.writeK3sBuildScripts("./out")
k.writeRunningScripts("./out", output_dir="/home/lxl/k8s/origin_k8s/emulate/output")
```

这三个生成函数是分阶段的：

- `writeKvmInstallScripts()` 只生成 KVM 创建所需的脚本和 `kvm.yaml`。
- `writeK3sBuildScripts()` 只生成 K3s 构建所需的脚本；它不会生成 `kvm.yaml`，也不会生成 `installKvmVms.sh`。
- 如果两者写到同一个 `out/setup`，推荐先调用 `writeKvmInstallScripts()`，再调用 `writeK3sBuildScripts()`；这样 `setup/` 中会同时拥有两个阶段的入口，但两个阶段的输入仍然解耦。

然后执行：

```bash
cd out/setup
bash installKvmVms.sh
bash buildK3sCluster.sh

cd ../running
make preflight
make build
make up
```

兼容旧 API：`kvminstall_script`、`k8sbuild_script`、`running_scripts` 仍可用，但新代码建议使用上面的 lowerCamelCase 函数。

## KVM 创建阶段

`writeKvmInstallScripts()` 会生成 `out/setup/kvm.yaml` 和 `installKvmVms.sh`。

它复制的资源只覆盖 KVM 阶段：`prepareHostAssets.sh`、`createKvmVms.sh`、`tuneVmLimits.sh`、`destroyKvmVms.sh`、`manageKvmConfig.py`、`manageK3sConfig.py`。其中 `manageK3sConfig.py` 只用于读取 `configK3s.yaml`，不会触发 K3s 安装。它不会生成 K3s 构建入口。

`kvm.yaml` 描述要创建的 KVM 资源：

```yaml
master:
  vcpus: 12
  memoryMb: 10240
  diskGb: 80
workers:
  count: 2
  vcpus: 6
  memoryMb: 10240
  diskGb: 80
ssh:
  user: ubuntu
  key: ~/.ssh/id_ed25519
```

`installKvmVms.sh` 顺序执行：

```bash
bash ./prepareHostAssets.sh ./kvm.yaml
bash ./createKvmVms.sh ./kvm.yaml
bash ./tuneVmLimits.sh ./configK3s.yaml
```

关键产物：

- `configK3s.yaml`：KVM 创建阶段直接生成的用户可读 K3s 输入，也可以由用户手写。它只保留 `clusterName` 和 `nodes[].role/ip/ssh` 这类必要字段。
- `kvmState.yaml`：KVM 阶段内部状态，包含 VM 名称、MAC、CPU、内存、磁盘目录、cloud-init 目录等清理所需信息。用户通常不需要手写。
- `cloud-init/`：每台 VM 的 cloud-init 配置。
- `/data/$USER/k8spre/.../disks`：默认 qcow2 磁盘目录，避免 libvirt 访问用户 home 目录失败。

安全保护：`createKvmVms.sh` 不会盲目复用旧 `kvmState.yaml`。如果其中的节点数量、角色或资源与当前 `kvm.yaml` 不匹配，会直接失败。即使状态文件误指向已有 VM，`createKvmVms.sh` 和 `destroyKvmVms.sh` 也会检查目标 VM 磁盘是否在当前 `kvm.diskDir` 下，避免误复用或误删已有集群。

## K3s 构建阶段

`writeK3sBuildScripts()` 会生成 `buildK3sCluster.sh`、`applyK3sCluster.sh`、`manageK3sConfig.py` 和 `ansible/k3s-install.yml`。它不会生成或修改 `kvm.yaml`。如果传入 `config=...`，这个 config 必须是包含 `nodes` 的 `configK3s.yaml` 风格配置，而不是 KVM 阶段的 `kvm.yaml`。

`buildK3sCluster.sh` 必须读取 `configK3s.yaml`，不会从环境变量或自动发现结果推断集群成员。

`configK3s.yaml` 可以由 KVM 阶段自动生成，也可以由用户给已有 VM 手写。最简已有 VM 配置只需要每台机器的角色、管理 IP 和 SSH 访问方式：

```yaml
clusterName: seedemu-k3s
nodes:
  - role: master
    ip: 192.168.122.122
    ssh:
      user: ubuntu
      key: /home/lxl/.ssh/id_ed25519
  - role: worker
    ip: 192.168.122.123
    ssh:
      user: ubuntu
      key: /home/lxl/.ssh/id_ed25519
```

缺省规则：如果没写 `name`，会生成 `seed-k3s-master` 和 `seed-k3s-workerN`。必须恰好一个 `role: master`。K3s 版本、pod CIDR、service CIDR、maxPods、registry port、kubeconfig 输出路径等均由 `manageK3sConfig.py` 使用内置缺省值补齐，不要求用户写入 `configK3s.yaml`。

`nodes[].name` 会被写入 K3s 的 `node-name` 配置，因此 `kubectl get nodes` 显示的节点名就是这个值。这样它不需要等于真实机器 hostname，也不需要等于 KVM domain 名字；只要在同一个 `configK3s.yaml` 内唯一即可。

`applyK3sCluster.sh` 会安装 K3s、配置 master registry、从宿主机导入 bootstrap 镜像、推送 SeedEMU base/router 镜像、拉取 kubeconfig，并生成：

- `seedemu-k3s.kubeconfig.yaml`：宿主机执行 kubectl 使用。
- `seedemu-k3s.inventory.yaml`：解释性集群清单。

setup 阶段不再默认生成 `setup/configRunning.yaml`。running 阶段的唯一配置由 `writeRunningScripts()` 写到 `out/running/configRunning.yaml`，避免同一集群出现两份容易混淆的 running 配置。

## Running 阶段

`writeRunningScripts()` 会生成 `out/running/configRunning.yaml` 和 Makefile。Makefile 读取这个 YAML，再通过 `configK3s.yaml` 得到 kubeconfig、registry 地址、SSH user/key。

registry 解析规则：如果 `configK3s.yaml` 明确写了 `registry.host`，优先使用它；否则使用唯一 `role: master` 节点的 `ip`，端口缺省为 `5000`。`make build` 也会用这个 master 节点的 `ssh.user` 和 `ssh.key` 作为远端构建登录方式。

```yaml
setupConfig: /abs/path/out/setup/configK3s.yaml
outputDir: /abs/path/origin_k8s/emulate/output
imageRegistryPrefix: seedemu
rolloutTimeoutSeconds: 1800
```

常用命令：

- `make preflight`：检查 `k8s.yaml`、`images.yaml`、kubeconfig、节点 Ready、kube-system、namespace 基线、registry、远端 docker/buildx。
- `make build`：把 compile output 上传到 registry master，用 BuildKit/buildx 构建并 push 镜像。
- `make up`：生成 `kustomization.yaml`，把逻辑镜像前缀 `seedemu/...` 映射到真实 registry，然后 `kubectl apply -k`。
- `make clean`：删除当前 manifest 中的 namespace，不删除 `kustomization.yaml`。

## 文件角色

`setup/` 资源：

- `prepareHostAssets.sh`：准备 Ubuntu cloud image 和 Docker 镜像 tar 缓存。
- `createKvmVms.sh`：读取 `kvm.yaml`，生成非冲突 VM 计划并创建 KVM。
- `tuneVmLimits.sh`：通过 SSH 打开 VM 内 OS/network 限制。
- `applyK3sCluster.sh`：读取 `configK3s.yaml` 构建 K3s 集群。
- `destroyKvmVms.sh`：从 `kvmState.yaml` 清理本轮 VM、磁盘、cloud-init、DHCP reservation。
- `manageKvmConfig.py`：KVM 阶段 YAML 解析、节点计划、`configK3s.yaml` 和 `kvmState.yaml` 生成。
- `manageK3sConfig.py`：K3s 阶段 YAML 解析、临时 Ansible inventory 和持久 cluster inventory 生成；`write-running-config` 仅保留为旧流程兼容命令。
- `ansible/k3s-install.yml`：静态 Ansible playbook 模板，必须保留。

`running/` 资源：

- `Makefile`：暴露 `preflight/build/up/wait/clean`。
- `manageK8sManifest.py`：解析 `configRunning.yaml`、生成 kustomization、读取 namespace/deployment/images。
- `buildRegistryImages.sh`：在 registry master 上用 BuildKit/buildx 构建并 push 镜像。
- `validateClusterPreflight.sh`：running 前置检查。

当前未使用旧文件名：`seedemu-k3s.env.sh`、`01_create_kvm_vms.sh`、`02_build_k3s_cluster.sh`、`*_snake_case.sh`。如果用户目录里还有这些旧生成物，可以按需删除；package resource 中不再依赖它们。
