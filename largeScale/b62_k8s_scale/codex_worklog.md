## 2026-06-18 09:05 - Macvlan VM Sweep Verification Gates

- User intent: switch the B62 4954 workflow to macvlan mode, run total-VM counts 6/9/12/24/32, include the master in workload placement, and verify 10 fixed AS brdnodes after start-bird only after VM load1 is below 50.
- Scope: `assignment.yaml`, `runFullExperiment.py`, new verification scripts, node schedulability helper, macvlan sweep runner, `test_kernel.sh`, and README documentation.
- Changes: added load-gated `verifyStartBird.*` and `verifyFibRoutes.*`; added `ensureAllNodesSchedulable.sh`; added `runMacvlanVmSweep.py`; made full flow record verification summaries and fail on verification errors; pinned macvlan deploy and placement settings in `assignment.yaml`.
- Commands: `bash -n` on changed shell scripts; `python3 -m py_compile` on changed Python scripts; `python3 runMacvlanVmSweep.py --print-plan --vm-counts 6 9 12 24 32` to confirm generated worker counts and parameters.
- Validation: syntax checks passed; dry-run plan mapped total VM counts to workerCount 5/8/11/23/31 and kept `macvlan + kube-ovn IPAM` with `excludeControlPlane: false`.
- Notes: full experiments have not been started in this entry; later entries should record sweep pass/fail, stage timings, and verification results.

## 2026-06-18 10:36 - 修复 macvlan 4954 build 阶段

- User intent: 修复当前 4954/macvlan run 的 build 失败，并给出一键全流程和分阶段执行命令。
- Scope: `manageK8sManifest.py`、`KubernetesCompiler` base image staging、BIRD `kernel.conf` 编译输出、B62 fast archive。
- Changes:
  - 修复 macvlan + kube-ovn IPAM 渲染 `k8s.kube-ovn.yaml` 时误要求 VPC shard 的问题；macvlan Subnet 不再挂 VPC，pure OVN shard 行为保留。
  - 在 Kubernetes 编译器中递归 staging `handsonsecurity/seedemu-base:2.0` 和 `handsonsecurity/seedemu-router:2.0` 到 `output/base_images/<md5>/`，避免 hash 基础镜像被当作 Docker Hub 镜像 pull。
  - 在 BIRD routing render 阶段恢复 `/etc/bird/conf/kernel.conf` 写入，确保所有 BIRD router/RS 镜像包含 kernel protocol 配置文件。
  - 增强 `fastSeedemuImageArchive.py`，优先使用编译产物里的本地 base image context 递归构建，缺少 compiler hash context 时给出明确错误。
- Commands:
  - `python3 -m py_compile ...`: 校验修改过的 Python 文件语法，结果通过。
  - `PYTHONPATH=/home/lxl/k8s python3 -m unittest tests.routing_scale_profiles_test`: 现有测试有 2 个旧期望失败，分别是 `bird -d` 和 OSPF hello/dead 参数；kernel.conf 相关读取已通过。
  - `./compile.sh <run_dir>`: 重新生成当前 4954 run 的编译产物，结果通过，核心编译耗时约 222.9s。
  - 产物检查脚本: 确认 4954 个 workload 全部有 kernel.conf 文件与 Dockerfile COPY，`base_images` 包含 `072d...` 与 `ad739...` 两个 context。
  - `./build.sh <run_dir>`: 验证 build 阶段通过；fast archive 为 6 个节点生成并导入 per-node image archive。
- Validation:
  - `k8s.kube-ovn.yaml` 统计为 4763 个 Subnet、4763 个 NAD、4954 个 Deployment；macvlan NAD 使用 kube-ovn IPAM，Subnet 不含 VPC 字段。
  - build 阶段最终输出 `Build completed`，所有 6 个节点镜像归档导入成功。
- Notes:
  - 为当前已存在 master VM 手动导入过本地 `072d7840db6a44d06e5c5fd4d7ea8a7d` 镜像以加速验证；后续新 compile 会自动带 base image context，不依赖这个手动导入。
  - 当前 `summary.json` 是早先全流程失败状态；本次是手动重跑 compile/build 验证，没有回写旧 summary。

## 2026-06-18 11:06 - 纯 macvlan Manifest 选择

- User intent: 默认仍使用 OVN+OVS；但 compile 阶段指定 `macvlan` 时必须走纯 macvlan，不再通过 Kube-OVN/OVS 渲染，macvlan 产物保持 `k8s.yaml`，OVN+OVS 产物仍使用 `k8s.kube-ovn.yaml`。
- Scope: `KubernetesCompiler`、`real_topology_k3s_compile.py`、b62/b63 manifest 选择脚本、`k8sTools` running/clean 入口。
- Changes:
  - `KubernetesCompiler` 默认 CNI 改为 `kube-ovn`，并输出 `networking.yaml` 描述 `cniType`、`networkBackend` 和运行 manifest 名称。
  - 移除 `BY_AS_HARD + macvlan/ipvlan` 自动把 Local/CrossConnect 网络降级为 bridge 的逻辑；只有显式 `localLinkCniType` 才覆盖。
  - `real_topology_k3s_compile.py` 接收并传递 `SEED_LOCAL_LINK_CNI_TYPE`，默认 `SEED_CNI_TYPE` 改为 `kube-ovn`。
  - b62/b63 运行脚本优先读取 `output/networking.yaml`；`macvlan` 时选择 `output/k8s.yaml` 并清理旧的 `output/k8s.kube-ovn.yaml`。
  - `k8sTools up/down` 的 manifest 解析读取编译元信息，避免纯 macvlan 产物因集群 fabric 是 OVN 而被自动转换。
- Commands:
  - `python3 -m py_compile ...`: 校验修改过的 Python 文件语法，结果通过。
  - `bash -n ...`: 校验 b62/b63 shell 脚本语法，结果通过。
  - `rg ...`: 检查 `bridge` 默认值、manifest 选择和 `networking.yaml` 引用，确认主路径已改。
- Validation: 只做静态检查，按用户要求未运行 compile/build/deploy，也未修改 VM、K3s 或 namespace。
- Notes: 一些旧 demo 脚本仍保留自身的 `bridge` demo 默认值；当前 b62/b63 主路径和 `KubernetesCompiler` 默认已切到 OVN+OVS。

## 2026-06-18 11:17 - 跳过 macvlan Subnet Cooldown

- User intent: 纯 macvlan deploy 时没有 Kube-OVN Subnet，不应因为 `postSubnetCooldownSeconds: 900` 等待 900 秒。
- Scope: `deploy.sh` 的 Subnet apply 后 cooldown 判断。
- Changes:
  - `post_subnet_cooldown` 增加 backend 判断；`seed_network_backend` 解析为 `macvlan`/非 OVN 时直接跳过 cooldown。
  - `post_subnet_cooldown` 增加 Subnet 数量判断；`EXPECTED_KUBE_OVN_SUBNETS=0` 时直接跳过 cooldown。
  - resume 和普通 deploy 路径都把 `EXPECTED_KUBE_OVN_SUBNETS` 传给 cooldown 函数。
- Commands:
  - `bash -n /home/lxl/k8s/largeScale/b62_k8s_scale/deploy.sh`: shell 语法检查通过。
  - `sed ... deploy.sh`: 人工确认 cooldown 函数和调用点。
- Validation: 只做静态检查，未执行 deploy，也未创建或删除 Kubernetes 资源。
- Notes: 纯 OVN+OVS 且确实存在 Subnet 的场景仍会按 `DEPLOY_POST_SUBNET_COOLDOWN_SECONDS` 等待。

## 2026-06-18 13:43 - FIB Verify 改为固定 10 AS 抽样

- User intent: `verify_after_fib_write` 不再检查每个 AS，而是抽取 10 个不同 AS，每个 AS 固定选一个 brdnode。
- Scope: `verifyFibRoutes.py`、`verifyFibRoutes.sh`、`README.md`。
- Changes:
  - `verifyFibRoutes.py` 的目标选择改为：Running brd Pod 按 AS 分组，每个 AS 选 `brd-r<N>` 中 `N` 最小者，再按 ASN 升序取前 10 个 AS。
  - 新增 `--sample-as-count` 参数，默认 `10`，并在 summary 中记录 `sample_as_count` 和新的 strategy。
  - `verifyFibRoutes.sh` 增加 `VERIFY_FIB_SAMPLE_AS_COUNT=10` 并传给 Python 脚本。
  - README 中把 `verify-after-fib-write` 描述改为 10-AS FIB route-count 验证。
- Commands:
  - `python3 -m py_compile /home/lxl/k8s/largeScale/b62_k8s_scale/verifyFibRoutes.py`: Python 语法检查通过。
  - `bash -n /home/lxl/k8s/largeScale/b62_k8s_scale/verifyFibRoutes.sh /home/lxl/k8s/largeScale/b62_k8s_scale/test_kernel.sh`: shell 语法检查通过。
  - `rg ...`: 确认旧的 every-AS 描述已从 verify FIB 主路径移除。
- Validation: 只做静态检查，未执行 verify、未访问或修改 Kubernetes 资源。
- Notes: 当前选择规则是稳定的 ASN 升序前 10 个 AS；不是随机抽样。

## 2026-06-18 14:30 - Verify Load Gate 提到 80

- User intent: 将 verify 阶段的 load average 阈值从 50 提到 80，写入代码。
- Scope: `verifyStartBird.sh`、`verifyFibRoutes.sh`、`README.md`。
- Changes:
  - `VERIFY_START_BIRD_LOAD_THRESHOLD=80`。
  - `VERIFY_FIB_LOAD_THRESHOLD=80`。
  - README 当前流程说明更新为 `load1 < 80`。
- Commands:
  - `bash -n /home/lxl/k8s/largeScale/b62_k8s_scale/verifyStartBird.sh /home/lxl/k8s/largeScale/b62_k8s_scale/verifyFibRoutes.sh`: shell 语法检查通过。
  - `rg ...`: 确认两个 verify 脚本默认阈值已更新。
- Validation: 未运行 verify，未访问或修改 Kubernetes 资源。
- Notes: 已经启动的 verify 进程不会读取新阈值；需要重启 verify 脚本才会生效。
## 2026-06-18 15:08 - Switch B62 Baseline Back To Pure OVN

- User intent: return from macvlan testing to pure OVN+OVS for incremental deploy profiling starting at 1078 routers, and make deploy tuning safer for Kube-OVN CNI ADD.
- Scope: `assignment.yaml`, `assignment_ovn_tuned.yaml`, `deploy.sh`, `README.md`, generated `configKvmOvn.yaml` and resource plan.
- Changes:
  - Set default experiment to `b62-pure-ovn-1078` / namespace `seedemu-b62-ovn-1078`.
  - Set `networking.cniType`, `localLinkCniType`, and `attachedCniType` to `kube-ovn`.
  - Tuned OVN defaults to larger control-plane resources, `cniOvsVsctlConcurrency=1`, `cniOvsVsctlTimeoutSeconds=180`, `interfaceReadyTimeoutSeconds=0`, `vpcShards=4`.
  - Changed deploy baseline to `controllerApplyMode=node-stream`, `nodeStreamMaxActivePerNode=1`, `subnetBatchSize=250`, `postSubnetCooldownSeconds=60`, and `skipKubeOvnGatewayCheck=true`.
  - Updated README current-mode text from macvlan to OVN+OVS.
- Commands:
  - `python3 - <<'PY' ... yaml.safe_load(...)`: verified both assignment files parse and expose kube-ovn networking.
  - `bash -n deploy.sh compile.sh build.sh runExperiment.sh`: shell syntax passed.
  - `python3 -m py_compile renderAssignmentConfig.py runOvnScaleProfile.py runFullExperiment.py`: Python syntax passed.
  - `python3 renderAssignmentConfig.py --assignment assignment.yaml --run-dir runs/manual_ovn_1078_config --timestamp manual_ovn_1078 prepare`: regenerated local KVM/resource config from the updated assignment.
  - `kubectl ... get nodes/pods/ds/deploy`: read-only live-cluster check; current live Kube-OVN is still image `v1.15.12` with CNI `ovs-vsctl` minimum timeout `120`, `ovs-vsctl-concurrency=5`, and `OVN_NORTHD_N_THREADS=4`.
- Validation: static checks passed; no workload deploy or kube-system restart was performed.
- Notes: assignment changes affect the next render/build/deploy flow. The already-running cluster will not automatically pick up `v1.16.2-seed-ifskip`, `timeout=180`, or CNI concurrency `1` unless Kube-OVN is rebuilt or patched.

## 2026-06-18 19:35 - Debug BIRD Protocol Failures After start-bird

- User intent: diagnose why `verify-after-start-bird` failed all 10 sampled BRD pods with `protocol_check_failed` in the live `runs/20260618_151608_1078_w5` Kube-OVN cluster.
- Findings:
  - `start_bird_summary.json` passed; BIRD processes started and `birdc` was reachable, so the failure was not a BIRD launch failure.
  - The 10 failures were real routing-protocol failures: sampled BGP peers were `Active Socket: No route to host`, OSPF was `Alone`, and route counts stayed near directly-connected routes.
  - Pod network annotations, IP CRs, OVS host interfaces, static IPs, and interface names were correct for `as1269`, `as1271`, and same-node AS1269 peers.
  - ARP failed on attached networks: `1.2.4.245 -> 1.2.4.247`, `1.2.4.245 -> 1.2.0.1`, and same-node `5.245.45.254 -> 5.245.45.253` all stayed unresolved.
  - OVN SB Port_Binding for attached ports showed `options={activation-strategy=rarp,...}` and `up=false`; primary `eth0` still worked. The `activation_strategy=rarp` annotations came from `deploy.skipKubeOvnGatewayCheck=true` and were injected 2812 times during deploy.
- Changes:
  - Set `deploy.skipKubeOvnGatewayCheck: false` in `assignment.yaml` and `assignment_ovn_tuned.yaml`.
  - Updated README to say `skipKubeOvnGatewayCheck` must remain disabled for BIRD/FIB validation runs.
  - Added a warning comment in `deploy.sh` near the `activation_strategy` patch path.
- Commands:
  - `kubectl ... exec ... birdc show protocols`, `birdc show route count`, `ip -br addr`, `ip route`, `ip neigh`, and `ping`: verified BIRD was running but attached-network ARP/connectivity failed.
  - `kubectl ... get ip/subnet/pod -o json`: verified static IP allocation and Subnet readiness.
  - `ovn-sbctl list Port_Binding ...`: confirmed attached ports had `activation-strategy=rarp` and `up=false`.
  - `ovs-vsctl find Interface external_ids:iface-id=...`: confirmed OVS ports existed with matching `iface-id` and valid `ofport`.
  - `python3 -c 'import yaml, pathlib; ...'`: assignment YAML parse check passed.
  - `bash -n deploy.sh`: shell syntax check passed.
- Validation: performed read-only live-cluster diagnostics plus static checks for edited files. Did not mutate the live namespace or rerun deploy; the current running cluster still has the old `activation_strategy=rarp` annotations and should be redeployed or recreated with the updated assignment for protocol verification.
- Notes: `skipKubeOvnGatewayCheck=true` may still be useful only for deploy/Pod-ready pressure experiments where BIRD, OSPF, BGP, and FIB correctness are not checked.

## 2026-06-21 12:33 - Multi-VM macvlan+VLAN Report

- User intent: 写一份报告记录 4954 规模 9/12/18/24/32 VM 的全流程实验结果，并补充 OVN+OVS CNI ADD 压力、macvlan+VLAN 对比、纯 macvlan 二层不隔离导致 ARP/BGP 异常的分析。
- Scope: `multiVmMacvlanVlanReport.md`, `codex_worklog.md`.
- Changes: 新增多 VM macvlan+VLAN 实验报告，记录各阶段耗时、reconvergence 结果、OVN+OVS 与 macvlan+VLAN 压力差异、纯 macvlan 的 ARP 污染/二层不隔离风险。
- Commands: `sed` 读取 `Error_ovn.md`、`bird.md`、`problem.md`、`codex_worklog.md` 中已有故障记录；`python3` 读取 9/12/18/24/32 run 的 `summary.json` 和 `reconvergence_summary.json`；`date` 获取报告时间。
- Validation: 仅做文档写入和本地文件读取；未部署、删除、重启 VM 或修改 Kubernetes 集群。
- Notes: 报告将 pure OVN+OVS、纯 macvlan、macvlan+VLAN 三种模式分开讨论，避免把纯 macvlan 误描述为严格二层隔离方案。

## 2026-06-21 14:03 - Strengthen ARP And OVN CNI Explanation

- User intent: 进一步增强报告中纯 macvlan 下 ARP 识别错误的论文级证据链，并用网络初学者也能理解的方式解释 OVN+OVS 下 CNI ADD 为什么压力大、为什么 macvlan+VLAN/VXLAN parent 预创建路径压力小。
- Scope: `multiVmMacvlanVlanReport.md`, `codex_worklog.md`.
- Changes:
  - 扩展第 7 节，补充 CNI ADD 定义、Kubernetes/Kube-OVN/OVN/OVS/kubelet 涉及对象、Pod secondary interface 创建流程、规模放大后的压力来源，以及 1078/4954 pure OVN+OVS 失败证据。
  - 扩展第 8 节，说明当前实验实际是 macvlan+VLAN；若后续写作使用 macvlan+VXLAN，低压力结论成立的前提是 VLAN/VXLAN parent 已在宿主机侧准备好，CNI ADD 不再走 OVN/OVS add-port。
  - 重写第 9 节，按二层边界要求、BGP 对 ARP 的依赖、已有异常证据表、逻辑推导、论文表述建议和风险总结组织纯 macvlan ARP/邻居发现污染问题。
- Commands:
  - `sed -n ... multiVmMacvlanVlanReport.md`: 检查修改后的第 7/8/9 节。
  - `wc -l ...`: 确认文档行数和工作记录行数。
  - `date '+%Y-%m-%d %H:%M %Z'`: 记录工作时间。
- Validation: 仅做文档修改和只读检查；未运行实验、未修改脚本、未访问或变更 Kubernetes/VM 状态。

## 2026-06-22 16:38 - Bridge VLAN And Ipvlan VLAN Feasibility Check

- User intent: 分析 `bridge+VLAN` 和 `ipvlan+VLAN` 是否符合 SeedEMU/K8s 大规模实验的二层隔离设计，并判断是否可以直接运行 4954 全流程。
- Scope: 只读检查 `seedemu/compiler/kubernetes.py`、`seedemu/k8sTools/resources/running/manageK8sManifest.py`、`seedemu/k8sTools/resources/running/manageRunningStage.py`、B62 `assignment.yaml`/`configK3s.yaml`、当前 K3s 节点 CNI 插件目录。
- Changes: 无代码修改；未启动或删除实验资源。
- Commands:
  - `rg ... kubernetes.py manageK8sManifest.py manageRunningStage.py ...`: 确认当前 VLAN 分配逻辑只挂在 macvlan 路径，`bridge` 和 `ipvlan` 未使用 VLAN parent 分配。
  - `sed -n ... kubernetes.py`: 检查 `macvlan`、`ipvlan`、`bridge` 三个 NAD 生成分支。
  - `kubectl get nodes -o wide`: 确认当前 32 VM K3s 集群可访问。
  - `ssh ... ls -l /opt/cni/bin /var/lib/rancher/k3s/data/cni`: 确认 Multus 可见目录有 `macvlan`/`ipvlan`/`static`，没有 `bridge`；K3s 自带目录有 `bridge`。
  - `kubectl -n kube-system get cm multus-cni-config -o yaml` 和节点 `00-multus.conf`: 确认 Multus secondary plugin 仍按默认 `/opt/cni/bin` 查找。
  - `ip link help ipvlan/macvlan`、`man ip-link`: 只读查看 Linux ipvlan/macvlan/bridge 基本模式说明。
- Validation:
  - `bridge+VLAN` 当前不能直接跑 4954：compiler 的 bridge 分支是 node-local bridge，没有 VLAN parent/uplink 准备；当前 Multus 默认插件目录也没有 `bridge` CNI plugin。
  - `ipvlan+VLAN` 当前不能直接跑 4954：节点有 `ipvlan` plugin，但 compiler 没有给 ipvlan 分配 `ensX.<vlan>` parent，也不会生成 VLAN annotation 供 deploy 预创建 parent。
- Notes:
  - `bridge+VLAN` 概念上可以表达 per-link L2 隔离，但需要每个网络在节点上创建 `VLAN parent + Linux bridge + enslave parent`，工程成本和设备数量高于 macvlan+VLAN。
  - `ipvlan+VLAN` 概念上可能降低 MAC/FDB 压力，但与 macvlan 的每接口独立 MAC 语义不同，需要先做小规模 ARP/OSPF/BGP 验证后再考虑 4954。

## 2026-06-22 13:43 - Node-Scoped macvlan VLAN Parent Preparation

- User intent: 完善 macvlan+VLAN deploy 优化，避免每个 VM 都创建全拓扑所有 VLAN parent，改为按该节点实际运行的 Pod 所需 network 创建。
- Scope: `seedemu/k8sTools/resources/running/manageK8sManifest.py`, `seedemu/k8sTools/resources/running/manageRunningStage.py`, `largeScale/b62_k8s_scale/deploy.sh`, `largeScale/b62_k8s_scale/README.md`, `largeScale/b62_k8s_scale/multiVmMacvlanVlanReport.md`.
- Changes:
  - 新增 `macvlan-vlan-interfaces-by-node` helper，根据 manifest 中 workload 的 `kubernetes.io/hostname` placement 和 Multus networks annotation 生成 `node -> VLAN parent` 映射。
  - B62 `deploy.sh` 改为使用 node-scoped VLAN rows，只在对应节点上创建 `ensX.<vlan>`；没有固定 placement 的 workload 会在 helper 中安全扩展到所有节点。
  - 通用 k8sTools `manageRunningStage.py` 同步使用 node-scoped VLAN parent preparation。
  - 移除 B62 deploy 和通用 running stage 中不再使用的全量 VLAN wrapper；底层 `macvlan-vlan-interfaces` helper 子命令保留用于只读对比和调试。
  - README 和多 VM 报告更新，说明历史实验是全节点全量准备，当前代码已改为按节点准备。
- Commands:
  - `python3 -m py_compile ...manageK8sManifest.py ...manageRunningStage.py`: Python 语法检查通过。
  - `bash -n largeScale/b62_k8s_scale/deploy.sh`: shell 语法检查通过。
  - `python3 ...manageK8sManifest.py --help`: helper argparse 入口检查通过。
  - `python3 manageK8sManifest.py macvlan-vlan-interfaces...` 与 `macvlan-vlan-interfaces-by-node...`: 使用既有 32VM/4954 manifest 做只读验证，全量 VLAN parent 为 `4763`，旧全节点重复量为 `152416`，按节点裁剪后为 `6902`。
- Validation: 只做静态检查和既有 manifest 的只读解析；未部署 workload、未 SSH 到 VM、未创建或删除 Kubernetes/VM 资源。
- Notes: 该优化依赖 workload manifest 中的固定 hostname placement。若某个 workload 未固定节点，helper 会把它用到的 VLAN parent 创建到所有节点，优先保证 CNI ADD 安全。

## 2026-06-22 20:20 - ipvlan+VLAN And bridge+VLAN Implementation And Validation

- User intent: 在现有 KVM/K3s 场景下实现 `ipvlan+VLAN`，小规模通过后再跑 4954；随后继续支持 `bridge+VLAN`，三种模式都能通过 compile 阶段的 `cniType` 配置选择，并确保二层网络隔离。
- Scope:
  - `seedemu/compiler/kubernetes.py`
  - `seedemu/k8sTools/resources/running/manageK8sManifest.py`
  - `seedemu/k8sTools/resources/running/manageRunningStage.py`
  - `largeScale/b62_k8s_scale/deploy.sh`
  - `largeScale/b62_k8s_scale/lib.sh`
  - `largeScale/b62_k8s_scale/renderAssignmentConfig.py`
  - `largeScale/b62_k8s_scale/runFullExperiment.py`
- Changes:
  - 将原先只服务 `macvlan+VLAN` 的逻辑泛化为 VLAN-backed CNI 路径，支持 `macvlan`、`ipvlan`、`bridge`。
  - `ipvlan+VLAN` 生成 `type=ipvlan, mode=l2, master=ensX.<vlan>, ipam=static` 的 NAD。
  - `bridge+VLAN` 生成 `type=bridge, bridge=br-<hash>, isGateway=false, ipam=static` 的 NAD，并在部署前按节点创建 `VLAN parent + Linux bridge + enslave parent`。
  - `manageK8sManifest.py` 的 node-scoped VLAN helper 增加 bridge 字段；空 bridge 使用 `-` 占位，避免 shell `read` 折叠空 TSV 字段导致 ipvlan/macvlan 字段错位。
  - `deploy.sh` 和通用 running stage 改为统一准备 VLAN-backed CNI parent；bridge 模式下自动检查并链接 K3s 自带 `bridge` CNI plugin。
  - B62 默认仍保持 OVN+OVS 语义；只有 assignment/compile 指定 `cniType` 为 `macvlan`、`ipvlan` 或 `bridge` 且启用 VLAN-backed 模式时，才走对应 VLAN 路径。
- Static validation:
  - `python3 -m py_compile /home/lxl/k8s/seedemu/compiler/kubernetes.py /home/lxl/k8s/seedemu/k8sTools/resources/running/manageK8sManifest.py /home/lxl/k8s/seedemu/k8sTools/resources/running/manageRunningStage.py /home/lxl/k8s/largeScale/b62_k8s_scale/renderAssignmentConfig.py /home/lxl/k8s/largeScale/b62_k8s_scale/runFullExperiment.py /home/lxl/k8s/examples/kubernetes/real_topology_k3s_compile.py`
  - `bash -n /home/lxl/k8s/largeScale/b62_k8s_scale/deploy.sh /home/lxl/k8s/largeScale/b62_k8s_scale/compile.sh /home/lxl/k8s/largeScale/b62_k8s_scale/build.sh /home/lxl/k8s/largeScale/b62_k8s_scale/runExperiment.sh`
  - `python3 /home/lxl/k8s/seedemu/k8sTools/resources/running/manageK8sManifest.py --help`
- ipvlan+VLAN 1078 result:
  - Run dir: `/home/lxl/k8s/largeScale/b62_k8s_scale/runs/ipvlan_vlan_smoke_20260622_175446_1078_w31`
  - Compile/deploy/wait-ready/L2 isolation/start-bird all passed.
  - `verifyStartBird.sh` failed: sampled iBGP sessions stayed `Passive/Active` after start-bird.
  - Debug conclusion: `ipvlan l2` receives traffic by destination IP, not by L2 next-hop MAC in the same way as macvlan/bridge. Packets whose IP destination is a remote loopback did not reach the next-hop router child interface, so this mode cannot faithfully emulate SeedEMU router multi-hop forwarding for iBGP loopback reachability.
  - Decision: did not run 4954 ipvlan+VLAN, because the failure is semantic rather than scale pressure.
- bridge+VLAN 1078 result:
  - Run dir: `/home/lxl/k8s/largeScale/b62_k8s_scale/runs/bridge_vlan_smoke_20260622_183559_1078_w31`
  - Compile output: `1078 Deployment`, `1065 NetworkAttachmentDefinition`, `0 Subnet`, no Kube-OVN workload subnet dependency.
  - L2 isolation passed: same VLAN ARP visible, different VLAN ARP not visible.
  - start-bird passed in `75.97s`; verifyStartBird passed after convergence retries; start-bird-kernel passed in `88.31s`; FIB verify passed with `failures=0`.
  - Namespace cleaned after smoke validation.
- bridge+VLAN 4954 result:
  - Run dir: `/home/lxl/k8s/largeScale/b62_k8s_scale/runs/bridge_vlan_full_20260622_190734_4954_w31`
  - Namespace: `seedemu-b62-bridge-vlan-4954-w31`
  - Compile passed in `224.40s`: `4954 Deployment`, `4763 NetworkAttachmentDefinition`, `0 Subnet`, no Kube-OVN workload subnet dependency.
  - Node-scoped VLAN preparation planned `6902` node-local parent/bridge entries across `32` nodes, much lower than all-nodes-all-networks expansion.
  - Build passed with fast image archive import to all nodes.
  - Deploy passed with `batchSize=80`; no OVN Subnet creation and no 900s Kube-OVN cooldown.
  - wait-ready passed: `4954/4954` Pods Ready at about `164s`.
  - L2 isolation passed; summary saved at `runs/bridge_vlan_full_20260622_190734_4954_w31/l2_isolation/l2_isolation_summary.json`.
  - start-bird passed: `targets=4954 expected=4954 started=4542 skipped=412 duration=61.36s`, load stayed low.
  - verifyStartBird passed on 10 fixed AS samples after normal convergence retries.
  - start-bird-kernel passed: `switched=4954 duration=99.81s`, load stayed low.
  - `test_kernel.sh` passed on 10 fixed AS samples; `failures=0`, output saved at `runs/bridge_vlan_full_20260622_190734_4954_w31/test_kernel.json`.
- Notes:
  - The successful 4954 bridge+VLAN namespace was intentionally left running for follow-up inspection.
  - `bridge+VLAN` provides per-network Linux bridge domains backed by VLAN parents, so ARP broadcast is constrained to the matching VLAN network and does not leak across unrelated emulated L2 networks.
  - Compared with pure OVN+OVS, this path avoids per-workload Subnet and OVN logical switch/static-route mutation during CNI ADD, so deploy pressure is much lower while preserving per-network L2 isolation.

## 2026-06-23 00:35 - bridge+VLAN vs macvlan+VLAN 4954 Comparison

- User intent: 进行 bridge+VLAN 和 macvlan+VLAN 的 4954 规模实验对比，并判断 bridge 的优势是否能通过实验体现。
- Scope:
  - `largeScale/b62_k8s_scale/compareVlanCniObservability.py`
  - `largeScale/b62_k8s_scale/deploy.sh`
  - `seedemu/k8sTools/resources/running/manageRunningStage.py`
  - `largeScale/b62_k8s_scale/bridgeMacvlanVlanCompare.md`
- Changes:
  - 新增 `compareVlanCniObservability.py`，用于抽样检查 VLAN-backed CNI 的同网 ARP/连通性、跨网 ARP 隔离，以及 host 侧可观测性。
  - 修复从 bridge+VLAN 切回 macvlan+VLAN 时的 VLAN parent 残留问题：非 bridge 模式下若 `ensX.<vlan>` 仍有 master，则先执行 `ip link set <iface> nomaster`。
  - 写入对比报告 `bridgeMacvlanVlanCompare.md`。
- Commands:
  - `python3 -m py_compile compareVlanCniObservability.py`: 静态检查通过。
  - `python3 compareVlanCniObservability.py runs/bridge_vlan_full_20260622_190734_4954_w31 --sample-network net-ix-ix13 --exec-timeout 20`: bridge+VLAN 隔离与 host bridge port list 检查通过。
  - `./clean.sh runs/bridge_vlan_full_20260622_190734_4954_w31`: 清理 bridge namespace。
  - `./preflight.sh runs/compare_macvlan_vlan_20260622_222821_4954_w31`: 32 节点 Ready。
  - `./compile.sh runs/compare_macvlan_vlan_20260622_222821_4954_w31`: macvlan+VLAN compile 通过，`编译时间: 223.265s`。
  - `./build.sh runs/compare_macvlan_vlan_20260622_222821_4954_w31`: build 通过，使用 fast image archive。
  - 第一次 `./deploy.sh ...`: 失败，CNI 报 `failed to create macvlan: device or resource busy`。
  - `ssh ubuntu@192.168.126.146 'ip -d link show ens3.502 ...'`: 确认 `ens3.502` 残留 master bridge。
  - 修复后 `bash -n deploy.sh` 和 `python3 -m py_compile ...manageRunningStage.py`: 检查通过。
  - `./clean.sh runs/compare_macvlan_vlan_20260622_222821_4954_w31`: 清理失败 namespace。
  - 第二次 `./deploy.sh ...`: 成功，max pressure 为 `pending=5 creating=31 failed=0 max_creating_per_node=2`。
  - `./wait-ready.sh ...`: `4954/4954` Pods Ready，`131s`。
  - `python3 compareVlanCniObservability.py runs/compare_macvlan_vlan_20260622_222821_4954_w31 --sample-network net-ix-ix13 --exec-timeout 20`: macvlan+VLAN 隔离检查通过。
  - `./start_bird.sh ...`: PASS，`duration=105.84s`。
  - `./verifyStartBird.sh ...`: PASS，10 个固定 AS 样本。
  - `./start_bird_kernel.sh ...`: PASS，`duration=136.27s`。
  - `./verifyFibRoutes.sh ...`: PASS，10 个固定 AS 样本。
  - `./runReconvergence.sh ...`: PASS，`total=15.49s`。
- Results:
  - bridge+VLAN 4954 既有 run：compile `224.40s`，build 约 `1072s`，deploy 约 `1870s`，wait-ready `164s`，start-bird `61.36s`，verifyStartBird `92.29s`，start-kernel `99.81s`，verifyFIB `10.19s`。
  - macvlan+VLAN 4954 本轮 run：compile `223.27s`，build 约 `922s`，成功 deploy 约 `2606s`，wait-ready `131s`，start-bird `105.84s`，verifyStartBird `51.74s`，start-kernel `136.27s`，verifyFIB `10.16s`，reconvergence `15.49s`。
  - bridge+VLAN 和 macvlan+VLAN 都通过同网 ARP 可见、跨网 ARP 不可见的二层隔离验证。
- Conclusion:
  - 两种 VLAN-backed CNI 都能避免 OVN+OVS 的 Subnet/logical switch/static route CNI ADD 长尾。
  - 本轮单次数据中 bridge+VLAN deploy 和 BIRD 阶段更快，但 macvlan+VLAN 成功重跑前经历失败与清理，host load 起点更高，性能结论需要干净环境复测。
  - bridge+VLAN 的实验优势非常明确：host 侧有 per-network Linux bridge，可直接列出 VLAN parent 和 veth 端口成员；macvlan+VLAN 没有 node-local bridge port list，调试和故障注入不如 bridge 直观。

## 2026-06-23 11:54 - Clean Rebuild bridge+VLAN vs macvlan+VLAN 4954 Comparison

- User intent: 直接重新构造 VM 与 cluster，在同一套 32 VM/4954 规模条件下比较 `bridge+VLAN` 和 `macvlan+VLAN` 的全流程耗时、deploy 压力、负载、验证结果和 bridge 优势。
- Scope:
  - `largeScale/b62_k8s_scale/bridgeMacvlanVlanCompare.md`
  - run artifacts under `largeScale/b62_k8s_scale/runs/rebuild_compare_bridge_macvlan_vlan_20260623_092436_4954_w31/`
  - generated comparison assignments under `runs/rebuild_compare_bridge_macvlan_vlan_20260623_092436_4954_w31/assignments/`
- Changes:
  - 生成并使用 bridge+VLAN 与 macvlan+VLAN 两份 4954/w31 assignment，参数保持一致，只改变 CNI 类型。
  - 先运行 bridge+VLAN 全流程，再重新 destroy/build VM 与 K3s cluster 后运行 macvlan+VLAN 全流程。
  - 运行 `compareVlanCniObservability.py`，记录同网 ARP/连通性、跨网 ARP 隔离和 host 侧可观测性证据。
  - 重写 `bridgeMacvlanVlanCompare.md`，只保留本次干净重建对比的阶段耗时、压力、load 和结论，避免混入旧的失败重跑数据。
- Commands:
  - `python3 runFullExperiment.py --assignment runs/rebuild_compare_bridge_macvlan_vlan_20260623_092436_4954_w31/assignments/assignment_bridge_vlan_4954_w31.yaml --run-dir runs/rebuild_compare_bridge_macvlan_vlan_20260623_092436_4954_w31/bridge_vlan_4954_w31`: bridge+VLAN 全流程 PASS。
  - `python3 compareVlanCniObservability.py runs/rebuild_compare_bridge_macvlan_vlan_20260623_092436_4954_w31/bridge_vlan_4954_w31 --sample-network net-ix-ix13 --exec-timeout 20`: bridge+VLAN 二层隔离与 host bridge port list 检查 PASS。
  - `python3 runFullExperiment.py --assignment runs/rebuild_compare_bridge_macvlan_vlan_20260623_092436_4954_w31/assignments/assignment_macvlan_vlan_4954_w31.yaml --run-dir runs/rebuild_compare_bridge_macvlan_vlan_20260623_092436_4954_w31/macvlan_vlan_4954_w31`: macvlan+VLAN 重新 destroy/build VM+cluster 后全流程 PASS。
  - `python3 compareVlanCniObservability.py runs/rebuild_compare_bridge_macvlan_vlan_20260623_092436_4954_w31/macvlan_vlan_4954_w31 --sample-network net-ix-ix13 --exec-timeout 20`: macvlan+VLAN 二层隔离检查 PASS。
  - 多次 `kubectl get pods/get nodes`、`tail summary/deploy/loadAverage logs`：用于过程监测，未修改 cluster 状态。
- Validation:
  - bridge+VLAN: total `4309.22s`, deploy `1504.93s`, start-bird `44.77s`, start-kernel `84.05s`, reconvergence `16.08s`, max deploy creating `34`, failed `0`。
  - macvlan+VLAN: total `4154.21s`, deploy `1358.92s`, start-bird `36.17s`, start-kernel `76.07s`, reconvergence `15.96s`, max deploy creating `25`, failed `0`。
  - 两轮 `verify-after-start-bird`、`verify-after-fib-write`、`reconvergence` 均 PASS；FIB 检查失败样本数为 `0`。
  - 两轮同网 ARP 可见、跨网 ARP 不可见，说明 VLAN 二层隔离成立。
- Notes:
  - 本轮 macvlan+VLAN 在总耗时、deploy、start-bird、start-kernel 和 deploy load 上优于 bridge+VLAN。
  - bridge+VLAN 的优势是可观测性和可控制性：host 侧有 per-network Linux bridge，可列出 VLAN parent 与 Pod veth 端口成员，适合调试、抓包和故障注入。
  - 当前 macvlan+VLAN namespace 保持运行，便于后续手动检查。
  - 本轮是单次顺序实验；若要写入严格性能结论，建议补跑反向顺序 `macvlan+VLAN -> bridge+VLAN` 后取均值或中位数。

## 2026-06-23 22:27 - Three CNI Scheme Conclusion Document

- User intent: 总结 `bridge+VLAN`、`macvlan+VLAN` 和 `OVN+OVS` 三种方案结合当前 SeedEMU/K8s 仿真器时的性能、隔离语义、可观测性和可控制性差异，并突出 `bridge+VLAN` 的 host 侧 per-network Linux bridge 优势。
- Scope:
  - `largeScale/b62_k8s_scale/0623conclusion.md`
  - `largeScale/b62_k8s_scale/codex_worklog.md`
- Changes:
  - 新增 `0623conclusion.md`，整理三种方案的总体对比、4954 VLAN-backed 实验结果、OVN+OVS 1078 profile 结论、仿真器语义匹配关系、二层隔离结论和推荐选择。
  - 明确写入 `bridge+VLAN` 的可观测性和可控制性优势：host 侧可见 per-network Linux bridge，可直接列出 VLAN parent 与 Pod veth 端口成员，便于抓包、定位、故障注入和论文证据展示。
  - 将 `OVN+OVS` 定位为语义最完整但 4954 规模下控制面和 CNI ADD 成本过高的 baseline。
- Commands:
  - `sed -n '1,260p' bridgeMacvlanVlanCompare.md`: 读取 2026-06-23 bridge/macvlan 重建对比数据。
  - `rg ... Error_ovn.md` and `tail -220 Error_ovn.md`: 读取 OVN+OVS profile 和 CNI ADD 长尾结论。
  - `sed -n '1,260p' 0623conclusion.md` and `tail -80 0623conclusion.md`: 检查新文档内容。
- Validation:
  - 仅文档变更，无代码或集群操作；未重新跑实验。
  - 文档中的关键数据来自已有 summary/report：`macvlan+VLAN` 4954 deploy `1358.92s`，`bridge+VLAN` 4954 deploy `1504.93s`，OVN+OVS 1078 稳定配置 deploy 约 `3000s`。
- Notes:
  - `OVN+OVS` 与 VLAN-backed 两类方案不是完全同规模同阶段的直接数值对比；文档已标注 OVN+OVS 4954 当前不适合作为稳定全流程主路径，1078 数据用于说明控制面和 CNI ADD 成本趋势。

## 2026-06-24 01:31 - macvlan+VLAN 9955 Full Workflow Test

- User intent: 在 `macvlan+VLAN` 模式下运行 9955 规模全流程，判断是否能够正常运行。
- Scope:
  - `largeScale/b62_k8s_scale/compile.sh`
  - run artifacts under `largeScale/b62_k8s_scale/runs/macvlan_vlan_9955_20260623_225237_w63/`
  - active namespace `seedemu-b62-macvlan-vlan-9955-w63-225237`
- Changes:
  - 修复 9955 规模 compile 的环境变量长度问题：不再把 `placement_expected.json` 内容放进 `SEED_NODE_LABELS_JSON`，改为传 `SEED_NODE_LABELS_FILE` 文件路径。
  - 在 compile 日志中打印 `SEED_MACVLAN_VLAN_MODE`、VLAN start 和 trunk 配置，便于确认本轮确实走 `macvlan+VLAN`。
- Commands:
  - `python3 runFullExperiment.py --assignment .../assignment_macvlan_vlan_9955_w63.yaml --run-dir .../macvlan_vlan_9955_20260623_225237_w63`: 初次全流程启动，build cluster 成功，compile 因 `/usr/bin/env: Argument list too long` 中断。
  - `bash -n compile.sh`: 修复后静态检查通过。
  - `./compile.sh runs/macvlan_vlan_9955_20260623_225237_w63`: PASS，输出 `k8s.yaml`，编译时间 `477.90s`。
  - `./build.sh runs/macvlan_vlan_9955_20260623_225237_w63`: PASS，fast image archive + per-node preload 成功。
  - `./deploy.sh runs/macvlan_vlan_9955_20260623_225237_w63`: PASS，创建 namespace、9343 个 NAD 和 9955 个 Deployment，无 Kube-OVN Subnet 阶段。
  - `./wait-ready.sh runs/macvlan_vlan_9955_20260623_225237_w63`: PASS，`9955/9955` Pods Running and Ready。
  - `./start_bird.sh runs/macvlan_vlan_9955_20260623_225237_w63`: PASS，`targets=9955 expected=9955 started=9183 skipped=772 duration=41.23s`。
  - `./verifyStartBird.sh runs/macvlan_vlan_9955_20260623_225237_w63`: PASS，10 个不同 AS 固定 brdnode 样本，`duration=101.49s`，失败样本 `0`。
  - `./start_bird_kernel.sh runs/macvlan_vlan_9955_20260623_225237_w63`: PASS，`targets=9955 switched=9955 duration=89.64s`。
  - `./verifyFibRoutes.sh runs/macvlan_vlan_9955_20260623_225237_w63`: PASS，10 个不同 AS 固定 brdnode 样本，`duration=17.63s`，失败样本 `0`。
  - `./runReconvergence.sh runs/macvlan_vlan_9955_20260623_225237_w63`: PASS，故障侧和恢复侧均观察到路由变化，`total=25.55s`。
  - `kubectl get pods/get nodes/get network-attachment-definitions`: 最终检查 64 个节点 Ready，9955 个 Pod Running，9343 个 NAD，无非 Running Pod。
- Validation:
  - Cluster setup: render `0.07s`，destroy-existing-cluster `200.75s`，prepare-libvirt-dhcp `68.43s`，build-cluster `2836.68s`，preflight `44.13s`。
  - Runtime stages: compile `477.90s`，build workload 约 `12-13min`，deploy 约 `66min`，wait-ready `0s`，start-bird `41.23s`，verify-start-bird `101.49s`，start-kernel `89.64s`，verify-FIB `17.63s`，reconvergence `25.55s`。
  - FIB 样本全部满足 `ip_route_count > bird_network_count`，例如 AS1272 `7747 -> 10277`、AS1277 `7600 -> 7616`。
  - Reconvergence: `faultInjectionSeconds=3.55`，`failureConvergenceSeconds=1.03`，`recoveryInjectionSeconds=4.90`，`recoveryConvergenceSeconds=1.55`。
- Notes:
  - 9955 规模下 `macvlan+VLAN` 本轮全流程可正常运行。
  - 本轮使用 64 个 VM 节点，master 也参与调度；3 条 VLAN trunk 覆盖 12282 个 VLAN 容量。
  - `start-bird` 后部分 Pod 被计为 `skipped` 是因为 BIRD 已经在这些 Pod 内运行，最终 `bird_count=expected`，不是失败。
  - `verify_after_start_bird` 期间控制主机 load 曾因并发 `birdc show protocols` 上升，但 VM load gate 通过，最终协议验证通过。

## 2026-06-24 08:40 - Stable B62 Directory Cleanup

- User intent: 将 `largeScale/b62_k8s_scale` 整理为稳定版本，顶层仅保留全流程需要的脚本；保留文档文件在当前目录；新增 `.gitignore` 防止 `runs/` 和 `base_image/` 等运行产物进入 GitHub；在 README 末尾说明每个顶层文件的作用。
- Scope:
  - `largeScale/b62_k8s_scale/.gitignore`
  - `largeScale/b62_k8s_scale/README.md`
  - top-level script cleanup under `largeScale/b62_k8s_scale/`
  - archived files under `largeScale/b62_k8s_scale/runs/cleanup_archive_20260624_083757/`
- Changes:
  - 新增 `.gitignore`，忽略 `runs/`、`base_image/`、`__pycache__/`、生成的 cluster/config/kubeconfig/resourcePlan 文件、本地 lifecycle marker 和临时日志。
  - 将不属于稳定全流程的旧实现和历史工具归档到 `runs/cleanup_archive_20260624_083757/`：`start_bird_helper.py`、`start_bird_kernel_helper.py`、`verify_bird.sh`、`verify_bird_helper.py`、`test.sh`、`test_kernel.sh`、`runMacvlanVmSweep.py`、`runOvnScaleProfile.py`、`collectOvnMetrics.py`、`assignment_1078_multitrunk.yaml`、`assignment_ovn_tuned.yaml`。
  - 按用户要求，所有 Markdown 文档仍保留在当前目录。
  - 更新 `README.md`：修正 start-bird/start-kernel 描述为 node-local `crictl`/`nsenter`；移除已归档 `runMacvlanVmSweep.py` 的顶层命令；追加“Top-Level File Roles”表格，解释当前保留的入口脚本、阶段脚本、Python helper、文档和生成文件。
- Commands:
  - `find . -maxdepth 1 -type f -printf '%f\n' | sort`: 盘点顶层文件。
  - `rg ... README.md *.sh *.py *.md *.yaml`: 检查旧脚本引用和 README 过时命令。
  - `mv ... runs/cleanup_archive_20260624_083757/`: 归档非主流程文件。
  - `git check-ignore -v largeScale/b62_k8s_scale/runs/foo largeScale/b62_k8s_scale/base_image/foo ...`: 验证 `.gitignore` 覆盖运行产物路径。
  - `bash -n build.sh buildCluster.sh clean.sh compile.sh deploy.sh destroyCluster.sh ensureAllNodesSchedulable.sh ensure_master_registry.sh lib.sh preflight.sh runExperiment.sh runReconvergence.sh start_bird.sh start_bird_kernel.sh verifyFibRoutes.sh verifyStartBird.sh wait-ready.sh`: shell 语法检查通过。
  - `python3 -m py_compile k8sTools.py renderAssignmentConfig.py prepareLibvirtDhcp.py seed_k8s_plan_real_topology_by_as.py fastSeedemuImageArchive.py generate_node_image_refs.py runFullExperiment.py start_bird_node_local.py verifyStartBird.py start_bird_kernel_node_local.py verifyFibRoutes.py measureReconvergence.py compareVlanCniObservability.py test.py`: Python 语法检查通过。
  - `rg ... *.sh *.py README.md assignment.yaml`: 稳定入口和 README 不再引用被归档脚本。
- Validation:
  - 顶层稳定全流程入口仍完整：`runExperiment.sh -> runFullExperiment.py -> clean/preflight/compile/build/deploy/wait-ready/start-bird/verify/start-kernel/verify-FIB/reconvergence`。
  - 没有运行真实 KVM/K3s/deploy 实验；本次只做文件整理、README 更新和静态检查。
- Notes:
  - `.gitignore` 对未跟踪的 `runs/` 和 `base_image/` 生效。对历史上已经被 Git 跟踪的生成文件，仍需在提交前用 `git rm --cached <file>` 取消跟踪，工作区文件本身不会被删除。

## 2026-06-24 09:40 - Down 9955 Namespace And Clear Cluster

- User intent: 将当前 9955 规模实验的 namespace down 掉，并清除当前 B62 K3s/KVM cluster。
- Scope:
  - Runtime resources for `/home/lxl/k8s/largeScale/b62_k8s_scale/runs/macvlan_vlan_9955_20260623_225237_w63`
  - Namespace `seedemu-b62-macvlan-vlan-9955-w63-225237`
  - B62 libvirt VMs and networks for `w63`
- Changes:
  - 删除 9955 规模实验 namespace 内的 workload、NAD、events，并等待 namespace 删除完成。
  - 执行 cluster destroy，清理 K3s、64 个 B62 VM、w63 libvirt 网络和生成的 kubeconfig/inventory。
  - 未处理 `bird-rtnl-dev`，因为它不是本轮 B62 cluster 的节点。
- Commands:
  - `kubectl get ns/pods/nodes`: 清理前确认 namespace 存在、9955 个 Pod Running、64 个节点 Ready。
  - `./clean.sh runs/macvlan_vlan_9955_20260623_225237_w63`: 删除 namespace 和相关 Kube-OVN 资源；`clean.log` 显示 namespace fully deleted。
  - `./destroyCluster.sh runs/macvlan_vlan_9955_20260623_225237_w63`: 清理 K3s/KVM cluster；`destroy-cluster.log` 显示 cleanup verification passed。
  - `virsh -c qemu:///system list --all | rg 'seedemu-b62|bird-rtnl-dev'`: 最终只剩 `bird-rtnl-dev` running，无 `seedemu-b62-*` VM。
  - `virsh -c qemu:///system net-list --all | rg 'seedemu-b62|virbrb62'`: 最终无 B62 libvirt network。
- Validation:
  - `clean.sh` 退出码 `0`，删除 9955 deployments、8077 replicasets、9343 NADs、8491 directly listed pods、9996 events，并确认 namespace 删除完成。
  - `destroyCluster.sh` 退出码 `0`，确认 B62 libvirt domains/networks 不再存在，`kubeconfig.yaml` 和 `cluster.inventory.yaml` 被移除。
- Notes:
  - 工作目录中仍存在 `configK3s.yaml` 这个生成配置文件，但当前 cluster 已销毁；该文件已被 `.gitignore` 忽略，不代表仍有活跃集群。
  - 删除大规模 namespace 期间宿主机 load 曾短暂升高，这是大量 Kubernetes 对象删除时的预期压力。

## 2026-06-24 15:11 - Expand Network Mode Selection Notes

- User intent: 将关于 `OVN+OVS`、`macvlan+VLAN`、`bridge+VLAN` 的使用建议写入 `0623conclusion.md`，并结合当前 SeedEMU/K8s 源码和仿真器语义，说明不同实验场景下应该选择哪种模式，尤其是 SDN 相关实验的边界。
- Scope:
  - `largeScale/b62_k8s_scale/0623conclusion.md`
  - Source/design references from `seedemu/core`, `seedemu/layers`, `seedemu/compiler/kubernetes.py`, and `seedemu/k8sTools/resources`
- Changes:
  - 将原来的简短“推荐选择”扩展为“面向仿真器语义的模式选择”。
  - 补充 SeedEMU 网络语义依据：`Local`、`InternetExchange`、`CrossConnect` 都是仿真二层广播域，BIRD/OSPF/BGP 依赖容器内真实接口、ARP 和路由表。
  - 增加按实验目标选择网络模式的表格，区分大规模路由实验、二层隔离证据、抓包、`tc` 故障注入、SDN fabric/OVN 控制面实验、无 underlay VLAN 的跨物理机场景。
  - 补充三种模式下 `tc` 的可用性和操作点差异：Pod 内接口、host veth、Linux bridge、VLAN parent、OVS/OVN port。
  - 明确 SDN 实验边界：当前 `OVN+OVS` 可用于 SDN fabric/Kube-OVN/OVS 控制面实验，但不能自动代表 SeedEMU AS 内部被 SDN controller 控制；后者需要新增 switch/controller/flow table/control channel 的编译和运行时映射。
- Commands:
  - `sed -n ... 0623conclusion.md`: 阅读原文档结构和现有实验结论。
  - `sed -n ... seedemu/compiler/kubernetes.py`: 核对 CNI type、VLAN-backed NAD、Kube-OVN NAD 生成逻辑。
  - `sed -n ... seedemu/k8sTools/resources/running/manageRunningStage.py`: 核对 VLAN parent 与 bridge 创建逻辑。
  - `rg ... seedemu/core seedemu/layers`: 查证 `NetworkType`、`joinNetwork()`、IX、OSPF、eBGP 与网络类型的关系。
  - `sed -n ... seedemu/k8sTools/resources/running/manageK8sManifest.py` 和 `installKubeOvnFabric.py`: 查证 Kube-OVN 是 non-primary CNI，只服务 SeedEMU secondary interfaces。
  - `rg ... 0623conclusion.md`: 检查新增章节关键词和位置；第一次检查时反引号中的 `tc` 被 shell 展开，触发了一次无参数 `tc` 帮助输出，没有修改系统状态，随后用单引号重跑成功。
  - `git -C /home/lxl/k8s diff -- largeScale/b62_k8s_scale/0623conclusion.md`: 检查实际文档 diff。
- Validation:
  - 文档新增章节已出现在 `0623conclusion.md` 第 238 行附近，包含 `tc`、SDN 边界和更具体的使用建议。
  - 本次只修改 Markdown 文档，没有运行真实 KVM/K3s/deploy 实验。
- Notes:
  - 结论强调：`OVN+OVS` 可以做 SDN fabric baseline 和控制面压力实验；如果要做真正的 SeedEMU 内部 SDN controller 实验，还需要扩展仿真器模型和 compiler 映射。

## 2026-06-25 14:30 - Add Large-Scale K8s Design Summary

- User intent: 总结大规模 K8s design，重点说明 `compile`、`build`、`deploy`、`start-bird`、`verify-bird`、`start-kernel`、`verify-kernel` 各阶段的逻辑与设计思路，并说明 `macvlan+VLAN`、`bridge+VLAN`、`OVN+OVS` 三种模式并存及默认选择。
- Scope:
  - `largeScale/b62_k8s_scale/README.md`
  - Source references from `runFullExperiment.py`, stage shell scripts, `verifyStartBird.py`, `verifyFibRoutes.py`, and `assignment.yaml`
- Changes:
  - 在 README 中新增 `Large-Scale K8s Design` 章节，说明 Kubernetes 只是运行时底座，SeedEMU 网络仍由容器内 Linux 接口、ARP、RIB/FIB、BIRD/OSPF/BGP 表达。
  - 增加 `Network Backend Model`：说明三种 backend 并存，当前默认 `assignment.yaml` 为 `macvlan+VLAN`；`bridge+VLAN` 用于可观测和故障注入；`OVN+OVS` 用于 SDN fabric/OVN 控制面实验。
  - 增加 `Stage Design`：分别总结 compile 的 by-AS hard placement、build 的远端构建/fast archive/per-node preload、deploy 的 VLAN parent 准备和 OVN node-stream 限流、start-bird 的 node-local `crictl`/`nsenter`、verify-bird 的 10-AS 采样和协议健康检查、start-kernel 的 kernel.conf 写入/reload、verify-kernel 的 FIB route count 检查。
  - 同步 README 的 complete flow、manual stage commands 和 artifacts，补上当前默认阶段 `reconvergence`。
  - 将 “Build only the VM/K3s/Kube-OVN cluster” 改为 “Build only the VM/K3s cluster”，避免默认 macvlan+VLAN 下产生误导。
- Commands:
  - `sed -n ... README.md`, `runExperiment.sh`, `runFullExperiment.py`, stage scripts: 阅读当前 workflow 和阶段实现。
  - `sed -n ... assignment.yaml`: 确认当前默认网络配置是 `macvlan+VLAN`。
  - `sed -n ... verifyStartBird.py`, `verifyFibRoutes.py` and `rg ...`: 核对 verify 目标选择和 pass condition。
  - `rg ... README.md`: 检查新增章节和关键术语。
  - `git -C /home/lxl/k8s diff -- largeScale/b62_k8s_scale/README.md`: 检查 README diff，并发现/删除重复的 `skipKubeOvnGatewayCheck` 说明段落。
- Validation:
  - 本次只修改 Markdown 文档，没有运行 KVM/K3s/deploy 实验。
  - README 现在明确记录当前默认 backend 为 `macvlan+VLAN`，并保留 `bridge+VLAN` 与 `OVN+OVS` 的设计定位。
- Notes:
  - 参数级细节仍以各 stage script 顶部常量和 `assignment.yaml` 为准；README 新增内容只描述 high-level design。

## 2026-06-25 15:04 - Create Standalone K8s Design Document

- User intent: 将刚刚总结的大规模 K8s design 单独写成一个 Markdown 文档。
- Scope:
  - `largeScale/b62_k8s_scale/largeScaleK8sDesign.md`
  - `largeScale/b62_k8s_scale/README.md`
  - `largeScale/b62_k8s_scale/codex_worklog.md`
- Changes:
  - 新增 `largeScaleK8sDesign.md`，用中文单独说明大规模 K8s 设计。
  - 文档覆盖总体思路、三种网络后端、默认 `macvlan+VLAN`、`compile`、`build`、`deploy`、`wait-ready`、`start-bird`、`verify-bird`、`start-kernel`、`verify-kernel` 和整体设计取舍。
  - 在 README 的 documentation/file role 表格中加入 `largeScaleK8sDesign.md`。
- Commands:
  - `sed -n ... README.md`: 读取刚刚写入 README 的 design 段落和文档列表位置。
  - `date '+%F %H:%M'`: 记录 worklog 时间。
- Validation:
  - 本次只修改 Markdown 文档，没有运行 KVM/K3s/deploy 实验。
  - 新文档为独立说明，不依赖生成目录或运行态文件。
- Notes:
  - README 仍保留简要 high-level design，新文档作为更完整的中文设计说明。

## 2026-06-26 06:27 - Document Reconvergence Design

- User intent: 参考 `/home/lxl/k8s/lxl/09_reconvegence_12node.py` 中 reconvergence 的思路，并把该思路补充到 `largeScaleK8sDesign.md`。
- Scope:
  - `largeScale/b62_k8s_scale/largeScaleK8sDesign.md`
  - Reference scripts: `/home/lxl/k8s/lxl/09_reconvegence_12node.py` and `/home/lxl/k8s/lxl/09_reconvegence.py`
- Changes:
  - 在 `largeScaleK8sDesign.md` 中新增 `Reconvergence 阶段`。
  - 说明 12-node wrapper 实际委托 legacy `09_reconvegence.py`，其核心设计是固定 chaos targets、固定 verify targets、baseline route count、`birdc down` 故障注入、load gate、route count 减少/增加作为收敛判据。
  - 明确 `birdc down` 是 daemon 级故障模型；如果实验语义要求只做 `kernel.conf + birdc configure`，应采用 protocol/config 级故障，而不是关闭整个 BIRD daemon。
  - 明确 start-kernel 的正常语义仍然是写 `/etc/bird/conf/kernel.conf` 后执行 `birdc configure` / `birdc 'reload kernel'`，reconvergence recovery 不能破坏该 include/kernel protocol 语义。
- Commands:
  - `sed -n ... /home/lxl/k8s/lxl/09_reconvegence_12node.py`: 确认 12-node 脚本是 wrapper。
  - `sed -n ... /home/lxl/k8s/lxl/09_reconvegence.py`: 阅读实际 reconvergence 实现。
  - `sed -n ... largeScaleK8sDesign.md`: 确认文档结构和插入位置。
  - `rg -n "Reconvergence 阶段|参考脚本的核心思路|daemon failure reconvergence|protocol/config reconvergence" largeScaleK8sDesign.md`: 验证新增章节关键词。
- Validation:
  - 本次只修改 Markdown 文档，没有修改 reconvergence Python 脚本，也没有主动停止后台 VM sweep。
  - 后台 sweep 在检查时仍在运行，已推进到 48VM compile；该运行状态不作为本次文档修改的验证结论。
- Notes:
  - 当前文档建议后续区分两类 reconvergence：daemon failure reconvergence 使用 `birdc down` 并按 start-bird 约束恢复；protocol/config reconvergence 不关闭 BIRD daemon，只通过 protocol/config 变更和 `birdc configure` 触发收敛。

## 2026-06-26 06:33 - Stop Sweep, Clean Cluster, Fix Reconvergence Recovery

- User intent: 停止正在运行的 multi-VM sweep，清除当前 Cluster 及相关配置，并修复 reconvergence 代码；后续由用户手动重新启动全流程。
- Scope:
  - `largeScale/b62_k8s_scale/measureReconvergence.py`
  - Current run directory: `runs/macvlan_vlan_vm_sweep_0625_fixed_20260625_235530_4954/20260625_235530_4954_vm48_w47`
  - Generated cluster files: `kubeconfig.yaml`, `cluster.inventory.yaml`, `configK3s.yaml`
- Changes:
  - 终止 `runMultiVmMacvlanVlanSweep.py`、当前 `runFullExperiment.py` 和 48VM `compile.sh` 进程。
  - 对 48VM 当前 run dir 执行 `clean.sh` 和 `destroyCluster.sh`，删除 namespace、K3s、B62 KVM VMs、B62 libvirt networks 和 `/data/lxl/k8sTools/b62/w47/disks`。
  - 删除 stale `configK3s.yaml`；`destroyCluster.sh` 已删除 `kubeconfig.yaml` 和 `cluster.inventory.yaml`。
  - 修复 `measureReconvergence.py` 的 `sendBirdUp()`：恢复阶段先复用 start-bird 的 `bird.conf` / `kernel.conf` include 去重规则；如果 BIRD daemon 已停，则清理 pid/socket、停止残留进程、重新启动 BIRD，并等待 `birdc show status`；如果 BIRD 仍可响应，则只执行 `birdc configure`。
- Commands:
  - `ps -eo ... | rg ...`: 确认 sweep、runFullExperiment、compile 进程。
  - `kill -TERM 987997 924188 502653`: 停止当前 sweep 和 48VM compile。
  - `./clean.sh <vm48 run dir>`: 清理当前 namespace；结果显示 namespace 已不存在并确认 matching resources deleted。
  - `./destroyCluster.sh <vm48 run dir>`: 销毁 K3s/KVM；结果显示 cleanup verification passed，B62 domains/networks 已清理。
  - `python3 -m py_compile measureReconvergence.py`: Python 语法检查通过。
  - `virsh -c qemu:///system list --all | rg 'seedemu-b62|bird-rtnl-dev'`: 确认无 B62 domain，仅 `bird-rtnl-dev` 仍 running。
  - `virsh -c qemu:///system net-list --all | rg 'seedemu-b62|virbrb62'`: 确认无 B62 libvirt network。
  - `rm -f configK3s.yaml`: 删除 stale generated cluster config。
- Validation:
  - 未发现残留 B62 sweep/runFullExperiment/stage 进程。
  - 未发现 B62 libvirt domain 或 network。
  - `kubeconfig.yaml`、`cluster.inventory.yaml`、`configK3s.yaml` 均已不存在。
  - `measureReconvergence.py` 通过 `py_compile`。
- Notes:
  - `bird-rtnl-dev` VM 不是本次 B62 cluster 的一部分，按用户当前请求未清理。
  - 本次没有重新运行全流程；修复后等待用户手动启动。

## 2026-06-26 06:45 - Add Reconvergence Protocol Health Checks

- User intent:
  - Ensure reconvergence monitor pods also record whether BIRD protocols are healthy.
  - Explain the duplicate `kernel.conf` include / `Kernel syncer already attached` failure mode.
- Scope:
  - `largeScale/b62_k8s_scale/measureReconvergence.py`
- Changes:
  - Added `birdc show protocols` and `birdc show route count` checks for the fixed verify pods used by reconvergence.
  - Added `reconvergence_protocol_checks.json`, containing baseline, failure, and recovery protocol snapshots.
  - Baseline and recovery protocol failures now fail the reconvergence stage.
  - Failure-stage protocol health is recorded but not treated as a hard failure because injected `birdc down` can legitimately make some peer sessions non-Established.
- Commands:
  - `rg -n "show protocols|protocol_check|Established|Passive|Active|birdc" ...`: found existing protocol-health logic.
  - `sed -n ... verifyStartBird.py`: reused the existing BIRD protocol parsing and failure rules.
  - `python3 -m py_compile /home/lxl/k8s/largeScale/b62_k8s_scale/measureReconvergence.py`: static validation.
- Validation:
  - `measureReconvergence.py` passed `py_compile`.

## 2026-06-26 06:54 - Remove Automatic kernel.conf Include Fallback

- User intent:
  - Clarify why the scripts were willing to append `include "/etc/bird/conf/kernel.conf";` when all generated `bird.conf` files should already contain that include.
- Scope:
  - `largeScale/b62_k8s_scale/start_bird_node_local.py`
  - `largeScale/b62_k8s_scale/start_bird_kernel_node_local.py`
  - `largeScale/b62_k8s_scale/measureReconvergence.py`
- Changes:
  - Removed automatic fallback behavior that appended `include "/etc/bird/conf/kernel.conf";`.
  - Start-bird, start-kernel, and reconvergence recovery now only validate include state.
  - If both wildcard include and explicit `kernel.conf` include are present, the stage reports duplicate include instead of silently mutating `bird.conf`.
  - If neither wildcard include nor explicit `kernel.conf` include is present while `kernel.conf` exists, the stage reports missing include instead of silently modifying `bird.conf`.
- Validation:
  - `rg ...`: confirmed no remaining automatic append pattern in the three scripts.
  - `python3 -m py_compile measureReconvergence.py start_bird_node_local.py start_bird_kernel_node_local.py`
  - `git diff --check` on the modified scripts.

## 2026-06-26 07:02 - Require Wildcard BIRD Config Include

- User intent:
  - Clarify that `include "/etc/bird/conf/kernel.conf";` is unnecessary because generated `bird.conf` files already use `include "/etc/bird/conf/*.conf";`.
- Scope:
  - `largeScale/b62_k8s_scale/start_bird_node_local.py`
  - `largeScale/b62_k8s_scale/start_bird_kernel_node_local.py`
  - `largeScale/b62_k8s_scale/measureReconvergence.py`
- Changes:
  - Tightened start-bird, start-kernel, and reconvergence recovery validation to require the wildcard include.
  - Treat explicit `include "/etc/bird/conf/kernel.conf";` as an unexpected configuration, because wildcard include already loads `kernel.conf`.
  - Treat missing wildcard include as an error instead of accepting explicit-only include.
- Commands:
  - `rg ...`: verified no automatic explicit `kernel.conf` include append remains.
  - `python3 -m py_compile measureReconvergence.py start_bird_node_local.py start_bird_kernel_node_local.py`: Python syntax validation passed.
  - `git diff --check ...`: whitespace validation passed.
- Validation:
  - Static validation passed; no runtime experiment was started.

## 2026-06-26 07:08 - Remove Explicit kernel.conf Include Check

- User intent:
  - Do not add checks for explicit `include "/etc/bird/conf/kernel.conf";`; rely on the code not generating that line.
- Scope:
  - `largeScale/b62_k8s_scale/start_bird_node_local.py`
  - `largeScale/b62_k8s_scale/start_bird_kernel_node_local.py`
  - `largeScale/b62_k8s_scale/measureReconvergence.py`
- Changes:
  - Removed `has_kernel` / unexpected explicit `kernel.conf` include checks from start-bird, start-kernel, and reconvergence recovery.
  - Kept the wildcard include prerequisite check because generated BIRD config is expected to load `/etc/bird/conf/*.conf`.
  - Confirmed the scripts do not append explicit `include "/etc/bird/conf/kernel.conf";`.
- Commands:
  - `rg ...`: confirmed no remaining `has_kernel`, unexpected explicit include check, or explicit include append pattern in the three scripts.
  - `python3 -m py_compile measureReconvergence.py start_bird_node_local.py start_bird_kernel_node_local.py`: syntax validation passed.
  - `git diff --check ...`: whitespace validation passed.
- Validation:
  - Static validation passed; no runtime experiment was started.

## 2026-06-26 09:19 - Remove BIRD Include Checks and Record 6VM Baseline

- User intent:
  - Do not add any check for `include "/etc/bird/conf/kernel.conf";`; the fix should be that local scripts never generate that line.
  - Preserve the latest 6VM baseline result and cleanup status.
- Scope:
  - `largeScale/b62_k8s_scale/start_bird_node_local.py`
  - `largeScale/b62_k8s_scale/start_bird_kernel_node_local.py`
  - `largeScale/b62_k8s_scale/measureReconvergence.py`
  - `largeScale/b62_k8s_scale/0625multiVM.md`
  - `runs/20260626_065919_4954_vm6_w5_baseline/summary.json`
- Changes:
  - Removed the remaining wildcard include checks from start-bird, start-kernel, and reconvergence recovery.
  - Confirmed these scripts no longer generate or check explicit `include "/etc/bird/conf/kernel.conf";`.
  - Added the 2026-06-26 6VM baseline retest result to `0625multiVM.md`.
  - Added `durationSeconds: 7282.37` to the baseline `summary.json`.
- Commands:
  - `rg -n "kernel\\.conf|include ..."`: located the remaining include-check blocks.
  - `python3 -m py_compile measureReconvergence.py start_bird_node_local.py start_bird_kernel_node_local.py`: Python syntax validation passed.
  - `rg -n "include \"/etc/bird/conf/kernel.conf\"|include \"/etc/bird/conf/*.conf\"|missing_wildcard|missing wildcard|has_glob"` on the three scripts: confirmed no include checks or explicit include generation remain.
  - `git diff --check` on the modified scripts: whitespace validation passed.
- Validation:
  - No cluster rebuild, namespace deployment, or VM operation was started for this code cleanup.
  - Earlier 6VM baseline run failed at `start-bird` after 4319s; B62 VMs/networks were cleaned by `destroyCluster.sh`.
- Notes:
  - The conditional 12/18/24/36/48 VM sweep was not started because the 6VM full flow did not pass.

## 2026-06-26 11:40 - Rerun 6VM Baseline and Add Start-BIRD Load Gate

- User intent:
  - Continue the active objective: run the 6VM full experiment under `assignment.yaml`; only run the 6/12/18/24/36/48 VM sweep if 6VM passes.
- Scope:
  - `largeScale/b62_k8s_scale/start_bird.sh`
  - `largeScale/b62_k8s_scale/0625multiVM.md`
  - `runs/20260626_092309_4954_vm6_w5_retry_include_free/summary.json`
  - `runs/20260626_102539_4954_vm6_w5_pre_start_load_gate/summary.json`
- Changes:
  - Added a start-bird pre-start host load gate in `start_bird.sh`: wait for host `load1 < 80` before invoking node-local SSH/nsenter startup.
  - Recorded two 6VM reruns in `0625multiVM.md`.
  - Added top-level `durationSeconds` and `failureReason` to the two rerun summaries.
- Commands:
  - `python3 runFullExperiment.py --assignment assignment.yaml --run-dir runs/20260626_092309_4954_vm6_w5_retry_include_free`: deploy and wait-ready passed, then start-bird entered node-local startup while host load1 was above 100; new SSH sessions to all six VMs timed out during banner exchange. Run was interrupted.
  - `bash -n start_bird.sh`: shell syntax validation passed after adding the load gate.
  - `python3 runFullExperiment.py --assignment assignment.yaml --run-dir runs/20260626_102539_4954_vm6_w5_pre_start_load_gate`: deploy and wait-ready passed; start-bird pre-start gate waited 1141s without host load1 dropping below 80. Manual SSH checks still timed out around host load1 92. Run was interrupted.
  - `virsh destroy/undefine` filtered to `seedemu-b62-*` domains and networks: cleaned the B62 VMs/networks after SSH management became unavailable.
- Validation:
  - Both reruns proved `compile`, `build`, `deploy`, and `wait-ready` can pass in 6VM macvlan+VLAN mode.
  - 6VM did not pass full flow because start-bird could not safely begin: VM SSH management remained unavailable before BIRD was started.
  - Post-cleanup verification found no B62 libvirt domains, no B62 libvirt networks, no B62 run/start-bird processes, and no generated `kubeconfig.yaml`, `cluster.inventory.yaml`, or `configK3s.yaml`.
- Notes:
  - The 12/18/24/36/48 VM sweep was not started because the user-defined precondition, “6VM full flow is OK,” was not satisfied.

## 2026-06-26 18:55 - Run 12/18/24/36/48 VM macvlan+VLAN Sweep

- User intent:
  - Do not add checks for `include "/etc/bird/conf/kernel.conf";`; rely on the scripts not generating that explicit include.
  - Continue the 4954-scale macvlan+VLAN multi-VM full-flow experiment for 12/18/24/36/48 VM and record the results.
- Scope:
  - `largeScale/b62_k8s_scale/0625multiVM.md`
  - `largeScale/b62_k8s_scale/codex_worklog.md`
  - `runs/macvlan_vlan_vm_sweep_0626_12plus_retry_20260626_124002_4954/`
- Changes:
  - Added the 12/18/24/36/48 VM full-flow results and key observations to `0625multiVM.md`.
  - Recorded this worklog entry with the experiment commands, cleanup actions, validation, and remaining state.
- Commands:
  - `python3 runMultiVmMacvlanVlanSweep.py --vm-counts 12 18 24 36 48 --topology-size 4954 --stop-on-failure --sweep-dir runs/macvlan_vlan_vm_sweep_0626_12plus_20260626_123734_4954`: first attempt failed at 12VM build because a stale disk image existed while the corresponding libvirt VM was not defined.
  - `./destroyCluster.sh runs/macvlan_vlan_vm_sweep_0626_12plus_20260626_123734_4954/destroy_after_disk_conflict`: cleaned the partial stale 12VM state.
  - `rm -rf /data/lxl/k8sTools/b62/w17/disks /data/lxl/k8sTools/b62/w23/disks /data/lxl/k8sTools/b62/w35/disks`: removed stale generated disk directories before retrying the sweep.
  - `python3 runMultiVmMacvlanVlanSweep.py --vm-counts 12 18 24 36 48 --topology-size 4954 --stop-on-failure --sweep-dir runs/macvlan_vlan_vm_sweep_0626_12plus_retry_20260626_124002_4954`: completed the full sweep successfully.
  - `rg -n 'include "/etc/bird/conf/kernel\.conf"|kernel\.conf.*include|include.*kernel\.conf' ...`: confirmed the checked b62 scripts do not generate or check the explicit `kernel.conf` include line.
- Validation:
  - Sweep status: `PASS`; total sweep wall time: `22204.48s`.
  - 12VM: `PASS`, total `3480.80s`, deploy `1290.87s`, start-bird `89.99s`, start-kernel `158.16s`, reconvergence `33.42s`.
  - 18VM: `PASS`, total `3894.90s`, deploy `1311.43s`, start-bird `51.79s`, start-kernel `107.98s`, reconvergence `31.21s`.
  - 24VM: `PASS`, total `4271.21s`, deploy `1350.04s`, start-bird `108.26s`, start-kernel `82.09s`, reconvergence `41.82s`.
  - 36VM: `PASS`, total `5245.21s`, deploy `1298.35s`, start-bird `74.87s`, start-kernel `57.29s`, reconvergence `31.61s`.
  - 48VM: `PASS`, total `5312.33s`, deploy `1292.42s`, start-bird `72.98s`, start-kernel `45.09s`, reconvergence `32.29s`.
  - `verify-after-start-bird`, `verify-after-fib-write`, and `reconvergence` all passed in every VM-count run.
- Notes:
  - The successful sweep leaves the final 48VM cluster available for manual inspection because the full-flow runner does not automatically destroy a successful final cluster.
  - During deploy, host load had short high peaks, but `pending`, `creating`, and per-node `creating_by_node` did not accumulate long term; this did not reproduce the OVN+OVS CNI ADD long-tail failure.
  - In the checked run logs, `start-bird` was idempotent: containers already had BIRD running, so the stage skipped already-running BIRD instances and then verified protocol health.

## 2026-06-27 15:25 - Add Reconciled Timing Columns to 0625multiVM

- User intent:
  - The 0625 multi-VM summary table should expose `build-cluster`, `compile`, and `build` time directly.
  - `total(s)` and `workload stages(s)` should be explainable from the measured stage timings, so the table does not hide where non-deploy time is spent.
- Scope:
  - `largeScale/b62_k8s_scale/runMultiVmMacvlanVlanSweep.py`
  - `largeScale/b62_k8s_scale/0625multiVM.md`
- Changes:
  - Added `measured stages(s)`, `infra stages(s)`, and `untracked overhead(s)` to the summary table.
  - Added direct columns for `build-cluster(s)`, `compile(s)`, `build(s)`, and `wait-ready(s)`.
  - Regenerated `0625multiVM.md` from the existing completed sweep summary; no VM, cluster, or workload command was run.
  - Updated the report explanation so the accounting is explicit: `total(s) ~= measured stages(s) + untracked overhead(s)` and `measured stages(s) = infra stages(s) + workload stages(s)`.
- Commands:
  - `python3 -m py_compile runMultiVmMacvlanVlanSweep.py`: syntax validation passed.
  - `python3 - <<'PY' ... sweep.updateReport(...)`: regenerated `0625multiVM.md` from `runs/macvlan_vlan_vm_sweep_0626_12plus_retry_20260626_124002_4954/macvlan_vlan_vm_sweep_summary.json`.
  - `python3 - <<'PY' ... column count check`: verified the Markdown summary table has a consistent 19-column shape.
  - `python3 - <<'PY' ... overhead check`: verified each run's `total(s) - measured stages(s)` is about 0.6s, so the reported timing now reconciles.
  - `git diff --check -- largeScale/b62_k8s_scale/runMultiVmMacvlanVlanSweep.py largeScale/b62_k8s_scale/0625multiVM.md`: whitespace validation passed.
- Validation:
  - The updated report now shows where the previously unexplained time went: most of it is in `infra stages(s)`, especially `build-cluster(s)`, plus `compile/build/wait-ready`.
  - No experiment was rerun; all timing data still comes from the previously completed PASS sweep.

## 2026-07-27 23:12 - Multi-Host macvlan+VLAN 4954 Full Flow

- User intent:
  - Use one minimal `configMultiHostKvmMacvlan.yaml` to build a multi-host,
    multi-VM K3s cluster for a strictly VLAN-isolated macvlan SeedEMU data
    plane.
  - Remove misleading infrastructure names such as `kvmOvn`.
  - Provide one full-flow script and manual stage commands.
  - Run the complete 4954 workflow, including static placement, ping/iperf3,
    BIRD/FIB, and reconvergence evidence.
- Confirmation:
  - The user explicitly approved destructive cluster operations on local
    hypervisor `10.202.236.88` and remote hypervisor `10.202.191.39`.
  - The user explicitly approved the full BIRD/FIB/reconvergence flow.
- Main scope:
  - `configMultiHostKvmMacvlan.yaml`
  - `runMultiHostMacvlanExperiment.sh`, `runFullExperiment.py`
  - `validateCrossHostWorkload.py`, `validateMultiHostK3s.py`
  - `fastSeedemuImageArchive.py`
  - `seedemu/k8sTools/` neutral kinds and multi-host fabric setup
  - `seedemu/compiler/kubernetes.py`
  - `0727k8sExperimentDesgin.md`, `README.md`
- Changes:
  - Added neutral canonical infrastructure kinds `kvm`, `physical`, and
    `multiHostKvm`; old OVN-suffixed values remain warning-producing aliases.
  - Added two cross-hypervisor VLAN trunks using dedicated VM `ens3/ens4`,
    unnumbered host bridges, and flood-mode VXLAN.
  - Added pre-K3s same-VLAN connectivity, 1400-byte DF packet, and
    different-VLAN isolation gates.
  - Added `fabric.mtu: 1400`; compiler-generated macvlan/ipvlan/bridge NADs
    now carry the configured secondary-interface MTU.
  - Added deterministic non-IX cross-hypervisor router-pair selection. The
    selected pair is fixed before checking iperf3, so missing software fails
    instead of changing the sample.
  - Added all-NAD MTU validation, one runtime secondary-interface MTU sample
    per K3s node, bidirectional warmup, ping, 1400-byte DF ping, and TCP
    iperf3.
  - Fixed fast archive handling of effective pre-COPY Dockerfile `RUN`
    instructions by building shared software-layer variants. Unsupported
    post-COPY `RUN` now fails fast.
  - Added automatic static placement statistics to `summary.json` after
    compile.
- Primary command:
  - `./runMultiHostMacvlanExperiment.sh runs/4954_20260727_202729_multihost_macvlan_vlan`
- Failure and recovery record:
  - The first deploy process failed after its active shell script was edited
    in place. The failed stage remains in `summary.json`; a clean process
    resumed deploy and submitted all 4954 Deployments.
  - The first dynamic scan found that the fast archive had omitted router
    Dockerfile `RUN apt-get ... iperf3`; the archive implementation and images
    were rebuilt.
  - Small ping passed while iperf3 connected but transferred zero bytes.
    Root cause was a 1500-byte secondary MTU plus VLAN/fabric-VXLAN overhead
    on a 1500-byte underlay. All 4763 NADs were updated to 1400.
  - Restarting only the probe pair left 4952 old-MTU Pods. Partial BGP
    sessions continued to flap, so all workload Pods were deleted and
    recreated from the repaired NADs and images.
  - Multus reported pre-rename interface `net1`; runtime interface lookup now
    resolves by selected secondary IP and correctly finds `ix10`.
- Final experiment result:
  - Run: `runs/4954_20260727_202729_multihost_macvlan_vlan`
  - Status: `PASS`; finished `2026-07-27T23:03:52+0800`.
  - Cluster: 2 hypervisors, 30 VMs, 280 vCPU, 589 GiB RAM, 80 GiB/VM.
  - Live state: 30/30 nodes Ready and schedulable; 4954/4954 workload Pods
    Running/Ready; no failed Pod.
  - Static placement: 1086 AS groups, 30 nodes, 4954 Pods, 165..166 Pods per
    node, master=166 Pods.
  - Secondary MTU: 4763/4763 NADs at 1400; 30/30 node runtime samples at 1400.
  - Fixed pair: AS1269 r10 on worker1 and AS1273 r10 on worker24 over
    `net-ix-ix10`.
  - Ping average: 0.411/0.421 ms, 0% loss.
  - 1400-byte DF ping average: 0.936/0.552 ms, 0% loss.
  - TCP iperf3: 882.53/894.09 Mbps over one five-second stream per direction.
  - Reconvergence: failure 0.83s, recovery 14.84s, total stage 34.70s;
    recovery protocol health 4/4.
- Validation commands:
  - `python3 -m unittest tests.seed_k8s_multihost_macvlan_test tests.seed_k8s_fast_archive_test tests.kubernetes_by_as_hard_test.KubernetesByAsHardCompilerTest.test_by_as_hard_uses_bridge_for_internal_links_and_macvlan_for_ix -v`: 9 tests passed.
  - `python3 -m py_compile ...`: all changed Python entrypoints passed.
  - `bash -n ...`: all changed B62 shell entrypoints passed.
  - `manageMultiHostKvmConfig.py --config configMultiHostKvmMacvlan.yaml validate`:
    validated 2 hypervisors, 30 VMs, and 2 fabric trunks.
  - `git diff --check` and trailing-whitespace scan on scoped files: passed.
  - Live `kubectl` verification: 30/30 nodes and 4954/4954 Pods Ready.
- Remaining state:
  - The successful cluster and namespace
    `seedemu-b62-mvlan-vlan-4954` are intentionally left running for
    inspection.
  - This run validates correctness but is not a placement comparison: it has
    one placement, one deterministic pair, and no repeated baseline trials.

## 2026-07-28 11:35 - Parent VLAN Inventory, Cluster Destroy, and Single-Host Config

- User intent:
  - Report the approximate parent VLAN count and VID ranges in every VM.
  - Clean the 4954 namespace and destroy all 30 B62 VMs and multi-host fabric.
  - Add a single-hypervisor, 30-VM macvlan+VLAN configuration with the same
    resource and network parameters as the multi-host configuration.
  - Document both complete execution flows and every major called file in
    `0728ExperimentDesign.md`.
- Confirmation:
  - The user explicitly requested namespace deletion and VM destruction.
  - The requested target was the active B62 multi-host cluster only; unrelated
    local domains and libvirt networks were preserved.
- Runtime evidence:
  - `runs/4954_20260728_105853_cleanup_destroy/`
  - `vlan_parent_inventory_before_destroy.json/.csv`
  - `cleanup_summary.json`
  - `clean.log`, `destroy-cluster.log`, `destroy-remote-recovery.log`
  - `destroy-final-hypervisor-verification.log`
  - `destroy-final-management-ip-verification.log`
  - `final_destroy_verification.json`
- Parent VLAN result:
  - 30/30 VMs were reachable and inspected before destroy.
  - Each VM had two physical data-plane parents, `ens3` and `ens4`.
  - Total VLAN parent subinterfaces: 6834; per VM min/max 215/254, mean
    227.8, median 227.
  - `ens3.<VID>` instances: 4094 total; `ens4.<VID>` instances: 2740 total.
  - Per-VM counts, sparse min/max VID ranges, and exact VLAN ID arrays are
    preserved in the JSON/CSV evidence.
- Cleanup and destroy commands:
  - Ran `./clean.sh runs/4954_20260728_105853_cleanup_destroy` with the explicit
    multi-host configK3s, kubeconfig, and inventory environment.
  - Clean removed 4954 Deployments, 3916 ReplicaSets, 4255 Pods, 4763 NADs,
    and 4967 Events. The namespace was confirmed absent after 18 seconds.
  - Ran `python3 k8sTools.py destroy -d
    configK3sMultiHostKvmMacvlan.yaml`.
  - Local 24-VM cleanup passed. Remote host-local cleanup returned 141 while
    processing worker28; two domains and three libvirt networks remained.
  - Verified the residual worker28/29 disks were under the recorded
    `/home/seed/k8sTools/b62-multihost/idc/disks` guard, then removed those
    domains, networks, VXLAN/bridge links, routes, and the remote work dir.
  - Copied the generated configK3s into the timestamped evidence directory
    before removing the stale generated cluster output.
- Destroy fix:
  - Changed the protected disk lookup in
    `seedemu/k8sTools/resources/setup/kvm/destroyKvmVms.py` from an early-exit
    awk pipeline to a full-input awk reduction. This prevents upstream
    `virsh domblklist` from receiving SIGPIPE under `set -o pipefail`.
- Single-host implementation:
  - Added `configSingleHostKvmMacvlan.yaml`: one master plus 29 workers,
    280 vCPU, 589 GiB memory, 80 GiB/VM, dedicated `10.81.0.0/24`
    management network, and two `ens3/ens4` VLAN trunks.
  - Added `kvm/prepareKvmNetworks.py` and wired it into
    `runner.py::_buildKvm()` so a standalone `kind: kvm` build defines its
    management NAT and unnumbered L2 trunk networks from YAML.
  - Added compact KVM resource-plan accounting and skipped the explicit-node
    DHCP preprocessing stage when `master/workers` generates the node list.
  - Added `runSingleHostMacvlanExperiment.sh`.
  - Extended `validateCrossHostWorkload.py` with deterministic
    `--pair-scope cross-node`; single-host full flow now records
    cross-node ping/DF-ping/iperf3, while multi-host retains cross-/24
    selection.
- Documentation:
  - Added `configSingleHostKvmMacvlan.md`.
  - Added `0728ExperimentDesign.md` with all 30 per-VM parent counts, cleanup
    evidence, architecture comparison, automatic/manual commands, stage file
    call chain, static placement outputs, and dynamic measurement behavior.
  - Updated `README.md`, `0727k8sExperimentDesgin.md`, and the k8sTools setup
    resource README to reflect the destroyed cluster and the new single-host
    entrypoint.
- Validation commands:
  - `python3 -m unittest -v tests.seed_k8s_singlehost_macvlan_test
    tests.seed_k8s_multihost_macvlan_test`: 9 tests passed.
  - `python3 -m py_compile ...`: new/modified Python entrypoints passed.
  - `bash -n runSingleHostMacvlanExperiment.sh
    runMultiHostMacvlanExperiment.sh`: passed.
  - `shellcheck runSingleHostMacvlanExperiment.sh`: passed when available.
  - YAML validation confirmed both explicit configs use canonical kinds,
    macvlan+VLAN, and `ens3/ens4` trunks.
  - Offline KVM helper rendering produced 30 nodes from
    `seed-k3s-master/10.81.0.10` through
    `seed-k3s-worker29/10.81.0.39`, two extra networks, a 30-node configK3s,
    and a 30-node kvmState.
  - `git diff --check` and trailing-whitespace validation passed for the
    scoped files.
  - Final local/remote verification found zero target domains, target
    networks, target VXLAN/bridge links, or experiment work directories.
  - A parallel SSH probe found 0/30 VM management IPs reachable after destroy.
- Remaining state:
  - The B62 4954 namespace and multi-host K3s VM cluster are destroyed.
  - No single-host VM or libvirt network was created during this task; the
    single-host deliverable was validated offline so the requested clean host
    state remains intact.

## 2026-08-09 18:25 - B62 3083/6-VM Full-Flow K8S Benchmark

- User intent:
  - Run the same complete logical stages as the native runtime, including
    BIRD verification, kernel export, all-AS FIB verification, deterministic
    workload validation, and reconvergence.
  - Compare stage time and host/QEMU CPU, memory, disk, network, throughput,
    and convergence under an identical VM resource contract.
- Implementation:
  - Added common five-second `performanceMetrics.py` sampling to every stage
    in `runFullExperiment.py`.
  - Made BIRD/FIB verification gates assignment-driven; the benchmark waits
    for load1 below 50, samples fixed 10 AS representatives after BIRD, and
    checks all 312 router AS representatives after kernel export.
  - Added deterministic six-chaos/four-observer reconvergence targets and
    timing settings to `assignmentBenchmark3083.yaml`.
  - Fixed assignment-generated KVM runs to include
    `validate-cross-node-workload`, and documented the benchmark fields and
    performance artifacts.
  - Used exactly six 48-vCPU/96-GiB/100-GiB VMs, totaling 288 vCPU and
    576 GiB, matching the native run.
- Formal command:
  - `python3 runFullExperiment.py --assignment assignmentBenchmark3083.yaml
    --run-dir runs/benchmark_3083_20260809_160300_k8s_vm6`
- Formal result:
  - Status PASS; 3083/3083 Pods Running/Ready; placement
    514/514/514/514/514/513.
  - Stage seconds: build-cluster 382.53, compile 128.73, build 289.38,
    deploy 1196.08, wait-ready 2.03, start-bird 32.44, start-kernel 190.82,
    all-AS FIB verify 19.45, workload 27.85, reconvergence 22.06.
  - Explicit start-bird found all 3083 BIRD processes already started by the
    Pod entrypoint and skipped them; this lifecycle attribution is documented
    in the cross-runtime report.
  - Fixed IX10 AS1269/AS1271 pair passed bidirectional ping and five-second
    TCP at 10546.69/10150.33 Mbit/s with zero retransmits.
  - Reconvergence PASS: failure 0.70s, recovery 9.41s, total 21.70s; fixed
    observer protocol health passed.
- Cleanup:
  - Ran `python3 k8sTools.py destroy -d configK3s.yaml --keep-temp` after all
    result artifacts were durable.
  - K3s uninstall completed on all six nodes, then exact
    `seedemu-benchmark-*-w5` domains and `seedemu-benchmark-net-w5` were
    removed; cleanup verification passed.
  - Unrelated `bird-rtnl-dev` and B63 networks were preserved.
- Validation:
  - Modified Python entrypoints passed `py_compile`; modified shell scripts
    passed `bash -n`; assignment rendering confirmed 288 vCPU/576 GiB;
    native-side comparator reported PASS for both completed run directories.
- Evidence:
  - `runs/benchmark_3083_20260809_160300_k8s_vm6/summary.json`
  - `performance_metrics.csv`, `performance_summary.json`,
    `cross_node_workload_summary.json`, `reconvergence_summary.json`
  - Cross-runtime report:
    `/home/lxl/multihost/runs/comparison_3083_20260809/`

## 2026-08-10 16:20 - Canonical output_4954 Six-VM Full-Flow Benchmark

- User intent:
  - Rerun native and K8S from the same
    `/home/lxl/seed-emulator/topology/output_4954` source.
  - Use this B62 directory's `start_bird.sh` and
    `start_bird_kernel.sh` for both runtimes with the same parameters.
  - Do not force a BIRD/kernel-export initial-state check before start;
    retain the mandatory post-BIRD protocol and post-kernel all-AS FIB gates.
- Implementation:
  - Added `assignmentBenchmark4954.yaml` with six 48-vCPU/96-GiB/100-GB
    VMs and the fixed reconvergence targets.
  - Added `adaptCanonicalOutput.py`; K8S compilation consumes all 4954
    canonical nodes and only adds the Kubernetes address-replacement hook.
  - Added Docker dispatch to both shared B62 shell entrypoints through
    `runDockerBirdStage.py`, keeping the same concurrency, delay, batch,
    cooldown, load, and timeout parameters as the K8S backend.
  - Made kernel reload strict and BIRD-version-safe by resolving the actual
    Kernel protocol name from `birdc show protocols`; added
    `runFullExperiment.py --resume-from start-kernel` for auditable recovery.
- Canonical evidence:
  - Compose SHA-256:
    `85ad7340d23401e7c23ab4623d97921684336a15075f8a1b26aea7c1f84edbf2`.
  - Runtime node count: 4954/4954.
  - BIRD/kernel aggregate SHA-256:
    `32757bd31fc92c53ca0552807e7d16a48ce0ebcfe559452d7cea17a479c5b2d5` /
    `08e7f572929b0464ed92ba3cf950c9a8698700a8bbc958a1aa3cbb8fc8615c25`.
- Formal run:
  - `runs/4954_20260810_124801_w5_canonical_retry1`, final status PASS.
  - The first strict `start-kernel` attempt correctly failed 4954/4954
    because the old command used the BIRD keyword `kernel` while the actual
    unnamed protocol was `kernel1`. The failed attempt remains in
    `summary.json`; after the dynamic-name fix, the run resumed from
    `start-kernel` and passed 4954/4954.
  - Successful stage seconds: compile 671.01, build 382.25, deploy 2285.28,
    start-bird 182.49, BIRD verify 39.54, start-kernel 682.71, FIB verify
    44.36, workload 31.13, reconvergence 80.19.
  - BIRD fixed samples passed 10/10 on the second poll; all 674 router-AS
    FIB representatives passed 674/674.
  - The deterministic IX10 AS1269/AS1270 pair passed bidirectional ping and
    5-second TCP at 9212.071/8876.397 Mbit/s with 1/1 retransmits.
  - Reconvergence PASS: fault convergence 2.91 seconds, recovery convergence
    58.95 seconds, total 79.81 seconds.
- Cleanup:
  - The official destroy reached its 900-second node cleanup timeout on two
    VMs but continued host cleanup. Six residual exact benchmark domains were
    then destroyed and undefined by name, and benchmark networks were
    removed. No unrelated domain was touched.
- Validation:
  - Changed Python entrypoints passed `py_compile`; B62 shell entrypoints
    passed `bash -n`; assignment YAML parsed; scoped `git diff --check` and
    trailing-whitespace checks passed.
  - The comparator explicitly marks two non-identical substage boundaries:
    native `wait-ready` includes container start/structural/fabric checks,
    while K8S starts Pods mainly in `deploy`; native workload validation runs
    20 routed pings plus 10 L2-isolation probes, while K8S uses one fixed pair.
    The report therefore provides grouped lifecycle timing and a total that
    excludes the unequal-breadth workload validation stage.
  - Cross-runtime comparator status PASS:
    `/home/lxl/multihost/runs/comparison_4954_20260810_161714_native_vs_k8s/`.
## 2026-09-14 - GiB Memory And Single-Host 1078 / 6-VM Validation

- User intent: support GB-scale memory directly in cluster YAML, run the full
  single-host wrapper at 1078 scale / 6 VMs, perform clean and destroy, and
  explain the subsequent 48-VM / 4954 configuration.
- Scope: resource-unit conversion shared by native API and copied setup,
  single/multi-host input YAML, B62 resource summary and wrapper, focused
  tests and documentation. Existing unrelated changes were preserved.
- Configuration: memoryGiB (memoryGb alias) converts to integral MiB; old
  memoryMb/memory_mb remains supported, conflicting/invalid values fail.
  Single-host preset: 48 vCPU/96 GiB master and 5 x 50 vCPU/100 GiB workers,
  80 GiB disk each. Multi-host retains existing resources, changing units only.
- Evidence directory: runs/1078_20260914_144851_singlehost_6vm/. The pre-edit
  input_before snapshot preserves the user's 47-worker/6-vCPU configuration.
- Validation: direct execution of the single-host (7 tests) and multi-host
  (5 tests) test files passed, using PYTHONPATH for repository/B62 imports.
  Python compilation and wrapper bash syntax checks passed. Earlier unittest
  discovery unexpectedly invoked unrelated package load_tests suites, which
  failed on a Docker SDK http+docker incompatibility; those are not counted as
  successful validation. Scoped tests were then run directly.
- Runtime: started runSingleHostMacvlanExperiment.sh with the explicit evidence
  directory, console.log and per-stage logs. Initial host inspection found no
  running libvirt VM; bird-rtnl-dev was shut off. Existing Docker workloads and
  default/B63 trunk networks are unrelated and must remain untouched.
- Progress: initial build is preparing bootstrap images. Missing local tags
  were loaded from the existing image-cache tarballs; completion and cleanup
  results will be recorded below once verified.

### Runtime Outcome And Follow-Up Validation

- Main evidence: `runs/1078_20260914_145500_singlehost_6vm/` and its
  `experimentResult.md`. The earlier directory retains the image preparation
  timeout before VM creation and original user input snapshots.
- Commands: `docker load -i` imported the existing Multus, local-path,
  CoreDNS and metrics-server archives; the wrapper was run against the main
  directory, then repeated with `--resume-from start-kernel` after fixing
  topology-specific reconvergence target selection.
- Full-run result remains FAIL, not silently upgraded: build through
  dataplane passed; first reconvergence attempt failed before fault injection
  due to missing hardcoded targets; resumed reconvergence failed because its
  immediate protocol health check preceded BGP session recovery.
- Manual read-only recovery check passed all 14 fault/observer routers at
  15:22:44 UTC+8. Post-recovery `verifyFibRoutes.py --sample-as-count 0`
  passed 82/82 AS representatives at load1 < 50. All 1078 Pods remained Ready.
- Scoped runtime fixes: wait-ready now requires the full expected Pod count;
  reconvergence defaults use numeric ASN/router ordering without Pod hashes
  or VM placement. Explicit target lists remain strict. No automatic recovery
  wait or weakened protocol check was introduced.
- Validation commands: direct single-host (9 tests), multi-host (5), runner
  stage contract (3), named inventory unittest module (1): 18 tests passed.
  Python `py_compile`, shell `bash -n`, and scoped `git diff --check` passed.
  The pre-existing global whitespace issue in `0623conclusion.md` was left
  untouched. No unrelated worktree changes, host Docker workloads, or B63
  networks were reverted or removed.
- Configuration guidance: 48 VMs = master + 47 workers. Documented example
  uses master 48 vCPU/96 GiB and workers 5 vCPU/10.5 GiB each, total 283 vCPU/
  589.5 GiB; assignment must also select 4954 and workerCount 47. This larger
  configuration was not started in this task.
- Cleanup is being recorded independently in `cleanup_summary.json` and
  `cleanup-orchestrator.log`, preserving the failed experiment status.
- Final cleanup result: PASS. `runStage` invoked `clean.sh <run-dir>` with
  the explicit single-host context (253.55 seconds), then
  `python3 k8sTools.py destroy -d configK3sSingleHostKvmMacvlan.yaml --keep-temp`
  (23.51 seconds). Namespace absence was checked before destroy. At 15:28:46
  UTC+8, independent virsh/filesystem checks found no experiment domains,
  VM disks/cloud-init, dedicated networks or bridges. The unrelated domain
  bird-rtnl-dev remained shut off; default/B63 networks and 137 host Docker
  workloads remained. Logs/caches/temp debug data were intentionally retained.
- Remaining issue: the one-shot recovery health gate should be redesigned
  to distinguish initial route change from protocol recovery; the automated
  experiment is still FAIL despite successful independent recovery checks.

## 2026-09-14 15:41 - Single-YAML Single-Host Entry

- User intent: maintain only configSingleHostKvmMacvlan.yaml for VM resources
  and topology/experiment settings; stop requiring edits to assignment.yaml.
- Scope: runSingleHostMacvlanExperiment.sh, runFullExperiment.py, lib.sh,
  the single-host input/guide, B62 README, B62B63RunGuide.md and focused tests.
- Changes: added --config single-input mode; copied effective placement and
  deploy settings into the user YAML. Derive workerCount from workers.count,
  default namespace/name from topologySize, and canonical output from topologyDir.
  Reject a duplicated experiment.workerCount and mixed old/new CLI inputs.
  Preserve the current 6-VM/1078 preset; no 48-VM launch or preset switch.
- Snapshots: automatically write run-local cluster-input.yaml and assignment.yaml
  with resolved paths. Standalone stages read saved runtime paths without B62
  exports; resume reads the old run even if its original input is removed.
  Existing run summaries cannot be overwritten by new single-config runs.
  Legacy assignment and explicit dual-input/multi-host entrypoints remain.
- Commands and validation:
  - `bash runSingleHostMacvlanExperiment.sh --prepare-only`: passed twice;
    latest evidence is runs/1078_20260914_154121_singlehost_macvlan_vlan/.
    Status PREPARED, 6 VMs, 298 vCPU, 596 GiB; no cluster operations.
  - Direct `python3 tests/seed_k8s_singlehost_macvlan_test.py`: 15 tests passed.
    Covers single-input independence, 48-VM resource derivation, invalid fields,
    wrapper prepare-only, standalone stage context, frozen resume, and legacy
    input compatibility. Full execution/resume lifecycle commands are mocked;
    test output saying experiment passed is not a live experiment result.
  - Direct multihost tests: 5 passed; runner stage contract tests: 3 passed;
    named `python3 -m unittest seed_k8s_cluster_inventory_test -v`: 1 passed.
    PYTHONPATH included repository/B62 (and tests for the named module).
  - `python3 -m py_compile` for changed runner/test passed; separate `bash -n`
    checks for wrapper and lib.sh passed; scoped `git diff --check` passed.
  - `virsh list --all --name`: only the unrelated bird-rtnl-dev remains.
- Notes: no build/deploy/clean/destroy or dataplane experiment was run for this
  configuration refactor. The known reconvergence timing problem is unchanged.
  Older experiment artifacts and unrelated dirty worktree changes were retained.
  Repository assignment.yaml is still used by legacy entrypoints, not unused.

## 2026-09-14 17:44 - BIRD Recovery Stability And 4954 / 48-VM Resume

- User intent: monitor the ten restarted BIRD routers every 5s, require two
  unchanged rounds, timestamp the first stable sample, resume the failed 4954
  run, then clean and destroy after verification.
- Diagnosis: run 4954_20260914_155524_singlehost_macvlan_vlan exited FAIL at
  17:16:42 UTC+8, recovery_protocol_health_failed. No experiment process was
  still running. All 48 VMs remained Ready. The old gate exited on two +1 FIB
  changes although aggregate FIB count was still -1 and all 4 protocol checks
  were unhealthy. Original evidence copied into before-recovery-stability-fix/.
- Changes: measureReconvergence.py tracks per-fault-router BIRD totals with
  resettable plateaus, two unchanged comparisons, monotonic first-sample
  timestamps, all-router/global times, protocol/load gates, bounded timeout,
  recovery_samples.jsonl and recovery_stability.json. runFullExperiment.py
  supports --resume-from reconvergence without rebuild/redeploy. Documentation
  and tests updated; the user's current VM resource config was not changed.
- Commands: live read-only precheck found 4954 Pods and identical 10/4 target
  prefixes; all 14 monitored protocols passed, max VM load1 0.88. Executed
  `runSingleHostMacvlanExperiment.sh <run-dir> --resume-from reconvergence`.
  New stage PASS, 67.57s; first stable time 43.998s, confirmation 53.936s,
  health validation 54.733s. Initial plateaus were correctly revoked when
  routing changed. All 10 stable, all 14 protocol checks passed.
- Validation: `python3 tests/seed_k8s_reconvergence_test.py` (with repository
  PYTHONPATH for runner import) passed 11 tests; py_compile and scoped
  git diff --check passed. Resume unit test mocks lifecycle operations.
  Independent verifyStartBird.py sample-count 10 passed 10/10, load1 4.02;
  verifyFibRoutes.py sample-as-count 0 passed 674/674, load1 0.85.
  kubectl snapshot confirmed 4954/4954 Ready before clean.
- Artifacts: recoveryFixResult.md, recovery-resume.console.log, new sampling
  JSON/JSONL and post-recovery-check/ in the existing scale+timestamp run.
  Original ping/iperf stage remained PASS; not repeated during this resume.
- Cleanup: started clean.sh through runStage with the recorded runtime context;
  clean/destroy completion and independent residual verification follow below.
- Final cleanup: clean PASS in 1067.51s, finished 18:01:50 UTC+8; removed the
  namespace after workload objects and 52651 events.k8s.io entries. Master
  briefly reached load1 80.22 during Pod deletion, with 32840 MiB available;
  load later fell to 28.58. This was cleanup, not recovery measurement.
- Verified namespace absence and matching embedded 48-node destroy metadata,
  then ran `python3 k8sTools.py destroy -d configK3sSingleHostKvmMacvlan.yaml
  --keep-temp` through runStage. PASS in 136.56s, finished 18:04:38.
  At 18:05:14 independent checks found no owned VM/domain, disk/cloud-init,
  dedicated network or bridge. New concurrent b63-sh6 VMs/networks were left
  untouched, as were old B63 networks and bird-rtnl-dev (shut off).
  cleanup_summary.json and cleanup-verification.json are PASS; the experiment
  summary retains original FAIL history, latest PASS, and destroyedAfterPass=true.
