# B62 Kubernetes Scale Experiment

This directory is the runnable B62 SeedEMU/K3s scale experiment workspace under
`largeScale/b62_k8s_scale`. The experiment resolves the SeedEMU source tree from
`assignment.yaml` and is configured to use this repository via `../..`.

## Quick Kubernetes Debug Commands

Run these commands from this directory. Use the local stable kubeconfig
explicitly so a stale `KUBECONFIG` from another experiment is not used by
mistake:

```bash
export B62_NS=seedemu-b62-macvlan-vlan-9955-w63-225237
export B62_KUBECONFIG="$(pwd)/kubeconfig.yaml"
```

Check namespace, nodes, and pod status:

```bash
kubectl --kubeconfig "${B62_KUBECONFIG}" get ns
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

Stage tuning is mostly defined near the top of each stage script. `deploy.sh`
also accepts a `deploy:` section in `assignment.yaml`; the current OVN+OVS
profile uses that YAML section to pin conservative CNI ADD pressure limits
without requiring exported environment variables.

- `build.sh`: Docker build, buildx, image archive, registry push, and preload
  concurrency.
- `deploy.sh`: deploy batch size, sleeps, pressure thresholds, monitoring, and
  Kube-OVN subnet wait. In macvlan+VLAN mode, deploy also prepares VLAN parent
  interfaces before workload Pods are applied. It now derives node-specific
  VLAN requirements from each workload's fixed `kubernetes.io/hostname`
  placement and Multus network annotation, then creates only the VLAN parents
  needed on each node. Workloads without fixed placement are expanded to all
  nodes as a safety fallback.
- `clean.sh`: namespace cleanup timeout/finalizer behavior.
- `wait-ready.sh`: pod readiness polling interval and timeout.
- `start_bird.sh`: node-local SSH + `crictl`/`nsenter` BIRD start mode,
  all-node concurrency, per-node serial execution, post-start settle time, and
  load wait behavior. Its tuning constants are defined at the top of the
  script.
- `start_bird_kernel.sh`: BIRD kernel export mode, kernel protocol scan timing,
  retry behavior, node-local all-node concurrency, and post-switch load wait.
  Its tuning constants are defined at the top of the script.
- `test.py`: manual BGP protocol test helper; it is not part of the current
  full experiment flow.

The current OVN+OVS assignment starts from 1078 routers and keeps the control
plane node free from workload Pods:

- `networking.backend: kube-ovn`
- `networking.cniType/localLinkCniType/attachedCniType: kube-ovn`
- `placement.excludeControlPlane: true`
- `placement.nodePodReserve: 20`

The OVN+OVS deploy parameters in `assignment.yaml` are:

- `controllerApplyMode: node-stream`, `nodeStreamMaxActivePerNode: 1`
- `batchSize: 5`, `warmupBatches: 2`, `warmupBatchSize: 5`
- `subnetBatchSize: 250`, `subnetBatchSleepSeconds: 0`
- `maxPendingPods: 8`, `maxCreatingPods: 0`, `maxNotReadyPods: 8`
- `kubeOvnSubnetTimeoutSeconds: 36000`
- `postSubnetCooldownSeconds: 60`
- `skipKubeOvnGatewayCheck: false`

Keep `skipKubeOvnGatewayCheck` disabled for any run that continues into
`start-bird`, `verify-after-start-bird`, `start-kernel`, or
`verify-after-fib-write`. Enabling it injects Kube-OVN
`activation_strategy=rarp` annotations on attached provider networks. That can
make Pod readiness faster, but the attached logical ports can remain
`up=false` until RARP activation, so ARP, OSPF, and BGP adjacencies do not form.
Use it only for deploy/Pod-ready pressure tests where routing protocols are not
validated.

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
ensure-all-nodes-schedulable
clean
preflight
compile
build
deploy
wait-ready
start-bird
verify-after-start-bird
start-kernel
verify-after-fib-write
# test-bgp is currently disabled in runFullExperiment.py
# destroy-cluster-after-pass is currently disabled in runFullExperiment.py
```

`verify-after-start-bird` waits until every VM has `load1 < 80`, then checks
one fixed brd pod from each of the first 10 ASNs. `verify-after-fib-write`
waits for the same load gate after start-kernel, then checks one smallest-r
brd pod from each of the first 10 ASNs with
`ip route | wc -l > birdc show route count` network count. The full flow leaves
the VM cluster in place after the final
verification so it can be inspected manually.

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
- `verify_after_start_bird_summary.json`: load-gated 10-AS BIRD protocol
  verification result.
- `start_bird_kernel_summary.json`: BIRD kernel export result.
- `verify_after_fib_write_summary.json`: load-gated 10-AS FIB route-count
  verification result.
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
./verifyStartBird.sh "${RUN_DIR}"
./start_bird_kernel.sh "${RUN_DIR}"
./verifyFibRoutes.sh "${RUN_DIR}"
python3 ./test.py seedemu-b62-1078 "${RUN_DIR}"
```
 ./clean.sh "${RUN_DIR}" && ./preflight.sh "${RUN_DIR}" && ./compile.sh "${RUN_DIR}" && ./build.sh "${RUN_DIR}" && ./deploy.sh "${RUN_DIR}" && ./wait-ready.sh "${RUN_DIR}"

./wait-ready.sh "${RUN_DIR}" && ./start_bird.sh "${RUN_DIR}" && ./verifyStartBird.sh "${RUN_DIR}" &&  ./start_bird_kernel.sh "${RUN_DIR}" && ./verifyFibRoutes.sh "${RUN_DIR}"


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

## Base Image Cache

The KVM Ubuntu cloud image is cached in
`base_image/jammy-server-cloudimg-amd64.img`. `assignment.yaml` references this
file through `kvm.legacyBaseImagePath`, and `renderAssignmentConfig.py` resolves
that relative path to an absolute path in `configKvmOvn.yaml`. This prevents
each VM rebuild from downloading a fresh Ubuntu cloud image into a new temporary
k8sTools setup directory.

## Top-Level File Roles

This section documents the stable top-level files that remain in this directory.
Generated files are kept locally for the active cluster, but are ignored by
`.gitignore` and should not be committed.

### User Entry Points

| File | Role |
| --- | --- |
| `README.md` | User-facing workflow, debugging commands, and file-role documentation. |
| `assignment.yaml` | Main editable experiment specification: topology size, VM resources, K3s, registry, networking, placement, and deploy tuning. |
| `runExperiment.sh` | Small shell entrypoint for the full workflow; delegates to `runFullExperiment.py`. |
| `runFullExperiment.py` | Full orchestrator: render assignment, rebuild cluster, clean, preflight, compile, build images, deploy, wait-ready, start/verify BIRD, start/verify kernel, and reconvergence. |
| `buildCluster.sh` | Manual entrypoint for rendering config and building only the KVM/K3s/Kube-OVN cluster. |
| `destroyCluster.sh` | Manual entrypoint for destroying the recorded cluster and matching B62 libvirt networks. |

### Stage Scripts

| File | Role |
| --- | --- |
| `clean.sh` | Deletes the active workload namespace and matching Kube-OVN objects/finalizers. |
| `preflight.sh` | Checks node, registry, kube-system, topology input, and namespace preconditions. |
| `compile.sh` | Generates placement and compiles the SeedEMU topology into Kubernetes manifests. |
| `build.sh` | Builds workload images on the master/registry node and preloads per-node image archives. |
| `deploy.sh` | Applies compiled manifests, prepares VLAN CNI parents when needed, batches workload creation, and monitors pressure. |
| `wait-ready.sh` | Waits until all workload Pods in the namespace are Running and Ready. |
| `start_bird.sh` | Starts BIRD inside router-like Pods through node-local `crictl`/`nsenter`. |
| `verifyStartBird.sh` | Load-gated BIRD protocol verification for 10 deterministic AS representative BRD Pods. |
| `start_bird_kernel.sh` | Writes/reloads BIRD kernel export configuration inside router-like Pods. |
| `verifyFibRoutes.sh` | Load-gated FIB verification for 10 deterministic AS representative BRD Pods. |
| `runReconvergence.sh` | Runs the reconvergence measurement after kernel export and FIB verification. |
| `ensureAllNodesSchedulable.sh` | Removes common master taints and uncordons all nodes so the master can also run workload Pods. |

### Python Helpers

| File | Role |
| --- | --- |
| `k8sTools.py` | Local wrapper that imports and runs `seedemu.k8sTools.K8sTools`. |
| `renderAssignmentConfig.py` | Renders `configKvmOvn.yaml` and resource plans from `assignment.yaml`. |
| `prepareLibvirtDhcp.py` | Prepares libvirt DHCP reservations and DNS lease state before VM creation. |
| `seed_k8s_plan_real_topology_by_as.py` | Creates deterministic by-AS hard node placement for compile-time nodeSelectors. |
| `fastSeedemuImageArchive.py` | Builds per-node Docker-compatible archives for fast SeedEMU image preload. |
| `generate_node_image_refs.py` | Extracts per-node image reference lists from a compiled Kubernetes manifest. |
| `start_bird_node_local.py` | Node-local implementation used by `start_bird.sh`. |
| `verifyStartBird.py` | Python implementation used by `verifyStartBird.sh`. |
| `start_bird_kernel_node_local.py` | Node-local implementation used by `start_bird_kernel.sh`. |
| `verifyFibRoutes.py` | Python implementation used by `verifyFibRoutes.sh`. |
| `measureReconvergence.py` | Python implementation used by `runReconvergence.sh`. |
| `compareVlanCniObservability.py` | Manual evidence tool for VLAN-backed CNI isolation and host-side observability. |
| `test.py` | Manual BGP protocol health test helper; not part of the default full workflow. |

### Shared Shell Helpers

| File | Role |
| --- | --- |
| `lib.sh` | Shared shell library for assignment loading, path resolution, kubeconfig setup, cluster node parsing, SSH helpers, load monitoring, and network-backend detection. |
| `ensure_master_registry.sh` | Registry helper used by the build stage to keep the master-hosted registry available. |

### Documentation And Analysis Notes

| File | Role |
| --- | --- |
| `0623conclusion.md` | Final comparison of `OVN+OVS`, `macvlan+VLAN`, and `bridge+VLAN` for the current simulator. |
| `Error_ovn.md` | Historical OVN/OVS failure analysis and deploy/CNI ADD tuning record. |
| `bird.md` | Historical and current BIRD/start-kernel behavior notes and risk analysis. |
| `bridgeMacvlanVlanCompare.md` | 4954-scale bridge+VLAN versus macvlan+VLAN rebuild comparison. |
| `multiVmMacvlanVlanReport.md` | Multi-VM macvlan+VLAN 4954 experiment report. |
| `parameter_audit.md` | Historical parameter audit; some start-bird details are superseded by `bird.md` and current scripts. |
| `problem.md` | Large-scale failure-mode log and recovery notes. |
| `codex_worklog.md` | Command/change worklog for this directory. |

### Generated Or Local Runtime Files

| File | Role |
| --- | --- |
| `.gitignore` | Prevents runtime artifacts, cache directories, generated configs, and local lock files from being committed. |
| `.cluster-lifecycle.lock` | Local lock used by cluster build/destroy scripts. |
| `.latest_run_dir` | Local marker for an older run directory. |
| `.latest_rebuild_compare_root` | Local marker for a previous comparison run root. |
| `configKvmOvn.yaml` | Generated KVM/k8sTools input rendered from `assignment.yaml`. |
| `configK3s.yaml` | Generated cluster state/config written by k8sTools build. |
| `kubeconfig.yaml` | Generated kubeconfig for the active K3s cluster. |
| `cluster.inventory.yaml` | Generated node inventory for SSH, preload, node-local BIRD operations, and load gates. |
| `resourcePlan.yaml` | Generated human-readable VM CPU/memory allocation plan. |
| `resourcePlan.json` | Generated machine-readable VM CPU/memory allocation plan. |
