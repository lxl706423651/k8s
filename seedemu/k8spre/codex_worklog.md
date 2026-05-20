## 2026-05-18 18:26 - Decouple K3s Node Names And Fix KVM Destroy Disk Parsing

- User intent: 让 `configK3s.yaml` 中的 `nodes[].name` 成为显式 Kubernetes node name，从真实 hostname/KVM domain name 解耦；同时排查 `destroyKvmVms.sh kvmState.yaml` 无法确定磁盘路径的问题。
- Scope: `seedemu/k8spre/resources/setup/ansible/k3s-install.yml`、`seedemu/k8spre/resources/setup/destroyKvmVms.sh`、`origin_k8s/test/k8spre-test/setup/destroyKvmVms.sh`、相关 README。
- Changes: 在 K3s server 和 agent config 中加入 `node-name: "{{ inventory_hostname }}"`，使 `kubectl get nodes` 显示 `configK3s.yaml` 的节点名。
- Changes: 修复 `destroyKvmVms.sh` 的 `virsh domblklist --details` 解析，从 `$3 == "disk"` 改为 `$2 == "disk"`，匹配当前 libvirt 输出列。
- Commands: `rg -n "node-name|Render K3s|kubectl label node|inventory_hostname|Install K3s" ...` 检查 node-name 和 label 相关逻辑。
- Commands: `rg -n "domblklist|awk .*disk|kvmState|state-vars|validateDestroyTarget" ...` 定位 destroy 脚本磁盘路径解析问题。
- Commands: `bash -n .../destroyKvmVms.sh .../applyK3sCluster.sh` 验证 shell 语法。
- Commands: `python3 -m py_compile .../manageK3sConfig.py .../manageKvmConfig.py .../k8spre_smoke_test.py` 验证 Python 语法。
- Commands: `conda activate seedpy310 && python /home/lxl/k8s/origin_k8s/test/k8spre_smoke_test.py` 验证 K8sPre 非破坏性生成流程。
- Commands: `virsh list --all | egrep 'seed-k3s-master2|seed-k3s-worker12|seed-k3s-worker13' || true` 确认测试 VM 当前仍在运行，未自动删除。
- Commands: `virsh domblklist seed-k3s-master2 --details | awk '$2 == "disk" && $4 != "-" {print $4; exit}'` 验证修复后的磁盘路径解析可得到 `/data/lxl/k8spre/k8spre-test-dce02673/disks/seed-k3s-master2.qcow2`。
- Validation: shell/Python/smoke 均通过；未执行 destructive cleanup。
- Notes: `destroyKvmVms.sh kvmState.yaml` 出错原因是 libvirt 当前输出中 `disk` 在第 2 列，而旧脚本检查第 3 列。

## 2026-05-18 19:57 - Resolve Running Registry From Master Node

- User intent: 修正 `make build` 使用旧 registry 地址 `192.168.122.110:5000` 的问题，让当前 `k8spre-test` 集群使用新 master 地址。
- Scope: `seedemu/k8spre/resources/running/manageK8sManifest.py`、`origin_k8s/test/k8spre-test/running/manageK8sManifest.py`、相关 README。
- Changes: `registryPrefix` 优先读取显式 `registry.host`，否则从唯一 `role: master` 节点的 `ip` 推导；`registry.port` 仍缺省为 `5000`。
- Changes: `sshUser` 和 `sshKey` 优先读取顶层 `ssh.*`，否则从 master 节点的 `ssh.user` / `ssh.key` 推导，保证 `make build` SSH 到正确 master。
- Commands: `sed -n .../Makefile .../configRunning.yaml .../configK3s.yaml .../manageK8sManifest.py` 检查 registry 来源。
- Commands: `python3 manageK8sManifest.py config-value --config configRunning.yaml --key registryPrefix/sshUser/sshKey` 验证当前测试目录解析为 `192.168.122.122:5000`、`ubuntu`、`/home/lxl/.ssh/id_ed25519`。
- Commands: `make --dry-run build` 验证 build 将使用 `ubuntu@192.168.122.122` 和 `--registry-prefix '192.168.122.122:5000'`。
- Commands: `python3 -m py_compile .../manageK8sManifest.py` 验证 Python 语法。
- Commands: `conda activate seedpy310 && python /home/lxl/k8s/origin_k8s/test/k8spre_smoke_test.py` 验证 K8sPre 非破坏性生成流程。
- Validation: Python 编译、当前配置解析、`make --dry-run build`、smoke test 均通过；未执行真实 `make build`，未 push 镜像。
- Notes: 根因是 `configK3s.yaml` 简化为 `nodes[]` 后，running helper 仍回退到旧硬编码 registry host。

## 2026-05-18 20:29 - Keep Running Config Only In Running Stage

- User intent: 去掉 setup 阶段和 running 阶段各自生成 `configRunning.yaml` 的重复关系，并在 setup README 中加入 K3s 完成后的 kubeconfig、registry、kubectl 基础命令。
- Scope: `seedemu/k8spre/resources/setup/*`、`seedemu/k8spre/config.py`、`seedemu/k8spre/README.md`、`origin_k8s/test/k8spre-test/setup/*`、`origin_k8s/test/README_K8SPRE_TEST.md`、`origin_k8s/test/k8spre_smoke_test.py`。
- Changes: `applyK3sCluster.sh` 不再调用 `write-running-config`，只写 cluster inventory、kubeconfig，并打印 registry 地址。
- Changes: KVM 生成配置和 `kvmState.yaml` 不再记录 `outputs.runningConfig`；当前测试目录的旧 `setup/configRunning.yaml` 已删除。
- Changes: setup README 增加 `KUBECONFIG`、`SEED_NAMESPACE`、查看 nodes/pods/deploy/kube-system、查询 registry、进入 Pod 的常用命令。
- Commands: `rg -n "configRunning|runningConfig|outputRunningConfig" ...` 定位重复配置链路。
- Commands: `bash -n .../applyK3sCluster.sh .../destroyKvmVms.sh` 验证 shell 语法。
- Commands: `python3 -m py_compile .../config.py .../manageK3sConfig.py .../manageKvmConfig.py .../k8spre_smoke_test.py` 验证 Python 语法。
- Commands: `python3 ./manageK3sConfig.py --config ./configK3s.yaml shell-vars | grep -E ...` 验证当前 setup 只输出 kubeconfig/registry 相关变量，不再输出 `outputRunningConfig`。
- Commands: `conda activate seedpy310 && python /home/lxl/k8s/origin_k8s/test/k8spre_smoke_test.py` 验证非破坏性生成流程。
- Validation: 静态检查和 smoke test 均通过；未执行 KVM/K3s/build/deploy。
- Notes: `manageK3sConfig.py write-running-config` 作为旧流程兼容命令保留，但 setup 主流程不再调用。
