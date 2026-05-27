# K8sPre 测试记录

目标：验证 `seedemu.k8spre` 的脚本生成能力和 YAML 驱动关系。默认 smoke test 不创建 VM、不安装 K3s、不修改已有集群。

## Smoke Test

```bash
cd /home/lxl/k8s
source /home/lxl/anaconda3/etc/profile.d/conda.sh
conda activate seedpy310
python /home/lxl/k8s/origin_k8s/test/k8spre_smoke_test.py
```

本轮结果：

```text
setup_dir=/home/lxl/k8s/origin_k8s/test/k8spre-generated/setup
running_dir=/home/lxl/k8s/origin_k8s/test/k8spre-generated/running
k8spre smoke test passed
```

## 覆盖范围

- `K8sPre.writeKvmInstallScripts()`：只复制 KVM 阶段资源，生成 `kvm.yaml` 和 `installKvmVms.sh`，不会生成 K3s 构建入口。
- `K8sPre.writeK3sBuildScripts()`：只复制 K3s 阶段资源，生成 `buildK3sCluster.sh`，不会生成 `kvm.yaml` 或 `installKvmVms.sh`；如果同目录已有 KVM 阶段文件，会保留 `configK3s.yaml`、`kvmState.yaml`、`kvm.yaml` 等状态文件。
- `K8sPre.writeRunningScripts()`：复制 `running/` 资源，生成 `configRunning.yaml`，不改写 Makefile 常量。
- `config` YAML 优先级：YAML 中已有字段优先，Python 参数只补齐缺省字段。
- 输出路径：生成的 `kvm.yaml` 会写入 kubeconfig、inventory、`configK3s.yaml`、`kvmState.yaml`、cloud-init、disk 等路径；`configRunning.yaml` 只由 `writeRunningScripts()` 写入 `running/`。
- 资源裁剪：package resource 不再依赖 `kvm_template.yaml`、`k3s_template.yaml`、`seedemu-k3s.env.sh`。

## 生成目录

```text
/home/lxl/k8s/origin_k8s/test/k8spre-generated/
  setup/                         # smoke test 中先生成 KVM 阶段，再叠加 K3s 阶段
    kvm.yaml                     # KVM 阶段输入
    installKvmVms.sh             # KVM 阶段入口
    prepareHostAssets.sh
    createKvmVms.sh
    tuneVmLimits.sh
    destroyKvmVms.sh
    manageKvmConfig.py
    buildK3sCluster.sh           # K3s 阶段入口
    applyK3sCluster.sh
    manageK3sConfig.py
  running/
    configRunning.yaml
    Makefile
    manageK8sManifest.py
    buildRegistryImages.sh
    validateClusterPreflight.sh
```

## 真实 E2E 用法

```bash
cd /home/lxl/k8s
source /home/lxl/anaconda3/etc/profile.d/conda.sh
conda activate seedpy310
python /home/lxl/k8s/origin_k8s/test/k8spre_e2e_flow.py
```

E2E 会真实执行：

```bash
cd /home/lxl/k8s/origin_k8s/test/k8spre-e2e/setup
bash installKvmVms.sh
bash buildK3sCluster.sh

cd ../running
make preflight
make build
make up
```

注意：E2E 会创建 KVM、安装 K3s、配置 registry、部署 workload，只能在确认没有残留 VM 冲突时执行。

## 当前 API

- `writeKvmInstallScripts(path, ...)`
- `installKvmVms(path=None, config=None, overwrite=True, **kwargs)`
- `writeK3sBuildScripts(path, config=None, overwrite=False)`
- `buildK3sCluster(path=None, config=None, overwrite=True)`
- `writeRunningScripts(path, output_dir=None, image_registry_prefix="seedemu", rollout_timeout_seconds=1800, overwrite=False)`

旧 API 仍作为兼容别名保留：`kvminstall_script`、`kvminstall`、`k8sbuild_script`、`k8sbuild`、`running_scripts`。

## 配置关系

`kvm.yaml` 用于 KVM 创建。`installKvmVms.sh` 创建 VM 后会生成用户可读的 `configK3s.yaml` 和内部清理用的 `kvmState.yaml`。

`configK3s.yaml` 是 K3s 构建唯一输入。已有 VM 场景只需要为每台机器写 `role`、`ip`、`ssh.user`、`ssh.key`。`name` 可省略；如果写了或由脚本生成，它会被配置成 K3s `node-name`，也就是 `kubectl get nodes` 看到的名字。

`configRunning.yaml` 是 running/Makefile 唯一输入，并且只应位于 `running/` 目录。Makefile 从它找到 `outputDir`，再通过 `setupConfig` 读取 kubeconfig、registry、SSH key。若 `configK3s.yaml` 没有显式 `registry.host`，running 会把唯一 `role: master` 节点的 `ip` 作为 registry host，并用该 master 节点的 `ssh.user` / `ssh.key` 执行远端 build。

## 未覆盖范围

- Smoke test 不调用 `installKvmVms()` 或 `buildK3sCluster()`。
- Smoke test 不执行 `installKvmVms.sh` 或 `buildK3sCluster.sh`。
- Smoke test 不创建 VM、不安装 K3s、不修改已有 namespace。
