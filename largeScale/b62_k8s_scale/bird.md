# B62 start-bird / start-kernel audit

Updated: 2026-06-10

This document records the current B62 BIRD stage behavior, how it compares with
`/home/lxl/k8s/lxl`, and the risks to watch when running the 4954-scale
experiment on the KVM + K3s + Multus + Kube-OVN/OVS environment.

## Current conclusion

- `start_bird.sh` now follows the main logic of
  `/home/lxl/k8s/lxl/start-bird` and
  `/home/lxl/k8s/lxl/seed_k8s_start_bird0130.py`.
- `start_bird.sh` no longer removes `/etc/bird/conf/kernel.conf` and no longer
  edits kernel protocol includes. This was changed back to match the lxl
  reference logic.
- `start_bird_kernel.sh` follows the same node-aware execution model as
  `/home/lxl/k8s/lxl/start-kernel`, but it has a few B62 compatibility checks
  around BIRD include paths and a stricter Kernel-protocol-up check.
- Both stages use `kubectl exec`, run all Kubernetes nodes concurrently, and run
  pods serially inside each node.
- The current design intentionally matches the previous macvlan-proven
  workflow first. If OVN/OVS load is still too high, the next change should be
  an explicit throttling design change, not an accidental hidden behavior.

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

### 1. `kernel.conf` may start too early

Current `start-bird` now matches lxl and does not remove `kernel.conf`. This is
correct for matching the reference workflow, but it means the image must not
already contain an active `/etc/bird/conf/kernel.conf` before `start-kernel`.

Why this matters:

- The fast image archive logic treats `etc/bird/conf/kernel.conf` as part of
  the generated image content.
- If compiled images already contain `kernel.conf` and `bird.conf` includes
  `/etc/bird/conf/*.conf`, BIRD will load the kernel protocol during
  `start-bird`.
- That would move kernel route injection into the BIRD-start phase and can
  explain unexpectedly high load during start-bird.

Validation before running a large experiment:

```bash
kubectl --kubeconfig kubeconfig.yaml -n "$SEED_NAMESPACE" exec <router-pod> -- \
  sh -lc 'ls -l /etc/bird/conf; grep -n "include" /etc/bird/bird.conf; test -f /etc/bird/conf/kernel.conf && sed -n "1,80p" /etc/bird/conf/kernel.conf || true'
```

If this shows an active `kernel.conf` before `start-kernel`, decide explicitly:

- keep exact lxl behavior and accept early kernel protocol, or
- restore a start-bird safeguard that removes/empties `kernel.conf`, which
  would no longer be exact lxl behavior.

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
