from __future__ import annotations

import shutil
import stat
from importlib.resources import as_file, files
from pathlib import Path


def resourcePath(resource_name: str):
    """Return the package resource path for a bundled setup/running tree."""
    return files("seedemu.k8spre.resources").joinpath(resource_name)


def copyTree(resource_name: str, target_dir: str | Path, overwrite: bool = False) -> Path:
    """Copy one bundled resource tree to a user-visible directory.

    Args:
        resource_name: Resource tree name, for example "setup" or "running".
        target_dir: Destination directory.
        overwrite: Remove the destination first when true.
    """
    target = Path(target_dir).expanduser()
    if target.exists():
        if not overwrite:
            raise FileExistsError(f"{target} already exists; use overwrite=True to replace it")
        shutil.rmtree(target)

    with as_file(resourcePath(resource_name)) as source:
        shutil.copytree(str(source), str(target))
    return target


def copyResourceItems(
    resource_name: str,
    item_names: list[str],
    target_dir: str | Path,
    overwrite: bool = False,
) -> Path:
    """Copy selected files/directories from one bundled resource tree.

    Args:
        resource_name: Resource tree name, for example "setup".
        item_names: Relative file or directory names to copy from the resource.
        target_dir: Destination directory.
        overwrite: Replace destination items with the same relative name.
    """
    target = Path(target_dir).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    with as_file(resourcePath(resource_name)) as source:
        source_root = Path(source)
        for item_name in item_names:
            source_item = source_root / item_name
            target_item = target / item_name
            if not source_item.exists():
                raise FileNotFoundError(f"Missing package resource: {resource_name}/{item_name}")
            if target_item.exists():
                if not overwrite:
                    continue
                if target_item.is_dir():
                    shutil.rmtree(target_item)
                else:
                    target_item.unlink()
            target_item.parent.mkdir(parents=True, exist_ok=True)
            if source_item.is_dir():
                shutil.copytree(str(source_item), str(target_item))
            else:
                shutil.copy2(str(source_item), str(target_item))
    return target


def chmodScripts(path: str | Path) -> None:
    """Make every shell script below path executable."""
    root = Path(path).expanduser()
    for script in root.rglob("*.sh"):
        mode = script.stat().st_mode
        script.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def writeExecutableScript(path: str | Path, content: str) -> Path:
    """Write one executable shell script.

    Args:
        path: Script path.
        content: Full script content.
    """
    script = Path(path).expanduser()
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(content, encoding="utf-8")
    mode = script.stat().st_mode
    script.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


# Backward-compatible aliases for the first prototype helpers.
resource_path = resourcePath
copy_tree = copyTree
copy_resource_items = copyResourceItems
chmod_scripts = chmodScripts
write_executable_script = writeExecutableScript
