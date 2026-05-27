# K8sPre setup scripts

这个目录由 `seedemu.k8spre.K8sPre` 复制生成，负责宿主机侧的 KVM 创建、VM 调优和 K3s 集群初始化。配置来源是 YAML，不要求用户手工 export 环境变量。

<!-- K8SPRE_GENERATED_CONTEXT_START -->
## 当前生成目录上下文

这个区块会由 `seedemu.k8spre.K8sPre` 在生成 `setup/` 目录时改写，写入本次输出目录对应的 kubeconfig 路径和默认实验 namespace。
<!-- K8SPRE_GENERATED_CONTEXT_END -->

## 推荐顺序

```bash
bash installKvmVms.sh
bash buildK3sCluster.sh
```

`installKvmVms.sh` 会调用：

```bash
bash ./kvm/prepareHostAssets.sh ./kvm.yaml
bash ./kvm/createKvmVms.sh ./kvm.yaml
bash ./kvm/tuneVmLimits.sh ./configK3s.yaml
```

`buildK3sCluster.sh` 会调用：

```bash
bash ./applyK3sCluster.sh ./configK3s.yaml
```

已有 VM 场景下，用户可以直接写最小 `configK3s.yaml`：

```yaml
clusterName: seedemu-k3s
nodes:
  - role: master
    ip: 192.168.122.110
    ssh:
      user: ubuntu
      key: ~/.ssh/id_ed25519
  - role: worker
    ip: 192.168.122.111
    ssh:
      user: ubuntu
      key: ~/.ssh/id_ed25519
```

K3s 版本、CIDR、maxPods、registry port、输出路径等由脚本缺省值补齐，不需要写进这个用户输入文件。

`nodes[].name` 可省略；省略时脚本生成 `seed-k3s-master` 和 `seed-k3s-workerN`。安装时 playbook 会显式把它写入 K3s `node-name`，所以 `kubectl get nodes` 显示的就是这个名字。它可以不同于真实机器 hostname 或 KVM domain 名，但在同一个集群配置内必须唯一。

已有真实物理机场景也使用同一个 `configK3s.yaml`。如果 master 就是执行脚本的本机，可以显式写 `connection: local`，也可以省略；当 master IP 属于本机且 `ssh.user` 等于当前用户时，脚本会自动识别为 local，本机操作不再通过 SSH-to-self 执行：

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

如果 worker 暂时只能密码登录，先把本机公钥追加到 worker 用户的 `~/.ssh/authorized_keys`，再运行 `buildK3sCluster.sh`。`ssh.password` 只建议作为临时人工引导信息，K3s 构建脚本本身仍要求可用的 SSH key 和 `sudo -n`。Python API 生成 `setup/configK3s.yaml` 时会剔除 `ssh.password`，避免把密码扩散到生成目录。

后续 running 阶段会从这个文件解析 registry 和 SSH 目标：如果没有显式 `registry.host`，默认使用唯一 master 节点的 `ip` 和 `registry.port` 缺省值 `5000`；`make build` 的远端 SSH 默认使用 master 节点的 `ssh.user` / `ssh.key`。

Multus/macvlan 的父接口由 `cni.defaultMasterInterface` 指定，缺省仍是 KVM 场景常见的 `ens2`。真实物理机必须确认所有需要承载仿真 Pod 的节点上都存在同名父接口，且这个父接口处在符合实验需求的二层网络中。若不同机器网卡名不同，建议先在机器侧创建一致命名的专用桥/接口，再把 `cni.defaultMasterInterface` 指向这个统一名字；不要直接假设 `ens2` 存在。

两台真实物理机可以使用内置 linux-vxlan fabric 脚本准备统一父接口。用户 YAML 通常只需要写节点基础信息，并在 Python API 中传 `connection="vxlan"`；脚本会自动使用缺省的 `br-seedemu/vxseed0/VNI 4242/UDP 4789`。如果用户不写每个节点的 `underlayInterface`，脚本会通过 `ip -o route get <peer-ip>` 在目标节点上自动探测承载 VXLAN 的 underlay 网卡。

最小输入示例：

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

如果自动探测不符合预期，可以显式覆盖：

```yaml
fabric:
  type: linux-vxlan
  nodes:
    amd:
      underlayInterface: eno8403
    idc:
      underlayInterface: enp101s0f1np1
```

`bridgeTestIp` 和 `macvlanTestIp` 是验证脚本内部使用的临时测试地址，通常不需要写入用户配置；缺省值会自动生成。

物理机执行顺序：

```bash
bash ./preparePhysicalNodes.sh ./configK3s.yaml
bash ./buildK3sCluster.sh
```

如果 `fabric.type=linux-vxlan`，在 `buildK3sCluster.sh` 前额外执行：

```bash
bash ./vxlan/configureLinuxVxlanFabric.sh ./configK3s.yaml
bash ./vxlan/validateLinuxVxlanFabric.sh ./configK3s.yaml
```

如果通过 `writePhysicalNodeScripts(..., connection="ovn")` 生成目录，输入 YAML 不需要手写 `fabric.type=ovn`；K8sPre 会在生成的 `setup/configK3s.yaml` 中自动写入。OVN 模式不需要手动配置 `br-seedemu/vxseed0`。`buildK3sCluster.sh` 会在 K3s 和 Multus 安装后调用：

```bash
bash ./ovn/installKubeOvnFabric.sh ./configK3s.yaml
```

OVN 模式适用于真实物理机不共享同一二层父接口、也无法控制上游交换机/VLAN 的情况。K3s primary CNI 仍使用 flannel；SeedEMU 仿真网卡由 Multus 调用 Kube-OVN non-primary CNI 创建。后续 `running/Makefile` 会根据 `fabric.type=ovn` 生成 `k8s.kube-ovn.yaml`，把 macvlan NAD 转换为 Kube-OVN NAD，并生成对应的 Kube-OVN `Vpc/Subnet`。

KVM 场景也可以选择 OVN。调用 `writeKvmInstallScripts(..., connection="ovn")` 时，API 会把 `fabric.type=ovn` 写入 `kvm.yaml`；`kvm/createKvmVms.sh` 生成 `configK3s.yaml` 时会保留这个字段以及 `k3s/ovn/registry/outputs` 等 K3s 阶段需要的配置。后续执行顺序不变：

```bash
bash ./installKvmVms.sh
bash ./buildK3sCluster.sh
bash ./ovn/validateKubeOvnFabric.sh ./configK3s.yaml
```

区别是 running 阶段会识别 `fabric.type=ovn`，不再使用 VM 内 `ens2` 上的 macvlan，而是生成 Kube-OVN NAD/Subnet/VPC。

`vxlan/validateLinuxVxlanFabric.sh` 会先验证 `br-seedemu` 上的临时 bridge IP 双向 ping，再验证 `macseed0` macvlan-on-bridge 双向 ping。验证失败时会自动调用 `vxlan/cleanLinuxVxlanFabric.sh` 回滚 fabric，避免残留错误接口。集群整体清理使用：

```bash
bash ./destroyPhysicalCluster.sh ./configK3s.yaml
```

该脚本会卸载配置节点上的 K3s、删除 master 上的 `registry` 容器、删除生成的 kubeconfig/inventory，并清理 linux-vxlan fabric；不会删除用户的 SSH key 或 Docker 镜像库。

## K3s 构建完成后的基础命令

`buildK3sCluster.sh` 成功后，setup 阶段会输出 kubeconfig 和 registry 地址。也可以随时在 `setup/` 目录中用下面命令重新读取当前配置：

```bash
eval "$(python3 ./manageK3sConfig.py --config ./configK3s.yaml shell-vars | grep -E '^(outputKubeconfig|registryHost|registryPort)=')"
echo "kubeconfig=${outputKubeconfig}"
echo "registry=${registryHost}:${registryPort}"
```

为了后续命令简短，可以在当前 shell 中导出 kubeconfig 和实验 namespace。这里的环境变量只是 kubectl 使用便利，不是项目配置来源：

```bash
export KUBECONFIG="${outputKubeconfig}"
export SEED_NAMESPACE="seedemu-k3s-real-topo"
```

常用查看命令：

```bash
kubectl get nodes -o wide
kubectl get pods -A -o wide
kubectl -n "${SEED_NAMESPACE}" get pods -o wide
kubectl -n "${SEED_NAMESPACE}" get deploy
kubectl -n kube-system get pods -o wide
curl -s "http://${registryHost}:${registryPort}/v2/_catalog" | head
```

进入一个 Pod：

```bash
POD="$(kubectl -n "${SEED_NAMESPACE}" get pods -o jsonpath='{.items[0].metadata.name}')"
kubectl -n "${SEED_NAMESPACE}" exec -it "${POD}" -- bash
```

如果镜像里没有 bash，改用：

```bash
kubectl -n "${SEED_NAMESPACE}" exec -it "${POD}" -- sh
```

## 文件作用

| 文件 | 作用 |
| --- | --- |
| `kvm.yaml` | KVM 创建输入。由 Python API 生成，或由用户 YAML 补齐缺省值。 |
| `configK3s.yaml` | K3s 构建输入。KVM 创建后自动生成，也可由用户为已有 VM 手写；推荐只写每台机器的 `role/ip/ssh`。KVM+OVN 时会额外保留 `fabric/k3s/ovn/registry/outputs` 等 K3s/running 阶段需要的字段。 |
| `kvmState.yaml` | KVM 阶段内部状态。记录 VM MAC、资源和磁盘/cloud-init 路径，供清理使用；用户通常不需要手写。 |
| `installKvmVms.sh` | 一键 KVM 阶段入口：准备宿主机资源、创建 VM、打开 VM OS 限制、生成 `configK3s.yaml`。 |
| `buildK3sCluster.sh` | 一键 K3s 阶段入口：读取 `configK3s.yaml` 构建集群。 |
| `preparePhysicalNodes.sh` | 真实物理机预检查：验证 SSH、`sudo -n`、基础命令和 linux-vxlan underlay 接口。 |
| `vxlan/configureLinuxVxlanFabric.sh` | 按 `fabric.type=linux-vxlan` 创建 `br-seedemu/vxseed0`，失败时自动回滚。 |
| `vxlan/validateLinuxVxlanFabric.sh` | 验证 bridge 与 macvlan 双向连通；失败时清理 fabric。 |
| `vxlan/cleanLinuxVxlanFabric.sh` | 删除配置中的 linux-vxlan bridge、VXLAN 和测试 macvlan 接口。 |
| `ovn/installKubeOvnFabric.sh` | 按 `fabric.type=ovn` 安装 Kube-OVN non-primary CNI，并预加载 Kube-OVN 镜像到各节点 containerd。 |
| `ovn/validateKubeOvnFabric.sh` | 验证 Kube-OVN CRD、controller、CNI daemonset、OVS/OVN daemonset 是否就绪。 |
| `ovn/cleanKubeOvnFabric.sh` | 在 K8s API 仍可用时卸载 Kube-OVN Helm release 并删除相关 CRD/资源。 |
| `destroyPhysicalCluster.sh` | 清理真实物理机 K3s 集群、master registry 容器、生成的 kubeconfig/inventory 和 fabric。 |
| `kvm/prepareHostAssets.sh` | 在宿主机准备 Ubuntu cloud image 和 Docker 镜像 tar 缓存。 |
| `kvm/createKvmVms.sh` | 自动避开已有 VM/IP/MAC 冲突，创建并启动 KVM。 |
| `kvm/tuneVmLimits.sh` | 通过 SSH 对 VM 打开文件句柄、netns、邻居表、cni0 hash 等限制。 |
| `applyK3sCluster.sh` | 安装 K3s、配置 registry、导入 bootstrap 镜像、生成 kubeconfig 和 inventory。 |
| `kvm/destroyKvmVms.sh` | 根据 `kvmState.yaml` 清理 VM、磁盘、cloud-init 和 DHCP reservation；根目录 `destroyKvmVms.sh` 是便捷包装入口。 |
| `kvm/manageKvmConfig.py` | KVM 阶段 YAML 解析器，负责生成 VM 计划、`configK3s.yaml` 和 `kvmState.yaml`。 |
| `manageK3sConfig.py` | K3s 阶段 YAML 解析器，负责生成临时 Ansible inventory 和持久 cluster inventory；`write-running-config` 仅保留为旧流程兼容命令。 |
| `ansible/k3s-install.yml` | K3s 安装使用的静态 Ansible playbook 模板，必须保留。 |

## 关键产物

| 产物 | 生成者 | 用途 |
| --- | --- | --- |
| `configK3s.yaml` | `kvm/createKvmVms.sh` / 用户 | K3s 集群构建的唯一输入。已有 VM 场景只需写每台机器的 `role/ip/ssh`。 |
| `kvmState.yaml` | `kvm/createKvmVms.sh` | KVM 清理状态。记录 MAC、CPU、内存、磁盘、cloud-init 和输出路径。 |
| `seedemu-k3s.kubeconfig.yaml` | `applyK3sCluster.sh` | 宿主机和 `running/Makefile` 通过它访问 K3s API server。 |
| `seedemu-k3s.inventory.yaml` | `applyK3sCluster.sh` | 解释性集群清单，便于调试和后续扩展。 |
| `configRunning.yaml` | `writeRunningScripts()` | running 阶段唯一配置文件，位于 `running/` 目录；setup 阶段不再默认生成。 |
| `image-cache/` | `kvm/prepareHostAssets.sh` / `applyK3sCluster.sh` | 宿主机镜像 tar 缓存，用于把 registry、K3s system image、Multus、SEED base/router 镜像导入新 VM。 |
| `cloud-init/` | `kvm/createKvmVms.sh` | 每台 VM 的 cloud-init 配置。 |
| `/data/$USER/k8spre/.../disks` | `kvm/createKvmVms.sh` | 默认 VM qcow2 磁盘目录，避免 libvirt 无法访问用户 home 目录。 |

## ansible 目录为什么保留

`ansible/k3s-install.yml` 不是运行后生成的 inventory，而是安装模板。`applyK3sCluster.sh` 会用 `manageK3sConfig.py` 动态生成临时 inventory，再执行这个 playbook。因此 package resource 中需要保留这个模板文件；运行时生成的 inventory 不需要打包。

## 可删除建议

旧版本生成目录里如果还有 `seedemu-k3s.env.sh`、`01_create_kvm_vms.sh`、`02_build_k3s_cluster.sh`、`create_kvm_vms.sh`、`build_k3s_cluster.sh` 等旧文件名，它们不再被当前资源脚本调用，可以在确认没有外部流程依赖后删除。

## 安全保护

`kvm/createKvmVms.sh` 会先验证已有 `kvmState.yaml` 是否仍匹配当前 `kvm.yaml` 的节点数量、角色和资源配置；不匹配时会拒绝复用，防止测试目录或旧目录里的 stale YAML 指向已有集群 VM。

`destroyKvmVms.sh` 在执行 destructive cleanup 前会检查目标 VM 的磁盘路径是否位于 `kvmState.yaml` 记录的 `kvm.diskDir` 下；如果 YAML 指向已有集群 VM 或其他目录的 VM，会直接拒绝删除。
