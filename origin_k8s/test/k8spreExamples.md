# K8sPre Physical Examples

This directory contains two minimal physical-node examples for the same amd/idc
machines. They only generate script directories; they do not execute cluster
installation or workload deployment.

It also contains a KVM + OVN example. That flow first creates KVM guests, then
builds K3s with Kube-OVN as the non-primary CNI for SeedEMU secondary networks.

## VXLAN example

Input config:

- `configK3sVxlan.yaml`

The VXLAN input file only needs cluster/node/SSH basics. `writeVxlanExample.py`
selects `connection="vxlan"` for the generated setup directory, and the setup
helper fills VXLAN defaults such as `br-seedemu`, `vxseed0`, VNI `4242`, UDP
port `4789`, and temporary validation IPs. If `underlayInterface` is omitted,
the generated scripts auto-detect it on each node with `ip -o route get
<peer-ip>`.

Generate scripts:

```bash
cd /home/lxl/k8s/origin_k8s/test
python3 ./writeVxlanExample.py
```

Manual execution after generation:

```bash
cd /home/lxl/k8s/origin_k8s/test/k8spre-vxlan-example/setup
bash ./preparePhysicalNodes.sh ./configK3s.yaml
bash ./vxlan/configureLinuxVxlanFabric.sh ./configK3s.yaml
bash ./vxlan/validateLinuxVxlanFabric.sh ./configK3s.yaml
bash ./buildK3sCluster.sh

cd ../running
make preflight
make build
make up
```

## OVN example

Input config:

- `configK3sOvn.yaml`

The OVN input file also only needs cluster/node/SSH basics. `writeOvnExample.py`
selects `connection="ovn"` for the generated setup directory, and K8sPre writes
`fabric.type=ovn` plus the K3s version required by the bundled Kube-OVN
installer into `setup/configK3s.yaml`.

Generate scripts:

```bash
cd /home/lxl/k8s/origin_k8s/test
python3 ./writeOvnExample.py
```

Manual execution after generation:

```bash
cd /home/lxl/k8s/origin_k8s/test/k8spre-ovn-example/setup
bash ./preparePhysicalNodes.sh ./configK3s.yaml
bash ./buildK3sCluster.sh
bash ./ovn/validateKubeOvnFabric.sh ./configK3s.yaml

cd ../running
make preflight
make build
make up
```

## Difference

- VXLAN mode creates a Linux `br-seedemu + vxseed0` fabric first, then SeedEMU
  secondary interfaces use Multus/macvlan on `br-seedemu`.
- OVN mode does not create a host bridge fabric. K3s is built first, then the
  setup flow installs Kube-OVN as a secondary CNI and running converts SeedEMU
  network attachments to Kube-OVN layer-2 resources.

## KVM + OVN example

Input config:

- `configKvmOvn.yaml`

Generate scripts:

```bash
cd /home/lxl/k8s/origin_k8s/test
python3 ./writeKvmOvnExample.py
```

Manual execution after generation:

```bash
cd /home/lxl/k8s/origin_k8s/test/k8spre-kvm-ovn-example/setup
bash ./installKvmVms.sh
bash ./buildK3sCluster.sh
bash ./ovn/validateKubeOvnFabric.sh ./configK3s.yaml

cd ../running
make preflight
make build
make up
```

This example uses the same KVM creation scripts as the default KVM flow, but
passes `connection="ovn"` to `writeKvmInstallScripts()`. The generated KVM
stage writes `fabric.type=ovn` into `kvm.yaml`, and the KVM helper preserves
that field when it generates `configK3s.yaml`.
