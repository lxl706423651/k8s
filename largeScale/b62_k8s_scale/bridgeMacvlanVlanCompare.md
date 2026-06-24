# bridge+VLAN 与 macvlan+VLAN 4954 规模重建对比

## 实验目标

本轮实验按用户要求直接重新构造 VM 与 K3s cluster，对比 `bridge+VLAN`
和 `macvlan+VLAN` 在 4954 规模下的全流程耗时、deploy 压力、系统负载、
BIRD/FIB 验证和二层隔离效果。

两轮均重新执行：

- destroy 旧 cluster/VM/libvirt 网络
- 重新准备 libvirt DHCP/network
- 重新创建 32 台 VM（master + 31 worker，master 参与调度）
- 重新 build K3s cluster
- compile/build/deploy/wait-ready
- start-bird、verify-start-bird、start-kernel、verify-FIB
- reconvergence
- VLAN/CNI 隔离观测

运行目录：

- bridge+VLAN: `runs/rebuild_compare_bridge_macvlan_vlan_20260623_092436_4954_w31/bridge_vlan_4954_w31`
- macvlan+VLAN: `runs/rebuild_compare_bridge_macvlan_vlan_20260623_092436_4954_w31/macvlan_vlan_4954_w31`

两轮 assignment：

- `runs/rebuild_compare_bridge_macvlan_vlan_20260623_092436_4954_w31/assignments/assignment_bridge_vlan_4954_w31.yaml`
- `runs/rebuild_compare_bridge_macvlan_vlan_20260623_092436_4954_w31/assignments/assignment_macvlan_vlan_4954_w31.yaml`

## 参数

| 项目 | 值 |
| --- | --- |
| 拓扑规模 | 4954 |
| VM 数量 | 32（master + 31 worker） |
| master 资源 | 48 vCPU / 96 GiB |
| 总资源 | 300 vCPU / 600 GiB |
| worker 资源 | 每台约 8-9 vCPU / 16.2 GiB |
| deploy batch size | 80 |
| warmup | 12 x 1 |
| maxPending / maxCreating / maxNotReady | 160 / 160 / 160 |
| maxCreatingPerNode | 40 |
| Kube-OVN workload Subnet | 不创建 |
| Kube-OVN subnet wait | false |
| post-subnet cooldown | 0s |
| node-stream max active per node | 4 |
| start-bird | 所有节点并发、节点内串行、容器间隔 0.1s、load 阈值 80 |
| start-kernel | 所有节点并发、节点内串行、容器间隔 0.3s、load 阈值 80、export mode all |

两组都走 VLAN-backed Multus + static IPAM，不走 workload 级 Kube-OVN Subnet。
区别只在 CNI 类型：

- bridge+VLAN: 每个网络在节点上使用 `ensX.<vlan> + Linux bridge + Pod veth`
- macvlan+VLAN: Pod macvlan 接口直接挂在 `ensX.<vlan>` parent 上

## 全流程结果

两轮均 PASS。

| 指标 | bridge+VLAN | macvlan+VLAN | 差异 |
| --- | ---: | ---: | ---: |
| 全流程总耗时 | 4309.22s | 4154.21s | macvlan 快 155.01s |
| 排除 destroy/network/build-cluster 后 | 2545.66s | 2417.23s | macvlan 快 128.43s |
| deploy | 1504.93s | 1358.92s | macvlan 快 146.01s |
| wait-ready | 101.91s | 166.30s | bridge 快 64.39s |
| start-bird | 44.77s | 36.17s | macvlan 快 8.60s |
| verify start-bird | 113.41s | 111.45s | 基本一致 |
| start-kernel | 84.05s | 76.07s | macvlan 快 7.98s |
| verify FIB | 9.58s | 9.55s | 一致 |
| reconvergence | 16.08s | 15.96s | 一致 |

完整阶段耗时：

| 阶段 | bridge+VLAN | macvlan+VLAN |
| --- | ---: | ---: |
| render-assignment | 0.07s | 0.07s |
| destroy-existing-cluster | 250.36s | 226.86s |
| prepare-libvirt-dhcp | 37.04s | 36.60s |
| build-cluster | 1476.16s | 1473.52s |
| ensure-all-nodes-schedulable | 2.55s | 2.51s |
| clean | 1.73s | 1.75s |
| preflight | 21.69s | 22.07s |
| compile | 231.02s | 228.03s |
| build | 413.87s | 388.38s |
| deploy | 1504.93s | 1358.92s |
| wait-ready | 101.91s | 166.30s |
| start-bird | 44.77s | 36.17s |
| verify-after-start-bird | 113.41s | 111.45s |
| start-kernel | 84.05s | 76.07s |
| verify-after-fib-write | 9.58s | 9.55s |
| reconvergence | 16.08s | 15.96s |

## Deploy 压力

| 指标 | bridge+VLAN | macvlan+VLAN |
| --- | ---: | ---: |
| pressure 采样数 | 148 | 148 |
| max pending | 4 | 2 |
| max creating | 34 | 25 |
| max running_notready | 0 | 0 |
| max failed | 0 | 0 |
| max creating per node | 2 | 2 |

两者都没有出现 OVN+OVS 里的 CNI ADD 长尾。典型表现是：

- `ContainerCreating` 数量始终较低；
- `failed=0`；
- 单节点 `max_creating_per_node` 不超过 2；
- Pod ready 数随 batch 线性增长。

本轮 macvlan+VLAN 在 deploy 阶段更快，且最大 `creating` 更低。
这说明在当前实现和参数下，macvlan+VLAN 的 CNI ADD 路径比 bridge+VLAN 更轻。

## Load 对比

| 阶段 | 指标 | bridge+VLAN | macvlan+VLAN |
| --- | --- | ---: | ---: |
| deploy | max load1 | 192.13 | 134.63 |
| deploy | avg load1 | 91.02 | 77.76 |
| deploy | min CPU idle | 55.60% | 54.10% |
| wait-ready | max load1 | 164.41 | 127.98 |
| start-bird | max load1 | 115.22 | 93.28 |
| start-kernel | max load1 | 126.75 | 95.73 |

两组运行中 load1 都会升高，但 CPU idle 仍保持较高。这个现象更像大量短生命周期
`kubectl`/container/CNI/进程调度带来的运行队列压力，不是 CPU 被持续打满。

macvlan+VLAN 的 deploy、start-bird、start-kernel load 峰值都低于 bridge+VLAN。
这符合实现预期：macvlan 直接挂在 VLAN parent 上，少了每个网络的 Linux bridge
对象和 veth enslave 操作。

## BIRD/FIB 验证

两轮 BIRD 与 FIB 验证均通过。

| 验证项 | bridge+VLAN | macvlan+VLAN |
| --- | ---: | ---: |
| verify start-bird failed samples | 0 | 0 |
| verify start-bird protocol attempts | 6 | 6 |
| verify start-bird convergence wait | 104s | 103s |
| verify FIB failed samples | 0 | 0 |
| verify FIB checked AS samples | 10 | 10 |
| reconvergence failure convergence | 0.75s | 0.72s |
| reconvergence recovery convergence | 0.76s | 0.76s |

结论：在 4954 规模下，两种 VLAN-backed CNI 都可以支持当前 BIRD 启动、
kernel route 写入和 reconvergence 测试。

## 二层隔离验证

两轮均使用 `compareVlanCniObservability.py` 抽样验证 `net-ix-ix13`。

共同结论：

- 同一 VLAN 网络内 ping 通过；
- 同一 VLAN 网络内 listener Pod 能看到 ARP；
- 不同 VLAN 网络的 listener Pod 看不到该 ARP；
- host 上对应 VLAN parent 存在且 `operstate=up`。

结果：

| 检查项 | bridge+VLAN | macvlan+VLAN |
| --- | --- | --- |
| `sameNetworkPingPassed` | true | true |
| `sameNetworkPodSawArp` | true | true |
| `differentNetworkPodSawNoArp` | true | true |
| `hostMasterVisible` | true | true |
| `hostBridgePortListVisible` | true | 不适用 |

注意：跨网络 ping 可以通过三层路由成功，这不代表二层广播泄漏。二层隔离看的是
ARP 广播是否只出现在相同 VLAN 广播域内。本轮不同网络 listener Pod 的 ARP
计数为 0，因此 VLAN 隔离成立。

## bridge 的优势

这次实验可以明确比较出 bridge+VLAN 的优势：不是性能，而是可观测性和可控制性。

bridge+VLAN 在每个节点上为对应仿真网络创建 Linux bridge。观测脚本在 bridge
轮中能直接看到类似以下结构：

- VLAN parent: `ens3.419`
- bridge: `br-c71549a70f47`
- Pod veth: 多个 `veth...`
- sysfs: `/sys/class/net/br-c71549a70f47/brif`
- 示例端口数：`port_count=14`

这意味着 bridge+VLAN 可以直接回答：

- 某个节点上某个仿真二层网络接了哪些 Pod 接口；
- 某个 VLAN parent 是否被正确接入 bridge；
- 某个 bridge 的 FDB、端口状态、qdisc/tc、抓包点是否正常；
- 后续如果要做链路故障注入、端口级限速、丢包、延迟或 debug，操作对象更明确。

macvlan+VLAN 没有 node-local bridge 对象。观测脚本记录：

- `macvlan_has_no_node_local_bridge_port_list=1`
- root namespace 中没有可枚举所有 Pod macvlan 子接口的 bridge port list

因此 macvlan+VLAN 的优势是路径更轻、性能更好；劣势是 host 侧看不到一个
per-network 的集中端口成员列表，后续做故障注入和定位广播路径不如 bridge 直接。

## 结论

1. 两种模式都能跑通 32 VM、4954 规模全流程，并且都满足二层网络隔离。
2. 本轮干净重建对比中，macvlan+VLAN 总体更快：全流程快约 155s，deploy 快约 146s。
3. macvlan+VLAN 的 deploy 压力更低：max creating 为 25，bridge 为 34；deploy load 峰值也更低。
4. bridge+VLAN 的核心优势是可观测性和可控制性：有 per-network Linux bridge，可直接查看端口成员并做网络级调试或故障注入。
5. 如果论文重点是最高部署性能，当前数据支持优先使用 macvlan+VLAN；如果论文需要展示、控制和解释每个二层网络的端口结构，bridge+VLAN 更适合作为可观测实验平台。

## 残余风险

本轮是单次顺序实验，顺序为 bridge+VLAN 后 macvlan+VLAN。虽然两轮都重新构造
VM 与 K3s cluster，但 host 侧 Docker layer cache、磁盘 cache 和系统背景负载仍可能影响
几十秒到一两分钟级别的差异。若要写入严格性能结论，建议后续至少做一次反向顺序：

`macvlan+VLAN -> bridge+VLAN`

并取两轮均值或中位数。
