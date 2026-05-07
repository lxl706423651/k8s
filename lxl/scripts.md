# Script Inventory And Cleanup Plan

## 1. Executive Summary

当前 `/home/lxl/k8s/lxl` 下的脚本体系已经从最早的 `3-node` 版本，逐步演化为：

- 一套早期通用/3-node 脚本：`preflight`、`compile`、`deploy-batched`、`wait-ready`、`env.sh`
- 一套 `9node` 版本脚本
- 一套 `12node` 版本脚本
- 一批运维、修复、监控、测量、扩容脚本

当前最大的问题不是“脚本不够多”，而是：

1. **节点列表已经是动态 inventory 驱动，但入口脚本仍然按 `9node/12node` 复制。**
2. **`12node` 版本很多只是对 `9node` 版本做字符串替换，不是真正参数化。**
3. **有一部分脚本已经被更晚的脚本覆盖，但还保留在目录里，容易误用。**

## 2. 结论：当前 `_12node` 是否已经满足“给环境和 inventory 即可运行”

**结论：还没有。**

理由：

- `01_cluster_nodes_9node.sh` 和 `01_cluster_nodes_12node.sh` 确实都已经能从 inventory 动态读取节点。
- 但大部分上层入口脚本仍然显式写死：
  - `source env_9node.sh`
  - `source env_12node.sh`
  - `source 01_cluster_nodes_9node.sh`
  - `source 01_cluster_nodes_12node.sh`
- `05_deploy-batched_12node` 和 `16_run_deploy_with_monitor_12node_bg.sh` 甚至不是独立实现，而是通过 `sed` 临时改写 `9node` 版本生成的。
- Python runtime 包装层 `_seed_runtime_12node.py` 也显式依赖 `env_12node.sh` 和 `01_cluster_nodes_12node.sh`。

所以现在的 `_12node` 脚本满足的是：

- “**12 节点能跑**”

但还没有满足：

- “**只要给一个环境文件和 inventory，就不需要再维护 `9node/12node` 两套脚本**”

### 建议的最终收敛目标

建议最终收敛成：

- 一个通用环境入口，例如：`env_cluster.sh`
- 一个通用节点加载脚本，例如：`01_cluster_nodes.sh`
- 一套不带 `9node/12node` 后缀的主流程脚本：
  - `02_preflight`
  - `03_compile`
  - `04_build`
  - `05_deploy-batched`
  - `07_seed_k8s_start_bird0130.py`
  - `08_seed_k8s_start_bird_kernel.py`
  - `09_reconvergence.py`
  - `11_run_full_flow.sh`
  - `12_run_full_flow_bg.sh`
  - `16_run_deploy_with_monitor_bg.sh`

而不是再继续复制 `*_9node` / `*_12node`。

---

## 3. Numbered Scripts (01-23)

### 01

#### `01_cluster_nodes_9node.sh`
- 功能：
  - 从 `SEED_CLUSTER_INVENTORY_PATH` 读取节点列表
  - 生成 `SEED_NODE_NAMES / SEED_NODE_IPS / SEED_WORKER_NODE_NAMES / SEED_WORKER_NODE_IPS / SEED_MASTER_NODE_*`
- 当前价值：
  - **有价值**
  - 已经是“动态 inventory”这一层的正确方向
- 问题：
  - 仍然默认 `source env_9node.sh`
- 删除建议：
  - **暂不删除**
  - 未来应与 `01_cluster_nodes_12node.sh` 合并为单个 `01_cluster_nodes.sh`

#### `01_cluster_nodes_12node.sh`
- 功能：
  - 与 `01_cluster_nodes_9node.sh` 相同，只是默认 `source env_12node.sh`
- 当前价值：
  - **有价值**
- 问题：
  - 与 `9node` 版本逻辑重复
- 删除建议：
  - **不建议立即删除**
  - 但应作为未来合并目标

### 02

#### `02_preflight_9node`
- 功能：
  - 集群健康检查
  - Multus 检查
  - registry 连通性检查
  - 节点标签/placement 文件生成
- 当前价值：
  - **有价值**
- 问题：
  - 依赖 `env_9node.sh`
- 删除建议：
  - **暂不删除**

#### `02_preflight_12node`
- 功能：
  - `12node` 版 preflight
- 当前价值：
  - **有价值**
- 问题：
  - 与 `9node` 版本基本只差环境入口
- 删除建议：
  - **不建议立即删除**
  - 未来应并入单一 `02_preflight`

### 03

#### `03_compile_9node`
- 功能：
  - 调用 `examples/kubernetes/real_topology_k3s_compile.py`
  - 生成 `k8s.yaml`、`images.txt`、`build_images.sh`
- 当前价值：
  - **有价值**
- 删除建议：
  - 暂不删除

#### `03_compile_12node`
- 功能：
  - `12node` 版 compile
- 当前价值：
  - **有价值**
- 问题：
  - 与 `9node` 几乎完全重复
- 删除建议：
  - 未来应并入单一 `03_compile`

### 04

#### `04_build_9node`
- 功能：
  - 远端 master build
  - push 到 registry
  - 按节点生成镜像清单
  - 按节点 preload 到 containerd
- 当前价值：
  - **核心脚本**
- 删除建议：
  - 不删除

#### `04_build_12node`
- 功能：
  - `12node` 版 build + preload
- 当前价值：
  - **核心脚本**
- 问题：
  - 本质仍是 `9node` 版逻辑复制
- 删除建议：
  - 暂不删除
  - 未来合并成单一 `04_build`

#### `04b_preload_only_9node`
- 功能：
  - 不 build，只根据已有 `images.txt` / `k8s.yaml` 做 per-node preload
- 当前价值：
  - **有价值**
  - 特别适合“重新 compile，但不重新 build”的工作流
- 问题：
  - 只有 `9node` 版，没有 `12node` 对应版本
- 删除建议：
  - **不建议删除**
  - 反而建议补一个通用 preload-only 版本

#### `04c_preload_selected_nodes_9node`
- 功能：
  - 仅对指定节点做补 preload
- 当前价值：
  - **有价值**
  - 适合 build 成功后部分节点补救
- 问题：
  - 只有 `9node` 版
- 删除建议：
  - **不建议删除**
  - 建议未来做成通用脚本

### 05

#### `05_deploy-batched_9node`
- 功能：
  - 当前最重要的 deploy 脚本之一
  - 负责 namespace、manifest 分类、分批提交 controllers、pressure/backpressure 控制、取证
- 当前价值：
  - **核心脚本**
- 删除建议：
  - 不删除

#### `05_deploy-batched_12node`
- 功能：
  - `12node` deploy
- 当前价值：
  - **有价值**
- 问题：
  - 不是独立实现，而是 `sed` 改写 `05_deploy-batched_9node`
  - 可维护性差，容易出路径和分类问题
- 删除建议：
  - **不建议长期保留这种形态**
  - 应重构为真正的通用脚本后再删除

### 06

#### `06_seed_k8s_plan_real_topology_by_as_9node.py`
- 功能：
  - 只是 Python wrapper
  - 真正逻辑在 `seed_k8s_plan_real_topology_by_as.py`
- 当前价值：
  - **低**
- 问题：
  - 名字带 `9node`，但逻辑本身其实并不天然绑定 9 节点
  - `03_compile_12node` 目前仍然调用它
- 删除建议：
  - **当前不能删**
  - 未来应改成通用 wrapper，例如 `06_seed_k8s_plan_real_topology_by_as.py`

### 07 / 08

#### `07_seed_k8s_start_bird0130_9node.py`
#### `07_seed_k8s_start_bird0130_12node.py`
- 功能：
  - Python runtime wrapper
  - 真正逻辑在 `seed_k8s_start_bird0130.py`
- 当前价值：
  - **有价值**
- 问题：
  - 只是 runtime env 包装，不该长期维护两套
- 删除建议：
  - 未来保留一个通用 wrapper 即可

#### `08_seed_k8s_start_bird_kernel_9node.py`
#### `08_seed_k8s_start_bird_kernel_12node.py`
- 功能：
  - Python runtime wrapper
  - 真正逻辑在 `seed_k8s_start_bird_kernel.py`
- 当前价值：
  - **有价值**
- 删除建议：
  - 同上，未来保留一个通用 wrapper 即可

### 09

#### `09_reconvegence.py`
- 功能：
  - reconvergence/chaos 验证主逻辑
- 当前价值：
  - **有价值**
- 问题：
  - 文件名拼写有误：`reconvegence`
- 删除建议：
  - 保留，但建议未来重命名

#### `09_reconvegence_12node.py`
- 功能：
  - 12 节点 wrapper，导入 `09_reconvegence.py`
- 当前价值：
  - **中等**
- 删除建议：
  - 未来可被通用 wrapper 替代

### 10

#### `10_optimize_k3s_network_9node.sh`
- 功能：
  - 对已有 9 节点做网络/内核调优补丁
- 当前价值：
  - **有价值**
- 问题：
  - 只适用于当前 9 节点环境命名
- 删除建议：
  - 暂不删除
  - 未来可通用化

### 11 / 12

#### `11_run_full_flow_9node.sh`
#### `11_run_full_flow_12node.sh`
- 功能：
  - 全流程串行入口
  - build -> deploy -> wait-ready -> bird -> kernel -> reconvergence
- 当前价值：
  - **核心入口**
- 问题：
  - 仍然分叉维护
  - 还夹杂了“是否注释掉 compile”的人工状态
- 删除建议：
  - 不删除
  - 未来应该收敛成一个通用 `11_run_full_flow.sh`

#### `12_run_full_flow_9node_bg.sh`
#### `12_run_full_flow_12node_bg.sh`
- 功能：
  - 后台 `nohup` 启动全流程
- 当前价值：
  - **有价值**
- 问题：
  - 依赖对应的 `11_*`
- 删除建议：
  - 未来与 `11` 一起收敛

### 13

#### `13_measure_cluster_memory_9node.sh`
- 功能：
  - 早期内存测量脚本
  - 统计 guest used / qemu rss
- 当前价值：
  - **仍有参考价值**
- 问题：
  - 已被更后续的 `20/22/23` 脚本覆盖
- 删除建议：
  - **建议归档，不建议作为主脚本继续使用**

### 14

#### `14_repair_master_docker_registry.sh`
- 功能：
  - 集成修复 master Docker / registry
  - 部分替代 `init_master_docker.sh + ensure_master_registry.sh`
- 当前价值：
  - **有价值**
- 删除建议：
  - 保留
  - 未来可作为 master 修复主脚本

### 15

#### `15_recover_stuck_deploy_9node.sh`
- 功能：
  - 恢复卡死 deploy 的现场
  - reboot/destroy-start VM
  - 清 `state.db`
  - 拉回集群 Ready
- 当前价值：
  - **非常有价值**
- 问题：
  - 名字写成 `9node`，但其中一部分逻辑可泛化
- 删除建议：
  - 不删除

### 16

#### `16_run_deploy_with_monitor_9node_bg.sh`
- 功能：
  - 后台 deploy + 故障现场监控
- 当前价值：
  - **非常有价值**
- 删除建议：
  - 不删除

#### `16_run_deploy_with_monitor_12node_bg.sh`
- 功能：
  - 12 节点监控版 deploy wrapper
- 当前价值：
  - **有价值**
- 问题：
  - 通过 `sed` 改写 `9node` 版生成
- 删除建议：
  - 不建议长期保留这种实现

### 17 / 18

#### `17_add_k3s_workers_3.sh`
- 功能：
  - 在已有集群上增加 3 个 worker（从 9 到 12）
- 当前价值：
  - **一次性迁移脚本**
- 删除建议：
  - **建议保留到确认 12 节点稳定后归档**

#### `18_resize_k3s_workers_24cpu_48g.sh`
- 功能：
  - 将所有 worker 调整到 `24 CPU / 48 GiB`
- 当前价值：
  - **一次性迁移/运维脚本**
- 删除建议：
  - **建议保留，但不属于日常主流程**

### 20

#### `20_measure_memory_breakdown_12node.sh`
- 功能：
  - 早期 guest 内多指标内存分解实验脚本
- 当前价值：
  - **调研脚本**
- 问题：
  - 已经验证出“不能严格做加法分解”
  - 不适合直接作为论文最终测量口径
- 删除建议：
  - **不建议作为主脚本**
  - 建议保留作 debug / 方法探索

### 21

- 当前不存在。

### 22

#### `22_measure_memory_footprint_k8s_12node.sh`
- 功能：
  - K8s 多 VM 版本的论文导向内存测量脚本
  - 采用“主总量 + 解释性指标”口径
- 当前价值：
  - **高**
- 删除建议：
  - 保留

### 23

#### `23_measure_memory_footprint_docker_host.sh`
- 功能：
  - 单机 Docker 版本的论文导向内存测量脚本
- 当前价值：
  - **高**
- 删除建议：
  - 保留

---

## 4. Non-numbered Core Scripts

### 推荐保留（仍有明确职责）

#### `clean`
- 功能：
  - 清理 namespace、residual resources、stuck finalizer
- 结论：
  - 保留

#### `wait-ready`
- 功能：
  - 等待 namespace 内 pod 达到 ready
- 结论：
  - 保留，但它当前仍然依赖 `env.sh`
  - 最终应收敛成真正通用版

#### `wait-ready_12node`
- 功能：
  - 12 节点 wait-ready
- 结论：
  - 暂时保留
  - 未来应与 `wait-ready` 合并

#### `_seed_runtime_9node.py` / `_seed_runtime_12node.py`
- 功能：
  - Python wrapper 的 runtime env 注入
- 结论：
  - 有价值
  - 但未来应合并成单一 `_seed_runtime.py`

#### `seed_k8s_plan_real_topology_by_as.py`
- 功能：
  - 节点标签 / placement 规划核心逻辑
- 结论：
  - 保留

#### `seed_k8s_start_bird0130.py`
- 功能：
  - BIRD 启动核心逻辑
- 结论：
  - 保留

#### `seed_k8s_start_bird_kernel.py`
- 功能：
  - BIRD kernel 切换核心逻辑
- 结论：
  - 保留

#### `generate_node_image_refs.py`
- 功能：
  - 从 `k8s.yaml` 生成每节点镜像清单
- 结论：
  - 保留

#### `ensure_master_registry.sh`
- 功能：
  - 轻量修复 master registry
- 结论：
  - 保留

#### `init_master_docker.sh`
- 功能：
  - 重置 master Docker，并重建 registry/base 镜像
- 结论：
  - 保留，但属于**重置级操作**
  - 不应频繁运行

#### `repair_k3s_state_db_seedemu_namespace.py`
- 功能：
  - 直接清理 `state.db` 中残留 namespace 键
- 结论：
  - 保留，属于强修复工具

### 推荐保留但属于“专项工具”

#### `add_k3s_workers_6.sh`
- 功能：
  - 历史扩容脚本
- 结论：
  - 保留作参考
  - 当前已被 `17_add_k3s_workers_3.sh` 补充/部分替代

#### `rebuild_k3s.sh`
- 功能：
  - 3 节点 K3s 重建
- 结论：
  - 保留作灾难恢复脚本
  - 非日常主流程

#### `resize_kvm_cluster_resources.sh`
- 功能：
  - 旧的资源调整脚本（3 节点导向）
- 结论：
  - 保留作参考
  - 已部分被 `18_resize_k3s_workers_24cpu_48g.sh` 覆盖

#### `seed_k8s_ultimate_all.sh`
- 功能：
  - 早期“终极调优”总脚本
- 结论：
  - 更像一次性 provisioning payload
  - 保留作参考，不建议作为日常运维入口

#### `14_repair_master_docker_registry.sh`
- 功能：
  - 集成 Docker/registry 修复
- 结论：
  - 推荐保留

### 可归档或明显偏旧的脚本

#### `compile`
#### `preflight`
#### `deploy-batched`
#### `deploy`
- 功能：
  - 旧的通用/3 节点脚本
- 问题：
  - 依赖 `env.sh`
  - 默认为 3 节点语义
  - 已不代表当前主流程
- 删除建议：
  - **不建议现在直接删除**
  - 但建议标记为 `legacy`

#### `check_memory.sh`
- 功能：
  - 3 节点简单内存查看
- 结论：
  - 已被 `13/20/22/23` 覆盖
  - 可归档

#### `check_resources`
- 功能：
  - 3 节点资源检查
- 结论：
  - 可归档

#### `fix_k3s_system_images.sh`
#### `fix_k3s_system_images_k3sctr.sh`
#### `fix_k3s_worker111_multus.sh`
#### `check_multus_110_111_112.sh`
#### `ensure_multus_bridge_9node.sh`
- 功能：
  - 针对特定历史故障的专项补丁
- 结论：
  - 保留作历史修复工具
  - 不属于主流程

#### `monitor_registry_preload.sh`
- 功能：
  - preload / registry 监控
- 结论：
  - 有价值，但偏专项

#### `update-podcidr.sh`
- 功能：
  - 修改 podCIDR / K3s 配置
- 结论：
  - 保留作专项脚本

#### `get_mapping.sh`
#### `list_routers_by_as.sh`
#### `test_last_pod_ping_by_as.sh`
#### `check_bird_status.py`
- 功能：
  - 辅助分析 / 诊断 / 导出
- 结论：
  - 保留

---

## 5. Which Scripts Are Already Covered By Others

### 已被更晚脚本覆盖或部分覆盖

- `13_measure_cluster_memory_9node.sh`
  - 被 `20_measure_memory_breakdown_12node.sh` 以及最终的 `22/23` 覆盖

- `20_measure_memory_breakdown_12node.sh`
  - 已被 `22_measure_memory_footprint_k8s_12node.sh` 作为论文主方法覆盖
  - `20` 可作为 debug 版保留

- `init_master_docker.sh + ensure_master_registry.sh`
  - 在“修复流程整合”上被 `14_repair_master_docker_registry.sh` 部分覆盖
  - 但由于用户当前仍直接使用前两者，暂不建议删除

- `resize_kvm_cluster_resources.sh`
  - 在当前 12 节点场景下，被 `18_resize_k3s_workers_24cpu_48g.sh` 部分覆盖

- `add_k3s_workers_6.sh`
  - 在当前从 9 到 12 的操作上，被 `17_add_k3s_workers_3.sh` 部分覆盖

---

## 6. Deletion Recommendations

## 6.1 不建议现在删除

以下脚本仍然有现实用途，或者仍被其他脚本依赖：

- `01_cluster_nodes_9node.sh`
- `01_cluster_nodes_12node.sh`
- `02_preflight_9node`
- `02_preflight_12node`
- `03_compile_9node`
- `03_compile_12node`
- `04_build_9node`
- `04_build_12node`
- `04b_preload_only_9node`
- `04c_preload_selected_nodes_9node`
- `05_deploy-batched_9node`
- `05_deploy-batched_12node`
- `06_seed_k8s_plan_real_topology_by_as_9node.py`
- `07_*`
- `08_*`
- `09_*`
- `10_optimize_k3s_network_9node.sh`
- `11_*`
- `12_*`
- `14_repair_master_docker_registry.sh`
- `15_recover_stuck_deploy_9node.sh`
- `16_*`
- `17_add_k3s_workers_3.sh`
- `18_resize_k3s_workers_24cpu_48g.sh`
- `22_measure_memory_footprint_k8s_12node.sh`
- `23_measure_memory_footprint_docker_host.sh`
- `clean`
- `wait-ready`
- `wait-ready_12node`
- `seed_k8s_plan_real_topology_by_as.py`
- `seed_k8s_start_bird0130.py`
- `seed_k8s_start_bird_kernel.py`
- `generate_node_image_refs.py`
- `ensure_master_registry.sh`
- `init_master_docker.sh`
- `repair_k3s_state_db_seedemu_namespace.py`

## 6.2 可以考虑归档（不建议直接物理删除）

建议先移动到 `legacy/` 或 `archive/`，而不是直接删除：

- `compile`
- `preflight`
- `deploy`
- `deploy-batched`
- `check_memory.sh`
- `check_resources`
- `13_measure_cluster_memory_9node.sh`
- `20_measure_memory_breakdown_12node.sh`
- `fix_k3s_system_images.sh`
- `fix_k3s_system_images_k3sctr.sh`
- `fix_k3s_worker111_multus.sh`
- `check_multus_110_111_112.sh`
- `ensure_multus_bridge_9node.sh`

理由：
- 它们对当前主流程不是最优入口
- 但仍可能在故障恢复或历史复现中有价值

---

## 7. Recommended Refactor Plan

### Phase 1: 收敛环境入口

新增：

- `env_cluster.sh`
- `01_cluster_nodes.sh`

要求：

- 只依赖：
  - `SEED_CLUSTER_INVENTORY_PATH`
  - `SEED_K3S_CLUSTER_NAME`
  - 其余拓扑/registry/build/deploy 参数
- 不再出现 `env_9node.sh` / `env_12node.sh` 写死引用

### Phase 2: 收敛主流程脚本

把这些脚本收敛成无后缀版本：

- `02_preflight`
- `03_compile`
- `04_build`
- `04b_preload_only`
- `04c_preload_selected_nodes`
- `05_deploy-batched`
- `07_seed_k8s_start_bird0130.py`
- `08_seed_k8s_start_bird_kernel.py`
- `09_reconvergence.py`
- `11_run_full_flow.sh`
- `12_run_full_flow_bg.sh`
- `16_run_deploy_with_monitor_bg.sh`

### Phase 3: 归档 legacy

将旧脚本移到：

- `legacy/3node/`
- `legacy/9node/`
- `legacy/12node_wrappers/`

这样目录层级会清晰很多，误用成本也会下降。

---

## 8. Recommended “Active” Script Set

如果只保留“当前主流程最有价值的一组脚本”，建议是：

- cluster env / nodes
  - `env_12node.sh`（临时）
  - `01_cluster_nodes_12node.sh`（临时）
- execution pipeline
  - `02_preflight_12node`
  - `03_compile_12node`
  - `04_build_12node`
  - `05_deploy-batched_12node`
  - `wait-ready_12node`
  - `07_seed_k8s_start_bird0130_12node.py`
  - `08_seed_k8s_start_bird_kernel_12node.py`
  - `09_reconvegence_12node.py`
  - `11_run_full_flow_12node.sh`
  - `12_run_full_flow_12node_bg.sh`
  - `16_run_deploy_with_monitor_12node_bg.sh`
- maintenance / repair
  - `clean`
  - `14_repair_master_docker_registry.sh`
  - `15_recover_stuck_deploy_9node.sh`
  - `ensure_master_registry.sh`
  - `init_master_docker.sh`
  - `repair_k3s_state_db_seedemu_namespace.py`
- memory measurement
  - `22_measure_memory_footprint_k8s_12node.sh`
  - `23_measure_memory_footprint_docker_host.sh`

---

## 9. Final Recommendation

**短期建议：**

- 不要立刻删脚本
- 先把明显旧的脚本移动到 `legacy/`
- 当前继续保留 `9node/12node` 双轨，避免误伤正在使用的流程

**中期建议：**

- 先完成“通用 env + 通用 inventory + 通用入口脚本”的重构
- 再删除 `*_12node` 这种 `sed` 包装脚本

**长期建议：**

- 目录里只保留：
  - 一套通用主流程
  - 一套 repair/ops 工具
  - 一套测量脚本
  - 一个 `legacy/` 归档目录

这样维护成本和误用概率都会显著下降。
