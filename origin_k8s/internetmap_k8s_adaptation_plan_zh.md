# InternetMap2 的 Kubernetes 适配方案

## 先说明：native k8s compiler 已经补了什么

我们已经修改了 `/home/lxl/k8s/origin_k8s/native_k8s_compiler.py`，目的是让
k8s 编译产物携带 InternetMap 能识别的拓扑语义。

原来的 Docker 版 InternetMap 依赖 Docker label：

```text
org.seedsecuritylabs.seedemu.meta.*
```

Docker 编译器会把这些 label 写到容器和 Docker network 上，InternetMap 后端再
通过 Docker API 读取这些 label，解析出：

- AS 号
- 节点名
- 节点角色
- 节点连接了哪些网络
- 网络名
- 网络作用域
- 网络前缀
- 网络类型是 local 还是 global

但 k8s 里不能直接复用 Docker network/container 模型。尤其是
`net.N.address` 里有类似 `10.0.0.1/24` 的值，Kubernetes label value 不能包含
`/`，所以我们把 InternetMap 元数据写入 Kubernetes annotation。

当前对 `native_k8s_compiler.py` 的改动包括：

1. 增加元数据前缀常量：

```python
INTERNET_MAP_META_PREFIX = "org.seedsecuritylabs.seedemu.meta."
```

2. 增加 SEED 节点类型列表，并把 `brdnode` 纳入编译范围：

```python
SEEDEMU_NODE_TYPES = ["rnode", "brdnode", "csnode", "hnode", "rs", "snode"]
```

这一步很重要。`brdnode` 对应 BorderRouter，如果不编译它，InternetMap 上会缺
少边界路由器，整体 Internet 拓扑会断。

3. 给每个 Pod template 写入节点元数据 annotation：

```text
org.seedsecuritylabs.seedemu.meta.asn
org.seedsecuritylabs.seedemu.meta.nodename
org.seedsecuritylabs.seedemu.meta.role
org.seedsecuritylabs.seedemu.meta.net.N.name
org.seedsecuritylabs.seedemu.meta.net.N.address
org.seedsecuritylabs.seedemu.meta.class
org.seedsecuritylabs.seedemu.meta.displayname
org.seedsecuritylabs.seedemu.meta.description
```

这些 annotation 来自 `_getInternetMapNodeMeta()`。

4. 给每个 `NetworkAttachmentDefinition` 写入网络元数据 annotation：

```text
org.seedsecuritylabs.seedemu.meta.type
org.seedsecuritylabs.seedemu.meta.scope
org.seedsecuritylabs.seedemu.meta.name
org.seedsecuritylabs.seedemu.meta.prefix
org.seedsecuritylabs.seedemu.meta.displayname
org.seedsecuritylabs.seedemu.meta.description
```

这些 annotation 来自 `_getInternetMapNetMeta()`。

5. 新增 `_nodeInternetMapRole()`，把 k8s 编译器内部的节点类型映射回
InternetMap 已经认识的角色名：

| registry 类型 | InternetMap 角色 |
| --- | --- |
| `hnode` | `Host` |
| `rnode` | `Router` |
| `brdnode` | `BorderRouter` |
| `csnode` | `SCION Control Service` |
| `snode` | `Emulator Service Worker` |
| `rs` | `Route Server` |

这些修改的意义是：compiler 现在已经把 InternetMap 需要的语义写入了 k8s
资源。下一步 InternetMap 后端只需要读取 Pod 和 NAD 的 annotations，再转换成
当前前端已经能消费的 Docker-like JSON，就能把 map 和 compiler 连起来。

连接关系如下：

```text
NativeKubernetesCompiler
  -> 生成 Deployment / Pod template annotations
  -> 生成 NetworkAttachmentDefinition annotations
  -> Kubernetes API 读取 Pod 和 NAD
  -> InternetMap k8s backend adapter 转成 Docker-like Labels / NetworkSettings
  -> 复用现有 Emulator.ParseNodeMeta / ParseNetMeta
  -> 现有前端继续渲染拓扑
```

## 目标

让 `InternetMap2` 支持 k8s-native SEED emulator runtime。

现有 InternetMap2 前端大体可以保持不变。主要工作是把后端的数据源从 Docker
API 换成 Kubernetes API，同时继续返回前端已经期待的数据结构：

- `/container` 返回 emulator 节点。
- `/network` 返回 emulator 网络。
- `/container/:id` 返回单个 emulator 节点。
- `/console/:id` 打开交互 shell。
- `/sniff` 在 emulator 节点内启动抓包。
- `/container/:id/net` 和 `/container/:id/bgp` 继续在目标节点内执行已有的
  SEED 控制脚本。

## 当前 Docker 版模型

InternetMap2 目前在三个地方依赖 Docker：

1. 拓扑发现
   - `docker.listContainers()` 提供 `/container`。
   - `docker.listNetworks()` 提供 `/network`。
   - 拓扑语义来自 `org.seedsecuritylabs.seedemu.meta.*` labels。

2. 节点交互
   - Docker exec 用来打开 `bash`、`/seedemu_worker` 和 `/seedemu_sniffer`。

3. 前端构图
   - 前端期待 Docker-like container 对象。
   - 关键字段包括：
     - `Id`
     - `Labels`
     - `NetworkSettings.Networks`
     - `meta.emulatorInfo`

k8s 版本应该先保留这套 API contract，等功能跑通后再考虑清理前端模型。

## 新的 Kubernetes 模型

使用 Kubernetes API 作为数据源：

| InternetMap 概念 | Docker 来源 | Kubernetes 来源 |
| --- | --- | --- |
| Emulator 节点 | Container | Deployment 管理的 Pod |
| Emulator 网络 | Docker network | Multus NetworkAttachmentDefinition |
| 节点和网络的边 | `NetworkSettings.Networks` | Pod Multus annotation + NAD metadata |
| 节点元数据 | Docker labels | Pod template annotations |
| 网络元数据 | Docker network labels | NAD annotations |
| Shell / 控制 | Docker exec | Kubernetes `pods/exec` |
| 运行状态 | Docker container state | Pod phase + container status |

backend adapter 应该把 Kubernetes labels 和 annotations 合并成 Docker-like
`Labels` 对象，然后继续调用现有的 metadata parser：

```ts
Emulator.ParseNodeMeta(Labels)
Emulator.ParseNetMeta(Labels)
```

## 后端重构形态

建议引入 runtime provider 抽象，不要让 API routes 直接依赖 `dockerode`。

建议新增文件：

- `backend/src/runtime/runtime-provider.ts`
- `backend/src/runtime/docker-provider.ts`
- `backend/src/runtime/kubernetes-provider.ts`
- `backend/src/runtime/types.ts`

建议接口：

```ts
export interface RuntimeProvider {
  listContainers(): Promise<SeedContainerInfo[]>;
  listNetworks(): Promise<SeedNetInfo[]>;
  getContainer(idPrefixOrName: string): Promise<SeedContainerInfo>;
  exec(idPrefixOrName: string, command: string[], tty: boolean): Promise<NodeExecSession>;
}
```

然后让 `backend/src/api/v1/main.ts` 使用 provider interface，而不是直接：

```ts
const docker = new dockerode();
```

为了兼容 Docker 版，可以保留 Docker 为默认 provider：

```text
INTERNETMAP_RUNTIME=docker
INTERNETMAP_RUNTIME=kubernetes
```

## Kubernetes Provider 细节

### 1. Kubernetes Client

使用官方 Kubernetes JavaScript client：

```bash
npm install @kubernetes/client-node
```

provider 需要同时支持集群内运行和本地 kubeconfig：

1. 优先尝试 `kc.loadFromCluster()`。
2. 如果失败，再 fallback 到 `kc.loadFromDefault()`。

建议环境变量：

```text
INTERNETMAP_K8S_NAMESPACE=seedemu-k3s-real-topo
INTERNETMAP_K8S_LABEL_SELECTOR=seedemu.io/workload=seedemu
```

### 2. `/container`

实现步骤：

1. 在 namespace 内按 `seedemu.io/workload=seedemu` 选择器列出 Pods。
2. 只保留合并后的 labels/annotations 中包含
   `org.seedsecuritylabs.seedemu.meta.nodename` 的 Pod。
3. 把每个 Pod 转成 Docker-like `SeedContainerInfo`。

转换后的对象至少应该包含：

```ts
{
  Id: pod.metadata.uid,
  Names: [`/${pod.metadata.name}`],
  Image: pod.spec.containers[0].image,
  State: pod.status.phase,
  Status: pod.status.phase,
  Labels: {
    ...pod.metadata.labels,
    ...pod.metadata.annotations
  },
  NetworkSettings: {
    Networks: {
      [networkName]: {
        NetworkID: networkName,
        IPAddress: ipFromMultusOrMeta,
        MacAddress: macFromMultusOrEmpty
      }
    }
  },
  meta: {
    hasSession: sessionManager.hasSession(pod.metadata.uid),
    emulatorInfo: Emulator.ParseNodeMeta(Labels)
  }
}
```

`NetworkSettings.Networks` 可以从以下来源构造：

- `k8s.v1.cni.cncf.io/network-status`：Pod 已运行且 Multus 已写入状态时优先使用。
- `k8s.v1.cni.cncf.io/networks`：Pod 还未 running 时可以用它。
- `org.seedsecuritylabs.seedemu.meta.net.N.*`：稳定 fallback。

对 InternetMap 的拓扑渲染来说，`NetworkID` 只需要和 `/network` 返回的 `Id`
一致。

### 3. `/network`

实现步骤：

1. 列出 namespace 内的 `NetworkAttachmentDefinition`。
2. 把每个 NAD 转成 Docker-like `SeedNetInfo`。

转换后的对象至少应该包含：

```ts
{
  Id: nad.metadata.name,
  Name: nad.metadata.name,
  Labels: {
    ...nad.metadata.labels,
    ...nad.metadata.annotations
  },
  meta: {
    emulatorInfo: Emulator.ParseNetMeta(Labels)
  }
}
```

前端用 `Id` 建图上的边，所以这里的 `Id` 必须和 Pod 转换对象里的
`NetworkSettings.Networks[*].NetworkID` 一致。

### 4. `/container/:id`

Docker 版支持用容器 ID 前缀查找。k8s 版建议按以下顺序解析：

1. 精确匹配 Pod UID。
2. 精确匹配 Pod name。
3. 前缀匹配 Pod UID。
4. 前缀匹配 Pod name。

如果匹配到多个 Pod，就返回和 Docker provider 类似的错误。

### 5. Console 和控制命令 exec

把 Docker exec 替换成 Kubernetes `pods/exec`。

需要保留的现有命令：

- Console:
  - `bash`
- Sniffer:
  - `/seedemu_sniffer`
- Controller:
  - `/seedemu_worker`

这个实现应该藏在同一个 `exec()` provider API 后面，这样 `SessionManager`、
`Sniffer` 和 `Controller` 不需要知道底层 runtime 是 Docker 还是 Kubernetes。

注意点：

- 默认使用容器名 `main`。
- console 和 worker session 使用 TTY。
- stdin/stdout/stderr streaming 行为要和现有 websocket 代码兼容。

### 6. Sniffer

现有 sniffer 的概念可以保持不变：

1. 解析所有 emulator Pods。
2. 在每个 Pod 内 exec `/seedemu_sniffer`。
3. 把 BPF 表达式写入每个 session。
4. 通过现有 websocket 转发输出。

backend 发送给前端的 `source` 应该继续使用图节点的同一个 ID。

### 7. BGP 和网络控制

现有 `Controller` 会连接 `/seedemu_worker` 并发送命令：

- `net_status`
- `net_up`
- `net_down`
- `bird_list_peer`
- BIRD peer enable/disable commands

只要 `SessionManager` 后面接的是 Kubernetes exec，这部分逻辑可以保持不变。

## 部署方式变化

Kubernetes 版 InternetMap deployment 不应该再挂载：

```text
/var/run/docker.sock
```

应该改成：

- ServiceAccount
- Role
- RoleBinding

最小 RBAC：

```yaml
rules:
- apiGroups: [""]
  resources: ["pods"]
  verbs: ["get", "list", "watch"]
- apiGroups: [""]
  resources: ["pods/exec"]
  verbs: ["create"]
- apiGroups: ["k8s.cni.cncf.io"]
  resources: ["network-attachment-definitions"]
  verbs: ["get", "list", "watch"]
```

可选：

```yaml
- apiGroups: [""]
  resources: ["pods/log"]
  verbs: ["get"]
```

生成的 InternetMap deployment 应该设置：

```yaml
env:
- name: INTERNETMAP_RUNTIME
  value: kubernetes
- name: INTERNETMAP_K8S_NAMESPACE
  valueFrom:
    fieldRef:
      fieldPath: metadata.namespace
```

## 实现顺序

1. 增加 runtime provider interface。
2. 把现有 Docker 逻辑移动到 `DockerRuntimeProvider`。
3. 增加 `KubernetesRuntimeProvider`，先支持 `/container`、`/network` 和
   `/container/:id`。
4. 确认现有前端可以从 k8s 数据渲染静态拓扑。
5. 实现 Kubernetes exec，并接入 `SessionManager`。
6. 重新启用 console、sniffer、BGP 和 network-control 功能。
7. 更新 k8s compiler 的 `attachInternetMap()` 等价逻辑，让它部署带 RBAC 的
   k8s-aware InternetMap，而不是挂 Docker socket。

## 验证清单

编译并部署 k8s-native topology 后，先检查资源：

```bash
kubectl -n seedemu-k3s-real-topo get pods
kubectl -n seedemu-k3s-real-topo get network-attachment-definitions
kubectl -n seedemu-k3s-real-topo get pod <pod> -o json | jq '.metadata.annotations'
```

检查后端 API：

```bash
curl http://localhost:8080/api/v1/container | jq
curl http://localhost:8080/api/v1/network | jq
```

期望前端行为：

- 节点显示 ASN/name label。
- 网络显示为 local/global network vertex。
- 节点和网络之间的边连接正确。
- Router 和 BorderRouter 的形状保留。
- 搜索可以按 ASN、node name、IP address、display name 工作。

然后验证交互功能：

- 打开某个节点的 console。
- 用简单 BPF filter 启动抓包。
- 在 router 上列出 BGP peers。
- 切换 BGP peer 状态。
- 切换网络 up/down。

## 兼容性说明

第一版 k8s backend 应该刻意返回 Docker-like 对象。这样可以稳定前端，减少改动
范围，把风险集中在 backend/runtime 适配层。

后续可以把前端清理成 runtime-neutral 命名，比如 `nodes`、`networks` 和
`interfaces`，但那应该是 k8s runtime 跑通之后的独立重构。

`crictl` 不应该作为主要数据源。它是 node-local 的，而且缺少 Kubernetes/Multus
拓扑上下文。后续如果需要低层 container ID 或 runtime stats，可以用它补充；
InternetMap 的主数据源应该是 Kubernetes API。

## 显示要求

可以显示出各node的pods分配数量，同时又可以显示任一node的运行的pods，同时有哪些AS在
这个node上（能不能用动态的方式）。静态的话在大规模的时候会在k8s.yaml中写明这个分配给哪个node。
最好是动态能实现。
