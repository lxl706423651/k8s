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
