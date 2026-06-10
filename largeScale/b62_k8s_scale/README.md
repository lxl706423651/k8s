# B62 Kubernetes Scale Experiment

This directory is the runnable B62 SeedEMU/K3s scale experiment workspace under
`largeScale/b62_k8s_scale`. The experiment resolves the SeedEMU source tree from
`assignment.yaml` and is configured to use this repository via `../..`.

## Quick Kubernetes Debug Commands

Run these commands from this directory. Use the local stable kubeconfig
explicitly so a stale `KUBECONFIG` from another experiment is not used by
mistake:

```bash
export B62_NS=seedemu-b62-4954
export B62_KUBECONFIG="$(pwd)/kubeconfig.yaml"
```

Check namespace, nodes, and pod status:

```bash
kubectl --kubeconfig "${B62_KUBECONFIG}" get namespace "${B62_NS}"
kubectl --kubeconfig "${B62_KUBECONFIG}" get nodes -o wide
kubectl --kubeconfig "${B62_KUBECONFIG}" -n "${B62_NS}" get pods -o wide
kubectl --kubeconfig "${B62_KUBECONFIG}" -n "${B62_NS}" get pods -l seedemu.io/role=brd -o wide
kubectl --kubeconfig "${B62_KUBECONFIG}" -n "${B62_NS}" get deploy -o wide
kubectl --kubeconfig "${B62_KUBECONFIG}" -n "${B62_NS}" get events --sort-by='.lastTimestamp'
```

Inspect or enter one pod/deployment. Replace the object name with a real name
from `kubectl get pods` or `kubectl get deploy`:

```bash
kubectl --kubeconfig "${B62_KUBECONFIG}" -n "${B62_NS}" describe pod <pod-name>
kubectl --kubeconfig "${B62_KUBECONFIG}" -n "${B62_NS}" logs <pod-name> --tail=80
kubectl --kubeconfig "${B62_KUBECONFIG}" -n "${B62_NS}" exec -it <pod-name> -- bash
kubectl --kubeconfig "${B62_KUBECONFIG}" -n "${B62_NS}" exec -it deploy/<deploy-name> -- sh
kubectl --kubeconfig "${B62_KUBECONFIG}" -n "${B62_NS}" exec deploy/<deploy-name> -- birdc show protocols
```
as1862brd-r13-1.13.7.70-7564dbb49c-9wctj

Large post-BIRD runs can intermittently return `TLS handshake timeout`,
`Client.Timeout exceeded`, or `http2: client connection lost` while VM CPUs are
saturated. First retry the same command with an explicit request timeout:

```bash
kubectl --kubeconfig "${B62_KUBECONFIG}" --request-timeout=120s -n "${B62_NS}" get pods -l seedemu.io/role=brd -o wide
```

## Configuration Model

`assignment.yaml` is the only user-facing experiment specification. It contains:

- topology size, worker count, namespace, source root, and topology input path;
- total CPU/memory budget and master VM resources;
- KVM network/IP/MAC settings and SSH identity;
- K3s, registry, and CNI/network settings.

Stage tuning is not stored in `assignment.yaml` and is not generated into a
`config/` directory. Each stage script defines its own constants near the top:

- `build.sh`: Docker build, buildx, image archive, registry push, and preload
  concurrency.
- `deploy.sh`: deploy batch size, sleeps, pressure thresholds, monitoring, and
  Kube-OVN subnet wait.
- `clean.sh`: namespace cleanup timeout/finalizer behavior.
- `wait-ready.sh`: pod readiness polling interval and timeout.
- `start_bird.sh`: kubectl-exec BIRD start mode, all-node concurrency,
  per-node serial execution, post-start settle time, and load wait behavior.
  Its tuning constants are defined at the top of the script.
- `start_bird_kernel.sh`: BIRD kernel export mode, kernel protocol scan timing,
  retry behavior, kubectl-exec all-node concurrency, and post-switch load wait.
  Its tuning constants are defined at the top of the script.
- `test.py`: manual BGP protocol test helper; it is not part of the current
  full experiment flow.

`renderAssignmentConfig.py` renders only stable generated files:

- `configKvmOvn.yaml`: input to `k8sTools.py build`.
- `resourcePlan.yaml` and `resourcePlan.json`: computed master/worker resource
  distribution.

`k8sTools.py build` then writes:

- `configK3s.yaml`
- `kubeconfig.yaml`
- `cluster.inventory.yaml`

There is intentionally no `config/` directory and no generated
`assignment_overrides.sh`.

## Complete Experiment

Run one full experiment with the default assignment:

```bash
./runExperiment.sh
```

Run with an explicit assignment:

```bash
./runExperiment.sh assignment.yaml
```

Useful debug form:

```bash
./runExperiment.sh assignment.yaml --skip-final-destroy
```

The complete flow is:

```text
render-assignment
destroy-existing-cluster
prepare-libvirt-dhcp
build-cluster
clean
preflight
compile
build
deploy
wait-ready
start-bird
start-kernel
# test-bgp is currently disabled in runFullExperiment.py
# destroy-cluster-after-pass is currently disabled in runFullExperiment.py
```

The full flow currently leaves the VM cluster in place after start-kernel so it
can be inspected manually.

Every run writes a directory like:

```text
runs/<timestamp>_<topology>_w<workers>/
```

Important artifacts:

- `summary.json`: stage start/end/duration/exit status.
- `<stage>.log`: direct logs for each stage.
- `output/`: compiled Kubernetes manifest and image build plan.
- `node_image_refs/`: per-node preload image lists.
- `start_bird_summary.json`: BIRD start result.
- `start_bird_kernel_summary.json`: BIRD kernel export result.
- `bgp_test_summary.json`: BGP protocol test result when `test.py` is run manually.
- `bgp_test_targets.json`: selected BRD pods when `test.py` is run manually.

## Manual Stage Debugging

The workload stages remain available for debugging on an existing cluster:

```bash
RUN_DIR="$(pwd)/runs/$(date +%Y%m%d_%H%M%S)_4954_w11"
mkdir -p "${RUN_DIR}"
cp assignment.yaml "${RUN_DIR}/assignment.yaml"

./clean.sh "${RUN_DIR}"
./preflight.sh "${RUN_DIR}"
./compile.sh "${RUN_DIR}"
./build.sh "${RUN_DIR}"
./deploy.sh "${RUN_DIR}"
./wait-ready.sh "${RUN_DIR}"
./start_bird.sh "${RUN_DIR}"
./start_bird_kernel.sh "${RUN_DIR}"
python3 ./test.py seedemu-b62-1078 "${RUN_DIR}"
```
 ./clean.sh "${RUN_DIR}" && ./preflight.sh "${RUN_DIR}" && ./compile.sh "${RUN_DIR}" && ./build.sh "${RUN_DIR}" && ./deploy.sh "${RUN_DIR}" && ./wait-ready.sh "${RUN_DIR}"

./wait-ready.sh "${RUN_DIR}" && ./start_bird.sh "${RUN_DIR}" && ./start_bird_kernel.sh "${RUN_DIR}"


Build only the VM/K3s/Kube-OVN cluster:

```bash
./buildCluster.sh "$(pwd)/runs/build_cluster_$(date +%Y%m%d_%H%M%S)" assignment.yaml
```

Destroy the cluster recorded in `configK3s.yaml`:

```bash
./destroyCluster.sh
```

The destroy script also destroys and undefines matching `seedemu-b62-net-*`
libvirt networks so stale NAT CIDRs do not block the next build.

The runtime path has separate `start-bird` and `start-kernel` stages. There is
no verify stage in this B62 flow.

## Base Image Cache

The KVM Ubuntu cloud image is cached in
`base_image/jammy-server-cloudimg-amd64.img`. `assignment.yaml` references this
file through `kvm.legacyBaseImagePath`, and `renderAssignmentConfig.py` resolves
that relative path to an absolute path in `configKvmOvn.yaml`. This prevents
each VM rebuild from downloading a fresh Ubuntu cloud image into a new temporary
k8sTools setup directory.
