"""Kit workers authenticate exact private asset bytes before invoking upstream code."""

import pytest

from runner.design_inputs import DesignError
from runner.design_store import json_bytes
from runner.kit_assets import MANIFEST_NAME, build_asset_manifest, validate_asset_manifest


def test_kit_asset_manifest_binds_tool_pins_paths_and_every_byte(tmp_path):
    directory = tmp_path / "boltz2"
    (directory / "mols").mkdir(parents=True)
    (directory / "boltz2_conf.ckpt").write_bytes(b"checkpoint")
    (directory / "mols/manifest.json").write_bytes(b"molecules")
    pins = b'{"model":"boltz2"}\n'
    manifest = build_asset_manifest("boltz2", directory, pins)
    (directory / MANIFEST_NAME).write_bytes(json_bytes(manifest))
    assert validate_asset_manifest("boltz2", directory, pins)["identity"] == manifest["identity"]
    (directory / "boltz2_conf.ckpt").write_bytes(b"tampered")
    with pytest.raises(DesignError, match="bytes differ"):
        validate_asset_manifest("boltz2", directory, pins)


def test_kit_asset_manifest_rejects_cross_tool_pins_and_unmanifested_files(tmp_path):
    directory = tmp_path / "boltz2"
    directory.mkdir()
    (directory / "weights.ckpt").write_bytes(b"checkpoint")
    pins = b"pins"
    manifest = build_asset_manifest("boltz2", directory, pins)
    (directory / MANIFEST_NAME).write_bytes(json_bytes(manifest))
    with pytest.raises(DesignError, match="identity"):
        validate_asset_manifest("boltz2", directory, b"different-pins")
    (directory / "extra").write_bytes(b"unreviewed")
    with pytest.raises(DesignError, match="tree differs"):
        validate_asset_manifest("boltz2", directory, pins)


def test_kit_asset_manifest_rejects_symlinks(tmp_path):
    directory = tmp_path / "boltz2"
    directory.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"private")
    (directory / "link").symlink_to(outside)
    with pytest.raises(DesignError, match="symbolic"):
        build_asset_manifest("boltz2", directory, b"pins")


def test_kit_asset_manifest_rejects_duplicate_json_keys(tmp_path):
    directory = tmp_path / "boltz2"
    directory.mkdir()
    (directory / "weights.ckpt").write_bytes(b"checkpoint")
    (directory / MANIFEST_NAME).write_text('{"schema":"anthropic-kit-assets/1","schema":"other"}')

    with pytest.raises(DesignError, match="malformed"):
        validate_asset_manifest("boltz2", directory, b"pins")


def test_kit_asset_manifest_rejects_non_object_json(tmp_path):
    directory = tmp_path / "boltz2"
    directory.mkdir()
    (directory / MANIFEST_NAME).write_text("[]")

    with pytest.raises(DesignError, match="malformed"):
        validate_asset_manifest("boltz2", directory, b"pins")
