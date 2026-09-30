#!/usr/bin/env python3
"""Stage the exact Anthropic optimization-kit source used by Modal Dockerfile builds."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from runner.kit_workers import KIT_TOOL_IDS
from runner.kit_source import SOURCE_REPOSITORY, build_source_manifest
from runner.tool_catalog import ANTHROPIC_KIT_REVISION, tool_spec

REPOSITORY = SOURCE_REPOSITORY


def command(*args: str, cwd: Path | None = None) -> str:
    completed = subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)
    if completed.returncode:
        raise RuntimeError(completed.stderr.strip() or "git command failed")
    return completed.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path,
                        default=Path(".local/vendor/uplifting-biomolecular-modeling"))
    args = parser.parse_args()
    destination = args.destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    kits = sorted({tool_spec(tool).kit for tool in KIT_TOOL_IDS})
    if not destination.exists():
        command("git", "clone", "--filter=blob:none", "--no-checkout", REPOSITORY, str(destination))
        command("git", "sparse-checkout", "set", "--no-cone", "/common/", *(f"/{kit}/" for kit in kits),
                cwd=destination)
        command("git", "checkout", "--detach", ANTHROPIC_KIT_REVISION, cwd=destination)
    head = command("git", "rev-parse", "HEAD", cwd=destination)
    status = command("git", "status", "--porcelain", cwd=destination)
    if head != ANTHROPIC_KIT_REVISION or status:
        parser.error("Destination is not a clean checkout of the reviewed Anthropic kit revision.")
    missing = [kit for kit in kits if not (destination / kit / "environment" / "Dockerfile").is_file()
               or not (destination / kit / "stock" / "PINS.json").is_file()]
    if missing:
        parser.error("Pinned checkout is missing required kit build files: " + ", ".join(missing))
    manifest = build_source_manifest(destination, kits)
    manifest_path = destination.parent / "uplifting-biomolecular-modeling.json"
    from runner.design_store import json_bytes
    manifest_path.write_bytes(json_bytes(manifest))
    print(f"Staged {len(manifest['files'])} source files at {manifest['identity']}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
