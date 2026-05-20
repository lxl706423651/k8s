# K8sPre setup scripts

这个目录由 `seedemu.k8spre.K8sPre` 复制生成，负责宿主机侧的 KVM 创建、VM 调优和 K3s 集群初始化。配置来源是 YAML，不要求用户手工 export 环境变量。

## 推荐顺序

```bash
bash installKvmVms.sh
bash buildK3sCluster.sh
```

`installKvmVms.sh` 会调用：

```bash
bash ./prepareHostAssets.sh ./kvm.yaml
bash ./createKvmVms.sh ./kvm.yaml
bash ./tuneVmLimits.sh ./configK3s.yaml
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
      key: /home/lxl/.ssh/id_ed25519
  - role: worker
    ip: 192.168.122.111
    ssh:
      user: ubuntu
      key: /home/lxl/.ssh/id_ed25519
```

K3s 版本、CIDR、maxPods、registry port、输出路径等由脚本缺省值补齐，不需要写进这个用户输入文件。

`nodes[].name` 可省略；省略时脚本生成 `seed-k3s-master` 和 `seed-k3s-workerN`。安装时 playbook 会显式把它写入 K3s `node-name`，所以 `kubectl get nodes` 显示的就是这个名字。它可以不同于真实机器 hostname 或 KVM domain 名，但在同一个集群配置内必须唯一。

后续 running 阶段会从这个文件解析 registry 和 SSH 目标：如果没有显式 `registry.host`，默认使用唯一 master 节点的 `ip` 和 `registry.port` 缺省值 `5000`；`make build` 的远端 SSH 默认使用 master 节点的 `ssh.user` / `ssh.key`。

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
| `configK3s.yaml` | K3s 构建输入。KVM 创建后自动生成，也可由用户为已有 VM 手写；推荐只写每台机器的 `role/ip/ssh`。 |
| `kvmState.yaml` | KVM 阶段内部状态。记录 VM MAC、资源和磁盘/cloud-init 路径，供清理使用；用户通常不需要手写。 |
| `installKvmVms.sh` | 一键 KVM 阶段入口：准备宿主机资源、创建 VM、打开 VM OS 限制、生成 `configK3s.yaml`。 |
| `buildK3sCluster.sh` | 一键 K3s 阶段入口：读取 `configK3s.yaml` 构建集群。 |
| `prepareHostAssets.sh` | 在宿主机准备 Ubuntu cloud image 和 Docker 镜像 tar 缓存。 |
| `createKvmVms.sh` | 自动避开已有 VM/IP/MAC 冲突，创建并启动 KVM。 |
| `tuneVmLimits.sh` | 通过 SSH 对 VM 打开文件句柄、netns、邻居表、cni0 hash 等限制。 |
| `applyK3sCluster.sh` | 安装 K3s、配置 registry、导入 bootstrap 镜像、生成 kubeconfig 和 inventory。 |
| `destroyKvmVms.sh` | 根据 `kvmState.yaml` 清理 VM、磁盘、cloud-init 和 DHCP reservation。 |
| `manageKvmConfig.py` | KVM 阶段 YAML 解析器，负责生成 VM 计划、`configK3s.yaml` 和 `kvmState.yaml`。 |
| `manageK3sConfig.py` | K3s 阶段 YAML 解析器，负责生成临时 Ansible inventory 和持久 cluster inventory；`write-running-config` 仅保留为旧流程兼容命令。 |
| `ansible/k3s-install.yml` | K3s 安装使用的静态 Ansible playbook 模板，必须保留。 |

## 关键产物

| 产物 | 生成者 | 用途 |
| --- | --- | --- |
| `configK3s.yaml` | `createKvmVms.sh` / 用户 | K3s 集群构建的唯一输入。已有 VM 场景只需写每台机器的 `role/ip/ssh`。 |
| `kvmState.yaml` | `createKvmVms.sh` | KVM 清理状态。记录 MAC、CPU、内存、磁盘、cloud-init 和输出路径。 |
| `seedemu-k3s.kubeconfig.yaml` | `applyK3sCluster.sh` | 宿主机和 `running/Makefile` 通过它访问 K3s API server。 |
| `seedemu-k3s.inventory.yaml` | `applyK3sCluster.sh` | 解释性集群清单，便于调试和后续扩展。 |
| `configRunning.yaml` | `writeRunningScripts()` | running 阶段唯一配置文件，位于 `running/` 目录；setup 阶段不再默认生成。 |
| `image-cache/` | `prepareHostAssets.sh` / `applyK3sCluster.sh` | 宿主机镜像 tar 缓存，用于把 registry、K3s system image、Multus、SEED base/router 镜像导入新 VM。 |
| `cloud-init/` | `createKvmVms.sh` | 每台 VM 的 cloud-init 配置。 |
| `/data/$USER/k8spre/.../disks` | `createKvmVms.sh` | 默认 VM qcow2 磁盘目录，避免 libvirt 无法访问用户 home 目录。 |

## ansible 目录为什么保留

`ansible/k3s-install.yml` 不是运行后生成的 inventory，而是安装模板。`applyK3sCluster.sh` 会用 `manageK3sConfig.py` 动态生成临时 inventory，再执行这个 playbook。因此 package resource 中需要保留这个模板文件；运行时生成的 inventory 不需要打包。

## 可删除建议

旧版本生成目录里如果还有 `seedemu-k3s.env.sh`、`01_create_kvm_vms.sh`、`02_build_k3s_cluster.sh`、`create_kvm_vms.sh`、`build_k3s_cluster.sh` 等旧文件名，它们不再被当前资源脚本调用，可以在确认没有外部流程依赖后删除。

## 安全保护

`createKvmVms.sh` 会先验证已有 `kvmState.yaml` 是否仍匹配当前 `kvm.yaml` 的节点数量、角色和资源配置；不匹配时会拒绝复用，防止测试目录或旧目录里的 stale YAML 指向已有集群 VM。

`destroyKvmVms.sh` 在执行 destructive cleanup 前会检查目标 VM 的磁盘路径是否位于 `kvmState.yaml` 记录的 `kvm.diskDir` 下；如果 YAML 指向已有集群 VM 或其他目录的 VM，会直接拒绝删除。
