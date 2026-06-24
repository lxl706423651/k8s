# B63 Large-Scale Placement Experiment

## 常用 Kubernetes 查看与调试命令

以下命令默认在 `largeScale/b63_deployment` 目录执行，并使用 b63 生成的
`./kubeconfig-b63.yaml`。如果你的实验
namespace 不是 `seedemu-b63-1078`，把命令中的 namespace 替换成
`summary.json` 里记录的 `namespace`。

查看 b63 集群节点：

```bash
kubectl --kubeconfig ./kubeconfig-b63.yaml get nodes -o wide
```

查看某个实验 namespace 下所有 Pod：

```bash
kubectl --kubeconfig ./kubeconfig-b63.yaml \
  -n seedemu-b63-1078 get pods -o wide
```

按状态统计 Pod 数量：

```bash
kubectl --kubeconfig ./kubeconfig-b63.yaml \
  -n seedemu-b63-1078 get pods --no-headers \
  | awk '{count[$3]++} END {for (status in count) print status, count[status]}'
```

持续观察 Pod 状态变化：

```bash
watch -n 5 'kubectl --kubeconfig ./kubeconfig-b63.yaml -n seedemu-b63-1078 get pods -o wide'
```

查看某个 Pod 的详细事件和调度信息：

```bash
kubectl --kubeconfig ./kubeconfig-b63.yaml \
  -n seedemu-b63-1078 describe pod <pod-name>
```

进入某个 Pod：

```bash
kubectl --kubeconfig ./kubeconfig-b63.yaml \
  -n seedemu-b63-1078 exec -it <pod-name> -- bash
```

如果镜像内没有 `bash`，使用 `sh`：

```bash
kubectl --kubeconfig ./kubeconfig-b63.yaml \
  -n seedemu-b63-1078 exec -it <pod-name> -- sh
```

在路由器/BRD Pod 内查看 BIRD 协议状态：

```bash
kubectl --kubeconfig ./kubeconfig-b63.yaml \
  -n seedemu-b63-1078 exec <pod-name> -- birdc show protocols
```

查看 Pod 日志：

```bash
kubectl --kubeconfig ./kubeconfig-b63.yaml \
  -n seedemu-b63-1078 logs <pod-name>
```

查看 namespace 事件，排查 CNI、调度、镜像拉取等问题：

```bash
kubectl --kubeconfig ./kubeconfig-b63.yaml \
  -n seedemu-b63-1078 get events --sort-by=.lastTimestamp
```

查看节点资源使用情况，要求集群已安装 metrics-server：

```bash
kubectl --kubeconfig ./kubeconfig-b63.yaml top nodes
kubectl --kubeconfig ./kubeconfig-b63.yaml \
  -n seedemu-b63-1078 top pods
```

This directory contains the b63-owned large-scale Kubernetes experiment flow.
It builds its own KVM/K3s/Kube-OVN cluster from `configkvm_b63.yaml`, compiles a
real topology, optimizes workload placement, deploys the manifest, starts BIRD,
enables kernel export, then runs a BGP protocol-level test.

The workflow does not reuse another example's generated cluster files. All
runtime stage scripts live under `running/`.

## Files

| Path | Role |
| --- | --- |
| `configkvm_b63.yaml` | User-edited b63 KVM/K3s/Kube-OVN input for cluster construction. |
| `k8sTools.py` | Thin local wrapper around `seedemu.k8sTools.K8sTools`. |
| `runB63LargeExperiment.py` | Full b63 orchestrator: cluster build, running stages, timing summary, dynamic report append. |
| `runPlacementExperiment.py` | Placement engine: network weights, pod resource estimates, algorithms, baselines, static report. |
| `running/*.sh` | b63 running stages: clean, compile, placement, preflight, build, deploy, wait-ready, start BIRD, and start kernel export. |
| `running/start_bird.sh` | Starts BIRD in router-like pods; tuning constants are defined at the top of the script. |
| `running/start_bird_kernel.sh` | Enables BIRD kernel export in router-like pods; tuning constants are defined at the top of the script. |
| `running/test.py` | Manual post-start BGP protocol test; not part of the current full experiment flow. |
Generated files such as `configK3s-b63.yaml`, `kubeconfig-b63.yaml`,
`inventory-b63.yaml`, and experiment run directories are intentionally not
stored in the cleaned example tree.

## Full Workflow

Run from `largeScale/b63_deployment`:

```bash
python3 ./runB63LargeExperiment.py \
  --topology-size 1897 \
  --kvm-config ./configkvm_b63.yaml
```

By default the run directory is:

```text
./experiments/<scale>_<YYYYmmdd_HHMMSS>/
```

The orchestrator runs these stages:

```text
build-cluster
clean
compile
placement
preflight
build
deploy
wait-ready
start-bird
start-kernel
# test-bgp is currently disabled in runB63LargeExperiment.py
```

Every running stage receives explicit command-line parameters such as
`--experiment-dir`, `--kubeconfig`, `--inventory`, `--namespace`,
`--topology-size`, and the stage-specific placement/build/deploy knobs. The
start-BIRD and start-kernel knobs are kept directly in their scripts.
The stage commands recorded in `summary.json` are therefore reproducible
without relying on hidden exported variables from the orchestrator.

Stage outputs are written into the run directory. Important files include:

```text
summary.json
output/k8s.raw.yaml
output/k8s.source.yaml
output/k8s.yaml
output/network_weights.yaml
output/placement_report.json
bgp_test_targets.json
bgp_test_summary.json
*.log
```

`summary.json` records each stage's status, command, log path, start/end time,
and duration. It also includes the BGP test result:

```json
{
  "bgpTest": {
    "pass": true,
    "failedBrdnodes": [],
    "failedProtocols": []
  }
}
```

`placement_report.json` records the selected placement, five baselines, three
network cut metrics, static validation, and dynamic node/overlay metrics after
deployment.

## Cluster Construction

The cluster stage calls:

```bash
python3 ./k8sTools.py build \
  --input configkvm_b63.yaml \
  --config-k3s configK3s-b63.yaml \
  --kubeconfig kubeconfig-b63.yaml \
  --inventory inventory-b63.yaml
```

Use `--skip-cluster-build` only when those generated files already exist and
point to the intended b63 cluster. Use `--destroy-existing-cluster` when the
recorded b63 cluster should be destroyed before rebuilding. The
`--destroy-after-pass` flag is retained for compatibility, but that stage is
currently disabled in the main flow.

## Placement Stage

`running/compile.sh` writes an unplaced manifest to `output/k8s.raw.yaml`.
`running/placement.sh` then calls `runPlacementExperiment.py`, generating:

```text
output/network_weights.yaml
output/placement_report.json
output/k8s.yaml
```

The final `output/k8s.yaml` contains the selected placement as
`nodeSelector["kubernetes.io/hostname"]`, which allows `running/build.sh` to
generate per-node image preload lists before deploy.

For small test clusters, pass `--skip-resource-request-injection` to keep
static CPU/memory estimates in `placement_report.json` without writing those
estimated requests into the Kubernetes manifest.

Supported placement algorithms:

- `optimized`
- `network-only`
- `pod-count`
- `pod-count-balanced`
- `resource-only`
- `hypergraph`
- `kubernetes-default-scheduler`

Supported network objective modes:

- `ratio`
- `pair`
- `endpoint-exposure`

## BGP Test

After `start-kernel`, the orchestrator runs:

```bash
python3 running/test.py <namespace> <run-dir>
```

The test selects one BRD pod per AS by choosing the `brd-r<N>` pod with the
smallest `N`, runs `birdc show protocols`, and fails if a BGP protocol is
missing or not healthy. Failures are recorded in `bgp_test_summary.json` and
copied into `summary.json`.
