"""The UI's public files are real evidence, with a deliberately narrow disclosure boundary."""
from dataclasses import asdict
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile

import pytest
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from demo import export_ui as exporter
from demo.demo import prepare_fixture
from demo.sessions import CallArtifacts, DESIGNS, build_clean, build_partial
from vatlog.bundle import Bundle, build_bundle
from vatlog.canon import sha256_hex
from vatlog.roles import Keyring, TrustBundle
from vatlog.notary import issue_receipt
from vatlog.verify import verify_bundle


@pytest.fixture
def exported(tmp_path):
    destination = tmp_path / "public"
    return destination, exporter.export_ui(destination)


@pytest.mark.parametrize("name,code,positions,size", [
    ("clean", 0, list(range(4)), 4), ("tampered", 2, list(range(4)), 4),
    ("doctored", 0, list(range(7)), 7), ("partial", 6, [5], 10),
])
def test_downloads_reproduce_report_and_cli(exported, name, code, positions, size):
    destination, catalog = exported
    entry = next(s for s in catalog["scenarios"] if s["id"] == name)
    archive = destination / f"{name}.zip"
    bundle = Bundle.load(archive)
    trust_path = destination / f"{name}.trust.json"
    sequence = (destination / f"{name}.sequence.txt").read_text()
    report = verify_bundle(bundle, TrustBundle.load(trust_path), submitted_sequence=sequence)
    assert report.exit_code == code
    assert bundle.revealed_indices() == positions and report.tree_size == size
    assert report.revealed == len(positions)
    assert [c.passed for c in report.checks] == (
        [False] if name == "tampered" else [True] * 4 + [name != "partial"])
    assert entry["report"] == {**asdict(report), "exit_code": code}
    assert entry["report_text"] == report.render()
    assert entry["archive_sha256"] == sha256_hex(archive.read_bytes())
    assert entry["bundle"] == bundle.model_dump(mode="json", exclude={"blobs"})
    assert entry["sequence"] == sequence
    assert entry["trust"] == TrustBundle.load(trust_path).model_dump(mode="json")
    result = subprocess.run(
        [sys.executable, "-m", "vatlog.cli", "verify", str(archive), "--trust", str(trust_path),
         "--sequence", sequence], env={**os.environ, "PYTHONPATH": "src"}, capture_output=True, text=True)
    assert result.returncode == code and result.stdout.strip() == report.render().strip()


def test_partial_exports_only_disclosed_references(exported):
    destination, catalog = exported
    partial = next(s for s in catalog["scenarios"] if s["id"] == "partial")
    bundle = Bundle.load(destination / "partial.zip")
    refs = {a.sha256 for r in bundle.records for a in r.inputs + r.results + r.intermediates}
    assert set(partial["artifacts"]) == refs == set(bundle.blobs)
    assert partial["report"]["designer_identity"] == "unverified"
    assert all(r.designer_statement is None for r in bundle.records)
    with ZipFile(destination / "partial.zip") as archive:
        assert set(archive.namelist()) == {"manifest.json", *(f"blobs/{d}" for d in refs)}


def test_hidden_sentinels_and_private_material_do_not_escape(tmp_path, monkeypatch):
    original = exporter.prepare_fixture
    ring = Keyring.generate()
    hidden = b"UNIQUE hidden opening instructions"
    calls = [CallArtifacts(transcript=f"UNIQUE hidden run {i}".encode()) for i in range(9)]

    def prepare(name, root, **kwargs):
        nonlocal ring
        ring = kwargs["keyring"]
        if name != "partial":
            return original(name, root, **kwargs)
        log, *_ = build_partial(root, keyring=ring, instructions=hidden, calls=calls)
        bundle = build_bundle(log, reveal=[5])
        bundle.notary_receipt = issue_receipt(build_bundle(log), ring.notary, ring.trust_bundle())
        full = build_bundle(log)
        full.notary_receipt = bundle.notary_receipt
        bundle.transparency_evidence = kwargs["transparency"].append(full)
        return bundle, ring.trust_bundle(), DESIGNS[4]

    monkeypatch.setattr(exporter, "prepare_fixture", prepare)
    catalog = exporter.export_ui(tmp_path / "out")
    encoded = json.dumps(catalog).encode()
    bundle = Bundle.load(tmp_path / "out/partial.zip")
    public_bytes = encoded + b"".join(bundle.blobs.values())
    assert hidden not in public_bytes
    for i, call in enumerate(calls):
        assert (call.transcript in public_bytes) == (i == 4)
    for signer in (ring.designer, ring.recorder, ring.notary, ring.transparency_log):
        private = signer.key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        assert private not in public_bytes
        assert base64.b64encode(private) not in public_bytes
        assert private.hex().encode() not in public_bytes
    assert len(list((tmp_path / "out").iterdir())) == 13


@pytest.mark.parametrize("data,preview", [
    (b"", ""), (b"\xff", None), (b"x" * 65536, "x" * 65536),
    (b"x" * 65537, None), ("🧬".encode() * 16385, None),
])
def test_preview_limits_preserve_real_bytes(tmp_path, data, preview):
    ring = Keyring.generate()
    log, *_ = build_clean(tmp_path, keyring=ring, instructions=data)
    bundle = build_bundle(log)
    ref = bundle.records[0].inputs[0]
    assert exporter.artifact_views(bundle)[ref.sha256] == {"available": True, "text": preview}
    assert bundle.blobs[ref.sha256] == data
    digest_only = build_bundle(log, include_blobs=False)
    assert exporter.artifact_views(digest_only)[ref.sha256] == {"available": False, "text": None}


def test_shared_digest_preview_does_not_depend_on_reference_order(tmp_path):
    bundle, _, _ = prepare_fixture("doctored", tmp_path)
    ref = bundle.records[0].inputs[0]
    unsupported = ref.model_copy(update={"kind": "log"})
    bundle.records[0].inputs = [unsupported]
    assert exporter.artifact_views(bundle)[ref.sha256]["text"] is None
    for refs in ([ref, unsupported], [unsupported, ref]):
        bundle.records[0].inputs = refs
        assert exporter.artifact_views(bundle)[ref.sha256]["text"] == bundle.blobs[ref.sha256].decode()


@pytest.mark.parametrize("attack,code", [("bytes", 2), ("trust", 3), ("sequence", 5)])
def test_changed_downloads_rejected(exported, attack, code):
    destination, _ = exported
    path = destination / "clean.zip"
    bundle = Bundle.load(path)
    trust = TrustBundle.load(destination / "clean.trust.json")
    sequence = (destination / "clean.sequence.txt").read_text()
    if attack == "bytes":
        bundle.blobs[next(iter(bundle.blobs))] = b"changed after export"
        bundle.save(path)
    elif attack == "trust":
        trust = Keyring.generate().trust_bundle()
    else:
        sequence = "NOT_THE_SUBMITTED_SEQUENCE"
    assert verify_bundle(Bundle.load(path), trust, submitted_sequence=sequence).exit_code == code


def test_failed_regeneration_invalidates_catalog_and_rerun_recovers(exported, monkeypatch):
    destination, _ = exported
    before = (destination / "clean.zip").read_bytes()
    original = exporter.prepare_fixture
    def fail_second(name, root, **kwargs):
        if name == "tampered":
            raise OSError("fixture disk failure")
        return original(name, root, **kwargs)
    monkeypatch.setattr(exporter, "prepare_fixture", fail_second)
    with pytest.raises(OSError, match="disk failure"):
        exporter.export_ui(destination)
    assert not (destination / "catalog.json").exists()
    assert (destination / "clean.zip").read_bytes() != before
    monkeypatch.setattr(exporter, "prepare_fixture", original)
    exporter.export_ui(destination)
    assert json.loads((destination / "catalog.json").read_text())["format"] == "vat-ui-demo/1"


def test_catalog_invalidation_failure_prevents_download_writes(exported, monkeypatch):
    destination, _ = exported
    before = {p.name: p.read_bytes() for p in destination.iterdir()}
    def fail_unlink(self, **kwargs):
        raise PermissionError("catalog locked")
    monkeypatch.setattr(Path, "unlink", fail_unlink)
    with pytest.raises(PermissionError):
        exporter.export_ui(destination)
    assert {p.name: p.read_bytes() for p in destination.iterdir()} == before


@pytest.mark.parametrize("failure", ["outcome", "load", "catalog_limit", "leaf_limit"])
def test_export_failure_does_not_publish_catalog(tmp_path, monkeypatch, failure):
    if failure == "outcome":
        original = exporter.prepare_fixture
        monkeypatch.setattr(exporter, "prepare_fixture", lambda name, root, **kwargs: original("clean", root, **kwargs))
    elif failure == "load":
        def fail_load(path):
            raise OSError("cannot load saved trust")
        monkeypatch.setattr(TrustBundle, "load", fail_load)
    else:
        monkeypatch.setattr(exporter, "MAX_CATALOG_BYTES" if failure == "catalog_limit" else "MAX_LEAVES", 1)
    with pytest.raises((ValueError, OSError)):
        exporter.export_ui(tmp_path / "out")
    assert not (tmp_path / "out/catalog.json").exists()
