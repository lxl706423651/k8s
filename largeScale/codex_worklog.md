## 2026-06-08 19:28 - B62/B63 Start Stage Parameter Refactor

- User intent: continue the B62/B63 large-scale workflow changes so start-bird/start-kernel parameters are declared directly in scripts, not passed through environment variables, and keep test-bgp plus destroy-cluster-after-pass out of the full experiment flow.
- Scope: `b62_k8s_scale` start scripts/helpers, `b62_k8s_scale/runFullExperiment.py`, `b63_deployment` start scripts, `b63_deployment/runB63LargeExperiment.py`, and the related README files.
- Changes: B62 start helpers now use explicit argparse parameters and explicit kubeconfig paths; B62/B63 start shell scripts pass top-of-script constants to helpers as CLI arguments; B62/B63 orchestrators leave test-bgp and destroy-cluster-after-pass commented out; help text and README flow descriptions now state those stages are disabled.
- Commands: inspected scripts with `sed` and `rg`; checked reference tuning values under `/home/lxl/k8s/lxl/test`; ran `python3 -m py_compile` on changed Python files; ran `bash -n` on changed start shell scripts; ran `PYTHONDONTWRITEBYTECODE=1 python3 ... --help` on the changed CLI entrypoints.
- Validation: Python compile checks passed; shell syntax checks passed; helper and orchestrator `--help` entrypoints parsed successfully; no VM, K3s, Kubernetes deploy, BGP test, or destroy commands were run.
- Notes: BGP test and destroy helper functions remain in the Python files for manual re-enable later, but their main-flow calls are commented out. `__pycache__` directories produced by validation were removed.

## 2026-06-08 19:36 - B62 Libvirt Network Cleanup

- User intent: remove the old `seedemu-b62-net-w11` libvirt network and make B62 cluster destruction remove stale B62 libvirt networks.
- Scope: `b62_k8s_scale/destroyCluster.sh`, `b62_k8s_scale/README.md`, and local libvirt network state.
- Changes: `destroyCluster.sh` now runs `k8sTools.py destroy` when `configK3s.yaml` exists, then destroys/autostart-disables/undefines matching B62 libvirt networks discovered from `configKvmOvn.yaml`, `assignment.yaml`, and current `virsh net-list --all --name`; README destroy instructions now mention network cleanup.
- Commands: ran `virsh -c qemu:///system net-destroy seedemu-b62-net-w11`; ran `virsh -c qemu:///system net-autostart --disable seedemu-b62-net-w11`; ran `virsh -c qemu:///system net-undefine seedemu-b62-net-w11`; inspected libvirt network state with `virsh net-list`, `net-info`, and `net-dumpxml`; inspected scripts with `sed` and `rg`.
- Validation: `bash -n b62_k8s_scale/destroyCluster.sh` passed; `python3 -c 'import yaml'` passed; `virsh net-list --all` shows `seedemu-b62-net-w11` removed and only `seedemu-b62-net-w5` remains among B62 networks.
- Notes: I did not run `destroyCluster.sh` because it now intentionally removes matching B62 libvirt networks, including the current `seedemu-b62-net-w5` network. Initial `sudo -n virsh` read-only queries hung and were killed; plain `virsh -c qemu:///system` commands worked and were used for the actual cleanup.

## 2026-06-09 01:03 - B62 Image Cache Reuse Fix

- User intent: fix `prepareHostAssets.py` failing on `ghcr.io/k8snetworkplumbingwg/multus-cni:snapshot`, using `/home/lxl/k8s/origin_k8s/setup/image-cache` when possible.
- Scope: `seedemu/k8sTools/resources/setup/kvm/prepareHostAssets.py`, `seedemu/k8sTools/resources/setup/applyK3sCluster.py`, `seedemu/k8sTools/resources/setup/kvm/manageKvmConfig.py`, `seedemu/k8sTools/resources/setup/manageK3sConfig.py`, and B62 assignment/rendered config.
- Changes: KVM host-asset and K3s apply setup scripts now honor `seedemu.imageCacheDirs`, copying matching tar files into each temporary setup cache before falling back to `docker pull`; B62 `assignment.yaml`, `renderAssignmentConfig.py`, and current `configKvmOvn.yaml` now point at `/home/lxl/k8s/origin_k8s/setup/image-cache`.
- Commands: inspected the failed build log; listed origin image-cache contents; checked base image validity with `qemu-img info`; checked disk space with `df -h`; pre-pulled missing K3s bootstrap images `rancher/mirrored-coredns-coredns:1.10.1`, `rancher/mirrored-metrics-server:v0.6.3`, and `rancher/local-path-provisioner:v0.0.24`.
- Validation: `python3 -m py_compile` passed for changed Python files; `prepareHostAssets.py --help` and `applyK3sCluster.py --help` passed; `manageKvmConfig.py ... kvm-vars` emits `seedemuImageCacheDirs=/home/lxl/k8s/origin_k8s/setup/image-cache`; generated configK3s smoke output preserves `seedemu.imageCacheDirs`; Docker image inspection passed for all locally pulled non-multus bootstrap images; origin cache contains the multus tar.
- Notes: I did not rerun `runExperiment.sh` because it creates/modifies KVM VMs and a K3s cluster. The next run should skip the GHCR multus pull by copying the origin tar cache; the remaining K3s bootstrap images are already present locally and will be saved into the new temporary image cache.

## 2026-06-09 08:04 CST - K3s Image Tar Path Output Fix

- User intent: explain and fix the `scp: stat local "...registry_2.tar\n...registry_2.tar"` failure during B62 K3s cluster build, then check for similar follow-on issues.
- Scope: `seedemu/k8sTools/resources/setup/applyK3sCluster.py` and image-cache stdout behavior used by K3s bootstrap image copy/import.
- Changes: made `seedHostImageTarball` stdout-silent in `applyK3sCluster.py`, so `saveHostImageTarball` is the only function that prints the local tar path captured by `tar_path="$(saveHostImageTarball ...)"`.
- Commands: inspected `applyK3sCluster.py` and `prepareHostAssets.py` around image-cache helpers with `sed`; scanned setup scripts with `rg` for `saveHostImageTarball`, `seedHostImageTarball`, `externalImageTarball`, path printing, and tar-path command substitutions; removed validation-generated `__pycache__` directories.
- Validation: `python3 -m py_compile /home/lxl/k8s/seedemu/k8sTools/resources/setup/applyK3sCluster.py /home/lxl/k8s/seedemu/k8sTools/resources/setup/kvm/prepareHostAssets.py` passed; extracted both embedded `SHELL_BODY` values and checked them with `bash -n`; `PYTHONDONTWRITEBYTECODE=1 python3 /home/lxl/k8s/seedemu/k8sTools/resources/setup/applyK3sCluster.py --help` passed; `PYTHONDONTWRITEBYTECODE=1 python3 /home/lxl/k8s/seedemu/k8sTools/resources/setup/kvm/prepareHostAssets.py --help` passed.
- Notes: I did not rerun `runExperiment.sh` because it creates or modifies KVM VMs and the K3s cluster. The failed temp directory still contains the old copied script; a fresh run will copy the fixed template into a new temp directory.

## 2026-06-09 08:35 CST - B62 Compile Monitoring And Routing Template Fix

- User intent: monitor the restarted `b62_k8s_scale/runExperiment.sh` and determine whether it is running normally.
- Scope: B62 run `runs/20260609_081750_1078_w5`, K3s cluster readiness, and `seedemu/layers/Routing.py`.
- Changes: restored escaped BIRD braces in `RoutingFileTemplates["kernel1"]` so `.format(interval=...)` no longer treats BIRD `{ ... }` blocks as Python format fields.
- Commands: inspected latest run logs and `summary.json`; compared `Routing.py`, `Ospf.py`, `Ibgp.py`, `Ebgp.py`, and `AutonomousSystem.py` against `/home/lxl/seed-emulator/seedemu`; checked K3s nodes with `kubectl get nodes -o wide`; reran `./compile.sh /home/lxl/k8s/largeScale/b62_k8s_scale/runs/20260609_081750_1078_w5`.
- Validation: `python3 -m py_compile /home/lxl/k8s/seedemu/layers/Routing.py` passed; the manual B62 compile rerun passed and generated `output/k8s.yaml` plus `output/rr_plan.json`; all six K3s nodes remained `Ready`.
- Notes: the original `runExperiment.sh` process had already exited with `compile` failure, so its `summary.json` still records the historical failure. I did not run the later workload build/deploy/start stages because they modify the live K3s cluster.

## 2026-06-09 09:04 CST - Seed Emulator K8s New Routing Brace Fix

- User intent: fix `== Emulator: rendering Routing... [real_topology_k3s_compile] ERROR: unexpected '{' in field name` and provide follow-up commands.
- Scope: `seed-emulator-k8s-new/seedemu/layers/Routing.py` plus comparison against the template repository and the active `/home/lxl/k8s` copy.
- Changes: escaped the BIRD braces in `RoutingFileTemplates["kernel1"]` in `seed-emulator-k8s-new`, matching `/home/lxl/seed-emulator/seedemu/layers/Routing.py` and the already-fixed `/home/lxl/k8s/seedemu/layers/Routing.py`.
- Commands: inspected the three `Routing.py` files; ran a direct Python `RoutingFileTemplates["kernel1"].format(interval=60000)` check against all three paths; compared `seed-emulator-k8s-new` against the template with `diff`.
- Validation: `python3 -m py_compile /home/lxl/k8s/seedemu/layers/Routing.py /home/lxl/seed-emulator-k8s-new/seedemu/layers/Routing.py` passed; the direct `.format()` smoke test passed for `/home/lxl/k8s`, `/home/lxl/seed-emulator-k8s-new`, and `/home/lxl/seed-emulator`.
- Notes: no KVM, K3s, workload build, deploy, or destroy stage was run in this fix.

## 2026-06-09 10:16 CST - Kubernetes Compiler Import Chain Repair

- User intent: repair `ImportError: cannot import name 'KubernetesCompiler' from 'seedemu.compiler'` by using the GitHub `k8s` branch calling chain as reference and avoiding duplicate Kubernetes compiler files.
- Scope: `/home/lxl/k8s/seedemu/compiler`, `/home/lxl/seed-emulator-k8s-new/seedemu/compiler`, and the current B62 run compile stage.
- Changes: consolidated the old `KubernetesCompiler` implementation into the existing lowercase `seedemu/compiler/kubernetes.py`; exported `KubernetesCompiler`, `SchedulingStrategy`, and compatible `NativeKubernetesCompiler` from `compiler/__init__.py`; made KubeVirt mode detection default to `Container` when current `Node` lacks `getVirtualizationMode`.
- Commands: cloned `https://github.com/lxl706423651/k8s` branch `k8s` into `/tmp/codex-k8s-branch` for reference; inspected compiler files and Kubernetes example imports with `rg`/`sed`; ran a minimal BY_AS_HARD compiler smoke test; reran `./compile.sh` for latest B62 run.
- Validation: Python compile checks passed for both compiler copies; `from seedemu.compiler import KubernetesCompiler, NativeKubernetesCompiler, SchedulingStrategy` passed for both repos; `real_topology_k3s_compile.py` import passed; B62 compile passed and generated `k8s.yaml`, `build_images.sh`, `images.txt`, `.env`, and `rr_plan.json`; 1078/1078 Deployments include `nodeSelector`.
- Notes: no uppercase `Kubernetes.py` was created. The compile validation only rewrote the current run's compile output; it did not build images, deploy workloads, start BIRD, or destroy/recreate VMs.

## 2026-06-09 10:32 CST - B62/B63 Kube-OVN Attached CNI Fix

- User intent: fix the largeScale B62/B63 workflow so it uses OVN/OVS through Kube-OVN instead of falling back to macvlan-style secondary networking.
- Scope: `seedemu/compiler/kubernetes.py` in `/home/lxl/k8s` and `/home/lxl/seed-emulator-k8s-new`, B62 largeScale defaults, B63 running defaults, and the current B62 run output.
- Changes: Kubernetes compiler NAD output now includes SeedEMU metadata annotations, especially `org.seedsecuritylabs.seedemu.meta.prefix`, so `renderKubeOvnManifest()` can create matching Kube-OVN `Subnet` objects; B62/B63 shell fallback defaults now use `kube-ovn`; B62 assignment/current generated config defaults stay on `cniType`, `localLinkCniType`, and `attachedCniType` set to `kube-ovn`.
- Commands: ran `python3 -m py_compile` on both compiler copies and B63 Python helpers; ran `bash -n` on changed B62/B63 shell scripts; reran `./compile.sh /home/lxl/k8s/largeScale/b62_k8s_scale/runs/20260609_081750_1078_w5`; rendered the B62 runtime manifest through `render_runtime_manifest`.
- Validation: B62 compile passed with `CNI type: kube-ovn`; B62 source `k8s.yaml` has 1065 NADs and none missing prefix annotations; rendered `k8s.kube-ovn.yaml` has 1065 NADs, 1065 Subnets, all NAD configs use `type=kube-ovn`, all point to `/run/openvswitch/kube-ovn-daemon.sock`, all providers end in `.ovn`, and 1078/1078 Deployments have `nodeSelector`.
- Notes: no KVM/K3s build, image build/push, workload deploy, BIRD start, BGP test, or cluster destroy was run. The validation only rewrote the current B62 run's compile/runtime manifest outputs.

## 2026-06-09 10:38 CST - Compile Output Kube-OVN Refresh Fix

- User intent: explain why rerunning `./compile.sh "$RUN_DIR"` still appeared to generate the old manifest and fix the compile output path.
- Scope: B62/B63 compile scripts and both Kubernetes compiler copies.
- Changes: added an explicit `kube-ovn`/`ovn` branch in the Kubernetes compiler so source `k8s.yaml` NAD configs now show `type=kube-ovn`, `/run/openvswitch/kube-ovn-daemon.sock`, and `.ovn` providers; changed B62 and B63 compile scripts to call `render_runtime_manifest` after compilation and print the runtime manifest path.
- Commands: inspected B62/B63 compile scripts, parsed current B62 manifests, ran `python3 -m py_compile` on both compiler copies, ran `bash -n` on B62/B63 compile scripts, reran `./compile.sh /home/lxl/k8s/largeScale/b62_k8s_scale/runs/20260609_081750_1078_w5`.
- Validation: B62 compile passed; source `k8s.yaml` now has 1065 NADs with `type=kube-ovn` and zero missing prefix annotations; runtime `k8s.kube-ovn.yaml` was regenerated automatically and has 1065 NADs, 1065 Subnets, OVS socket configs, `.ovn` providers, and 1078/1078 Deployments with `nodeSelector`.
- Notes: the cause was that compile previously rewrote only the compiler source manifest and deleted the previously rendered runtime manifest when the output directory was overridden. No image build, workload deploy, BIRD start, BGP test, VM creation, or cluster destroy was run.

## 2026-06-09 12:05 CST - BIRD Include And Kernel Repair

- User intent: debug why pod `as1892brd-r13-1.13.7.100-78b4f9b66-29fs5` did not have a working BIRD process and repair all pods in the namespace.
- Scope: B62/B63 `start_bird_helper.py`, B62/B63 `start_bird_kernel_helper.py`, and the live `seedemu-b62-1078` namespace.
- Changes: `start_bird` now creates `/etc/bird/conf/00-empty.conf`, removes duplicate explicit `kernel.conf` include when wildcard include exists, and uses `pgrep bird` for the fast path instead of `birdc show status`; `start_bird_kernel` now avoids appending `include "/etc/bird/conf/kernel.conf";` when `include "/etc/bird/conf/*.conf";` is already present.
- Commands: inspected the target pod with `kubectl exec`, checked `/etc/bird/bird.conf`, `/etc/bird/conf/kernel.conf`, `bird -p`, `birdc show status`, and prior start summaries; ran `python3 -m py_compile` and `bash -n`; reran `./start_bird.sh` and `./start_bird_kernel.sh` for `/home/lxl/k8s/largeScale/b62_k8s_scale/runs/20260609_081750_1078_w5`.
- Validation: B62 `start_bird_summary.json` is `PASS` with 1078/1078 started; `start_bird_kernel_summary.json` is `PASS` with 1078/1078 processed; target pod has one `bird` process, `birdc show status` reports daemon up, `kernel1` is up on `master4`, only wildcard include remains, and `bird -p -c /etc/bird/bird.conf` returns 0.
- Notes: The earlier `Unable to match pattern /etc/bird/conf/*.conf` log was from an earlier failed start before a matching `.conf` file existed; the later visible `kernel.conf` was created after that failure. The repeated kernel error came from wildcard include plus an added explicit `kernel.conf` include reading the same file twice.

## 2026-06-09 12:21 CST - B62/B63 Stale Image Reuse Default Fix

- User intent: confirm and fix the BIRD startup failure root cause where old Docker images were reused after the compile/build context gained /etc/bird/conf/kernel.conf.
- Scope: B62 and B63 large-scale image build defaults.
- Changes: changed B62 build.sh default SEED_BUILD_SKIP_EXISTING from 1 to 0; changed B63 running defaults and full-experiment CLI default --build-skip-existing from 1 to 0. Explicitly passing 1 still keeps the legacy skip-existing behavior.
- Commands: inspected B62/B63 build defaults with sed and rg; attempted apply_patch twice before permission elevation but the sandbox helper failed with bwrap: loopback: Failed RTM_NEWADDR; used exact sed -i replacements for the three default values; after permission elevation, reran rg, bash syntax checks, Python compile, and Git status checks.
- Validation: rg confirmed the default values are now 0; bash -n passed for B62 build.sh, B63 running/lib.sh, and B63 running/build.sh; python3 -m py_compile passed for B63 runB63LargeExperiment.py; the py_compile-generated pyc cache was removed.
- Notes: no image build, registry push, workload deploy, VM operation, namespace cleanup, or cluster destroy was run for this change. Future normal builds now rebuild images by default, avoiding stale image reuse after compiler/runtime file changes. These largeScale files are currently untracked from the /home/lxl/k8s Git repository view, so git diff has no tracked diff for them.

## 2026-06-09 12:42 CST - B62 Fast Archive Kernel Config Inclusion

- User intent: inspect whether the current build strategy can still reuse stale images when scaling to 4954, where BIRD/kernel configuration can differ from the 1078 run.
- Scope: B62/B63 build strategy, image tag reuse behavior, and B62 fast image archive generation.
- Changes: added `/etc/bird/conf/kernel.conf` and the `/etc/bird/conf/` directory to B62 `fastSeedemuImageArchive.py` so the fast archive path includes the same kernel config file that the generated Dockerfile copies in normal Docker build mode.
- Commands: inspected B62/B63 build scripts, compiler image naming, generated 1078 Dockerfiles/images, B62 preload/import logic, and the B62 fast archive implementation.
- Validation: `python3 -m py_compile /home/lxl/k8s/largeScale/b62_k8s_scale/fastSeedemuImageArchive.py` passed; `bash -n /home/lxl/k8s/largeScale/b62_k8s_scale/build.sh` passed; a direct `make_layer()` smoke check on an existing B62 context confirmed the generated layer contains `etc/bird/conf/kernel.conf` and `etc/bird/conf`.
- Notes: no image build, image import, registry push, workload deploy, VM operation, namespace cleanup, or cluster destroy was run. B62/B63 still use stable `:latest` tags per SeedEMU node name, so same logical nodes across scales reuse the same tag name, but the normal build path now rebuilds and the B62 fast archive path now regenerates the layer from the current compile context.

## 2026-06-09 17:49 CST - Kube-OVN Helm Partial Download Recovery

- User intent: explain and recover the B62 4954 `applyK3sCluster.py` failure with exit status 139.
- Scope: Kube-OVN setup stage, Helm bootstrap logic, current failed temp run `/tmp/seedemu-k8s-tools-build-_tpmg7ca`, and the active B62 4954 K3s cluster.
- Changes: `installKubeOvnFabric.py` now validates any cached helm binary with `helm version --short`, removes unusable partial binaries, downloads through a temporary archive with retry, validates the tarball before extraction, and fails cleanly instead of executing a corrupt helm binary. The same fix was applied to `/home/lxl/k8s`, the current temp directory, and `/home/lxl/seed-emulator-k8s-new`.
- Commands: inspected `build-cluster.log`; confirmed the bad helm was 40M and exited 139 while a prior cached helm was 51M and usable; copied the good helm into the current run cache; reran only `installKubeOvnFabric.py` for the half-built cluster; then ran only stages 8-10 from `applyK3sCluster.py` to apply K3s tuning, verify nodes, and write inventory outputs.
- Validation: Kube-OVN install completed; all K3s nodes are Ready; Multus, Kube-OVN CNI, OVS/OVN, controller, monitor, pinger, CoreDNS, metrics-server, and local-path pods are Running; `subnets.kubeovn.io` and `vpcs.kubeovn.io` CRDs are established; `cluster.inventory.yaml` and `kubeconfig.yaml` exist; Python compile and embedded shell syntax checks passed for all three patched `installKubeOvnFabric.py` files.
- Notes: the original run summary still records `build-cluster` as FAIL because the full orchestrator had already exited before manual recovery. The live cluster itself was recovered without recreating VMs or rerunning the full Ansible/K3s install.

## 2026-06-09 18:01 CST - B62 Persistent Setup Cache

- User intent: store reusable downloads and image tarballs under `b62_k8s_scale/base_image` so repeated B62 cluster builds avoid downloading Helm, Kube-OVN images, and bootstrap Docker images whenever possible.
- Scope: B62 assignment/rendered config, k8sTools setup config helpers, host-asset/K3s apply image-cache plumbing, and the local B62 `base_image` cache directory.
- Changes: added `seedemu.hostImageCacheDir` support to KVM and K3s config helpers; made `prepareHostAssets.py` and `applyK3sCluster.py` use that persistent host-side cache for saved bootstrap image tarballs; made B62 rendering default `seedemu.hostImageCacheDir` and first `seedemu.imageCacheDirs` entry to `base_image/image-cache`; made B62 rendering/default assignment set `ovn.helmCacheDir` to `base_image/helm`; copied existing image-cache tarballs, a validated Helm 3.15.4 binary/archive, and `kube-ovn_v1.15.12.tar` into `base_image`.
- Commands: listed existing `base_image`, origin image-cache, and previous run Helm cache contents; validated the 1078 Helm binary/archive and Kube-OVN tar; copied origin image tarballs plus validated Helm/Kube-OVN artifacts into `base_image`; inspected generated cache fields with `rg`; checked helper output through `manageKvmConfig.py` and a temporary `configK3s.yaml`; removed validation-generated `__pycache__` files and the temporary K3s config.
- Validation: `base_image/helm/bin/helm version --short` returns `v3.15.4+gfa9efb0`; `tar -tzf base_image/helm/download/helm-v3.15.4-linux-amd64.tar.gz` passed; `tar -tf base_image/helm/kube-ovn_v1.15.12.tar` reads OCI entries; `base_image/image-cache` contains registry, multus, SeedEMU base/router, Ubuntu, and Rancher bootstrap image tarballs; `python3 -m py_compile` passed for the changed setup/rendering Python files; helper smoke checks emit `seedemuHostImageCacheDir=/home/lxl/k8s/largeScale/b62_k8s_scale/base_image/image-cache` and `ovnHelmCacheDir=/home/lxl/k8s/largeScale/b62_k8s_scale/base_image/helm`.
- Notes: no KVM, libvirt, K3s, Kubernetes workload, image build/push, deploy, or destroy command was run. VM-internal `apt` and the `get.k3s.io` installer script are still external downloads in the current workflow; this change caches the reusable host-side image and Helm/Kube-OVN artifacts used by B62 setup.

## 2026-06-09 18:20 CST - B62 Local-First Cache And Offline Guard

- User intent: make B62 setup use `base_image` under the experiment working directory when present, download only missing reusable artifacts, and support a clearer offline mode for frequent reuse.
- Scope: `seedemu/k8sTools` config/path handling, KVM host-asset setup, K3s apply setup, Kube-OVN install setup, K3s Ansible inventory/playbook, and B62 assignment/rendered config/cache.
- Changes: relative setup paths such as `base_image/helm` are now resolved relative to the original input YAML before k8sTools copies setup files into `/tmp`; B62 now renders `kvm.baseImagePath` directly to `base_image/jammy-server-cloudimg-amd64.img`; host Docker image preparation now tries cached tarballs via `docker load` before `docker pull` or build; K3s apply uses the same local-first tarball logic without polluting captured tar-path stdout; Kube-OVN image tar, Helm binary/archive, and Helm chart are all cache-first and only downloaded when missing; Helm repo/cache state is kept under `base_image/helm/helm-home`; `seedemu.offline` is propagated to setup helpers and Ansible inventory; strict offline mode now fails fast before VM-side apt/curl paths that are not implemented as offline installs.
- Commands: re-rendered B62 `configKvmOvn.yaml`; checked KVM/K3s helper shell variables; downloaded and cached `base_image/helm/charts/kube-ovn-v1.15.12.tgz`; ran a temporary `makeKvmConfig()` path-resolution smoke test; generated a temporary K3s config and Ansible inventory; removed temporary files and validation-generated cache files.
- Validation: Python compile passed for changed Python files; extracted embedded shell bodies passed `bash -n`; `tar -tzf base_image/helm/charts/kube-ovn-v1.15.12.tgz` passed and contains `kube-ovn/Chart.yaml`; generated helper output points base image, image cache, and Helm cache to `b62_k8s_scale/base_image`; generated Ansible inventory includes `seedemu_offline: false`; `ansible-playbook --syntax-check` passed for the patched K3s install playbook.
- Notes: no VM, libvirt, K3s install, Kubernetes workload, image build/push, deploy, or destroy command was run. `seedemu.offline: true` is now safe as a no-network guard for cached host-side artifacts, but a fresh fully offline VM build still requires a prebuilt base image that already has Docker, K3s server/agent support, CNI plugins, and related apt-installed packages; otherwise the playbook fails early by design.

## 2026-06-09 21:18 CST - B62 Kube-OVN Subnet Timeout And Scale Tuning

- User intent: analyze the B62 4954 deploy failure at `[kube-ovn-subnets] timeout after 7200s`, determine whether Kube-OVN subnet parameters are wrong or inefficient, and change the timeout to 36000s.
- Scope: B62 `deploy.sh`, Kube-OVN install resource values, B62 assignment/rendered config, current run logs/artifacts, and live read-only cluster diagnostics.
- Changes: changed `DEPLOY_KUBE_OVN_SUBNET_TIMEOUT_SECONDS` from 7200 to 36000; added explicit `./deploy.sh <run_dir> --resume` mode so a deploy that already created CRD/foundation objects can wait for existing Subnets, apply Services idempotently, and then continue controller submission; added OVN large-scale resource fields to B62 assignment/config; made Kube-OVN Helm install consume `ovn.northdThreads`, controller CPU/memory, and ovn-central CPU/memory settings, with larger defaults than the chart's 1-core controller limit.
- Commands: inspected `deploy.log`, failure artifacts, `deploy.sh`, and Kube-OVN Helm chart templates/values; queried live nodes, Kube-OVN pods, Subnet CR status, controller describe/logs/current and previous logs, and node/pod top metrics; re-rendered B62 config; ran Helm template smoke validation against the cached chart.
- Validation: `bash -n deploy.sh` passed; Python compile passed for changed setup/rendering Python files; extracted embedded `installKubeOvnFabric.py` shell body passed `bash -n`; Helm template with the new resource `--set` values rendered controller and ovn-central resources correctly; live Subnet status progressed from the failed-run tail `processed=3677 ready=3673` to `processed=4719 ready=4718` with `error=0`, confirming continued controller progress after timeout.
- Notes: the failure was not caused by malformed Subnet parameters. The previous kube-ovn-controller container exited with `fatal error: concurrent map read and map write` after processing thousands of Subnets, then restarted and continued. The chart default controller limit was `1 CPU/1Gi`; current metrics showed the controller near 1 CPU and ovn-central also busy. I did not run the resumed deploy or patch live Kube-OVN resources because that would modify the active cluster; for the current partial run, use `./deploy.sh <run_dir> --resume` after or while Subnets finish processing.

## 2026-06-10 10:59 CST - B62 4954 Start-BIRD Recovery And Namespace Cleanup

- User intent: continue the B62 4954 full experiment after the pre-batch timeout, run it through if possible, then directly clear the current experiment namespace and summarize what happened.
- Scope: B62 run directories `20260610_072657_4954_w5` and `20260610_083211_4954_w5`, B62 start-BIRD/start-kernel/wait-ready helpers, the live namespace `seedemu-b62-4954`, and Kube-OVN Subnet/Event cleanup.
- Changes: added node-level concurrency control to `start_bird_node_local.py` and `start_bird_kernel_node_local.py`; changed `start_bird.sh` defaults to one node at a time with per-node parallelism 8 for the next run; added a pre-kernel SSH wait and node concurrency parameter in `start_bird_kernel.sh`; kept the lighter `wait-ready.sh` pod counting path.
- Commands: ran two B62 4954 full attempts with `runExperiment.sh`; interrupted stuck `start-bird` stages; reset/restarted only `seedemu-b62-master-w5` after master SSH became unresponsive; deleted namespace `seedemu-b62-4954`; force-deleted remaining pods and namespaced resources; removed finalizers from 1724 stuck Kube-OVN Subnet CRs; batch-deleted 31701 remaining `events.events.k8s.io` objects; verified namespace, Subnet, event, and active cleanup-process counts.
- Validation: run `20260610_072657_4954_w5` passed build-cluster, deploy in 962.93s, and wait-ready in 480.74s before `start-bird` was interrupted after 1427.74s; run `20260610_083211_4954_w5` passed build-cluster in 482.77s, compile in 255.03s, build in 211.31s, deploy in 963.13s, and wait-ready in 512.02s before `start-bird` was interrupted after 2553.54s. `bash -n` passed for `start_bird.sh`, `start_bird_kernel.sh`, and `wait-ready.sh`; `python3 -m py_compile` passed for both node-local helpers. Final cleanup checks show namespace `seedemu-b62-4954` not found, matching Kube-OVN Subnets 0, events 0, and no residual wait/start cleanup processes.
- Notes: the current blocker is still BIRD startup at 4954 scale. Deploy and initial readiness are no longer the blocker. Fully serial per-node startup left the master in a long partially-started BIRD state, SSH became unresponsive, and after VM reset the master's 826 pods became `Unknown`, leaving only 4128/4954 pods running. The next run should use the updated one-node-at-a-time, per-node-parallel-8 start-BIRD strategy, then continue to start-kernel if SSH remains healthy. The `bird-rtnl-dev` VM was not modified during the namespace cleanup.

## 2026-06-10 11:37 CST - B62 Cleanup And Load-Average Instrumentation

- User intent: make `clean.sh` safely and efficiently clear the current namespace plus matching Subnet/event leftovers, compare current B62 start-bird/start-kernel logic and parameters with `/home/lxl/k8s/lxl`, and log load/CPU/memory every 30s for every stage.
- Scope: B62 `clean.sh`, `lib.sh`, `runFullExperiment.py`, and a new `parameter_audit.md` under `largeScale/b62_k8s_scale`.
- Changes: rewrote `clean.sh` to delete namespaced workload resources, batch-delete `events.events.k8s.io` and core events, delete matching Kube-OVN IP/Subnet/Vpc objects, clear matching Kube-OVN finalizers after a grace period, and safely clear namespace finalizers only when no content remains; added `loadAverage.log` monitoring in `lib.sh` for standalone shell stages; added the same monitor in `runFullExperiment.py` for orchestrated stages and disabled duplicate child monitors; documented all timeout/build/deploy/start-bird/start-kernel parameters and differences from `/home/lxl/k8s/lxl`.
- Commands: inspected `/home/lxl/k8s/lxl/start-bird`, `/home/lxl/k8s/lxl/start-kernel`, `/home/lxl/k8s/lxl/test/start_bird.sh`, `/home/lxl/k8s/lxl/test/start_bird_kernel.sh`, both reference Python helpers, current B62 helpers, and `/home/lxl/seed-emulator/topology/cpu_monitor.sh`; ran shell/Python syntax checks; ran isolated monitor smoke tests in `/tmp`; removed validation-generated `__pycache__`.
- Validation: `bash -n` passed for `clean.sh`, `lib.sh`, `start_bird.sh`, `start_bird_kernel.sh`, `build.sh`, `deploy.sh`, and `wait-ready.sh`; `python3 -m py_compile` passed for `runFullExperiment.py`, `start_bird_node_local.py`, and `start_bird_kernel_node_local.py`; Python and shell monitor smoke tests wrote valid CSV rows with load, CPU, and memory and left no residual monitor processes.

## 2026-06-10 11:53 CST - B62 Subnet Batch Deployment

- User intent: decide whether Kube-OVN Subnets should also be created in batches and modify the deploy code if that is safer.
- Scope: B62 `deploy.sh` and `parameter_audit.md`.
- Changes: kept workload controller batch size at 40; added independent `DEPLOY_SUBNET_BATCH_SIZE=100` and `DEPLOY_SUBNET_BATCH_SLEEP_SECONDS=2`; split `Subnet` resources into `output/deploy_batches/01-subnets`; apply Subnets through generated `05-subnet-batches` files before waiting for Kube-OVN readiness; made resume mode require the new split directory before reusing old split artifacts.
- Commands: inspected the deploy split/apply flow and parameter audit; updated the script and documentation; ran `bash -n` and `rg` confirmations.
- Validation: `bash -n /home/lxl/k8s/largeScale/b62_k8s_scale/deploy.sh` passed; no K3s deploy, namespace mutation, VM, libvirt, or workload operation was run as part of this change.
- Notes: no namespace cleanup, cluster operation, VM operation, image build, deploy, start-bird, or start-kernel experiment was run. The comparison shows B62 is not a strict copy of `/home/lxl/k8s/lxl`: B62 uses node-local `crictl + nsenter`, while the reference uses `kubectl exec`; current B62 start-bird is one node at a time with per-node parallelism 8, while the reference is all nodes concurrently with node-internal serial execution and load-threshold waiting.

## 2026-06-10 11:47 CST - B62 Deploy Batch Size Review

- User intent: reduce the B62 deploy batch size to 40 and clarify which parameter controls Subnet creation speed.
- Scope: B62 `deploy.sh`, B62 `parameter_audit.md`.
- Changes: changed `DEPLOY_BATCH_SIZE` from 100 to 40; documented that `DEPLOY_BATCH_SIZE` only controls controller resources and does not control Kube-OVN Subnet creation speed.
- Commands: inspected `deploy.sh` split/apply paths for `Subnet`, `foundation`, and controller batches; ran `bash -n deploy.sh`; confirmed `DEPLOY_BATCH_SIZE=40` and the audit note with `rg`.
- Validation: shell syntax check passed. No Kubernetes deploy, namespace cleanup, VM action, or image build was run.
- Notes: This finding was true before the 11:53 change above. Subnet creation is now controlled by `DEPLOY_SUBNET_BATCH_SIZE` and `DEPLOY_SUBNET_BATCH_SLEEP_SECONDS`; `DEPLOY_KUBE_OVN_SUBNET_TIMEOUT_SECONDS` and `DEPLOY_PRESSURE_CHECK_SECONDS` still only affect waiting/monitoring.

## 2026-06-10 12:07 CST - B62 Start-BIRD/Kernel Kubectl-Exec Restore

- User intent: change B62 start-bird and start-kernel back to the `/home/lxl/k8s/lxl` execution model: all nodes concurrent, pods within each node serial, wait for load average below 40, and skip the final per-pod `birdc show status` verify stage.
- Scope: B62 `start_bird.sh`, `start_bird_kernel.sh`, `start_bird_helper.py`, `start_bird_kernel_helper.py`, `README.md`, and `parameter_audit.md`.
- Changes: switched both wrappers from node-local SSH/nsenter helpers to kubectl-exec helpers; exposed reference parameters directly at the top of the scripts; made start-bird wait for per-node load < 40 after the 60s settle instead of only warning; added explicit start/switch delay parameters; kept OVN-safe start-bird cleanup of stale `kernel.conf` and explicit kernel include so kernel export remains in the start-kernel phase.
- Commands: inspected `/home/lxl/k8s/lxl/start-bird`, `/home/lxl/k8s/lxl/start-kernel`, their Python helpers, and current B62 helpers; ran `chmod +x` on the rewritten kernel wrapper; ran `bash -n` on both wrappers; ran `python3 -m py_compile` on both helpers; ran both helper `--help` commands; confirmed wrappers call `start_bird_helper.py` and `start_bird_kernel_helper.py`; removed validation-generated `__pycache__`.
- Validation: shell syntax checks passed; Python compile checks passed; helper CLI argument parsing passed. No Kubernetes deploy, namespace mutation, VM/libvirt operation, image build, start-bird execution, start-kernel execution, or full experiment run was performed.
- Notes: This restores the reference kubectl-exec pressure profile, which is known to have worked in the prior macvlan setup. At 4954 scale it may put more pressure on the API server/kubelet than the node-local path; the node-local helper files remain available for fallback/debugging but are no longer the default wrapper path.

## 2026-06-10 14:05 CST - B62 K3s/Kube-OVN Rebuild Race And CNI Repair

- User intent: fix the failed B62 rebuild, explain why `node-token` disappeared, repair the Kube-OVN CNI damage path, document the issues in `problem.md`, and reinitialize the environment for the next full experiment.
- Scope: B62 lifecycle scripts, `seedemu/k8sTools` K3s/Kube-OVN setup resources, B62 problem documentation, and the live B62 KVM/K3s/Kube-OVN environment.
- Changes: added `.cluster-lifecycle.lock` plus child-process cleanup to `buildCluster.sh` and `destroyCluster.sh`; made build refuse to start while old destroy/OVN cleanup/helm-uninstall processes are present; added an explicit Ansible wait for active K3s service and `/var/lib/rancher/k3s/server/node-token`; sanitized both the configured K3s CNI bin dir and `/opt/cni/bin` before Kube-OVN install; created `b62_k8s_scale/problem.md` with the new failures and the relevant `lxl/README.md` problem index.
- Commands: inspected the failed build/destroy logs, master VM systemd/journal state, k8sTools Ansible/Kube-OVN scripts, and `lxl/README.md`; ran `bash -n` on B62 lifecycle scripts; ran `python3 -m py_compile` on changed k8sTools Python files; ran `destroyCluster.sh` in `runs/reinit_after_fix_20260610_135116`; ran `buildCluster.sh` in `runs/rebuild_after_fix_20260610_135204`.
- Validation: destroy removed K3s/Kube-OVN, six KVM VMs, disk state, and `seedemu-b62-net-w5`; rebuild completed with six Ready nodes; Kube-OVN controller, ovn-central, ovs-ovn, and kube-ovn-cni all rolled out; kube-system Pods later converged to 1/1 Running; all six nodes had active K3s/k3s-agent, a present `/run/openvswitch/kube-ovn-daemon.sock`, and no dangling `loopback`, `portmap`, or `kube-ovn` links in checked CNI paths; no `seedemu-b62-*` workload namespace remained.
- Notes: Root cause of the missing token was an old destroy process continuing after an interrupted/timed-out cleanup and running `k3s-uninstall.sh` against the newly rebuilt master. Root cause of the earlier deploy stall was master-side Kube-OVN CNI failure, not BIRD. The rebuilt cluster is intentionally empty of workload namespaces and ready for the next experiment stage.

## 2026-06-10 14:20 CST - B62 Route Count Test Script

- User intent: add `./test.sh "${RUN_DIR}"` to enter 10 different AS brd pods, compare `ip route | wc` count against `birdc show route count` network count, print failing Pod names, and write `test.json`.
- Scope: `b62_k8s_scale/test.sh`.
- Changes: added a read-only test stage script that derives namespace/kubeconfig from `lib.sh`, selects one Running brd Pod per AS, checks 10 ASes, writes `test.log` and `test.json`, exits nonzero on failure, and prints each failing Pod name.
- Commands: created the script, made it executable, ran `bash -n`, and compiled the embedded Python heredoc.
- Validation: shell syntax and embedded Python compile checks passed.
- Notes: I did not run the live test because the current rebuilt cluster has no B62 workload namespace deployed yet; running it now would only fail at namespace lookup.

## 2026-06-13 12:58 CST - B62 4954 Bird 0.1s Load80 Experiment

- User intent: rerun the B62 4954 experiment with `start-bird` interval `0.1s`, load threshold `80`, run the existing BGP test after `start-bird`, run `start-kernel`, then validate 10 different-AS BRD nodes with `ip route | wc > birdc show route count` and report timings/status.
- Scope: `largeScale/b62_k8s_scale/start_bird.sh`, `start_bird_kernel.sh`, `test.sh`, new/updated `test_kernel.sh`, and run directory `runs/20260613_103142_4954_bird01_load80`.
- Changes: set `BIRD_START_DELAY_SECONDS=0.1` and `BIRD_LOAD_THRESHOLD=80`; kept `KERNEL_LOAD_THRESHOLD=80`; added `test_kernel.sh`; fixed route-test target selection to require real `as<asn>brd-r<rid>-...` pods and choose the smallest `r` per AS; added `test_kernel.sh` fallback from failing `kubectl exec` to SSH + `crictl exec` on the target node.
- Commands: ran `clean.sh`, `preflight.sh`, `compile.sh`, `build.sh`, `deploy.sh`, `wait-ready.sh`, `start_bird.sh`, `python3 test.py`, `start_bird_kernel.sh`, and multiple `test_kernel.sh` runs; used read-only `kubectl`, SSH, `crictl`, and BIRD commands to diagnose worker3 kubelet exec failures and one eBGP connectivity sample.
- Validation: clean/preflight/compile/build/deploy/wait-ready/start-bird/start-kernel all exited 0; `start_bird` started 4954/4954 BIRD processes in 492s; `start-kernel` switched 4954/4954 in 662s; final `test_kernel.sh` passed 10/10 route-count checks and wrote `test_kernel.json`.
- Notes: the BGP protocol test after `start-bird` failed after 1896s with 77 verified and 597 failures; 485 were protocol-not-up and 112 were worker3 kubelet proxy `502 Bad Gateway`. A sample failed eBGP pair, AS1269 to AS3506 over `1.2.0.0/16`, still showed BGP `Connect` and ping loss after `start-kernel`, pointing to partial secondary-network/OVN data-plane connectivity issues rather than missing BIRD processes. Final run logs are under `runs/20260613_103142_4954_bird01_load80`.

## 2026-06-15 02:09 CST - B62 4954 Rerun With 0.1s Start-BIRD And Kernel Test

- User intent: rerun the B62 4954 experiment with `start-bird` interval `0.1s`, load threshold `80`, run `test.sh` after `start-bird`, run `start-kernel`, then run `test_kernel.sh` to check 10 distinct-AS BRD pods with `ip route | wc > birdc show route count`.
- Scope: `largeScale/b62_k8s_scale/deploy.sh`, `test.sh`, `assignment.yaml`, live KVM/K3s/Kube-OVN environment, and run directory `runs/20260615_002235_4954_w5`.
- Changes: restored the running `DEPLOY_BATCH_SIZE` from 6 to 40 before the final run; kept `BIRD_START_DELAY_SECONDS=0.1`, `BIRD_LOAD_THRESHOLD=80`, `KERNEL_SWITCH_DELAY_SECONDS=0.3`, and `KERNEL_LOAD_THRESHOLD=80`; kept the data-plane assignment on macvlan with Kube-OVN IPAM/non-primary CNI because the pure Kube-OVN attached mode had repeatedly stalled in deploy; enhanced `test.sh` earlier in the turn with SSH+crictl fallback and strict BRD target selection.
- Commands: stopped an initial partial run `runs/20260614_233248_4954_w5` after finding it was using batch size 6; ran `clean.sh` against that run and fully deleted namespace `seedemu-b62-4954` plus matching Kube-OVN resources; ran `bash -n` for `deploy.sh`, `start_bird.sh`, `start_bird_kernel.sh`, `test.sh`, and `test_kernel.sh`; executed `./runExperiment.sh` for `runs/20260615_002235_4954_w5`; monitored summary, Pod counts, deploy logs, BIRD logs, and loadAverage samples.
- Validation: final run passed build-cluster, compile, build, deploy, wait-ready, start-bird, start-kernel, and `test_kernel.sh`; deploy created 4954 Pods with 0 failed Pods; `start-bird` started 4954/4954 BIRD processes in 383.46s; `start-kernel` switched 4954/4954 in 476.41s; `test_kernel.sh` passed 10/10 sampled BRD pods and wrote `test_kernel.json`.
- Notes: `test.sh` immediately after `start-bird` failed 10/10 because kernel export was not enabled yet, so `ip route` counts were far below BIRD network counts; this is expected for that specific route-count predicate before `start-kernel`. Host `loadAverage.log` showed deploy max load1 58.73, start-bird max load1 221.26, and start-kernel max load1 178.26. The 0.1s start-bird interval is functional but causes a large transient host load spike.

## 2026-06-15 12:23 CST - B62 OVN/OVS Error Explanation Document

- User intent: document the OVN/OVS-related eBGP failure symptoms, analysis chain, conclusion, improvement path, and whether the final improvement provides per-network layer-2 isolation.
- Scope: `largeScale/b62_k8s_scale/Error_ovn.md`, with references to prior B62 `problem.md`, `bird.md`, and experiment worklog findings.
- Changes: added a standalone Chinese analysis document that separates early eBGP data-plane symptoms from the later pure Kube-OVN attached deploy blocker; documented observed kubelet/OVS timeout events, why BIRD/timer/Subnet syntax were not the main cause, why controller argument tuning did not solve pure OVN attached mode, and why the final macvlan plus Kube-OVN IPAM path was used.
- Commands: inspected existing `problem.md`, `bird.md`, `assignment.yaml`, and worklog records; created `Error_ovn.md`; checked the resulting sections and key terms with `sed` and `rg`.
- Validation: documentation-only change; no K3s, VM, namespace, deploy, image, or BIRD experiment commands were run.
- Notes: the document explicitly states that the final `macvlan + Kube-OVN IPAM` path can run the BGP/route-convergence experiment but does not strictly provide one isolated layer-2 broadcast domain per SeedEMU network. Strict per-network L2 isolation still requires a scalable pure Kube-OVN/OVS design, VLAN/bridge isolation, or topology/VPC sharding.

## 2026-06-15 13:22 CST - B62 Pure OVN/OVS 1897 Scale Profile

- User intent: implement pure OVN+OVS incremental testing, collect logical switch count, OVS/`ovs-vsctl` timing, `ovn-northd`/OVN component CPU, `kube-ovn-controller` queue, and CNI ADD latency, then use the results to judge how to remove the OVN/OVS scale limit.
- Scope: `largeScale/b62_k8s_scale/collectOvnMetrics.py`, `runOvnScaleProfile.py`, `Error_ovn.md`, live B62 K3s/Kube-OVN cluster, and run directory `runs/ovn_profile_20260615_130537_1897_w5`.
- Changes: added a read-only OVN/OVS metric collector; added a pure Kube-OVN scale profile runner that renders `networking.backend/cniType/localLinkCniType/attachedCniType=kube-ovn`; added Subnet processed/status-key and controller/CNI log counters to the collector; appended the 1897 pure OVN profile result and next-step design recommendations to `Error_ovn.md`.
- Commands: cleaned the previous `seedemu-b62-4954` namespace and matching Kube-OVN CRs; ran `python3 runOvnScaleProfile.py --scales 1897 2599 3083 4192 --metrics-interval 20 --clean-after --stop-on-fail`; interrupted the 1897 deploy after Subnet progress remained stuck; collected manual metric snapshots; ran `clean.sh` for the 1897 run; verified namespace/CR cleanup and node readiness.
- Validation: `python3 -m py_compile` passed for both new scripts; 1897 clean/preflight/compile/build passed; deploy was intentionally interrupted after 432.32s because this run stayed at 1876 Subnets total, 21 processed, 18 Ready, and 0 Pods for multiple samples; cleanup fully deleted `seedemu-b62-ovn-1897` and matching Kube-OVN resources; all six nodes remained Ready.
- Notes: key 1897 metrics were `Logical_Switch=20`, `Logical_Switch_Port=26`, `Logical_Router_Static_Route=978`, `AddSubnet queue=1873`, `UpdateSubnetStatus queue=747`, max slow OVN NB operation `2013ms`, max sampled `ovs-vsctl` query `10ms`, no `ovs-vsctl` timeouts, and no CNI ADD samples because no Pod was created. Larger pure OVN scales were not run because this 1897 run showed no Subnet reconcile progress in the observation window; this is an interrupted abnormal run, not proof that 1897 pure OVN can never complete.

## 2026-06-15 15:25 CST - B62 Pure OVN/OVS 1078 Clean Rebuild Profile

- User intent: after the 1897 abnormal stop, allow VM destroy/K3s/Kube-OVN/OVS reinstall and first try 1078 scale to observe Subnet behavior.
- Scope: live B62 KVM/K3s/Kube-OVN environment, `largeScale/b62_k8s_scale/Error_ovn.md`, and run directory `runs/ovn_profile_20260615_143633_1078_w5`.
- Changes: documentation only after the run; appended the 1078 clean-rebuild Subnet/CNI findings to `Error_ovn.md`.
- Commands: ran `python3 runOvnScaleProfile.py --scales 1078 --metrics-interval 10 --rebuild-first --clean-after --stop-on-fail`; stopped the profile after Subnet completion and first-Pod CNI/OVS evidence was collected; ran `clean.sh runs/ovn_profile_20260615_143633_1078_w5`; verified nodes, kube-system OVN/OVS/Multus Pods, namespace deletion, matching Kube-OVN CR deletion, and libvirt VM state.
- Validation: cluster rebuild passed; six VMs are running; six K3s nodes are Ready; Multus, kube-ovn-controller, ovn-central, ovs-ovn, and kube-ovn-cni Pods are Running; `seedemu-b62-ovn-1078` and matching Kube-OVN IP/Subnet/VPC resources are deleted.
- Notes: 1078 produced 1065 Subnets and reached `processed=1065 ready=1065`; peak `kube-ovn-controller` CPU was about `1439m`, peak `ovn-central` CPU about `2029m`, and peak master `ovs-ovn` CPU about `1152m`. After the 900s cooldown, the first workload Pod hit CNI/OVS add-port instability, including 3 sandbox failures, 2 interface-not-ready events, 1 `ovs-vsctl --timeout=30` event, and CNI log samples up to about `30035ms`; the Pod later recovered to Running before cleanup. This indicates 1078 can finish the Subnet stage after a clean rebuild, but Pod CNI ADD becomes the next pressure point.

## 2026-06-15 17:00 CST - B62 OVN Resource-Tuned 1078/1897 Probe

- User intent: try small experiments to observe whether increasing OVN/Kube-OVN resources improves pure OVN+OVS scale behavior and identify the next improvement direction.
- Scope: `largeScale/b62_k8s_scale/assignment_ovn_tuned.yaml`, `Error_ovn.md`, live B62 KVM/K3s/Kube-OVN environment, and run directories `runs/ovn_profile_20260615_155916_1078_w5` and `runs/ovn_profile_20260615_162744_1897_w5`.
- Changes: created experiment-only `assignment_ovn_tuned.yaml` with larger Kube-OVN/OVS requests, limits, `northdThreads`, and `controllerWorkerNum`; appended the tuned 1078/1897 findings and improvement recommendations to `Error_ovn.md`. Original `assignment.yaml` was not modified.
- Commands: rendered the tuned assignment and checked generated config values; ran `python3 -m py_compile runOvnScaleProfile.py renderAssignmentConfig.py collectOvnMetrics.py`; ran `python3 runOvnScaleProfile.py --assignment assignment_ovn_tuned.yaml --scales 1078 --namespace-template 'seedemu-b62-ovn-tuned-{scale}' --metrics-interval 10 --rebuild-first --clean-after --stop-on-fail`; ran the same profile for 1897 without rebuild; attempted 1500 but preflight failed because `/data/lxl/seed-emulator/topology/real_topology_1500.txt` is missing; collected stall/post-interrupt/subnet-complete OVN metric snapshots; ran `clean.sh` for the 1078 and 1897 tuned run directories; checked nodes, kube-system Pods, and residual namespace/Subnet state.
- Validation: 1078 rebuilt the cluster, finished Subnet convergence to `1065/1065`, and was interrupted only during the 900s cooldown before Pods; 1897 finished compile/build, created 1876 Subnets, paused for a long static-route phase, then kube-ovn-controller continued in the background to `1876/1876` after deploy was interrupted. Final cleanup removed `seedemu-b62-ovn-tuned-1897` and matching Kube-OVN resources; six K3s nodes are Ready and 30 kube-system Pods are Running.
- Notes: tuned resources did not remove the Subnet plateau. In 1078, peak sampled `kube-ovn-controller` and `ovn-central` CPU were about `1473m` and `2193m`; in 1897 they were about `1482m` and `1125m`, far below the new limits. The observed bottleneck is single-VPC static route/OVNDB mutation followed by logical-switch/status catch-up, not CPU limit saturation. No 1897 Pod/CNI ADD samples were produced because the run was stopped before workload Pod creation.

## 2026-06-15 22:25 CST - B62 OVN Feature Flags And VPC Sharding Probe

- User intent: after confirming that reducing Subnet count conflicts with SeedEMU link semantics, try Kube-OVN version upgrade support, disabling unnecessary Kube-OVN features, and VPC/logical-router sharding.
- Scope: `seedemu/k8sTools/resources/setup/manageK3sConfig.py`, `seedemu/k8sTools/resources/setup/ovn/installKubeOvnFabric.py`, `seedemu/k8sTools/resources/running/manageK8sManifest.py`, `largeScale/b62_k8s_scale/lib.sh`, `runOvnScaleProfile.py`, `Error_ovn.md`, and live B62 run `runs/ovn_profile_20260615_221817_1078_w5`.
- Changes: added Kube-OVN Helm feature toggles for LB/NP/EIP-SNAT/NAT-GW/LB-SVC/external-VPC/gateway-check; added `SEED_KUBE_OVN_VPC_SHARDS` manifest rendering support; changed B62 runtime manifest rendering to regenerate every time; added `--ovn-chart-version`, `--disable-extra-ovn-features`, and `--vpc-shards` to the pure OVN profile runner; documented why Subnet count should not be reduced by default.
- Commands: ran Python compile checks for changed Python files; ran `bash -n` for B62 shell scripts; smoke-tested `load_assignment_runtime`; rendered a 1078 `vpcShards=4` manifest offline and validated no duplicate resources; verified profile assignment generation for v1.16.2 plus feature-off plus 8 shards; extracted and `bash -n` checked the embedded Kube-OVN install shell; ran `python3 runOvnScaleProfile.py --assignment assignment_ovn_tuned.yaml --scales 1078 --namespace-template 'seedemu-b62-ovn-shard4-{scale}' --vpc-shards 4 --metrics-interval 15 --no-stop-on-fail`.
- Validation: offline render produced 4 VPCs, 1065 Subnets, 1065 NADs, and 1078 Deployments; live 1078 shard4 run created 4 VPCs and all 1065 Subnets reached Ready with 0 errors; at the time of this entry deploy was in the configured 900s post-Subnet cooldown before workload Pod creation.
- Notes: official Kube-OVN docs support the tested directions: latest release is v1.16.2, setup options document LB/NP/EIP-SNAT disable switches, and VPC docs state each VPC maps to an OVN logical router. Pod/CNI ADD results are still pending for the active run.

## 2026-06-15 23:32 CST - B62 OVN Feature-Off Shard4 CNI Result

- User intent: continue trying directions 1, 2, and 4 for pure OVN+OVS scale while preserving SeedEMU Subnet semantics.
- Scope: live B62 K3s/Kube-OVN environment, `largeScale/b62_k8s_scale/Error_ovn.md`, and run directory `runs/ovn_profile_20260615_225357_1078_w5`.
- Changes: documentation only after the run; appended the final shard4 and feature-off CNI observations to `Error_ovn.md`.
- Commands: monitored the active feature-off run; verified `ovnEnableLb/ovnEnableNp/ovnEnableEipSnat/ovnEnableNatGw/ovnEnableLbSvc/ovnEnableExternalVpc/ovnCheckGateway=false`; collected Pod events and one post-stop OVN metric sample; stopped the run after reproducing first-Pod CNI ADD failure; ran `clean.sh runs/ovn_profile_20260615_225357_1078_w5`.
- Validation: K3s/Kube-OVN rebuild passed; Subnet convergence reached `1065/1065 Ready`; cleanup fully deleted namespace `seedemu-b62-ovn-featureoff-shard4-1078` and matching Kube-OVN IP/Subnet/VPC resources.
- Notes: disabling extra Kube-OVN features did not fix the pure OVN attached CNI path. The first workload Pod failed on secondary network `net-ix-ix3` with `ovs interface ... is not ready after 30s`, then hit `ovs-vsctl --timeout=30 ... add-port br-int ... signal: alarm clock`. Stop-time metrics recorded `failed_create_pod_sandbox=2`, `ovs_interface_not_ready=1`, `ovs_vsctl_timeout=1`, and CNI `ovs_add_port_log_max_ms=30031`.

## 2026-06-16 00:05 CST - B62 OVN v1.16.2 Feature-Off Shard4 Probe

- User intent: try the Kube-OVN version-upgrade direction after feature-off and VPC sharding did not remove the pure OVN attached CNI failure.
- Scope: live B62 KVM/K3s/Kube-OVN environment, `largeScale/b62_k8s_scale/Error_ovn.md`, and run directory `runs/ovn_profile_20260615_232825_1078_w5`.
- Changes: documentation only after the run; appended the v1.16.2 result and cached artifact paths to `Error_ovn.md`.
- Commands: ran `python3 runOvnScaleProfile.py --assignment assignment_ovn_tuned.yaml --scales 1078 --namespace-template 'seedemu-b62-ovn-v1162-featureoff-shard4-{scale}' --vpc-shards 4 --disable-extra-ovn-features --ovn-chart-version v1.16.2 --metrics-interval 15 --rebuild-first --no-stop-on-fail`; monitored K3s/Kube-OVN rebuild, Subnet convergence, first-Pod events, and OVN metrics; collected stop-time metrics; ran `clean.sh runs/ovn_profile_20260615_232825_1078_w5`.
- Validation: Kube-OVN v1.16.2 installed and rolled out; six K3s nodes are Ready; Subnet convergence reached `1065/1065 Ready`; cleanup fully deleted namespace `seedemu-b62-ovn-v1162-featureoff-shard4-1078` and matching Kube-OVN IP/Subnet/VPC resources; Multus, kube-ovn-cni, ovn-central, and ovs-ovn are Running after cleanup.
- Notes: v1.16.2 did not remove the first-Pod secondary-interface failure. Events showed `ovs interface ... is not ready after 30s`, then `ovs-vsctl --timeout=30 ... add-port br-int ... signal: alarm clock`; stop-time metrics recorded `failed_create_pod_sandbox=2`, `ovs_interface_not_ready=1`, `ovs_vsctl_timeout=1`, and CNI `ovs_add_port_log_max_ms=30033`. The v1.16.2 chart and image are now cached under `base_image/helm`.

## 2026-06-16 10:14 CST - B62 Pure OVN CNI ADD Timeout Mitigation Patch

- User intent: continue solving the pure OVN+OVS CNI ADD blocker so 1078 can deploy and then scale upward.
- Scope: `largeScale/b62_k8s_scale/seed_k8s_plan_real_topology_by_as.py`, `lib.sh`, `deploy.sh`, `runOvnScaleProfile.py`, `assignment_ovn_tuned.yaml`, `Error_ovn.md`, `seedemu/k8sTools/resources/setup/manageK3sConfig.py`, and `seedemu/k8sTools/resources/setup/ovn/installKubeOvnFabric.py`.
- Changes: added assignment-driven control-plane exclusion for workload placement; made pure OVN profiles default to `ovs-vsctl` CNI concurrency 1 and timeout 180s; added `ovn.cniOvsVsctlTimeoutSeconds` to the K3s/Kube-OVN config chain; patched kube-ovn-cni startup to install an `ovs-vsctl` wrapper that raises low positive timeouts; made kube-ovn attached deploy use smaller batches and log per-node ContainerCreating pressure.
- Commands: inspected live VM/K3s/kube-system status; checked kube-ovn-cni daemon flags and PATH; ran `python3 -m py_compile` on changed Python files; ran `bash -n` on changed shell scripts; extracted and `bash -n` checked the embedded Kube-OVN install shell; smoke-rendered `assignment_ovn_tuned.yaml`; smoke-generated a pure OVN profile assignment.
- Validation: Python compile checks passed; shell syntax checks passed; embedded Kube-OVN install shell syntax passed; smoke profile assignment recorded `networking.*=kube-ovn`, `cniOvsVsctlConcurrency=1`, `cniOvsVsctlTimeoutSeconds=180`, `vpcShards=4`, and `placement.excludeControlPlane=true`.
- Notes: no destructive cluster action or live deployment was run in this patch step. The next required validation is a real 1078 run with K3s/Kube-OVN rebuild so the patched kube-ovn-cni DaemonSet is installed from the updated setup script.

## 2026-06-16 13:56 CST - B62 Pure OVN 1078 Timeout Mitigation Validation

- User intent: continue validating the pure OVN+OVS fix and determine whether the CNI ADD timeout blocker is removed at 1078 scale.
- Scope: live B62 KVM/K3s/Kube-OVN cluster, run directory `largeScale/b62_k8s_scale/runs/ovn_profile_20260616_101529_1078_w5`, `Error_ovn.md`, and this worklog.
- Changes: no additional source changes during the validation run; the profile result was appended to `Error_ovn.md`.
- Commands: ran `python3 runOvnScaleProfile.py --assignment assignment_ovn_tuned.yaml --scales 1078 --namespace-template 'seedemu-b62-ovn-timeout180-shard4-{scale}' --vpc-shards 4 --disable-extra-ovn-features --ovn-chart-version v1.16.2 --cni-ovs-vsctl-concurrency 1 --cni-ovs-vsctl-timeout-seconds 180 --metrics-interval 15 --rebuild-first --no-stop-on-fail`; monitored `summary.json`, `deploy.log`, Kube-OVN resources, nodes, kube-system Pods, and cleanup state.
- Validation: run status `PASS`; build-cluster passed in 481.73s, compile in 11.13s, build in 72.84s, deploy in 12324.6s, wait-ready in 1.26s, and clean-after-profile in 324.04s. All 1065 Subnets reached Ready; 1078 Pods were created with zero `FailedCreatePodSandBox`, zero `ovs-vsctl` timeout events, and zero `interface not ready after 30s` events.
- Notes: this proves the previous ADD-path 30s timeout can be mitigated at 1078 by CNI `ovs-vsctl` timeout lifting, CNI OVS concurrency 1, worker-only placement, and conservative deploy pacing. The run auto-cleaned, so a second `--no-clean-after` run was started afterward to keep the namespace for BIRD/kernel validation.

## 2026-06-16 14:32 CST - B62 Pure OVN 1078 No-Rebuild Rerun Interrupted

- User intent: keep a 1078 pure OVN namespace after deploy so start-bird, BGP protocol testing, start-kernel, and kernel route testing can run.
- Scope: live B62 K3s/Kube-OVN cluster, run directory `largeScale/b62_k8s_scale/runs/ovn_profile_20260616_135808_1078_w5`, `Error_ovn.md`, and this worklog.
- Changes: appended the no-rebuild CNI ready-wait finding to `Error_ovn.md`; no source code changes were made for this observation.
- Commands: ran `python3 runOvnScaleProfile.py --assignment assignment_ovn_tuned.yaml --scales 1078 --namespace-template 'seedemu-b62-ovn-timeout180-shard4-{scale}-keep' --vpc-shards 4 --disable-extra-ovn-features --ovn-chart-version v1.16.2 --cni-ovs-vsctl-concurrency 1 --cni-ovs-vsctl-timeout-seconds 180 --metrics-interval 15 --no-clean-after --no-stop-on-fail`; interrupted it with `SIGINT`; ran `./clean.sh runs/ovn_profile_20260616_135808_1078_w5`; verified matching namespace/IP/Subnet/VPC residual counts were zero.
- Validation: the no-rebuild deploy was intentionally interrupted after 1759.35s because the first Pods repeatedly hit Kube-OVN CNI `ovs interface ... is not ready after 30s`; cleanup completed in about 92s and removed the namespace plus matching Kube-OVN CRs.
- Notes: the event pattern was different from the old `ovs-vsctl --timeout=30 alarm clock` failure. The wrapper allowed an `ovs-vsctl` add-port operation to run beyond 30s, but Kube-OVN daemon's internal interface-ready wait still produced 30s failures. A cold `--rebuild-first --no-clean-after` rerun was started afterward.

## 2026-06-16 19:55 CST - B62 Pure OVN 1078 Ifskip Validation PASS

- User intent: continue the interrupted/long-running pure OVN+OVS validation after the chat stream disconnected, and verify whether the 1078 run completed normally.
- Scope: live B62 KVM/K3s/Kube-OVN cluster, run directory `largeScale/b62_k8s_scale/runs/ovn_profile_20260616_160915_1078_w5`, `Error_ovn.md`, and this worklog.
- Changes: appended the final ifskip interpretation to `Error_ovn.md`; no source code changes were made in this continuation step.
- Commands: resumed/polled the existing `runOvnScaleProfile.py` session; checked `ps` for the profile process; checked namespace Pod/Deployment counts and Warning events with `kubectl`; inspected `summary.json`, `deploy.log`, `wait-ready.log`, `ovn_metrics_summary.json`, and sampled `ovn_metrics_samples.jsonl`.
- Validation: run status `PASS`; deploy passed in 9993.82s, wait-ready passed in 1.14s, and 1078/1078 Pods were Running and Ready. Event counters stayed at zero for `FailedCreatePodSandBox`, `ovs_interface_not_ready`, `ovs_vsctl_timeout`, and `add_nic_to_ovs_failed`.
- Notes: final OVN DB counts were 1067 logical switches, 1066 static routes, 1079 router policies, 3885 logical switch ports, and 4952 southbound port bindings. Final Pod lifecycle/ready latency was p50=20s, p90=21s, p99=46s, max=99s. The chat stream disconnect was only an interaction-layer issue; the local experiment kept running and completed.

## 2026-06-16 22:57 CST - B62 Pure OVN Deploy Pacing Optimization

- User intent: reduce the 1078-scale pure OVN deploy time after the previous successful run spent 9993.82s in deploy.
- Scope: `/home/lxl/k8s/largeScale/b62_k8s_scale/deploy.sh`, `runOvnScaleProfile.py`, `assignment_ovn_tuned.yaml`, and `Error_ovn.md`.
- Changes: raised pure OVN controller batch size from the old effective 5 to 40, removed per-batch sleep, reduced warmup to 2x5, allowed bounded in-flight Pending/Creating Pods, shortened pressure polling to 5s, increased subnet batch size to 250, removed the 900s post-subnet cooldown, and made `vpcShards=4` the explicit tuned/default pure OVN profile value.
- Commands: inspected `summary.json`, `deploy.log`, and `ovn_metrics_summary.json`; ran `bash -n deploy.sh`; ran `python3 -m py_compile runOvnScaleProfile.py`; loaded `assignment_ovn_tuned.yaml` with PyYAML to confirm `ovn.vpcShards=4`; calculated that 1078 controllers now need about 29 deploy batches instead of 232.
- Validation: static shell/Python checks passed; no new VM, namespace, or workload experiment was started in this optimization pass.
- Notes: the tuning keeps Kube-OVN CNI `cniOvsVsctlConcurrency=1`, so per-node OVS mutations remain serialized while deploy allows a limited cluster-wide in-flight window.

## 2026-06-16 23:53 CST - B62 Pure OVN Deploy Pacing Runtime Recheck

- User intent: continue running after the previous chat stream disconnected, and verify whether the deploy-time optimization can actually reduce runtime safely.
- Scope: live B62 K3s/Kube-OVN cluster, `/home/lxl/k8s/largeScale/b62_k8s_scale/deploy.sh`, `Error_ovn.md`, and run directories `runs/ovn_profile_20260616_230729_1078_w5`, `runs/ovn_profile_20260616_231731_1078_w5`, `runs/ovn_profile_20260616_232715_1078_w5`, `runs/ovn_profile_20260616_234008_1078_w5`.
- Changes: reverted the unsafe pure OVN deploy window back to stable `DEPLOY_BATCH_SIZE=5`, `DEPLOY_MAX_CREATING_PODS=0`, and `DEPLOY_MAX_CREATING_PODS_PER_NODE=0`; kept safer non-concurrency optimizations including subnet batch size 250, zero subnet batch sleep, zero post-subnet cooldown, zero controller batch sleep, and 5s pressure polling; appended the runtime findings to `Error_ovn.md`.
- Commands: cleaned the previous kept namespace; ran four 1078-scale probes with progressively smaller controller in-flight windows; interrupted and cleaned each unsafe/diagnostic run; checked Warning events, Pod phases by node, node CPU/memory, live kube-ovn-cni DaemonSet flags, `summary.json`, and deploy logs; ran `bash -n deploy.sh`.
- Validation: all test namespaces and matching Kube-OVN IP/Subnet/VPC resources were deleted after interruption; current `kubectl get ns | egrep 'seedemu-b62|NAME'` shows no `seedemu-b62-*` workload namespace; live kube-ovn-cni still has the 180s `ovs-vsctl` wrapper and `--ovs-vsctl-concurrency=1`.
- Notes: `batch=40` failed with about 130 ContainerCreating and per-node peaks around 26; `batch=10` failed around 30 ContainerCreating and per-node peaks around 6; the shallow pipeline `batch=5`, `creating<=5`, `creatingPerNode<=1` still failed as soon as it reached per-node 2. The stable setting `batch=5`, `creating<=0`, `creatingPerNode<=0` produced no sandbox failures through the first three batches but was interrupted because it only repeated the already-known long safe path. The bottleneck is therefore not simple CPU pressure but per-node Kube-OVN/OVS CNI ADD serialization.
## 2026-06-17 09:41 - 1078 OVN Deploy Node-Stream Limit Prep

- User intent: continue the 1078-scale pure OVN deploy optimization sweep and evaluate whether controlled per-node CNI ADD overlap improves deploy time without reintroducing OVS add-port timeouts.
- Scope: `/home/lxl/k8s/largeScale/b62_k8s_scale/deploy.sh`, `/home/lxl/k8s/largeScale/b62_k8s_scale/runOvnScaleProfile.py`, and the previous failed run namespace `seedemu-b62-ovn-gwskip-b40-1078`.
- Changes: added `nodeStreamMaxActivePerNode` deploy tuning, CLI passthrough `--deploy-node-stream-max-active-per-node`, and a `node_active_count` gate so node-stream limits not-yet-Running Pods per Kubernetes node instead of relying only on visible `ContainerCreating` reasons.
- Commands: stopped the stale `gwskip-b40` run processes; ran `clean.sh runs/ovn_profile_20260617_092555_1078_w5`, which fully removed the namespace and matching Kube-OVN subnet/VPC resources; checked `kubectl get nodes` and kube-system pods to confirm the current K3s/Kube-OVN cluster is Ready.
- Validation: `bash -n deploy.sh`, `python3 -m py_compile runOvnScaleProfile.py`, and `runOvnScaleProfile.py --help | rg 'node-stream-max|gateway-check|controller-apply'` passed.
- Notes: the prior `batch=40` + gateway-check-skip attempt failed because node-local concurrency still reached about 32 creating Pods per worker and produced many `add nic to ovs failed context canceled by timeout` events. Next run should test `node-stream` with `nodeStreamMaxActivePerNode=2`.
## 2026-06-17 09:52 - 1078 OVN Deploy MaxActive=2 Result

- User intent: test whether gateway-check-skip plus node-stream with two node-local active CNI ADDs can complete 1078-scale pure OVN deploy faster than strict serial submission.
- Scope: runtime experiment `/home/lxl/k8s/largeScale/b62_k8s_scale/runs/ovn_profile_20260617_094157_1078_w5` and namespace `seedemu-b62-ovn-gwskip-ns2-1078`.
- Changes: no additional source changes in this step.
- Commands: ran `runOvnScaleProfile.py ... --deploy-controller-apply-mode node-stream --deploy-node-stream-max-active-per-node 2 --deploy-skip-kube-ovn-gateway-check`; sampled pods, events, metrics, and logs; stopped the failing run process group; ran `clean.sh runs/ovn_profile_20260617_094157_1078_w5`.
- Validation: compile passed in 11.02s, build passed in 71.78s, all 1065 Kube-OVN Subnets reached ready; deploy failed at the first controller wave with 10 Pending Pods and 35+ `add nic to ovs failed context canceled by timeout` / `FailedCreatePodSandBox` events.
- Notes: `maxActivePerNode=2` is not safe on the current w5 pure OVN cluster. The next valid comparison is `maxActivePerNode=1` with gateway-check-skip.
## 2026-06-17 12:05 - 1078 OVN Deploy MaxActive=1 Pass

- User intent: continue the 1078-scale pure OVN deploy optimization sweep and verify a stable deploy path after `maxActivePerNode=2` failed.
- Scope: runtime experiment `/home/lxl/k8s/largeScale/b62_k8s_scale/runs/ovn_profile_20260617_095318_1078_w5`, namespace `seedemu-b62-ovn-gwskip-ns1-1078`, and documentation of the observed boundary.
- Changes: no source changes in this step; recorded results after the run completed.
- Commands: ran `runOvnScaleProfile.py ... --deploy-controller-apply-mode node-stream --deploy-node-stream-max-active-per-node 1 --deploy-skip-kube-ovn-gateway-check`; sampled `summary.json`, `ovn_metrics_samples.jsonl`, `ovn_metrics_summary.json`, `wait-ready.log`, `clean.log`, process state, namespace state, and matching Kube-OVN Subnet state.
- Validation: run status `PASS`; deploy passed in 5894.99s, wait-ready passed in 32.48s, clean-after-profile passed in 295.71s; `wait-ready.log` confirmed 1078/1078 pods Running and Ready; metrics showed zero `add_nic_to_ovs_failed`, zero `failed_create_pod_sandbox`, zero `ovs_interface_not_ready`, and zero `ovs_vsctl_timeout`; post-clean checks found no matching namespace/subnet residue.
- Notes: compared with the old stable 1078 w5 baseline deploy time of 9993.82s, this is about a 41% deploy-time reduction while preserving stability. `maxActivePerNode=2` remains unsafe on the current w5 pure OVN cluster.

## 2026-06-17 13:22 - 1078 OVN w11 Worker Scaling Pass

- User intent: evaluate concrete ways to reduce pure OVN+OVS CNI creation pressure, including whether adding workers helps.
- Scope: runtime experiment `/home/lxl/k8s/largeScale/b62_k8s_scale/runs/ovn_profile_20260617_120845_1078_w11`, namespace `seedemu-b62-ovn-gwskip-ns1-w11-1078`, and documentation in `Error_ovn.md`.
- Changes: no source changes during the run; appended the w11 result and CNI pressure mitigation conclusion to `Error_ovn.md`.
- Commands: first removed stale libvirt networks `seedemu-b62-net-w5`/`seedemu-b62-net-w11` after a bridge collision, then ran `runOvnScaleProfile.py` with `--worker-count 11`, `--rebuild-first`, `--deploy-controller-apply-mode node-stream`, `--deploy-node-stream-max-active-per-node 1`, `--deploy-skip-kube-ovn-gateway-check`, `--cni-ovs-vsctl-concurrency 1`, `--cni-ovs-vsctl-timeout-seconds 180`, `--interface-ready-timeout-seconds 0`, `--vpc-shards 4`, and `--clean-after`; monitored Pod phase distribution, Kube events, loadAverage.log, summary.json, and OVN metrics.
- Validation: run status `PASS`; build-cluster passed in 880.05s, compile in 11.03s, build in 76.73s, deploy in 2995.74s, wait-ready in 1.25s, and clean-after-profile in 367.56s. `wait-ready` confirmed 1078/1078 Pods Running and Ready; all 1065 Subnets were ready; event counters were zero for `add_nic_to_ovs_failed`, `failed_create_pod_sandbox`, `ovs_interface_not_ready`, and `ovs_vsctl_timeout`.
- Notes: w11 with per-node single active CNI ADD reduced deploy time from the w5 stable `5894.99s` to `2995.74s`. This supports the current mitigation priority: add workers to spread workload, but keep each worker's OVS/CNI mutation path serialized.

## 2026-06-17 15:45 - 1078 OVN CNI Pressure Mitigation Sweep

- User intent: continue testing concrete CNI pressure mitigation options at 1078 scale and determine which options are actually useful, not just tune one parameter once.
- Scope: runtime experiments `/home/lxl/k8s/largeScale/b62_k8s_scale/runs/ovn_profile_20260617_132748_1078_w11`, `/home/lxl/k8s/largeScale/b62_k8s_scale/runs/ovn_profile_20260617_134406_1078_w11`, `/home/lxl/k8s/largeScale/b62_k8s_scale/runs/ovn_profile_20260617_144310_1078_w11`, and documentation in `Error_ovn.md`.
- Changes: appended the deploy optimization sweep conclusion to `Error_ovn.md`; no deploy logic or cluster configuration was changed in this step.
- Commands: ran a w11 `maxActivePerNode=2` profile with gateway skip and subnet batch 250, interrupted after repeated CNI/OVS failure evidence, and cleaned the namespace; ran a w11 `maxActivePerNode=1` profile without gateway skip and cleaned after PASS; ran a w11 `maxActivePerNode=1` profile with gateway skip and subnet batch 50 and cleaned after PASS. Also checked process and namespace state after cleanup.
- Validation: `maxActivePerNode=2` was unsafe even on w11, with `add_nic_to_ovs_failed=233`, `failed_create_pod_sandbox=241`, and CNI logs showing 322 `ovs-vsctl` timeout entries before interruption. The no-gateway-skip run passed with deploy 3027.68s and no add-nic failures. The subnet batch 50 run passed with deploy 3118.5s and no add-nic failures. After the sweep, no matching `seedemu-b62-ovn*` namespace or active profile/deploy/clean process remained.
- Notes: the best current 1078 setting remains w11 plus `nodeStreamMaxActivePerNode=1`, `cniOvsVsctlConcurrency=1`, `cniOvsVsctlTimeoutSeconds=180`, gateway skip enabled, and subnet batch 250. Increasing workers helped; increasing per-node CNI concurrency did not. Smaller subnet batches were stable but slower.

## 2026-06-17 23:52 - 1078 OVN Worker Count 24/32/48 Sweep

- User intent: continue the worker-count curve after the stream disconnect, try `worker_num=24/32/48`, allow master resource changes if needed, and judge whether more workers improve pure OVN+OVS deploy time.
- Scope: live KVM/K3s/Kube-OVN experiments under `/home/lxl/k8s/largeScale/b62_k8s_scale`, `assignment.yaml`, `assignment_ovn_tuned.yaml`, `renderAssignmentConfig.py`, `prepareLibvirtDhcp.py`, `seedemu/k8sTools/resources/setup/applyK3sCluster.py`, `seedemu/k8sTools/resources/setup/ansible/k3s-install.yml`, and `Error_ovn.md`.
- Changes: changed K3s Pod CIDR from `10.42.0.0/16` to `10.48.0.0/12`; added render-time validation that cluster CIDR can allocate one `/20` PodCIDR per VM and that `maxPods` fits the per-node CIDR; added stale sibling libvirt network cleanup when worker-count changes leave `virbrb62wXX` conflicts; preloaded the Multus bootstrap image into K3s agent image directories; fixed the worker K3s agent Ansible `creates` guard so image preloading does not skip agent installation; appended the worker-count conclusion to `Error_ovn.md`.
- Commands: ran `python3 -m py_compile` for the changed Python files; parsed the Ansible playbook with PyYAML; ran three real profiles with `runOvnScaleProfile.py --assignment assignment_ovn_tuned.yaml --scales 1078 --worker-count 24/32/48 --rebuild-first --disable-extra-ovn-features --ovn-chart-version v1.16.2 --ovn-image-tag v1.16.2-seed-ifskip --cni-ovs-vsctl-concurrency 1 --cni-ovs-vsctl-timeout-seconds 180 --interface-ready-timeout-seconds 0 --vpc-shards 4 --exclude-control-plane --deploy-skip-kube-ovn-gateway-check --deploy-controller-apply-mode node-stream --deploy-node-stream-max-active-per-node 1 --deploy-subnet-batch-size 250 --deploy-kube-ovn-subnet-timeout-seconds 36000 --clean-after`; monitored `summary.json`, Pod phases, events, `deploy.log`, `clean.log`, and OVN metrics.
- Validation: w24 passed with build-cluster 1729.74s, deploy 3043.72s, wait-ready 32.28s, clean-after 378.23s; w32 passed with build-cluster 2204.34s, deploy 3005.81s, wait-ready 32.30s, clean-after 334.80s; w48 passed with build-cluster 3315.12s, deploy 3051.77s, wait-ready 32.44s, clean-after 378.02s. After w48 clean, namespace `seedemu-b62-ovn-w48-curve-1078` and matching Kube-OVN IP/Subnet/VPC resources were gone.
- Notes: worker-count increases beyond w11 did not materially reduce deploy time because placement created stable hotspots: `worker1=86`, `worker10=64`, `worker11=64`, `worker12=50`, `worker13=42`. With `nodeStreamMaxActivePerNode=1`, the deploy tail is governed by the largest per-node queue. w32 and w48 had a few transient Multus apiserver query deadline events, but no `add_nic_to_ovs_failed`, no `ovs_interface_not_ready`, and no `ovs-vsctl` timeout events; all pods became Running and Ready before cleanup.
