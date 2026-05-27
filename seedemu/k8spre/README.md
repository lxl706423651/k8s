# seedemu.k8spre

`seedemu.k8spre` 是 SeedEMU/K8s 预处理流程的轻量 Python 包装。它不重写 KVM、K3s、registry、running 逻辑，只负责把内置 `setup/`、`running/` 资源复制到用户目录，生成 YAML 配置和入口脚本，然后可选地执行这些入口脚本。

核心原则：用户配置写入 YAML，不要求手工 `export` 环境变量。开发者仍可以直接修改生成出来的 shell 脚本。

## 最小用法

```python
from seedemu.k8spre import K8sPre

k = K8sPre()

k.writeKvmInstallScripts("./out")
k.writeK3sBuildScripts("./out")
k.writeRunningScripts("./out", output_dir="/abs/path/to/emulate/output")
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

生成后的 `setup/README.md` 会被 API 自动写入本次目录对应的 kubeconfig 路径和默认实验 namespace，方便用户直接复制 `kubectl` 查看命令。

## 两种支持形式总览

`seedemu.k8spre` 现在支持两种入口形态：先创建 KVM VM 再建 K3s 集群，或者直接把已有真实物理机组织成 K3s 集群。两者共享 K3s 构建阶段和 running 阶段，但节点来源不同。

| 对照项 | 多个 KVM VM 构造成集群 | 多个真实物理机构造成集群 |
| --- | --- | --- |
| 适用场景 | 单台宿主机上用 libvirt/KVM 批量创建 master/worker VM。 | 已经有多台服务器，需要把它们安装成 SeedEMU/K3s 集群。 |
| Python 入口 | `writeKvmInstallScripts()`，然后 `writeK3sBuildScripts()`。 | `writePhysicalNodeScripts()`，然后 `writeK3sBuildScripts()`。 |
| 用户输入 | `kvm.yaml` 或 API 参数，描述 master/worker 的 CPU、内存、磁盘和 SSH 用户；`writeKvmInstallScripts(connection="ovn")` 可选择 KVM + Kube-OVN。 | `configK3s.yaml`，描述每台物理机的 `role/ip/ssh`，可选 `fabric/cni/k3s`；`writePhysicalNodeScripts(connection=...)` 可选择 `vxlan` 或 `ovn`。 |
| 节点来源 | `kvm/createKvmVms.sh` 自动避开已有 VM/IP/MAC 冲突并创建 VM。 | 用户指定已有机器，脚本只做 SSH/sudo/underlay/fabric 检查和配置。 |
| K3s 输入 | KVM 阶段生成 `configK3s.yaml`，K3s 阶段读取它。 | 用户提供或 API 复制生成 `configK3s.yaml`，K3s 阶段读取它。 |
| 关键中间文件 | `kvm.yaml`、`cloud-init/`、`kvmState.yaml`、`configK3s.yaml`、`image-cache/`。 | `configK3s.yaml`、可选 `br-seedemu/vxseed0` Linux fabric 或 Kube-OVN fabric、`image-cache/`。 |
| K3s 产物 | `seedemu-k3s.kubeconfig.yaml`、`seedemu-k3s.inventory.yaml`。 | `seedemu-k3s.kubeconfig.yaml`、`seedemu-k3s.inventory.yaml`。 |
| Running 输入 | `running/configRunning.yaml` 指向 `setup/configK3s.yaml` 和 compile output。 | 同左。 |
| 清理脚本 | `destroyKvmVms.sh` 删除本轮 VM、磁盘、cloud-init、KVM DHCP reservation。 | `destroyPhysicalCluster.sh` 卸载 K3s、删除 registry 容器，并按当前 `fabric.type` 清理 VXLAN 或 OVN fabric。 |

真实物理机生成目录会按 `fabric.type` 解耦 fabric 资源：`connection="vxlan"` 只生成 `setup/vxlan/`，`connection="ovn"` 只生成 `setup/ovn/`。如果重新生成到已有目录，请使用 `overwrite=True` 清理上一次生成残留的另一种 backend 子目录。

KVM VM 路径的脚本顺序：

```bash
cd out/setup
bash installKvmVms.sh
bash buildK3sCluster.sh

cd ../running
make preflight
make build
make up
```

KVM 路径中，`installKvmVms.sh` 先执行 `kvm/prepareHostAssets.sh` 准备 cloud image 和 image cache，再执行 `kvm/createKvmVms.sh` 生成 VM、`configK3s.yaml` 与 `kvmState.yaml`，最后执行 `kvm/tuneVmLimits.sh` 打开 VM 内 OS/network 限制。`buildK3sCluster.sh` 后续只读取 `configK3s.yaml`，不会再从 KVM 状态中推断节点。

KVM 路径默认保持历史行为：SeedEMU secondary networks 使用 compiler 输出的 macvlan NAD，父接口缺省为 VM 内的 `ens2`。如果调用：

```python
k.writeKvmInstallScripts("./out", connection="ovn")
```

生成的 `kvm.yaml` 会包含 `fabric.type=ovn`，并在 KVM 创建后传递到 `configK3s.yaml`。这样 `buildK3sCluster.sh` 会在 K3s/Multus 就绪后自动安装 Kube-OVN non-primary CNI；running 阶段也会自动把 compile 输出转换为 Kube-OVN NAD/Subnet/VPC。

真实物理机路径的脚本顺序：

```bash
cd out/setup
bash preparePhysicalNodes.sh ./configK3s.yaml
bash vxlan/configureLinuxVxlanFabric.sh ./configK3s.yaml
bash vxlan/validateLinuxVxlanFabric.sh ./configK3s.yaml
bash buildK3sCluster.sh

cd ../running
make preflight
make build
make up
```

物理机路径中，`preparePhysicalNodes.sh` 只验证 SSH、`sudo -n`、基础命令和 underlay 接口；`vxlan/` 脚本只在 `fabric.type=linux-vxlan` 时创建 `br-seedemu/vxseed0`；`ovn/` 脚本由 `buildK3sCluster.sh` 在 K3s/Multus 就绪后安装 Kube-OVN non-primary CNI。`buildK3sCluster.sh` 与 KVM 路径相同，统一读取 `configK3s.yaml` 安装 K3s、registry、Multus 和基础镜像。

两条路径在 K3s 构建完成后都会生成同类输出：

| 输出文件 | 生成阶段 | 后续消费者 |
| --- | --- | --- |
| `setup/configK3s.yaml` | KVM 自动生成，或物理机用户提供/API 复制 | `buildK3sCluster.sh`、`running/Makefile`、`validateClusterPreflight.sh`。 |
| `setup/seedemu-k3s.kubeconfig.yaml` | `applyK3sCluster.sh` | `kubectl`、`make preflight`、`make up`、`make clean`。 |
| `setup/seedemu-k3s.inventory.yaml` | `applyK3sCluster.sh` | 调试和审计集群节点，不作为 running 阶段的强制输入。 |
| `running/configRunning.yaml` | `writeRunningScripts()` | `running/Makefile` 和 `manageK8sManifest.py`。 |
| `emulate/output/k8s.kube-ovn.yaml` 或 `emulate/output/k8s.yaml` | SeedEMU compile 阶段 | `make preflight`、`make up`。 |
| `emulate/output/images.yaml` | SeedEMU compile 阶段 | `make build` 和 kustomization 生成。 |
| `emulate/output/kustomization.yaml` | `make up` | `kubectl apply -k`。 |

文件角色可以按阶段理解：

| 阶段 | 用户主要看什么 | 脚本主要读什么 | 脚本主要写什么 |
| --- | --- | --- | --- |
| KVM 创建 | `kvm.yaml` | `kvm.yaml` | `configK3s.yaml`、`kvmState.yaml`、`cloud-init/`、VM 磁盘。 |
| 物理机准备 | `configK3s.yaml` | `configK3s.yaml` | Linux fabric 接口状态；失败时回滚。 |
| K3s 构建 | `configK3s.yaml` | `configK3s.yaml`、`ansible/k3s-install.yml`、`image-cache/` | kubeconfig、inventory、master registry、K3s 服务。 |
| Running | `configRunning.yaml`、compile output | `configRunning.yaml`、`configK3s.yaml`、manifest、`images.yaml` | registry 镜像、`kustomization.yaml`、K8s namespace/pods。 |

## KVM 创建阶段

`writeKvmInstallScripts()` 会生成 `out/setup/kvm.yaml` 和 `installKvmVms.sh`。

它复制的资源只覆盖 KVM 阶段：`kvm/prepareHostAssets.sh`、`kvm/createKvmVms.sh`、`kvm/tuneVmLimits.sh`、`kvm/destroyKvmVms.sh`、`kvm/manageKvmConfig.py`、`manageK3sConfig.py`。其中 `manageK3sConfig.py` 只用于读取 `configK3s.yaml`，不会触发 K3s 安装。它不会生成 K3s 构建入口。

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
bash ./kvm/prepareHostAssets.sh ./kvm.yaml
bash ./kvm/createKvmVms.sh ./kvm.yaml
bash ./kvm/tuneVmLimits.sh ./configK3s.yaml
```

关键产物：

- `configK3s.yaml`：KVM 创建阶段直接生成的用户可读 K3s 输入，也可以由用户手写。它保留 `clusterName`、`nodes[].role/ip/ssh`，以及 `fabric/k3s/ovn/registry/outputs` 等后续 K3s/running 阶段确实需要的字段；不会重复写入 CPU、内存、磁盘等 KVM-only 资源字段。
- `kvmState.yaml`：KVM 阶段内部状态，包含 VM 名称、MAC、CPU、内存、磁盘目录、cloud-init 目录等清理所需信息。用户通常不需要手写。
- `cloud-init/`：每台 VM 的 cloud-init 配置。
- `/data/$USER/k8spre/.../disks`：默认 qcow2 磁盘目录，避免 libvirt 访问用户 home 目录失败。

安全保护：`kvm/createKvmVms.sh` 不会盲目复用旧 `kvmState.yaml`。如果其中的节点数量、角色或资源与当前 `kvm.yaml` 不匹配，会直接失败。即使状态文件误指向已有 VM，`kvm/createKvmVms.sh` 和 `kvm/destroyKvmVms.sh` 也会检查目标 VM 磁盘是否在当前 `kvm.diskDir` 下，避免误复用或误删已有集群。

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
      key: ~/.ssh/id_ed25519
  - role: worker
    ip: 192.168.122.123
    ssh:
      user: ubuntu
      key: ~/.ssh/id_ed25519
```

缺省规则：如果没写 `name`，会生成 `seed-k3s-master` 和 `seed-k3s-workerN`。必须恰好一个 `role: master`。K3s 版本、pod CIDR、service CIDR、maxPods、registry port、kubeconfig 输出路径等均由 `manageK3sConfig.py` 使用内置缺省值补齐，不要求用户写入 `configK3s.yaml`。

`nodes[].name` 会被写入 K3s 的 `node-name` 配置，因此 `kubectl get nodes` 显示的节点名就是这个值。这样它不需要等于真实机器 hostname，也不需要等于 KVM domain 名字；只要在同一个 `configK3s.yaml` 内唯一即可。

真实物理机也走同一套 K3s 构建入口。如果 master 是执行脚本的本机，可以在 master 节点上写 `connection: local`；即使不写，脚本也会在“master IP 属于本机且 `ssh.user` 是当前用户”时自动识别为 local。local master 会用本地命令安装 K3s、启动 registry、导入镜像和读取 kubeconfig，worker 仍按各自 `nodes[].ssh.user/key` 走 SSH。

已有物理机最小例子：

```yaml
clusterName: seedemu-k3s
nodes:
  - name: amd
    role: master
    ip: 10.202.236.88
    connection: local
    ssh:
      user: lxl
      key: ~/.ssh/id_ed25519
  - name: idc
    role: worker
    ip: 10.202.191.39
    ssh:
      user: seed
      key: ~/.ssh/id_ed25519
```

注意：`cni.defaultMasterInterface` 缺省是 `ens2`，这是 KVM 场景的默认值。真实服务器如果网卡名不同，必须显式配置一个所有节点都存在且处于目标二层网络的父接口；否则 K3s 集群可以建，但 SeedEMU 的 Multus/macvlan 仿真网络后续会失败。

两台物理机可以使用内置 Linux VXLAN bridge fabric，把每台机器统一暴露为 `br-seedemu`，再让 Multus/macvlan 挂载到这个桥上。用户配置可以只写节点基础信息；调用 `writePhysicalNodeScripts(..., connection="vxlan")` 后，生成目录会自动写入 `fabric.type=linux-vxlan`。`bridgeName/vxlanName/vni/dstPort/mtu` 都有缺省值，`bridgeTestIp/macvlanTestIp` 只是验证脚本内部临时地址，通常不需要写。

```yaml
clusterName: seedemu-k3s
nodes:
  - name: amd
    role: master
    ip: 10.202.236.88
    connection: local
    ssh:
      user: lxl
      key: ~/.ssh/id_ed25519
  - name: idc
    role: worker
    ip: 10.202.191.39
    ssh:
      user: seed
      key: ~/.ssh/id_ed25519
```

如果用户不写 `fabric.nodes.<node>.underlayInterface`，VXLAN 脚本会在目标节点上执行 `ip -o route get <peer-ip>` 自动探测承载 VXLAN UDP 包的 underlay 网卡；如果自动探测不符合预期，可以在 `fabric.nodes` 中显式覆盖。

如果真实物理机无法控制上游交换机、且不希望依赖统一二层父接口，可以选择 Kube-OVN secondary L2 fabric。推荐方式是在 API 调用时设置 `connection="ovn"`，用户输入 YAML 仍只需要写节点基础信息；生成出的 `setup/configK3s.yaml` 会自动包含 `fabric.type=ovn` 和当前 Kube-OVN installer 所需的 K3s 版本。

```python
from seedemu.k8spre import K8sPre

k = K8sPre()
k.writePhysicalNodeScripts("./out", config="configK3s.yaml", connection="ovn")
k.writeK3sBuildScripts("./out", config="configK3s.yaml")
k.writeRunningScripts("./out", output_dir="/abs/path/to/emulate/output")
```

```yaml
clusterName: seedemu-k3s
nodes:
  - name: amd
    role: master
    ip: 10.202.236.88
    connection: local
    ssh:
      user: lxl
      key: ~/.ssh/id_ed25519
  - name: idc
    role: worker
    ip: 10.202.191.39
    ssh:
      user: seed
      key: ~/.ssh/id_ed25519
```

OVN 模式的差异：

- `buildK3sCluster.sh` 会在 K3s 和 Multus 安装后调用 `ovn/installKubeOvnFabric.sh`，安装 Kube-OVN non-primary CNI；K3s 的 `eth0` 仍由 flannel 负责。
- running 阶段读取 `fabric.type=ovn` 后，会优先使用 compiler 直接产出的 `emulate/output/k8s.kube-ovn.yaml`。如果只有旧式 `k8s.yaml`，`make up` 仍会把 macvlan NAD 转成 `type=kube-ovn` NAD，并为每个仿真网段生成 Kube-OVN `Vpc/Subnet`。
- Kube-OVN 静态 IP 使用 `<nad>.<namespace>.ovn.kubernetes.io/ip_address` provider annotation，避免把 macvlan/static IPAM 的 `ips: ["10.x.x.x/24"]` 直接交给 Kube-OVN。
- `connection="ovn"` 默认把 K3s 版本设为 `v1.29.15+k3s1`，因为当前 Kube-OVN Helm chart 需要 Kubernetes 1.29 或更高版本。

生成物理机脚本：

```python
from seedemu.k8spre import K8sPre

k = K8sPre()
k.writePhysicalNodeScripts("./out", config="configK3s.yaml")
k.writeK3sBuildScripts("./out", config="configK3s.yaml")
k.writeRunningScripts("./out", output_dir="/abs/path/to/emulate/output")
```

执行顺序：

```bash
cd out/setup
bash preparePhysicalNodes.sh ./configK3s.yaml
bash vxlan/configureLinuxVxlanFabric.sh ./configK3s.yaml
bash vxlan/validateLinuxVxlanFabric.sh ./configK3s.yaml
bash buildK3sCluster.sh

cd ../running
make preflight
make build
make up
```

`vxlan/validateLinuxVxlanFabric.sh` 会做 bridge IP 双向 ping 和 macvlan-on-bridge 双向 ping；验证失败会调用 `vxlan/cleanLinuxVxlanFabric.sh` 自动删除 `br-seedemu/vxseed0/macseed0`。如果要清理真实物理机集群，执行 `setup/destroyPhysicalCluster.sh ./configK3s.yaml`，它会卸载 K3s、移除 master registry 容器、删除生成的 kubeconfig/inventory，并清理 linux-vxlan 或 OVN fabric。

如果源配置临时写了 `ssh.password` 用于手工引导免密，Python API 生成 `setup/configK3s.yaml` 时会自动剔除该字段；K3s 构建阶段只使用 SSH key。

`applyK3sCluster.sh` 会安装 K3s、配置 master registry、从宿主机导入 bootstrap 镜像、推送 SeedEMU base/router 镜像、拉取 kubeconfig，并生成：

- `seedemu-k3s.kubeconfig.yaml`：宿主机执行 kubectl 使用。
- `seedemu-k3s.inventory.yaml`：解释性集群清单。

setup 阶段不再默认生成 `setup/configRunning.yaml`。running 阶段的唯一配置由 `writeRunningScripts()` 写到 `out/running/configRunning.yaml`，避免同一集群出现两份容易混淆的 running 配置。

## Running 阶段

`writeRunningScripts()` 会生成 `out/running/configRunning.yaml` 和 Makefile。Makefile 读取这个 YAML，再通过 `configK3s.yaml` 得到 kubeconfig、registry 地址、SSH user/key。

registry 解析规则：如果 `configK3s.yaml` 明确写了 `registry.host`，优先使用它；否则使用唯一 `role: master` 节点的 `ip`，端口缺省为 `5000`。`make build` 也会用这个 master 节点的 `ssh.user` 和 `ssh.key` 作为远端构建登录方式。

当 master 被解析为 `connection: local` 时，`running/Makefile` 的 `make build` 不再 SSH 到 master，而是在本机 `/tmp/seedemu-native-build-*` 中暂存 compile output 并直接执行 BuildKit/buildx 构建。`make preflight` 会按 `configK3s.yaml` 对每个节点分别解析 SSH 用户和 key，不再假设所有节点都使用 master 的账号。

```yaml
setupConfig: /abs/path/out/setup/configK3s.yaml
outputDir: /abs/path/to/emulate/output
imageRegistryPrefix: seedemu
rolloutTimeoutSeconds: 1800
```

常用命令：

- `make preflight`：检查 manifest（`k8s.kube-ovn.yaml` 或 `k8s.yaml`）、`images.yaml`、kubeconfig、节点 Ready、kube-system、namespace 基线、registry、远端 docker/buildx。
- `make build`：把 compile output 上传到 registry master，用 BuildKit/buildx 构建并 push 镜像。
- `make up`：生成 `kustomization.yaml`，把逻辑镜像前缀 `seedemu/...` 映射到真实 registry，然后 `kubectl apply -k`。
- `make clean`：如果 `kustomization.yaml` 存在，先 `kubectl delete -k` 删除其中的 cluster-scoped 资源，再删除当前 manifest 中的 namespace；不会删除 `kustomization.yaml` 文件本身。

`make up` 在 macvlan/VXLAN 模式下会根据 `configK3s.yaml` 的 `cni.defaultMasterInterface` 重写 compile 产物中所有 NetworkAttachmentDefinition 的 macvlan `master`。因此 compile 输出可以继续保持 KVM 默认的 `ens2`，而 running 阶段会在物理机配置下把它改成 `br-seedemu`。在 OVN 模式下，推荐 compiler 直接产出 `k8s.kube-ovn.yaml`；旧式 `k8s.yaml` 仍会在 running 阶段转换为 Kube-OVN manifest。

## 文件角色

`setup/` 资源：

- `kvm/prepareHostAssets.sh`：准备 Ubuntu cloud image 和 Docker 镜像 tar 缓存。
- `kvm/createKvmVms.sh`：读取 `kvm.yaml`，生成非冲突 VM 计划并创建 KVM。
- `kvm/tuneVmLimits.sh`：通过 SSH 打开 VM 内 OS/network 限制。
- `applyK3sCluster.sh`：读取 `configK3s.yaml` 构建 K3s 集群。
- `preparePhysicalNodes.sh`：验证真实物理机 SSH、`sudo -n`、基础命令和 VXLAN underlay 接口。
- `vxlan/configureLinuxVxlanFabric.sh`：按 `configK3s.yaml` 创建 `br-seedemu/vxseed0`。
- `vxlan/validateLinuxVxlanFabric.sh`：验证 bridge 和 macvlan 双向连通，失败时自动清理 fabric。
- `vxlan/cleanLinuxVxlanFabric.sh`：删除配置中的 linux-vxlan fabric。
- `ovn/installKubeOvnFabric.sh`：按 `fabric.type=ovn` 安装 Kube-OVN non-primary CNI。
- `ovn/validateKubeOvnFabric.sh`：验证 Kube-OVN CRD、controller、CNI daemonset 和 OVS/OVN daemonset。
- `ovn/cleanKubeOvnFabric.sh`：卸载 Kube-OVN Helm release 并清理相关资源。
- `destroyPhysicalCluster.sh`：卸载真实物理机 K3s、移除 registry 容器并清理 fabric。
- `kvm/destroyKvmVms.sh`：从 `kvmState.yaml` 清理本轮 VM、磁盘、cloud-init、DHCP reservation；根目录 `destroyKvmVms.sh` 是便捷包装入口。
- `kvm/manageKvmConfig.py`：KVM 阶段 YAML 解析、节点计划、`configK3s.yaml` 和 `kvmState.yaml` 生成。
- `manageK3sConfig.py`：K3s 阶段 YAML 解析、临时 Ansible inventory 和持久 cluster inventory 生成；`write-running-config` 仅保留为旧流程兼容命令。
- `ansible/k3s-install.yml`：静态 Ansible playbook 模板，必须保留。

`running/` 资源：

- `Makefile`：暴露 `preflight/build/up/wait/clean`。
- `manageK8sManifest.py`：解析 `configRunning.yaml`、生成 kustomization、读取 namespace/deployment/images。
- `buildRegistryImages.sh`：在 registry master 上用 BuildKit/buildx 构建并 push 镜像。
- `validateClusterPreflight.sh`：running 前置检查。

当前未使用旧文件名：`seedemu-k3s.env.sh`、`01_create_kvm_vms.sh`、`02_build_k3s_cluster.sh`、`*_snake_case.sh`。如果用户目录里还有这些旧生成物，可以按需删除；package resource 中不再依赖它们。
