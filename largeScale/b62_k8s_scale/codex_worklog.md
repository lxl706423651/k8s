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
