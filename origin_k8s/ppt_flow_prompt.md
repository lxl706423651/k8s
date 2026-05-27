# PPT 流程图生成 Prompt

下面这段可以直接给另一个模型，用来生成 PPT 流程图和说明页。

```text
请基于下面信息，帮我生成一组适合 PPT 的流程说明和流程图。要求图清晰、概括、工程化，不要写成长篇文档。重点展示 origin_k8s 下 setup / emulate / running 三部分如何协作，以及 setup 阶段内部流程。

项目路径：
/home/lxl/k8s/origin_k8s

总体目标：
这是一个 KVM + K3s + Kubernetes native SEED 仿真实验流程。整体分三层：
1. setup：创建 VM、打开系统限制、安装 K3s、准备 registry 和基础镜像。
2. emulate：编译仿真拓扑，生成 Kubernetes workload 产物。
3. running：对编译产物执行 preflight、build、up、clean。

请生成：
1. 一张总流程图：setup -> emulate -> running。
2. 一张 setup 内部流程图。
3. 一张 running 内部流程图。
4. 每张图旁边配简短 bullet 说明。
5. 风格适合放进一页或两页 PPT，信息密度中等，不要太细碎。

总流程如下：

[setup]
输入：
- kvm_template.yaml：描述要创建的 KVM VM 资源，例如 master/worker 的 vCPU、memory、disk、worker count。
- 可选 k3s_template.yaml：如果不使用 resolved TSV，也可以显式指定已有 VM 组成 K3s 集群。

核心脚本：
- cluster_config.py：解析 KVM YAML，读取已有 libvirt VM/DHCP 信息，自动规划不冲突的 VM name/IP/MAC。
- kvm_from_yaml.sh：根据 YAML 创建 KVM VM，生成 cloud-init、qcow2 disk、DHCP reservation，并等待 SSH ready。
- unlock_vm_limits_from_yaml.sh：通过 SSH 打开每台 VM 的系统限制和网络内核参数，例如 nofile/nproc/netns/neigh/conntrack/cni0 hash_max。
- k3s_config.py：解析 K3s 节点输入，生成 Ansible inventory、cluster inventory 和 env 文件。
- k3s_from_yaml.sh：把已有 VM 组装成 K3s 集群，安装 K3s/Multus/macvlan，启动 master 本地 registry，并准备 bootstrap/base images。
- clean_kvm_from_resolved.sh：按 resolved TSV 清理本轮创建的 VM、磁盘、cloud-init、DHCP reservation/lease、K3s 输出文件。

核心中间/输出文件：
- base/jammy-server-cloudimg-amd64.img：Ubuntu cloud image，用作 KVM VM 基础系统镜像。
- cloud-init/<vm>/user-data.yaml、meta-data.yaml：VM 首次启动初始化配置。
- kvm_template.resolved-nodes.tsv：实际创建出来的 VM 清单，包含 name/role/ip/mac/vcpus/memory/disk。
- image-cache/*.tar：宿主机准备好的 registry、Multus、Ubuntu、SEED base/router 镜像 tar，用于导入 VM，避免新 VM 从公网拉关键镜像。
- seedemu-k3s.kubeconfig.yaml：宿主机 kubectl 访问新 K3s 集群的 kubeconfig。
- seedemu-k3s.env.sh：setup 给 running 暴露的最小连接文件，主要包含 registry host/port 和 kubeconfig 路径。
- seedemu-k3s.inventory.yaml：解释性集群 inventory，记录节点、K3s CIDR/max-pods、registry 等信息。

setup 阶段逻辑：
1. kvm_from_yaml.sh 读取 kvm_template.yaml。
2. cluster_config.py 读取已有 libvirt VM、DHCP reservation、DHCP lease，规划不冲突的 VM name/IP/MAC。
3. kvm_from_yaml.sh 生成 kvm_template.resolved-nodes.tsv、cloud-init、qcow2 disk，并创建/启动 VM。
4. unlock_vm_limits_from_yaml.sh 读取 resolved TSV，通过 SSH 打开 VM OS/network limits。
5. k3s_from_yaml.sh 读取 resolved TSV。
6. k3s_config.py 生成临时 Ansible inventory。
7. k3s_from_yaml.sh 调用 /home/lxl/k8s/ansible/k3s-install.yml 安装 K3s master/worker、Multus、macvlan。
8. k3s_from_yaml.sh 在 master 上启动 registry。
9. k3s_from_yaml.sh 从宿主机准备并导入关键镜像：registry:2、Multus、ubuntu:20.04、handsonsecurity/seedemu-multiarch-base/router、以及 compiler 使用的 hash tag base images。
10. k3s_from_yaml.sh 输出 kubeconfig/env/inventory，供 running 阶段使用。

setup 的一句话概括：
setup 把一份极简 VM 资源 YAML 转换成一个可运行 SEED Kubernetes workload 的 K3s 集群，并保证 registry、Multus、基础镜像和高密度系统参数都已就绪。

[emulate]
路径：
/home/lxl/k8s/origin_k8s/emulate

核心脚本：
- mini_internet_k8s_native_compile.py：示例编译入口，直接构造 SEED mini internet，并调用 NativeKubernetesCompiler。
- NativeKubernetesCompiler 位于 /home/lxl/k8s/origin_k8s/native_k8s_compiler.py。
- 当前 native baseline 不依赖 inventory，不做 nodeSelector placement，不绑定 K8s node。
- image registry prefix 默认使用逻辑前缀 seedemu，后续 running 用 kustomization 替换成真实 registry IP:port。

核心输出目录：
- emulate/output/

核心输出文件：
- output/k8s.yaml：Kubernetes workload manifest，包含 Deployment、NetworkAttachmentDefinition 等最低 K8s 部署信息。
- output/images.yaml：编译产物中的镜像清单，记录逻辑 image name 与 build context 的关系。
- output/base_images/：compiler 生成的基础镜像构建目录，例如 98a269... 和 39e016...。
- output/<node-image-context>/：每个仿真节点对应的 Docker build context，包含 Dockerfile、bird.conf、interface_setup、start.sh 等。

emulate 的一句话概括：
emulate 把 SEED 拓扑编译成 Kubernetes 原生部署产物，只生成 workload 需要的 YAML 和镜像构建上下文，不负责实际集群和部署。

[running]
路径：
/home/lxl/k8s/origin_k8s/running

核心文件/脚本：
- Makefile：用户入口，提供 preflight/build/up/clean。
- k8s_preflight.sh：检查 output、kubeconfig、K3s 节点、kube-system、namespace baseline、registry、buildx、worker 到 registry 连通性。
- k8s_local_build.sh：在 registry 所在 master 上执行 docker buildx build --load，并 push workload images 到 registry。
- k8s_make_helper.py：解析 images.yaml/k8s.yaml，生成 kustomization.yaml、deployment 列表、namespace 等。
- clean 操作：删除 workload namespace，但不清 KVM/K3s 集群。

Makefile 从 setup 读取：
- ../setup/seedemu-k3s.env.sh
- 由其中的 SEED_REGISTRY_HOST/SEED_REGISTRY_PORT 得到 REGISTRY_PREFIX。
- 由 SEED_OUTPUT_KUBECONFIG 得到 KUBECONFIG。

running 阶段流程：
1. make preflight
   - 读取 output/k8s.yaml 和 output/images.yaml。
   - 读取 setup/seedemu-k3s.env.sh。
   - 检查 kubeconfig、节点 Ready、Multus/kube-system、namespace 不存在、registry 可达、master docker buildx 可用。
2. make build
   - 上传 emulate/output 和 running 脚本到 registry master。
   - 在 master 上运行 k8s_local_build.sh。
   - 使用 buildx + BuildKit 构建 base_images 和每个 workload image。
   - push 到 REGISTRY_PREFIX，例如 192.168.122.122:5000。
3. make up
   - 根据 images.yaml 生成 output/kustomization.yaml。
   - 用 kustomize 把逻辑 image prefix seedemu 替换为真实 registry prefix。
   - kubectl apply -k output。
   - 等待所有 deployment rollout 和 pod Ready。
4. make clean
   - 删除 workload namespace seedemu-k3s-real-topo。
   - 不删除 K3s 集群、不删除 KVM VM。

running 的一句话概括：
running 读取 emulate 的编译产物和 setup 的集群连接信息，完成部署前检查、镜像构建推送、K8s apply、ready 等待和 workload 清理。

建议总流程图使用 Mermaid 或 PPT 方框图：

总流程：
KVM YAML -> setup -> kubeconfig/env/registry-ready K3s
SEED topology script -> emulate -> output/k8s.yaml + images.yaml + build contexts
setup outputs + emulate outputs -> running -> preflight -> build -> up -> running pods

setup 内部流程：
kvm_template.yaml
  -> cluster_config.py
  -> kvm_from_yaml.sh
  -> resolved-nodes.tsv + VM/cloud-init/disk
  -> unlock_vm_limits_from_yaml.sh
  -> tuned VMs
  -> k3s_from_yaml.sh + k3s_config.py + ansible/k3s-install.yml
  -> K3s + Multus + registry + image-cache
  -> seedemu-k3s.kubeconfig.yaml + seedemu-k3s.env.sh

running 内部流程：
output/k8s.yaml + output/images.yaml + setup/seedemu-k3s.env.sh
  -> make preflight
  -> make build -> master buildx -> local registry
  -> make up -> kustomization.yaml -> kubectl apply -k
  -> deployments/pods Ready
  -> make clean -> delete namespace

请输出：
1. PPT 页标题建议。
2. 每页的图结构。
3. Mermaid 代码版本的流程图。
4. 每页不超过 6 条 bullet。
5. 术语保持工程准确：KVM VM、cloud-init、K3s、Multus/macvlan、local registry、kubeconfig、kustomization、buildx/BuildKit。
```
