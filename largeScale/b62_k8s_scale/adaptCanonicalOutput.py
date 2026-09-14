#!/usr/bin/env python3
"""Overlay Kubernetes build contexts with one canonical DockerCompiler output.

The Kubernetes compiler remains responsible for manifests, placement, Multus
attachments, and per-container address replacement.  Every logical node image
is then sourced from the supplied canonical output, with only the generated
``/replace_address.sh`` hook added before the canonical interface setup.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil

import yaml


COPY_PATTERN = re.compile(r"^\s*COPY\s+(\S+)\s+(\S+)\s*$", re.IGNORECASE)
PREPARE_PATTERN = re.compile(
    r'^prepare_dummy_image\s+"[^"]+"\s+"[^"]+"\s*$', re.MULTILINE
)
ROLE_LABEL = "org.seedsecuritylabs.seedemu.meta.role"
RUNTIME_ROLES = {"BorderRouter", "Route Server"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalizeLabels(raw: object) -> dict[str, str]:
    if isinstance(raw, dict):
        return {str(key): str(value) for key, value in raw.items()}
    result: dict[str, str] = {}
    if isinstance(raw, list):
        for item in raw:
            key, separator, value = str(item).partition("=")
            result[key] = value if separator else ""
    return result


def buildContext(service: dict[str, object]) -> str:
    build = service.get("build")
    if isinstance(build, str):
        return build.removeprefix("./")
    if isinstance(build, dict):
        return str(build.get("context") or "").removeprefix("./")
    return ""


def copiedSource(dockerfile: Path, destination: str) -> str:
    for line in dockerfile.read_text(encoding="utf-8").splitlines():
        match = COPY_PATTERN.match(line)
        if match and match.group(2).rstrip("/") == destination.rstrip("/"):
            return match.group(1)
    raise ValueError(f"{dockerfile} does not COPY a file to {destination}")


def loadCanonicalServices(
    canonical_output: Path,
) -> tuple[dict[str, str], dict[str, list[str]], str, str]:
    compose_path = canonical_output / "docker-compose.yml"
    data = yaml.safe_load(compose_path.read_text(encoding="utf-8")) or {}
    services = data.get("services") or {}
    contexts: dict[str, str] = {}
    addresses: dict[str, list[str]] = {}
    dummy_tag = ""
    dummy_base = ""
    for name, raw in services.items():
        service = raw if isinstance(raw, dict) else {}
        role = normalizeLabels(service.get("labels")).get(ROLE_LABEL, "")
        context = buildContext(service)
        if role in RUNTIME_ROLES:
            if not context:
                raise ValueError(f"canonical service {name} has no build context")
            contexts[str(name)] = context
            labels = normalizeLabels(service.get("labels"))
            indexed: list[tuple[int, str]] = []
            for key, value in labels.items():
                match = re.fullmatch(
                    r"org\.seedsecuritylabs\.seedemu\.meta\.net\.(\d+)\.address",
                    key,
                )
                if match:
                    indexed.append((int(match.group(1)), value))
            addresses[context] = [value for _, value in sorted(indexed)]
            if not addresses[context]:
                raise ValueError(f"canonical service {name} has no interface addresses")
            continue
        build = service.get("build")
        if isinstance(build, dict) and str(build.get("context") or "") == ".":
            dockerfile = canonical_output / str(build.get("dockerfile") or "")
            first = dockerfile.read_text(encoding="utf-8").splitlines()[0]
            if not first.startswith("FROM "):
                raise ValueError(f"canonical dummy Dockerfile has no FROM: {dockerfile}")
            dummy_base = first.split(maxsplit=1)[1]
            dummy_tag = str(service.get("image") or name)
    if not dummy_tag or not dummy_base:
        raise ValueError("canonical Compose file has no usable base-image dummy")
    return contexts, addresses, dummy_base, dummy_tag


def adaptContext(
    canonical: Path,
    generated: Path,
    canonical_addresses: list[str],
) -> tuple[str, str]:
    generated_dockerfile = generated / "Dockerfile"
    replacement_source = copiedSource(generated_dockerfile, "/replace_address.sh")
    replacement_content = (generated / replacement_source).read_bytes()
    replacement_addresses = re.findall(
        rb'^configure_iface\s+\d+\s+"([^"]+)"\s*$',
        replacement_content,
        flags=re.MULTILINE,
    )
    decoded_addresses = [value.decode("utf-8") for value in replacement_addresses]
    if decoded_addresses != canonical_addresses:
        raise ValueError(
            f"Kubernetes address adapter differs from canonical context {canonical.name}: "
            f"generated={decoded_addresses} canonical={canonical_addresses}"
        )

    canonical_dockerfile = canonical / "Dockerfile"
    start_source = copiedSource(canonical_dockerfile, "/start.sh")
    bird_source = copiedSource(canonical_dockerfile, "/etc/bird/bird.conf")
    kernel_source = copiedSource(canonical_dockerfile, "/etc/bird/conf/kernel.conf")

    shutil.rmtree(generated)
    shutil.copytree(canonical, generated)
    (generated / replacement_source).write_bytes(replacement_content)

    start_path = generated / start_source
    start_lines = start_path.read_text(encoding="utf-8").splitlines()
    if not start_lines or not start_lines[0].startswith("#!"):
        raise ValueError(f"canonical start script has no shebang: {start_path}")
    if "/replace_address.sh" not in "\n".join(start_lines):
        start_lines[1:1] = ["chmod +x /replace_address.sh", "/replace_address.sh"]
    start_path.write_text("\n".join(start_lines) + "\n", encoding="utf-8")

    docker_lines = canonical_dockerfile.read_text(encoding="utf-8").splitlines()
    start_copy = f"COPY {start_source} /start.sh"
    try:
        start_index = docker_lines.index(start_copy)
    except ValueError as exc:
        raise ValueError(f"canonical Dockerfile has no exact {start_copy!r}") from exc
    docker_lines.insert(start_index, f"COPY {replacement_source} /replace_address.sh")
    (generated / "Dockerfile").write_text("\n".join(docker_lines) + "\n", encoding="utf-8")

    return sha256(generated / bird_source), sha256(generated / kernel_source)


def parseArgs() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canonical-output", type=Path, required=True)
    parser.add_argument("--compiled-output", type=Path, required=True)
    parser.add_argument("--expected-nodes", type=int, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parseArgs()
    canonical_output = args.canonical_output.expanduser().resolve()
    compiled_output = args.compiled_output.expanduser().resolve()
    if canonical_output == compiled_output:
        raise ValueError("canonical and compiled output directories must differ")

    services, canonical_addresses, dummy_base, dummy_tag = loadCanonicalServices(
        canonical_output
    )
    if len(services) != args.expected_nodes:
        raise ValueError(
            f"canonical runtime service count {len(services)} != expected {args.expected_nodes}"
        )
    missing_canonical = [
        name for name, context in services.items()
        if not (canonical_output / context / "Dockerfile").is_file()
    ]
    missing_generated = [
        name for name, context in services.items()
        if not (compiled_output / context / "Dockerfile").is_file()
    ]
    if missing_canonical or missing_generated:
        raise ValueError(
            f"context mismatch: canonical_missing={missing_canonical[:10]} generated_missing={missing_generated[:10]}"
        )

    bird_digest = hashlib.sha256()
    kernel_digest = hashlib.sha256()
    for index, (name, context) in enumerate(sorted(services.items()), start=1):
        bird_hash, kernel_hash = adaptContext(
            canonical_output / context,
            compiled_output / context,
            canonical_addresses[context],
        )
        bird_digest.update(f"{name}:{bird_hash}\n".encode())
        kernel_digest.update(f"{name}:{kernel_hash}\n".encode())
        if index % 500 == 0 or index == len(services):
            print(f"[canonical-output] adapted {index}/{len(services)}", flush=True)

    build_script = compiled_output / "build_images.sh"
    build_text = build_script.read_text(encoding="utf-8")
    replacement = f'prepare_dummy_image "{dummy_base}" "{dummy_tag}"'
    build_text, replacements = PREPARE_PATTERN.subn(replacement, build_text)
    if replacements != 1:
        raise ValueError(
            f"expected one prepare_dummy_image call in {build_script}, replaced {replacements}"
        )
    build_script.write_text(build_text, encoding="utf-8")

    summary = {
        "status": "PASS",
        "canonicalOutput": str(canonical_output),
        "compiledOutput": str(compiled_output),
        "canonicalComposeSha256": sha256(canonical_output / "docker-compose.yml"),
        "runtimeNodeCount": len(services),
        "kubernetesOnlyDifferences": [
            "/replace_address.sh",
            "two start.sh lines invoking /replace_address.sh",
            "one Dockerfile COPY for /replace_address.sh",
        ],
        "birdConfigAggregateSha256": bird_digest.hexdigest(),
        "kernelConfigAggregateSha256": kernel_digest.hexdigest(),
        "baseImage": dummy_base,
        "dummyImage": dummy_tag,
    }
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
