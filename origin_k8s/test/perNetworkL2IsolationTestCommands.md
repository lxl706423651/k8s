# Per-network L2 隔离检测命令

本文记录两套部署的手工检测命令：

- K8s/KVM + Multus/macvlan：检测 `net-2-net-101-102` 的 ARP 是否泄露到其他仿真网段。
- Docker 单机：检测 Docker per-network bridge 是否把 ARP 限制在对应仿真网络内。

核心判断标准：

```text
如果非目标仿真网段的 Pod/容器/bridge 也看到：
  ARP, Request who-has 10.2.1.253 tell 10.2.1.254

说明没有做到严格 per-network L2 隔离。

如果只有目标网络本身看到 ARP，非目标网络看不到，
说明这次测试中的 per-network L2 隔离成立。
```

## 1. K8s/KVM + Multus/macvlan 版本

### 1.1 设置测试变量

```bash
KUBECONFIG=/home/lxl/k8s/output/kubeconfigs/seedemu-k3s.yaml
```

指定访问 KVM K3s 集群的 kubeconfig。

```bash
NS=seedemu-k3s-real-topo
```

指定 SeedEMU workload 所在 namespace。

```bash
SRC=as2brd-r101-10.101.0.2-6bfb57c455-6b99s
```

指定 ARP 源 Pod。这个 Pod 在共同仿真网络 `net_101_102` 上的地址是 `10.2.1.254/24`。

```bash
DST_IP=10.2.1.253
```

指定目标 IP。它属于 `as2brd-r102` 的 `net_101_102` 接口。

```bash
OBS_TARGET=as2brd-r102-10.102.0.2-fd7f67946-jrlvg
```

指定正样本观察 Pod。它属于同一个仿真网络 `10.2.1.0/24`，应该能看到 ARP。

```bash
OBS_OTHER1=as160brd-router0-10.160.0.254-58db8fbc4d-snrnf
```

指定负样本观察 Pod。它的 `net0` 是 `10.160.0.0/24`，不属于 `10.2.1.0/24`。

```bash
OBS_OTHER2=as150h-host-0-10.150.0.71-76647f4d48-rv7gv
```

指定另一个负样本观察 Pod。它的 `net0` 是 `10.150.0.0/24`。

### 1.2 查看 Pod 是否存在并确认调度节点

```bash
kubectl --kubeconfig "$KUBECONFIG" -n "$NS" get pod "$SRC" "$OBS_TARGET" "$OBS_OTHER1" "$OBS_OTHER2" -o wide
```

确认 4 个 Pod 都是 `Running`，并记录它们在哪些 KVM worker 上。

### 1.3 确认源 Pod 和目标 Pod 的共同接口

```bash
kubectl --kubeconfig "$KUBECONFIG" -n "$NS" exec "$SRC" -- ip -br addr
```

查看源 Pod 的接口。应能看到：

```text
net_101_102  10.2.1.254/24
```

```bash
kubectl --kubeconfig "$KUBECONFIG" -n "$NS" exec "$OBS_TARGET" -- ip -br addr
```

查看目标 Pod 的接口。应能看到：

```text
net_101_102  10.2.1.253/24
```

### 1.4 确认负样本 Pod 不属于 10.2.1.0/24

```bash
kubectl --kubeconfig "$KUBECONFIG" -n "$NS" exec "$OBS_OTHER1" -- ip -br addr
```

确认 `OBS_OTHER1` 的观察接口是 `net0`，并且地址是 `10.160.0.254/24`。

```bash
kubectl --kubeconfig "$KUBECONFIG" -n "$NS" exec "$OBS_OTHER2" -- ip -br addr
```

确认 `OBS_OTHER2` 的观察接口是 `net0`，并且地址是 `10.150.0.71/24`。

### 1.5 在目标 Pod 上抓 ARP 正样本

在第一个终端执行：

```bash
kubectl --kubeconfig "$KUBECONFIG" -n "$NS" exec "$OBS_TARGET" -- \
  timeout 15 tcpdump -eni net_101_102 'arp and host 10.2.1.253'
```

这个命令在目标 Pod 的 `net_101_102` 接口上抓 ARP。正样本应看到：

```text
Request who-has 10.2.1.253 tell 10.2.1.254
Reply 10.2.1.253 is-at ...
```

### 1.6 在非目标 Pod 上抓 ARP 负样本

在第二个终端执行：

```bash
kubectl --kubeconfig "$KUBECONFIG" -n "$NS" exec "$OBS_OTHER1" -- \
  timeout 15 tcpdump -eni net0 'arp and host 10.2.1.253'
```

这个命令在 `10.160.0.0/24` 网络的 Pod 接口上抓同一个 ARP。若这里看到 `who-has 10.2.1.253`，说明 ARP 泄露到了非目标仿真网段。

在第三个终端执行：

```bash
kubectl --kubeconfig "$KUBECONFIG" -n "$NS" exec "$OBS_OTHER2" -- \
  timeout 15 tcpdump -eni net0 'arp and host 10.2.1.253'
```

这个命令在 `10.150.0.0/24` 网络的 Pod 接口上抓同一个 ARP。

### 1.7 触发源 Pod 重新发送 ARP

在第四个终端执行：

```bash
kubectl --kubeconfig "$KUBECONFIG" -n "$NS" exec "$SRC" -- \
  ip neigh flush 10.2.1.253 dev net_101_102
```

清除源 Pod 对 `10.2.1.253` 的邻居缓存，确保下一次 ping 会重新发 ARP。

```bash
kubectl --kubeconfig "$KUBECONFIG" -n "$NS" exec "$SRC" -- \
  ping -c 3 10.2.1.253
```

触发源 Pod 从 `net_101_102` 访问目标 IP，从而产生 ARP request。

### 1.8 KVM/macvlan 版判断

如果 `OBS_OTHER1` 或 `OBS_OTHER2` 看到：

```text
Request who-has 10.2.1.253 tell 10.2.1.254
```

说明 KVM/macvlan 当前不是严格 per-network L2 隔离。

我们已实测到：

```text
target-in-prefix: yes
same-source-node-nonprefix: yes
target-node-nonprefix: yes
other-node-nonprefix: yes
```

原因是当前很多 NAD 都是：

```text
macvlan over ens2
```

底层共享同一个：

```text
VM ens2 -> host vnetX -> host virbr0
```

Multus 只负责按 NAD 创建接口，不负责为每个 NAD 建独立二层广播域。

### 1.9 一条命令运行 KVM 自动测试脚本

```bash
/home/lxl/k8s/origin_k8s/test/validateMacvlanArpVisibility.sh \
  --kubeconfig /home/lxl/k8s/output/kubeconfigs/seedemu-k3s.yaml \
  --namespace seedemu-k3s-real-topo \
  --source-pod as2brd-r101-10.101.0.2-6bfb57c455-6b99s \
  --source-interface net_101_102 \
  --target-ip 10.2.1.253 \
  --observer target-in-prefix:as2brd-r102-10.102.0.2-fd7f67946-jrlvg:net_101_102 \
  --observer same-source-node-nonprefix:as160brd-router0-10.160.0.254-58db8fbc4d-snrnf:net0 \
  --observer other-node-nonprefix:as150h-host-0-10.150.0.71-76647f4d48-rv7gv:net0 \
  --capture-seconds 12 \
  --output-dir /home/lxl/k8s/origin_k8s/test/manual-kvm-arp-test
```

这个脚本自动执行抓包、flush neighbor、ping，并生成 `result.md`。

## 2. Docker 单机版本

### 2.1 查看 Docker 容器

```bash
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}' | grep 'as2brd-r10[12]'
```

确认 `as2brd-r101-10.101.0.2` 和 `as2brd-r102-10.102.0.2` 正在运行。

```bash
docker ps --format '{{.Names}}' | grep -E 'as160brd-router0|as150h-host_0'
```

确认两个非目标网络观察容器正在运行。

### 2.2 查看 Docker 网络

```bash
docker network ls | grep 'output_net_2_net_101_102'
```

确认 `10.2.1.0/24` 对应 Docker 网络存在。

```bash
docker network ls | grep -E 'output_net_150_net0|output_net_160_net0'
```

确认非目标仿真网络对应的 Docker 网络存在。

### 2.3 查看目标网络连接了哪些容器

```bash
docker network inspect output_net_2_net_101_102 --format '{{json .Containers}}'
```

确认这个 Docker 网络中只有属于 `net_101_102` 的容器，例如：

```text
as2brd-r101-10.101.0.2  10.2.1.254/24
as2brd-r102-10.102.0.2  10.2.1.253/24
```

### 2.4 找到 Docker bridge 名称

```bash
ip -br link show type bridge | grep 'br-37b29e35d299'
```

确认 `output_net_2_net_101_102` 对应的 host bridge 存在。当前实测为：

```text
br-37b29e35d299
```

```bash
ip -br link show type bridge | grep -E 'br-4de4b2ae28c4|br-7d7363381ae3'
```

确认非目标网络的 host bridge 存在。当前实测为：

```text
output_net_150_net0 -> br-4de4b2ae28c4
output_net_160_net0 -> br-7d7363381ae3
```

### 2.5 确认源容器与目标容器接口

```bash
docker exec as2brd-r101-10.101.0.2 ip -br addr
```

确认源容器有：

```text
net_101_102  10.2.1.254/24
```

```bash
docker exec as2brd-r102-10.102.0.2 ip -br addr
```

确认目标容器有：

```text
net_101_102  10.2.1.253/24
```

### 2.6 在目标容器抓 ARP 正样本

在第一个终端执行：

```bash
docker exec as2brd-r102-10.102.0.2 \
  timeout 15 tcpdump -eni net_101_102 'arp and host 10.2.1.253'
```

这个命令在目标容器接口上抓包。应看到 ARP request/reply。

### 2.7 在非目标容器抓 ARP 负样本

在第二个终端执行：

```bash
docker exec as160brd-router0-10.160.0.254 \
  timeout 15 tcpdump -eni net0 'arp and host 10.2.1.253'
```

这个命令在 `10.160.0.0/24` 网络的容器接口上抓目标 ARP。若这里看到目标 ARP，则说明 Docker 网络隔离有问题。

在第三个终端执行：

```bash
docker exec as150h-host_0-10.150.0.71 \
  timeout 15 tcpdump -eni net0 'arp and host 10.2.1.253'
```

这个命令在 `10.150.0.0/24` 网络的容器接口上抓目标 ARP。

### 2.8 在宿主机 Docker bridge 上抓包

在第四个终端执行：

```bash
sudo -n timeout 15 tcpdump -eni br-37b29e35d299 'arp and host 10.2.1.253'
```

这个命令在目标 Docker bridge 上抓包。应看到 ARP request/reply。

在第五个终端执行：

```bash
sudo -n timeout 15 tcpdump -eni br-4de4b2ae28c4 'arp and host 10.2.1.253'
```

这个命令在非目标 `10.150.0.0/24` Docker bridge 上抓包。若隔离正确，应为 `0 packets captured`。

在第六个终端执行：

```bash
sudo -n timeout 15 tcpdump -eni br-7d7363381ae3 'arp and host 10.2.1.253'
```

这个命令在非目标 `10.160.0.0/24` Docker bridge 上抓包。若隔离正确，应为 `0 packets captured`。

### 2.9 触发源容器重新发送 ARP

在第七个终端执行：

```bash
docker exec as2brd-r101-10.101.0.2 \
  ip neigh flush 10.2.1.253 dev net_101_102
```

清除源容器到目标 IP 的 ARP 缓存。

```bash
docker exec as2brd-r101-10.101.0.2 \
  ping -c 3 10.2.1.253
```

触发源容器重新 ARP 并发送 ICMP。

### 2.10 Docker 版判断

如果只有下面两个位置看到 ARP：

```text
as2brd-r102-10.102.0.2:net_101_102
br-37b29e35d299
```

而下面位置都看不到：

```text
as160brd-router0-10.160.0.254:net0
as150h-host_0-10.150.0.71:net0
br-4de4b2ae28c4
br-7d7363381ae3
```

说明 Docker 单机版本在这次测试中做好了 per-network L2 隔离。

我们已实测到：

```text
target container: yes
target bridge: yes
nonprefix containers: no
nonprefix bridges: no
```

原因是 Docker 为每个仿真网络创建独立 bridge：

```text
output_net_2_net_101_102 -> br-37b29e35d299
output_net_150_net0      -> br-4de4b2ae28c4
output_net_160_net0      -> br-7d7363381ae3
```

ARP 广播被限制在对应 bridge 内。

### 2.11 一条命令运行 Docker 自动测试脚本

```bash
/home/lxl/k8s/origin_k8s/test/validateDockerArpIsolation.sh \
  --source-container as2brd-r101-10.101.0.2 \
  --source-interface net_101_102 \
  --target-ip 10.2.1.253 \
  --container-observer target-in-prefix:as2brd-r102-10.102.0.2:net_101_102 \
  --container-observer nonprefix-router:as160brd-router0-10.160.0.254:net0 \
  --container-observer nonprefix-host:as150h-host_0-10.150.0.71:net0 \
  --bridge-observer target-bridge:br-37b29e35d299 \
  --bridge-observer nonprefix-150-bridge:br-4de4b2ae28c4 \
  --bridge-observer nonprefix-160-bridge:br-7d7363381ae3 \
  --capture-seconds 12 \
  --output-dir /home/lxl/k8s/origin_k8s/test/manual-docker-arp-test
```

这个脚本自动执行容器抓包、host bridge 抓包、flush neighbor、ping，并生成 `result.md`。

## 3. 两种版本的结论对比

| 部署方式 | 数据面模型 | per-network L2 隔离结果 |
| --- | --- | --- |
| K8s/KVM + Multus/macvlan | 多个 NAD 共享 `ens2/virbr0` 底层二层域 | 未隔离，非目标 Pod 可看到 ARP |
| Docker 单机 | 每个仿真网络一个 Docker bridge | 已隔离，非目标 bridge/容器看不到 ARP |

如果后续希望 K8s 版本也达到 Docker 版本这种 per-network L2 隔离，需要考虑：

```text
1. 每个仿真网络一个 VLAN/subinterface；
2. 每个仿真网络一个 Linux bridge/OVS bridge；
3. OVN/Kube-OVN secondary L2 network，让每个 NAD 对应一个独立 logical switch。
```
