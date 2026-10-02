# PDF 约束式放置研究

本目录按 `SEED_K8S_placement_model.pdf` 建立独立模型，不沿用上一版通信收益与争用损失的加权和。完整链路是：编译信息提取 → 路由资源标定 → VM 二维性能标定 → 带不确定性余量的可行域 → 通信最优化 → 独立部署验证。当前已完成求解、增量评估、214镜像审计与真实214-Pod部署链验证。容器模型独立测试已完成，但原Q模型未通过全部精度门槛，且实测发现必须补计节点管理开销；**尚未完成可支持全部审稿结论的独立部署验证**。

## 模型实现与语义

`solvePlacement.py` 实现 PDF 式 (16)–(22)。每个虚拟网络使用全体成员对数量作为分母，按各 VM 成员计数计算通信切分；没有用环形图近似替代目标函数。资源容量和部署限制为硬约束，Q 阈值默认为 0.8。质量查询使用单调二维网格与独立校准余量；正式画像采用上角点保守查表，双线性用于兼容性测试。超出网格范围拒绝放置。

容量 C 是 Pod 可用预算；背景 b 是该预算范围内未被 Pod 需求解释的占用，不能重复扣除系统预留。按 PDF 原式，硬约束检查 Σd≤C，压力检查 (b+Σd)/C。CPU 单位为核、Memory 单位为 GiB；容量、基础占用、Pod 需求必须同口径。

求解器按 dominant demand、网络重要度、固定种子的随机顺序构造多个可行解，再使用单 Pod 移动、跨 VM 交换及网络/AS 社区内 10、15、20 Pod 的批量移动。仅接受可行且严格降低通信代价的变更。启发式构造失败不等于不存在可行解。群组候选采用有序分块，属于有界候选启发式，不声称枚举全部社区子集。候选使用受影响超边和VM的增量评估，每个接受步骤仍以全量公式复核。规模实验结果见 ALGORITHM_FINDINGS.md。

## 文件角色

| 文件 | 角色 |
| --- | --- |
| `SEED_K8S_placement_model.pdf` | 用户提供的模型规范 |
| `solvePlacement.py` | 纯离线求解器与约束审计库 |
| `validatePlacement.py` | 独立成员对公式、穷举最优对照及边界验证 |
| `EXPERIMENT_PROTOCOL.md` | 实测前冻结的设计、验收门槛与证据缺口 |
| `results/source.txt` | PDF 文本提取产物 |
| `results/synthetic_instance.json` | 自动生成的合成算法测试输入，禁止作为标定 |
| `results/algorithm_validation.json` / `.log` | 算法测试结果与输出 |
| `results/synthetic_solution.json` | CLI 冒烟测试产物 |
| `codex_worklog.md` | 命令、变更和验证记录 |
| `__pycache__/` | Python 运行缓存 |

## JSON 接口

顶层 `pods`、`nodes`、`networks`、`costs`、`surfaces` 为必需字段，`eta` 可选，默认 0.8。

- Pod：`id,cpu,memory` 必需；`allowedNodes` 可选（默认全部节点），`networkTokens` 可选（默认无额外 CNI/VLAN 要求），`community` 可选（AS/社区标签）。cpu/memory 应来自拟合路由需求加用户额外预算，而不是随意配置常数。
- Node：`id,cpu,memory,slots,profile` 必需；`baseCpu,baseMemory` 默认 0；`networkTokens` 默认空。节点能力必须由实际 inventory 生成。token 可表达例如 `trunk0:100` 的具体可用 VLAN。复杂亲和性/反亲和性尚未实现，不应宣称覆盖全部 Kubernetes 部署规则。
- Network：`id,members,weight` 必需；单成员网络代价为 0。
- `costs`：依照 nodes 数组顺序的对称 VM 间代价矩阵；对角为 0。
- `surfaces[profile]`：`cpu,memory` 为严格递增压力轴；`q` 为两轴上均非递增的网格；`margin` 为非负预测余量。必须由实测拟合，测试夹具仅供验证。

```bash
python3 -m py_compile solvePlacement.py validatePlacement.py
python3 validatePlacement.py
python3 solvePlacement.py results/synthetic_instance.json --output results/synthetic_solution.json
```

这些命令不修改 VM、集群或现有实验。编译产物提取与离线manifest/镜像清单导出已实现；仍须接入最终实测模型和实际集群验证，不能把合成CLI结果称为可直接部署的最终方案。

## 真实标定与编译特征（新增）

- `calibration.yaml`：独立实验配置。network 的 name/bridge/gateway/cidr、baseImage/storage、sshKey/sshUser、profiles 中 id/name/address/mac/cpus/memory/hostCpus，以及 pilot 的 seed/repeats/seconds/warmup/cpu/memory/modes 均为必需字段，无环境变量覆盖。CPU/内存点是 guest 能力比例。`provisionCalibration.py` 消费基础设施字段，`executeCalibration.py` 消费 SSH/profile/pilot 字段。
- `provisionCalibration.py`：创建专属 VM/network 的脚本，拒绝覆盖已有资源。`results/provisioned_profiles.json` 是已创建资源清单，磁盘/cloud-init 在配置 storage 下。此脚本不能直接重跑；先检查现有资源，无自动销毁行为。
- `measurePressure.py`：VM 内采集程序；只使用 Python 标准库，输出完整 JSONL，清理自己创建的压力进程。
- `executeCalibration.py`：主机端串行执行器，保存代码/配置哈希、进程 PID、状态与原始数据。失败不自动重试；SSH 断开需先查 guest 实际进程。
- `analyzeCalibration.py`：离线归一化、基线漂移和压力跟踪检查，输出 CSV/JSON/PNG/PDF。不完整区块不会参与归一化。
- `extractTopology.py`：从编译目录提取四特征与超图，不启动 BIRD。`results/topology214_features.json` 是真实编译数据的特征结果，含源文件哈希和矩阵秩。
- `PILOT_FINDINGS.md`：诊断观察和下一步决策；不是最终审稿结论。
- `results/pilot-20260920/`：真实首轮批次，包括冻结采集源码、输入、manifest 和每种 profile 的原始记录。
- `results/pilot-snapshot*/`：运行中快照和分析图，不能作为独立重复计数。

```bash
# 创建资源仅执行一次，已经完成；禁止不经检查再次调用
python3 provisionCalibration.py
# 本批次正在执行；恢复时先核对 manifest PID 和 guest 进程，不重复启动
python3 executeCalibration.py --output results/pilot-20260920
# 批次完成后汇总
python3 analyzeCalibration.py results/pilot-20260920 --output results/pilot-analysis
python3 extractTopology.py ../experiments/research_e1_topology214_20260918/capacity/output --output results/topology214_features.json
```

## 正式划分与路由实验（新增）

- `configs/`：冻结的三规格×三划分 YAML、split JSON、独立路由 VM 配置及路由 smoke/pilot 样本表；均为实验输入。
- `fitResponseSurface.py`：训练集单调二维最小二乘，独立校准集估计经验一侧余量，独立测试命令只评估不改模型。缺失训练格点、外推或重复使用相同数据会报错。
- `continueCalibration.py`：从真实存活的训练 PID 接续校准、拟合与保留测试；每一步输出自己的日志。不能在同一 profile 已有 pipeline 运行时再次调用。
- `measureRouting.py`：独立 guest 的 root 脚本，创建自有 target/peer netns 和 BIRD 进程，配置完整协议邻接并采集进程资源，完成后清理自有资源。输入 `configs/route-*.json` 的 seed/seconds/cases，以及每个 case 的 id/nnet/niBGP/neBGP/nOSPF/routes/repeat；均必需。无 kernel protocol、无宿主机 Docker 操作。
- `RESEARCH_BASIS.md`：已核对的原始研究链接、方法边界及统计余量解释。
- `results/formal-*/` 与 `results/pipeline-*.log`：正式实验冻结源码、输入、状态、数据和连续执行日志。
- `results/route-smoke/`、`results/route-pilot.log`：真实 BIRD 就绪与资源采样证据。路由原始完整批次稍后从专属 VM 回收。
- `results/seedemu-placement-route-0920-profiles.json`：新路由 VM 的资源清单；provision 脚本现按网络名分别记录，避免覆盖首批 VM 清单。

## 部署导出与算法验证（新增）

- `exportDeployment.py`：读取instance/solution/source三个必需输入和新output目录；重新审计Q、容量、通信目标和源网络一致性，写deployment.yaml、mapping.json、images_by_node.json、export_audit.json。仅处理单容器、单副本Pod/Deployment；不认识的affinity/spread/init/sidecar条件会拒绝。保留源资源限额，资源request上取整后再次核对总容量。仅离线导出，不调用kubectl。
- `validateExport.py`：8个独立合同验证；输出 `results/export_validation.log`。`results/synthetic_export/` 和 synthetic_source.yaml 是带example.invalid镜像的测试产物，禁止当作实际部署输入。
- `benchmarkPlacement.py` / `analyzeBenchmarks.py`：合成消融和阈值扫描脚本、离线汇总脚本。生成JSONL/CSV/PNG/PDF，明确标注非平台实测。
- `validateIncremental.py`：增量目标与全量目标、局部可行性与全量审计的独立对照。
- `ALGORITHM_FINDINGS.md`：算法实验的结果与限制。旧版本源码/验证快照位于results，用于追溯加速前后差异。

```bash
python3 validateExport.py
python3 validateIncremental.py
python3 exportDeployment.py --instance results/synthetic_instance.json --solution results/synthetic_solution.json --source results/synthetic_source.yaml --output results/synthetic_export_new
python3 analyzeBenchmarks.py results/algorithm_ablations.jsonl --output results/algorithm-ablation-analysis
```

导出器已完成离线检查；真实Kubernetes部署与全模型独立验证仍未完成。详细进度和真实句柄见工作日志。

## 路由需求与校准异常（新增）

- `analyzeRouting.py`：验证保存的协议/邻接/RIB证据，拟合非负四特征资源模型，按整种特征组合交叉验证；识别process与container两种采样口径，不混用。
- `measurePodRouting.py`：独立路由VM中的完整SEED镜像容器cgroup采样器；命令参数config/output/image必需。case沿用路由字段，`sharedBgpNetwork`可选，默认false；true支持同一接口上的多个唯一地址对，最多126会话。全部自有资源会清理。
- `evaluatePodDemand.py`：冻结模型的独立测试评估，检查镜像/数据身份，报告凸包内外误差和经验预算不足次数，不重新拟合。
- `continuePodCalibration.py`：等待已有训练的真实SSH PID，回收数据→冻结模型→执行独立测试；不重启丢失的训练。
- `ROUTING_FINDINGS.md`、`CALIBRATION_FAILURE.md`：真实路由结果、配置失败与2核性能校准反例。
- `results/seed-router-image.tar`：从既有集群缓存只读导出的205MiB镜像档案，供新路由VM使用；不是新发布的镜像。
- `results/route-pilot*/`、`results/pod-route-smoke*`、`results/pod-route-train.log`：原始资源测量、协议证据、失败及修复日志。
- `configs/c2m2-lowload-diagnostic.yaml`：重现有效低负载反例的诊断输入，字段与calibration配置相同；结果不冒充最终测试。

正式性能画像目前采用upper_corner分段查表，合成测试兼容bilinear。两个模式都拒绝真实域外查询，仅对浮点加和到网格边界的误差容忍1e-12。API中的qLower是经验校准值，不是无条件概率保证。

`pilot.warmupMode`为可选实验字段，由`executeCalibration.py`透传、`measurePressure.py`消费：默认`pressure_only`只预热后台压力，`full_workload`同时执行固定canary预热。正式模型会校验预热模式、预热秒数和采样秒数一致。旧冻结模型缺少显式协议字段时，只能从原始数据哈希完全匹配的归档训练恢复身份，模型文件本身不改写。新增低负载/预热诊断批次用于检验顺序效应，角色及限制见`CALIBRATION_FAILURE.md`。

冻结模型评估同时报告逐点错误与按随机化区块汇总的违约次数，以及最坏预测误差/下界缺口。区块内多个点相关，不能将其逐点总数当作独立部署次数；当前5区块也不构成95%安全保证。

`configs/deploymentCluster.yaml`是实际部署验证的独立KVM/K3s输入，含1个控制节点和c2m2/c4m2/c4m4各1个工作节点；使用独立10.97.0.0/24管理网络和一个macvlan VLAN trunk。`results/deployment-cluster/`存放构建日志、生成配置、inventory和kubeconfig（凭据不在报告中引用）。该集群用于K3s基础开销与画像迁移验证，不能直接假定纯VM标定曲线适用。

- `collectNodeBaseline.py`：guest只读CPU/内存/cgroup采样器，参数`--seconds`默认180、`--interval`默认1、`--output`必需；不注入负载，不更改服务。`MemTotal-MemAvailable`仅作为压力代理，不能当作Pod工作集之和。
- `executeNodeBaseline.py`：并行采集独立部署worker，保存采集源码、配置、SSH PID和原始数据；运行前需确认新集群Ready且无仿真负载。
- `continueK3sCalibration.py`：固定执行空闲基线→三规格迁移诊断；使用独占输出文件，失败后不重启实验。
- `configs/*-k3s-transfer.yaml`：迁移诊断输入，5区块×CPU={0,.1,.35,.45,.55}×Memory={0,.3}，固定resident、2秒pressure-only预热/10秒采样；不能直接用于创建VM，也不计入原最终保留测试。
- `analyzeWarmupDiagnostic.py`：完成批次的预热对照汇总，验证点集与顺序一致，生成JSON/PNG/PDF；不拟合曲面、不作因果保证。

`analyzeNodeBaseline.py`将完成的空闲基线归档转为CSV/JSON/时间序列图，只报告观测分位数，不自动把它选作安全资源预留。`mirrorDeploymentImages.py`仅在自有部署master上运行，将经过哈希校验的214镜像重新标记并推送到新私有registry，逐一核对config/layer digest；不会部署Pod。`continueK3sCalibration.py --suffix <批次标识>`用于环境修复后的全新基线/诊断，既有失败批次仍保留，不覆盖、不续写。

- `verifyImageContexts.py`：从OCI归档内存读取每个镜像最终层，与实际编译目录所有COPY文件比较SHA256；不解包到文件系统。实际214镜像/1712文件全部匹配，结果位于`results/deployment-cluster/image-context-audit.json`。
- `prepareDeploymentSource.py`：根据registry/context两份成功审计，将原manifest切换为专属namespace和固定镜像digest，并验证其他全部字段保留。`results/deployment-source/source.yaml`仍保留旧nodeSelector，必须经过最终求解/导出，禁止直接apply。

响应面评估的`blockBootstrap`使用固定种子20260920、10000次整区块重采样，报告MAE的95%百分位区间。该区间只描述当前设计和观测区块的误差变化；少量时间相关区块不等于独立部署安全保证。

`verifyTopologyProtocols.py`是实际部署后的只读全量协议审计器，参数features/inventory/kubeconfig/output必需，namespace默认seedemu-placement0920；重用B62的load门槛与BIRD解析，所有214个工作负载Ready且期望协议/邻接齐全才通过。它不启动BIRD、不写FIB，也不能替代每AS代表的FIB检查。

`evaluateTransfer.py`仅对已完成K3s诊断与冻结裸VM曲面作比较，显式记录两环境容量比、canary/协议身份、有效点覆盖与误差门槛；允许并报告跨环境容量差异，不伪装成原规格同环境保留测试，也不修改模型或恢复原本无安全区的画像。

`plotTransferEvidence.py`生成三规格迁移观测与冻结下界的图；`TRANSFER_FINDINGS.md`解释诊断结果及其不能证明的范围。`preloadDeploymentImages.py`只在独立worker上通过CRI按digest拉取实际镜像，保存逐镜像日志并验证repoDigests；必须在压力采样结束后运行，不启动仿真Pod。

- `prepareResourceDiagnostic.py` / `deployRoutingDiagnostic.py`：为实际214拓扑构造明确标记为未经过Q准入的资源诊断映射，并按12个Deployment一批部署、等待Available及核对实际nodeName；仅操作独立集群和专属namespace。不是最终优化导出器。
- `executeRoutingDiagnostic.py`：依次采集配置VLAN后的空节点基线、部署诊断、启动BIRD、固定180秒收敛等待、固定AS协议检查、全量协议检查和全AS FIB检查；阶段失败即停，记录真实进程句柄与退出码。
- `collectRoutingResources.py` / `executeRoutingMeasurement.py`：协议/FIB通过后并行采集完整拓扑的容器、Pod父cgroup、kubepods及VM资源；严格绑定Pod UID/容器ID，层级计数不能相加，采样后重查协议。该数据用于平台开销诊断，不是Q安全放置的证明。

## 2026-09-20 22:36 实验证据更新

- 原c4m2独立测试270点：MAE=.0730，P95绝对误差=.5268，原精度验收未通过。结果和原冻结模型保留。
- 容器资源90训练/50独立测试完成；真实214-Pod全协议与全部3AS代表FIB通过，180秒资源窗口揭示明显的节点管理开销。细节见[ROUTING_FINDINGS.md](ROUTING_FINDINGS.md)。
- 36/84/94新分布正在独立验证已冻结的节点开销修正；真正默认Kubernetes调度器的对照已离线准备并排队，未用自定义均衡映射冒充默认调度。
- 低压力包络新校准及独立测试、c4m2中心预测/接纳下界分离的追加训练在运行。解释与边界见[MODEL_SCOPE.md](MODEL_SCOPE.md)；完整状态和精确PID在`results/*stages.json`、批次manifest及[codex_worklog.md](codex_worklog.md)。
- 失败证据总图：`results/transfer-limits-figures/transfer_limits.pdf`。图中各节点和Pod不视为独立部署重复。

面向审稿人的连贯说明、证据表与尚未通过的门槛见[REVIEW_REPORT.md](REVIEW_REPORT.md)。该报告当前是工作稿，不把进行中的独立测试当作结果。


### 实际worker画像集成与仪器审计

- `measureNodePressure.py`：目标K3s worker的新压力采集器；固定canary不变，累计CPU债务控制。`collectorSource`为executeCalibration.py可选输入（默认measurePressure.py）；新配置指定measureNodePressure.py。`pilot.memoryReserveFraction`可选，默认.2，新协议.15且至少64MiB；`pressureControl`和`budgetTrackingTolerance`记录协议身份及CPU跟踪门槛，由fitResponseSurface.py读取；`streamChunkBytes=65536`为与归档源码匹配的元数据，不是运行时调参开关。
- `auditPressureBudget.py --raw <raw.jsonl> --output <new.json>`：保留旧数据的实际CPU/设定CPU诊断，不改原冻结指标。生成输出及各模式统计。
- `buildNodePlacement.py --root results --output results/node-placement-instance`：在三worker独立测试全部完成后，核验冻结模型/原始split哈希及跟踪完整性，生成instance/provenance/cardinality_bounds.json。模型先将Pod预算向上取整至1m CPU/1MiB，与导出预留一致；后续所有对照必须用相同requests，不能将旧零requests默认实验当成完全匹配对照。未通过画像精度/安全点检查会明确标为profileAcceptancePassed=false，不能据此宣称安全部署。

此集成仍需新部署前的空背景、slots、CNI检查和真实路由工作负载验证。输出是可审计候选模型，独立画像测试通过不等于部署层概率保证；旧版buildDeploymentDiagnostic.py保留为历史裸VM迁移诊断，不删除。


`buildNodePlacement.py`另接受可选`--extra-budgets budgets.yaml`，文件直接映射精确Pod ID到`{cpu: 1, memory: 2}`（核/GiB）。未列出的Pod额外预算为0；负数、非有限数、未知Pod及任务类型字段均拒绝。额外预算按PDF式(8)–(9)加到路由及平台需求后统一取整，配置原文件哈希写入provenance。该参数只声明规划预算，不替用户创建未知程序或证明其实际用量受限。


`compareNodePlacements.py --instance results/node-placement-instance/instance.json --output results/node-placement-comparison --starts 3 --seeds 11 29 47`在同一冻结实例上比较资源优先（按最大归一化需求排序，选择负载最低硬可行节点）、network-only（取消Q约束）和完整方法，并扫描六个预定η。资源优先基线不使用通信成本决定落点，与算法内部按资源排序再最小化通信的初始解不同。所有结果保留完整映射、模型Q审计、源实例hash和拒绝/不可行证据；未执行任何部署。资源优先与network-only明确标为Q未接纳，不能经安全导出器当成完整方案发布。小规模接口smoke在results/comparison-interface-smoke-v2，仍是合成证据。


`preparePlacementComparison.py --instance <instance.json> --source results/deployment-source/source.yaml --mode <full|resource_first|network_only|default> --solution <comparison.json> --output <new-directory>`离线生成保持镜像/命令/环境及相同requests的实际对照清单；default不传solution，由Kubernetes决定映射。完整方法要求独立画像验收通过，且复用exportDeployment的全部Q/容量/目标审计；两个基线只检查硬约束，明确不声明Q接纳。所有清单仍带实验诊断所有权标识，`modelAdmitted`单独记录数学模型接纳，`qualityGuarantee=false`直到物理转移得到验证。该入口只生成文件，不部署、不启动额外用户程序。


后续匹配请求的真实对照使用`configs/node-{c2m2,c4m2,c4m4}-routing-canary.yaml`，与目标画像一致的累计控制器/15%内存保护，五块共15个零注入窗口，部署前/中/后保持同一配置。旧对照配置不改。`analyzeRoutingCanary.py --eta <threshold>`默认.8；改变η只改变违约计数，不改变Q归一化，前后窗口还核对控制器和保护比例身份。各η结果必须按所属部署块报告，不能把同一组Q重复评价六个阈值当成六次独立部署。


`refreshPlacementBackground.py`为部署前离线复核库：读取完整15点空canary窗口和新节点/Pod快照，核对VM/测量协议身份，更新背景，并按系统占用与Kubernetes剩余requests/slots收紧有效容量。常驻容器请求总和与非重启初始化容器最大请求取最大值，再加Pod overhead；遇到restartable init明确拒绝。冻结Q曲线只换坐标、不重拟合，后续必须重新auditMapping，不能沿用旧“可行”状态。一次库检查不等于已测新空背景。

`plotResponseValidation.py --evaluation <evaluation.json> --budget-audit <audit.json> --output <new-directory>`生成可导出PDF/PNG及输入hash；检查两份报告属于同一raw批次，分别展示中心预测、误差分布和CPU跟踪，保留全部评估点。当前实际完成示例在results/c4m2-dual-validation-figures。


`executePlacementTrial.py --prepared <prepared-directory> --instance <instance.json> --output <new-trial-directory>`执行单次真实214-Pod对照，会在自有10.97.0.10集群创建并最终删除经过身份核对的seedemu-placement0920 namespace。目标节点画像流水线未完成时立即拒绝；输入/清单hash固定，前置空闲180秒、空节点资源180秒、空canary15点，新背景/容量重新审计。随后分批部署、启动BIRD、固定180秒收敛、214全协议/各AS FIB、实际资源180秒、负载canary15点；采后协议与UID/容器ID/重启计数核对后删除自有namespace，冷却180秒再取空参考。输出完整阶段PID/日志、请求一致映射、资源、各节点Q和实际通信切分成本。任一阶段失败不自动重启或删除现场；代码检查通过不等于真实流程已完成。控制器目前只准备，未在画像标定期间运行。

单次试验另输出`resource-budget-audit.json`：计划节点CPU/内存预算对比实测增量，保留带正负号残差。内存观测为不可用内存代理，不称作容器RSS；complete.json分别标记资源预算超出和硬约束满足情况。实际超出不能通过只看Q良好来掩盖，也不把经验预算称作认证上界。

模型集成对守门停止规格明确记录`testStatus=not_run_guard_stopped`、测试指标null及验收false，不要求伪造未执行的测试；对仍应执行但尚未完成的测试继续拒绝集成。不可行性审计另识别任何必需VM在所有支持负载下均无法满足Q的情况，即使其余VM的乐观Pod总数足够也仍不可行。此证书仅约束冻结经验实例。

`analyzeResponseAmbiguity.py`支持互斥的`--evaluation`或`--raw`输入；raw路径经过原完整性/预算跟踪检查。输出各相同声明预算组的Q范围、事后最优L1误差与最小可能最大绝对误差（范围的一半），并明确标注有限样本诊断，不将校准数据冒充独立测试。

`refineResponseEnvelope.py --model <uncalibrated-training-model> --raw <failed-calibration-raw> --output <new-training-model>`将失败校准明确提升为下包络训练，加入观测坐标并仅降低单调上方区域，中心不改。原失败模型不覆盖；新模型必须重新独立校准/测试。`continueRefinedResponse.py`等待精确c4m2复现SSH及父级数据归档完成，运行预先冻结的refined配置；子阶段执行归档源码，失败不重试。对应部署控制器在这条验证链完成前同样拒绝启动。

### 独立尾延迟复现与周期诊断（2026-09-21）

`results/node-c4m2-tail-reproduction/analyze.py` 是完成批次的只读分析脚本；从项目根目录执行 `PYTHONPATH=. python3 results/node-c4m2-tail-reproduction/analyze.py`，输出同目录 `summary.json`，保留原校准和新诊断逐组结果、预算检查与哈希。该诊断不进入修订模型训练。

`measureBurstPressure.py` 为隔离的实验采集器，固定 canary 与原采集器相同，仅将 resident 注入器周期设为 5ms/50ms。`configs/c4m2-burst-diagnostic.yaml` 是用户输入配置，由 `executeCalibration.py` 读取：必需 `collectorSource` 指向新采集器，`pilot.modes` 为 `resident_5ms`/`resident_50ms`，其余 CPU、内存、重复块、预热、采样、预算公差字段沿用现有协议；可选 `scopeNote` 仅记录解释，无运行默认值。执行命令 `python3 executeCalibration.py --config configs/c4m2-burst-diagnostic.yaml --output results/c4m2-burst-diagnostic` 已启动，不应重复执行。结果目录为原始数据、配置、源码和进程清单等生成物。此实验仅占用裸 c4m2 VM，不修改 K3s 集群；控制平均 CPU 和分配内存，不声称限制瞬时 CPU 峰值，周期也不作为模型输入。

### 显式选择响应模型证据

`buildNodePlacement.py --evidence-sources configs/refined-node-evidence.yaml --output results/node-refined-placement` 可选择修订模型，默认 `--root results`；此命令需等完整新校准与测试结束，当前未执行正式组装。省略 `--evidence-sources` 保留原 v3 来源。YAML 必须包含 c2m2/c4m2/c4m4，每节点必需字段 `model`、`evaluation`、`training`（按模型训练审计顺序排列的非空原始文件列表）、`calibration`、`test`，全部路径相对 `--root`。不允许隐式覆盖部分节点。生成的 provenance 记录配置哈希、各来源路径及全部训练哈希，并检查训练/校准/测试不重用同一原始数据、容量和测量协议一致。修订 c4m2 的旧校准仅列为追加训练，新的校准/测试有各自独立路径；未完成测试时拒绝生成实例。

`results/node-c4m2-tail-reproduction/plot.py` 为该80点诊断的可复现绘图脚本，使用 `PYTHONPATH=. python3 results/node-c4m2-tail-reproduction/plot.py` 生成同目录 reproduction.pdf/png 与 plot-provenance.json。所有点均显示，数据仍只作追加诊断。

`analyzeBurstDiagnostic.py --raw results/c4m2-burst-diagnostic/c4m2/raw.jsonl --output <new-report.json>` 是已完成周期实验的分析入口：严格检查完整点数、CPU跟踪、子进程周期及成对分配内存，输出逐块配对Q与分组描述结果，输出必须新建。生成物`results/c4m2-burst-diagnostic-paired.json`不用于模型拟合。

### 同预算真实对照清单（待画像采样结束后执行）

`results/node-v3-prepared-{default,network_only,resource_first}/`为preparePlacementComparison生成的三组完整214工作负载清单，含deployment.yaml、diagnostic.json和固定映射（默认调度不指定映射）。`results/node-v3-matched-request-audit.json`记录逐文档比较和哈希：除放置字段及方法注解外完全一致，总请求.433核/2.306640625GiB。

`results/node-v3-comparison-round1-plan.json`为采样前固定的首轮次序和输入哈希，依次default、network_only、resource_first。每次用`python3 executePlacementTrial.py --prepared results/node-v3-prepared-<method> --instance results/node-v3-placement/instance.json --output results/node-v3-round1-<method>`执行；该首轮现已完成，勿向已有输出目录再次执行；程序也拒绝打扰未完成的画像链。该命令会在自有集群创建214工作负载、启动BIRD并校验协议/FIB、测量资源与Q，身份检查通过后清理该次自有namespace。任何失败保留现场，不自动重试。一轮三个方法各一次，不冒充独立部署风险保证或充分重复的总体性能结论。

`summarizeNodeEvidence.py --output <new-directory>`从完成的目标worker模型和独立测试生成evidence.json/md快照（可选--root默认results），重查原始数据完整性/跟踪和模型/测试hash及数据独立性；缺少修订结果明确标pending，不补造成功。`results/node-evidence-snapshot-refined-test/`包含已完成的修订画像独立测试。快照不读取运行中的部分测试Q，也不替代真实部署证明。

真实对照的stages.json同时记录各canary阶段和三个worker子进程的PID/命令/退出码；子进程失败保留对应日志并终止后续阶段。sources.json覆盖直接使用的采集、分析和模型源码，批次目录另保留各自采集配置与源码。

主研究叙述见REVIEW_REPORT.md；重整前的完整时间线保存在REVIEW_HISTORY_BEFORE_CONSOLIDATION.md。历史“进行中”状态按当时记录理解，当前状态以主报告及最新证据快照为准。

### 真实对照统一分析

`analyzePlacementComparisons.py --plan results/node-v3-comparison-round1-plan.json --trials results/node-v3-round1-default results/node-v3-round1-network_only results/node-v3-round1-resource_first --output <new-directory>`按计划顺序检查每个方法的实际部署。完成批次重新核对manifest/实例hash、214映射和容器身份、全部必要阶段，从原始前/负载/后窗口重新计算Q，并重新计算通信代价及资源预算残差。未完成批次保留incomplete与最后阶段、Q未知；重复目录拒绝。生成summary.json/md，每个方法每轮仅一次部署，不能据三个节点或15窗口扩大样本数，不宣称风险认证。完整路径已在首轮三个实际部署上从原始数据重算通过，结果见`results/node-v3-round1-analysis/`。

### 首轮对照的自动衔接

`continuePlacementComparison.py --plan results/node-v3-comparison-round1-plan.json --wait-pid 3321287`为旧首轮队列入口，现已因空worker仪器缺陷停止并通过显式恢复完成后续工作（不要再次启动）。它等待既有continueRefinedResponse协调进程，以命令和内核启动tick识别PID，只有明确终态才按冻结顺序执行executePlacementTrial，最后运行统一分析。观察到短暂状态文件写入仅重读，不重启实验。每步开始前检查归档的本地Python源码、配置、模型与清单hash；发生修改或子步骤失败则停止并保留现场。状态为results/node-v3-round1-stages.json，源码/输入归档在results/node-v3-round1-sources，日志为results/pipeline-node-v3-round1.log和每方法日志。真实副作用与executePlacementTrial一致；该等待已结束；此处保留原队列机制说明。

### 空worker采集修复与明确恢复

网络优先合法使用106/108/0布局，原collectRoutingResources对空worker报错。现在空Pod列表仍采整节点/系统cgroup，非空列表但缺失目标仍拒绝。原失败批次和队列完整保留。`resumeComparisonRound.py --plan results/node-v3-comparison-round1-plan.json`为首轮显式恢复入口（已结束，不要再次启动）；其仅允许已记录的三个源码改动，记录前后hash，先调用resumePlacementTrial在同一214容器上复查身份和协议，再用独立目录采新资源窗口并继续原流程，之后按原序执行resource_first和汇总。

恢复状态：results/node-v3-round1-recovery-stages.json；源码：results/node-v3-round1-recovery-sources；每trial的instrumentation-recovery.json与stages-before-resource-recovery.json保留失败原因/阶段及恢复证据。executePlacementTrial共享createTrialSteps/completeTrial，后半流程与原归档AST相同（资源目录参数除外），canary/cooldown AST相同。统一分析器仅允许这一明确解析的失败被后续成功同阶段解决，并标注uninterruptedProtocol=false；不能把中断的网络对照说成未中断预设实验。原自动队列已因真实采集失败停止，未自动重试或重新部署。

恢复的首次代码提取遗漏kubeconfig已保留为检查点失败；后续完成首轮的队列为 `resumeComparisonRound.py --plan results/node-v3-comparison-round1-plan.json --continue-after-checkpoint-fix`（已全部完成，不要重复）。状态/日志使用node-v3-round1-recovery2前缀，trial内checkpointFixAttempt保留第一恢复错误及第二次代码hash；此标记严格要求新资源窗口尚未开始，不是通用失败重试选项。

### 当前容量一致性修正与六轮重复实验

首轮结果：default成本770.667、最低Q=.912503；resource_first成本768.667、最低Q=.921633；network_only成本98.667、c4m2主Q未知且有仪器恢复。三者各一次、均非完整方法接纳，详见[主报告](REVIEW_REPORT.md)。

新实例`results/node-refined-reserve-aligned-placement/`按采集器原15%内存保护扣减容量，响应面坐标同步换算，Pod请求与η不变。新实例整体画像验收仍false、η=.8数量上界128<214；不能作为已验证安全模型。新network候选成本50也在旧约束下可行，属启发式候选改进，不是容量收紧改善最优值。

六轮设计：`results/reserve-aligned-series-design/plan.json`；活动状态：`results/reserve-aligned-series/stages.json`；冻结源码：`results/reserve-aligned-series/sources/sha256.json`。`executeComparisonSeries.py`已启动，勿重复运行。每轮三方法，六顺序各一次，总计18个计划部署；计划数量不等于已完成证据。

只读验证冻结设计：

```bash
python3 executeComparisonSeries.py --plan results/reserve-aligned-series-design/plan.json --validate-only
```

汇总到一个尚不存在的新目录（即使未完成，也会明确保留pending/未知）：

```bash
python3 results/comparison-series-tools/analyzeComparisonSeries.py --plan results/reserve-aligned-series-design/plan.json --output results/reserve-aligned-series-review-audit
```

汇总器重新读取真实原始产物，固定六配对的规则见设计和`results/comparison-series-tools/registration.json`。旧首轮不合并进新协议；缺失Q不填0，不以节点/窗口扩大独立部署数。即使六次零违约也不能达到原定单侧95%风险上界≤.10。
