# B62 关键参数和 start-bird/start-kernel 对照

更新时间：2026-06-10

> Note: start-bird/start-kernel 的当前准确信息以 `bird.md` 为准。
> 本文件保留较宽泛的参数审计记录，其中早期关于 start-bird 删除
> `kernel.conf`、跳过 Phase 3 的描述已经被后续 lxl 对齐改动替代。

## 结论

- 当前 B62 的 `start_bird.sh` / `start_bird_kernel.sh` 已切回 `/home/lxl/k8s/lxl/start-bird`、`/home/lxl/k8s/lxl/start-kernel` 的主要执行逻辑：通过 `kubectl exec` 对 router-like pod 执行，所有节点并发，节点内串行。
- 当前 B62 的 start-bird 启动后会等待每个节点的探针 Pod 看到 `/proc/loadavg` 低于 `40`，检查间隔 `20s`，不再执行逐 Pod `birdc show status` verify 阶段。
- 当前 B62 的 start-kernel 使用同样的节点并发/节点内串行模型，写入 `kernel.conf` 后执行 `birdc configure`，再等待每节点 load < 40。
- 为适配当前 OVN/OVS 镜像，start-bird 会移除 stale `kernel.conf` 和显式 `kernel.conf` include，避免 kernel 阶段提前启动；start-kernel 会重新写入并 reload。
- 当前 B62 已增加统一 `loadAverage.log`，任意由 `runExperiment.sh` 调度的阶段、以及单独执行的 shell 阶段，都会每 30 秒记录一次 controller host 的 load average、CPU 和内存。

## 统一监控

| 项 | 当前值 | 来源 | 说明 |
| --- | ---: | --- | --- |
| 采样间隔 | 30s | `lib.sh`, `runFullExperiment.py` | 每个阶段开始即采样一次，然后每 30s 采样 |
| 日志文件 | `runs/.../loadAverage.log` | `lib.sh`, `runFullExperiment.py` | CSV 格式 |
| 字段 | Timestamp, Stage, ParentPID, Load_1/5/15, CPU_User/System/Idle, Mem_Total/Available/Used/Used_Pct | `lib.sh`, `runFullExperiment.py` | 与 `cpu_monitor.sh` 的 load/CPU 思路一致，补充内存 |
| 采样对象 | controller host | `lib.sh`, `runFullExperiment.py` | 不是每个 VM 内部的 load；如需 VM 级别需要另加 SSH 采样 |

## Clean 参数

| 参数 | 当前值 | 说明 |
| --- | ---: | --- |
| `CLEAN_CHECK_INTERVAL_SECONDS` | 15 | 清理状态轮询间隔 |
| `CLEAN_TIMEOUT_SECONDS` | 36000 | 清理总超时 |
| `CLEAN_DELETE_REQUEST_TIMEOUT_SECONDS` | 120 | 单次 kubectl 请求超时 |
| `CLEAN_BATCH_SIZE` | 100 | xargs 每批对象数 |
| `CLEAN_EVENT_DELETE_PARALLELISM` | 16 | events 批量删除并发 |
| `CLEAN_KUBE_OVN_DELETE_PARALLELISM` | 12 | Kube-OVN IP/Subnet/Vpc 删除并发 |
| `CLEAN_KUBE_OVN_FINALIZER_PARALLELISM` | 16 | Kube-OVN finalizer patch 并发 |
| `CLEAN_FORCE_NAMESPACED_AFTER_SECONDS` | 60 | 60s 后对残留 Pod/控制器资源发 force delete |
| `CLEAN_RETRY_DELETE_INTERVAL_SECONDS` | 60 | 重发 delete 的间隔 |
| `CLEAN_KUBE_OVN_FINALIZER_AFTER_SECONDS` | 120 | 120s 后清匹配 Kube-OVN 对象 finalizer |
| `CLEAN_NAMESPACE_FINALIZER_AFTER_SECONDS` | 300 | 300s 后在安全条件满足时清 namespace finalizer |

## Build 参数

| 参数 | 当前值 | 说明 |
| --- | ---: | --- |
| `SEED_BUILD_PARALLELISM` | 12 | 远端镜像构建并发 |
| `SEED_DOCKER_BUILDKIT` | 1 | 启用 BuildKit |
| `SEED_DOCKER_BUILDX` | 1 | 启用 buildx |
| `SEED_BUILD_SKIP_EXISTING` | 0 | 默认不复用已有镜像，避免 stale image |
| `SEED_FAST_IMAGE_ARCHIVE` | 1 | 启用 fast archive |
| `SEED_FAST_ARCHIVE_BASE_IMAGE` | 空 | 使用脚本默认基础来源 |
| `SEED_COMPOSE_DOCKER_CLI_BUILD` | 1 | docker compose 使用 Docker CLI build |
| `SEED_PRELOAD_NODE_CONCURRENCY` | 4 | 预加载节点并发 |
| `SEED_PRELOAD_IMAGE_CONCURRENCY` | 2 | 单节点镜像预加载并发 |
| `SEED_PRELOAD_RETRIES` | 3 | 预加载重试次数 |
| `SEED_PRELOAD_BACKOFF_SECONDS` | 5 | 预加载重试退避 |
| `SEED_REGISTRY_PUSH_RETRIES` | 5 | registry push 重试 |
| `SEED_REGISTRY_PUSH_BACKOFF_SECONDS` | 5 | registry push 退避 |
| `SEED_REGISTRY_PUSH_TIMEOUT_SECONDS` | 180 | 单次 push 超时 |

## Deploy 参数

| 参数 | 当前值 | 说明 |
| --- | ---: | --- |
| `DEPLOY_BATCH_SIZE` | 40 | workload controller 分批提交的批大小 |
| `DEPLOY_BATCH_SLEEP_SECONDS` | 5 | 批间 sleep |
| `DEPLOY_SUBNET_BATCH_SIZE` | 100 | Kube-OVN Subnet 分批提交的批大小 |
| `DEPLOY_SUBNET_BATCH_SLEEP_SECONDS` | 2 | Subnet 批间 sleep |
| `DEPLOY_MONITOR_INTERVAL` | 20 | deploy 内置 monitor 间隔 |
| `DEPLOY_MONITOR_ENABLED` | false | deploy 内置 monitor 默认关闭；统一 `loadAverage.log` 仍会开启 |
| `DEPLOY_STATIC_APPLY_MODE` | batch | foundation/static 资源分批 |
| `DEPLOY_CONTROLLER_APPLY_MODE` | batch | Deployment 等控制器资源分批 |
| `DEPLOY_USE_APPLY` | false | 使用 create/replace 风格而不是 apply |
| `DEPLOY_FAIL_FAST` | true | 压力检查失败直接退出 |
| `DEPLOY_CAPTURE_DESCRIBE_LIMIT` | 20 | 失败采集 describe 数量 |
| `DEPLOY_WARMUP_BATCHES` | 2 | warmup 批数 |
| `DEPLOY_WARMUP_BATCH_SIZE` | 5 | warmup 批大小 |
| `DEPLOY_PRESSURE_CHECK_SECONDS` | 15 | 压力检查间隔 |
| `DEPLOY_STABILIZE_TIMEOUT_SECONDS` | 36000 | 分批压力等待总超时 |
| `DEPLOY_MAX_PENDING_PODS` | 200 | pending 上限 |
| `DEPLOY_MAX_CREATING_PODS` | 200 | creating 上限 |
| `DEPLOY_MAX_NOTREADY_PODS` | 300 | running-not-ready 上限 |
| `DEPLOY_MAX_FAILED_PODS` | 10 | failed 上限 |
| `DEPLOY_REQUIRE_ALL_NODES_READY` | true | 要求所有节点 Ready |
| `DEPLOY_WAIT_KUBE_OVN_SUBNETS` | true | 等待 Kube-OVN Subnet Ready |
| `DEPLOY_KUBE_OVN_SUBNET_TIMEOUT_SECONDS` | 36000 | Subnet Ready 超时 |

Subnet 创建节流说明：当前 `Subnet` 会被 split 到 `01-subnets`，并由 `DEPLOY_SUBNET_BATCH_SIZE` / `DEPLOY_SUBNET_BATCH_SLEEP_SECONDS` 单独控制提交速度。`DEPLOY_BATCH_SIZE=40` 只控制 controller 资源（Deployment/StatefulSet/DaemonSet/Job）的提交速度。`DEPLOY_KUBE_OVN_SUBNET_TIMEOUT_SECONDS` 和 `DEPLOY_PRESSURE_CHECK_SECONDS` 只控制等待/轮询，不控制创建速率。

## Start-BIRD 参数对照

| 项 | 当前 B62 | `/home/lxl/k8s/lxl` / `lxl/test` | 是否一致 |
| --- | ---: | ---: | --- |
| 执行方式 | `kubectl exec` | `kubectl exec` | 一致 |
| 目标 Pod 过滤 | `seedemu.io/workload=seedemu` 且 role in r/brd/rs | `seedemu.io/workload=seedemu` 且 role in r/brd/rs | 一致 |
| 节点间并发 | 所有节点并发 | 所有节点并发 | 一致 |
| 节点内并发 | 串行 | 串行 | 一致 |
| 节点内启动延迟 | 0.08s | 0.08s | 一致 |
| 单容器 BIRD 启动 timeout | 45s exec timeout | 45s exec timeout | 一致 |
| 启动重试 | 2 次，退避 1s | 2 次，退避 1s | 一致 |
| 启动后 settle | 60s | 60s | 一致 |
| load threshold | 阈值 40，20s 检查一次，低于阈值才继续 | 阈值 40，20s 检查一次，低于阈值才继续 | 一致 |
| 最终 birdc 验证 | 不执行逐 Pod `birdc show status` verify | 参考脚本有 Phase 3 `birdc show status` | 按你的要求跳过 |
| 额外保护 | 删除 stale `kernel.conf` 和显式 include，创建 `00-empty.conf` | 删除 stale bird ctl/pid | B62 为 OVN/OVS 保留额外保护 |

当前 B62 wrapper 参数：

| 参数 | 当前值 |
| --- | ---: |
| `KUBECTL_LIST_TIMEOUT_SECONDS` | 300 |
| `BIRD_START_DELAY_SECONDS` | 0.08 |
| `BIRD_LOAD_THRESHOLD` | 40 |
| `BIRD_LOAD_CHECK_INTERVAL_SECONDS` | 20 |
| `BIRD_KUBECTL_EXEC_TIMEOUT_SECONDS` | 30 |
| `BIRD_START_EXEC_TIMEOUT_SECONDS` | 45 |
| `BIRD_START_RETRIES` | 2 |
| `BIRD_START_RETRY_BACKOFF_SECONDS` | 1 |
| `BIRD_POST_START_SETTLE_SECONDS` | 60 |

## Start-Kernel 参数对照

| 项 | 当前 B62 | `/home/lxl/k8s/lxl` / `lxl/test` | 是否一致 |
| --- | ---: | ---: | --- |
| 执行方式 | `kubectl exec` | `kubectl exec` | 一致 |
| 节点间并发 | 所有节点并发 | 所有节点并发 | 一致 |
| 节点内并发 | 1 | 串行 | 一致，都是单步串行 |
| 节点内 sleep | 0.3s | 0.3s | 一致 |
| 单容器 timeout | 45s exec timeout | 45s exec timeout | 一致 |
| `birdc` timeout | 10s | 10s | 一致 |
| export mode | all | all | 一致 |
| scan base/jitter | 6000 / 120 | 6000 / 120 | 一致 |
| post-switch settle | 15s | 15s | 一致 |
| load threshold | 阈值 40，20s 检查一次，低于阈值才继续 | 阈值 40，20s 检查一次，低于阈值才继续 | 一致 |
| pre-SSH wait | 无 | 无 | 一致 |
| Kernel 协议验证 | `birdc show protocols` 要求 Kernel up | 当前 `/home/lxl/k8s/lxl` 脚本要求 Kernel up | 一致 |

当前 B62 wrapper 参数：

| 参数 | 当前值 |
| --- | ---: |
| `KERNEL_SWITCH_DELAY_SECONDS` | 0.3 |
| `KERNEL_LOAD_THRESHOLD` | 40 |
| `KERNEL_LOAD_CHECK_INTERVAL_SECONDS` | 20 |
| `KERNEL_KUBECTL_EXEC_TIMEOUT_SECONDS` | 30 |
| `KERNEL_EXEC_TIMEOUT_SECONDS` | 45 |
| `KERNEL_BIRDC_TIMEOUT_SECONDS` | 10 |
| `KERNEL_EXPORT_MODE` | all |
| `KERNEL_SCAN_BASE_SECONDS` | 6000 |
| `KERNEL_SCAN_JITTER_SECONDS` | 120 |
| `KERNEL_SWITCH_RETRIES` | 2 |
| `KERNEL_SWITCH_RETRY_BACKOFF_SECONDS` | 1 |
| `KERNEL_POST_SWITCH_SETTLE_SECONDS` | 15 |

## 需要你审核的点

- 当前默认入口已按你的要求改回 `/home/lxl/k8s/lxl` 的 kubectl-exec 并发模型，但跳过 start-bird 最后的逐 Pod `birdc show status` verify。
- 上一轮卡住的直接原因是 BIRD 启动阶段 load average 过高并导致 master SSH 变慢/失联。现在 start-bird/start-kernel 自身会等待每节点 load < 40；`loadAverage.log` 仍会记录 controller host 侧负载。
- 在 4954 规模下，这条 kubectl-exec 路径会比 node-local 路径给 API server/kubelet 更多 exec 压力；如果它在 OVN/OVS 下仍卡住，可以再回退到 node-local helper 做对照。
