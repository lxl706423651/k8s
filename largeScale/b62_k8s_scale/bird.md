# B62 start-bird / start-kernel audit

Updated: 2026-06-12

This document records the current B62 BIRD stage behavior, how it compares with
`/home/lxl/k8s/lxl`, and the risks to watch when running the 4954-scale
experiment on the KVM + K3s + Multus + Kube-OVN/OVS environment.

## Current conclusion

- `start_bird.sh` and `start_bird_kernel.sh` now use node-local SSH +
  `crictl`/`nsenter` helpers instead of per-Pod `kubectl exec`.
- Host-side `kubectl` is used only once per stage to discover running
  router-like targets and expected per-node counts.
- All K3s nodes run concurrently. Inside each node, pods are processed
  serially.
- Each node checks its own `/proc/loadavg` before every pod operation. If the
  1-minute load is `>= 50`, that node sleeps for `30s` and checks again.
- `start-bird` no longer performs the expensive Phase 3 per-Pod
  `birdc show status` verification.
- `start-kernel` writes `kernel.conf` and runs `birdc configure` /
  `birdc 'reload kernel'`, but no longer checks every Pod for a Kernel protocol
  up state.
- The compiled images currently include `/etc/bird/conf/kernel.conf`, and many
  `bird.conf` files include `/etc/bird/conf/*.conf`. `start-bird` now keeps
  that image-baked kernel protocol active, because the generated kernel policy
  exports only `RTS_DEVICE` and `RTS_OSPF` into Linux, not BGP routes.
  `start-kernel` can still overwrite `kernel.conf` later when the experiment
  needs the full route-export policy.

## 2026-06-11 current parameters

`start_bird.sh`:

| Parameter | Value | Meaning |
| --- | ---: | --- |
| `BIRD_NODE_CONCURRENCY` | 0 | 0 means all discovered K3s nodes concurrently |
| `BIRD_PARALLEL_PER_NODE` | 1 | Node-local helper forces serial per-node execution |
| `BIRD_START_BATCH_SIZE` | 20 | Per node, fixed cooldown after this many newly started BIRD processes |
| `BIRD_START_BATCH_COOLDOWN_SECONDS` | 60 | Fixed cooldown after each start batch |
| `BIRD_START_DELAY_SECONDS` | 0.08 | Sleep after each pod start attempt on a node |
| `BIRD_LOAD_THRESHOLD` | 50 | Per-node load gate before each pod operation |
| `BIRD_LOAD_CHECK_INTERVAL_SECONDS` | 30 | Cooldown interval when node load is too high |
| `BIRD_START_EXEC_TIMEOUT_SECONDS` | 120 | Timeout for one container nsenter start operation |
| `BIRD_NODE_TIMEOUT_SECONDS` | 36000 | Timeout for one node's full serial stage |
| `KUBECTL_LIST_TIMEOUT_SECONDS` | 60 | One-time target discovery timeout |

`start_bird_kernel.sh`:

| Parameter | Value | Meaning |
| --- | ---: | --- |
| `KERNEL_NODE_CONCURRENCY` | 0 | 0 means all discovered K3s nodes concurrently |
| `KERNEL_PARALLEL_PER_NODE` | 1 | Node-local helper forces serial per-node execution |
| `KERNEL_SWITCH_BATCH_SIZE` | 20 | Per node, fixed cooldown after this many kernel switch operations |
| `KERNEL_SWITCH_BATCH_COOLDOWN_SECONDS` | 60 | Fixed cooldown after each kernel switch batch |
| `KERNEL_SWITCH_DELAY_SECONDS` | 0.3 | Sleep after each pod kernel switch attempt |
| `KERNEL_LOAD_THRESHOLD` | 50 | Per-node load gate before each pod operation |
| `KERNEL_LOAD_CHECK_INTERVAL_SECONDS` | 30 | Cooldown interval when node load is too high |
| `KERNEL_EXEC_TIMEOUT_SECONDS` | 180 | Timeout for one container nsenter kernel operation |
| `KERNEL_BIRDC_TIMEOUT_SECONDS` | 20 | Timeout around `birdc configure` and `reload kernel` |
| `KERNEL_EXPORT_MODE` | `all` | Export all BIRD routes into Linux kernel |
| `KERNEL_SCAN_BASE_SECONDS` | 6000 | Kernel protocol scan base |
| `KERNEL_SCAN_JITTER_SECONDS` | 120 | Per-pod scan jitter |
| `KERNEL_NODE_TIMEOUT_SECONDS` | 36000 | Timeout for one node's full serial stage |

The sections below include the older 2026-06-10 comparison against the lxl
kubectl-exec implementation. They remain useful for parameter provenance, but
the node-local implementation above is now the active B62 path.

## 2026-06-11 protocol parameter assessment

Observed from the current `runs/20260610_235841_4954_w11/output` Docker build
contexts:

- Many router `bird.conf` files include `include "/etc/bird/conf/*.conf";`.
- Every sampled Dockerfile copies `8e9a384...` to
  `/etc/bird/conf/kernel.conf`; there are 4954 copies in the current output.
- The image-baked `kernel.conf` exports only `RTS_DEVICE` and `RTS_OSPF`, not
  BGP, and uses a very large `scan time` around 60k-70k seconds.
- `start-kernel` intentionally overwrites that with the runtime script's
  `KERNEL_EXPORT_MODE=all`, `scan base=6000`, and `jitter=120`.
- OSPF is commonly generated with `tick 1` and interface settings like
  `hello 1; dead count 2;`.
- iBGP timer lines such as `hold time 36000;` and `keepalive time 60;` appear
  commented out in the generated config. eBGP timer settings were not found in
  the sampled active blocks, so BIRD defaults are being used unless another
  generated block sets them elsewhere.

Assessment:

- First fix the execution staging before changing routing-protocol semantics.
  If `kernel.conf` is active during `start-bird`, then `start-bird` is no
  longer just a BIRD process-start stage; it is also a kernel route injection
  stage. That is the most direct explanation for unexpectedly high load during
  start-bird.
- OSPF `hello 1` / `dead count 2` is aggressive at this scale. It gives fast
  adjacency detection, but it also increases periodic control traffic and makes
  transient scheduling delays more likely to be interpreted as neighbor
  failure. If OSPF adjacency churn appears after the node-local start is stable,
  test a conservative profile such as `hello 5; dead count 4; tick 5`.
- BGP hold/keepalive timers are unlikely to be the first lever for initial
  start-bird CPU pressure. They mostly affect failure detection and keepalive
  traffic after sessions are established. If BGP sessions flap under high load,
  then enabling a long hold time for iBGP, for example a 30-60 minute hold time,
  may help tolerate slow convergence, but it changes failure detection behavior.
- `KERNEL_EXPORT_MODE=all` is semantically correct for full data-plane route
  installation, but it is the heaviest start-kernel mode. `device_ospf_only`
  remains useful only as a diagnostic mode to separate BIRD control-plane
  convergence from Linux FIB insertion pressure.

## start-bird comparison

| Item | Current B62 | `/home/lxl/k8s/lxl` reference | Status |
| --- | --- | --- | --- |
| Entrypoint | `start_bird.sh` | `start-bird` | Equivalent wrapper role |
| Helper | `start_bird_helper.py` | `seed_k8s_start_bird0130.py` | Same main model |
| Target pods | `seedemu.io/workload=seedemu`, role in `r/brd/rs`, phase `Running` | Same | Match |
| Pod ordering | ASN then pod name | Same | Match |
| Node model | all nodes concurrent | all nodes concurrent | Match |
| Per-node model | pods serial inside each node | pods serial inside each node | Match |
| Start delay | `0.08s` | `0.08s` | Match |
| Start command | cleanup BIRD ctl/pid files, then `bird ... || (bird -d ... & sleep 1)` | Same | Match |
| Kernel config handling | does not delete or modify `kernel.conf` | Same | Match |
| Post-start settle | `60s` | `60s` | Match |
| Load wait | one probe pod per node, wait for load `< 40`, check every `20s` | Same | Match |
| Final status pass | serial `birdc show status`, progress every 200, timeout 1200s | Same | Match |
| B62 additions | explicit kubeconfig arg, explicit wrapper params, B62 and lxl artifact names, cached target fallback | Not in lxl | Compatibility only |

Current `start_bird.sh` parameters:

| Parameter | Value | Notes |
| --- | ---: | --- |
| `KUBECTL_LIST_TIMEOUT_SECONDS` | 60 | lxl uses a 60s pod list timeout |
| `BIRD_START_DELAY_SECONDS` | 0.08 | delay between pods inside one node |
| `BIRD_LOAD_THRESHOLD` | 40 | post-start node load threshold |
| `BIRD_LOAD_CHECK_INTERVAL_SECONDS` | 20 | load polling interval |
| `BIRD_KUBECTL_EXEC_TIMEOUT_SECONDS` | 30 | base exec timeout |
| `BIRD_START_EXEC_TIMEOUT_SECONDS` | 45 | BIRD start command timeout |
| `BIRD_START_RETRIES` | 2 | per-pod retries |
| `BIRD_START_RETRY_BACKOFF_SECONDS` | 1 | retry sleep |
| `BIRD_POST_START_SETTLE_SECONDS` | 60 | sleep before load wait |
| `BIRD_PHASE_TIMEOUT_SECONDS` | 1200 | final `birdc show status` timeout |
| `BIRD_PHASE3_PROGRESS_EVERY` | 200 | final status progress interval |

## start-kernel comparison

| Item | Current B62 | `/home/lxl/k8s/lxl` reference | Status |
| --- | --- | --- | --- |
| Entrypoint | `start_bird_kernel.sh` | `start-kernel` | Equivalent wrapper role |
| Helper | `start_bird_kernel_helper.py` | `seed_k8s_start_bird_kernel.py` | Same main model |
| Target pods | `seedemu.io/workload=seedemu`, role in `r/brd/rs`, phase `Running` | Same | Match |
| Pod ordering | ASN then pod name | Same | Match |
| Node model | all nodes concurrent | all nodes concurrent | Match |
| Per-node model | pods serial inside each node | pods serial inside each node | Match |
| Switch delay | `0.3s` | `0.3s` | Match |
| Export mode | `all` | default `all` | Match |
| Kernel scan | base `6000s`, jitter `120s` | default `6000s` and `120s` | Match |
| Post-switch settle | `15s` | `15s` | Match |
| Load wait | wait for load `< 40`, check every `20s` | Same | Match |
| Config write | writes `/etc/bird/conf/kernel.conf` | Same | Match |
| Include handling | removes explicit `kernel.conf` include when wildcard include exists; appends explicit include only if no wildcard exists | lxl assumes include path is already usable | B62 compatibility difference |
| Reload/check | `birdc configure`, then requires a Kernel protocol to be up | lxl runs `birdc configure` and `birdc 'reload kernel' || true` | B62 is stricter |

Current `start_bird_kernel.sh` parameters:

| Parameter | Value | Notes |
| --- | ---: | --- |
| `KERNEL_SWITCH_DELAY_SECONDS` | 0.3 | delay between pods inside one node |
| `KERNEL_LOAD_THRESHOLD` | 40 | post-switch load threshold |
| `KERNEL_LOAD_CHECK_INTERVAL_SECONDS` | 20 | load polling interval |
| `KERNEL_KUBECTL_EXEC_TIMEOUT_SECONDS` | 30 | base exec timeout |
| `KERNEL_EXEC_TIMEOUT_SECONDS` | 45 | config/reload command timeout |
| `KERNEL_BIRDC_TIMEOUT_SECONDS` | 10 | `birdc` timeout inside pod |
| `KERNEL_EXPORT_MODE` | `all` | exports all BIRD routes into Linux kernel |
| `KERNEL_SCAN_BASE_SECONDS` | 6000 | kernel protocol scan base |
| `KERNEL_SCAN_JITTER_SECONDS` | 120 | per-pod scan jitter |
| `KERNEL_SWITCH_RETRIES` | 2 | per-pod retries |
| `KERNEL_SWITCH_RETRY_BACKOFF_SECONDS` | 1 | retry sleep |
| `KERNEL_POST_SWITCH_SETTLE_SECONDS` | 15 | sleep before load wait |

## Key risks

### 1. `kernel.conf` start-bird policy

Current `start-bird` keeps `kernel.conf` active. This matches the intended B62
policy after the BIRD config change: the image-baked kernel protocol exports
only direct and OSPF routes into Linux, so iBGP loopback reachability can work
before the later full `start-kernel` route-export stage.

Why this matters:

- The fast image archive logic treats `etc/bird/conf/kernel.conf` as part of
  the generated image content.
- If compiled images already contain `kernel.conf` and `bird.conf` includes
  `/etc/bird/conf/*.conf`, BIRD will load this limited kernel protocol during
  `start-bird`.
- This intentionally moves only direct/OSPF kernel route installation into the
  BIRD-start phase. BGP route export remains deferred until `start-kernel`.

Validation before running a large experiment:

```bash
kubectl --kubeconfig kubeconfig.yaml -n "$SEED_NAMESPACE" exec <router-pod> -- \
  sh -lc 'ls -l /etc/bird/conf; grep -n "include" /etc/bird/bird.conf; test -f /etc/bird/conf/kernel.conf && sed -n "1,80p" /etc/bird/conf/kernel.conf || true'
```

If this shows `kernel.conf.disabled-before-start-kernel`, the run used an older
`start-bird` helper and iBGP loopback reachability may be missing until
`start_bird_kernel.sh` or a batch `birdc configure` restores kernel export.

### 2. Load control only happens after each node finishes its serial loop

The lxl logic starts all pods on a node serially, then waits for load after all
nodes finish. There is no mid-node pause when a worker reaches high load.

For B62 4954 scale, each worker can have roughly 800 router-like pods. Starting
hundreds of BIRD processes on one node can push load far above 40 before the
post-start load gate is reached.

This is the main mismatch between the lxl logic and the new larger OVN/OVS
scale. It is not a bug in the code; it is a scaling assumption.

### 3. OVN/OVS likely adds load compared with the macvlan-only run

The previous successful run was under a macvlan setup. In the current setup,
Kube-OVN/OVS is installed as a non-primary CNI and the deployed topology creates
thousands of Kube-OVN Subnet/IP custom resources.

Possible extra costs:

- OVN/OVS datapath and flow setup when many BGP sessions become active.
- Kube-OVN controller work for thousands of Subnet/IP objects.
- Multus + Kube-OVN CNI overhead during deploy and pod sandbox setup.
- More API pressure from Kube-OVN CR watches and status updates.

This does not prove OVN/OVS is the only cause. The BIRD process count and BGP
session churn are still the primary load source. OVN/OVS likely amplifies the
load and makes cleanup failures much more expensive.

### 4. Final `birdc show status` can be slow at 4954 scale

The lxl start-bird Phase 3 checks every target pod serially. At 4954 targets,
even a small per-pod delay can turn into tens of minutes. This protects
correctness but adds thousands of `kubectl exec` calls after BIRD startup.

Risk:

- API server load increases after the heavy BIRD-start phase.
- `BIRD_PHASE_TIMEOUT_SECONDS=1200` may be too small if the cluster is already
  slow.

### 5. start-kernel `export all` can inject a very large route set

`KERNEL_EXPORT_MODE=all` is currently consistent with lxl and with the goal of
testing route installation. It is also the heaviest mode.

Risk:

- Large route tables can trigger high kernel route insertion load.
- `ip route` scale can become the bottleneck after BIRD itself is running.

`device_ospf_only` is available in the helper, but it changes experiment
semantics by not exporting all BGP-learned routes.

### 6. Failure cleanup can be slower than rebuild

The observed bad state was:

- namespace had thousands of Pods and many old objects,
- Kube-OVN had more than ten thousand `ip.kubeovn.io` objects and thousands of
  `subnet.kubeovn.io` objects,
- those Kube-OVN objects were stuck behind
  `kubeovn.io/kube-ovn-controller` finalizers,
- API calls and SSH became slow under high worker load.

In this state, direct `destroyCluster.sh && buildCluster.sh` or VM/libvirt
rebuild is usually faster and cleaner than trying to patch/delete every CR.

## Parameter adjustment options

Keep the current values when the goal is to reproduce the lxl macvlan-proven
workflow exactly. Change parameters only when the next run shows the same load
pattern.

Conservative start-bird options:

| Parameter | Current | Conservative candidate | Tradeoff |
| --- | ---: | ---: | --- |
| `BIRD_START_DELAY_SECONDS` | 0.08 | 0.2, 0.3, or 0.5 | Slower start, less burst per node |
| `BIRD_START_EXEC_TIMEOUT_SECONDS` | 45 | 90 or 120 | Fewer false timeouts under load, slower failure detection |
| `BIRD_PHASE_TIMEOUT_SECONDS` | 1200 | 3600 | Better for 4954 serial status checks, longer wait on real failure |

Design-level start-bird option if load spikes again:

| Option | Suggested value | Tradeoff |
| --- | ---: | --- |
| Add a mid-node load gate every N pods | 50 or 100 | Deviates from lxl, but prevents a worker from starting 800 BIRD processes without pausing |
| Start fewer nodes concurrently | 2 or 3 nodes | Deviates from lxl, reduces cluster-wide burst |
| Use node-local helper for comparison | one SSH command per node | Less API exec pressure, but different execution mechanism |

Conservative start-kernel options:

| Parameter | Current | Conservative candidate | Tradeoff |
| --- | ---: | ---: | --- |
| `KERNEL_SWITCH_DELAY_SECONDS` | 0.3 | 0.5 or 1.0 | Slower route injection, less burst |
| `KERNEL_EXEC_TIMEOUT_SECONDS` | 45 | 90 or 120 | Fewer false timeouts under load |
| `KERNEL_BIRDC_TIMEOUT_SECONDS` | 10 | 20 or 30 | Better for large configs, slower failure detection |
| `KERNEL_EXPORT_MODE` | `all` | `device_ospf_only` only for diagnosis | Reduces route pressure but changes experiment semantics |

## Recommended next-run procedure

1. After deploy, pick several router pods and confirm whether
   `/etc/bird/conf/kernel.conf` exists before `start-bird`.
2. Start with the current exact-lxl start-bird parameters.
3. Watch `runs/<run>/loadAverage.log` and VM-level `uptime` for each worker.
4. If worker load climbs into the hundreds before a node finishes its serial
   loop, stop the stage and use VM-level reset or `destroyCluster.sh` +
   `buildCluster.sh`; do not spend a long time patching Kube-OVN finalizers.
5. For the next attempt after such a failure, use a mid-node load gate or a
   larger `BIRD_START_DELAY_SECONDS`. That is a deliberate scaling change from
   the lxl baseline.

## Useful commands

Check script syntax:

```bash
bash -n start_bird.sh start_bird_kernel.sh
python3 -m py_compile start_bird_helper.py start_bird_kernel_helper.py
```

Check current BIRD/kernel config in one pod:

```bash
kubectl --kubeconfig kubeconfig.yaml -n "$SEED_NAMESPACE" exec <router-pod> -- \
  sh -lc 'pgrep -a bird || true; birdc show status || true; birdc show protocols || true; ls -l /etc/bird/conf'
```

Fast recovery when the cluster is heavily degraded:

```bash
cd /home/lxl/k8s/largeScale/b62_k8s_scale
RUN_DIR="runs/recover_$(date +%Y%m%d_%H%M%S)"
./destroyCluster.sh "$RUN_DIR"
./buildCluster.sh "$RUN_DIR"
```

## 2026-06-11 本轮继续执行记录

本轮接手时，当前 run 目录为：

```bash
/home/lxl/k8s/largeScale/b62_k8s_scale/runs/20260610_235841_4954_w11
```

尝试和判断如下：

- 先确认 `destroyCluster.sh`、`k8sTools destroy` 等清理进程已经不在运行，6 台 K3s VM 仍然存在且 `kubectl get nodes` 显示全部 `Ready`。
- 使用 `clean.sh` 清理当前实验 namespace、SeedEMU workload、event 以及匹配的 Kube-OVN `ip/subnet/vpc` 资源。清理后 namespace 已删除，匹配的 Kube-OVN 残留资源为 0，各 VM 上 `bird` 进程数为 0。
- 因为 VM、K3s、Multus、Kube-OVN、OVS 组件均保持健康，没有直接重建 VM 或重新 build cluster，而是复用已可用的 K3s 环境继续部署。
- 重新执行 `deploy.sh` 后，Kube-OVN subnet 阶段已完成：`processed=4763 ready=4763 error=0 expected=4763`。
- controller deploy 阶段使用当前 `DEPLOY_BATCH_SIZE=40`，共 126 个 batch，最终完成提交；整个 deploy 期间未复现之前 `creating=209` 长时间不动的问题，最后状态约为 `pending=0 creating=9 failed=0 nodes=6/6`。
- 随后执行 `wait-ready.sh`，4954 个 Pod 已全部 `Running` 且 `Ready`。
- 随后执行 `start_bird.sh`。脚本按 node-local/nsenter 方式运行，6 个节点并发、节点内串行，当前参数包括 `BIRD_START_BATCH_SIZE=10`、`BIRD_START_BATCH_COOLDOWN_SECONDS=120`、`BIRD_START_DELAY_SECONDS=0.08`、`BIRD_LOAD_THRESHOLD=50`、`BIRD_LOAD_MAX_WAIT_SECONDS=600`。
- 截至 2026-06-11 09:33 CST，6 个节点均已启动到约 100 个 `bird` 进程，宿主机 `loadAverage.log` 中 start-bird 阶段 load 主要在 20-40 附近波动，没有再次升到 200+。
- 继续观察后，`bird` 进程数增长到约每节点 230 个时，宿主机 load 升到最高约 `115/92/70`，CPU idle 仍有约 60%，K3s 节点也还保持 `Ready`，但这个趋势已经接近上次高负载故障路径。
- 为避免再次冲到 200+ 后导致 Kube-OVN/kubelet 清理困难，已主动中断本次 `start_bird.sh`。
- 中断后发现远端 launcher 已不存在，但已经启动的 `bird` 进程仍留在容器命名空间内。因此只在 6 台 K3s VM 上执行 `pkill -x bird`，没有删除 namespace、Pod 或 Kube-OVN 资源。
- 清理后 6 台 K3s VM 上 `bird_count=0`，`kubectl get nodes` 仍为 6/6 `Ready`，namespace 中 4954 个 Pod 仍为 `Running`。

当前所处阶段：

- `deploy`：已完成。
- `wait-ready`：已完成。
- `start-bird`：已主动停止，未完成；当前已清空 6 台 K3s VM 上的 `bird` 进程。
- `start-kernel`：未开始；在 start-bird 无法稳定跑满之前，不应进入 start-kernel。

当前风险和需要继续观察的点：

- 当前 start-bird 速率已经偏保守，每个节点每 10 个 Pod 后冷却 120 秒，但宿主机 load 仍随已运行 BIRD 总数升高。因此这次问题不只是“启动瞬间并发过大”，还可能是 OVN/OVS CNI 下大量 BIRD 同时运行带来的持续负载。
- 保守节流可以避免一开始快速冲高，但它和 `/home/lxl/k8s/lxl` 旧 macvlan 流程相比多了中途批次冷却；即使如此，4954 规模在当前 OVN/OVS 环境下仍有明显负载风险。
- 如果后续 BIRD 进程数长时间不增长，同时 VM 负载不高，需要重点排查 node-local helper 是否卡在某个具体 Pod 的 `nsenter`/`crictl` 操作。
- 如果 VM 负载重新升到很高并导致 Kube-OVN 或 kubelet 异常，优先尝试 `virsh destroy/start` 恢复 VM 和 K3s 服务；若 namespace/Kube-OVN CR 已严重堆积，再执行 `destroyCluster.sh` 和 `buildCluster.sh` 会更干净。
- 下一次不建议直接按当前参数继续跑满 4954。更合理的下一步是先做分段上限测试，例如每节点 100、200、300 个 BIRD 时分别观察宿主机 load 是否稳定；如果 load 随 BIRD 总数近似线性增长，那么单纯调小启动并发无法解决最终 4954 全量运行的问题，需要考虑回到 macvlan、降低规模、增加物理资源/分散宿主机，或重新评估 OVN/OVS 数据面开销。

## 2026-06-11 start-bird 0.5s 间隔测试

本次按新的假设调整 `start_bird.sh`：

- 将 `BIRD_START_DELAY_SECONDS` 从 `0.08` 改为 `0.5`，即同一 node 内每个 Pod 启动 BIRD 后等待 0.5 秒。
- 保持 `BIRD_START_BATCH_SIZE=10` 和 `BIRD_START_BATCH_COOLDOWN_SECONDS=120` 不变。
- 测试前确认 6 个 K3s node 均为 `Ready`，namespace 中 4954 个 Pod 均为 `Running`，6 个 node 上 `bird_count=0`。
- 目的：区分之前 load 升高是否主要由单 node 内启动间隔太短造成，还是由大量 BIRD 进程在 OVN/OVS 环境下持续运行造成。

测试观察：

- 每节点约 70 个 BIRD 时，宿主机 load 大约在 28 左右，表现明显平滑。
- 每节点约 100 个 BIRD 时，宿主机 load 上升到约 `47/35/30`。
- 每节点约 120 个 BIRD 时，宿主机 load 上升到约 `58-60`，随后一度到 `82/54/39`。
- 每节点约 140 个 BIRD 时，宿主机 load 大约在 `72/60/44`。
- 每节点约 170-180 个 BIRD 时，宿主机 load 达到约 `85-88/75-77/56`，K3s node 仍为 6/6 `Ready`，Pod 仍为 4954 `Running`。
- 为避免继续抬升到 100+ 后再次进入难清理状态，主动中断本轮 `start_bird.sh`，随后杀掉残留远端 launcher 和所有 node 上的 `bird` 进程。
- 清理后 6 个 node 均为 `bird_count=0`、launcher=0，namespace 和 Pod 未删除。

阶段结论：

- `BIRD_START_DELAY_SECONDS=0.5` 相比 `0.08` 有明显改善：早期 70-120 个/节点阶段更平滑，没有立刻复现 100+ load。
- 但它没有完全解决问题：到 170-180 个/节点时，宿主机 load 仍然进入 80+，5 分钟 load 也持续抬升。
- 因此问题不是单纯的启动瞬间间隔太短；大量 BIRD 在当前 OVN/OVS 环境中持续运行本身也会带来明显宿主机负载。
- 下一轮如果继续测试，建议保持 `0.5s`，同时把单轮测试目标显式限制在每节点 200 或 300，并记录稳定后的 load；不要直接跑满 826/节点。

## 2026-06-11 BIRD 配置修改后完整 start-bird 结果

本轮 run 目录：

```bash
/home/lxl/k8s/largeScale/b62_k8s_scale/runs/20260611_135218_4954_w11_birdcfg
```

执行流程：

- 先用 `clean.sh` 清理旧 namespace 和匹配的 Kube-OVN 资源，清理耗时约 511 秒。
- 重新执行 `compile.sh`、`build.sh`、`deploy.sh`、`wait-ready.sh`。
- `wait-ready.sh` 结果为 4954 个 Pod 全部 `Running` 且 `Ready`。
- `start_bird.sh` 参数保持：`BIRD_START_DELAY_SECONDS=0.5`、`BIRD_START_BATCH_SIZE=10`、`BIRD_START_BATCH_COOLDOWN_SECONDS=120`、`BIRD_LOAD_THRESHOLD=50`、`BIRD_LOAD_MAX_WAIT_SECONDS=600`。
- 本轮生成的 BIRD 配置中已包含用户修改后的较长 BGP timer，例如 `hold time 36000`、`keepalive time 60`。

start-bird 结果：

- `start_bird.sh` 最终完成，状态 `PASS`。
- 总目标 `targets=4954 expected=4954 started=4954 skipped=0`。
- 各节点结果：
  - `seedemu-b62-master-w5`: `826/826`，失败 0。
  - `seedemu-b62-worker1-w5`: `826/826`，失败 0。
  - `seedemu-b62-worker2-w5`: `826/826`，失败 0。
  - `seedemu-b62-worker3-w5`: `826/826`，失败 0。
  - `seedemu-b62-worker4-w5`: `825/825`，失败 0。
  - `seedemu-b62-worker5-w5`: `825/825`，失败 0。
- `start_bird` 总耗时约 `12364.88s`，约 3 小时 26 分钟。

负载观察：

- 宿主机 `loadAverage.log` 中，`start_bird` 阶段最高 1 分钟 load 约 `190.90`，最高 5 分钟 load 约 `177.23`，最高 15 分钟 load 约 `171.76`。
- 采样到的最低 CPU idle 约 `37.1%`，说明负载很高，但没有出现 CPU 完全打满或集群失联。
- 约每节点 500-600 个 BIRD 以后，宿主机 load 进入 100+ 高平台；约每节点 700+ 以后，部分节点 1 分钟 load 短暂超过 `BIRD_LOAD_THRESHOLD=50`。
- 超过阈值的节点包括 master、worker1、worker4、worker5 等，但这些短峰都能回落，脚本的 `load < 50` 等待保护有效。
- 最尾段 master 在 `820/826` 附近被 load 阈值拖慢，反复等待回落后才补完最后几个 Pod。

本轮结论：

- 用户修改后的 BIRD 配置加上 `0.5s` 单节点启动间隔、`10` 个一批和 `120s` cooldown，可以在当前 OVN/OVS 环境下把 4954 规模的 `start-bird` 跑完。
- 这轮没有复现之前的 start-bird 卡死或集群崩溃；但宿主机负载仍然很高，说明 OVN/OVS 下全量 BIRD 的稳态开销仍不可忽略。
- 当前 `BIRD_LOAD_THRESHOLD=50` 对尾段比较保守，能保护节点但会显著拉长最后几十个 Pod 的启动时间。后续如果要缩短时间，可以考虑只小幅上调阈值，例如 60，但需要接受更高宿主机 load 风险。

## 2026-06-12 BGP 协议异常排查记录

用户观察到 `as1850brd-r12-1.12.7.58-745bdc5949-69jxl` 中存在
`Ebgp_p_as1824 Active Socket: No route to host` 以及多个 `Ibgp_to_cli_*`
处于 `Passive`。

本轮非破坏性排查结论：

- `test.py` 已补成 BGP 层测试脚本。它会每个 AS 固定选一个
  `brd-r<N>` 中 `N` 最小的 brd Pod，执行 `birdc show protocols` 和
  `birdc show route count`，并把目标和结果写入
  `bgp_test_targets.json`、`bgp_test_summary.json`。
- 当前命令既支持旧格式
  `python3 ./test.py <namespace> <run_dir> --kubeconfig ./kubeconfig.yaml`，
  也支持阶段式调用
  `python3 ./test.py <run_dir> --kubeconfig ./kubeconfig.yaml`。
- AS1850 到 AS1824 的内核路由存在：
  `1.12.7.32 dev ix12 src 1.12.7.58`；AS1824 到 AS1850 也存在：
  `1.12.7.58 dev ix12 src 1.12.7.32`。
- 但是 AS1850 到 AS1824 的 TCP 高端口临时测试失败，反向也失败；
  因此 `Ebgp_p_as1824` 不是单纯的 BIRD 协议参数问题，而是这对 Pod 在
  `ix12` 数据面上无法互通。
- 对照测试中，AS1850 可以连接一个已 Established 的 EBGP 邻居
  `1.12.5.2:179`，说明 AS1850 的 `ix12` 和 BIRD 监听不是全局不可用。
- 当前 `net-ix-ix12` 的 NetworkAttachmentDefinition 实际是
  `type: macvlan`、`master: ens2`、`mode: bridge`，Kube-OVN 只作为 IPAM；
  对应 Subnet 状态是 `SetNonOvnSubnetSuccess`。这不是 Kube-OVN overlay
  转发链路。
- 两个 Pod 的 `ix12` 接口均显示为 `macvlan mode bridge`，且 RX
  `errors/dropped` 数量很高，达到几十万到百万级。这比 OSPF/BGP timer
  更像当前 EBGP 邻接失败的直接方向。
- 当前已运行 Pod 里仍存在
  `/etc/bird/conf/kernel.conf.disabled-before-start-kernel`，说明本轮运行时
  `start-bird` 实际没有加载用户保留的 kernel protocol。这个行为已修正：
  后续 `start-bird` 会保留或恢复 `kernel.conf`，使 OSPF/直连路由能进入
  Linux kernel。对当前这一轮已启动的 BIRD，需要执行 `start_bird_kernel.sh`
  或批量 `birdc configure` 才能补上内核路由。

暂不建议修改 `ospf tick/hello/dead` 来处理 `Ebgp_p_as1824`，因为该异常发生在
同一个 IX 的直连邻居 TCP 建连之前，OSPF timer 不会修复直连 `ix12`
数据面不通的问题。下一步更应该确认是否需要把
`assignment.yaml` 中的 `networking.cniType/localLinkCniType/attachedCniType`
从 `macvlan` 改成真正的 `kube-ovn` attached network，并重新 compile/deploy；
如果继续使用 macvlan，则需要重点排查 KVM 节点 `ens2` 二层承载、macvlan
跨 VM 转发、以及接口丢包原因。

## 2026-06-12 2599 规模 kernel.conf 保留修复与 start-bird 复测

用户明确要求 `start-bird` 不能移走 `/etc/bird/conf/kernel.conf`。本轮修复：

- `start_bird_node_local.py` 不再把 `kernel.conf` 移到
  `kernel.conf.disabled-before-start-kernel`。
- 如果旧镜像或旧 Pod 中只剩
  `kernel.conf.disabled-before-start-kernel`，脚本只会把它复制回
  `kernel.conf` 作为兼容兜底，不会删除或移动原文件。
- 当 `bird.conf` 已包含 `include "/etc/bird/conf/*.conf";` 时，脚本会删除显式
  `include "/etc/bird/conf/kernel.conf";`，避免同一份 kernel 配置被重复加载并触发
  `Kernel syncer already attached to table master4`。
- `start_bird.sh` 参数按 `/home/lxl/k8s/lxl/test` 的主逻辑调回：
  所有节点并发、节点内串行、单 Pod 间隔 `0.5s`、启动完成后等待节点 load
  低于 `40`；取消此前额外的 `10` 个一批、`120s` 强制 cooldown。

执行与验证：

- 已先用 `clean.sh` 清理旧 `seedemu-b62-4954` namespace 及匹配 Kube-OVN 资源。
- `assignment.yaml` 已切到 `topologySize: 2599` 和
  `namespace: seedemu-b62-2599`。
- 已完成 `preflight.sh`、`compile.sh`、`build.sh`、`deploy.sh`、`wait-ready.sh`。
- `wait-ready.sh` 显示 `2599/2599` Pod 均为 `Running` 且 `Ready`。
- 修正参数后的 `start_bird.sh` 完成，状态 `PASS`：
  `targets=2599 expected=2599 started=2239 skipped=360 bird_processes=2599`。
  其中 `skipped=360` 是因为中途发现旧 cooldown 策略不符合 lxl/test 逻辑后中止重跑，
  前一次已启动的 BIRD 被新脚本正确跳过。
- 各节点结果均 `rc=0`，无 `failed`，无 `missing_pid`。
- 修正后的 start-bird 耗时约 `225.06s`，显著快于此前强制 cooldown 策略。
- 宿主机 load 在启动后段短时升到约 `64`，但各节点结束时本地 load 已降到
  `8.61` 到 `14.15` 范围，未出现 load 失控或集群失联。

Pod 内抽样结果：

- IX 类 brd Pod 和真实 `brd-r*` Pod 中 `/etc/bird/conf/kernel.conf` 均存在。
- `/etc/bird/conf/kernel.conf.disabled-before-start-kernel` 不存在，说明新脚本没有再移动
  `kernel.conf`。
- `bird.conf` 只保留 glob include，例如
  `include "/etc/bird/conf/*.conf";`，没有重复 include kernel 配置。
- 抽样 `brd-r*` Pod 中 `kernel1` 为 `up`，`ospf1` 为 `Running`，
  多个 EBGP/IBGP 协议为 `Established`。

## 2026-06-12 2599 规模 start-kernel 复测

执行 `start_bird_kernel.sh` 时先发现当前脚本仍带有旧的保守节流：

- `KERNEL_SWITCH_BATCH_SIZE=10`
- `KERNEL_SWITCH_BATCH_COOLDOWN_SECONDS=120`
- `KERNEL_LOAD_THRESHOLD=50`
- 每个 Pod 前做 load gate

这不符合 `/home/lxl/k8s/lxl/test/start_bird_kernel_helper.py` 的主流程。旧参数运行约
2 分钟后每节点只切换了 10 个 Pod，预计完整 2599 规模会接近 1.5 小时，因此已中止
这次旧参数运行。

随后修正为 lxl/test 风格：

- 所有节点并发。
- 节点内串行。
- 单 Pod 间隔 `0.3s`。
- 不再做 `10` 个一批和 `120s` 强制 cooldown。
- 节点内全部 Pod 切换完成后，再等待本节点 load 低于 `40`。
- `birdc` timeout 使用 `10s`，单 Pod nsenter timeout 使用 `45s`。
- `KERNEL_EXPORT_MODE=all`，`scan_base=6000`，`scan_jitter=120`。

修正后结果：

- `start_bird_kernel.sh` 完成，状态 `PASS`。
- 总目标 `targets=2599 expected=2599 switched=2599`。
- 各节点均 `rc=0`，`failed=0`，`missing_pid=0`。
- 总耗时约 `189.16s`。
- 各节点结束时本地 load 在约 `14.91` 到 `25.25` 之间，均低于阈值 `40`。
- 宿主机 `loadAverage.log` 中 start-kernel 阶段 load 仍在约 `90-115` 区间，
  但 CPU idle 约 `60%+`，未出现集群失联。

验证结果：

- 抽样 `brd-r*` Pod 中 `/etc/bird/conf/kernel.conf` 已变为 `export all`，
  scan time 约 `6000 + jitter`，`kernel1` 为 `up`。
- 抽样 `brd-r*` Pod 中 Linux FIB 路由数已大于 BIRD network 数，例如
  `as1269brd-r10` 为 `ip_route_count=1928`、`bird_networks=1745`。
- 执行 `test.sh` 生成 `test.json`，脚本结果为 `PASS: 10/10`。
- 额外手动选取 10 个不同 AS 的真实 `brd-r*` Pod 检查，
  全部满足 `ip route | wc -l > birdc show route count` 中的 network 数。
