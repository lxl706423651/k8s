# B62 Kubernetes Scale Experiment

> Current single-host preset (2026-09-14): 1078 topology, 6 VMs, 298 vCPU,
> 596 GiB. Older 4954/30-VM examples below are historical configurations.
> The single-host wrapper now reads only `configSingleHostKvmMacvlan.yaml`;
> its run-local assignment is generated automatically.
> See [the current single-host guide](configSingleHostKvmMacvlan.md) for GiB
> memory fields and the 48-VM/4954 configuration changes.

This directory is the runnable B62 SeedEMU/K3s scale experiment workspace under
`largeScale/b62_k8s_scale`. The single-config entry discovers this repository
as its source root unless `experiment.sourceRoot` is specified. Legacy
assignment entrypoints still resolve the source tree from `assignment.yaml`.

## Quick Kubernetes Debug Commands

Run these commands from this directory. Use the local stable kubeconfig
explicitly so a stale `KUBECONFIG` from another experiment is not used by
mistake:

```bash
export B62_NS="${B62_NS:-seedemu-b62-mvlan-vlan-4954}"
export B62_KUBECONFIG="${B62_KUBECONFIG:-$(pwd)/kubeconfigMultiHostKvmMacvlan.yaml}"
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

Large post-BIRD runs can intermittently return `TLS handshake timeout`,
`Client.Timeout exceeded`, or `http2: client connection lost` while VM CPUs are
saturated. First retry the same command with an explicit request timeout:

```bash
kubectl --kubeconfig "${B62_KUBECONFIG}" --request-timeout=120s -n "${B62_NS}" get pods -l seedemu.io/role=brd -o wide
```

## Configuration Model

For the original single-hypervisor flow, `assignment.yaml` is the main
user-facing experiment specification. It contains:

- topology size, worker count, namespace, source root, and topology input path;
- total CPU/memory budget and master VM resources;
- KVM network/IP/MAC settings and SSH identity;
- K3s, registry, and CNI/network settings.

Stage tuning is mostly defined near the top of each stage script. `deploy.sh`
also accepts a `deploy:` section in `assignment.yaml`, so deployment pressure
limits can be changed in the assignment without exporting environment
variables.

The full-flow runner also accepts these stable assignment sections:

- `verification.loadThreshold`, `loadCheckIntervalSeconds`, and
  `loadMaxWaitSeconds`: common VM load gate used after BIRD and kernel export;
- `verification.protocolWaitSeconds` and
  `protocolCheckIntervalSeconds`: bounded BGP verification retries;
- `verification.fibSampleAsCount`: number of AS representatives for FIB
  verification; `0` means every router AS and is required by
  `assignmentBenchmark3083.yaml`;
- `reconvergence.loadThreshold`, `loadCheckIntervalSeconds`,
  `routeCheckIntervalSeconds`, and `phaseTimeoutSeconds`: fault/recovery gates;
- `reconvergence.chaosTargetPrefixes` and `verifyTargetPrefixes`: fixed Pods
  used for directly comparable failure and recovery measurements.

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

## Large-Scale K8s Design

The large-scale K8s design treats Kubernetes as the runtime substrate and keeps
SeedEMU's network model intact: every SeedEMU local network, IX LAN, and
cross-connect is compiled into a Multus secondary network. Router containers
still run BIRD and own their Linux interfaces, ARP state, RIB, and FIB. The
K8s layer is responsible for deterministic placement, image distribution,
secondary-interface creation, controlled startup, and sampling-based
verification.

### Network Backend Model

The implementation supports three backend families side by side:

| Backend | How it represents one SeedEMU network | Main use |
| --- | --- | --- |
| `macvlan+VLAN` | One VLAN-backed parent such as `ens2.<vlan>` or `ens3.<vlan>`; Pod gets a macvlan interface on that parent | Default high-throughput large-scale routing experiments |
| `bridge+VLAN` | One VLAN-backed parent plus one per-network Linux bridge; Pod joins through veth | Debugging, host-side observability, packet capture, and fault injection |
| `OVN+OVS` | One Kube-OVN Subnet / OVN logical switch; Pod joins through Kube-OVN/OVS | SDN fabric and OVN/OVS control-plane experiments |

The default `assignment.yaml` currently uses `macvlan+VLAN`:

- `networking.backend: macvlan`
- `networking.cniType/localLinkCniType/attachedCniType: macvlan`
- `networking.macvlanVlanMode: true`
- `placement.excludeControlPlane: false`, so the master can also run workload
  Pods when the assignment asks for it.

`bridge+VLAN` is selected by using the bridge CNI type with VLAN mode enabled.
It keeps the same VLAN-based isolation but adds a host-visible Linux bridge per
network. `OVN+OVS` is selected by using `kube-ovn` for the backend/CNI fields.
For OVN runs, `deploy.sh` automatically switches to conservative node-stream
creation because the bottleneck is local OVS add-port/CNI ADD pressure, not
only global `kubectl apply` throughput.

Keep `skipKubeOvnGatewayCheck` disabled for any OVN run that continues into
`start-bird`, `verify-after-start-bird`, `start-kernel`, or
`verify-after-fib-write`. Enabling it injects Kube-OVN
`activation_strategy=rarp` annotations on attached provider networks. That can
make Pod readiness faster, but the attached logical ports can remain
`up=false` until RARP activation, so ARP, OSPF, and BGP adjacencies do not form.
Use it only for deploy/Pod-ready pressure tests where routing protocols are not
validated.

### Stage Design

`compile` resolves the target topology and the current Ready node set, then
generates a deterministic by-AS hard placement plan. This placement is written
into the manifest as fixed `kubernetes.io/hostname` selectors. Hard placement
is important at scale because later stages need to know exactly which node will
run each Pod: image preload can be reduced to each node's required images, and
VLAN-backed deploy can create only the VLAN parents needed on each node. The
compiler writes `output/k8s.yaml` for VLAN-backed modes and
`output/k8s.kube-ovn.yaml` when the runtime backend is Kube-OVN.

`build` runs after compile and assumes hard placement is present. It uploads
the compiled output to the master/registry node, checks the local registry,
builds or fast-archives the SeedEMU images, pushes them to the registry, and
preloads only the image set each K3s node needs. This avoids relying on
Internet pulls during deploy and prevents large runs from repeatedly rebuilding
or transferring the same image layers.

`deploy` applies the compiled Kubernetes resources in pressure-controlled
batches. For `macvlan+VLAN` and `bridge+VLAN`, it first derives node-specific
VLAN requirements from the manifest and creates the required `ensX.<vlan>`
parents only on nodes that need them; in bridge mode it also creates the
per-network Linux bridge and attaches the VLAN parent to it. For `OVN+OVS`,
it applies Kube-OVN VPC/Subnet/NAD resources and waits for Subnet readiness
before creating workload Pods. OVN Pod creation uses node-stream throttling so
each node has only a small number of active CNI ADD operations, avoiding the
`ovs-vsctl` timeout and add-port failures seen at high concurrency.

`start-bird` starts BIRD after all workload Pods are Ready. It deliberately uses
a node-local path instead of thousands of `kubectl exec` starts: the script
groups target router-like Pods by node, SSHes to the VM, finds containers with
`crictl`, and uses `nsenter` to start BIRD inside the container namespaces.
All nodes proceed concurrently, while each node processes its Pods serially
with a small per-container delay. This keeps API-server pressure low and makes
startup bounded by per-node workload rather than one global serial loop.

`verify-after-start-bird` is a sampling verification stage. It first waits
until every VM's `load1` is below the configured threshold, then selects one
deterministic BRD Pod from each of the first 10 ASNs. For each selected Pod it
runs `birdc show protocols` and `birdc show route count`. The check requires
BGP protocols to exist and protocol rows to be in healthy states; it retries for
a bounded protocol convergence window before failing the run.

`start-kernel` enables BIRD-to-Linux-FIB export after BIRD has converged. It
uses the same node-local SSH + `crictl` + `nsenter` design as `start-bird`.
Inside each router-like container it writes `/etc/bird/conf/kernel.conf` with
the configured export mode, long scan interval, and jitter, then reloads BIRD.
The long scan/jitter settings avoid synchronized kernel protocol scans across
thousands of containers.

`verify-after-fib-write` again waits for VM load to settle and selects one fixed
smallest-router-id BRD Pod per configured AS count. A count of `0` selects
every router AS, as used by the 3083 benchmark. It compares Linux FIB size with
BIRD's route count. The pass condition is
`ip route | wc -l > birdc show route count` network count. This does not prove
every route in the topology is perfect, but it verifies the intended kernel
export effect on deterministic AS representatives.

`renderAssignmentConfig.py` renders only stable generated files:

- `configKvm.yaml`: neutral single-host input to `k8sTools.py build`.
- `resourcePlan.yaml` and `resourcePlan.json`: computed master/worker resource
  distribution.

`k8sTools.py build` then writes:

- `configK3s.yaml`
- `kubeconfig.yaml`
- `cluster.inventory.yaml`

There is intentionally no `config/` directory and no generated
`assignment_overrides.sh`.

### Single-Host macvlan+VLAN

`configSingleHostKvmMacvlan.yaml` is the sole editable single-hypervisor input,
currently 6 VMs, 298 vCPU, 596 GiB, topology 1078. It also holds experiment,
placement and deploy settings alongside macvlan+VLAN, MTU 1400, and `ens3/ens4`.
`kvm/prepareKvmNetworks.py` creates its dedicated management NAT network and
two unnumbered libvirt trunk networks as part of `k8sTools.py build`.

Run the workflow at the scale specified in that YAML:

```bash
./runSingleHostMacvlanExperiment.sh
```

The post-FIB dataplane gate uses a deterministic shared-NAD Pod pair on
different K3s nodes and records bidirectional ping/DF-ping/iperf3 in
`cross_node_workload_summary.json`. See `configSingleHostKvmMacvlan.md` for
the parameter contract and `0728ExperimentDesign.md` for both execution flows.

### Multi-Host macvlan+VLAN

`configMultiHostKvmMacvlan.yaml` is the minimal explicit infrastructure input
for the 4954 multi-host experiment. It describes two physical hypervisors, 30
VMs, one routed K3s management NIC per VM, and two dedicated VLAN trunk NICs.
The infrastructure kind is `multiHostKvm`; macvlan+VLAN is selected
independently by `fabric.type: macvlan-vlan`.

The management plane and emulated data plane are separate:

- `ens2` carries VM management and K3s/Flannel traffic over routed per-host
  subnets.
- `ens3` and `ens4` carry tagged SeedEMU secondary networks.
- Each physical host owns the same two unnumbered libvirt bridges. One VXLAN
  flood domain per bridge preserves VLAN tags and L2 broadcast domains across
  the physical-host boundary.
- The 4763 secondary networks in the 4954 topology require two trunks because
  one 802.1Q trunk has at most 4094 usable VLAN IDs.
- `fabric.mtu: 1400` leaves room for VLAN and fabric VXLAN headers on a
  1500-byte underlay. The compiler writes this MTU into every secondary NAD.

The build performs two independent cross-host gates. Before K3s installation,
`validateMultiHostKvmFabric.py` tests bidirectional tagged macvlan traffic on
every trunk. After all nodes are schedulable, `validateMultiHostK3s.py` pins
two temporary Pods to different hypervisors and requires primary Pod-IP ping
plus a 1400-byte unfragmented IP packet to pass in both directions. The fabric
gate applies the same MTU check and also requires adjacent VLANs to remain
unreachable.

After FIB verification, `validateCrossHostWorkload.py` deterministically picks
the first ready non-IX router pair that shares a Multus NAD while running on
nodes in different management `/24`s. The fixed pair must contain iperf3; a
missing binary fails the test instead of changing the pair. The gate verifies
all NAD MTUs, one real secondary interface on every K3s node, bidirectional
ping, 1400-byte DF ping, and five-second TCP iperf3 before reconvergence starts.

The declared VM allocation is 280 vCPU and 589 GiB RAM. The master receives
48 vCPU and 96 GiB RAM; 29 workers receive 8 vCPU and 17 GiB RAM each. Every
VM disk is 80 GiB.

Run the complete 4954 workflow:

```bash
./runMultiHostMacvlanExperiment.sh
```

Use an explicit scale-first run directory:

```bash
RUN_DIR="$(pwd)/runs/4954_$(date +%Y%m%d_%H%M%S)_multihost_macvlan_vlan"
./runMultiHostMacvlanExperiment.sh "${RUN_DIR}"
```

The stable generated cluster files are
`configK3sMultiHostKvmMacvlan.yaml`,
`kubeconfigMultiHostKvmMacvlan.yaml`, and
`clusterMultiHostKvmMacvlan.inventory.yaml`. They are intentionally separate
from the original single-host files.

The recovered 4954 validation run and its measured stage results are recorded
in `0727k8sExperimentDesgin.md`. The namespace and all 30 VMs were cleaned and
destroyed on 2026-07-28; the parent VLAN inventory and destroy evidence are in
`0728ExperimentDesign.md`.

## Complete Experiment

Run one full experiment with the default assignment:

```bash
./runExperiment.sh
```

Run with an explicit assignment:

```bash
./runExperiment.sh assignment.yaml
```

Run the fair six-VM 4954-node comparison from the canonical
`/home/lxl/seed-emulator/topology/output_4954` contexts:

```bash
RUN_DIR="$(pwd)/runs/4954_$(date +%Y%m%d_%H%M%S)_w5_canonical"
python3 runFullExperiment.py \
  --assignment assignmentBenchmark4954.yaml \
  --run-dir "${RUN_DIR}"
```

This assignment allocates 288 vCPU, 576 GiB RAM, and 100 GB per VM across
one master and five workers. Its two VLAN trunks provide 8188 IDs for the
4763 isolated L2 networks. The compile stage validates all 4954 canonical
addresses and records aggregate BIRD/kernel hashes. `start-bird` and
`start-kernel` use this directory's `start_bird.sh` and
`start_bird_kernel.sh`; no pre-start BIRD/kernel-export state gate is imposed,
while the post-stage protocol and FIB gates remain mandatory.

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
validate-cross-host-k3s
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
[kvm] validate-cross-node-workload
[multiHostKvm] validate-cross-host-workload
reconvergence
# test-bgp is currently disabled in runFullExperiment.py
# destroy-cluster-after-pass is currently disabled in runFullExperiment.py
```

`verify-after-start-bird` waits until every VM has `load1 < 50`, then checks
one fixed brd pod from each of the first 10 ASNs. `verify-after-fib-write`
waits for the same load gate after start-kernel; the benchmark assignment then
checks one smallest-r brd pod from every router AS with
`ip route | wc -l > birdc show route count` network count. The full flow leaves
the VM cluster in place after reconvergence so it can be inspected manually.

Every run writes a directory like:

```text
runs/<topology>_<timestamp>_w<workers>/
```

Important artifacts:

- `summary.json`: stage start/end/duration/exit status.
- `performance_metrics.csv` / `performance_summary.json`: common five-second
  host CPU/load/memory/disk/network and QEMU RSS/CPU samples.
- `placement_plan.json`: deterministic static placement assignments and
  per-node Pod counts; compact statistics are copied into `summary.json`.
- `<stage>.log`: direct logs for each stage.
- `output/`: compiled Kubernetes manifest and image build plan.
- `node_image_refs/`: per-node preload image lists.
- `start_bird_summary.json`: BIRD start result.
- `verify_after_start_bird_summary.json`: load-gated 10-AS BIRD protocol
  verification result.
- `start_bird_kernel_summary.json`: BIRD kernel export result.
- `verify_after_fib_write_summary.json`: load-gated 10-AS FIB route-count
  verification result.
- `reconvergence_summary.json`: post-FIB reconvergence measurement result.
- `k3s_cross_host_summary.json`: deterministic bidirectional primary-CNI
  probe between Pods on different hypervisors.
- `cross_host_workload_summary.json`: all-NAD/per-node MTU checks plus
  deterministic cross-hypervisor ping, DF ping, and iperf3 results.
- `bgp_test_summary.json`: BGP protocol test result when `test.py` is run manually.
- `bgp_test_targets.json`: selected BRD pods when `test.py` is run manually.

## Manual Stage Debugging

The workload stages remain available for debugging on an existing cluster:

```bash
RUN_DIR="$(pwd)/runs/4954_$(date +%Y%m%d_%H%M%S)_w11"
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
./runReconvergence.sh "${RUN_DIR}"
python3 ./test.py seedemu-b62-1078 "${RUN_DIR}"
```

Build only the VM/K3s cluster:

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
that relative path to an absolute path in `configKvm.yaml`. This prevents
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
| `assignment.yaml` | Legacy entrypoint input; not read by the single-config wrapper. Run-local files of this name are generated stage snapshots. |
| `assignmentBenchmark3083.yaml` | Fair 3083/6-VM benchmark: 288 vCPU, 576 GiB RAM, 100 GB/VM, load1 50, all-AS FIB, and fixed reconvergence targets. |
| `assignmentBenchmark4954.yaml` | Fair canonical-output 4954/6-VM benchmark with the same total resources, two VLAN trunks, all-AS FIB, and fixed reconvergence targets. |
| `runExperiment.sh` | Small shell entrypoint for the full workflow; delegates to `runFullExperiment.py`. |
| `runFullExperiment.py` | Full orchestrator: render assignment, rebuild cluster, clean, preflight, compile, build images, deploy, wait-ready, start/verify BIRD, start/verify kernel, and reconvergence. |
| `runSingleHostMacvlanExperiment.sh` | Single-YAML full workflow, prepare-only validation, and snapshot-based resume. |
| `configSingleHostKvmMacvlan.yaml` | Sole single-host user configuration: VM resources/network plus experiment, placement and deploy parameters. |
| `runMultiHostMacvlanExperiment.sh` | Complete 4954 flow using the explicit multi-host macvlan+VLAN cluster config and scale-first run directory names. |
| `configMultiHostKvmMacvlan.yaml` | Minimal two-hypervisor/30-VM infrastructure and VLAN trunk specification. |
| `validateCrossHostWorkload.py` | Deterministic post-FIB shared-NAD ping/iperf3 gate; supports single-host cross-node and multi-host cross-/24 modes. |
| `runMultiVmMacvlanVlanSweep.py` | Sequential 4954-scale macvlan+VLAN sweep runner for total VM counts such as 6/12/18/24/36/48; writes `0625multiVM.md`. |
| `buildCluster.sh` | Manual entrypoint for rendering config and building only the KVM/K3s cluster. |
| `destroyCluster.sh` | Manual entrypoint for destroying the recorded cluster and matching B62 libvirt networks. |

### Stage Scripts

| File | Role |
| --- | --- |
| `clean.sh` | Deletes the active workload namespace and, only for an OVN backend, matching Kube-OVN objects/finalizers. |
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
| `performanceMetrics.py` | Imports the common native/K8S five-second host and QEMU sampler. |
| `validateMultiHostK3s.py` | Creates two temporary fixed-node Pods and verifies bidirectional Flannel Pod-IP reachability across hypervisors. |
| `ensureAllNodesSchedulable.sh` | Removes common master taints and uncordons all nodes so the master can also run workload Pods. |

### Python Helpers

| File | Role |
| --- | --- |
| `k8sTools.py` | Local wrapper that imports and runs `seedemu.k8sTools.K8sTools`. |
| `renderAssignmentConfig.py` | Renders neutral `configKvm.yaml` and resource plans from `assignment.yaml`. |
| `prepareLibvirtDhcp.py` | Prepares libvirt DHCP reservations and DNS lease state before VM creation. |
| `seed_k8s_plan_real_topology_by_as.py` | Creates deterministic by-AS hard node placement for compile-time nodeSelectors. |
| `fastSeedemuImageArchive.py` | Builds per-node Docker-compatible archives with shared Dockerfile software-layer variants for fast SeedEMU image preload. |
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
| `largeScaleK8sDesign.md` | High-level design note for the large-scale K8s workflow, including compile/build/deploy/start/verify stages and backend choices. |
| `configSingleHostKvmMacvlan.md` | Single-host config parameter contract, resource budget, and build/destroy commands. |
| `0728ExperimentDesign.md` | Per-VM parent VLAN inventory, clean/destroy evidence, and single-/multi-host file-level execution flows. |
| `0727k8sExperimentDesgin.md` | Multi-host macvlan+VLAN design, manual commands, 4954 measured results, and recovery analysis. |
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
| `configKvm.yaml` | Generated single-host KVM/k8sTools input rendered from `assignment.yaml`. |
| `configK3sSingleHostKvmMacvlan.yaml` | Generated explicit single-host cluster state/config. |
| `kubeconfigSingleHostKvmMacvlan.yaml` | Generated kubeconfig for the explicit single-host cluster. |
| `clusterSingleHostKvmMacvlan.inventory.yaml` | Generated explicit single-host node inventory. |
| `configK3sMultiHostKvmMacvlan.yaml` | Generated multi-host cluster state/config written by k8sTools build. |
| `kubeconfigMultiHostKvmMacvlan.yaml` | Generated kubeconfig for the multi-host macvlan+VLAN cluster. |
| `clusterMultiHostKvmMacvlan.inventory.yaml` | Generated multi-host node inventory. |
| `configK3s.yaml` | Generated cluster state/config written by k8sTools build. |
| `kubeconfig.yaml` | Generated kubeconfig for the active K3s cluster. |
| `cluster.inventory.yaml` | Generated node inventory for SSH, preload, node-local BIRD operations, and load gates. |
| `resourcePlan.yaml` | Generated human-readable VM CPU/memory allocation plan. |
| `resourcePlan.json` | Generated machine-readable VM CPU/memory allocation plan. |
## Current Single-Host Configuration (2026-09-14)

`configSingleHostKvmMacvlan.yaml` is now the sole user input for the single-host
wrapper: it defines 6 VMs (298 vCPU, 596 GiB total), topology 1078, placement
and deployment settings. Memory uses `memoryGiB`. Worker count and default
namespace/name are derived, not duplicated in a second user configuration.
The wrapper invokes `runFullExperiment.py --config`; it does not read the
repository's `assignment.yaml`. The run-local `assignment.yaml` is a generated
compatibility snapshot for stage scripts, not another file to edit.
The multi-host configuration remains 30 VMs, with memory also expressed in GiB.
Older 4954/30-VM examples below describe prior configurations, not current defaults.
See [the single-host configuration guide](configSingleHostKvmMacvlan.md) for
memory conversion semantics and the exact 48-VM/4954 changes. A full run still
requires matching canonical `output_<size>` input and explicit final clean/destroy.

Use `./runSingleHostMacvlanExperiment.sh --prepare-only` to validate the
configuration and write a PREPARED resource summary without cluster operations.
Standalone `./clean.sh <run-dir>` reads paths from the generated runtime
snapshot; no B62 environment exports are needed for new single-config runs.
Resume with `./runSingleHostMacvlanExperiment.sh <run-dir> --resume-from start-kernel`
uses that run's snapshots, even if the original input has since changed.
The original `runExperiment.sh` and explicit dual-input/multi-host entrypoints
remain supported; their repository `assignment.yaml` has not been deleted.

The 2026-09-14 1078/6-VM run passed deployment, BIRD/FIB and ping/iperf;
that historical automated result remains FAIL because the old reconvergence
logic checked protocol health immediately after the first route increase.
Independent recovery checks passed, and final clean/destroy was verified.
See the [run report](runs/1078_20260914_145500_singlehost_6vm/experimentResult.md)
for failures, both dataplane measurements, timings and cleanup evidence.

Recovery now polls BIRD total routes on all restarted fault targets every 5s.
Two unchanged comparisons (three equal samples) confirm a plateau; its first
sample is the convergence timestamp. Changes or invalid readings reset the
plateau, and all targets remain monitored until all are stable and protocol/
VM load checks pass. Global recovery time is the latest per-target plateau
start, measured from before the first restart command, not the confirmation.
`recovery_samples.jsonl` and `recovery_stability.json` retain the full evidence.
Use `--resume-from reconvergence` to repeat only fault/recovery on an existing
run; this injects a new fault and does not reconstruct an earlier missing trace.
The [4954/48-VM recovery validation](runs/4954_20260914_155524_singlehost_macvlan_vlan/recoveryFixResult.md)
passed with a 43.998s plateau-start recovery time, 53.936s confirmation,
14/14 healthy protocol samples and 674/674 post-recovery FIB samples.
Final clean and destroy were completed and independently verified.

## B63 复用入口

`../b63_deployment/runB63LargeExperiment.py` 直接调用本目录 `runFullExperiment.main()`，
并传入可选 `after_compile` / `after_ready` 回调，分别替换 placement 和检查实际落点。
回调接收 `(summary, summary_path, run_dir, env)`。未传回调时 B62 行为不变；
B63 的输入、集群状态及 namespace 均由其运行目录独立保存。详见
[B63 共享流程](../b63_deployment/README.md)。
