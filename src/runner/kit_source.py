"""Source-tree manifest for Modal builds of Anthropic's pinned optimization kits."""

from __future__ import annotations

import hashlib
from pathlib import Path

from vatlog.canon import parse_json

from .design_store import json_bytes
from .tool_catalog import ANTHROPIC_KIT_REVISION

SOURCE_MANIFEST_SCHEMA = "anthropic-kit-source/1"
SOURCE_REPOSITORY = "https://github.com/anthropics/uplifting-biomolecular-modeling.git"


def _files(root: Path, kits: list[str]) -> list[Path]:
    paths = []
    for directory in [root / "common", *(root / kit for kit in kits)]:
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError(f"Pinned kit source directory is missing or unsafe: {directory.name}")
        for path in sorted(directory.rglob("*")):
            if path.is_symlink():
                raise ValueError("Pinned kit source tree cannot contain symbolic links.")
            if path.is_file():
                paths.append(path)
    if not paths:
        raise ValueError("Pinned kit source tree contains no files.")
    return paths


def build_source_manifest(root: Path, kits: list[str]) -> dict:
    entries = []
    for path in _files(root, kits):
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        entries.append({"path": path.relative_to(root).as_posix(), "sha256": digest,
                        "bytes_len": path.stat().st_size})
    manifest = {"schema": SOURCE_MANIFEST_SCHEMA, "repository": SOURCE_REPOSITORY,
                "revision": ANTHROPIC_KIT_REVISION, "kits": kits, "files": entries}
    manifest["identity"] = hashlib.sha256(json_bytes(manifest)).hexdigest()
    return manifest


def validate_source_manifest(root: Path, manifest_path: Path, kits: list[str]) -> dict:
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("Pinned kit source manifest is missing.")
    try:
        manifest = parse_json(manifest_path.read_bytes())
    except (OSError, ValueError) as exc:
        raise ValueError("Pinned kit source manifest is malformed.") from exc
    if not isinstance(manifest, dict):
        raise ValueError("Pinned kit source manifest is malformed.")
    expected_identity = hashlib.sha256(json_bytes(
        {key: value for key, value in manifest.items() if key != "identity"})).hexdigest()
    if (manifest.get("schema") != SOURCE_MANIFEST_SCHEMA
            or manifest.get("repository") != SOURCE_REPOSITORY
            or manifest.get("revision") != ANTHROPIC_KIT_REVISION
            or manifest.get("kits") != kits or manifest.get("identity") != expected_identity
            or not isinstance(manifest.get("files"), list)):
        raise ValueError("Pinned kit source manifest identity does not match this deployment.")
    declared = {entry.get("path"): entry for entry in manifest["files"] if isinstance(entry, dict)}
    if len(declared) != len(manifest["files"]) or any(
            set(entry) != {"path", "sha256", "bytes_len"} or not isinstance(name, str)
            or not isinstance(entry["sha256"], str) or len(entry["sha256"]) != 64
            or type(entry["bytes_len"]) is not int or entry["bytes_len"] < 0
            or Path(name).is_absolute() or ".." in Path(name).parts
            for name, entry in declared.items()):
        raise ValueError("Pinned kit source manifest contains an invalid path.")
    actual = {path.relative_to(root).as_posix(): path for path in _files(root, kits)}
    if set(actual) != set(declared):
        raise ValueError("Pinned kit source tree differs from its manifest.")
    for name, path in actual.items():
        entry = declared[name]
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if path.stat().st_size != entry["bytes_len"] or digest != entry["sha256"]:
            raise ValueError("Pinned kit source bytes differ from their manifest.")
    return manifest
