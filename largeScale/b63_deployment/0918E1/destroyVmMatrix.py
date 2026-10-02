#!/usr/bin/env python3
"""Destroy only the libvirt domains declared by the 0918E1 configuration.

Inputs: experiment.yaml and explicit optional storage/network deletion flags.
Outputs: command trace on stdout.
Side effects: stops and undefines named experiment VMs; storage/network survive unless explicitly selected.
Context: manual cleanup after results have been copied back to 0918E1/results/.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

from loadExperiment import loadConfig, resolvePath


SCRIPT_DIR = Path(__file__).resolve().parent


def parseArgs() -> argparse.Namespace:
    """Parse explicit cleanup scopes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=SCRIPT_DIR / "experiment.yaml")
    parser.add_argument("--delete-storage", action="store_true", help="delete qcow2, seed, and cloud-init files")
    parser.add_argument("--delete-network", action="store_true", help="destroy and undefine the dedicated network")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def runIfEnabled(command: list[str], dryRun: bool) -> None:
    """Print every cleanup command and execute it unless this is a preview."""
    print("+ " + " ".join(command))
    if not dryRun:
        subprocess.run(command, check=True)


def main() -> int:
    """Remove exact configured domains and optionally their generated storage/network."""
    args = parseArgs()
    config = loadConfig(args.config)
    vm = config["vm"]
    storageDir = resolvePath(config, vm["storageDir"])
    cloudInitDir = resolvePath(config, vm["cloudInitDir"])
    for node in vm["nodes"]:
        name = str(node["name"])
        info = subprocess.run(["virsh", "dominfo", name], text=True, capture_output=True, check=False)
        if info.returncode == 0:
            state = subprocess.run(["virsh", "domstate", name], text=True, capture_output=True, check=True).stdout
            if state.strip() != "shut off":
                runIfEnabled(["virsh", "destroy", name], args.dry_run)
            runIfEnabled(["virsh", "undefine", name], args.dry_run)
        if args.delete_storage:
            for path in (storageDir / f"{name}.qcow2", storageDir / f"{name}-seed.img"):
                print(f"delete {path}")
                if not args.dry_run:
                    path.unlink(missing_ok=True)
            generatedDir = cloudInitDir / name
            print(f"delete {generatedDir}")
            if not args.dry_run and generatedDir.is_dir():
                shutil.rmtree(generatedDir)

    if args.delete_network:
        for networkName in (str(vm["network"]["name"]), str(vm["externalNetwork"]["name"])):
            info = subprocess.run(["virsh", "net-info", networkName], text=True, capture_output=True, check=False)
            if info.returncode == 0:
                if "Active:         yes" in info.stdout:
                    runIfEnabled(["virsh", "net-destroy", networkName], args.dry_run)
                runIfEnabled(["virsh", "net-undefine", networkName], args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
