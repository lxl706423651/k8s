# origin_k8s native baseline 流程

这个目录提供一套尽量接近 Kubernetes native 行为的 SEED compile/build/deploy baseline。它不读取 inventory，不做 nodeSelector，不做 per-node preload，也不在 compile 阶段固定 Pod 跑在哪台 node 上。

当前目录边界：

- `emulate/`：放实验拓扑 Python 入口。默认 compile 输出目录是 `./output`，实际路径是 `/home/lxl/k8s/origin_k8s/emulate/output`。
- `emulate/output/`：放本次 compile 输出。顶层只应有 `k8s.yaml` 和 `images.yaml` 两个控制产物；镜像 build context 目录和 `base_images/` 也是 build 输入。
- `running/`：放固定运行逻辑，包括 `Makefile`、`k8s_make_helper.py`、`k8s_local_build.sh`。这些不再由 compiler 生成。

## 1. 最小运行方式

生成 compile 产物：

```bash
cd /home/lxl/k8s/origin_k8s/emulate
source /home/lxl/anaconda3/etc/profile.d/conda.sh
conda activate seedpy310
python mini_internet_k8s_native_compile.py
```

构建并 push 镜像：

```bash
cd /home/lxl/k8s/origin_k8s/running
make build REGISTRY_PREFIX=192.168.122.110:5000
```

部署并等待 Ready：

```bash
cd /home/lxl/k8s/origin_k8s/running
make up \
  REGISTRY_PREFIX=192.168.122.110:5000 \
  KUBECONFIG=/home/lxl/k8s/output/kubeconfigs/seedemu-k3s.yaml
```

清理 namespace：

```bash
cd /home/lxl/k8s/origin_k8s/running
make clean KUBECONFIG=/home/lxl/k8s/output/kubeconfigs/seedemu-k3s.yaml
```

顶层便利入口也可用：

```bash
cd /home/lxl/k8s/origin_k8s
./deploy.sh
./clean.sh
```

## 2. Compile 产物

默认输出目录：

```text
/home/lxl/k8s/origin_k8s/emulate/output
```

关键产物：

- `k8s.yaml`：原始 Kubernetes manifest，包括 Namespace、NetworkAttachmentDefinition、Deployment；镜像名保持为 `seedemu/<image>:latest`。
- `images.yaml`：镜像逻辑名和 build context 列表，供 `running/Makefile` build 阶段读取。
- `base_images/`：基础镜像构建上下文，避免节点镜像 `FROM <digest>` 时去 Docker Hub 查不存在的本地 dummy image。
- `brdnode_*`、`rnode_*`、`hnode_*`、`rs_*`：每个 SEED node 的 Docker build context。

`images.yaml` 示例：

```yaml
images:
- name: seedemu/brdnode_2_r100:latest
  context: ./brdnode_2_r100
```

`k8s.yaml` 示例：

```yaml
image: seedemu/brdnode_2_r100:latest
```

## 3. Registry 解耦方式

compile 阶段只写逻辑镜像前缀：

```text
seedemu/brdnode_2_r100:latest
```

deploy 前 `running/Makefile` 会根据 `images.yaml` 生成：

```yaml
resources:
- k8s.yaml
images:
- name: seedemu/brdnode_2_r100
  newName: 192.168.122.110:5000/brdnode_2_r100
  newTag: latest
```

然后执行：

```bash
kubectl apply -k /home/lxl/k8s/origin_k8s/emulate/output
```

Kustomize 的 `images` 字段不是通配符替换，不能只写一行把 `seedemu/*` 全部映射到 registry。因此这里由 `running/k8s_make_helper.py` 根据 `images.yaml` 自动生成每个镜像的映射项。

如果 registry IP 变化，不需要重新 compile，只需要重新运行：

```bash
make build REGISTRY_PREFIX=<new-registry-ip:port>
make up REGISTRY_PREFIX=<new-registry-ip:port> KUBECONFIG=<kubeconfig>
```

## 4. Makefile 目标

`running/Makefile` 常用目标：

- `make build REGISTRY_PREFIX=...`：把 `emulate/output` 和 `running/` 上传到 registry host，并在远端用 Docker buildx + BuildKit 构建和 push。
- `make local-build REGISTRY_PREFIX=...`：在当前机器本地 build/push，适合已经在 master 上执行时使用。
- `make render-kustomization REGISTRY_PREFIX=...`：只在 output 目录生成 `kustomization.yaml`，不 deploy，便于人工检查映射结果。
- `make up REGISTRY_PREFIX=... KUBECONFIG=...`：生成 `kustomization.yaml`，执行 `kubectl apply -k`，等待 Deployment/Pod Ready；`kustomization.yaml` 会保留，便于检查实际 registry 映射。
- `make wait KUBECONFIG=...`：只等待当前 namespace 里的 Deployment rollout 和 Pod Ready。
- `make clean KUBECONFIG=...`：删除 `k8s.yaml` 里的 Namespace，并删除生成的 `kustomization.yaml`。
- `make clean-kustomization`：只删除生成的 `kustomization.yaml`。

默认变量：

```makefile
OUTPUT_DIR ?= ../emulate/output
REGISTRY_PREFIX ?= 192.168.122.110:5000
KUBECONFIG ?= /home/lxl/k8s/output/kubeconfigs/seedemu-k3s.yaml
SSH_USER ?= ubuntu
SSH_KEY ?= /home/lxl/.ssh/id_ed25519
```

如果要指定其他输出目录：

```bash
make up OUTPUT_DIR=/path/to/output REGISTRY_PREFIX=... KUBECONFIG=...
```

## 5. NativeKubernetesCompiler 参数

普通用户不需要配置 JSON 文件。下面是 `NativeKubernetesCompiler` 支持的主要参数：

| 参数 | 默认值 | 作用 |
| --- | --- | --- |
| `image_registry_prefix` | `seedemu` | compile 阶段写入 `k8s.yaml` / `images.yaml` 的逻辑镜像前缀。 |
| `registry_prefix` | `None` | 兼容旧调用的别名；如果传入，会覆盖 `image_registry_prefix`。不推荐新流程使用。 |
| `namespace` | `seedemu-k3s-real-topo` | `k8s.yaml` 中所有资源所在 namespace。 |
| `cni_type` | `macvlan` | Multus secondary network 的 CNI 类型。常用值是 `macvlan`、`ipvlan`、`bridge`、`host-local`。 |
| `cni_master_interface` | `ens2` | `macvlan` / `ipvlan` 使用的 K8s node VM 内部 master interface。 |
| `image_pull_policy` | `Always` | Pod 的 `imagePullPolicy`。使用 `latest` tag 时建议保持 `Always`。 |
| `rollout_timeout_seconds` | `1800` | `make wait` 等待每个 Deployment rollout 的超时时间。 |
| `platform` | `amd64` | 镜像目标平台，支持 `amd64` / `arm64`。这是传给底层 Docker compiler 的参数。 |

下面这些不再是 compiler 参数：

- `kubeconfig`：deploy 阶段通过 `make up KUBECONFIG=...` 传入。
- `use_multus`：固定启用。SEED 拓扑节点需要多网卡。
- `create_namespace`：固定启用。compile 产物自包含 Namespace。

## 6. kubeconfig 来源

kubeconfig 不是用户手写的。K3s master 本机先生成：

```text
/etc/rancher/k3s/k3s.yaml
```

项目脚本再把它拉到宿主机：

```text
/home/lxl/k8s/output/kubeconfigs/seedemu-k3s.yaml
```

并把 server 地址从 `127.0.0.1` 替换成 master IP。

相关脚本：

- `/home/lxl/k8s/scripts/setup_k3s_cluster.sh`：集群初始化末尾会 fetch kubeconfig。
- `/home/lxl/k8s/scripts/k3s_fetch_kubeconfig.sh`：可单独重新拉取 kubeconfig。

正常流程是：配置好 K3s 服务后，由项目初始化/fetch 脚本生成本地 kubeconfig；用户不需要手工编写这个文件。

## 7. 当前验证状态

本轮已验证：

- `emulate/mini_internet_k8s_native_compile.py` 可在 `seedpy310` 中完成 compile。
- compile 后 `/home/lxl/k8s/origin_k8s/emulate/output` 顶层控制产物是 `k8s.yaml` 和 `images.yaml`。
- `running/Makefile` 可生成 `kustomization.yaml`，并通过 `kubectl apply -k` 完成 deploy/wait。
- `running/Makefile build` 已真实上传 output 和 running 到 master，并用 Docker buildx + BuildKit 构建、push 57 个镜像。
