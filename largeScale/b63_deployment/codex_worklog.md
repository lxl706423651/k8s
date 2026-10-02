## 2026-06-24 11:02 - 1078 Full Workflow Placement Test

- User intent: test whether the 1078-scale b63 workflow can complete with placement optimization, and ensure static and dynamic metrics are monitored.
- Scope: b63 large-scale workflow, placement objective, running scripts, generated experiment artifacts under `experiments/1078_20260624_102400`.
- Changes: adjusted placement scoring to include macvlan/VLAN-oriented pod pressure and VLAN-parent pressure; kept estimated resource metrics in reports while skipping Kubernetes resource request injection for this VM-scale test; fixed registry probing in `running/build.sh`; generated compact `evaluation_summary.json` from static and dynamic metrics.
- Commands: `python3 ./runB63LargeExperiment.py --topology-size 1078 --kvm-config ./configkvm_b63.yaml --skip-resource-request-injection --post-deploy-seconds 60 --deploy-max-creating-pods-per-node 40` built the b63 cluster, compiled, placed, built images, deployed, started BIRD, enabled kernel export, and verified FIB; `kubectl --kubeconfig ./kubeconfig-b63.yaml ... get pods/get nodes` checked live pod and node state; `python3 -m py_compile runPlacementExperiment.py runB63LargeExperiment.py` and `bash -n running/placement.sh running/lib.sh running/deploy.sh running/build.sh` validated changed Python and shell files.
- Validation: full workflow passed in `experiments/1078_20260624_102400`; static optimized placement was worker1/worker2/worker3 = 444/311/323 pods and 526/413/418 VLAN parent estimates, with pod and VLAN hard-cap excess both 0; dynamic deployment reached 1078/1078 Running and Ready pods, final actual placement matched the static plan, BIRD start passed for 1078 targets, kernel export switched 1078 targets, and FIB verification passed on 10 AS samples.
- Notes: the earlier resource-request-injected run failed with Kubernetes CPU scheduling pressure on the current 3-worker VM cluster, so the successful 1078 run intentionally used `--skip-resource-request-injection`; resource estimates remain available in `placement_report.json` for static evaluation.

## 2026-06-24 11:28 - Default No Resource Request Injection

- User intent: make placement default to partitioning only, without writing Kubernetes CPU/memory request constraints unless explicitly requested.
- Scope: `runPlacementExperiment.py`, `runB63LargeExperiment.py`, `running/lib.sh`, `running/placement.sh`, `README.md`, `0624design.md`.
- Changes: added explicit `--inject-resource-requests`; changed default behavior to leave `resources.requests` absent; kept legacy `--skip-resource-request-injection` accepted as compatibility; added `metadata.resourceRequestsInjected` to `placement_report.json`.
- Commands: ran direct placement smoke tests against the 1078 raw manifest with default settings and with `--inject-resource-requests`; ran `python3 -m py_compile runPlacementExperiment.py runB63LargeExperiment.py`; ran `bash -n running/placement.sh running/lib.sh running/deploy.sh running/build.sh`.
- Validation: default smoke manifest had 1078 controllers and 0 containers with requests; explicit injection smoke manifest had 1078 controllers and 1078 containers with requests; syntax checks passed.
- Notes: default full workflow now matches the intended SeedEMU behavior: placement constrains node partitioning, while CPU/memory estimates remain report metrics unless scheduler resource-request experiments are explicitly enabled.

## 2026-06-24 14:45 - Default Scheduler Baseline Path

- User intent: compare optimized placement against a true Kubernetes default scheduler dynamic baseline using `k8sTools.py up`.
- Scope: `runDefaultSchedulerBaseline.py`, `README.md`, `0624design.md`.
- Changes: added a default-scheduler baseline runner that copies compile output, removes hard placement and `resources.requests`, rewrites the namespace, optionally runs `k8sTools.py up`, and can then run BIRD/FIB verification stages; documented the workflow and fairness constraint.
- Commands: `python3 ./runDefaultSchedulerBaseline.py --topology-size 1078 --source-output ./experiments/1078_20260624_102400/output --experiment-dir /tmp/b63_default_scheduler_prepare_smoke` prepared a non-deploy smoke output; `python3 -m py_compile runDefaultSchedulerBaseline.py runPlacementExperiment.py runB63LargeExperiment.py`; `bash -n running/placement.sh running/lib.sh running/deploy.sh running/build.sh`.
- Validation: smoke output had 1078 controllers, 0 `nodeName`, 0 hostname `nodeSelector`, 0 `resources.requests`, and namespace `seedemu-b63-1078-default-scheduler`; syntax checks passed.
- Notes: the script does not deploy unless `--run-up` is passed. For fair dynamic comparison, clean the optimized workload or use a separate b63 cluster before running default scheduler.

## 2026-06-24 14:59 - Latency And Throughput Design

- User intent: extend the b63 experiment design to compare placement algorithms with ping latency and iperf throughput now that router images include `iperf3`.
- Scope: `0624design.md`.
- Changes: added an active dataplane measurement design covering router endpoint selection, canonical pair sampling, same/cross-node and same/cross-AS strata, ping/iperf3 commands, dynamic metrics, derived penalties, control variables, and updated success criteria.
- Commands: inspected `examples/kubernetes/real_topology_k3s_compile.py` and `0624design.md`; searched for existing ping/iperf references with `rg`; reviewed the updated Markdown sections.
- Validation: documentation now includes `latency_throughput_report.json`, canonical pair-set reuse, post-FIB measurement timing, and controls for fair comparison across optimized/default/baseline placements.
- Notes: this was a design-only change; no ping/iperf runner was implemented or executed in this step.

## 2026-06-24 15:13 - Deterministic Pair Plan And Runtime Budget

- User intent: make pod/router pair selection reproducible and keep ping/iperf measurements within a one-to-two-hour experiment budget.
- Scope: `0624design.md`.
- Changes: specified deterministic pair generation from topology/assignment/rr_plan hashes, stable `pair_seed`, per-stratum hash ranking, pair plan reuse checks, stable execution ordering, ping/iperf batch scheduling, concurrency limits, load gates, retry rules, and expected runtime.
- Commands: reviewed the active measurement section with `sed`; searched for pair plan/runtime keywords with `rg`.
- Validation: design now requires `pair_plan_<scale>_<topology_hash>.json`, fixed seed/version parameters, no unseeded random pair selection, and low-concurrency iperf batches with contamination marking.
- Notes: this remains a design-only update; implementation of pair-plan generation and measurement runner is still a follow-up task.

## 2026-06-24 15:25 - High-Concurrency Iperf Stress Design

- User intent: strengthen the dataplane design so concurrent iperf groups expose placement differences in bandwidth and loaded latency.
- Scope: `0624design.md`.
- Changes: split dataplane measurement into low-interference baseline and high-concurrency stress layers; added deterministic stress round planning, concurrency levels `8/16/32` with optional `48/64`, synchronized server/client startup, loaded ping canaries, overload cutoffs, stress-specific metrics, and separate aggregation rules.
- Commands: reviewed the updated section with `sed`.
- Validation: design now captures both single-flow path quality and concurrent contention behavior while preserving deterministic pair and round selection.
- Notes: no measurement runner was executed; this is a design update.

## 2026-06-24 23:03 - 1078 Round 1 Placement And Baseline Comparison

- User intent: continue the 1078-scale full-flow experiment and compare optimized placement against multiple baselines with static and dynamic metrics.
- Scope: b63 1078 experiments, `running/fastSeedemuImageArchive.py`, dataplane measurement artifacts, comparison report.
- Changes: fixed `running/fastSeedemuImageArchive.py` so it derives Dockerfile `COPY` inputs dynamically instead of using stale hard-coded layer hashes; wrote `experiments/1078_20260624_230342_comparison/round1_comparison.md`.
- Commands: ran `python3 runB63LargeExperiment.py --topology-size 1078 --kvm-config ./configkvm_b63.yaml --skip-cluster-build --algorithm pod-count-balanced --post-deploy-seconds 60 --deploy-max-creating-pods-per-node 40`; reran it after the archive fix; ran `python3 runB63LargeExperiment.py --topology-size 1078 --kvm-config ./configkvm_b63.yaml --skip-cluster-build --algorithm hypergraph --post-deploy-seconds 60 --deploy-max-creating-pods-per-node 40`; ran `python3 running/measureDataplane.py --namespace seedemu-b63-1078 --kubeconfig ./kubeconfig-b63.yaml --topology-size 1078 --artifact-dir ./experiments/1078_20260624_222225 --pair-plan ./experiments/pair_plans/pair_plan_1078_6f647396ba2e2db4_seed20260624_v1_36pairs.json --ping-count 10 --ping-concurrency 8 --baseline-tcp-directions 24 --baseline-tcp-duration 8 --baseline-tcp-concurrency 4 --stress-levels 8,16,32,48 --stress-repetitions 2 --stress-duration 15 --loaded-ping-canaries 12 --loaded-ping-count 15`; used `jq`, `kubectl`, and a read-only Python summarizer to extract comparison metrics.
- Validation: pod-count-balanced full flow passed in `experiments/1078_20260624_214757`; hypergraph full flow passed in `experiments/1078_20260624_222225`; hypergraph dataplane measurement produced `latency_throughput_summary.json`, `latency_throughput_report.json`, and `stress_rounds.json`; all compared runs used the same deterministic 36-pair plan and the same ping/iperf parameters.
- Notes: optimized and hypergraph have identical static placement at 444/311/323 pods and 1357 VLAN parents, while optimized had slightly better measured baseline TCP and high-concurrency loaded RTT in this run. Pod-count-balanced and default scheduler are the clearest negative baselines for macvlan/VLAN locality.

## 2026-06-25 11:30 - B62-Sized B63 Cluster And 1897 Optimized Run

- User intent: continue validation on larger scales using resources comparable to B62: total 300 CPU, 600 GiB memory, and 80 GB disk per VM.
- Scope: `configkvm_b63.yaml`, b63 cluster rebuild, optimized 1897 experiment, dataplane v2 measurement.
- Changes: configured b63 as 1 master plus 5 workers, with master `48 vCPU/96 GiB/80 GB` and each worker `50 vCPU/100 GiB/80 GB`; kept workload placement off the master by default; retained default no Kubernetes `resources.requests` injection.
- Commands: destroyed the prior small b63 cluster; built the new cluster with `python3 ./k8sTools.py build --input ./configkvm_b63.yaml --config-k3s ./configK3s-b63.yaml --kubeconfig ./kubeconfig-b63.yaml --inventory ./inventory-b63.yaml`; ran `python3 runB63LargeExperiment.py --topology-size 1897 --kvm-config ./configkvm_b63.yaml --skip-cluster-build --algorithm optimized --post-deploy-seconds 60 --deploy-batch-size 80 --deploy-batch-sleep-seconds 0 --deploy-warmup-batches 12 --deploy-warmup-batch-size 1 --deploy-stabilize-timeout-seconds 36000 --deploy-max-pending-pods 160 --deploy-max-creating-pods 160 --deploy-max-creating-pods-per-node 40 --deploy-max-notready-pods 160 --deploy-max-failed-pods 10`; ran dataplane v2 with 96 pairs, baseline TCP 48 directions, and stress levels `8,16,32,48,64`.
- Validation: `experiments/1897_20260625_113126` passed full clean/compile/placement/preflight/build/deploy/wait-ready/start-bird/verify/start-kernel/FIB verification; final pod distribution was worker1-5 `379/379/379/380/380`; dataplane summary was written under `experiments/1897_20260625_113126/dataplane/`.
- Notes: static optimized VLAN parent total was `2427`, versus `3935` for pod-count-balanced and `3933` for kubernetes-default-scheduler in the same static report.

## 2026-06-25 14:25 - 3083 Optimized Run, Routing Failure, And Cleanup

- User intent: run the larger 3083 dataset to validate whether the current static and dynamic metrics still work at bigger scale.
- Scope: optimized 3083 experiment, BIRD/FIB routing validation, dataplane v2 measurement, experiment artifact repair, cleanup, and documentation.
- Changes: made load-gated routing scripts use env-overridable thresholds; changed finite load waits in `verifyStartBird.py` and `verifyFibRoutes.py` to log and continue instead of raising a timeout; updated `experiments/3083_20260625_122845/summary.json` and `output/placement_report.json` with manual continuation, routing failure, and dataplane summary; documented the 1897/3083 results in `README.md` and `0624design.md`.
- Commands: ran `python3 runB63LargeExperiment.py --topology-size 3083 --kvm-config ./configkvm_b63.yaml --skip-cluster-build --algorithm optimized --post-deploy-seconds 60 --deploy-batch-size 80 --deploy-batch-sleep-seconds 0 --deploy-warmup-batches 12 --deploy-warmup-batch-size 1 --deploy-stabilize-timeout-seconds 36000 --deploy-max-pending-pods 160 --deploy-max-creating-pods 160 --deploy-max-creating-pods-per-node 40 --deploy-max-notready-pods 160 --deploy-max-failed-pods 10`; manually resumed `start_bird.sh` with `BIRD_LOAD_THRESHOLD=200 BIRD_LOAD_MAX_WAIT_SECONDS=30`; ran `verifyStartBird.sh` with `VERIFY_START_BIRD_LOAD_THRESHOLD=350 VERIFY_START_BIRD_LOAD_MAX_WAIT_SECONDS=30`; attempted `start_bird_kernel.sh` with higher load thresholds and aborted after no worker progress; ran `running/measureDataplane.py` with pair-plan v2 and stress levels `8,16,32,48,64`; cleaned the namespace with `running/clean.sh` into `experiments/3083_20260625_142244_cleanup_after_dataplane`.
- Validation: deploy and wait-ready passed with 3083/3083 Pods Running/Ready and distribution worker1-5 `617/617/616/617/616`; static optimized VLAN parent total was `3696`, versus `6259` for pod-count-balanced and `6249` for kubernetes-default-scheduler; start-bird resume confirmed `3083/3083` BIRD processes; `verify-after-start-bird` failed on `10/10` sampled AS routers; no FIB verification was produced; dataplane summary reported ping `88/192` success and baseline TCP `15/48` success; cleanup deleted namespace `seedemu-b63-3083` and all b63 nodes were Ready afterward.
- Notes: the 3083 dataplane numbers are diagnostic under routing failure. They should not be used as clean placement performance rankings until BIRD protocol convergence and FIB export pass.

## 2026-06-25 19:15 - 12VM Master-Included 3083 Full Workflow And Upstream Pull

- User intent: first switch b63 to 12 VMs with the master participating, set the large-scale protocol timers through the compiler API, run the 3083 full workflow to completion, then pull `upstream development` and inspect conflicts.
- Scope: `configkvm_b63.yaml`, `runB63LargeExperiment.py`, `runPlacementExperiment.py`, `running/lib.sh`, `running/placement.sh`, `examples/kubernetes/real_topology_k3s_compile.py`, `README.md`, `0624design.md`, and the 3083 experiment artifact.
- Changes: configured 12 VMs at `25 vCPU / 50 GiB / 80 GB` each for total `300 vCPU / 600 GiB`; made master participation the default placement behavior with `--exclude-master` as the opt-out; added `--fast-greedy-score` propagation for large placement runs; kept resource request injection opt-in only; set OSPF `hello=10/dead=40/tick=3` and iBGP/eBGP `holdTime=36000/keepaliveTime=60` in `real_topology_k3s_compile.py`; documented the successful 3083 run and current routing thresholds.
- Commands: restored libvirt `default` network after an initial destroyed-cluster attempt removed it; reused the b63 12VM cluster with `--skip-cluster-build`; ran `env BIRD_LOAD_THRESHOLD=200 BIRD_LOAD_MAX_WAIT_SECONDS=30 BIRD_NODE_CONCURRENCY=3 VERIFY_START_BIRD_LOAD_THRESHOLD=250 VERIFY_START_BIRD_LOAD_MAX_WAIT_SECONDS=30 KERNEL_LOAD_THRESHOLD=250 KERNEL_LOAD_MAX_WAIT_SECONDS=30 KERNEL_NODE_CONCURRENCY=3 VERIFY_FIB_LOAD_THRESHOLD=250 VERIFY_FIB_LOAD_MAX_WAIT_SECONDS=30 python3 runB63LargeExperiment.py --topology-size 3083 --kvm-config ./configkvm_b63.yaml --skip-cluster-build --algorithm optimized --fast-greedy-score --post-deploy-seconds 60 --deploy-batch-size 80 --deploy-batch-sleep-seconds 0 --deploy-warmup-batches 12 --deploy-warmup-batch-size 1 --deploy-stabilize-timeout-seconds 36000 --deploy-max-pending-pods 240 --deploy-max-creating-pods 240 --deploy-max-creating-pods-per-node 40 --deploy-max-notready-pods 240 --deploy-max-failed-pods 10 --preload-node-concurrency 6 --preload-image-concurrency 2`; ran `git fetch upstream development`, stashed tracked local changes, ran `git pull --no-rebase upstream development`, and restored the stash.
- Validation: `experiments/3083_20260625_183501/summary.json` is `PASS`; stage durations were compile `125.52s`, placement `133.91s`, build `244.51s`, deploy `699.40s`, wait-ready `227.34s`, start-bird `51.52s`, start-kernel `383.61s`, verify-after-start-bird `7.50s`, and verify-after-fib-write `7.81s`. Dynamic state reached `3083/3083` Running/Ready pods; master and worker1-10 each hosted `257` pods and worker11 hosted `256`. `start_bird_kernel_summary.json` reports `3083/3083` targets switched. BIRD and FIB verification both passed `10/10` sampled AS representatives. `placement_report.json` kept `resourceRequestsInjected=false`; static optimized VLAN parent total was `3962`, versus `6708` for `pod-count-balanced`, `6722` for `kubernetes-default-scheduler`, and `5674` for `resource-only`.
- Git result: `upstream development` was pulled by merge because the local branch had diverged. The merge and stash restore produced no conflict markers; a marker scan over the touched b63/b62/compiler paths found none.
- Notes: the earlier 5-worker 3083 routing failure should now be treated as a historical diagnostic run. Clean 3083 placement/dataplane comparisons should use a BIRD/FIB-passing run such as `3083_20260625_183501`.

## 2026-06-25 20:42 - 3083 Clean Dataplane Supplement And Automatic Dataplane Stage

- User intent: rerun the ping/iperf dataplane measurement for the clean 12VM 3083 artifact, and make future full-flow experiments include ping/iperf by default.
- Scope: `experiments/3083_20260625_183501/dataplane/`, `runB63LargeExperiment.py`, `README.md`, `0624design.md`, `summary.json`, and `output/placement_report.json`.
- Changes: added a default post-FIB `dataplane` stage to `runB63LargeExperiment.py`; added `--skip-dataplane` plus dataplane parameter flags; recorded dataplane artifact references and compact summary in `summary.json` and `placement_report.json`; documented that full b63 runs now measure dataplane after `verify-after-fib-write`.
- Commands: ran `python3 ./running/measureDataplane.py --namespace seedemu-b63-3083 --kubeconfig ./kubeconfig-b63.yaml --topology-size 3083 --artifact-dir ./experiments/3083_20260625_183501 --pair-plan ./experiments/3083_20260625_122845/dataplane/pair_plan_3083_9f6e118305f37617_seed20260624_v2.json --pair-plan-version 2 --pairs-per-stratum 32 --max-pairs-total 96 --ping-count 10 --ping-concurrency 8 --baseline-tcp-directions 48 --baseline-tcp-duration 8 --baseline-tcp-concurrency 4 --stress-levels 8,16,32,48,64 --stress-repetitions 3 --stress-duration 15 --loaded-ping-canaries 16 --loaded-ping-count 15`; used a structured Python update to add `dataplaneValidation` to the existing 3083 summary and placement report; ran `python3 -m py_compile largeScale/b63_deployment/runB63LargeExperiment.py`; ran `python3 largeScale/b63_deployment/runB63LargeExperiment.py --help | rg -n 'dataplane|fast-greedy|exclude-master'`.
- Validation: dataplane artifacts were written under `experiments/3083_20260625_183501/dataplane/`: `ping_results.json`, `iperf_baseline_results.json`, `stress_rounds.json`, `latency_throughput_summary.json`, and `latency_throughput_report.json`. Ping succeeded for `156/192` directions with overall median RTT `0.2495 ms` and cross-node median RTT `0.316 ms`. Baseline TCP succeeded for `36/48` directions with aggregate goodput `273617 Mbps` and Jain fairness `0.884`. Stress `8/16/32/48/64` each completed 3 rounds; average aggregate goodput was about `36.8/67.7/102.2/136.8/177.3 Gbps`, with average loaded RTT about `2.35/4.00/6.69/10.29/12.94 ms`.
- Notes: the supplemented 3083 dataplane used the same deterministic v2 pair plan as the earlier failed-routing 3083 run, so pair selection is comparable. Some high-concurrency iperf directions timed out and are intentionally included in failure-rate metrics.

## 2026-06-25 20:47 - Clean 3083 Workload Namespace

- User intent: clear the current b63 cluster before continuing with more experiments.
- Scope: live `seedemu-b63-3083` Kubernetes namespace and cleanup artifact `experiments/3083_20260625_204138_cleanup/`.
- Changes: no code changes; deleted the active 3083 workload namespace while preserving the 12VM/K3s cluster.
- Commands: ran `running/clean.sh` with namespace `seedemu-b63-3083`, topology size `3083`, b63 kubeconfig/inventory/config paths, and clean timeout `3600s`.
- Validation: cleanup log reports deletion of `3083` deployments, `2454` replicasets, `3023` network-attachment-definitions, `2666` pods at deletion-list time, and `3088` event objects; namespace and matching Kube-OVN cluster-scoped resources were fully deleted after `24s`. `kubectl get ns seedemu-b63-3083 --ignore-not-found` returned no namespace, and all `12` cluster nodes remained `Ready`.
- Notes: this was workload cleanup only. It did not destroy the b63 VMs, K3s cluster, registry, images, or experiment artifacts.

## 2026-06-27 17:24 - 4954 12VM Optimized Full Workflow With Dataplane

- User intent: continue the 12/18/24/36/48 VM matrix exploration for larger b63 scales, including static placement and ping/iperf dynamic measurements.
- Scope: 12VM b63 cluster, `4954` topology, optimized placement run, macvlan/VLAN trunk capacity, static placement report, and dataplane artifacts under `experiments/4954_20260627_154613_12vm_optimized/`.
- Changes: generated and used 12VM matrix KVM config with four macvlan/VLAN trunks; kept master schedulable; used optimized placement without Kubernetes resource request injection; no code changes in this entry.
- Commands: ran `python3 ./runB63LargeExperiment.py --topology-size 4954 --run-dir ./experiments/4954_20260627_154613_12vm_optimized --kvm-config ./vm_matrix_configs/configkvm_b63_12vm.yaml --destroy-existing-cluster --algorithm optimized --fast-greedy-score ...`; monitored `summary.json`, `start_bird.log`, `start_bird_kernel.log`, `loadAverage.log`, and dataplane artifacts with `jq`, `find`, `ps`, and read-only SSH checks.
- Validation: full workflow passed. Stage durations included build-cluster `766.89s`, compile `664.59s`, placement `323.68s`, build `278.52s`, deploy `1074.52s`, wait-ready `375.12s`, start-bird `196.27s`, start-kernel `649.35s`, verify-after-FIB `11.10s`, and dataplane `1180.46s`. Static optimized placement reduced VLAN parent total to `6090` and spanning network count to `945`; static default-scheduler simulation was `10392` VLAN parents and `4737` spanning networks. Dataplane ping succeeded for `151/192`; baseline TCP iperf succeeded for `35/48` with aggregate goodput `356015.75 Mbps` and fairness `0.9456`; stress levels `8/16/32/48/64` completed three rounds each with average aggregate goodput about `38.9/94.7/151.9/194.7/232.9 Gbps` and loaded-ping loss `0`.
- Notes: this is the optimized side of the 4954/12VM comparison. A matching `kubernetes-default-scheduler` dynamic baseline was started afterward in `experiments/4954_20260627_172324_12vm_kubernetes-default-scheduler/`.

## 2026-06-27 18:25 - 4954 12VM Default Scheduler Fallback Fix

- User intent: continue the 12/18/24/36/48 VM matrix and include a real default-scheduler baseline using `k8sTools.py up`-style build/deploy behavior.
- Scope: `running/build.sh`, `running/generate_node_image_refs.py`, `runB63LargeExperiment.py`, `seedemu/k8sTools/runner.py`, and 4954 default-scheduler experiment artifacts.
- Changes: made `build.sh` recognize `metadata.selectedAlgorithm` in `placement_report.json`; changed `generate_node_image_refs.py --all-nodes` to collect workload images before nodeSelector validation so default-scheduler manifests without hard placement preload every image on every node; added/validated the physical K3s cleanup timeout path used before this run.
- Commands: ran `bash -n running/build.sh`; ran `python3 -m py_compile running/generate_node_image_refs.py`; ran `generate_node_image_refs.py --all-nodes` against `experiments/4954_20260627_173908_12vm_kubernetes-default-scheduler/output/k8s.yaml`; checked live 12VM cluster readiness with `kubectl get nodes`; started `python3 ./runB63LargeExperiment.py --topology-size 4954 --namespace seedemu-b63-4954 --run-dir ./experiments/4954_20260627_181411_12vm_kubernetes-default-scheduler --kvm-config ./vm_matrix_configs/configkvm_b63_12vm.yaml --skip-cluster-build --algorithm kubernetes-default-scheduler --fast-greedy-score ...`.
- Validation: syntax checks passed; all-node image-ref smoke test produced `4954` images per tested node with `selector_missing_count_observed=4954`; the live 12VM cluster was Ready before the rerun. The previous default run `experiments/4954_20260627_173908_12vm_kubernetes-default-scheduler` failed in build because the fallback read the wrong placement-report field.
- Notes: the rerun is ongoing at the time of this entry. Compile completed in `651.77s` and confirmed macvlan/VLAN trunks plus OSPF/BGP timer settings; placement is currently running.

## 2026-06-27 18:58 - Default Scheduler Registry-Pull Build Mode

- User intent: compare the default Kubernetes scheduler the same way `k8sTools.py up` would deploy it: build/push images, then let Kubernetes schedule Pods without hard placement.
- Scope: `running/build.sh`, aborted 4954 default artifact, and default-scheduler baseline execution policy.
- Changes: changed default-scheduler missing-nodeSelector handling from all-node preload to registry-pull mode; when `metadata.selectedAlgorithm=kubernetes-default-scheduler`, `build.sh` now disables `SEED_FAST_IMAGE_ARCHIVE`, skips per-node preload, runs normal `build_images.sh`, and lets deployed Pods pull images from the registry after scheduling.
- Commands: interrupted `experiments/4954_20260627_181411_12vm_kubernetes-default-scheduler` during all-node import; checked local and remote `k3s ctr images import` process cleanup; ran `bash -n running/build.sh`; marked the aborted artifact summary as `FAIL`.
- Validation: no local or remote import processes remained after interruption; cluster still had 12 nodes and no non-running Pods; `build.sh` syntax check passed; aborted summary now records the all-node-preload stop reason.
- Notes: the aborted run partially imported images into some nodes, so its build/deploy timing is not usable. Dataplane was never run for that artifact.

## 2026-06-27 19:48 - 4954 12VM Default Scheduler Registry-Pull Run

- User intent: rerun the 4954 default-scheduler baseline with no hard placement and with dynamic ping/iperf included.
- Scope: 12VM b63 cluster and `experiments/4954_20260627_185334_12vm_kubernetes-default-scheduler_registry_pull/`.
- Changes: no new code changes in this entry; this run uses the registry-pull default-scheduler build path from the previous entry.
- Commands: started `python3 ./runB63LargeExperiment.py --topology-size 4954 --namespace seedemu-b63-4954 --run-dir ./experiments/4954_20260627_185334_12vm_kubernetes-default-scheduler_registry_pull --kvm-config ./vm_matrix_configs/configkvm_b63_12vm.yaml --skip-cluster-build --algorithm kubernetes-default-scheduler --fast-greedy-score ...` with stdout redirected to the run outer log; monitored `summary.json`, `build.log`, `loadAverage.log`, and remote master Docker processes.
- Validation: clean passed in `1.98s`, compile passed in `659.40s`, placement passed in `321.12s`, and preflight passed in `9.42s`; build entered registry-pull mode and disabled fast archive as expected. At `19:47`, build had pushed about `857/4954` image digests and was still running.
- Notes: because default scheduler has no deterministic node-to-image mapping before scheduling, this run builds/pushes images to the registry and skips per-node preload. This is slower than optimized fast-archive runs but matches the default-scheduler baseline semantics.

## 2026-06-27 20:51 - Matrix Default Build Parallelism

- User intent: continue the larger VM/scale matrix without letting default-scheduler registry build become an avoidable bottleneck.
- Scope: `runVmScaleMatrix.py`.
- Changes: added algorithm-specific flow args so future `kubernetes-default-scheduler` matrix runs use `--build-parallelism 24`; did not enable `--build-skip-existing` because image tags can repeat across topology sizes while their generated contexts may differ.
- Commands: ran `python3 -m py_compile runVmScaleMatrix.py`; ran `python3 runVmScaleMatrix.py --vm-counts 12 --topology-sizes 3083 --algorithms kubernetes-default-scheduler --dry-run`.
- Validation: py_compile passed; dry-run command includes `--build-parallelism 24` only for the default-scheduler algorithm.
- Notes: the currently running `4954_20260627_185334_12vm_kubernetes-default-scheduler_registry_pull` run keeps its original build parallelism for reproducibility.

## 2026-06-27 21:54 - Live Default Scheduler Build Parallelism Increase

- User intent: keep the 4954/12VM default-scheduler full-flow baseline moving while preserving the `k8sTools.py up`-style registry-pull semantics.
- Scope: live build stage for `experiments/4954_20260627_185334_12vm_kubernetes-default-scheduler_registry_pull/`.
- Changes: no code changes; sent `SIGUSR1` to the running GNU `xargs` process on the master VM to raise live `docker buildx build` concurrency from 24 to about 48 after confirming CPU, memory, and disk headroom.
- Commands: checked `summary.json`, `build.log`, `loadAverage.log`, `kubectl get nodes`, and master VM `nproc/free/df`; ran a remote `sudo kill -USR1` loop against the active `xargs -P 12` process; confirmed `48` active `docker buildx build` processes afterward.
- Validation: the run remained in `build=RUNNING`; master VM reported `25` vCPU, about `49GiB` memory with only `3.3GiB` used, root disk `24%` used, and load average about `13.76/11.05/10.54` immediately after increasing concurrency.
- Notes: this affects build-stage wall time only. Placement, deploy scheduling, BIRD/FIB validation, and dataplane ping/iperf metrics remain unchanged.

## 2026-06-27 22:12 - VM-Aware Default Scheduler Build Fanout

- User intent: continue the 12/18/24/36/48 VM matrix while reducing avoidable default-scheduler registry-build bottlenecks.
- Scope: `runVmScaleMatrix.py` and the live 4954/12VM default-scheduler build observation.
- Changes: changed future matrix default-scheduler runs from a fixed `--build-parallelism 24` to a VM-aware fanout: approximately `3 * master_vcpus`, capped at `72` and floored at `24`.
- Commands: increased the live 4954/12VM xargs fanout from `48` to `72`; checked local image cache coverage for the 4954 target set; attempted and then stopped a direct `buildx --push` smoke test because it was too slow under current build pressure; ran `python3 -m py_compile runVmScaleMatrix.py`; ran `python3 runVmScaleMatrix.py --vm-counts 12,18,24,36,48 --topology-sizes 3083 --algorithms kubernetes-default-scheduler --dry-run`.
- Validation: dry-run showed default-scheduler build parallelism `72/51/39/27/24` for `12/18/24/36/48` VMs respectively; the direct-push test process was cleaned up and the live run returned to `72` buildx processes.
- Notes: direct `buildx --push` may still be worth testing in a separate low-load window, but it was not applied to the current baseline.

## 2026-06-28 01:36 - Reduce Live Default Scheduler Build Fanout

- User intent: keep the long 4954/12VM default-scheduler registry build progressing without letting high BuildKit concurrency degrade throughput.
- Scope: live build stage for `experiments/4954_20260627_185334_12vm_kubernetes-default-scheduler_registry_pull/`.
- Changes: no code changes; sent `SIGUSR2` to the running GNU `xargs` process to lower target build fanout from `72` to `48` after the 72-way run slowed to about `119` image digests per 30 minutes.
- Commands: checked `summary.json`, digest counts, `loadAverage.log`, and master VM load/memory/disk; sent a remote `sudo kill -USR2` loop against the active `xargs` process; rechecked active `docker buildx build` process count after 10 minutes.
- Validation: active buildx processes naturally dropped to `48`; digest count increased from `3563` to `3651` in the 10-minute follow-up window, with the run still in `build=RUNNING` and no recorded push retry/error.
- Notes: the live 12VM default-scheduler build now appears healthier at `48` than at `72`; `runVmScaleMatrix.py` was refined so future default-scheduler dry-runs use `48/48/39/27/24` build parallelism for `12/18/24/36/48` VMs, verified by `py_compile` and dry-run.

## 2026-06-28 09:16 - Default Scheduler All-Node Preload Build Script

- User intent: avoid the very slow `k8sTools.py up`-style registry-pull build path for baseline runs, while keeping default scheduler placement unconstrained.
- Scope: `running/build.sh`, `running/buildBaselineAllNodePreload.sh`, `runB63LargeExperiment.py`, `running/placement.sh`, `README.md`, and `runVmScaleMatrix.py` validation.
- Changes: added `SEED_DEFAULT_SCHEDULER_BUILD_MODE` support to `build.sh`; added `buildBaselineAllNodePreload.sh` to force `all-node-preload` plus fast archive mode; made `runB63LargeExperiment.py --algorithm kubernetes-default-scheduler` default to that wrapper, with `--default-scheduler-build-mode registry-pull` retained for reproducing the old slow path; updated placement warning and README.
- Commands: ran `chmod +x running/buildBaselineAllNodePreload.sh`; ran `bash -n running/build.sh running/buildBaselineAllNodePreload.sh running/placement.sh`; ran `python3 -m py_compile runB63LargeExperiment.py runVmScaleMatrix.py running/generate_node_image_refs.py`; ran `python3 runB63LargeExperiment.py --help | rg -n 'default-scheduler-build-mode|skip-build|algorithm'`; ran an all-node image-ref smoke test against the 4954 default-scheduler manifest; ran a matrix dry-run for the 12VM default-scheduler command.
- Validation: syntax and py_compile passed; help exposes `--default-scheduler-build-mode {all-node-preload,registry-pull}`; all-node smoke test generated `4954` image refs per tested node with `missing_selector_count=0` and `selector_missing_count_observed=4954`; dry-run still builds future default-scheduler matrix commands normally.
- Notes: this change does not alter hard-placement optimized builds. Direct manual `build.sh` still defaults to registry-pull fallback for no-selector manifests; use `buildBaselineAllNodePreload.sh` or the default `runB63LargeExperiment.py --algorithm kubernetes-default-scheduler` path for fast baseline runs.

## 2026-06-28 09:53 - 4954 12VM Default Scheduler Full Workflow Passed

- User intent: complete the 4954/12VM default-scheduler baseline with static and ping/iperf dataplane metrics.
- Scope: `experiments/4954_20260627_185334_12vm_kubernetes-default-scheduler_registry_pull/` and comparison against `experiments/4954_20260627_154613_12vm_optimized/`.
- Changes: no code changes in this entry; recorded the completed long-running baseline.
- Commands: monitored `summary.json`, `build.log`, `deploy.log`, `start-bird`, `start-kernel`, FIB verification, and dataplane artifacts with `jq`, `kubectl`, and SSH; extracted optimized/default static and dynamic metrics with read-only Python summaries.
- Validation: full workflow passed. Stage durations were build `46283.07s`, deploy `3685.18s`, start-bird `753.84s`, start-kernel `223.56s`, verify-after-FIB `14.71s`, and dataplane `1176.48s`. Static selected default-scheduler VLAN parent total was `10392` and spanning networks `4737`, versus optimized `6090` and `945`. Dataplane used the same deterministic pair plan parameters and produced ping `151/192` successes, baseline TCP `35/48` successes, aggregate TCP goodput `281271.88 Mbps`, and TCP fairness `0.7774`.
- Notes: optimized on the same 4954/12VM scale had baseline TCP `356015.75 Mbps` and fairness `0.9456`; stress aggregate goodput at `8/16/32/48/64` was `38.9/94.7/151.9/194.7/232.9 Gbps` for optimized versus `25.7/68.8/87.4/115.3/121.5 Gbps` for default. Default also had higher loaded RTT at every stress level, especially `64` concurrency (`11.30 ms` vs `1.95 ms`).

## 2026-06-28 17:37 - Matrix Cluster Reuse Fix During 7001/9955 Runs

- User intent: continue 7001 and 9955 full-flow static plus ping/iperf experiments across the VM matrix.
- Scope: `runVmScaleMatrix.py`, `experiments/7001_20260628_153715_12vm_optimized/`, and the failed same-VM follow-up run records.
- Changes: changed matrix command generation so runs that do not intentionally destroy/rebuild the VM cluster pass `--skip-cluster-build`; matrix records now include `skipClusterBuild`.
- Commands: ran the matrix until `7001_20260628_153715_12vm_optimized` passed; interrupted the matrix after same-VM follow-up runs hit VM name conflicts; ran `python3 -m py_compile runVmScaleMatrix.py`; ran a dry-run for 12/18 VM, 7001/9955, optimized/default-scheduler to verify first run per VM rebuilds and later runs reuse the cluster.
- Validation: `7001_20260628_153715_12vm_optimized` passed full flow with `dataplane=PASS`; dry-run showed `--skip-cluster-build` on non-destroy runs. The failed `7001_20260628_173509_12vm_kubernetes-default-scheduler` and `9955_20260628_173530_12vm_optimized` runs failed in `build-cluster` with existing VM name conflicts before this fix.
- Notes: the matrix will be resumed with `--skip-existing-pass`; failed/interrupted directories are retained for audit and are not used as valid comparisons.

## 2026-06-29 00:30 - Continue 9955 Larger-VM Full Flow

- User intent: continue larger-scale 7001 and 9955 full-flow experiments with static placement plus ping/iperf dynamic metrics, focusing on whether optimized placement improves at larger VM counts.
- Scope: 12VM/9955 diagnostic artifact, `running/start_bird_node_local.py`, `running/start_bird_kernel_node_local.py`, and new 18VM/9955 optimized run `experiments/9955_20260629_003000_18vm_optimized/`.
- Changes: added bounded SSH transport retry handling to the node-local start-BIRD and start-kernel scripts. The retry is limited to three total attempts and only applies to SSH return code 255 with connection/banner-exchange style errors before a remote `SUMMARY` line is produced.
- Commands: ran `python3 -m py_compile running/start_bird_node_local.py running/start_bird_kernel_node_local.py`; checked active experiment processes; inspected 12VM/9955 status and logs; started `runB63LargeExperiment.py --topology-size 9955 --run-dir ./experiments/9955_20260629_003000_18vm_optimized --kvm-config ./vm_matrix_configs/configkvm_b63_18vm.yaml --namespace seedemu-b63-matrix-18 --algorithm optimized --destroy-existing-cluster ...`.
- Validation: syntax check passed. The earlier `experiments/9955_20260628_205158_12vm_optimized/` run reached `9955/9955` Running pods but failed manual start-BIRD verification after an extended wait, so it has no valid FIB/dataplane result and should be treated as a 12VM capacity/convergence failure.
- Notes: the 18VM/9955 optimized run is currently in progress. `build-cluster` passed after two live systemd readiness interventions: `worker16` and `worker17` had `k3s-agent.service` stuck in `activating/start` even though kubelet had joined the cluster, so `systemd-notify --ready` was sent with each unit's active `ExecMainPID` during install/tuning to unblock Ansible. This changed only the live service readiness state and did not alter placement, topology, workload, or dataplane measurement logic. The retry patch does not hide remote script failures, container enumeration failures, BIRD protocol non-establishment, FIB verification failures, or dataplane failures.

## 2026-06-29 02:36 - Fix VM Matrix PodCIDR Capacity

- User intent: continue 7001/9955 larger-scale full-flow experiments with static placement and ping/iperf dataplane metrics.
- Scope: failed 18VM/9955 diagnostic run, `generateVmMatrixConfigs.py`, `configkvm_b63.yaml`, and regenerated `vm_matrix_configs/configkvm_b63_{12,18,24,36,48}vm.yaml`.
- Changes: diagnosed 18VM deploy stall as K3s PodCIDR exhaustion: `cluster-cidr=10.42.0.0/16` with `node-cidr-mask-size-ipv4=20` only provides 16 node CIDRs, leaving worker16/worker17 without `.spec.podCIDR`; changed generated matrix configs and base b63 config to the downstream-supported `nodeCidrMaskSizeIpv4: 22`.
- Commands: inspected node CIDRs and stuck pod events; stopped `experiments/9955_20260629_003000_18vm_optimized/`; marked its summary `FAIL`; regenerated matrix configs; ran `python3 -m py_compile generateVmMatrixConfigs.py runB63LargeExperiment.py running/start_bird_node_local.py running/start_bird_kernel_node_local.py`; launched and then stopped `experiments/9955_20260629_023654_18vm_optimized_podcidr22/` after discovering the first fix used the wrong snake_case field.
- Validation: KVM input and generated `configK3s` now both report `{'maxPods': 4000, 'nodeCidrMaskSizeIpv4': 22}`. The failed run reached compile, placement, and build PASS, then stalled in deploy with 1700 Running and 212 ContainerCreating pods, all on the two nodes with empty PodCIDR.
- Notes: `/22` gives about 1022 pod IPs per node and up to 64 node CIDRs inside the existing `/16`, which covers the 12/18/24/36/48 VM matrix without overlapping the service CIDR.

## 2026-06-29 17:45 - 9955 36VM Optimized Full Workflow Passed

- User intent: continue the larger 7001/9955 experiments with optimized placement, static placement evaluation, and dynamic ping/iperf dataplane measurement.
- Scope: `experiments/9955_20260629_055258_36vm_optimized_full/` plus comparison against the completed `7001_20260628_153715_12vm_optimized/` optimized run.
- Changes: no code changes in this entry; recorded the completed 9955 full-flow result.
- Commands: ran the 9955 workflow with `--topology-size 9955`, `--algorithm optimized`, 36VM config, `--include-master`, macvlan/vlan backend, deterministic dataplane pair plan seed `20260624`, stress levels `8,16,32,48,64`, and ping/iperf enabled; parsed `summary.json`, `output/placement_report.json`, and `dataplane/latency_throughput_summary.json` with read-only Python summaries.
- Validation: full workflow passed. Key stage durations were cluster build `1885.30s`, compile `948.84s`, placement `3854.89s`, build `627.61s`, deploy `30813.32s`, start-kernel `1255.01s`, and dataplane `1586.95s`; wait-ready, start-BIRD, verify-after-start-BIRD, and verify-after-FIB all passed. Placement metadata reports `resourceRequestsInjected=false`, so this run did not inject Pod CPU/memory requests.
- Static results: optimized reduced default-scheduler static objective by `97.35%`, VLAN parent total by `41.02%`, max VLAN parents per node by `37.07%`, network cost ratio by `80.25%`, pair cost by `77.61%`, and endpoint exposure by `76.69%`.
- Dynamic results: ping succeeded `144/192` with average RTT `1.98 ms`; baseline TCP iperf succeeded `35/48`, aggregate goodput was `328304.92 Mbps`, and fairness was `0.9749`. Stress aggregate goodput at `8/16/32/48/64` concurrency was `54.99/105.02/193.51/255.96/303.50 Gbps`; loaded RTT averages were `0.615/0.603/1.148/1.321/2.143 ms`.
- Notes: the valid large-scale dynamic results currently cover optimized placement for 7001 and 9955. Default-scheduler dynamic comparison is still unavailable at these two scales because earlier default/failed runs did not pass through BIRD/FIB/dataplane; static baseline comparison is available inside each `placement_report.json`.

## 2026-06-29 19:09 - 0629 Experiment Result Summary

- User intent: summarize collected b63 placement experiment results across scales, compare optimized placement against each baseline method, and write the tables and analysis into `0629experimentResult.md`.
- Scope: read-only experiment artifacts under `experiments/*/{summary.json,output/placement_report.json,dataplane/latency_throughput_summary.json}`; created `0629experimentResult.md`; updated this worklog.
- Changes: added a structured result report covering data scope, metric definitions, optimized static absolute metrics, optimized-vs-baseline static reductions, 1078 multi-method dynamic comparison, 4954 optimized/default dynamic comparison, optimized dynamic scaling trend, workflow durations, missing/failed baseline caveats, and conclusions.
- Commands: listed available summary, placement, and dataplane artifacts with `find`; parsed run status, selected algorithms, static metrics, dynamic ping/iperf metrics, stress metrics, and stage durations with read-only Python snippets; inspected the generated Markdown with `sed`; checked `wc -l` and searched for placeholder tokens with `rg -n 'TODO|FIXME|NaN|None|null'`.
- Validation: `0629experimentResult.md` was created with 187 lines; no placeholder tokens were found. The report only uses full-flow PASS dataplane results for dynamic tables and explicitly marks default-scheduler dynamic gaps for 7001/9955.
- Notes: no experiments were launched and no cluster/VM/Kubernetes resources were modified. `7001` and `9955` still lack valid default-scheduler dynamic baselines, so those scales are compared dynamically only for optimized and statically against the baseline methods embedded in `placement_report.json`.

## 2026-06-30 09:49 - Diagnose 15000 48VM Optimized Failure

- User intent: investigate why the largest unscaled topology run failed after deploying with optimized placement only.
- Scope: `experiments/15000_20260629_195450_48vm_optimized_full/`, live master VM `seedemu-b63-placement-master`, deploy logs, K3s journal, and failure artifacts.
- Changes: no code changes; appended this diagnostic record.
- Commands: inspected `summary.json`, `deploy.log`, `deploy.orchestrator.log`, `deploy_failure_artifacts/*`, `loadAverage.log`, and `placement_report.json`; checked the live cluster with `kubectl version/get nodes/get pods`; SSHed to the master VM to inspect `systemctl status k3s`, `journalctl -u k3s`, `journalctl -k`, memory/disk usage, K3s datastore size, and live `k3s-server` RSS; checked libvirt VM state with `virsh list --all`.
- Validation: the run failed in `deploy` after `16016.62s`, immediately after batch 279 created controllers `13766..13815/15265`; the last successful pressure check was `pre-batch-279` with `13765` Pods and `48/48` Ready nodes. Failure artifacts and later `kubectl` checks show apiserver access failure (`TLS handshake timeout`, later `connection refused`). Master K3s logs around `2026-06-29 18:05..18:17 UTC` show `Slow SQL`, API traces delayed 4-11 minutes, `http: Handler timeout`, and `context deadline exceeded`. Kernel logs show master memory pressure killing workload `bird` processes during the failure window, and later repeated `k3s-server` OOM kills around 9GB RSS on a 12Gi master VM. The K3s datastore is about `1.7G`.
- Notes: this was not a placement-generation failure and not a single bad Pod. The direct failure was deploy pressure-check/API unavailability; the underlying issue is K3s embedded datastore/apiserver overload at about 13.8k workload Pods plus many controller/event/token objects on the 48VM/12Gi-master configuration. Because the run stopped in deploy, wait-ready, BIRD/FIB, and ping/iperf dataplane stages did not run.

## 2026-06-30 19:45 - 15000 48VM Master48G Optimized Full Workflow Passed

- User intent: increase the master VM memory to at least 48GiB, rerun the largest unscaled `real_topology.txt` using optimized placement only, and include static plus ping/iperf dataplane measurements.
- Scope: `generateVmMatrixConfigs.py`, regenerated b63 VM matrix configs, and run `experiments/15000_20260630_095613_48vm_master48g_optimized_full/`.
- Changes: added a master minimum memory split in `generateVmMatrixConfigs.py` so 18/24/36/48VM configs reserve at least `49152` MiB for the master while preserving the 600GiB total memory budget; regenerated matrix configs and verified the 48VM config master memory is `49152` MiB.
- Commands: ran `python3 generateVmMatrixConfigs.py`; ran `python3 -m py_compile generateVmMatrixConfigs.py`; launched `runB63LargeExperiment.py --topology-size 15000 --kvm-config ./vm_matrix_configs/configkvm_b63_48vm.yaml --destroy-existing-cluster --algorithm optimized --fast-greedy-score --placement-selected-only --include-master --network-backend macvlan --attached-cni-type macvlan --cni-type macvlan --cni-master-interface ens2 --deploy-batch-size 50 ...`; monitored `summary.json`, BIRD/FIB summaries, `output/placement_report.json`, and `dataplane/latency_throughput_summary.json`.
- Validation: full workflow passed. Key stage durations were cluster build `2467.39s`, compile `1430.21s`, placement `272.73s`, build `945.22s`, deploy `18582.70s`, wait-ready `74.85s`, start-bird `454.52s`, start-kernel `6951.79s`, verify-after-FIB `116.26s`, and dataplane `2303.93s`. All `15265` Pods reached Running/Ready; start-BIRD, start-kernel, verify-after-start-BIRD, verify-after-FIB, and dataplane all passed.
- Static results: placement report records `resourceRequestsInjected=false`, `fabricAssumption=macvlan-vlan`, objective `546.66`, VLAN parent total `25441`, VLAN parent max per node `1179`, dominant CPU utilization average `12.57%`, and dominant max `29.50%`. The master received `796` Pods, far above the worker range of about `302..327`.
- Dynamic results: deterministic dataplane plan used 192 directions across 96 pairs. Ping succeeded `129/192`; eBGP IX peer ping succeeded `64/64`, intra-AS link `60/64`, and cross-AS multihop only `5/64`, so the overall ping failure rate is dominated by unreachable cross-AS multihop samples. Baseline TCP succeeded `31/48`, aggregate goodput was `289000.96 Mbps`, and fairness was `0.9650`. Stress aggregate goodput at `8/16/32/48/64` concurrency was `47.80/81.23/112.86/151.52/163.52 Gbps`; loaded RTT averages were `0.685/1.089/1.462/2.221/2.474 ms`.
- Notes: increasing master memory fixed the previous deploy/API/OOM failure, but the main bottleneck moved to master CPU/load. The master start-kernel node-local step took `6924.07s` versus roughly `170..196s` on workers, ending with load `464.00 476.45 505.90`; a post-run SSH `uptime` check still timed out during banner exchange. This run demonstrates the full flow can pass at 15000 scale, but the placement objective should cap or penalize master router load, and dataplane pair selection should separate verified reachable direct/eBGP paths from cross-AS multihop reachability probes.

## 2026-07-02 12:55 - Destroy Current B63 K3s Cluster

- User intent: stop the currently running b63 K3s cluster and clear the namespace/resources; direct destroy was acceptable and preferred if faster because the final state should be destroyed.
- Scope: live libvirt domains matching `seedemu-b63-placement-*`, `configK3s-b63.yaml`, and the active 15000/48VM master48G b63 cluster.
- Changes: no code changes. Started the supported `python3 ./k8sTools.py destroy -d ./configK3s-b63.yaml` path, then interrupted it because it was still doing slow per-node K3s/CNI cleanup while all 48 VM domains still existed. Switched to direct libvirt destroy for only the b63 domains.
- Commands: ran `python3 ./k8sTools.py destroy -d ./configK3s-b63.yaml` and interrupted it after confirming the slow cleanup path; ran a force cleanup loop over `virsh list --all --name | awk '/^seedemu-b63-placement-/ {print}'`; for each matched domain, ran `virsh destroy` when running and `virsh undefine --remove-all-storage`.
- Validation: `virsh list --all --name | rg '^seedemu-b63-placement-' | wc -l` returned `0`; `pgrep -af '[q]emu.*seedemu-b63-placement'` returned no b63 qemu processes; no `k8sTools.py destroy` or b63 SSH cleanup process remained. `bird-rtnl-dev` was still present and was not touched. Host memory after cleanup was `1.0Ti total`, `200Gi used`, `857Gi free`, and `806Gi available`.
- Logs: slow cleanup log is `cleanup_logs/destroy_20260702_123928.log`; force libvirt cleanup log is `cleanup_logs/force_libvirt_destroy_20260702_125356.log`.

## 2026-09-14 18:00 - B63 复用 B62 主流程与 6 VM / 1078 验证

- User intent: 对齐 B63/B62，B63 只增加分区与 Pod/node 约束；沿用 B62 单 YAML 结构；用 6 VM、1078 规模验证，每台 24 GiB、master 16 CPU、workers 8 CPU，避开已有 VM 与清理任务。
- Scope: `runB63LargeExperiment.py`、`runPlacementExperiment.py`、新增 `verifyPlacement.py`、`configSingleHostKvmMacvlan.yaml`、B63/B62 README、B62 `runFullExperiment.py` 可选回调、`tests/seed_k8s_b63_shared_test.py`。
- Findings: 原 B63 使用复制的 running 脚本，与 B62 编译/镜像适配、运行参数、验证阶段已分叉。改为直接调用 B62 主流程，编译后替换 nodeSelector，wait-ready 后逐 Pod 验证实际落点。保留 B62 by-AS manifest 作为基线。历史 running/ 中 20 个共用阶段/helper 已改为 B62 转发，余下专用历史文件和矩阵文件未删除，新主入口不再调用；旧矩阵 CLI 尚不适用于新主入口，README 已明确迁移边界。
- Isolation: 执行前发现 B62 的 48 台 VM 仍在运行，4954 实验 namespace 正在清理；只创建 `b63-sh6-*` 六台 VM、`10.83.0.0/24` 管理网与两条独立 L2 fabric；总 56 vCPU/144 GiB。VM 磁盘为独立目录。cluster state/kubeconfig/inventory 均位于本次运行目录，没有调用已有集群的 destroy。执行前资源快照保存为本次运行的 `resources_before.json`。
- Commands:
  - `rg --files`、`rg -n`、`sed`/`cat`、`git status --short`：检查规范、调用链、脚本引用、已有未提交变更。
  - `virsh list --all`、`virsh net-list --all`、逐 VM `virsh dumpxml`、`ip -4 route`、`ip -j link`、`free -h`、`uptime`、`df -h /data/lxl`、进程列表：检查已有资源与清理；新名字、MAC 前缀、bridge、存储路径均无冲突。
  - `python3 -m py_compile runB63LargeExperiment.py verifyPlacement.py runPlacementExperiment.py ../b62_k8s_scale/runFullExperiment.py`：语法检查。
  - `python3 runB63LargeExperiment.py --prepare-only --run-dir experiments/1078_shared_prepare`：无 VM 副作用生成配置，资源汇总为 6 VM / 56 CPU / 144 GiB。
  - `python3 ../../tests/seed_k8s_b63_shared_test.py -v`：5 项测试通过；阶段顺序、失败停止、内存/隔离配置、实际落点拒绝错误/缺失/重复/未就绪、placement 保持镜像与网络不变。
  - `PYTHONPATH=../b62_k8s_scale:../.. python3 -m unittest tests.seed_k8s_singlehost_macvlan_test.SeedK8sSingleHostMacvlanTest.test_single_config_rejects_duplicate_count_and_invalid_size tests.seed_k8s_singlehost_macvlan_test.SeedK8sSingleHostMacvlanTest.test_single_config_rejects_mixed_inputs_before_execution tests.seed_k8s_singlehost_macvlan_test.SeedK8sSingleHostMacvlanTest.test_legacy_assignment_entry_still_uses_explicit_assignment -v`：3 项 B62 入口回归通过。
  - `python3 runB63LargeExperiment.py --config configSingleHostKvmMacvlan.yaml --run-dir experiments/1078_6vm_shared_validation`：实际完整实验；外层控制台暂写 `/tmp/b63-shared-validation.console.log`，每阶段日志与 summary 位于运行目录。
- Validation: 静态/入口检查通过；实际实验已完成且 PASS，完整结果见下方最终验证记录。
- Notes: 未运行历史矩阵/default-scheduler 动态基线；本任务验证硬 placement。未销毁或清理其他集群；本例 VM 完成后保留供检查。已有 B62 配置在任务前已被用户改为 48 VM，其旧测试中固定资源预期不适合本次配置，因此只运行与入口变更相关的回归项。

### 实验发现与修复

- 首次 build 在 6.35 秒失败：B62 `build.sh` 远端探测硬编码 `127.0.0.1:5000`，与本例 `registry.port: 5063` 不符。修复为使用传入的 `SEED_REGISTRY_LOCAL_ENDPOINT` 或 `SEED_REGISTRY`；B62 的默认 5000 配置仍使用同一路径。
- 同时将 build 的控制端临时压缩包/脚本改为运行目录内 `mktemp` 独立目录，远端目录按 namespace 隔离，避免并行例子争用 `/tmp/compiled.tar.gz` 等固定文件。
- 共用 runner 新增 `--resume-from build`，只恢复镜像构建及之后阶段；B63 要求已有 placement report，仍在 wait-ready 后强制核对实际落点。恢复读取运行目录的固定输入，不依赖原 YAML 仍然存在。
- 恢复命令：`python3 runB63LargeExperiment.py --run-dir experiments/1078_6vm_shared_validation --resume-from build`，控制台为 `/tmp/b63-shared-validation.resume.log`。原失败记录保留在 summary 中，恢复后的 build 已通过（215.42 秒）。
- `running/` 的 20 个共用 shell/Python 文件改为转发到 B62，原实现约 6996 行变为 250 行；原始任务前内容暂存 `/tmp/b63-refactor-before/running/`。`running/lib.sh` 和 B63 专用历史文件不自动删除。shell 转发必须提供有 assignment.yaml 的运行目录，缺失参数返回 2，避免误用 B62 默认集群。
- 验证：所有 `running/*.sh` 的 `bash -n`、所有 `running/*.py` 的 `py_compile`、三个 Python helper 的 `--help`、缺少运行目录的 clean wrapper 拒绝执行均通过。新增恢复流程失败门槛测试，B63 针对性测试现为 6 项通过，另有上述 3 项 B62 入口回归通过。
- 实际 manifest 审计：1078 个 workload / 2143 个文档，除 hostname 约束和 `seedemu.k8s.placement/*` 注解外，其他 manifest 内容零差异；见运行目录 `reuse_audit.json`。`vm_resources_actual.json` 通过 libvirt XML 确认实际 16/8 CPU 与全部 24 GiB 内存。
- 当前部署分布：master 244，worker1 166，worker2..5 各 167；共 1078。K3s 六个节点全部 Ready。

### 最终验证结果（18:22）

- 完整实际实验最终 `summary.json.status=PASS`。建群 340.58s，编译 10.76s，placement 4.76s，修复后 build/preload 215.42s，deploy 238.79s，wait-ready 154.15s。首次 build 的 FAIL 和恢复后的 PASS 均保留，未改写失败历史。
- 全部 1078 Pod Ready；逐 Pod 实际 node 与分区计划完全一致，失败数 0。BIRD 启动 1078/1078 成功；首次协议验证固定 10 AS 通过（邻居收敛等待 123s，验证最大 VM load1=6.86）。全部 82 AS 的 RIB/FIB 检查通过（load1=0.76）。跨节点 ping/iperf 检查通过；reconvergence 通过，报告 recovery=42.782s。
- 故障恢复后使用新 Python 转发入口复查：`python3 running/verifyStartBird.py --namespace seedemu-b63-sh6-1078 --artifact-dir experiments/1078_6vm_shared_validation/final_checks --inventory experiments/1078_6vm_shared_validation/inventory.yaml --kubeconfig experiments/1078_6vm_shared_validation/kubeconfig.yaml --load-threshold 50` 通过 10 AS，load1=0.92。
- `python3 running/verifyFibRoutes.py` 使用上述相同路径参数、`--load-threshold 50 --sample-as-count 0`，恢复后全部 82 AS 通过，load1=0.59。直接 helper 的默认 sample-as-count 是 10，因此首次复查后补充显式 0 的全 AS 检查，最终保存全 AS 结果。
- `python3 verifyPlacement.py --run-dir experiments/1078_6vm_shared_validation --kubeconfig experiments/1078_6vm_shared_validation/kubeconfig.yaml --namespace seedemu-b63-sh6-1078` 最后再次确认 1078/1078 Ready 且落点一致。
- 资源复查最初以“原 VM 全部仍存在”为断言，因此报告不通过；查阅原 B62 的 `cleanup_summary.json` 后确认这是既有独立清理流程正常完成：final-clean 结束 18:01:50，final-destroy 结束 18:04:38，48 台 B62 VM/3 个 B62 网络被该流程删除，清理验证明确保留了本例六台 VM、bird-rtnl-dev 和其余网络。本轮未执行任何 B62 destroy 命令。`resources_after.json` 记录该外部变化及证据路径。
- 最终保留六台 b63-sh6 VM 与本次 namespace；原 `bird-rtnl-dev` 仍为 shut off，历史 B63 trunk 网络保留。新集群状态只位于本次运行目录。
- 报告：`experiments/1078_6vm_shared_validation/summary.json`、`reuse_audit.json`、`placement_actual_summary.json`、`final_checks/`；外层控制台已复制为运行目录下的 `initial.console.log` / `resume.console.log`，最终复查日志也已复制。
- 文档：同步更新 `../B62B63RunGuide.md` 的 B63 配置、执行顺序、状态路径、清理和恢复命令；保留其 B62 历史配置说明。
- 未运行/边界：旧矩阵 CLI 与动态 default-scheduler 基线没有迁移或运行，使用新 YAML 硬 placement 主流程；历史专用文件保留，不自动删除。没有提交 git commit，也没有销毁本次验证集群。

## 2026-09-15 09:32 - 复核 B62 / B63 的 7001 实验

- User intent: 检查用户已经执行的两轮实验是否异常，并比较所有阶段运行时间。
- Scope: B62 runs/7001_20260914_211251_singlehost_macvlan_vlan 与 B63 experiments/0914test7001 的已有记录；只读检查当前 libvirt/磁盘状态。
- Changes: 新增 experiments/0914test7001/comparisonB62B63.md 与 comparisonB62B63.csv；追加本工作日志；未修改实验代码、配置或原始记录。
- Commands: 用 rg / Python 读取两轮 summary、冻结配置、各阶段日志、逐节点 BIRD/kernel 汇总、placement/static validation、canonical 哈希、数据面及 reconvergence 记录；用 virsh list --all、virsh net-list --all 和 Python 目录检查复核实验资源清理状态；用 Python 从 summary 生成完整阶段 CSV/Markdown 并核对合计。
- Validation: 两轮 PASS，7001 Pod/BIRD/kernel 全部成功，949 AS 代表路由器 FIB 检查通过；B63 实际落点验证通过。统一从建群计时 B62 5472.42s、B63 6103.50s，B63 +631.08s（11.53%）；master 515 Pod 与节点内串行执行构成性能关键路径。实验对应 VM/网络不存在、磁盘与 cloud-init 目录为空。
- Notes: B62 实验前旧 master SSH 超时后 KVM 清理校验通过；外部 destroy 无阶段耗时。流对不同，吞吐量不能直接比较。未重新运行测试/实验，也未执行 build/deploy/destroy，因为任务为已有记录审计。

## 2026-09-15 11:44 - Pod/VLAN 分布统计

- User intent: 给出 B62/B63 Pod 与 VLAN 节点分布的均值、方差，分析 B63 变慢原因。
- Scope: 只读取两轮 7001/48VM 实验记录。
- Changes: experiments/0914test7001/distributionB62B63.md/.csv/.json/.png/.pdf 与 distributionStatsB62B63.csv，提供逐节点计数、节点耗时、总体和 worker 子集统计。
- Commands: rg 检查 deploy 的 VLAN 日志；Python 解析节点汇总、正则提取 VLAN planned 条目，用 statistics.mean/pvariance/pstdev 计算；matplotlib 生成分布图。
- Validation: 两轮各 48 个唯一节点且 Pod/VLAN 节点集合一致；Pod 合计均为 7001，VLAN 合计 10061/12960；B63 实际落点与 Pod 统计一致、placement report 与 VLAN 统计逐节点一致。
- Notes: 方差为总体方差（除以 N）、不按节点容量加权；VLAN 数为成功部署的计划条目，不是当前接口现场采样；未改代码、配置或重新运行实验。

## 2026-09-15 13:27 - 吞吐 RTT 与 stress48 记录复核

- User intent: 比较本次 B62/B63 的吞吐、RTT、stress48 TCP 流速。
- Scope: 两轮 7001/48VM 的原始记录及当前共享主流程。
- Changes: 新增 experiments/0914test7001/tcpRttComparison.md 和 tcpRttComparison.csv。
- Commands: Python 读取 cross_node_workload_summary.json，计算两方向吞吐/普通 RTT/DF RTT 差异；rg 搜索 stress/latency/throughput 文件并检查 runFullExperiment.py、runB63LargeExperiment.py、validateCrossHostWorkload.py 和历史 measureDataplane.py。初次查找 validateCrossNodeWorkload.py 路径不存在，随后确认实际文件为 validateCrossHostWorkload.py。
- Validation: 两轮只有一对 Pod 的两个方向各 5 秒单流 TCP 与 ping，全部通过；没有 stress48 阶段或结果。当前入口未接入历史多流压力测试。
- Notes: 流对不同，数值变化不能解释为算法因果效果；不能把单流结果乘以 48。未修改脚本或启动已销毁集群补测。

## 2026-09-15 14:51 - B63 默认接入完整数据面测量

- User intent: 在 runB63LargeExperiment.py 默认调用 measureDataplane.py，除非显式传 --skipMeasureDataplane。
- Scope: B63 入口、B62 共享 runner 的可选结束回调、B62/B63 README、B62B63RunGuide.md 和 tests/seed_k8s_b63_shared_test.py。
- Changes: 新增 after_workload 可选回调，在正常/恢复工作流成功后、最终 PASS 前执行；B63 注册 measureDataplane，使用解析后的当前 namespace、kubeconfig、topology 路径和运行目录。默认 RTT、48 方向基线 TCP、8/16/32/48 并发各 3 轮，每流 15 秒。dataplane 阶段统一记录命令、日志、性能采样、耗时和 tests.dataplane。跳过参数记录 SKIPPED，prepare-only 不运行；异常退出使整个流程 FAIL。B62 默认不传回调，行为保持原样。
- Commands: rg/cat/sed 检查入口、测量参数、共享状态处理和文档；git status --short 区分用户已有修改；python3 -m py_compile runB63LargeExperiment.py ../b62_k8s_scale/runFullExperiment.py ../../tests/seed_k8s_b63_shared_test.py；python3 runB63LargeExperiment.py --help；git diff --check；python3 ../../tests/seed_k8s_b63_shared_test.py DataplaneWorkflowTest SharedWorkflowTest.test_hooks_run_before_preload_and_before_bird SharedWorkflowTest.test_failed_placement_never_preloads_or_deploys SharedWorkflowTest.test_resume_build_preserves_placement_and_still_gates_routing SharedWorkflowTest.test_live_check_rejects_wrong_missing_extra_and_unready_pods SharedWorkflowTest.test_pinning_preserves_networks_images_and_resources -v。
- Validation: 10 项定向测试全部通过，包含默认/跳过/prepare-only/测量失败/冻结配置恢复，以及原有 placement 和 BIRD 门控回归；语法和 CLI 检查通过。测试替换集群和子进程操作，未创建 VM、部署或压测。
- Notes: 保持 measureDataplane.py 原语义，逐流失败计入报告，不新增失败率门限；PASS 表示测量正常完成。原 test_compact_memory_and_isolation 固定断言 6VM/1078-era 资源，而用户现有配置为 48VM，未运行此无关固定配置断言、未更改用户配置。无新增或删除历史脚本，无 git 提交。

## 2026-09-15 16:08 - 6VM / 1078 默认 dataplane 实际全流程通过

- User intent: 运行 6VM、1078 B63 例子验证默认接入 measureDataplane 后代码正常工作。
- Scope: 新运行目录 experiments/1078_6vm_dataplane_validation_20260915，独立 b63-dp6-* VM/网络/存储。
- Changes: 新增 input.yaml、resources_before.json、vm_resources_actual.json、final_runtime_check.json 和 validationReport.md；实际运行生成全部阶段和 dataplane 产物。未修改用户 48VM 默认配置或代码。
- Commands: virsh list/net-list、ip route/link、ss、free/df/uptime、进程检查确认隔离与余量；Python 生成独立配置；在 seedpy310 中执行 python3 -u runB63LargeExperiment.py --config experiments/1078_6vm_dataplane_validation_20260915/input.yaml --run-dir experiments/1078_6vm_dataplane_validation_20260915（console.log 保存输出）；通过 virsh dumpxml 检查 6 台 VM 的实际 24GiB/16或8CPU；读取各阶段汇总和 stress 原始结果；kubectl get pods/nodes 和六节点 SSH cat /proc/loadavg 完成最终检查。
- Validation: 全流程 PASS / rc0，15:17:43→16:07:05，共49分22秒；dataplane 默认执行1229.41秒，8/16/32/48各3轮全部完成、flowCount符合档位、missingDirections=0，主 summary 收录测量汇总。1078 Pod/BIRD/kernel 全成功；10AS协议检查和82AS FIB通过；跨节点验收和reconvergence通过，恢复42.706秒。最终1078/1078 Pod及6/6节点Ready，VM最大load1=0.39。
- Measurement findings: 多流ping144/192通过（AS内64/64、IX64/64、多跳16/64）；基线TCP32/48成功，失败均跨AS；stress48成功数34/48、36/48、33/48，吞吐49.307/36.811/45.561Gbps，平均43.893Gbps。对一个失败方向执行 kubectl exec ... -- sh -c 'ip route get 1.17.7.49; birdc show route for 1.17.7.49 all'，发现目标无BIRD路由、走管理eth0默认网关，命令退出1符合Network not found；没有过滤失败样本。
- Notes: 保留本轮6台VM以便复查；原bird-rtnl-dev与历史网络仍在，未执行destroy。流程PASS表示测量正常完成，不代表跨AS样本全部可达；详见validationReport.md。尝试读取尚未生成的kernel逐节点日志时文件不存在，随后阶段正常完成，未因此修改代码。

## 2026-09-15 17:05 - 清理6VM并增加静态placement门控与历史复现

- User intent: 清理上次实验环境，在B63入口增加静态验证，并用之前实验检查复现。
- Scope: b63-dp6的6台VM；B63入口、新verifyStaticPlacement.py、B62 before_build可选回调、测试与README/运行指南；离线复现1078/6VM和7001/48VM。
- Changes: 默认verify-static-placement在placement之后、build之前执行，从build恢复也执行；验证分配覆盖/节点资格、manifest、重算静态指标，输出逐节点Pod/VLAN均值/总体方差。仅复用现有模型，不改分区算法。
- Commands: python3 k8sTools.py destroy -d experiments/1078_6vm_dataplane_validation_20260915/configK3s.yaml --keep-temp，退出0；virsh list/net-list与Python目录检查确认6台VM/3网络和磁盘清空、其他资源保留。Python生成独立重放目录，按replay_command.json调用runPlacementExperiment.py，无--deploy；verifyStaticPlacement.py检查原始及重放产物，Python比较映射/指标/manifest。
- Validation: python3 -m py_compile runB63LargeExperiment.py verifyStaticPlacement.py ../b62_k8s_scale/runFullExperiment.py ../../tests/seed_k8s_b63_shared_test.py通过；python3 ../../tests/seed_k8s_b63_shared_test.py StaticPlacementTest DataplaneWorkflowTest SharedWorkflowTest.test_static_failure_blocks_preload_on_fresh_and_resumed_runs SharedWorkflowTest.test_hooks_run_before_preload_and_before_bird SharedWorkflowTest.test_failed_placement_never_preloads_or_deploys SharedWorkflowTest.test_resume_build_preserves_placement_and_still_gates_routing SharedWorkflowTest.test_live_check_rejects_wrong_missing_extra_and_unready_pods SharedWorkflowTest.test_pinning_preserves_networks_images_and_resources -v共16项通过；git diff --check通过。原固定6VM配置断言不适用用户当前48VM配置，未运行。
- Reproducibility: 1078系统Python3.12重放落点和指标一致、manifest字节一致；7001系统Python3.12重放有6303落点变化/921指标差异，检查发现节点顺序与容量相同；改用历史seedpy310 Python3.10.19命令后完全一致。首次不同环境的结果保留，未掩盖差异。报告experiments/placement_static_replay_20260915/reproducibilityReport.md及各reproducibility_summary.json。
- Notes: 初版检查器调用了不存在的loadYaml，已改为yaml.safe_load；7001旧inventory已被清理，使用保留配置fallback；一次重放未完成即触发检查返回缺失manifest，保留missing_artifact_check.json并在完成后重新验证。本轮未启动新集群、未重测动态吞吐/RTT，不能据静态复现声称动态数值复现；未修改默认48VM配置或删除历史记录，未提交commit。

## 2026-09-15 18:00 - 按历史配置实际重跑6VM/1078并逐流核对

- User intent: 根据历史结果检查并实际验证可复现性；承接此前清理环境、加入静态placement验证的任务。
- Scope: 参考 experiments/1078_6vm_dataplane_validation_20260915，新建 experiments/1078_6vm_reproduction_20260915，完整建群/placement/静态门控/部署/BIRD/FIB/reconvergence/默认dataplane。没有新增B62动态对照或7001实机重跑。
- Changes: 未改生产代码或用户48VM默认配置；复制历史input.yaml，新增资源/环境指纹、离线对比脚本compareHistorical.py、最终现场检查脚本checkFinalRuntime.py、报告生成脚本writeComparisonReport.py，以及JSON/CSV/Markdown证据。
- Commands: virsh list/net-list/dumpxml、ip route/link、资源检查，确认仅bird-rtnl-dev关闭及历史trunk网络，b63-dp6命名/地址/存储无冲突。激活seedpy310后执行 python3 -u runB63LargeExperiment.py --config experiments/1078_6vm_reproduction_20260915/input.yaml --run-dir experiments/1078_6vm_reproduction_20260915，输出console.log。执行compareHistorical.py、checkFinalRuntime.py和writeComparisonReport.py；通过kubectl exec复查历史失败方向。清理命令 python3 k8sTools.py destroy -d experiments/1078_6vm_reproduction_20260915/configK3s.yaml --keep-temp，日志destroy_after_reproduction.log。
- Validation: 全流程PASS/rc0，17:13:58→17:58:35（44分37秒），历史49分22秒。新verify-static-placement自动执行PASS/7.05s；部署manifest字节一致、落点变化0、静态指标及Pod/VLAN均值和方差一致。静态VLAN标准差仅1.42e-14舍入差（历史静态报告由Python3.12生成，本次3.10）；报告明确采用1e-9浮点容差。拓扑/BIRD/kernel三个哈希一致。BIRD和kernel全部1078成功，固定10AS协议检查、82AS FIB检查通过；恢复42.769s（历史42.706s）。最终1078/1078 Pod、6/6 Node Ready，VM最大load1=1.03。
- Dataplane: 相同96对/192方向，12轮的TCP及canary选择相同；所有普通ping、基线TCP、312个stress TCP尝试和loaded ping逐方向成败一致。ping144/192成功，基线TCP32/48成功，stress48各轮34/48、36/48、33/48。平均RTT6.559→6.358ms（-3.06%）；成功基线TCP均值5.039→5.440Gbps（+7.95%）；stress48三轮平均43.893→44.310Gbps（+0.95%），loaded RTT33.389→29.530ms（-11.56%）。各档吞吐均值变化+0.75%/+4.88%/+2.75%/+0.95%。13项报告复现断言及canonical哈希、每轮流数/312总数断言通过。
- Findings: 跨AS不可达历史问题也复现。方向0c50b6e1c40daec6-a（as1372r6→as1841r17目标1.17.7.49）现场BIRD仍Network not found，ip route get走管理eth0。只对该样本断言路由原因，不推广至全部失败。loaded ping沿用16方向、8并发、15次/1秒间隔，第二批可能在15秒TCP结束后采样；报告注明其不是严格全程同步满载RTT。单次重建重跑不足以证明长期统计稳定，未事后制定性能等价门槛。
- Notes: 初次查找测量脚本误用B62/running路径（不存在），随后确认B63/running/measureDataplane.py并补充哈希；建群未完成时inventory尚未生成，不影响主流程。失败方向首次在基线TCP子集查找未找到（StopIteration），随后在完整ping集合按directionId定位并保存failed_direction_route_check.json。原始历史及失败记录保留，未提交git commit。
- Final cleanup: destroy退出0，cleanup_after_reproduction_summary.json=PASS；6台b63-dp6 VM/3个网络删除，/data/lxl/k8sTools/b63-dp6/{disks,cloud-init}为空；bird-rtnl-dev仍shut off，default及3个历史B63 trunk网络原样保留。最终报告已更新清理状态，原始实验记录保留。

## 2026-09-16 11:19 - Historical B63 TSV Summary With Resource Checks

- User intent: 汇总历史 B63 各分区算法的表现，核对总资源是否一致，输出 TSV 到 /home/lxl/k8s。
- Scope: 只读 experiments 中的历史 summary、placement_report、dataplane、配置快照和 VM 资源记录；不启动新实验、不操作集群、不改原始结果或生产代码。
- Outputs: /home/lxl/k8s/b63_experiment_summary.tsv（46条实验）；b63_algorithm_comparison.tsv（202条动态指标差距）；b63_static_metrics.tsv（178条静态候选/重放）；b63_stress_metrics.tsv（72条并发档位）；b63_resource_nodes.tsv（552条节点资源）；b63_summary_notes.tsv（41条口径说明）。采用 UTF-8 BOM、TAB 分隔，缺失值为 NA。
- Commands: ls/find/git status 定位历史产物与既有改动；python3 内联程序通过 json、yaml.safe_load 解析原始文件，csv.DictWriter 生成 TSV；独立 python3 断言校验 TSV 列数、46条主记录覆盖、逐 VM CPU/内存加总、原始静态及动态指标、改善百分比、压力失败率、公平性和资源分组。
- Validation: 全部汇总断言 PASS；15条记录具备流程/BIRD/FIB PASS 和多流 dataplane。1078 旧实验为26vCPU/38GiB，但五算法只在18vCPU/30GiB的三个worker上运行，真实default还使用master（324Pod），因此对照标记资源池不同。4954两组均12VM/300vCPU/600GiB，节点资源形状与测试方向匹配。9月7001为48VM/306vCPU/600GiB，不能写成300vCPU。
- Corrections: 9月 baselinePlacement 是编译初始方案，最终 staticPlacement/placement报告为 optimized；本次纠正此前答复归类。15000目录实际有15265个manifest工作负载，另列实际数量。default静态结果标记为模拟，未作为真实调度静态实测；selected-only与旧候选择优模式分开。
- Notes: 首轮解析发现 vm_resources_actual.json 存在 list/dict、vcpu/vcpus、memoryKiB/memoryGiB 两类格式，兼容后重跑并验证。未找到的 comparison_summary.json 已改用原始 JSON，不依赖不存在的汇总。默认调度1078缺独立VM配置，沿其明确sourceOutput关联历史资源，表中标记 inferred。fairness另提供失败流记0的重算值；RTT/吞吐保留原测量时序限制。没有修改代码，因此未运行生产测试或新增集群实验。

## 2026-09-16 - Filter TSVs To Paired Comparable Experiments

- User intent: 过滤1078，仅保留可比较规模的有效数据。
- Scope: 更新根目录六个 b63_*.tsv 汇总；不删除或修改 experiments 原始数据，不操作集群。
- Changes: 保留4954/12VM optimized与真实default scheduler两组；均300vCPU/600GiB，节点资源形状、使用节点资源池、测试方向匹配。其余规模缺配对动态基线或按用户要求移除。主表2行，差距表34行，stress表10行，节点资源24行。无真实default静态重算对照，主表静态列置NA，静态表仅保留表头；说明表更新为35行。
- Commands: python3/csv读取并按comparison_type、scale及五个匹配条件筛选TSV；复读验证只含4954、逐VM CPU加总300、内存总量600及资源签名一致。原有ping151/192、TCP35/48及stress失败流统计完整保留。
- Validation: 所有筛选断言PASS。澄清1078其他五种算法实际跑过，差别是未使用master，并非未启动算法。缺测NA不视为0；构建路径差异与测量时序限制仍保留说明。

## 2026-09-16 - Restore Observed Algorithms Including 1078 Network-Only

- User intent: 以实际运行过为准恢复1078各方法，解释network-only为何从上轮表中消失。
- Scope: 从原始JSON/YAML重新生成根目录TSV，不运行新实验、不改原始实验文件。
- Changes: 撤销1078整规模过滤，恢复六算法及其五组optimized对照；资源池差异只作提示。新增b63_1078_algorithm_comparison.tsv，6行精简实测表。总表仅保留19条有数据面观测的记录：15条完整成功多流、1条流程失败但有多流、1条仅ping、2条单对验收。差距表202行、静态所选计划17行、stress72行、逐节点资源238行、说明45行。只存在模拟的default静态指标不填入实测表。
- Commands: python3内联json/yaml/csv重建并筛选已有观测，另生成1078并排表；独立python3读取原始dataplane核对六方法stress48、资源总量及工作负载节点容量、master Pod数和算法覆盖。
- Validation: 断言PASS；network-only与optimized均使用18vCPU/30GiB的三个worker，集群总量26vCPU/38GiB；default使用master且有324个Pod，保留并提示混杂。首次验证发现1078 default没有静态报告时basis未统一，已将所有真实default行统一标记静态实测不可用并重跑通过。流程失败的3083保留诊断数据，但不纳入改善百分比对照。
- Notes: 上轮network-only缺失仅因汇总整体排除1078，原始数据从未删除。其他规模没有独立network-only完整动态实验的，不把离线候选当作运行结果。diff检查覆盖本次工作日志；无生产代码变更或新增集群实验。

## 2026-09-16 - Simplify Successful Experiment TSV Fields

- User intent: TSV只保留规模、算法、VM数、CPU/内存、建群/build含镜像分发/deploy/BIRD/kernel/reconv耗时、RTT/TCP/STRESS、静态指标与日志路径；保留optimized和基线成功运行。
- Scope: 仅整理历史数据与生成表格；未操作集群、未改生产代码或原始实验数据。旧七张宽表移至 /home/lxl/k8s/b63_summary_archive/20260916_114434/，避免同目录混用旧失败记录。
- Outputs: b63_experiment_summary.tsv中文48列、19条成功运行（13条optimized、6条其他基线）；b63_1078_algorithm_comparison.tsv为其中六算法子表；b63_stress_metrics.tsv为67条成功实验的逐并发档位测量。删除无关哈希、候选算法、状态追踪列，保留必要资源/计时/测量口径。
- Selection: summary、BIRD和FIB检查均PASS；15条有完整多流测量，另有1条仅ping、2条单对验收、1条没有保存ping/TCP/STRESS。缺测填NA，并列测量范围，不将这4条当成多流完成。流程失败的3083记录排除。
- Timing: 核对build.log尾部预加载记录。1078 default的up合并build/deploy为6298.71秒，不能拆分；4954 default原始build为46283.07秒，但跳过预加载，分发在deploy拉取，故build含分发列为NA并保留原阶段耗时。复用集群未单独计时的建群及未运行的reconv均NA。
- Metrics: 主表列最高STRESS档位，明细列所有8/16/32/48/64档位；保留成功数、失败率、公平性、丢包和TCP重传。单对验收双向均值标注为单对，不和多流混同。default模拟静态指标排除，其他实际所选placement计划静态指标保留。15000目录按实际15265 Pod列规模。
- Commands: python3内联json/yaml/csv读取原始配置和测量，生成精简表并归档旧表；独立python3逐行核验19条流程及BIRD/FIB PASS、阶段耗时、TCP/静态指标、67档stress、六算法覆盖与两个特殊build口径。全部断言PASS；git diff --check检查工作日志。没有新增实验或生产测试。

## 2026-09-16 11:33 - 三种分区模式串行全流程与自动结果收集

- User intent: 先审阅 runDefaultSchedulerBaseline.py、runPlacementExperiment.py、verifyPlacement.py、verifyStaticPlacement.py，再复用当前 configSingleHostKvmMacvlan.yaml，实现 optimized / K8s默认调度 / Pod数均衡三种可选全流程及串行对比。
- Scope: 新增 runPlacementComparison.py 和 collectPlacementComparison.py；扩展现有 B63 placement/默认调度/验证 helper；B62 build.sh 与 generate_node_image_refs.py 支持全节点预加载；更新 B63/B62 README、B62B63RunGuide.md；新增 tests/seed_k8s_placement_comparison_test.py。未修改用户默认48VM/7001 YAML，未启动VM或动态压测。
- Script review: runPlacementExperiment已有所有分区算法，默认调度算法只是least-allocated静态模拟；runDefaultSchedulerBaseline会清除固定落点/请求，旧CLI自带up/routing但参数未适配共享runner；verifyPlacement原来逐Pod强制匹配固定映射；verifyStaticPlacement原来只验证固定注入及重算指标。新实现复用现有算法和默认调度scrubber，不复制建群/路由/压测流程。
- Changes: 新入口 --algorithm all（默认）依次optimized、kubernetes-default-scheduler、pod-count-balanced；每轮冻结绝对输入路径，复用B63入口跑全部阶段，成功后按本轮configK3s销毁（含最后一轮），任何运行/清理失败停止并保留现场。单算法默认保留集群，可 --destroy-after-pass；prepare-only仅写配置。已有批次目录拒绝覆盖、allowExisting=true拒绝采用已有VM。汇总每轮更新JSON/CSV/Markdown，保留失败/未完成状态，比较逻辑流和stress/canary选择；不把缺失测量当0。
- Default scheduler: 复用scrubber解除固定落点，保留原requests/网络/镜像，设置default-scheduler；excludeControlPlane时只限制合格节点集合。静态结果标记scheduler-simulation。共享B62构建从冻结YAML选择--all-nodes镜像预加载；原VLAN部署已支持无固定落点时展开到全节点。实际验证不比较模拟落点，检查合格节点、Ready、数量及scheduler，再按真实映射重算Pod/VLAN分布；同时记录已配置VLAN与按落点推算的使用VLAN，避免混淆。
- Commands: rg/cat/sed读取四个指定脚本、共享runner/build/deploy/lib、项目文档与规范，git status/diff区分已有用户变更；python3 -m py_compile检查变更Python，bash -n检查B62 build.sh，git diff --check通过。runPlacementComparison.py --help检查CLI。使用seedpy310执行 runPlacementComparison.py --config configSingleHostKvmMacvlan.yaml --prepare-only --run-dir experiments/placement_comparison_prepare_20260916，三轮PREPARED；collectPlacementComparison.py --run-dir该目录，状态PREPARED/samples=INCOMPLETE符合未测量语义。
- Tests: 系统Python与seedpy310均通过12项新测试（默认调度解除pin且保留requests、静态/实际门控、全节点含init image预加载、异构节点Pod数均衡、串行清理/失败停止/保留现场/目录保护/路径冻结、汇总真实分布/样本差异/零节点统计）。seed_k8s_b63_shared_test.py中StaticPlacementTest、DataplaneWorkflowTest及6项SharedWorkflow定向测试共16项通过。旧固定6VM配置断言仍不适用于当前48VM用户配置，未运行或改动。
- Offline validation: experiments/placement_comparison_offline_20260916，以历史1078的k8s.b62.yaml及6VM配置分别运行真实分区CLI/静态验证/预加载计划/VLAN生成helper，三种均PASS。optimized分布244/166/167/167/167/167；pod-count-balanced为180/180/180/180/179/179。默认调度manifest不含固定nodeName/hostname selector，全部节点预加载1078镜像、每节点准备1065 VLAN parents。默认调度静态预测266/162/162/163/163/162仅为模型，未当成现场结果；另外用明确标记synthetic的Pod样本验证观测分布可不同于预测，三种actual模型重算PASS。offline_validation.json与observed_model_validation.json、验证脚本和测试日志均保留。
- Corrections during validation: 初次均衡测试fixture漏NodeInfo.role，补齐后通过；历史inventory已被destroy删除，离线回放改用保留cluster-input的节点容量fallback。最初将同名镜像helper补丁落在lxl/test，随后核实B62 lib.sh的TEST_DIR后迁移至实际B62 helper，精确撤回本次在lxl/test的修改，重跑正确helper测试及1078预加载计划验证。两次查找历史helper路径不存在，已定位manageK8sManifest.py及B62 fastSeedemuImageArchive.py。
- Notes: 生产算法未重写，历史脚本保留；没有使用旧runVmScaleMatrix.py的失效参数。无实际48VM全流程/真实K8s默认调度运行证据，本次完成代码、配置准备、模拟流程测试和真实静态产物回放。未提交git commit。手动恢复子运行不会改写原失败批次状态或自动启动后续算法，文档说明用新目录执行剩余轮次。

## 2026-09-16 12:01 - 4954规模三算法实际串行对比（运行中）

- User intent: 实际运行4954规模optimized、K8s默认调度、Pod数均衡，汇总静态/动态性能并提供本轮命令。
- Configuration: 冻结当前48VM配置至 experiments/4954_comparison_20260916_setup/input.yaml，仅修改拓扑规模/name/namespace为4954并解析绝对路径；306vCPU、600GiB，master24CPU/36GiB、47worker各6CPU/12GiB。原始用户YAML未改动。
- Isolation: 启动前只有bird-rtnl-dev（30CPU/200GiB）运行，保留其default网络及已有3个trunk网络；本轮10.83.0.0/24、b63-sh48名称/磁盘目录无占用。主机384CPU、可用778GiB，存储40TiB可用。resources_before.json保存原始状态。
- Command: /home/lxl/anaconda3/envs/seedpy310/bin/python -u runPlacementComparison.py --config experiments/4954_comparison_20260916_setup/input.yaml --algorithm all --run-dir experiments/4954_48vm_comparison_20260916 > experiments/4954_comparison_20260916_setup/batch.console.log 2>&1
- Execution: 默认保留静态验证/真实落点验证/BIRD/FIB/重收敛及RTT/TCP/stress8,16,32,48（每档3轮）；每种算法独立新建并在成功后销毁集群。开始时间12:01:18+0800，完成结果后续追加。

- First result (13:47): optimized 4954/48VM全流程PASS，耗时1h46m07s。静态/实际落点4954个Pod全部匹配，10AS协议检查PASS、674AS FIB检查PASS、重收敛PASS。Pod均值103.2083/总体方差1274.9566；VLAN实际需要与部署计划均9285，总体均值193.4375/方差4022.7461。ping152/192、成功方向平均4.46995ms；基线TCP35/48、成功流均值8024.823Mbps；stress48均170282.450Mbps，成功38/42/33流，平均失败率21.5278%。40个ping失败均cross_as_multihop；只读抽查0021731254aa8c9c-b源端birdc show route for 1.73.4.253返回Network not found，证据failed_direction_route_probe.json，未更改路由。
- Final-state gate: 新增本批次专用monitorComparison.py，仅在dataplane运行时SIGSTOP批次父进程（测量子进程与VM不暂停），待子流程结束复用B62 waitForClusterLoad，再SIGCONT父进程进行原有cleanup；实际VM资源与final_load_gate.json保留在各轮目录。optimized测后48VM最大load1=1.45，PASS后13:47启动清理。观察器命令保存在setup/commands.log，无生产代码修改。

- Recovery (14:43): 第二轮default静态验证PASS、build失败，原因add-apt-repository请求Launchpad返回HTTP504（该步骤等待247秒）；不是调度失败。保留comparison_runs.initialFailure.json、summary.json.beforeBuildResume、build.orchestrator.log.beforeBuildResume及原console。专用resumeComparison.py从现有集群--resume-from build恢复，随后执行负载门控/cleanup，再创建pod-count-balanced；各attempt命令/退出码单独记录，不隐去原失败。原批次与观察器已退出，恢复协调器自行执行测后负载检查，无SIGSTOP需要。实际命令见setup/commands.log。

- Recovery resolution (14:48): build第一次恢复成功，Launchpad暂时故障自行恢复；开始生成每节点4954镜像归档（约471MB/节点）。期间只读检查主机缓存BIRD镜像为2.17.2并导出其PPA签名源作为备用诊断，但没有注入或替换镜像、没有修改Dockerfile、未启用备用源。实验继续使用原始构建逻辑与原始包源。

- Checkpoint (16:38): optimized清理PASS；default建群1655.9s、编译673.4s、静态验证PASS，build第一次HTTP504后resume成功（同一PPA请求本次150s完成）；目前全量预加载28/48节点完成，每批4节点约14min。默认每节点4954镜像、累计237792镜像到节点项；optimized累计4954项。全量导入明显拉长准备时间，保持参数不变，不把这部分算作调度算法计算时间。没有修改生产代码、用户YAML或Dockerfile。主机负载约13、无swap，不相关VM保留。恢复协调器继续负责完成default测量/负载检查/清理，再启动pod-count-balanced。

- Second result (20:37): default恢复后全流程PASS；13:56:12至20:37:04共24052s（含首次失败与恢复间隔），成功build含全节点分发10828.35s、失败build379.68s、deploy8357.30s。实际4954Pod均Ready；Pod均值103.208333/总体方差579.414931；所需VLAN11386个，均值237.208333/方差2037.373264。无pin模式为每节点预配置4763VLAN，共228624个；这与实际落点所需11386个分开统计。静态/真实落点/10AS协议/674AS FIB/重收敛均PASS。RTT152/192、均值5.206526ms；基线TCP35/48、成功流均值7496.175367Mbps；stress48三轮157450.080/149291.908/152935.493Mbps，平均153225.827Mbps、成功38/42/33流。逐directionId核对全部12轮stress，optimized/default的方向与成功失败标记完全一致。测后48VM最大load1=1.22，开始cleanup。新增只读汇总/配对/验证脚本位于setup目录，等待第三轮完成后统一运行。

- Third round checkpoint (21:42): default cleanup PASS后自动启动pod-count-balanced。建群1642.05s、编译674.16s、分区25.89s、静态验证32.90s均PASS；静态选中方案4954Pod/48节点，每节点103或104，Pod总体方差0.16493056；VLAN计划11502，均值239.625、总体方差19.44270833。build含分发514.98s PASS，按节点分发总4954项；进入VLAN与Pod部署，实际分布待Ready后核验。
- Software comparability: 三轮build.log的191个“Setting up”包名/版本完全一致，包括BIRD2.17.2，保存software_package_comparison.json。发现第三轮ubuntu:24.04多架构索引摘要变化，通过docker buildx imagetools inspect --raw对两个确切digest只读查询；两者linux/amd64条目均sha256:496754492fb28b4d3049432f2ca787449331e23fb14f0dd3fffea86bf5a93eb4，因此实际基础镜像相同；原始manifest及ubuntuPlatformComparison.json保存于setup。未修改镜像、Dockerfile或实验参数。

- Final result (22:40): 三种实验及三次cleanup全部PASS；optimized全流程6367s、default24052s（含失败重试与恢复间隔）、pod-count-balanced6144s。balanced于22:32:10完成，最后清理22:40:11结束；协调器退出码0。balanced真实4954Pod全部Ready/落点匹配，10AS协议、674AS FIB及重收敛PASS；测后最大load1=0.87。
- Static comparison: Pod均值都103.208333，总体方差分别1274.956597/579.414931/0.164931；按落点所需VLAN总量9285/11386/11502，均值193.4375/237.208333/239.625，方差4022.746094/2037.373264/19.442708。部署配置VLAN总量9285/228624/11502分开列。master Pod348/264/104，Pod/vCPU方差0.068564/1.461613/3.381；不能把异构CPU节点上的原始Pod方差直接当作容量均衡。
- Dynamic comparison: 原始RTT均值4.469947/5.206526/6.313712ms，ping152/192、152/192、153/192。balanced新增成功方向b7678bbab902b0c3-a（10/10收到，236.694ms）导致均值上升；三轮共同成功152方向均值4.469947/5.206526/4.798053ms另列。TCP均35/48，成功流平均8024.823458/7496.175367/7804.467754Mbps。stress48总吞吐均值170282.450349/153225.826998/145784.802296Mbps；三轮成功38/42/33在所有算法完全一致，失败率均21.527778%。优化分区stress48分别高11.13%/16.80%；本次均衡分区恢复收敛23.946s优于43.760/38.913s。这里只是单次完整实验，不作显著性推断。
- Independent validation: collectPlacementComparison最终PASS/samples=MATCH；comparePairedFlows核验全部方向ID、baseline TCP以及12轮stress成功/失败逐流一致，仅ping有1个差异。validateResults重新计算每节点均值/总体方差、逐轮吞吐和成功数；核对48VM/306CPU/600GiB资源签名、相同重收敛目标、所有负载门控及路由验证，全部PASS。宿主机复查本轮VM/网络/磁盘已清除，bird-rtnl-dev仍running，UUID、CPU、内存、磁盘路径和网卡来源/MAC与开始时一致，原有4个网络保留，默认YAML SHA256未改动。
- Outputs: experiments/4954_48vm_comparison_20260916/experimentReport.md（中文总结）、detailedComparison.md、comparison_metrics.csv、comparison_stages.csv、comparison_summary.json、paired_metrics.json、paired_flow_analysis.json、software_package_comparison.json、validation.json、commandsExecuted.md。setup中保留冻结input、resources_before/after、平台镜像manifest核对、结束代码哈希、恢复及分析脚本与commands.log。原始HTTP504失败及成功恢复记录均保留。
- Final commands: seedpy310 Python执行 collectPlacementComparison.py --run-dir experiments/4954_48vm_comparison_20260916；setup/summarizeExperiment.py；setup/comparePairedFlows.py；全部cleanup后重新collect，执行setup/validateResults.py及setup/exportCommands.py。完整绝对命令见setup/commands.log及批次commandsExecuted.md，子阶段命令来自各summary.json。
- Scope and limitations: 本轮未改生产脚本、原始默认YAML或Dockerfile，未提交Git；只写实验配置/产物、专用恢复/分析脚本与工作日志。保留其他VM运行。loaded ping现有采样窗口限制、失败流、公平性统计口径、阶段准备策略差异在报告中明确保留。只读诊断曾误试不存在的start-bird.log与当前目录verifyStartBird.py，随后使用真实日志/rg定位；没有因此执行额外路由操作。
## 2026-09-17 09:34 - 增加 network-only 对比并完成 4954 实验

- User intent: 在 `runPlacementComparison.py` 中加入更侧重跨边代价的 `network-only`，并按上一轮相同的 4954/48VM 配置实际运行一次完整实验。
- Scope: 更新 B63 串行入口、README、B62/B63 中文运行说明及对比入口测试；复用 `experiments/4954_comparison_20260916_setup/input.yaml`，单独运行 network-only，全流程成功后销毁本轮集群。不会修改当前默认 7001 YAML，也不会操作现有 `bird-rtnl-dev`。
- Changes: `ALGORITHMS` 增加 `network-only`，`all` 顺序改为 optimized → network-only → kubernetes-default-scheduler → pod-count-balanced；文档说明该算法使用网络目标权重、关闭资源/Pod/VLAN均衡目标，但保留容量硬约束与固定 nodeSelector。
- Commands: seedpy310 `py_compile` 通过；`seed_k8s_placement_comparison_test.py` 13项通过；`runPlacementComparison.py --help` 显示新选项；4954冻结配置的 `--prepare-only --algorithm network-only` 为 PREPARED。完整实际运行命令记录在 `experiments/4954_network_only_20260917_setup/commands.log`。
- Validation: 全量 `seed_k8s_b63_shared_test.py` 运行17项时仅旧 `test_compact_memory_and_isolation` 失败，因为该测试仍固定断言历史6VM，而当前默认YAML是48VM；其余16项通过。实验开始前仅 `bird-rtnl-dev` 运行，本轮 VM/网络名无占用，磁盘与cloud-init目录为空，主机可用内存789GiB、数据盘40TiB可用。
- Execution: 使用 `/home/lxl/anaconda3/envs/seedpy310/bin/python -u runPlacementComparison.py --config experiments/4954_comparison_20260916_setup/input.yaml --algorithm network-only --run-dir experiments/4954_network_only_20260917 --destroy-after-pass`。09:35:07开始，11:20:28全流程PASS，11:28:59自动清理PASS。建群/编译/placement/build/deploy/dataplane分别1670.44/675.28/115.30/536.09/1302.61/1256.13秒；4954 Pod全部Running、Ready且与静态分区逐Pod一致，10 AS BIRD协议和674 AS FIB验证均PASS，故障/恢复收敛1.05/23.755秒。
- Static result: 48VM实际资源306vCPU/600GiB；Pod均值103.208333、总体方差1274.956597、范围98–348。VLAN总数6844、均值142.583333、总体方差708.118056、范围126–322；相比optimized的9285个VLAN少26.29%。网络ratio成本0.256434，optimized为0.592520；跨节点网络1117，optimized为3393；容量加权资源偏斜0.029259，仍符合容量硬约束。
- Dynamic result: ping 153/192，原始成功方向均值5.661268ms、中位数0.268ms、P95 1.6636ms；四算法共同成功152方向时network-only为4.127243ms。基线TCP 35/48，成功流均值9138.022Mbps。stress8/16/32/48总吞吐均值43.662/97.851/171.318/232.616Gbps；stress48三轮196.521/259.953/241.373Gbps，成功流38/42/33，与前三算法逐轮样本和成功标记匹配；负载RTT均值10.721ms、公平性0.798。
- Four-way comparison: `experiments/4954_four_algorithm_comparison_20260917/` 汇总上一轮三算法与本轮network-only，collect结果PASS且sampleComparison=MATCH；`detailedComparison.md`同时保存静态、动态、阶段耗时、共同成功方向与逐轮stress48。network-only在本次单轮中有最低网络代价/VLAN总量、最高基线TCP和stress48吞吐，但四组分两天各运行一次，不作显著性结论。
- Final validation: 自动清理后无任何`b63-sh48-*` domain，实验磁盘/cloud-init目录不存在；`bird-rtnl-dev`及default、seedemu-b63-trunk1/2/3网络保留。默认7001 YAML和冻结4954输入哈希仍为`e1e4afe9...`/`0023b529...`。对比入口13项测试及共享流程排除旧固定6VM断言后的16项测试全部通过；py_compile和diff-check通过。完整命令和原始控制台见setup目录，未提交Git。

## 2026-09-18 16:15 - E1-A 五 worker 并行正式实验完成

- User intent: 按用户修订的 E1 设计先验证“单个 4 vCPU/8 GiB VM 承载过量固定工作 Pod 会出现性能转折”，并在不改变单 VM 实验定义的前提下用多台 VM 并行加速，得到可用于设计 E1-B 的正式曲线。
- Scope: 新增 E1-A 固定到达原生负载、节点采样、全流程、离线分析、结果验证、专属 1 master + 5 worker 配置与文档；执行单 worker pilot 和 150 点正式实验。未启动 E1-B，未操作 `bird-rtnl-dev`、`e1-0918-as060` 至 `e1-0918-as150` 或既有 trunk/`seedemu-0918e1` 网络。
- Implementation: `e1aWorkload.c` 使用绝对到达表、确定性相位、有界队列和守恒计数；`executeE1aExperiment.py` 使用真实 `allocatable.pods`、固定 nodeName、无 CPU limit、Ready 屏障和 hostPath epoch；`collectE1aNodeMetrics.py` 采集 CPU/run queue/PSI/steal/cgroup/memory；`analyzeE1aExperiment.py` 输出重复性过渡带、P99 min/median/max、过载率及全范围/聚焦 PNG/PDF；`validateE1aResults.py` 核对计划、计数、采样、限流和清理。配置与使用方法见 `research_configs/configE1aMarginalPodsParallel.yaml`、`E1A_README.md`。
- Pilot: `experiments/e1_marginal_pods/20260918_e1a` 使用 2 VM，冻结 background/canary work units 为 `1828943/36719`。Loaded N=0..200 的 30 秒探索扫描首次在 N=80 进入预设 workload_overload；Idle N=0/100/200 全部正常。pilot 分段拟合不明确，正式细点预先冻结为 70/75/80/85/90。
- Formal command: `python3 executeE1aExperiment.py --config research_configs/configE1aMarginalPodsParallel.yaml --run-dir experiments/e1_marginal_pods/20260918_e1a_formal_parallel --phase formal`。正式阶段为 15 个 N × Loaded/Idle × 5 blocks，共 150 点；五个 4 vCPU/8 GiB worker 各独占一个 point 并按 wave 同步执行，每台恰好 30 点，30 秒预热 + 120 秒测量。master/worker pCPU 分别固定到 340–341 与互不重叠的 342–361。
- Result: 93 success、57 workload_overload，零 admission/harness/infrastructure 失败；75 个 Idle 点全部 success。Loaded N=40 为 0/5 过载，N=60 为 2/5，N=70 及以上为 5/5，故稳定正常到稳定过载的重复性区间为 `(40,70]`。CPU busy 中位数首次达到 99% 且背景完成率首次低于 99% 均在 N=80；全范围 P99 描述性分段拟合在 N=140，SSE 改善 81.7%。这些是 SLA 转折、资源饱和和高负载斜率加速三个不同口径，不合并为人为 Pod 上限。
- Representative metrics: Loaded P99 中位数 N=0/40/60/70/80/140/200 分别为 0.464/7.343/9.647/17.931/14.033/56.265/161.312 ms；相应 N=70/80/200 on-time 为 0.9530/0.9847/0.7708，run queue 为 15.28/80.84/201.97。Idle N=0..200 的 P99 中位数维持 0.450–0.522 ms、on-time=1。
- Validation commands: `python3 -m py_compile analyzeE1aExperiment.py validateE1aResults.py executeE1aExperiment.py collectE1aNodeMetrics.py`; `python3 /home/lxl/k8s/tests/seed_k8s_e1a_test.py`; `python3 analyzeE1aExperiment.py experiments/e1_marginal_pods/20260918_e1a_formal_parallel --phase formal`; `python3 validateE1aResults.py experiments/e1_marginal_pods/20260918_e1a_formal_parallel --check-cleanup`。12 项测试通过；150 个 summary 与冻结计划一一对应、任务计数全部守恒、每点 120 个节点样本、cgroup throttled delta 全为 0；最大平均 steal 0.55%，最低可用内存 5,263,816 KiB。
- Cleanup: runner destroy 返回 0 并删除六台 `b63-e1ap-*` VM、专属 libvirt 网络和磁盘文件。最终验证发现 `/data/lxl/k8sTools/b63-e1a-parallel` 仅剩四个空目录；确认无文件/符号链接后用 `rmdir` 删除，随后 cleanupComplete PASS。其他 domain/network 清单保存在正式目录 `metrics/validation.json`。
- Artifacts: `report_zh.md`、`metrics/e1a_summary.csv`、`metrics/knee.json`、`metrics/validation.json`、`figures/e1a_formal_curves.{png,pdf}` 和 `figures/e1a_formal_transition.{png,pdf}` 位于正式实验目录。`RESEARCH_PROGRESS_20260918.md` 已更新为 E1-A 完成、E1-B 待执行，并保留旧 topology 214 未通过验收的探索记录。
- Corrections: 实验完成时一次即时口头计数沿用了未完成阶段暂存值，随后明确更正为原始数据与正式报告中的 93/57；第一次 `unittest` 使用了错误的绝对模块参数，改为直接执行测试文件后 12 项通过；首次 cleanup 验证因空目录残留失败，完成上述精确清理后重跑通过。这些不影响测量原始数据。
- Scientific boundary: E1-A 证明给定 VM/工作画像下的本地共置代价，不包含 SEED 拓扑或映射变化，不能单独支持“减少跨 VM 通信后全局性能反而下降”。E1-B 必须固定 topology 214、全部 214 个仿真 Pod、真实逐跳路由和 offered workload，再测局部性梯度与全局净效果。

## 2026-09-18 - 0918E1 与现有结果的 motivation 证据审计

- User intent: 结合 `0918E1`、E1-A、旧 topology 214 和 4954/48VM 数据，判断当前能否把 E1 作为论文 motivation，并明确缺少的实验。
- Evidence reviewed: `0918E1/README.md`、`WORKLOG.md`、三个主要批次的冻结 YAML/CSV/JSON/逐重复 summary；E1-A 正式 knee/报告/验证；4954 四算法静态与动态配对汇总；旧 topology 214 探索限制。
- Finding: 当前可支持“局部性影响性能”“固定外生工作共置会出现本地失约/饱和”“Pod 数均衡不等于性能最优”三个分离事实，不能支持同一固定拓扑和工作量下“通信继续减少但争用导致全局性能反转”的联合因果命题。完整 E1 状态为 E1-A PASS、E1-B NOT RUN。
- 0918E1 interpretation: 不限速批次 60→120 AS 的平均每流吞吐下降15.4%、RTT上升22.3%，但总吞吐仍增长，130 AS RTT回落；100Mbps/流批次在60–140 AS无拐点。各规模改变拓扑/容器/流数量并固定绑定不同VM，流全部在单VM内，三次为技术重复，估算跨机节省不是fabric实测；140旧超时与150返回137不能当作完整性能点。
- 4954 interpretation: network-only 网络成本0.256、stress48 232.616Gbps，优于 optimized 的0.593/170.282、default的0.958/153.226及Pod均衡的0.976/145.785；说明局部性有价值且数量均衡不是最终目标。本轮每算法仅一次完整运行、并非连续映射路径，而且最强局部性方案没有出现反转。
- Deliverable: 新增 `E1_MOTIVATION_EVIDENCE_ASSESSMENT.md`，逐命题标记证据强度，给出 E1-B 最小方案：固定 topology214/214Pod/4个4vCPU worker，预冻结约44/54/64/74/84目标占用和严格下降的跨worker通信需求，增加matched-count控制，分开固定速率UDP与TCP capacity，使用固定canary/节点指标/协议验证，正式5个随机化block及block内配对差值。未运行或修改任何实验环境。
- Boundary: 0918E1 可作为辅助单机容量图；即使按300秒宽限、VM轮换和5 blocks重跑，也不能替代E1-B。没有观察到反转时必须保留结果并相应收窄论文主张。
## 2026-09-19 - corrected E1-B pilot continuation

- Stopped the first E1-B pilot after finding a protocol-design error: its
  `intra_as_link` candidates were multi-hop IX-to-IX demands, not compiled
  direct networks. Their endpoint host relation could not serve as the
  measured cut metric; eight UDP flows consequently failed. Those artifacts
  are marked `harness-invalid` and excluded from evidence.
- Corrected `planE1bExperiment.py` to parse Multus interface addresses and use
  real two-endpoint `intra-AS` networks plus shared IX networks for all 16
  fixed flows. The frozen plan is under
  `experiments/e1_marginal_pods/e1b_topology214_frozen_20260919/plans/` with
  direct-link semantics, 214 Pods, strict X sequence 16/13/10/7/4, and a
  matched-count control.
- Added fail-closed remapping image preparation. Fast archives from the first
  mapping are imported as a union to every worker before remapping; failed
  archive/pull commands now propagate nonzero status. Added UDP receiver-view
  parsing, `analyzeE1bExperiment.py`, and offline test
  `/home/lxl/k8s/tests/seed_k8s_e1b_test.py` (5 tests pass).
- `20260919_e1b_corrected_pilot` completed pi00 and then hit the intentionally
  retained image-preload harness bug during pi04; it is marked invalid. A
  fresh `20260919_e1b_corrected_pilot2` reused the live dedicated cluster,
  passed clean/preflight/build/deploy/wait-ready, and is currently waiting at
  the existing BIRD host-load gate because unrelated `e1-0918-as100` through
  `as300` VMs are active. No unrelated VM, network, or storage resource was
  stopped or modified.

## 2026-09-19 - corrected E1-B pilot4 and formal2 continuation

- Fixed two harness issues found during corrected E1-B continuation. The
  worker image check now uses `k3s ctr images list -q` and waits for the union
  of required archive tags; it no longer calls the unsupported `ctr images
  inspect`. Formal point directories are created before measurement, so a
  missing `udp_phase` directory cannot invalidate an otherwise valid point.
  Added `--resume-run` for missing pilot points and `analyzeE1bFormal.py` for
  incremental formal aggregation.
- `20260919_e1b_corrected_pilot4` is the first valid corrected pilot. Its three
  points completed PASS: `pi00-target044`, `pi04-target084`, and the
  matched-count/low-locality control. pi00 averaged over four available
  repetitions had UDP receive `336.128 +/- 3.428 Mbps`, delivery
  `0.841385 +/- 0.008535`, loss `15.820 +/- 0.856%`, canary P99
  `11.696 +/- 0.303 ms`, and background on-time `0.998972 +/- 0.000159`.
  pi04 (84 pods on the target worker) had UDP receive about `120.551 Mbps`,
  delivery `0.432568`, loss `65.250%`, background on-time `0.60943`, and
  target-worker PSI some-average about `97.18%` with run queue about `102`.
  The matched-count control had UDP receive `324.986 Mbps`, delivery
  `0.8135`, canary P99 `11.124 ms`, and background on-time `0.99947`.
  This is evidence of a local contention reversal in the corrected harness,
  but remains a pilot until the randomized formal blocks finish.
- Started the corrected formal run with five randomized blocks (30 points),
  fixed topology/workload, UDP 60 seconds, TCP 60 seconds with three
  technical repetitions, and a fresh workload namespace per phase:
  `python3 runE1bFormal.py --run-dir experiments/e1_marginal_pods/20260919_e1b_corrected_pilot4 --repetitions 5 --seed 20260918 --output-subdir e1b/formal2`.
  At the latest checkpoint, `formal_summary.json` reports `RUNNING` and 4/30
  points PASS; the fifth point is being redeployed. The dedicated five-VM
  cluster is intentionally retained while the formal sequence runs. Existing
  unrelated VMs and networks were not changed.
- Incremental analysis currently reports all four completed points as
  successful. It writes `e1b/formal2/analysis/formal_point_metrics.csv`,
  `formal_mapping_summary.csv`, `formal_analysis.json`, and a plot; rerunning
  it after each additional completed point is safe and does not alter the
  experiment.

## 2026-09-19 - corrected E1-B formal2 completed and audited

- Formal command completed normally:
  `python3 runE1bFormal.py --run-dir experiments/e1_marginal_pods/20260919_e1b_corrected_pilot4 --repetitions 5 --seed 20260918 --output-subdir e1b/formal2`.
  The run contains 30 points (6 mappings × 5 randomized blocks), and
  `formal_summary.json` is `PASS` with all 30 runner points marked `PASS`.
- Static verification is complete for all 30 points: every point has 214
  emulation Pods, all Pods are Ready and correctly placed, and
  `placementStatus=PASS`. UDP and TCP dataplane summaries are also successful
  for all 30 points.
- The final audit found one phase-level data-quality exception that the formal
  runner status does not encode: UDP workload at
  `block02/01-pi04-target084` is `harness_invalid` because the background
  collector reported 213 Pods instead of the expected 214
  (`countConservationOk=false`). TCP workload is successful at that point.
  The original JSON is preserved; it is not silently repaired or counted as a
  valid UDP workload repetition.
- Fixed `analyzeE1bFormal.py` to emit workload validity and status counts,
  exclude invalid workload phases from mapping-level means/SDs, report valid
  sample counts, and generate the grouped `formal_curves.png` correctly.
  Re-analysis passed `py_compile` and `git diff --check`. The resulting
  mapping summary uses UDP workload `n=4` for `pi04` and `n=5` elsewhere;
  UDP/TCP dataplane and TCP workload counts remain `n=5` per mapping.
- Main evidence: planned fixed-flow crossings decrease 16→4 while target
  worker occupancy increases 44→84. Across the same sequence, UDP maximum
  PSI rises about 71.8%→97.3%, UDP maximum run queue about 17.7→119.1,
  UDP background on-time falls about 0.999→0.609, TCP background on-time
  falls about 0.997→0.608, and UDP delivery falls from about 0.840 to
  0.437 (with the `pi04` workload aggregate based on four valid repeats).
  `pi03` is the transition region: PSI≈94.7%, run queue≈88, UDP delivery
  ≈0.365, and TCP background on-time≈0.683. TCP dataplane goodput improves
  with locality, so it is reported separately from workload service quality.
- Report written to
  `experiments/e1_marginal_pods/20260919_e1b_corrected_pilot4/e1b/formal2/e1b_formal_report_zh.md`.
  It records the exact command, mappings, static/dynamic metrics, validity
  caveat, and the boundary that this is evidence for the fixed 214-Pod /
  four-worker scenario rather than a universal threshold. A final paper-grade
  table should either retain `pi04` UDP workload as `n=4` or repeat only the
  invalid measurement; the invalid record must not be treated as a script
  failure or hidden from the audit trail.

## 2026-09-19 - pi04 invalid UDP workload replacement

- To close the single phase-level data-quality gap without mutating the
  pre-randomized `formal2` sequence, reran only the frozen
  `pi04-target084` mapping into
  `e1b/repair_pi04/01-pi04-target084/`. The one-shot runner reused
  `runE1bExperiment.redeploy`, `verifyLivePlacement`, `runDataplane`, and
  `measurePointWorkload` with UDP/TCP dataplane durations of 60 seconds and
  TCP technical repetitions of 3.
- The first temporary invocation stopped before the clean stage because its
  local `summary` object omitted the shared runner's required `stages` and
  `tests` keys (`KeyError: 'stages'`). It did not alter the formal2 artifacts.
  The corrected invocation used the same summary shape as the formal runner.
- Replacement result: clean/deploy/wait-ready, BIRD start and protocol
  verification, kernel/FIB write and verification, placement, UDP/TCP
  dataplane, and both workload phases all passed. Both workload phases were
  `success`; the UDP background count was conserved at 214 Pods.
- Replacement key values: UDP receive `123.034 Mbps`, UDP delivery `0.462609`,
  UDP background on-time `0.608254`, TCP background on-time `0.608014`,
  UDP maximum PSI `97.15%`, and UDP maximum run queue `119.85`.
- Generated
  `e1b/formal2/analysis/repair_pi04_summary.json`, combining the four valid
  formal2 `pi04` UDP workload repetitions with the replacement. The
  supplemental n=5 values are UDP receive `121.977 ± 1.063 Mbps`, delivery
  `0.439378 ± 0.014743`, background on-time `0.608744 ± 0.000497`, maximum
  PSI `97.235 ± 0.056%`, and maximum run queue `119.278 ± 1.221`. These
  confirm the same severe-contention conclusion; the replacement remains
  separately labeled because it was not part of the pre-randomized formal
  block order.
- Added reusable entry point `runE1bRepairPoint.py`; its help/compile checks
  and the five E1-B offline tests pass. The report now records the exact
  rerun command and links the replacement summary and script.

## 2026-09-19 - SIGCOMM motivation chapter

- Added `0919E1Motivation.md` in the B63 deployment directory as a
  paper-style motivation chapter for introducing the Kubernetes partitioning
  algorithm.
- The chapter explains the two-level E1 design: E1-A isolates the local
  co-location capacity knee on a fixed 4 vCPU/8 GiB worker, while E1-B fixes
  the 214-Pod SEED topology and workload and changes only the static
  Pod-to-Node mapping.
- It uses the audited results: E1-A transitions from 0/5 overload at 40
  workload Pods, to 2/5 at 60, and 5/5 at 70 and above; E1-B reduces fixed
  cross-worker directions from 16 to 4 while TCP dataplane goodput rises
  about 32.4→93.3 Gbps, but maximum PSI rises about 71.8→97.2%, maximum run
  queue about 17.7→119.3, and workload on-time rates fall to about 0.609.
- The text explicitly distinguishes network-path throughput from whole
  workload service quality, states the scope limitation of the observed
  thresholds, and transitions to a static global partitioner that estimates
  locality gain and co-location contention using pre-deployment information.
- Validation: local links were checked against the existing E1-A/E1-B
  artifacts; `git diff --check` is required after this documentation edit.

## 2026-09-20 - Marginal locality/contention model and VM profile pilot

- Added `MARGINAL_BENEFIT_MODEL.md`, defining the boundary decision as
  importance-weighted cross-VM offered traffic saved minus a profile-aware
  contention/performance cost. The model keeps raw TCP/UDP throughput, delivery,
  RTT, and workload on-time responses separate and supports per-flow network
  weights plus per-Pod CPU/memory/resource weights.
- Added `marginalBenefitModel.py`. `replay-e1b` reads the frozen 214-Pod plan,
  selected 16-flow pair plan, and formal dynamic metrics; it writes an
  auditable data set, ridge response models, plots, and
  `marginal_boundary_steps.csv`. `evaluate` scores a generic before/after
  placement and can load profile overrides.
- E1-B replay completed in
  `experiments/e1_marginal_pods/20260920_marginal_model/e1b_replay_v3`.
  The primary adjacent steps each save 75 weighted Mbps, while CPU contention
  changes are approximately -0.002, +0.780, +1.598, and +2.451 from
  `pi00→pi01`, `pi01→pi02`, `pi02→pi03`, and `pi03→pi04`. The output preserves
  the measured response deltas and marks the one-profile fit as a calibration
  artifact rather than a universal model.
- Added `runVmProfileCalibration.py`, a serial, isolated profile runner, and
  `analyzeVmProfileCalibration.py`, an offline reducer. The pilot matrix used
  the same E1-A workload at `2c/8G`, `4c/4G`, `8c/8G`, and `4c/16G`.
  Observed transition summaries: `2c/8G` stable success only at N=0 then
  overload at N=20; `4c/4G` stable success through N=40 and overload at N=60;
  `8c/8G` had no overload through N=80 (right-censored); `4c/16G` had an
  invalid N=0 baseline and is excluded from coefficient fitting. All four
  generated profile clusters were cleaned by the child runner.
- Profile artifacts:
  `experiments/e1_marginal_pods/20260920_vm_profile_calibration_analysis_v3/`.
  `node_profile_overrides.json` contains only profiles with a valid pilot
  baseline; each entry carries a warning that formal repeats are still needed.
- Validation run: `py_compile` for all three new Python scripts, E1-B replay,
  generic before/after scoring, profile config generation, profile pilot
  execution, profile reduction, and `git diff --check`. Existing E1-B and
  unrelated VMs were left running.
- Added `research_configs/e1bNetworkWeightsExample.json` and reran the E1-B
  replay with one fixed flow weighted 3x; the baseline weighted crossing
  traffic changed from 400 Mbps to 650 Mbps, confirming that network
  importance weights affect the communication term without changing the
  placement or dynamic measurements.
- Replayed once more after binding adjacent-step extraction to the frozen
  `primarySequence`; the latest auditable artifact is
  `experiments/e1_marginal_pods/20260920_marginal_model/e1b_replay_v5/`.

## 2026-09-20 10:30 - E1-B Pod placement tradeoff figure

- User intent: draw a concise figure with target-VM Pod count on the x-axis,
  showing cross-machine bandwidth reduction, UDP delivery rate, and task
  processing speed, then write a short explanation of the experiment and
  observed result.
- Scope: offline analysis only; no VM, Kubernetes, namespace, image, or cluster
  state was changed.
- Changes: added `renderE1bPlacementTradeoff.py`; generated
  `experiments/e1_marginal_pods/20260920_tradeoff_plot/e1b_pod_placement_tradeoff.{png,pdf,csv}`;
  added `0920E1BPlacementTradeoff.md`.
- Commands: `python3 -m py_compile renderE1bPlacementTradeoff.py` checked the
  plotting script; `python3 renderE1bPlacementTradeoff.py` rendered the figure
  and value table; `git diff --check` verified patch formatting; `test -s`
  checked that the expected figure and document artifacts exist.
- Validation: the figure was visually inspected. It uses the frozen E1-B
  primary sequence `pi00→pi04`, 25 Mbps per fixed flow direction, and valid UDP
  workload points only. `pi04` task speed uses 4 valid formal UDP repeats.
- Notes: `matched-count-target054-low-locality` is intentionally excluded from
  the monotonic Pod-count curve because it is a same-count low-locality control
  rather than a primary sequence point.
