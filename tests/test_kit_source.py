"""The Dockerfile build context is authenticated independently of its git checkout."""

import pytest

from runner.design_store import json_bytes
from runner.kit_source import build_source_manifest, validate_source_manifest


def test_source_manifest_binds_exact_tree_and_rejects_changes(tmp_path):
    (tmp_path / "common").mkdir()
    (tmp_path / "common/core.py").write_text("shared\n")
    (tmp_path / "pxdesign").mkdir()
    (tmp_path / "pxdesign/run.sh").write_text("exit 0\n")
    manifest = build_source_manifest(tmp_path, ["pxdesign"])
    path = tmp_path.parent / "uplifting-biomolecular-modeling.json"
    path.write_bytes(json_bytes(manifest))
    assert validate_source_manifest(tmp_path, path, ["pxdesign"])["identity"] == manifest["identity"]
    (tmp_path / "pxdesign/run.sh").write_text("changed\n")
    with pytest.raises(ValueError, match="bytes differ"):
        validate_source_manifest(tmp_path, path, ["pxdesign"])


def test_source_manifest_rejects_unlisted_files_and_symlinks(tmp_path):
    (tmp_path / "common").mkdir()
    (tmp_path / "common/core.py").write_text("shared\n")
    (tmp_path / "kit").mkdir()
    (tmp_path / "kit/run.sh").write_text("exit 0\n")
    manifest = build_source_manifest(tmp_path, ["kit"])
    path = tmp_path.parent / "uplifting-biomolecular-modeling.json"
    path.write_bytes(json_bytes(manifest))
    (tmp_path / "kit/extra").write_text("extra\n")
    with pytest.raises(ValueError, match="differs"):
        validate_source_manifest(tmp_path, path, ["kit"])
    (tmp_path / "kit/extra").unlink()
    (tmp_path / "kit/link").symlink_to(tmp_path / "common/core.py")
    with pytest.raises(ValueError, match="symbolic"):
        validate_source_manifest(tmp_path, path, ["kit"])


def test_source_manifest_rejects_duplicate_json_keys(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text('{"schema":"anthropic-kit-source/1","schema":"other"}')

    with pytest.raises(ValueError, match="malformed"):
        validate_source_manifest(tmp_path, path, ["kit"])


def test_source_manifest_rejects_non_object_json(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text("[]")

    with pytest.raises(ValueError, match="malformed"):
        validate_source_manifest(tmp_path, path, ["kit"])
