"""ZIP transport preserves original bytes and rejects ambiguous packaging."""
import json
import subprocess
import sys
import os
from pathlib import Path
from zipfile import ZipFile

import pytest

from demo.sessions import build_clean
from vatlog.bundle import Bundle, build_bundle
from vatlog.keys import public_b64
from vatlog.store import SessionLog
from vatlog.verify import Outcome, verify_bundle


@pytest.fixture
def exported(tmp_path):
    source, key, *_ = build_clean(tmp_path)
    log = SessionLog(tmp_path, "binary-files")
    artifacts = [
        log.blobs.artifact("coordinates", "binary.dat", bytes(range(256))),
        log.blobs.artifact("log", "empty.log", b""),
        log.blobs.artifact("prompt", "unicode.txt", "α🧬".encode()),
    ]
    log.append(source.records[0].model_copy(update={
        "inputs": [artifacts[0]], "intermediates": [artifacts[1]], "results": [artifacts[2]],
    }))
    log.seal(key, recorder_identity="test", environment=source.records[0].environment)
    bundle = build_bundle(log)
    path = tmp_path / "bundle.zip"
    bundle.save(path)
    return log, key, bundle, path


def rewrite(path, transform):
    with ZipFile(path) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(members["manifest.json"])
    transform(members, manifest)
    members["manifest.json"] = json.dumps(manifest).encode()
    with ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)


def test_archive_contains_original_files_and_no_encoded_blob_payload(exported):
    log, key, bundle, path = exported
    with ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert "blobs" not in manifest
        assert manifest["bundle_format"] == "vat-bundle/1"
        assert set(manifest["blob_digests"]) == set(bundle.blobs)
        for digest, data in bundle.blobs.items():
            assert archive.read(f"blobs/{digest}") == data
    loaded = Bundle.load(path)
    assert loaded.blobs == bundle.blobs
    assert loaded.attestation == bundle.attestation
    assert loaded.signed_tree_head == bundle.signed_tree_head
    assert verify_bundle(loaded, key.public_key()).outcome is Outcome.OK


def test_tampered_binary_member_fails_verification(exported):
    _, key, bundle, path = exported
    digest = next(d for d, data in bundle.blobs.items() if len(data) == 256)
    rewrite(path, lambda members, _: members.update({f"blobs/{digest}": b"changed"}))
    assert verify_bundle(Bundle.load(path), key.public_key()).outcome is Outcome.TAMPERED


@pytest.mark.parametrize("case", ["missing", "extra", "traversal", "version", "digest", "duplicate_digest", "embedded", "not_object", "unknown_record_type"])
def test_malformed_manifest_or_members_are_rejected(exported, case):
    _, _, bundle, path = exported
    def change(members, manifest):
        if case == "missing":
            del members[f"blobs/{next(iter(bundle.blobs))}"]
        elif case in ("extra", "traversal"):
            members["extra.bin" if case == "extra" else "../outside"] = b"bad"
        elif case == "version":
            manifest["bundle_format"] = "unknown/2"
        elif case == "digest":
            manifest["blob_digests"] = ["../outside"]
        elif case == "duplicate_digest":
            manifest["blob_digests"] *= 2
        elif case == "embedded":
            manifest["blobs"] = {}
        elif case == "unknown_record_type":
            manifest["records"][0]["record_type"] = "not_a_record_type"
    rewrite(path, change)
    if case == "not_object":
        with ZipFile(path, "w") as archive:
            archive.writestr("manifest.json", "[]")
    with pytest.raises(ValueError):
        Bundle.load(path)
    assert not (path.parent.parent / "outside").exists()


def test_duplicate_archive_member_rejected(exported):
    _, _, _, path = exported
    with pytest.warns(UserWarning, match="Duplicate name"):
        with ZipFile(path, "a") as archive:
            archive.writestr("manifest.json", "{}")
    with pytest.raises(ValueError, match="duplicate"):
        Bundle.load(path)


def test_missing_manifest_rejected(tmp_path):
    path = tmp_path / "empty.zip"
    with ZipFile(path, "w"):
        pass
    with pytest.raises(ValueError, match="missing manifest"):
        Bundle.load(path)


def test_missing_source_blob_fails_export_but_digest_only_succeeds(exported):
    log, key, bundle, path = exported
    (log.blobs.root / next(iter(bundle.blobs))).unlink()
    with pytest.raises(FileNotFoundError):
        build_bundle(log)
    build_bundle(log, include_blobs=False).save(path)
    loaded = Bundle.load(path)
    assert loaded.blobs == {}
    assert verify_bundle(loaded, key.public_key()).outcome is Outcome.OK


@pytest.mark.parametrize("command", ["show", "verify"])
def test_cli_accepts_binary_archive_and_reports_malformed_input(exported, command):
    _, key, _, path = exported
    args = [sys.executable, "-m", "vatlog.cli", command, str(path)]
    if command == "verify":
        args += ["--pubkey", public_b64(key.public_key())]
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    valid = subprocess.run(args, env=env, capture_output=True, text=True)
    assert valid.returncode == 0, valid.stderr
    path.write_bytes(b"not a ZIP")
    invalid = subprocess.run(args, env=env, capture_output=True, text=True)
    assert invalid.returncode == 2
    assert "invalid bundle" in invalid.stderr
    assert "Traceback" not in invalid.stderr
