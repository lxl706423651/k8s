#!/usr/bin/env python3
"""Build per-node Docker archives for compiled SeedEMU K8s images.

The compiled B62 workloads all use the same router base image and differ only
by small runtime configuration files. Building and pushing thousands of nearly
identical Dockerfiles is slow, so this tool creates Docker-compatible archives
directly: each archive contains the shared base layers once and one small layer
per target image. K3s/containerd can import these archives with
`k3s ctr images import`.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import time
from typing import Dict, Iterable, List, Tuple


BUILD_RE = re.compile(r"^seedemu_build_and_push\s+(\S+)\s+(\S+)\s*$")

COPY_MAP = (
    ("92872f20cfb75af4e3e1c588b00d6484", "replace_address.sh", None),
    ("365d27384adc9719099d1f63f2d15824", "root/.zshrc.pre", None),
    ("082b96ec819c95ae773daebde675ef80", "start.sh", 0o755),
    ("d18858afc6bb66ec3a19d872077acfd2", "seedemu_sniffer", 0o755),
    ("17ac2d812a99a91e7f747e1defb72a29", "seedemu_worker", 0o755),
    ("2b0ae038330eccd43095538618caee7d", "etc/bird/bird.conf", None),
    ("8e9a384ab59c7626a3ffab63886cc30f", "etc/bird/conf/kernel.conf", None),
    ("e01e36443f9f72c6204189260d0bd276", "ifinfo.txt", None),
    ("d3d51fdf7f4bad30dc5db560a01ce629", "interface_setup", None),
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_build_images(path: Path) -> Dict[str, Path]:
    mapping: Dict[str, Path] = {}
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            match = BUILD_RE.match(line.strip())
            if not match:
                continue
            image, context = match.groups()
            mapping[image] = Path(context)
    if not mapping:
        raise RuntimeError(f"no seedemu_build_and_push entries found in {path}")
    return mapping


def run(cmd: List[str], *, stdout=None) -> None:
    subprocess.run(cmd, check=True, stdout=stdout)


def docker_image_exists(image: str) -> bool:
    return subprocess.run(
        ["docker", "image", "inspect", image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def mirror_image_name(image: str) -> str:
    mirror = os.environ.get("SEED_DOCKER_IO_MIRROR_ENDPOINT", "https://docker.m.daocloud.io")
    if not mirror:
        return image
    mirror_host = mirror.removeprefix("https://").removeprefix("http://").rstrip("/")
    if "/" in image:
        return f"{mirror_host}/{image}"
    return f"{mirror_host}/library/{image}"


def ensure_source_image(image: str) -> None:
    if docker_image_exists(image):
        return
    mirror = mirror_image_name(image)
    if mirror != image:
        if subprocess.run(["docker", "pull", mirror], check=False).returncode == 0:
            run(["docker", "tag", mirror, image])
            return
    run(["docker", "pull", image])


def dockerfile_from_images(path: Path) -> Iterable[str]:
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.strip().split()
        if len(fields) >= 2 and fields[0].upper() == "FROM":
            yield fields[1]


def ensure_base_image(base_image: str, work_dir: Path) -> None:
    if docker_image_exists(base_image):
        return
    context_dir = work_dir / "base_images" / base_image
    dockerfile = context_dir / "Dockerfile"
    if not dockerfile.exists():
        ensure_source_image(base_image)
        return
    for source in dockerfile_from_images(dockerfile):
        ensure_source_image(source)
    print(f"[fast-archive] building base image {base_image} from {context_dir}", flush=True)
    run(["docker", "build", "-t", base_image, str(context_dir)])


def extract_base_archive(base_image: str, base_dir: Path) -> None:
    marker = base_dir / ".ready"
    if marker.exists() and marker.read_text(encoding="utf-8").strip() == base_image:
        return
    if base_dir.exists():
        shutil.rmtree(base_dir)
    base_dir.mkdir(parents=True)
    proc = subprocess.Popen(["docker", "save", base_image], stdout=subprocess.PIPE)
    assert proc.stdout is not None
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as archive:
            archive.extractall(base_dir)
    finally:
        rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"docker save failed for {base_image} with exit code {rc}")
    marker.write_text(f"{base_image}\n", encoding="utf-8")


def load_base(base_dir: Path) -> Tuple[dict, List[str]]:
    manifest_path = base_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not manifest:
        raise RuntimeError(f"empty base manifest: {manifest_path}")
    entry = manifest[0]
    config_path = base_dir / entry["Config"]
    config = json.loads(config_path.read_text(encoding="utf-8"))
    layers = list(entry["Layers"])
    for layer in layers:
        if not (base_dir / layer).exists():
            raise RuntimeError(f"base layer listed in manifest is missing: {layer}")
    return config, layers


def add_dir(archive: tarfile.TarFile, name: str, mode: int = 0o755) -> None:
    info = tarfile.TarInfo(name.rstrip("/") + "/")
    info.type = tarfile.DIRTYPE
    info.mode = mode
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = 0
    archive.addfile(info)


def add_bytes(
    archive: tarfile.TarFile,
    name: str,
    data: bytes,
    mode: int,
) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    info.mtime = 0
    archive.addfile(info, io.BytesIO(data))


def add_context_file(
    archive: tarfile.TarFile,
    src: Path,
    dest: str,
    mode_override: int | None,
) -> None:
    mode = mode_override
    if mode is None:
        mode = src.stat().st_mode & 0o777
        if mode == 0:
            mode = 0o644
    add_bytes(archive, dest, src.read_bytes(), mode)


def make_layer(context_dir: Path) -> Tuple[str, bytes]:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for dirname in (
            "usr",
            "usr/share",
            "usr/share/doc",
            "usr/share/doc/bird2",
            "usr/share/doc/bird2/examples",
            "etc",
            "etc/bird",
            "etc/bird/conf",
            "root",
        ):
            add_dir(archive, dirname)
        add_bytes(archive, "usr/share/doc/bird2/examples/bird.conf", b"", 0o644)
        for src_name, dest, mode in COPY_MAP:
            src = context_dir / src_name
            if not src.exists():
                raise RuntimeError(f"missing context file {src}")
            add_context_file(archive, src, dest, mode)
    data = buffer.getvalue()
    return sha256_bytes(data), data


def make_config(base_config: dict, layer_diff_id: str) -> Tuple[str, bytes]:
    config = copy.deepcopy(base_config)
    config.setdefault("config", {})
    config["config"]["Cmd"] = ["/start.sh"]
    config.setdefault("rootfs", {}).setdefault("diff_ids", [])
    config["rootfs"]["diff_ids"] = list(config["rootfs"]["diff_ids"]) + [
        f"sha256:{layer_diff_id}"
    ]
    history = list(config.get("history", []))
    history.append(
        {
            "created_by": "fastSeedemuImageArchive.py",
            "comment": "SeedEMU runtime configuration layer",
        }
    )
    config["history"] = history
    data = json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256_bytes(data), data


def image_context(work_dir: Path, context_ref: Path) -> Path:
    if context_ref.is_absolute():
        return context_ref
    return (work_dir / context_ref).resolve()


def infer_base_image(work_dir: Path, image_to_context: Dict[str, Path]) -> str:
    for context_ref in image_to_context.values():
        dockerfile = image_context(work_dir, context_ref) / "Dockerfile"
        if not dockerfile.exists():
            continue
        for source in dockerfile_from_images(dockerfile):
            return source
    raise RuntimeError("cannot infer base image from workload Dockerfiles")


def read_images(path: Path) -> List[str]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def add_file_to_archive(archive: tarfile.TarFile, src: Path, arcname: str) -> None:
    archive.add(src, arcname=arcname, recursive=False)


def write_node_archive(
    *,
    node_name: str,
    images: List[str],
    work_dir: Path,
    base_dir: Path,
    base_config: dict,
    base_layers: List[str],
    image_to_context: Dict[str, Path],
    archive_dir: Path,
) -> dict:
    start = time.monotonic()
    archive_path = archive_dir / f"images_{node_name}.tar"
    manifest_entries = []
    layer_cache: Dict[str, Tuple[str, bytes]] = {}
    config_blobs: Dict[str, bytes] = {}

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, mode="w") as output:
        for layer in base_layers:
            add_file_to_archive(output, base_dir / layer, layer)
        for image in images:
            context_ref = image_to_context.get(image)
            if context_ref is None:
                raise RuntimeError(f"image {image} is missing from build_images.sh")
            context_dir = image_context(work_dir, context_ref)
            layer_diff_id, layer_data = layer_cache.get(str(context_dir), (None, None))  # type: ignore[arg-type]
            if layer_diff_id is None or layer_data is None:
                layer_diff_id, layer_data = make_layer(context_dir)
                layer_cache[str(context_dir)] = (layer_diff_id, layer_data)
            layer_path = f"blobs/sha256/{layer_diff_id}"
            add_bytes(output, layer_path, layer_data, 0o444)

            config_digest, config_data = make_config(base_config, layer_diff_id)
            config_path = f"blobs/sha256/{config_digest}"
            if config_digest not in config_blobs:
                config_blobs[config_digest] = config_data
                add_bytes(output, config_path, config_data, 0o444)
            manifest_entries.append(
                {
                    "Config": config_path,
                    "RepoTags": [image],
                    "Layers": base_layers + [layer_path],
                }
            )
        add_bytes(
            output,
            "manifest.json",
            json.dumps(manifest_entries, indent=2).encode("utf-8"),
            0o644,
        )
    duration = time.monotonic() - start
    return {
        "node": node_name,
        "imageCount": len(images),
        "archive": str(archive_path),
        "archiveBytes": archive_path.stat().st_size,
        "durationSeconds": round(duration, 2),
    }


def iter_node_lists(node_image_dir: Path) -> Iterable[Tuple[str, Path]]:
    for path in sorted(node_image_dir.glob("images_*.txt")):
        node = path.name[len("images_") : -len(".txt")]
        yield node, path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", required=True, type=Path)
    parser.add_argument("--node-image-dir", required=True, type=Path)
    parser.add_argument("--archive-dir", required=True, type=Path)
    parser.add_argument("--base-image", default="")
    parser.add_argument("--base-cache-dir", type=Path)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args()

    work_dir = args.work_dir.resolve()
    node_image_dir = args.node_image_dir.resolve()
    archive_dir = args.archive_dir.resolve()
    base_dir = (args.base_cache_dir or (work_dir / ".fast_base_archive")).resolve()

    build_images = work_dir / "build_images.sh"
    if not build_images.exists():
        raise RuntimeError(f"missing build_images.sh: {build_images}")
    if not node_image_dir.exists():
        raise RuntimeError(f"missing node image directory: {node_image_dir}")

    started = time.monotonic()
    image_to_context = parse_build_images(build_images)
    base_image = args.base_image or infer_base_image(work_dir, image_to_context)
    ensure_base_image(base_image, work_dir)
    extract_base_archive(base_image, base_dir)
    base_config, base_layers = load_base(base_dir)

    results = []
    total_images = 0
    for node_name, list_path in iter_node_lists(node_image_dir):
        images = read_images(list_path)
        if not images:
            results.append(
                {
                    "node": node_name,
                    "imageCount": 0,
                    "archive": "",
                    "archiveBytes": 0,
                    "durationSeconds": 0,
                }
            )
            continue
        result = write_node_archive(
            node_name=node_name,
            images=images,
            work_dir=work_dir,
            base_dir=base_dir,
            base_config=base_config,
            base_layers=base_layers,
            image_to_context=image_to_context,
            archive_dir=archive_dir,
        )
        total_images += len(images)
        results.append(result)
        print(
            f"[fast-archive] {node_name}: {len(images)} images, "
            f"{result['archiveBytes']} bytes, {result['durationSeconds']}s",
            flush=True,
        )

    summary = {
        "status": "PASS",
        "workDir": str(work_dir),
        "nodeImageDir": str(node_image_dir),
        "archiveDir": str(archive_dir),
        "baseImage": base_image,
        "nodeCount": len(results),
        "imageCount": total_images,
        "durationSeconds": round(time.monotonic() - started, 2),
        "nodes": results,
    }
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
