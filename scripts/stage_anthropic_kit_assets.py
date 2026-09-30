#!/usr/bin/env python3
"""Manifest and optionally upload one asset tree verified by a pinned Anthropic kit.

First run the selected kit's documented ``run.sh install --weights DIR`` inside its pinned
image. This command then records every resulting byte against that kit's PINS.json identity.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from dotenv import load_dotenv

from runner.design_store import json_bytes
from runner.kit_assets import MANIFEST_NAME, asset_root_name, build_asset_manifest, validate_asset_manifest
from runner.kit_workers import KIT_TOOL_IDS
from runner.tool_catalog import tool_spec


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tool", choices=sorted(KIT_TOOL_IDS))
    parser.add_argument("directory", type=Path,
                        help="Asset directory produced and checked by that kit's install --weights route")
    parser.add_argument("--kit-root", type=Path,
                        default=Path(".local/vendor/uplifting-biomolecular-modeling"))
    parser.add_argument("--review-and-upload", action="store_true")
    args = parser.parse_args()
    root, directory = args.kit_root.resolve(), args.directory.resolve()
    pins = root / tool_spec(args.tool).kit / "stock" / "PINS.json"
    if pins.is_symlink() or not pins.is_file():
        parser.error("Pinned kit PINS.json is missing; stage the reviewed source checkout first.")
    manifest = build_asset_manifest(args.tool, directory, pins.read_bytes())
    (directory / MANIFEST_NAME).write_bytes(json_bytes(manifest))
    validate_asset_manifest(args.tool, directory, pins.read_bytes())
    print(f"Manifested {len(manifest['files'])} {args.tool} asset files as {manifest['identity']}.")
    if args.review_and_upload:
        load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False, interpolate=False)
        import modal
        destination = "/" + asset_root_name(args.tool)
        volume = modal.Volume.from_name("biodesign-vat-weights", create_if_missing=True)
        with volume.batch_upload() as upload:
            for path in sorted(directory.rglob("*")):
                if path.is_file() and not path.is_symlink():
                    upload.put_file(path, destination + "/" + path.relative_to(directory).as_posix())
        print(f"Uploaded reviewed {args.tool} assets to {destination}; no image or GPU run was started.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
