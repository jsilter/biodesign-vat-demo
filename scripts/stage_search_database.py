#!/usr/bin/env python3
"""Manifest and optionally upload a prepared MMseqs2 or Foldseek database."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from dotenv import load_dotenv

from runner.design_store import json_bytes
from runner.suite_workers import build_search_database_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tool", choices=["mmseqs2", "foldseek"])
    parser.add_argument("identity", help="Stable release identifier used in requests")
    parser.add_argument("release", help="Publisher/source release label")
    parser.add_argument("prefix", help="Database prefix filename inside the directory")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--review-and-upload", action="store_true")
    args = parser.parse_args()
    directory = args.directory.resolve()
    manifest = build_search_database_manifest(
        args.tool, args.identity, args.release, args.prefix, directory)
    (directory / "manifest.json").write_bytes(json_bytes(manifest))
    digest = hashlib.sha256(json_bytes(manifest)).hexdigest()
    print(f"Manifested {len(manifest['files'])} {args.tool} database files as {digest}.")
    if args.review_and_upload:
        load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False, interpolate=False)
        import modal
        volume = modal.Volume.from_name("biodesign-vat-protein-databases", create_if_missing=True)
        with volume.batch_upload() as upload:
            for path in sorted(directory.iterdir()):
                if path.is_file() and not path.is_symlink():
                    upload.put_file(path, f"/{args.identity}/{path.name}")
        print(f"Uploaded reviewed {args.tool} database {args.identity}; no search job was started.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
