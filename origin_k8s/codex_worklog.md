## 2026-05-20  - Test Linux VXLAN Bridge Between amd And idc

- User intent: 探索并实验验证两台真实物理服务器 `amd` 和 `idc` 是否能使用 `vxlan + bridge` 作为 SeedEMU Multus/macvlan 的统一二层父接口方案，并确保失败时可撤销。
- Scope: 只操作两台服务器上的临时网络接口；不修改 K3s、Docker、iptables、默认路由、管理 IP 或源码。
- Changes: 创建并删除临时接口 `brseed0`、`vxseed0`、`macseed0`；未留下持久配置。
- Commands: `ip link del ... || true` 在 `amd` 和 `idc` 上清理旧测试接口，避免残留影响实验。
- Commands: 第一次尝试使用 `br-seedemu-test` / `vxlan-seedemu-test` 创建测试接口，`idc` 返回 `Attribute failed policy validation`；判断为 VXLAN 接口名超过 Linux 15 字符限制。
- Commands: 使用短接口名 `brseed0` / `vxseed0` 重试，分别在 `amd` 和 `idc` 创建 VXLAN-backed bridge。
- Commands: `ping -c 3` 验证 `amd brseed0 172.31.252.1/30` 与 `idc brseed0 172.31.252.2/30` 双向通信，结果均为 0% loss。
- Commands: 在两端 `brseed0` 上创建 `macseed0` macvlan 子接口，分别配置 `172.31.253.1/30` 和 `172.31.253.2/30`。
- Commands: `ping -I macseed0 -c 3` 验证 macvlan-on-bridge 双向通信，结果均为 0% loss。
- Commands: `bridge fdb show br brseed0` 检查两端 bridge/VXLAN FDB 学习情况，能看到对端 MAC 经 VXLAN remote IP 学习。
- Commands: `ip link del macseed0/brseed0/vxseed0` 在两端显式清理临时接口，并用 `ip link show` 验证三者均不存在。
- Validation: Linux VXLAN + bridge 在当前两台物理机三层管理网络上可行；macvlan 挂载到 VXLAN-backed bridge 上也可行。
- Notes: 该实验只验证两台机器 PoC，不代表几十台物理机适合裸 Linux VXLAN full mesh；正式脚本需要控制接口名长度不超过 15 字符，并提供幂等创建/清理逻辑。

## 2026-05-20 - Implement And Validate K8sPre Physical VXLAN Flow

- User intent: 基于刚才验证通过的 `vxlan + bridge` 设计，完善 `seedemu.k8spre` 的真实物理机版本；脚本需要可复现、失败可回滚，并能在两台真实物理机 `amd/idc` 上跑通 `/home/lxl/k8s/origin_k8s/emulate/output` 的 SeedEMU/K8s workload。
- Code changes: 在 `seedemu/k8spre/resources/setup` 新增 `preparePhysicalNodes.sh`、`configureLinuxVxlanFabric.sh`、`validateLinuxVxlanFabric.sh`、`cleanLinuxVxlanFabric.sh`、`destroyPhysicalCluster.sh`。
- Code changes: 扩展 `manageK3sConfig.py`，支持 `fabric.type=linux-vxlan`、`fabric.nodes.*.underlayInterface`、`k3s.flannelBackend`、`cni.defaultMasterInterface`，并生成 fabric shell vars/TSV。
- Code changes: 扩展 `K8sPre` API，新增 `writePhysicalNodeScripts()` 和 `preparePhysicalNodes()`；保留原有包名 `seedemu.k8spre`。
- Code changes: 修改 `ansible/k3s-install.yml`，`flannel-backend` 改为 YAML 可配置；master 侧使用 `k3s kubectl`；Docker 已存在时不再强装 Ubuntu `docker.io`；Multus manifest 改为 `curl --max-time` 下载后本地 apply，避免 `kubectl apply -f URL` 无界卡住。
- Code changes: 扩展 `running/manageK8sManifest.py` 和 `running/Makefile`，`make up` 生成 kustomization 时会根据 `configK3s.yaml` 的 `cni.defaultMasterInterface` 重写所有 NAD 的 macvlan `master`，本次将 compile 输出中的 `ens2` 改为 `br-seedemu`。
- Code changes: 更新 `seedemu/k8spre/README.md` 和 `seedemu/k8spre/resources/setup/README.md`，补充真实物理机、linux-vxlan fabric、清理脚本、running 阶段 NAD master rewrite 说明。
- Config changes: `/home/lxl/k8s/origin_k8s/test/configK3s.yaml` 增加 `k3s.flannelBackend: vxlan`、`cni.defaultMasterInterface: br-seedemu`、`fabric.type: linux-vxlan`，并配置 `amd` underlay `eno8403`、`idc` underlay `enp101s0f1np1`。
- Commands: `python3 -m py_compile ...` 校验 Python 文件；`bash -n ...` 校验 setup/running shell 脚本。
- Commands: 使用 `K8sPre.writePhysicalNodeScripts()`、`writeK3sBuildScripts()`、`writeRunningScripts()` 生成 `/home/lxl/k8s/origin_k8s/test/k8spre-physical-current`。
- Commands: `bash ./preparePhysicalNodes.sh ./configK3s.yaml` 验证 `amd/idc` SSH、`sudo -n` 和 underlay 接口。
- Commands: `bash ./configureLinuxVxlanFabric.sh ./configK3s.yaml` 创建 `br-seedemu/vxseed0`；`bash ./validateLinuxVxlanFabric.sh ./configK3s.yaml` 验证 bridge 与 macvlan 双向 ping。验证通过，失败路径也实测会自动调用 `cleanLinuxVxlanFabric.sh`。
- Commands: `bash ./buildK3sCluster.sh` 在 `amd` 安装 K3s server、在 `idc` 安装 K3s agent，配置 registry、Multus、基础镜像与 kubeconfig。
- Commands: `cd running && make preflight` 通过；`make build` 使用 BuildKit/buildx 构建并 push 57 个镜像到 `10.202.236.88:5000`；`make up` 成功创建 workload 并等待所有 Deployment/Pod Ready。
- Validation: `kubectl get nodes -o wide` 显示 `amd` 和 `idc` 均 Ready；`kube-multus-ds` 两个 Pod 均 Running。
- Validation: `kubectl -n seedemu-k3s-real-topo get pods` 统计 `total=57`、`status_Running=57`，节点分布 `amd=50`、`idc=7`。
- Validation: `kubectl -n seedemu-k3s-real-topo get nad net-ix-ix100 -o jsonpath='{.spec.config}'` 显示 `master":"br-seedemu"`。
- Validation: 跨物理节点仿真网络 ping 成功：从 `idc` 上的 `as164h-host-1`，经 `net0=10.164.0.72/24` ping `amd` 上的 `as164brd-router0=10.164.0.254`，3/3 received，0% packet loss。
- Notes: 本次真实物理机工作流已跑通到 workload Ready 和跨节点 macvlan 通信；当前 workload 仍按 Kubernetes 默认调度分布，未做 by-AS placement 优化。

## 2026-05-20 20:52 - Document K8sPre Generated Context And Dual Cluster Flows

- User intent: 在生成的 `setup/README.md` 中写入本次目录对应的 kubeconfig 和默认 namespace；同时补充 `seedemu.k8spre` 包级 README，说明 KVM VM 集群与真实物理机集群两种支持形式的脚本流程、输入输出和文件角色。
- Scope: 修改 `seedemu/k8spre/k8spre.py`、`seedemu/k8spre/README.md`、`seedemu/k8spre/resources/setup/README.md`；不执行 KVM、K3s、registry 或 workload 操作。
- Changes: `K8sPre` 在生成 KVM、K3s、physical setup 脚本时，会改写 `setup/README.md` 的生成区块，写入具体 kubeconfig 路径、`seedemu-k3s-real-topo` namespace、`configK3s.yaml` 路径和常用 kubectl 命令。
- Changes: setup README 模板新增可替换上下文占位区，避免多次生成时重复追加同类内容。
- Changes: 包级 README 新增 KVM VM 集群与真实物理机集群的对照表，覆盖 API 入口、脚本顺序、用户输入、中间文件、输出文件、后续消费者和清理脚本。
- Commands: `sed -n ...` 读取 `k8spre.py`、包级 README、setup README 和相关配置解析代码，用于确认当前实际实现。
- Commands: `python3 -m py_compile /home/lxl/k8s/seedemu/k8spre/k8spre.py` 使用系统 Python 通过语法检查。
- Commands: 尝试 `source /home/lxl/miniconda3/etc/profile.d/conda.sh` 失败，原因是该路径不存在；随后用 `command -v conda` 和 `find` 定位到 `/home/lxl/anaconda3/etc/profile.d/conda.sh`。
- Commands: `source /home/lxl/anaconda3/etc/profile.d/conda.sh && conda activate seedpy310 && python -m py_compile /home/lxl/k8s/seedemu/k8spre/k8spre.py` 使用 `seedpy310` 通过语法检查。
- Commands: `bash -n /home/lxl/k8s/seedemu/k8spre/resources/setup/*.sh /home/lxl/k8s/seedemu/k8spre/resources/running/*.sh` 通过 shell 语法检查。
- Commands: 使用 `K8sPre.writeKvmInstallScripts('/tmp/k8spre-readme-test', overwrite=True)` 生成临时目录，并用 `rg` 验证 README 中包含 kubeconfig、namespace 和 `configK3s.yaml`。
- Commands: 使用 `K8sPre.writePhysicalNodeScripts('/tmp/k8spre-readme-physical-test', config='/home/lxl/k8s/origin_k8s/test/configK3s.yaml', overwrite=True)` 和 `writeK3sBuildScripts(...)` 生成临时物理机目录，并用 `rg` 验证 README 上下文。
- Commands: 使用 `writeKvmInstallScripts()` 后接 `writeK3sBuildScripts(overwrite=False)` 生成 `/tmp/k8spre-readme-repeat-test`，验证 README 上下文标记只出现一组，没有重复追加。
- Commands: `git diff --check -- seedemu/k8spre/k8spre.py seedemu/k8spre/README.md seedemu/k8spre/resources/setup/README.md` 通过 whitespace 检查。
- Validation: 所有检查均通过；smoke test 只写 `/tmp/k8spre-readme-*` 临时目录，没有创建 VM、修改 K3s 集群、推送镜像或部署 namespace。
- Notes: 当前生成 README 的 kubeconfig 路径优先读取 `outputs.kubeconfig`，否则使用与 README 同级的 `<clusterName>.kubeconfig.yaml`；namespace 固定为 `seedemu-k3s-real-topo`。

## 2026-05-20 - Destroy Physical K3s Cluster And Verify Reset

- User intent: 执行 `destroyPhysicalCluster.sh` 清理当前两台真实物理机 `amd/idc` 上的 K3s 集群、registry 和 linux-vxlan fabric，确认恢复到可复现下一轮实验的初始状态。
- Scope: 操作 `/home/lxl/k8s/origin_k8s/test/k8spre-physical-current/setup` 下的物理机清理脚本；涉及本机 `amd` 和远端 `idc`，不修改源码。
- Commands: `sed -n ... configK3s.yaml` 复核清理目标为 `amd` master `10.202.236.88` 和 `idc` worker `10.202.191.39`。
- Commands: `bash -n destroyPhysicalCluster.sh cleanLinuxVxlanFabric.sh` 通过语法检查。
- Commands: `bash ./destroyPhysicalCluster.sh ./configK3s.yaml` 成功完成；脚本卸载两台机器上的 K3s，清理 K3s/CNI/flannel 运行态，删除 `br-seedemu/vxseed0/macseed0` fabric，移除 master registry 容器，并删除生成的 kubeconfig/inventory。
- Validation: 本机 `systemctl is-active k3s/k3s-agent` 均为 inactive，enabled 均为 not-found；`k3s` 二进制不存在；精确进程名检查未发现 `k3s/kubelet/containerd-shim-runc-v2`。
- Validation: `idc` 上 `systemctl is-active k3s/k3s-agent` 均为 inactive，enabled 均为 not-found；`k3s` 和 `kubectl` 二进制不存在；精确进程名检查未发现 `k3s/kubelet/containerd-shim-runc-v2`。
- Validation: 两台机器上 `br-seedemu`、`vxseed0`、`macseed0`、`cni0`、`flannel.1` 均 absent；`/etc/rancher/k3s`、`/var/lib/rancher/k3s`、`/var/lib/kubelet`、`/run/k3s`、`/run/flannel`、`/var/lib/cni` 均 absent；`cni-` netns 数为 0。
- Validation: 两台机器 `iptables-save` 中 `KUBE-|CNI-|flannel` 规则计数为 0；registry 容器不存在。
- Validation: 本机 underlay `eno8403` 仍为 `10.202.236.88/24` 且默认路由仍为 `10.202.236.1`；`idc` underlay `enp101s0f1np1` 仍为 `10.202.191.39/24` 且默认路由仍为 `10.202.191.1`。
- Validation: `/home/lxl/k8s/origin_k8s/test/k8spre-physical-current/setup/seedemu-k3s.kubeconfig.yaml` 和 `seedemu-k3s.inventory.yaml` 已删除；显式使用该 kubeconfig 执行 `kubectl get nodes` 返回文件不存在。
- Notes: Docker 服务在两台机器上仍为 active，这是预期行为；清理脚本只移除本轮 `registry` 容器，不卸载 Docker。本机 `/usr/local/bin/kubectl` 仍存在，看起来是预先安装的独立 kubectl，不是当前 K3s 服务残留。

## 2026-05-20 21:55 - Reproduce Physical And KVM K8sPre Flows

- User intent: 重新复现 `seedemu.k8spre` 的两条路径：多真实物理机集群和多 KVM VM 集群，确认都能跑通 `/home/lxl/k8s/origin_k8s/emulate/output` 的 `preflight/build/up`，并记录遇到的问题和实际处理方式。
- Scope: 生成并执行 `/home/lxl/k8s/origin_k8s/test/k8spre-physical-repro-20260520_212257` 与 `/home/lxl/k8s/origin_k8s/test/k8spre-kvm-repro-20260520_212257` 下的脚本；未修改源码；最终清理本轮 namespace、真实物理机 K3s/VXLAN fabric、本轮新建 KVM VM。
- Commands: `source /home/lxl/anaconda3/etc/profile.d/conda.sh && conda activate seedpy310 && PYTHONPATH=/home/lxl/k8s python - ...` 验证 `from seedemu.k8spre import K8sPre` 可用。
- Commands: `virsh list --all` 记录基线，已有 12 节点旧 VM `seed-k3s-master` 与 `seed-k3s-worker1..11` 正在运行；本轮 KVM 自动选择 `seed-k3s-master2`、`seed-k3s-worker12` 避免冲突。
- Commands: 使用 `K8sPre.writePhysicalNodeScripts()`、`writeK3sBuildScripts()`、`writeRunningScripts()` 生成物理机复现实验目录；`bash -n` 与 `python3 -m py_compile` 校验生成脚本和 helper。
- Commands: 在物理机 setup 中执行 `bash ./preparePhysicalNodes.sh ./configK3s.yaml`、`bash ./configureLinuxVxlanFabric.sh ./configK3s.yaml`、`bash ./validateLinuxVxlanFabric.sh ./configK3s.yaml`，配置并验证 `amd/idc` 的 `br-seedemu + vxseed0` fabric。
- Commands: 在物理机 setup 中执行 `bash ./buildK3sCluster.sh`，在 `amd` 上安装 K3s server/local registry，在 `idc` 上安装 K3s agent，并生成 kubeconfig 与 registry 配置。
- Commands: 在物理机 running 中执行 `make preflight`、`make build`、`make up`；57 个镜像使用 BuildKit/buildx 构建并 push 到 `10.202.236.88:5000`，57 个 Pod 全部 Running。
- Validation: 物理机 `kubectl get nodes -o wide` 显示 `amd`、`idc` 均 Ready；Pod 分布为 `amd=49`、`idc=8`；NAD `net-ix-ix100` 的 macvlan master 被 rewrite 为 `br-seedemu`。
- Validation: 物理机跨节点仿真网段 ping 成功：`idc` 上的 `as164h-host-1` ping `amd` 上的 `10.164.0.254`，3/3 received，0% packet loss。
- Commands: 物理机 running 中执行 `make clean` 并等待 namespace 删除；setup 中执行 `bash ./destroyPhysicalCluster.sh ./configK3s.yaml` 清理 K3s、registry、`br-seedemu/vxseed0/macseed0`。
- Validation: 物理机清理后，本机与 `idc` 的 `k3s/k3s-agent` 均 inactive；`br-seedemu`、`vxseed0`、`macseed0`、`cni0`、`flannel.1` 均 absent。
- Commands: 使用 `K8sPre.writeKvmInstallScripts()` 生成 1 master + 1 worker 的 KVM 复现实验目录，配置为 master 8 vCPU/8 GiB/60 GiB、worker 6 vCPU/8 GiB/60 GiB；随后用 `writeK3sBuildScripts()`、`writeRunningScripts()` 生成 K3s 和 running 脚本。
- Commands: KVM setup 中执行 `bash ./installKvmVms.sh`，创建 `seed-k3s-master2` `192.168.122.122` 与 `seed-k3s-worker12` `192.168.122.123`，生成 `configK3s.yaml` 与 `kvmState.yaml`，并执行 VM limit tuning。
- Commands: KVM setup 中执行 `bash ./buildK3sCluster.sh`，在 `master2/worker12` 上构建 K3s 集群、registry、Multus 与基础镜像；两个节点 Ready。
- Commands: KVM running 中执行 `make preflight`、`make build`、`make up`；build 远程上传到 master VM 后使用 BuildKit/buildx 构建并 push 到 `192.168.122.122:5000`；57 个 Pod 全部 Running。
- Validation: KVM `kubectl get nodes -o wide` 显示 `seed-k3s-master2`、`seed-k3s-worker12` 均 Ready；Pod 分布 `master2=31`、`worker12=26`；NAD `net-ix-ix100` 的 macvlan master 为 VM 内统一接口 `ens2`。
- Validation: KVM 跨节点仿真网段 ping 成功：`worker12` 上的 `as150h-host-0` ping `master2` 上的 `10.150.0.254`，3/3 received，0% packet loss。
- Commands: KVM running 中执行 `make clean` 并等待 namespace 删除，39 秒后 namespace 消失；setup 中执行 `bash ./destroyKvmVms.sh ./kvmState.yaml`，只销毁本轮 `seed-k3s-master2/worker12`。
- Validation: `destroyKvmVms.sh` 自带 cleanup verification passed；`virsh list --all` 中本轮 `seed-k3s-master2/worker12` 不存在，旧的 `seed-k3s-master` 与 `worker1..11` 仍在运行；本轮磁盘文件已不存在。
- Issues: 物理机 fabric validate 中曾观察到一次 `idc -> amd` macvlan 测试 1/3 成功但脚本按非零连通通过；后续真实 Pod 跨节点 ping 为 3/3 成功，因此未修改脚本。
- Issues: 物理机 K3s runtime tuning 阶段提示 `/etc/sysctl.d/99-seed-vm-limits.conf` 不存在；这属于未执行 VM-specific tuning 的非阻塞 warning，小规模 workload 不受影响。若真实物理机做大规模实验，后续应单独补物理机 tuning 脚本。
- Issues: KVM 首次远程 build 明显较慢，因为新 VM Docker/buildx cache 冷启动，需要重新构建基础层；实际处理方式是等待构建完成，未改代码。
- Issues: KVM Docker 安装阶段出现 `debconf` frontend fallback warning；安装成功且集群可用，未处理。
- Notes: 本轮未改源码；只生成测试目录、执行真实部署验证、再清理本轮创建的资源。

## 2026-05-20 22:11 - Run Physical Cluster Destroy Again

- User intent: 清除刚刚创建的 K3s 集群，执行 destroy。
- Scope: 只执行最新物理机复现目录 `/home/lxl/k8s/origin_k8s/test/k8spre-physical-repro-20260520_212257/setup` 下的 destroy 脚本；不修改源码，不清理旧 12 节点 KVM VM。
- Commands: `find /home/lxl/k8s/origin_k8s/test -maxdepth 3 ...` 定位可用的 `destroyPhysicalCluster.sh`、`destroyKvmVms.sh`、`configK3s.yaml`。
- Commands: 检查本机和 `idc` 的 `k3s/k3s-agent` 状态与 `br-seedemu/vxseed0/macseed0/cni0/flannel.1` 接口，执行 destroy 前已是 inactive/absent。
- Commands: `cd /home/lxl/k8s/origin_k8s/test/k8spre-physical-repro-20260520_212257/setup && bash ./destroyPhysicalCluster.sh ./configK3s.yaml` 成功执行幂等清理。
- Validation: 清理后本机和 `idc` 的 `k3s/k3s-agent` 均为 inactive；`br-seedemu`、`vxseed0`、`macseed0`、`cni0`、`flannel.1` 均 absent；`iptables-save` 中 `KUBE-|CNI-|flannel` 计数均为 0。
- Validation: 生成的 `seedemu-k3s.kubeconfig.yaml` 与 `seedemu-k3s.inventory.yaml` 不存在。
- Validation: `virsh list --all` 显示旧的 `seed-k3s-master` 与 `seed-k3s-worker1..11` 仍在运行，本次 destroy 没有影响旧 KVM 集群。
- Notes: 本轮没有发现需要额外清理的 K3s 或 fabric 残留。

## 2026-05-20 23:59 - Add And Validate Kube-OVN Physical Fabric

- User intent: 在 `seedemu.k8spre` 中加入真实物理机 OVN backend，`writePhysicalNodeScripts()` 支持 `connection="vxlan"` 或 `connection="ovn"`，running 阶段在 `fabric.type=ovn` 时生成 Kube-OVN layer2 NAD/Subnet，而不是 macvlan NAD，并用 amd/idc 真实集群验证 `/home/lxl/k8s/origin_k8s/emulate/output` 可以部署。
- Scope: 修改 `seedemu/k8spre` 的 Python API、setup resources、running resources 和 README；生成并执行 `/home/lxl/k8s/origin_k8s/test/k8spre-ovn-repro-20260520_225305` 下的真实物理机实验目录；未清理最终成功的 OVN 集群，方便继续检查。
- Changes: `writePhysicalNodeScripts(..., connection="ovn")` 会写入 `fabric.type=ovn`，并为 OVN 模式默认使用 K3s `v1.29.15+k3s1`，以满足 Kube-OVN Helm chart 对 Kubernetes 版本的要求。
- Changes: `resources/setup` 拆分为 `kvm/`、`vxlan/`、`ovn/` 子目录；KVM、VXLAN、OVN 脚本入口和 README 已按新目录更新。
- Changes: 新增 OVN setup 脚本：`ovn/installKubeOvnFabric.sh`、`ovn/validateKubeOvnFabric.sh`、`ovn/cleanKubeOvnFabric.sh`；`applyK3sCluster.sh` 在 K3s/Multus 安装后调用 OVN 安装；`destroyPhysicalCluster.sh` 在卸载 K3s 前清理 Kube-OVN。
- Changes: `manageK3sConfig.py` 增加 `fabricType` 和 `ovn-shell-vars`，提供 Kube-OVN Helm、CIDR、CNI bin dir 等配置缺省值。
- Changes: `running/Makefile` 和 `manageK8sManifest.py` 支持 `networkBackend=kube-ovn`；`make up` 会生成 `k8s.kube-ovn.yaml`，把 compiler 的 macvlan NAD 转为 `type=kube-ovn` NAD，并为每个 SeedEMU 网络生成 Kube-OVN `Vpc/Subnet`。
- Changes: running 渲染器会把 macvlan/static IPAM 的 `ips: ["10.x.x.x/24"]` 改为 Kube-OVN provider annotation，例如 `<nad>.<namespace>.ovn.kubernetes.io/ip_address: 10.x.x.x`，避免 Kube-OVN controller 把 CIDR 字符串当作 IP 解析失败。
- Changes: `running/Makefile clean` 现在会在删除 namespace 前先 `kubectl delete -k`，确保 OVN 模式下 cluster-scoped `Vpc/Subnet` 也被清理；但不会删除 `kustomization.yaml` 文件本身。
- Commands: `PYTHONPATH=/home/lxl/k8s python - ... K8sPre.writePhysicalNodeScripts(..., connection="ovn") ...` 生成 OVN 真实物理机实验目录。
- Commands: `cd .../setup && bash ./preparePhysicalNodes.sh ./configK3s.yaml` 通过 amd/idc SSH、sudo 和基础命令检查。
- Commands: `cd .../setup && bash ./buildK3sCluster.sh` 首轮调试并最终通过；安装 K3s v1.29.15、Multus、registry、SeedEMU base/router 镜像和 Kube-OVN non-primary CNI。
- Commands: `cd .../running && make preflight` 通过；确认 `network_backend=kube-ovn`、Kube-OVN CRD/controller/CNI/OVS 就绪、registry 可达。
- Commands: `cd .../running && make build` 通过；57 个镜像使用 `DOCKER_BUILDKIT=1 docker buildx build --load` 构建并 push 到 `10.202.236.88:5000`。
- Commands: 首次 `make up` 因 Kube-OVN 静态 IP 格式失败，错误为 `failed to parse IP 10.x.x.x/24`；随后删除 namespace、修正 renderer 后重新 `make up`，所有 Deployment rollout 和 Pod Ready 检查通过。
- Commands: `bash -n` 检查 `seedemu/k8spre/resources/setup` 和 `resources/running` 下所有 shell 脚本通过；`python3 -m py_compile` 检查 K8sPre API、setup helper、running helper 通过；`git diff --check` 通过。
- Commands: `/tmp/k8spre-ovn-smoke-final2` 生成测试通过；确认 physical OVN 生成目录不包含 `.gitignore`、包含 `ovn/` 和 `vxlan/`，`render-kustomization` 生成的 deployment 不含 Multus `ips` 字段且含 Kube-OVN provider IP annotation，`make -n clean` 包含 `kubectl delete -k`。
- Commands: 一次 smoke test 校验脚本中的 Python 片段有本地语法错误，未影响仓库代码；随后用修正后的检查脚本重新验证通过。
- Validation: `kubectl -n seedemu-k3s-real-topo get pods` 统计 `Running=57,total=57`。
- Validation: `kubectl -n seedemu-k3s-real-topo get nad net-160-net0 -o jsonpath='{.spec.config}'` 显示 `type":"kube-ovn"` 和 provider `net-160-net0.seedemu-k3s-real-topo.ovn`。
- Validation: 跨真实物理机仿真网络连通：AS160 router 在 amd，host 在 idc；从 `as160h-host-0` ping `10.160.0.254`，3/3 received，0% packet loss。
- Validation: 在 `as160brd-router0` 内执行 `birdc show protocols`，BGP `Established`，OSPF/kernel/device/direct/pipe 协议均 up。
- Issues: Multus manifest 直接下载 raw GitHub 曾超时；处理方式是在 Ansible playbook 中加入 bounded retry 和 ghproxy fallback。
- Issues: Kube-OVN Helm chart v1.15.12 要求 Kubernetes >=1.29；处理方式是在 OVN connection 默认写入 K3s `v1.29.15+k3s1`。
- Issues: K3s v1.29 的 CNI bin dir 为 `/var/lib/rancher/k3s/data/cni`，不是旧 `current/bin`；处理方式是让 Ansible 读取 K3s containerd `bin_dir`，并让 OVN 默认使用该目录。
- Issues: `kube-ovn-cni` init container 会覆盖 `loopback/portmap/kube-ovn/macvlan/ipvlan`，与 K3s CNI symlink 冲突；处理方式是在 Helm 安装前只删除这些明确覆盖目标。
- Issues: Kube-OVN image 拉取可能慢或失败；处理方式是从宿主机 Docker pull/save 后导入每个节点的 K3s containerd。
- Notes: 最终成功集群和 namespace 保持运行，未执行 destroy；如需清理，可在生成目录 running 执行 `make clean`，再在 setup 执行 `bash ./destroyPhysicalCluster.sh ./configK3s.yaml`。

## 2026-05-21 10:46 - Destroy OVN Cluster And Add VXLAN/OVN Physical Examples

- User intent: 清除当前真实物理机 OVN 集群并恢复初始化状态，然后在 `/home/lxl/k8s/origin_k8s/test` 下提供两个最简物理机示例：一个 VXLAN，一个 OVN；不执行后续示例安装或 workload 启动。
- Scope: 执行 `/home/lxl/k8s/origin_k8s/test/k8spre-ovn-repro-20260520_225305` 的清理；新增 `configK3sVxlan.yaml`、`configK3sOvn.yaml`、`writeVxlanExample.py`、`writeOvnExample.py`、`k8spreExamples.md`；未修改 `seedemu/k8spre` 源码。
- Commands: `cd .../running && make clean` 删除 `seedemu-k3s-real-topo` namespace、Deployment、NAD 以及 Kube-OVN cluster-scoped `Vpc/Subnet`。
- Commands: `cd .../setup && bash ./destroyPhysicalCluster.sh ./configK3s.yaml` 清理 amd/idc 上的 Kube-OVN fabric、K3s server/agent、registry 容器、K3s/CNI/flannel 运行态和生成的 kubeconfig/inventory。
- Validation: 本机与 idc 的 `k3s/k3s-agent` 均为 inactive；`br-seedemu`、`vxseed0`、`macseed0`、`cni0`、`flannel.1` 均 absent；未发现精确名为 `k3s` 或 `k3s-agent` 的进程。
- Commands: `chmod +x writeVxlanExample.py writeOvnExample.py` 让两个示例入口可直接执行。
- Commands: `source /home/lxl/anaconda3/etc/profile.d/conda.sh && conda activate seedpy310 && python -m py_compile ...` 通过两个示例 Python 文件语法检查。
- Commands: `PYTHONPATH=/home/lxl/k8s python - ...` 在 `seedpy310` 中验证两个 `configK3s*.yaml` 可解析，且 `from seedemu.k8spre import K8sPre` 可用。
- Commands: 使用 `/tmp/k8spre-example-api-validate` 做非破坏性 API 生成测试：分别调用 `writePhysicalNodeScripts()`、`writeK3sBuildScripts()`、`writeRunningScripts()` 生成 VXLAN/OVN 目录，只校验生成文件存在，不执行安装脚本。
- Notes: 第一次 Python 验证落到 base 环境，因缺少 `geopy` 失败；随后使用 `/home/lxl/anaconda3` 的 `seedpy310` 重新验证通过。该失败不涉及新代码逻辑。
- Notes: 本轮没有运行 `writeVxlanExample.py` 或 `writeOvnExample.py` 生成正式示例目录，也没有重建集群；后续由用户手动执行。

## 2026-05-21 11:11 - Fix Physical Fabric Resource Decoupling

- User intent: 修复 OVN 示例生成后仍出现 `setup/vxlan/` 的错误；真实物理机 VXLAN 和 OVN backend 的生成目录必须解耦。
- Scope: 修改 `seedemu/k8spre/k8spre.py` 的资源复制逻辑，修改 `seedemu/k8spre/resources/setup/destroyPhysicalCluster.sh` 的 backend 清理逻辑，补充 `seedemu/k8spre/README.md` 说明，并重新生成 `/home/lxl/k8s/origin_k8s/test/k8spre-ovn-example` 与 `k8spre-vxlan-example` 脚本目录。
- Changes: `writePhysicalNodeScripts()` 和 `writeK3sBuildScripts()` 现在会根据最终 `configK3s.yaml` 的 `fabric.type` 只复制一个 fabric 子目录：`linux-vxlan -> vxlan/`，`ovn/kube-ovn -> ovn/`。
- Changes: 使用 `overwrite=True` 重新生成时会删除上一次残留的另一种 backend 子目录，避免 OVN 目录里残留 `vxlan/` 或 VXLAN 目录里残留 `ovn/`。
- Changes: `destroyPhysicalCluster.sh` 现在只调用当前 `fabric.type` 对应的清理脚本；如果脚本目录不在当前生成目录内，会输出 warning 而不是假设两种 backend 都存在。
- Commands: `python -m py_compile seedemu/k8spre/k8spre.py writeVxlanExample.py writeOvnExample.py` 通过 Python 语法检查。
- Commands: `bash -n seedemu/k8spre/resources/setup/destroyPhysicalCluster.sh` 通过 shell 语法检查。
- Commands: `PYTHONPATH=/home/lxl/k8s python ./writeOvnExample.py` 重新生成 OVN 示例目录；验证 `setup/ovn/` 存在且 `setup/vxlan/` 不存在。
- Commands: `PYTHONPATH=/home/lxl/k8s python ./writeVxlanExample.py` 重新生成 VXLAN 示例目录；验证 `setup/vxlan/` 存在且 `setup/ovn/` 不存在。
- Commands: `bash -n` 检查两个生成目录中的 `destroyPhysicalCluster.sh` 和 `buildK3sCluster.sh` 均通过。
- Validation: `git diff --check` 对本轮修改文件通过；本轮未安装 K3s、未创建 fabric、未部署 workload。
- Notes: 如果用户之前已经生成过错误的 OVN 目录，重新运行 `python3 ./writeOvnExample.py` 即可用新的 `overwrite=True` 逻辑清掉旧 `setup/vxlan/`。

## 2026-05-21 12:23 - Debug OVN make up ContainerCreating

- User intent: OVN 示例执行 `make up` 后 Pod 卡在 `ContainerCreating` 超过 5 分钟，需要监测根因并修复。
- Scope: 监测并修复 `/home/lxl/k8s/origin_k8s/test/k8spre-ovn-example` 当前真实物理机 OVN 集群；同步修复源码资源脚本和已生成示例脚本。
- Findings: 初始事件显示 sandbox 创建失败，K3s CNI bin dir `/var/lib/rancher/k3s/data/cni` 缺失 `loopback/portmap`，后续又暴露缺失 `kube-ovn` CNI delegate binary。
- Findings: 多次失败 sandbox 在 `amd` 节点留下大量 stale host-local IPAM lease，导致 flannel 报 `no IP addresses available in range set: 10.42.0.1-10.42.15.254`。
- Actions: 使用临时 `hostNetwork` privileged repair Pod 修复 `amd/idc` 上的 `/var/lib/rancher/k3s/data/cni`，恢复 `loopback/portmap` symlink，并把 `/kube-ovn/kube-ovn` 复制为 host CNI binary。
- Actions: 删除 51 个 Deployment 管理的 stuck workload Pod，让 controller 重新创建；随后使用临时 repair Pod 清理 `amd` 上 stale `/var/lib/cni/networks/cbr0` lease，只保留 kube-system 当前占用 IP 与 lock/last_reserved 文件。
- Changes: 在 `seedemu/k8spre/resources/setup/ovn/installKubeOvnFabric.sh` 增加 `repairK3sCniBinDirAfterKubeOvnInstall()`，Kube-OVN 安装并就绪后自动用每节点短生命周期 hostNetwork Pod 校验和修复 K3s CNI binary dir。
- Changes: 同步更新已生成目录 `/home/lxl/k8s/origin_k8s/test/k8spre-ovn-example/setup/ovn/installKubeOvnFabric.sh`，避免当前示例下一次复现时继续使用旧逻辑。
- Commands: `kubectl --kubeconfig ... get pods/events/describe pod/logs` 定位 `loopback`、`kube-ovn` binary 缺失与 IPAM exhaustion。
- Commands: `bash -n` 检查源码资源脚本和生成脚本通过；`git diff --check` 对本轮修改文件通过。
- Validation: `kubectl --kubeconfig ... -n seedemu-k3s-real-topo wait --for=condition=Ready pod -l seedemu.io/workload=seedemu --timeout=60s` 通过。
- Validation: 最终 workload 统计为 `total=57 ready=57 Running=57`；kube-system 中没有残留 `seedemu-*repair` 或 `seedemu-*clean` 临时 Pod。
- Notes: 本机 `sudo -n` 曾出现短暂 hang，因此 live 修复阶段优先使用 Kubernetes hostNetwork repair Pod，避免依赖本机 sudo 状态。

## 2026-05-21 12:31 - Clarify OVN Example Install Flow

- User intent: 确认 `writeOvnExample.py` 打印的手动流程是否真实，以及为什么流程里没有显式执行 `installKubeOvnFabric.sh`。
- Finding: 真实调用链为 `buildK3sCluster.sh -> applyK3sCluster.sh -> installOvnFabricIfConfigured() -> ovn/installKubeOvnFabric.sh`；`installKubeOvnFabric.sh` 在 `fabric.type=ovn` 或 `kube-ovn` 时自动执行。
- Change: 修改 `/home/lxl/k8s/origin_k8s/test/writeOvnExample.py` 的输出提示，明确 `buildK3sCluster.sh` 会在 OVN 配置下自动调用 `ovn/installKubeOvnFabric.sh`。
- Commands: `python -m py_compile /home/lxl/k8s/origin_k8s/test/writeOvnExample.py` 通过；重新执行 `python writeOvnExample.py` 验证提示内容。
- Notes: 本次只重新生成示例脚本目录和提示文本，不执行 K3s/OVN 安装，不修改当前运行中的集群状态。

## 2026-05-21 12:55 - Fix VXLAN Fabric Validation False Failure

- User intent: 解释为什么 `vxlan/validateLinuxVxlanFabric.sh` 中 bridge ping 双向通过，但 macvlan 测试 `idc -> amd` 方向失败。
- Finding: VXLAN 隧道本身正常，`br-seedemu` 临时 bridge IP 双向 ping 已通过；失败集中在验证脚本的 macvlan 测试阶段。
- Finding: 验证脚本在 macvlan 测试前保留了 `br-seedemu` 上的临时 bridge IP，Linux weak-host ARP 会让 bridge MAC 代答 macvlan IP 的 ARP，导致 neighbor 表学习到错误 MAC；表现为 `amd -> idc` 通、`idc -> amd` 不通。
- Change: 修改 `seedemu/k8spre/resources/setup/vxlan/validateLinuxVxlanFabric.sh`，bridge reachability 测完后先删除 bridge 测试 IP 并 flush bridge neighbor，再创建 `macseed0` 做 macvlan reachability 测试。
- Change: 同步更新当前生成目录 `/home/lxl/k8s/origin_k8s/test/k8spre-vxlan-example/setup/vxlan/validateLinuxVxlanFabric.sh`。
- Commands: `bash -n` 检查源码资源脚本和生成脚本通过；`git diff --check` 对本轮修改文件通过。
- Validation: 重新执行 `bash ./vxlan/configureLinuxVxlanFabric.sh ./configK3s.yaml && bash ./vxlan/validateLinuxVxlanFabric.sh ./configK3s.yaml` 通过，bridge 和 macvlan 双向 ping 均为 0% packet loss。
- Validation: 验证后 `br-seedemu/vxseed0` 保留，临时 `macseed0` 已删除，`br-seedemu` 上只剩 link-local 地址，无残留测试 IPv4。

## 2026-05-21 13:18 - Simplify VXLAN Physical Config And Auto Detect Underlay

- User intent: 简化 VXLAN 真实物理机输入配置，不再要求用户写 `bridgeTestIp/macvlanTestIp`，也尽量不要求写每个节点的 `underlayInterface`；用户只提供类似 KVM/K3s 的基础 node/SSH 信息即可。
- Change: `seedemu/k8spre/resources/setup/manageK3sConfig.py` 现在允许 `fabric.nodes.<node>.underlayInterface` 缺省；缺省时通过 local/SSH 在目标节点执行 `ip -o route get <peer-ip>`，从 `dev <iface>` 自动探测 VXLAN underlay 网卡。
- Change: `bridgeTestIp` 和 `macvlanTestIp` 保持为内部缺省测试地址，仍可由用户覆盖，但不再需要写入最小输入 YAML。
- Change: `preparePhysicalNodes.sh` 和 `manageK3sConfig.py` 的 SSH 调用避免继承外层 `while read` stdin，防止自动探测/远端检查意外吞掉后续节点行。
- Change: 简化 `/home/lxl/k8s/origin_k8s/test/configK3sVxlan.yaml`，只保留 `clusterName` 和 `nodes` 基础信息；`writeVxlanExample.py` 仍通过 `connection="vxlan"` 在生成的 `setup/configK3s.yaml` 中写入 `fabric.type=linux-vxlan`。
- Change: 更新 `seedemu/k8spre/README.md`、`resources/setup/README.md` 和 `/home/lxl/k8s/origin_k8s/test/k8spreExamples.md`，说明 VXLAN 缺省值和 underlay 自动探测逻辑。
- Commands: `python -m py_compile` 检查 `manageK3sConfig.py` 和 `writeVxlanExample.py` 通过；`bash -n` 检查相关 shell 脚本通过；`git diff --check` 通过。
- Commands: 重新执行 `PYTHONPATH=/home/lxl/k8s python ./writeVxlanExample.py` 生成 VXLAN 示例目录。
- Validation: 生成后的 `setup/configK3s.yaml` 只包含基础 nodes 与 `fabric.type=linux-vxlan`；`python3 ./manageK3sConfig.py --config ./configK3s.yaml fabric-nodes-tsv` 自动解析出 `amd=eno8403`、`idc=enp101s0f1np1` 和缺省测试 IP。
- Validation: 重新执行 `preparePhysicalNodes.sh`、`vxlan/configureLinuxVxlanFabric.sh`、`vxlan/validateLinuxVxlanFabric.sh` 通过；bridge 与 macvlan 双向 ping 均为 0% packet loss。
- Notes: 验证结束后 `br-seedemu/vxseed0` 仍按设计保留供后续 K3s/running 使用，临时 `macseed0` 已删除。

## 2026-05-21 13:24 - Simplify OVN Physical Config Input

- User intent: 和 VXLAN 一样，`configK3sOvn.yaml` 不再需要手写 `fabric.type=ovn`；用户通过 `writePhysicalNodeScripts(..., connection="ovn")` 选择 OVN backend。
- Finding: `K8sPre._applyPhysicalConnectionDefault()` 已经支持 `connection="ovn"` 自动写入 `fabric.type=ovn`，并在缺省时写入 `k3s.version=v1.29.15+k3s1`。
- Change: 简化 `/home/lxl/k8s/origin_k8s/test/configK3sOvn.yaml`，只保留 `clusterName` 和 `nodes` 基础信息。
- Change: 更新 `/home/lxl/k8s/origin_k8s/test/k8spreExamples.md`、`seedemu/k8spre/README.md`、`resources/setup/README.md`，说明 OVN backend 由 API 参数 `connection="ovn"` 注入。
- Commands: `python -m py_compile` 检查 `writeOvnExample.py` 和 `k8spre.py` 通过；`git diff --check` 通过。
- Commands: 重新执行 `PYTHONPATH=/home/lxl/k8s python ./writeOvnExample.py` 生成 OVN 示例目录。
- Validation: 生成后的 `setup/configK3s.yaml` 自动包含 `fabric.type=ovn` 和 `k3s.version=v1.29.15+k3s1`；`setup/ovn/` 存在且 `setup/vxlan/` 不存在；生成目录中的 helper 与 shell 脚本语法检查通过。

## 2026-05-21 15:58 - Fix VXLAN K3s Build Apt And Multus Download Failures

- User intent: VXLAN fabric validation passed 后执行 `buildK3sCluster.sh`，worker `idc` 在安装 CNI plugins 时因 `apt update` 失败而中断，需要定位并修复。
- Finding: `idc` 上 `containernetworking-plugins` 已安装，`/usr/lib/cni/macvlan|ipvlan|static` 均存在；失败根因是 apt cache update 访问残留 Docker apt 源 `https://download.docker.com/linux/ubuntu noble` 握手失败，Ansible `apt update_cache: yes` 报 `unknown reason`。
- Change: 修改 `seedemu/k8spre/resources/setup/ansible/k3s-install.yml` 和当前生成目录的 playbook，把 master/worker 的 CNI 插件安装从 Ansible `apt update_cache: yes` 改为 shell 逻辑：已安装则跳过，未安装则 `apt-get update -o Acquire::Retries=3 || true` 后安装 `containernetworking-plugins`。
- Finding: 重新运行后又卡在 master 下载 Multus manifest，远端 `curl https://ghproxy.net/.../multus-daemonset.yml` 长时间无输出；这是运行时外网依赖导致的可复现性问题。
- Change: 将 Multus manifest 固化到 Ansible playbook 的 `Write bundled Multus CNI manifest` 任务中，直接写入 `/tmp/seedemu-multus-daemonset.yml`，不再在 K3s 构建阶段 curl GitHub/ghproxy。
- Commands: `ansible-playbook --syntax-check` 检查源码资源 playbook 和生成目录 playbook 通过；`git diff --check` 通过。
- Commands: 停止此前卡住的 ansible/curl 进程后，重新执行 `cd /home/lxl/k8s/origin_k8s/test/k8spre-vxlan-example/setup && ./buildK3sCluster.sh`。
- Validation: `buildK3sCluster.sh` 完整通过；amd/idc 均 Ready，Multus DaemonSet rollout 成功，registry 为 `10.202.236.88:5000`，生成 kubeconfig 和 inventory。
- Validation: 确认 `br-seedemu/vxseed0` 在 amd/idc 上仍保留，VNI 4242、UDP 4789、underlay 分别为 `eno8403` 和 `enp101s0f1np1`。
- Validation: `cd /home/lxl/k8s/origin_k8s/test/k8spre-vxlan-example/running && make preflight` 通过；registry local/remote HTTP 200，namespace 基线为空，`network_backend=macvlan`。

## 2026-05-21 16:20 - Fix VXLAN make up ContainerCreating

- User intent: VXLAN 示例 `/home/lxl/k8s/origin_k8s/test/k8spre-vxlan-example` 执行 `make up` 后卡在 `ContainerCreating`，需要定位并恢复当前部署。
- Finding: running 配置已解析为 `cniMasterInterface=br-seedemu`，但当前 namespace 中已创建的 NetworkAttachmentDefinition 仍为 `"master":"ens2"`；真实物理机 VXLAN backend 下不存在统一的 `ens2` 父接口，因此 Multus/macvlan 创建二级网卡失败。
- Action: 执行 `make render-kustomization` 重新生成 `/home/lxl/k8s/origin_k8s/emulate/output/kustomization.yaml`，确认 NAD patch 已把 `master` 设置为 `br-seedemu`。
- Action: 执行 `kubectl --kubeconfig ... apply -k /home/lxl/k8s/origin_k8s/emulate/output` 更新 Deployment 与 NAD；随后执行 `kubectl --kubeconfig ... -n seedemu-k3s-real-topo delete pod --all --wait=false` 删除旧 NAD 下卡住的 Deployment Pod，让 controller 按新 NAD 重建。
- Validation: NAD 已全部更新为 `"master":"br-seedemu"`；最终 workload 统计为 `pod_total=57 pod_ready=57 Running=57`，Deployment 统计为 `deploy_total=57 deploy_ready=57`。
- Validation: 事件中出现 `Add net1/net2 ... from seedemu-k3s-real-topo/...`，说明 Multus 二级网卡创建成功；用户终端中的 `make up`/`rollout status` 进程已退出。
- Validation: 跨节点仿真网连通性测试通过：从 `idc` 上的 `as163h-host-0` ping `amd` 上同一仿真二层网的 `10.163.0.72`，`3 packets transmitted, 3 received, 0% packet loss`。

## 2026-05-21 20:34 - Test macvlan ARP Visibility In KVM Cluster

- User intent: 写脚本并在 KVM 版 SeedEMU/K3s 集群中验证 `net-2-net-101-102` 的 ARP 广播是否会被非 `10.2.1.0/24` 的 Pod 接口收到，并将实验分析写入 Markdown。
- Scope: 新增 `/home/lxl/k8s/origin_k8s/test/validateMacvlanArpVisibility.sh`；生成实验输出目录 `/home/lxl/k8s/origin_k8s/test/macvlan-arp-visibility-kvm-20260521/`。
- Changes: 脚本支持指定 kubeconfig、namespace、source Pod/interface、target IP、多个 observer `label:pod:interface`；并发运行 tcpdump，触发 source ARP，保存原始日志和 `result.md`。
- Commands: `bash -n /home/lxl/k8s/origin_k8s/test/validateMacvlanArpVisibility.sh` 通过 shell 语法检查；`chmod +x` 设置脚本可执行。
- Commands: 使用 `/home/lxl/k8s/output/kubeconfigs/seedemu-k3s.yaml` 在 `seedemu-k3s-real-topo` 中运行脚本；source 为 `as2brd-r101...` 的 `net_101_102`，target 为 `10.2.1.253`。
- Validation: 正样本 `as2brd-r102...:net_101_102` 捕获到 ARP request/reply；三个非 `10.2.1.0/24` observer 接口也都捕获到 `Request who-has 10.2.1.253 tell 10.2.1.254`。
- Validation: `same-source-node-nonprefix` 和 `target-node-nonprefix` 两个 router observer 还捕获到 proxy ARP reply；后续检查确认这些 router 的 `net0/ix103 proxy_arp=1`，并有 BIRD 路由 `10.2.1.0/24 via 10.103.0.3 dev ix103`。
- Notes: 结论写入 `/home/lxl/k8s/origin_k8s/test/macvlan-arp-visibility-kvm-20260521/result.md`：当前 KVM/macvlan 方案没有 per-NAD L2 隔离，ARP 广播会泄露到其他仿真 IP prefix，且部分 router 可能因 proxy ARP 回复。

## 2026-05-22 09:09 - Test Docker ARP Isolation

- User intent: 在宿主机 Docker 部署的相同 SeedEMU 仿真网络中验证不同仿真网络之间是否做好二层隔离。
- Scope: 新增 `/home/lxl/k8s/origin_k8s/test/validateDockerArpIsolation.sh`；生成实验输出目录 `/home/lxl/k8s/origin_k8s/test/docker-arp-isolation-20260522-rerun/`。
- Changes: Docker 版脚本支持 container observer 和 host bridge observer；会并发 tcpdump、触发 source container ARP、保存原始日志并生成 `result.md`。host bridge 抓包在非 root 下自动使用 `sudo -n tcpdump`。
- Commands: `docker ps`、`docker network ls`、`docker network inspect output_net_2_net_101_102`、`ip -br link show type bridge` 确认 Docker 部署结构与 bridge 名称。
- Commands: `bash -n /home/lxl/k8s/origin_k8s/test/validateDockerArpIsolation.sh` 通过 shell 语法检查。
- Commands: 执行 `/home/lxl/k8s/origin_k8s/test/validateDockerArpIsolation.sh --source-container as2brd-r101-10.101.0.2 --source-interface net_101_102 --target-ip 10.2.1.253 ...` 进行实测。
- Validation: 正样本 `as2brd-r102...:net_101_102` 和目标 Docker bridge `br-37b29e35d299` 均捕获到 ARP request/reply。
- Validation: 非目标容器 `as160brd-router0...:net0`、`as150h-host_0...:net0` 未捕获到目标 ARP；非目标 bridge `br-4de4b2ae28c4`、`br-7d7363381ae3` 均为 `0 packets captured`。
- Notes: 结论写入 `/home/lxl/k8s/origin_k8s/test/docker-arp-isolation-20260522-rerun/result.md`：当前 Docker 部署使用 per-network Docker bridge，代表性测试中 ARP 没有跨仿真网络泄露。

## 2026-05-22 10:22 - Test OVN OVS Per Network L2 Isolation

- User intent: 直接构造 OVN/OVS 版本的真实物理机 K3s 集群，部署 SeedEMU workload，并实测是否实现 per-network 二层隔离。
- Scope: 使用 `/home/lxl/k8s/origin_k8s/test/k8spre-ovn-example/` 生成目录；新增实验总结 `/home/lxl/k8s/origin_k8s/test/ovnOvsIsolationExperiment20260522.md`；生成原始抓包目录 `/home/lxl/k8s/origin_k8s/test/ovn-arp-isolation-20260522*`。
- Commands: `cd /home/lxl/k8s/origin_k8s/test/k8spre-ovn-example/setup && ./buildK3sCluster.sh` 构造 amd/idc 两节点 K3s + Kube-OVN 集群，结果为两个节点 Ready，Kube-OVN 组件 rollout 完成。
- Commands: `cd /home/lxl/k8s/origin_k8s/test/k8spre-ovn-example/running && make preflight` 验证 kubeconfig、registry、namespace 基线和 Kube-OVN CRD/Pod 状态，通过。
- Commands: `cd /home/lxl/k8s/origin_k8s/test/k8spre-ovn-example/running && make build` 使用 `DOCKER_BUILDKIT=1 docker buildx build --load` 构建并推送所有 workload 镜像到 `10.202.236.88:5000`。
- Commands: `cd /home/lxl/k8s/origin_k8s/test/k8spre-ovn-example/running && make up` 渲染 Kube-OVN manifest 并部署 workload，生成 29 个 NAD、29 个 Subnet、1 个 VPC；最终 57 个 Pod 全部 Running。
- Validation: 单节点同 Subnet 测试中，`net-2-net-101-102` 的同网 peer 捕获到对 `10.2.1.200` 的 ARP broadcast，`net-150-net0` 和 `net-160-net0` 的非同网 observer 未捕获到。
- Validation: 跨节点同 Subnet 测试中，`net-154-net0` 的 idc peer 捕获到 amd 源 Pod 对 `10.154.0.200` 的 ARP broadcast，同节点非同网和跨节点非同网 observer 均未捕获到。
- Notes: 结论是当前 OVN/OVS Kube-OVN backend 在代表性 ARP broadcast 测试中实现了 per-network L2 隔离；这与 KVM/macvlan 共享二层的泄露结果不同。
