# B62 Large-Scale Problem Log

This file records the failure modes seen during B62 large-scale K3s/Kube-OVN
experiments and the related issues summarized in `../../lxl/README.md`.

## 2026-06-10: Deploy Stuck In `ContainerCreating`

Stage: `deploy`, before start-bird/start-kernel.

Observed symptoms:

- `deploy.sh` stopped at a pressure gate similar to:
  `[pressure:pre-batch-34] total=1250 pending=0 creating=209 running_notready=0 failed=0 succeeded=0 nodes=6/6`.
- The 209 stuck workload Pods were scheduled on `seedemu-b62-master-w5`.
- Pod events showed Multus/Kube-OVN CNI ADD failure because
  `/run/openvswitch/kube-ovn-daemon.sock` did not exist.
- The master `kube-ovn-cni` Pod was `Unknown` or `CrashLoopBackOff`.
- Its init logs included:
  `cp: not writing through dangling symlink '/opt/cni/bin/loopback'`.

Conclusion:

- This was not a BIRD startup failure.
- The immediate blocker was the master-side Kube-OVN CNI daemon not becoming
  healthy, so workload Pods could not get their secondary network interfaces.
- Large deploy concurrency can increase pressure, but it was not the direct
  error. The direct error was a broken CNI plugin path on the node.

Fix/prevention:

- `seedemu/k8sTools/resources/setup/ovn/installKubeOvnFabric.py` now sanitizes
  both the configured K3s CNI bin directory and `/opt/cni/bin` before installing
  Kube-OVN, removing only known Kube-OVN overwrite targets:
  `loopback`, `portmap`, and `kube-ovn`.
- `deploy.sh` creates workload controllers in batches of 40 and Kube-OVN
  Subnets in independent batches to reduce pressure while the controller
  converges.

Validation commands:

```bash
kubectl --kubeconfig kubeconfig.yaml -n kube-system get pods -o wide | \
  grep -E 'kube-ovn-cni|ovs-ovn|kube-ovn-controller|ovn-central'
kubectl --kubeconfig kubeconfig.yaml -n kube-system logs ds/kube-ovn-cni -c install-cni --tail=100
kubectl --kubeconfig kubeconfig.yaml -n <workload-namespace> get pods -o wide | \
  awk '$3=="ContainerCreating" {print $1, $7}'
```

## 2026-06-10: Rebuild Failed Because `node-token` Was Missing

Stage: `build-cluster`, inside `k8sTools.py build` ->
`applyK3sCluster.py` -> Ansible `Install K3s Master`.

Observed symptoms:

- Ansible failed at `Get K3s token`:
  `cat: /var/lib/rancher/k3s/server/node-token: No such file or directory`.
- On the master VM after failure, `/usr/local/bin/k3s`,
  `/etc/rancher/k3s`, and `/var/lib/rancher/k3s` were gone.
- The master journal showed K3s had started successfully and then was stopped.
- The older destroy log showed a previous `destroyCluster.sh` had continued
  running K3s uninstall after the new build started.

Root cause:

- A stale destroy process survived a timed-out/interrupted cleanup.
- That old process ran `k3s-uninstall.sh` against the same VM while the new
  build was installing K3s.
- Therefore the token was missing because K3s had just been uninstalled by the
  overlapping cleanup, not because normal `k8sTools.py build` cannot create it.

Why earlier builds did not hit it:

- Earlier `k8sTools.py build` runs were not racing a still-running destroy.
- When build runs alone, K3s creates `/var/lib/rancher/k3s/server/node-token`
  as part of normal server initialization.

Fix/prevention:

- `buildCluster.sh` and `destroyCluster.sh` now use the same lifecycle lock
  `.cluster-lifecycle.lock`.
- Both scripts track their `k8sTools.py` child process and terminate descendant
  processes on interrupt, so an interrupted destroy should not keep uninstalling
  K3s in the background.
- `buildCluster.sh` refuses to start if it sees surviving
  `destroyPhysicalCluster.py`, `cleanKubeOvnFabric.py`, `helm uninstall
  kube-ovn`, or `seedemu-k8s-tools-destroy` processes.
- `ansible/k3s-install.yml` now explicitly waits for K3s service health and
  `node-token` existence before it tries to fetch the token.

Validation commands:

```bash
ps -eo pid,ppid,stat,etime,cmd | \
  grep -E 'destroyPhysicalCluster.py|cleanKubeOvnFabric.py|helm uninstall kube-ovn|seedemu-k8s-tools-destroy' | \
  grep -v grep
ssh -i ~/.ssh/id_ed25519 ubuntu@192.168.126.130 \
  'sudo -n systemctl is-active k3s && sudo -n test -s /var/lib/rancher/k3s/server/node-token'
```

## Related Issues From `../../lxl/README.md`

### Multus kubeconfig compatibility path

Symptom:

- Workload Pods stay in `ContainerCreating`.
- Events include:
  `plugin type="multus" failed (add): stat /etc/cni/net.d/multus.d/multus.kubeconfig: no such file or directory`.

Root cause:

- K3s stores Multus kubeconfig under
  `/var/lib/rancher/k3s/agent/etc/cni/net.d/multus.d`, while some Multus paths
  still read `/etc/cni/net.d/multus.d`.

Expected invariant:

```text
/etc/cni/net.d/multus.d -> /var/lib/rancher/k3s/agent/etc/cni/net.d/multus.d
```

### K3s API remains slow after failed large deploy

Symptom:

- Namespace cleanup is slow or stuck.
- `k3s` stays in `activating (start)`.
- `/readyz` reports failures such as `etcd`, `informer-sync`, or RBAC
  post-start hooks.
- `journalctl -u k3s` shows `Slow SQL` and handler timeouts.

Root cause:

- The K3s sqlite/kine datastore can be dominated by historical rows for the
  failed experiment namespace, especially after many `ContainerCreating`,
  `SandboxChanged`, and event updates.

Recovery direction:

- Prefer normal namespace/resource cleanup first.
- If the API server cannot recover, inspect and compact/clean the master
  `state.db` only with a backup and a namespace-targeted plan.

### VM paused by host disk exhaustion

Symptom:

- Libvirt VM enters `paused (I/O error)`.
- Multiple VMs may pause together.

Root cause:

- The host filesystem backing VM disks filled up.

Validation:

```bash
df -h
virsh -c qemu:///system list --all
```

### `maxPods` and PodCIDR are not the real network ceiling

Symptom:

- Even with `maxPods=4000` and `/20` PodCIDR, Pods can stick near roughly
  1000 Pods per node.
- Events include Multus/flannel failures and sometimes:
  `failed to connect "veth..." to bridge cni0: exchange full`.

Root cause:

- The practical limit may be `cni0`, bridge FDB/hash pressure, veth count,
  flannel churn, or Multus secondary-interface load rather than scheduler
  `maxPods`.

Validation:

```bash
kubectl --kubeconfig kubeconfig.yaml get nodes -o wide
kubectl --kubeconfig kubeconfig.yaml get nodes \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.podCIDR}{"\n"}{end}'
ssh <node> 'cat /sys/class/net/cni0/bridge/hash_max; bridge fdb show br cni0 | wc -l; ip link show type veth | wc -l'
```

### Very large deploys can drag down the control plane

Symptom:

- Deploy or wait-ready stalls with most Pods Running but not all Ready.
- Later `kubectl` calls time out against the API server.
- Some workers may become `NotReady`.

Likely cause:

- Large-scale Pod state churn and event volume overload the control plane and
  then leave a large namespace-specific backlog in the K3s datastore.

Recovery direction:

- Collect node readiness, Pod distribution, CNI/FDB/veth counters, K3s logs,
  and datastore size before deleting evidence.
- Use normal cleanup first; use datastore surgery only as last resort.

### Image cache reuse can hide generated file changes

Symptom:

- A Pod appears to have a file only after a later script writes it, while the
  original image did not contain it.
- BIRD startup can fail if a stale image is reused after the compile output
  gained new BIRD config files.

Prevention:

- Rebuild or regenerate the fast image archive whenever compile output changes.
- Do not skip existing images by default for scale changes where generated
  router/BIRD config can differ.

### Internal `net_*` prefixes may not be exported by eBGP

Symptom:

- A `net_*` prefix is visible inside one AS but missing from another AS.
- `birdc show route for <prefix>` works internally but returns `Network not
  found` elsewhere.

Root cause:

- eBGP export policy depends on route communities. Internal prefixes learned
  through OSPF/iBGP may not carry the `LOCAL_COMM` required by the export
  filter.

Debug direction:

- Check route source and communities with `birdc show route for <prefix> all`.

### Start-BIRD overload can leave workers and the API server unrecoverable

Observed on 2026-06-10 during the B62 4954-node run.

Symptom:

- `start_bird.sh` appears stuck after some node summaries, for example
  `started=548/826`, `started=568/826`.
- Worker VM load averages rise to hundreds, for example 200-570.
- Each affected worker still has hundreds of `bird` processes even after the
  experiment namespace is reported absent.
- `kubectl` calls time out with `context deadline exceeded` or
  `Client.Timeout exceeded while awaiting headers`.
- `clean.sh` can block while querying Kube-OVN CRs such as
  `vpc.kubeovn.io`, because the API server is too slow to answer.
- SSH to the master or overloaded workers may time out.

Root cause:

- Large-scale BIRD startup left many live BIRD processes inside workload
  containers. The remaining processes kept worker load very high, which slowed
  kubelet/containerd and then degraded the K3s API server. At that point,
  Kubernetes-level cleanup is too slow and may not be the fastest recovery
  path.

Fast recovery protocol:

```bash
cd /home/lxl/k8s/largeScale/b62_k8s_scale

# Stop any active stage first.
ps -eo pid,pgid,ppid,stat,etime,cmd | grep -E 'start_bird|clean.sh|kubectl exec' | grep -v grep
kill -TERM -<pgid>
sleep 5
kill -KILL -<pgid>

# Prefer VM-level cold restart when API/SSH are already timing out.
for vm in seedemu-b62-master-w5 seedemu-b62-worker1-w5 seedemu-b62-worker2-w5 \
          seedemu-b62-worker3-w5 seedemu-b62-worker4-w5 seedemu-b62-worker5-w5; do
  virsh -c qemu:///system destroy "$vm" || true
done

for vm in seedemu-b62-master-w5 seedemu-b62-worker1-w5 seedemu-b62-worker2-w5 \
          seedemu-b62-worker3-w5 seedemu-b62-worker4-w5 seedemu-b62-worker5-w5; do
  virsh -c qemu:///system start "$vm"
done

# After SSH is back, verify and restart services if needed.
kubectl --kubeconfig kubeconfig.yaml get nodes -o wide
kubectl --kubeconfig kubeconfig.yaml -n kube-system get pods -o wide
```

Preferred reset path for a fully degraded cluster:

```bash
cd /home/lxl/k8s/largeScale/b62_k8s_scale
RUN_DIR="runs/recover_$(date +%Y%m%d_%H%M%S)"
./destroyCluster.sh "$RUN_DIR"
./buildCluster.sh "$RUN_DIR"
```

This is usually faster than namespace cleanup when the namespace still has
thousands of Pods plus more than ten thousand Kube-OVN `ip.kubeovn.io` and
`subnet.kubeovn.io` objects stuck behind
`kubeovn.io/kube-ovn-controller` finalizers. In that state, API-level cleanup
can spend a long time patching or deleting CRs; cluster rebuild avoids that
backlog and returns the environment to a known baseline.

Post-recovery checks:

```bash
kubectl --kubeconfig kubeconfig.yaml get ns seedemu-b62-4954
kubectl --kubeconfig kubeconfig.yaml -n kube-system rollout status ds/kube-multus-ds --timeout=180s
kubectl --kubeconfig kubeconfig.yaml -n kube-system rollout status ds/kube-ovn-cni --timeout=180s
kubectl --kubeconfig kubeconfig.yaml -n kube-system rollout status ds/ovs-ovn --timeout=180s
kubectl --kubeconfig kubeconfig.yaml -n kube-system rollout status deploy/kube-ovn-controller --timeout=180s
kubectl --kubeconfig kubeconfig.yaml -n kube-system rollout status deploy/ovn-central --timeout=180s
```

Notes:

- Do not spend a long time debugging API CR query timeouts once worker load is
  already hundreds and SSH is unstable. Use VM-level restart first.
- `virsh destroy` powers off the VM; it does not delete the VM disk or the K3s
  cluster files.
- After the VM-level restart, rerun `clean.sh <RUN_DIR>` only if the namespace
  or Kube-OVN resources still remain.
