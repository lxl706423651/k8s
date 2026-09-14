# 2026-06-25 4954 规模 macvlan+VLAN 多 VM 实验

Generated at: `2026-06-27 15:23:01 CST`

## 实验目标

本轮重新测试 B62 4954 规模在 `macvlan+VLAN` 模式下，不同总 VM 数量的全流程阶段耗时。
总 VM 数量包含 master，因此 6/12/18/24/36/48 VM 分别对应 `workerCount=5/11/17/23/35/47`。

默认网络模式固定为：

- `networking.backend: macvlan`
- `networking.cniType/localLinkCniType/attachedCniType: macvlan`
- `networking.macvlanVlanMode: true`
- `placement.excludeControlPlane: false`，master 也参与 workload 调度

## Sweep 状态

- Sweep directory: `/home/lxl/k8s/largeScale/b62_k8s_scale/runs/macvlan_vlan_vm_sweep_0626_12plus_retry_20260626_124002_4954`
- Sweep status: `PASS`
- Topology size: `4954`

## 汇总表

| 总 VM | workerCount | status | total(s) | measured stages(s) | infra stages(s) | workload stages(s) | untracked overhead(s) | build-cluster(s) | compile(s) | build(s) | deploy(s) | wait-ready(s) | start-bird(s) | verify-bird(s) | start-kernel(s) | verify-kernel(s) | reconvergence(s) | run dir |
| ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 12 | 11 | PASS | 3480.80 | 3480.21 | 714.14 | 2766.07 | 0.59 | 694.39 | 655.53 | 311.50 | 1290.87 | 200.11 | 89.99 | 7.87 | 158.16 | 8.86 | 33.42 | `/home/lxl/k8s/largeScale/b62_k8s_scale/runs/macvlan_vlan_vm_sweep_0626_12plus_retry_20260626_124002_4954/20260626_124002_4954_vm12_w11` |
| 18 | 17 | PASS | 3894.90 | 3894.30 | 1128.50 | 2765.80 | 0.60 | 911.37 | 658.28 | 373.84 | 1311.43 | 199.98 | 51.79 | 7.96 | 107.98 | 9.17 | 31.21 | `/home/lxl/k8s/largeScale/b62_k8s_scale/runs/macvlan_vlan_vm_sweep_0626_12plus_retry_20260626_124002_4954/20260626_124002_4954_vm18_w17` |
| 24 | 23 | PASS | 4271.21 | 4270.61 | 1407.91 | 2862.70 | 0.60 | 1178.50 | 647.99 | 461.45 | 1350.04 | 134.74 | 108.26 | 8.51 | 82.09 | 9.30 | 41.82 | `/home/lxl/k8s/largeScale/b62_k8s_scale/runs/macvlan_vlan_vm_sweep_0626_12plus_retry_20260626_124002_4954/20260626_124002_4954_vm24_w23` |
| 36 | 35 | PASS | 5245.21 | 5244.62 | 1976.86 | 3267.76 | 0.59 | 1729.93 | 650.89 | 943.84 | 1298.35 | 166.20 | 74.87 | 8.41 | 57.29 | 9.45 | 31.61 | `/home/lxl/k8s/largeScale/b62_k8s_scale/runs/macvlan_vlan_vm_sweep_0626_12plus_retry_20260626_124002_4954/20260626_124002_4954_vm36_w35` |
| 48 | 47 | PASS | 5312.33 | 5311.76 | 2489.78 | 2821.98 | 0.57 | 2218.66 | 653.32 | 474.56 | 1292.42 | 198.92 | 72.98 | 8.48 | 45.09 | 8.69 | 32.29 | `/home/lxl/k8s/largeScale/b62_k8s_scale/runs/macvlan_vlan_vm_sweep_0626_12plus_retry_20260626_124002_4954/20260626_124002_4954_vm48_w47` |

## 各阶段明细

### 12 VM

- Run dir: `/home/lxl/k8s/largeScale/b62_k8s_scale/runs/macvlan_vlan_vm_sweep_0626_12plus_retry_20260626_124002_4954/20260626_124002_4954_vm12_w11`
- Namespace: `seedemu-b62-mvlan-vlan-4954-vm12`
- Status: `PASS`
- verify-after-start-bird: `PASS`
- verify-after-fib-write: `PASS`
- reconvergence: `PASS`

| Stage | Duration(s) |
| --- | ---: |
| `render-assignment` | 0.06 |
| `destroy-existing-cluster` | 0.00 |
| `prepare-libvirt-dhcp` | 18.43 |
| `build-cluster` | 694.39 |
| `ensure-all-nodes-schedulable` | 1.26 |
| `clean` | 1.76 |
| `preflight` | 8.00 |
| `compile` | 655.53 |
| `build` | 311.50 |
| `deploy` | 1290.87 |
| `wait-ready` | 200.11 |
| `start-bird` | 89.99 |
| `verify-after-start-bird` | 7.87 |
| `start-kernel` | 158.16 |
| `verify-after-fib-write` | 8.86 |
| `reconvergence` | 33.42 |

### 18 VM

- Run dir: `/home/lxl/k8s/largeScale/b62_k8s_scale/runs/macvlan_vlan_vm_sweep_0626_12plus_retry_20260626_124002_4954/20260626_124002_4954_vm18_w17`
- Namespace: `seedemu-b62-mvlan-vlan-4954-vm18`
- Status: `PASS`
- verify-after-start-bird: `PASS`
- verify-after-fib-write: `PASS`
- reconvergence: `PASS`

| Stage | Duration(s) |
| --- | ---: |
| `render-assignment` | 0.06 |
| `destroy-existing-cluster` | 190.62 |
| `prepare-libvirt-dhcp` | 24.81 |
| `build-cluster` | 911.37 |
| `ensure-all-nodes-schedulable` | 1.64 |
| `clean` | 1.75 |
| `preflight` | 12.41 |
| `compile` | 658.28 |
| `build` | 373.84 |
| `deploy` | 1311.43 |
| `wait-ready` | 199.98 |
| `start-bird` | 51.79 |
| `verify-after-start-bird` | 7.96 |
| `start-kernel` | 107.98 |
| `verify-after-fib-write` | 9.17 |
| `reconvergence` | 31.21 |

### 24 VM

- Run dir: `/home/lxl/k8s/largeScale/b62_k8s_scale/runs/macvlan_vlan_vm_sweep_0626_12plus_retry_20260626_124002_4954/20260626_124002_4954_vm24_w23`
- Namespace: `seedemu-b62-mvlan-vlan-4954-vm24`
- Status: `PASS`
- verify-after-start-bird: `PASS`
- verify-after-fib-write: `PASS`
- reconvergence: `PASS`

| Stage | Duration(s) |
| --- | ---: |
| `render-assignment` | 0.06 |
| `destroy-existing-cluster` | 194.78 |
| `prepare-libvirt-dhcp` | 32.40 |
| `build-cluster` | 1178.50 |
| `ensure-all-nodes-schedulable` | 2.17 |
| `clean` | 1.86 |
| `preflight` | 16.64 |
| `compile` | 647.99 |
| `build` | 461.45 |
| `deploy` | 1350.04 |
| `wait-ready` | 134.74 |
| `start-bird` | 108.26 |
| `verify-after-start-bird` | 8.51 |
| `start-kernel` | 82.09 |
| `verify-after-fib-write` | 9.30 |
| `reconvergence` | 41.82 |

### 36 VM

- Run dir: `/home/lxl/k8s/largeScale/b62_k8s_scale/runs/macvlan_vlan_vm_sweep_0626_12plus_retry_20260626_124002_4954/20260626_124002_4954_vm36_w35`
- Namespace: `seedemu-b62-mvlan-vlan-4954-vm36`
- Status: `PASS`
- verify-after-start-bird: `PASS`
- verify-after-fib-write: `PASS`
- reconvergence: `PASS`

| Stage | Duration(s) |
| --- | ---: |
| `render-assignment` | 0.07 |
| `destroy-existing-cluster` | 198.38 |
| `prepare-libvirt-dhcp` | 45.55 |
| `build-cluster` | 1729.93 |
| `ensure-all-nodes-schedulable` | 2.93 |
| `clean` | 1.81 |
| `preflight` | 25.04 |
| `compile` | 650.89 |
| `build` | 943.84 |
| `deploy` | 1298.35 |
| `wait-ready` | 166.20 |
| `start-bird` | 74.87 |
| `verify-after-start-bird` | 8.41 |
| `start-kernel` | 57.29 |
| `verify-after-fib-write` | 9.45 |
| `reconvergence` | 31.61 |

### 48 VM

- Run dir: `/home/lxl/k8s/largeScale/b62_k8s_scale/runs/macvlan_vlan_vm_sweep_0626_12plus_retry_20260626_124002_4954/20260626_124002_4954_vm48_w47`
- Namespace: `seedemu-b62-mvlan-vlan-4954-vm48`
- Status: `PASS`
- verify-after-start-bird: `PASS`
- verify-after-fib-write: `PASS`
- reconvergence: `PASS`

| Stage | Duration(s) |
| --- | ---: |
| `render-assignment` | 0.07 |
| `destroy-existing-cluster` | 207.13 |
| `prepare-libvirt-dhcp` | 60.11 |
| `build-cluster` | 2218.66 |
| `ensure-all-nodes-schedulable` | 3.81 |
| `clean` | 1.78 |
| `preflight` | 33.45 |
| `compile` | 653.32 |
| `build` | 474.56 |
| `deploy` | 1292.42 |
| `wait-ready` | 198.92 |
| `start-bird` | 72.98 |
| `verify-after-start-bird` | 8.48 |
| `start-kernel` | 45.09 |
| `verify-after-fib-write` | 8.69 |
| `reconvergence` | 32.29 |

## 说明

- `total(s)` 是单轮 full-flow 的墙钟总耗时；如果单轮 `summary.json` 没有顶层总耗时，则使用 sweep runner 记录的外层耗时。
- `measured stages(s)` 是该轮 `summary.json` 中所有 stage 耗时之和。
- `infra stages(s)` = `render-assignment` + `destroy-existing-cluster` + `prepare-libvirt-dhcp` + `build-cluster` + `ensure-all-nodes-schedulable`。
- `workload stages(s)` = `measured stages(s)` - `infra stages(s)`，包括 `clean/preflight/compile/build/deploy/wait-ready/start/verify/reconvergence`。
- `untracked overhead(s)` = `total(s)` - `measured stages(s)`，主要来自 stage 外层包装、进程启动/收尾、日志刷新和计时粒度差异。正常情况下应远小于总耗时。
- 因此核账关系为：`total(s) ~= measured stages(s) + untracked overhead(s)`；`measured stages(s) = infra stages(s) + workload stages(s)`。
- 每轮开始时 `runFullExperiment.py` 会销毁上一次记录的 cluster，再重新构建目标 VM 数量的 K3s cluster。
- 默认全流程不会在成功后自动销毁最后一轮 cluster，便于手工检查。

## 本轮关键观察

- 本轮 12/18/24/36/48 VM 全部通过 full flow，包括 `verify-after-start-bird`、`verify-after-fib-write` 和 `reconvergence`。
- 12VM 重跑前曾遇到 `/data/lxl/k8sTools/b62/w11/disks/seedemu-b62-master-w11.qcow2` 残留但对应 VM 未定义的问题；已通过 `destroyCluster.sh` 和清理 stale disk 目录后重跑。
- `deploy` 阶段主机 load 会出现短时高峰，但每轮 `pending`、`creating`、`creating_by_node` 都没有长期堆积；macvlan+VLAN 的 CNI ADD 没有复现 OVN+OVS 下的长尾卡死现象。
- `start-bird` 阶段多轮日志显示 `started=0 skipped=4954`，说明进入该阶段时容器内 BIRD 已经在运行；该阶段保持幂等检查并继续执行后续协议验证。
- 当前脚本不生成、也不检查显式 `include "/etc/bird/conf/kernel.conf";`；BIRD 配置加载依赖编译生成的通配 include。
