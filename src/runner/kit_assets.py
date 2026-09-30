"""Canonical private-asset manifests for the pinned Anthropic kit deployments."""

from __future__ import annotations

import hashlib
from pathlib import Path

from vatlog.canon import parse_json

from .design_inputs import DesignError
from .design_store import json_bytes
from .kit_workers import KIT_TOOL_IDS
from .tool_catalog import ANTHROPIC_KIT_REVISION, tool_spec

MANIFEST_NAME = "vat-assets.json"
ASSET_MANIFEST_SCHEMA = "anthropic-kit-assets/1"
MAX_ASSET_FILES = 50_000


def asset_root_name(tool: str) -> str:
    spec = tool_spec(tool)
    if tool not in KIT_TOOL_IDS or not spec.assets:
        raise ValueError("Tool has no Anthropic kit asset root.")
    return spec.assets[0]


def _asset_files(directory: Path) -> list[Path]:
    if directory.is_symlink() or not directory.is_dir():
        raise DesignError("ASSET_MISSING", "Kit asset directory is absent or unsafe.")
    files = []
    for path in sorted(directory.rglob("*")):
        if path.name == MANIFEST_NAME and path.parent == directory:
            continue
        if path.is_symlink():
            raise DesignError("ASSET_MISMATCH", "Kit assets cannot contain symbolic links.")
        if path.is_file():
            relative = path.relative_to(directory)
            if ".." in relative.parts or len(relative.as_posix()) > 1024:
                raise DesignError("ASSET_MISMATCH", "Kit asset path is unsafe.")
            files.append(path)
            if len(files) > MAX_ASSET_FILES:
                raise DesignError("ASSET_MISMATCH", "Kit asset directory contains too many files.")
    if not files:
        raise DesignError("ASSET_MISSING", "Kit asset directory contains no model files.")
    return files


def build_asset_manifest(tool: str, directory: Path, pins: bytes) -> dict:
    """Hash one already upstream-verified asset tree without following links."""
    entries = []
    for path in _asset_files(directory):
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        entries.append({"path": path.relative_to(directory).as_posix(),
                        "sha256": digest, "bytes_len": path.stat().st_size})
    manifest = {"schema": ASSET_MANIFEST_SCHEMA, "tool": tool,
                "asset": asset_root_name(tool), "kit_revision": ANTHROPIC_KIT_REVISION,
                "kit_pins_sha256": hashlib.sha256(pins).hexdigest(), "files": entries}
    manifest["identity"] = hashlib.sha256(json_bytes(manifest)).hexdigest()
    return manifest


def validate_asset_manifest(tool: str, directory: Path, pins: bytes) -> dict:
    """Authenticate manifest identity, exact file set and every byte before inference."""
    path = directory / MANIFEST_NAME
    if path.is_symlink() or not path.is_file():
        raise DesignError("ASSET_MISSING", "Kit asset manifest is not staged.")
    try:
        manifest = parse_json(path.read_bytes())
    except (OSError, ValueError) as exc:
        raise DesignError("ASSET_MISMATCH", "Kit asset manifest is malformed.") from exc
    if not isinstance(manifest, dict):
        raise DesignError("ASSET_MISMATCH", "Kit asset manifest is malformed.")
    expected_identity = hashlib.sha256(json_bytes(
        {key: value for key, value in manifest.items() if key != "identity"})).hexdigest()
    if (manifest.get("schema") != ASSET_MANIFEST_SCHEMA or manifest.get("tool") != tool
            or manifest.get("asset") != asset_root_name(tool)
            or manifest.get("kit_revision") != ANTHROPIC_KIT_REVISION
            or manifest.get("kit_pins_sha256") != hashlib.sha256(pins).hexdigest()
            or manifest.get("identity") != expected_identity
            or not isinstance(manifest.get("files"), list) or not manifest["files"]):
        raise DesignError("ASSET_MISMATCH", "Kit asset manifest identity does not match this deployment.")
    declared = {}
    for entry in manifest["files"]:
        if (not isinstance(entry, dict) or set(entry) != {"path", "sha256", "bytes_len"}
                or not isinstance(entry["path"], str) or not isinstance(entry["sha256"], str)
                or len(entry["sha256"]) != 64 or type(entry["bytes_len"]) is not int
                or entry["bytes_len"] < 0):
            raise DesignError("ASSET_MISMATCH", "Kit asset manifest contains an invalid file entry.")
        relative = Path(entry["path"])
        if (relative.is_absolute() or ".." in relative.parts or relative.as_posix() in declared):
            raise DesignError("ASSET_MISMATCH", "Kit asset manifest contains an unsafe or duplicate path.")
        declared[relative.as_posix()] = entry
    actual = {path.relative_to(directory).as_posix(): path for path in _asset_files(directory)}
    if set(actual) != set(declared):
        raise DesignError("ASSET_MISMATCH", "Kit asset tree differs from its reviewed manifest.")
    for name, file_path in actual.items():
        entry = declared[name]
        with file_path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if file_path.stat().st_size != entry["bytes_len"] or digest != entry["sha256"]:
            raise DesignError("ASSET_MISMATCH", "Kit asset bytes differ from their reviewed manifest.")
    return manifest
