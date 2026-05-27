from __future__ import annotations

import json
import os
import re
import shutil
from hashlib import md5
from os import chdir, mkdir
from pathlib import Path
from typing import Any, Dict, List

import yaml

from seedemu.compiler.Docker import Docker
from seedemu.core import Network, Node


INTERNET_MAP_META_PREFIX = "org.seedsecuritylabs.seedemu.meta."
# Border routers are already represented through the router node path in SEED's
# registry. Compiling "brdnode" again creates duplicate Deployment names.
SEEDEMU_NODE_TYPES = ["rnode", "csnode", "hnode", "rs", "snode"]


class NativeKubernetesCompiler(Docker):
    """Minimal Kubernetes compiler for a k8s-native baseline.

    Design goals:
    - no inventory dependency during compile
    - no nodeSelector / affinity / topology spreading
    - no node-aware preload planning
    - generated orchestration artifacts are limited to k8s.yaml and images.yaml;
      build/deploy scripts live in origin_k8s/running.
    """

    __namespace: str
    __cni_type: str
    __cni_master_interface: str
    __image_pull_policy: str
    __rollout_timeout_seconds: int
    __image_registry_prefix: str
    __manifests: List[Dict[str, Any]]
    __image_entries: List[Dict[str, str]]

    def __init__(
        self,
        image_registry_prefix: str = "seedemu",
        registry_prefix: str | None = None,
        namespace: str = "seedemu-k3s-real-topo",
        cni_type: str = "macvlan",
        cni_master_interface: str = "ens2",
        image_pull_policy: str = "Always",
        rollout_timeout_seconds: int = 1800,
        # use_multus: bool = True,
        # create_namespace: bool = True,
        **kwargs: Any,
    ) -> None:
        kwargs["selfManagedNetwork"] = True
        super().__init__(**kwargs)
        if registry_prefix is not None:
            image_registry_prefix = registry_prefix
        self.__image_registry_prefix = image_registry_prefix.strip().strip("/")
        self.__namespace = namespace.strip() or "seedemu"
        self.__cni_type = cni_type.strip().lower() or "bridge"
        self.__cni_master_interface = cni_master_interface.strip() or "eth0"
        self.__image_pull_policy = image_pull_policy.strip() or "Always"
        self.__rollout_timeout_seconds = int(rollout_timeout_seconds)
        self.__manifests = []
        self.__image_entries = []

    def getName(self) -> str:
        return "NativeKubernetes"

    @staticmethod
    def _safeBridgeName(name: str) -> str:
        return f"br-{md5(name.encode()).hexdigest()[:12]}"

    def _doCompile(self, emulator) -> None:
        registry = emulator.getRegistry()
        self._groupSoftware(emulator)

        self.__manifests.append(
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": self.__namespace},
            }
        )

        for ((_, obj_type, _), obj) in registry.getAll().items():
            if obj_type == "net":
                self.__manifests.append(self._compileNetK8s(obj))

        for ((_, obj_type, _), obj) in registry.getAll().items():
            if obj_type in SEEDEMU_NODE_TYPES:
                self.__manifests.append(self._compileNodeK8s(obj))

        with open("k8s.yaml", "w", encoding="utf-8") as handle:
            yaml.safe_dump_all(self.__manifests, handle, sort_keys=False)

        self._stage_base_image_contexts()
        with open("images.yaml", "w", encoding="utf-8") as handle:
            yaml.safe_dump({"images": self.__image_entries}, handle, sort_keys=False)

    def _compileNetK8s(self, net: Network) -> Dict[str, Any]:
        name = self._getRealNetName(net).replace("_", "-").lower()
        prefix = str(net.getPrefix())

        if self.__cni_type == "macvlan":
            config = {
                "cniVersion": "0.3.1",
                "type": "macvlan",
                "master": self.__cni_master_interface,
                "mode": "bridge",
                "ipam": {"type": "static"},
            }
        elif self.__cni_type == "ipvlan":
            config = {
                "cniVersion": "0.3.1",
                "type": "ipvlan",
                "master": self.__cni_master_interface,
                "mode": "l2",
                "ipam": {"type": "static"},
            }
        elif self.__cni_type == "host-local":
            config = {
                "cniVersion": "0.3.1",
                "type": "bridge",
                "bridge": self._safeBridgeName(f"{self.__namespace}:{name}"),
                "isGateway": False,
                "ipam": {"type": "host-local", "subnet": prefix},
            }
        else:
            config = {
                "cniVersion": "0.3.1",
                "type": "bridge",
                "bridge": self._safeBridgeName(f"{self.__namespace}:{name}"),
                "ipam": {"type": "static"},
            }

        return {
            "apiVersion": "k8s.cni.cncf.io/v1",
            "kind": "NetworkAttachmentDefinition",
            "metadata": {
                "name": name,
                "namespace": self.__namespace,
                "annotations": self._getInternetMapNetMeta(net),
            },
            "spec": {"config": json.dumps(config)},
        }

    def _compileNodeK8s(self, node: Node) -> Dict[str, Any]:
        real_nodename = self._getRealNodeName(node)
        if not os.path.exists(real_nodename):
            mkdir(real_nodename)

        cwd = os.getcwd()
        chdir(real_nodename)
        dockerfile = self._computeDockerfile(node)
        with open("Dockerfile", "w", encoding="utf-8") as handle:
            handle.write(dockerfile)
        self._patch_interface_setup_context(Path("Dockerfile"))
        chdir(cwd)

        full_image_name = f"{self.__image_registry_prefix}/{real_nodename}:latest"
        self.__image_entries.append(
            {
                "name": full_image_name,
                "context": f"./{real_nodename}",
            }
        )

        node_name = self._getComposeNodeName(node).replace("_", "-").lower()
        asn = str(node.getAsn())
        role = self._nodeRoleToString(node.getRole())

        annotations = self._getInternetMapNodeMeta(node)
        net_specs = []
        for iface in node.getInterfaces():
            net = iface.getNet()
            net_name = self._getRealNetName(net).replace("_", "-").lower()
            prefix_len = net.getPrefix().prefixlen
            net_specs.append({"name": net_name, "ips": [f"{iface.getAddress()}/{prefix_len}"]})
        if net_specs:
            annotations["k8s.v1.cni.cncf.io/networks"] = json.dumps(net_specs)

        envs = [{"name": "CONTAINER_NAME", "value": node_name}]
        for opt, _scope in node.getScopedRuntimeOptions():
            envs.append({"name": opt.name.upper(), "value": str(opt.value)})

        labels = {
            "app": node_name,
            "seedemu.io/asn": asn,
            "seedemu.io/role": role,
            "seedemu.io/name": node.getName(),
            "seedemu.io/workload": "seedemu",
        }

        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": node_name,
                "namespace": self.__namespace,
                "labels": labels,
            },
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": node_name}},
                "template": {
                    "metadata": {"labels": labels, "annotations": annotations},
                    "spec": {
                        "containers": [
                            {
                                "name": "main",
                                "image": full_image_name,
                                "imagePullPolicy": self.__image_pull_policy,
                                "securityContext": {
                                    "privileged": True,
                                    "capabilities": {"add": ["ALL"]},
                                },
                                "command": ["/start.sh"],
                                "env": envs,
                                "volumeMounts": [],
                            }
                        ]
                    },
                },
            },
        }

    def _metaKey(self, key: str) -> str:
        return f"{INTERNET_MAP_META_PREFIX}{key}"

    def _nodeInternetMapRole(self, node: Node) -> str:
        _scope, obj_type, _name = node.getRegistryInfo()
        if obj_type == "hnode":
            return "Host"
        if obj_type == "rnode":
            return "Router"
        if obj_type == "brdnode":
            return "BorderRouter"
        if obj_type == "csnode":
            return "SCION Control Service"
        if obj_type == "snode":
            return "Emulator Service Worker"
        if obj_type == "rs":
            return "Route Server"
        return obj_type

    def _getInternetMapNodeMeta(self, node: Node) -> Dict[str, str]:
        _scope, _obj_type, name = node.getRegistryInfo()
        meta = {
            self._metaKey("asn"): str(node.getAsn()),
            self._metaKey("nodename"): str(name),
            self._metaKey("role"): self._nodeInternetMapRole(node),
        }

        if node.getDisplayName() is not None:
            meta[self._metaKey("displayname")] = str(node.getDisplayName())
        if node.getDescription() is not None:
            meta[self._metaKey("description")] = str(node.getDescription())
        if len(node.getClasses()) > 0:
            meta[self._metaKey("class")] = json.dumps(node.getClasses())

        for key, value in node.getLabel().items():
            meta[self._metaKey(key)] = str(value)

        for index, iface in enumerate(node.getInterfaces()):
            net = iface.getNet()
            meta[self._metaKey(f"net.{index}.name")] = str(net.getName())
            meta[self._metaKey(f"net.{index}.address")] = (
                f"{iface.getAddress()}/{net.getPrefix().prefixlen}"
            )

        return meta

    def _getInternetMapNetMeta(self, net: Network) -> Dict[str, str]:
        scope, _obj_type, name = net.getRegistryInfo()
        meta = {
            self._metaKey("type"): "global" if scope == "ix" else "local",
            self._metaKey("scope"): str(scope),
            self._metaKey("name"): str(name),
            self._metaKey("prefix"): str(net.getPrefix()),
        }

        if net.getDisplayName() is not None:
            meta[self._metaKey("displayname")] = str(net.getDisplayName())
        if net.getDescription() is not None:
            meta[self._metaKey("description")] = str(net.getDescription())

        return meta

    def _patch_interface_setup_context(self, dockerfile_path: Path) -> None:
        dockerfile = dockerfile_path.read_text(encoding="utf-8")
        match = re.search(r"^COPY\s+(\S+)\s+/interface_setup\s*$", dockerfile, re.MULTILINE)
        if not match:
            return

        script_path = dockerfile_path.parent / match.group(1)
        if not script_path.exists():
            return

        script_path.write_text(
            """#!/bin/bash
set -euo pipefail

cidr_to_net() {
    ipcalc -n "$1" | sed -E -n 's/^Network: +([0-9]{1,3}\\.[0-9]{1,3}\\.[0-9]{1,3}\\.[0-9]{1,3}\\/[0-9]{1,2}) +.*/\\1/p'
}

tmpfile="$(mktemp)"
trap 'rm -f "$tmpfile"' EXIT

ip -j addr | jq -cr '.[]' | while read -r iface; do
    ifname="$(jq -cr '.ifname' <<< "$iface")"
    jq -cr '.addr_info[]?' <<< "$iface" | while read -r iaddr; do
        addr="$(jq -cr '"\\(.local)/\\(.prefixlen)"' <<< "$iaddr")"
        net="$(cidr_to_net "$addr")"
        [ -z "$net" ] && continue
        line="$(grep -F "$net" ifinfo.txt || true)"
        [ -z "$line" ] && continue
        new_ifname="$(cut -d: -f1 <<< "$line")"
        latency="$(cut -d: -f3 <<< "$line")"
        bw="$(cut -d: -f4 <<< "$line")"
        loss="$(cut -d: -f5 <<< "$line")"
        [ -z "$new_ifname" ] && continue
        [ "$bw" = 0 ] && bw=1000000000000
        printf '%s|%s|%s|%s|%s\\n' "$ifname" "$new_ifname" "$latency" "$bw" "$loss" >> "$tmpfile"
    done
done

i=0
while IFS='|' read -r ifname new_ifname latency bw loss; do
    [ -z "$ifname" ] && continue
    tmp_ifname="seedtmp${i}"
    if [ "$ifname" != "$new_ifname" ]; then
        ip link set "$ifname" down
        ip link set "$ifname" name "$tmp_ifname"
    else
        tmp_ifname="$ifname"
    fi
    printf '%s|%s|%s|%s|%s\\n' "$tmp_ifname" "$new_ifname" "$latency" "$bw" "$loss" >> "${tmpfile}.stage2"
    i=$((i + 1))
done < "$tmpfile"

while IFS='|' read -r tmp_ifname new_ifname latency bw loss; do
    [ -z "$tmp_ifname" ] && continue
    if [ "$tmp_ifname" != "$new_ifname" ]; then
        ip link set "$tmp_ifname" name "$new_ifname"
    fi
    ip link set "$new_ifname" up
    [ -z "$loss" ] && loss=0
    tc qdisc add dev "$new_ifname" root handle 1:0 tbf rate "${bw}bit" buffer 1000000 limit 1000
    tc qdisc add dev "$new_ifname" parent 1:0 handle 10: netem delay "${latency}ms" loss "${loss}%"
done < "${tmpfile}.stage2"

rm -f "${tmpfile}.stage2"
""",
            encoding="utf-8",
        )

    def _stage_base_image_contexts(self) -> None:
        used_images = sorted(getattr(self, "_used_images", set()))
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        base_image_sources = {
            "handsonsecurity/seedemu-multiarch-base:buildx-latest": os.path.join(
                repo_root, "docker_images", "multiarch", "seedemu-base"
            ),
            "handsonsecurity/seedemu-multiarch-router:buildx-latest": os.path.join(
                repo_root, "docker_images", "multiarch", "seedemu-router"
            ),
        }

        staged_any = False
        for image in used_images:
            src = base_image_sources.get(image)
            if not src or not os.path.isdir(src):
                continue
            digest = md5(image.encode("utf-8")).hexdigest()
            dst_root = os.path.join("base_images", digest)
            os.makedirs(os.path.dirname(dst_root), exist_ok=True)
            if os.path.exists(dst_root):
                shutil.rmtree(dst_root)
            shutil.copytree(src, dst_root)
            staged_any = True

        if not staged_any and os.path.isdir("base_images"):
            shutil.rmtree("base_images")
