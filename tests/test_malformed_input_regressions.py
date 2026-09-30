"""Malformed transport, bounded verification and recorder-state regressions."""

import base64
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile, ZIP_DEFLATED

import pytest

from demo.sessions import build_clean
from vatlog.attest import pae, seal
from vatlog.bundle import Bundle, BundleLimits, build_bundle
from vatlog.canon import canonical_bytes
from vatlog.roles import Keyring, TrustBundle
from vatlog.store import SessionLog
from vatlog.verify import Outcome, verify_bundle


@pytest.fixture
def signed(tmp_path):
    ring = Keyring.generate()
    log, *_ = build_clean(tmp_path, keyring=ring)
    return ring, log


def write_manifest(path, bundle, transform):
    bundle.save(path)
    with ZipFile(path) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    members["manifest.json"] = transform(members["manifest.json"].decode()).encode()
    with ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)


@pytest.mark.parametrize("nested", [False, True])
def test_duplicate_manifest_keys_rejected(signed, tmp_path, nested):
    _, log = signed
    path = tmp_path / "duplicate.zip"
    def duplicate(text):
        if nested:
            return text.replace('"seq_no": 0', '"seq_no": 99, "seq_no": 0', 1)
        return '{"session_uuid":"unrelated",' + text[1:]
    write_manifest(path, build_bundle(log), duplicate)
    with pytest.raises(ValueError, match="duplicate"):
        Bundle.load(path)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1e999", "9007199254740992"])
def test_noncanonical_numeric_record_rejected_at_load(signed, tmp_path, value):
    _, log = signed
    path = tmp_path / "numeric.zip"
    def change(text):
        if value == "9007199254740992":
            return text.replace('"seq_no": 0', f'"seq_no": {value}', 1)
        return text.replace('"clock_skew_ms": 0.0', f'"clock_skew_ms": {value}', 1)
    write_manifest(path, build_bundle(log), change)
    with pytest.raises(ValueError):
        Bundle.load(path)


def test_noncanonical_record_in_direct_api_is_rejected_not_crashed(signed):
    ring, log = signed
    bundle = build_bundle(log)
    bundle.records[0] = bundle.records[0].model_copy(deep=True)
    bundle.records[0].params.seeds = [2**60]
    report = verify_bundle(bundle, ring.trust_bundle())
    assert report.outcome is Outcome.TAMPERED
    assert report.checks[-1].number == 1


def test_duplicate_trust_json_keys_rejected(signed, tmp_path):
    ring, _ = signed
    text = ring.trust_bundle().model_dump_json()
    text = text.replace('"role":"designer"', '"role":"notary","role":"designer"', 1)
    path = tmp_path / "trust.json"
    path.write_text(text)
    with pytest.raises(ValueError, match="duplicate"):
        TrustBundle.load(path)


def test_duplicate_signed_attestation_fields_rejected(signed):
    ring, log = signed
    bundle = build_bundle(log)
    envelope = bundle.attestation
    payload = base64.b64decode(envelope.payload).decode()
    payload = ('{"session_uuid":"different",' + payload[1:]).encode()
    envelope.payload = base64.b64encode(payload).decode()
    envelope.signatures[0]["sig"] = ring.recorder.sign(pae(envelope.payloadType, payload))
    assert verify_bundle(bundle, ring.trust_bundle()).outcome is Outcome.BAD_SIGNATURE


def test_conflicting_attested_capture_tiers_rejected(signed):
    ring, log = signed
    bundle = build_bundle(log)
    envelope = bundle.attestation
    data = json.loads(base64.b64decode(envelope.payload))
    data["assurance_tier"] = "tee_coexec"
    payload = json.dumps(data).encode()
    envelope.payload = base64.b64encode(payload).decode()
    envelope.signatures[0]["sig"] = ring.recorder.sign(pae(envelope.payloadType, payload))
    assert verify_bundle(bundle, ring.trust_bundle()).outcome is Outcome.BAD_SIGNATURE


def test_signed_naive_head_timestamp_rejected_by_age_check(signed):
    ring, log = signed
    bundle = build_bundle(log)
    head = bundle.signed_tree_head
    now = head.timestamp
    head.timestamp = head.timestamp.replace(tzinfo=None)
    head.signature = ring.recorder.sign(head.signing_payload())
    report = verify_bundle(bundle, ring.trust_bundle(), max_head_age=timedelta(hours=1), now=now)
    assert report.outcome is Outcome.NOT_INCLUDED


@pytest.mark.parametrize("offset,expected", [(0, Outcome.OK), (60, Outcome.OK), (61, Outcome.NOT_INCLUDED),
                                            (-3600, Outcome.OK), (-3601, Outcome.NOT_INCLUDED)])
def test_head_age_boundaries(signed, offset, expected):
    ring, log = signed
    bundle = build_bundle(log)
    head = bundle.signed_tree_head
    now = head.timestamp
    head.timestamp += timedelta(seconds=offset)
    head.signature = ring.recorder.sign(head.signing_payload())
    assert verify_bundle(bundle, ring.trust_bundle(), max_head_age=timedelta(hours=1), now=now).outcome is expected


@pytest.mark.parametrize("operation", ["verify", "notarize"])
def test_huge_signed_count_has_bounded_memory_use(signed, tmp_path, operation):
    ring, log = signed
    bundle = build_bundle(log, reveal=[])
    head = bundle.signed_tree_head
    head.tree_size = 10**12
    head.signature = ring.recorder.sign(head.signing_payload())
    att = bundle.attestation.attestation()
    bundle.attestation = seal(ring.recorder, root_hash=head.root_hash, tree_size=head.tree_size,
                              session_uuid=head.session_uuid, recorder_identity=att.recorder_identity,
                              environment=att.environment)
    path, trust = tmp_path / "huge.zip", tmp_path / "trust.json"
    bundle.save(path)
    ring.trust_bundle().save(trust)
    # A subprocess bounds memory so a regression fails instead of exhausting the host.
    code = '''
import resource, sys
from pathlib import Path
from vatlog.bundle import Bundle
from vatlog.roles import TrustBundle
from vatlog.verify import verify_bundle, Outcome
from vatlog.notary import local_null_screen
resource.setrlimit(resource.RLIMIT_AS, (256 * 1024**2, 256 * 1024**2))
bundle = Bundle.load(Path(sys.argv[1]))
trust = TrustBundle.load(Path(sys.argv[2]))
if sys.argv[3] == "verify":
    report = verify_bundle(bundle, trust)
    assert report.outcome is Outcome.INCOMPLETE
    assert len(report.render()) < 3000
    assert report.unrevealed_leaves == 10**12
else:
    try:
        local_null_screen(bundle, trust)
    except ValueError:
        pass
    else:
        raise AssertionError("incomplete session accepted")
'''
    result = subprocess.run([sys.executable, "-c", code, str(path), str(trust), operation],
                            env={**os.environ, "PYTHONPATH": "src"}, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("access", ["original", "returned"])
def test_appended_record_does_not_alias_caller_owned_models(signed, tmp_path, access):
    ring, source = signed
    log = SessionLog(tmp_path / "new", source.session_uuid)
    record = source.records[0].model_copy(deep=True)
    appended = log.append(record)
    if access == "original":
        record.user_session.purpose_tag = "changed by original caller"
    else:
        appended.designer_statement.purpose_tag = "changed through return value"
    log.seal(ring.recorder, recorder_identity="test", environment=record.environment)
    assert verify_bundle(build_bundle(log), ring.trust_bundle()).outcome is Outcome.OK


def test_failed_append_does_not_advance_tree(signed, tmp_path):
    ring, source = signed
    log = SessionLog(tmp_path / "new", source.session_uuid)
    # Real filesystem failure; no mock of the code being checked.
    blocked = log.dir / "records.jsonl"
    blocked.mkdir()
    with pytest.raises(IsADirectoryError):
        log.append(source.records[0])
    assert log.tree.size == 0 and log.records == []
    blocked.rmdir()
    log.append(source.records[0])
    log.seal(ring.recorder, recorder_identity="test", environment=source.records[0].environment)
    assert verify_bundle(build_bundle(log), ring.trust_bundle()).outcome is Outcome.OK


@pytest.mark.parametrize("field", ["sha_prev", "seq_no", "leaf_index", "session_uuid", "encoding"])
def test_corrupted_unsealed_log_cannot_be_resumed(signed, tmp_path, field):
    _, source = signed
    log = SessionLog(tmp_path / "new", source.session_uuid)
    log.append(source.records[0])
    log.append(source.records[1])
    path = log.dir / "records.jsonl"
    lines = path.read_bytes().splitlines()
    data = json.loads(lines[1])
    if field == "sha_prev":
        data["link"]["sha_prev"] = "ff" * 32
    elif field == "leaf_index":
        data["link"]["leaf_index"] = 9
    elif field == "seq_no":
        data["seq_no"] = 9
    elif field == "session_uuid":
        data["session_uuid"] = "other"
    # Keep all other invariants valid to isolate the one being exercised.
    lines[1] = json.dumps(data).encode() if field == "encoding" else canonical_bytes(data)
    path.write_bytes(b"\n".join(lines) + b"\n")
    with pytest.raises(ValueError):
        SessionLog.load(log.root, log.session_uuid)


@pytest.mark.parametrize("limit", ["max_archive_bytes", "max_manifest_bytes", "max_uncompressed_bytes", "max_members"])
def test_archive_reader_limits_reject_at_boundary_and_allow_explicit_budget(signed, tmp_path, limit):
    ring, log = signed
    path = tmp_path / "bounded.zip"
    build_bundle(log).save(path)
    with ZipFile(path) as archive:
        values = {"max_archive_bytes": path.stat().st_size,
                  "max_manifest_bytes": archive.getinfo("manifest.json").file_size,
                  "max_uncompressed_bytes": sum(info.file_size for info in archive.infolist()),
                  "max_members": len(archive.infolist())}
    with pytest.raises(ValueError, match="limit"):
        Bundle.load(path, limits=BundleLimits(**{limit: values[limit] - 1}))
    loaded = Bundle.load(path, limits=BundleLimits(**{limit: values[limit]}))
    assert verify_bundle(loaded, ring.trust_bundle()).outcome is Outcome.OK


def test_small_compressed_archive_cannot_bypass_expansion_budget(signed, tmp_path):
    _, log = signed
    path = tmp_path / "compressed.zip"
    # A genuine compressed member expands beyond a deliberately small test budget.
    bundle = build_bundle(log)
    from vatlog.canon import sha256_hex
    data = b"A" * 100_000
    bundle.blobs[sha256_hex(data)] = data
    bundle.save(path)
    with ZipFile(path) as archive:
        entries = [(info.filename, archive.read(info)) for info in archive.infolist()]
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    assert path.stat().st_size < 10_000
    with pytest.raises(ValueError, match="uncompressed"):
        Bundle.load(path, limits=BundleLimits(max_uncompressed_bytes=50_000))


def test_zip_member_cannot_lie_about_its_uncompressed_length(signed, tmp_path):
    import struct
    _, log = signed
    path = tmp_path / "wrong-size.zip"
    build_bundle(log).save(path)
    data = bytearray(path.read_bytes())
    central = data.index(b"PK\x01\x02")
    size = struct.unpack_from("<I", data, central + 24)[0]
    struct.pack_into("<I", data, central + 24, size + 1)
    struct.pack_into("<I", data, 22, size + 1)
    path.write_bytes(data)
    with pytest.raises(ValueError, match="size"):
        Bundle.load(path)


@pytest.mark.parametrize("case", ["encryption", "compression", "deflate"])
def test_unsupported_or_corrupt_zip_is_cleanly_rejected(signed, tmp_path, capsys, case):
    from vatlog.cli import main
    import struct
    ring, log = signed
    path = tmp_path / "corrupt.zip"
    build_bundle(log).save(path)
    with ZipFile(path) as archive:
        entries = [(info.filename, archive.read(info)) for info in archive.infolist()]
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    data = bytearray(path.read_bytes())
    central = data.index(b"PK\x01\x02")
    if case == "encryption":
        struct.pack_into("<H", data, 6, 1)
        struct.pack_into("<H", data, central + 8, 1)
    elif case == "compression":
        struct.pack_into("<H", data, 8, 99)
        struct.pack_into("<H", data, central + 10, 99)
    else:
        name_size, extra_size = struct.unpack_from("<HH", data, 26)
        data[30 + name_size + extra_size] = 0xff  # invalid deflate block type
    path.write_bytes(data)
    trust = tmp_path / "trust.json"
    ring.trust_bundle().save(trust)
    with pytest.raises(SystemExit) as error:
        main(["verify", str(path), "--trust", str(trust)])
    assert error.value.code == 2
    assert "invalid bundle" in capsys.readouterr().err


def test_deep_json_is_rejected_without_recursion_crash(signed, tmp_path, capsys):
    from vatlog.cli import main
    ring, log = signed
    path = tmp_path / "nested.zip"
    write_manifest(path, build_bundle(log), lambda _: "[" * 2000 + "0" + "]" * 2000)
    trust = tmp_path / "trust.json"
    ring.trust_bundle().save(trust)
    with pytest.raises(SystemExit) as error:
        main(["verify", str(path), "--trust", str(trust)])
    assert error.value.code == 2
    assert "invalid bundle" in capsys.readouterr().err


@pytest.mark.parametrize("access", ["entry", "bundle"])
def test_read_snapshot_cannot_mutate_live_log(signed, access):
    ring, log = signed
    original = log.records[0].user_session.purpose_tag
    record = log.entry(0) if access == "entry" else build_bundle(log).records[0]
    record.user_session.purpose_tag = "mutated exported object"
    assert log.records[0].user_session.purpose_tag == original
    assert verify_bundle(build_bundle(log), ring.trust_bundle()).outcome is Outcome.OK


@pytest.mark.parametrize("field", ["session_uuid", "log_id"])
def test_bundle_context_must_match_authenticated_head(signed, field):
    ring, log = signed
    bundle = build_bundle(log)
    setattr(bundle, field, "another")
    assert verify_bundle(bundle, ring.trust_bundle()).outcome is Outcome.BAD_SIGNATURE


def test_record_from_different_session_rejected_before_membership_check(signed):
    ring, log = signed
    bundle = build_bundle(log, reveal=[3])
    bundle.records[0] = bundle.records[0].model_copy(update={"session_uuid": "other"})
    report = verify_bundle(bundle, ring.trust_bundle())
    assert report.outcome is Outcome.BAD_SIGNATURE
    assert report.checks[-1].number == 2


def test_nonzero_genesis_link_rejected(signed):
    ring, log = signed
    bundle = build_bundle(log, reveal=[0])
    bundle.records[0] = bundle.records[0].model_copy(deep=True)
    bundle.records[0].link.sha_prev = "ff" * 32
    report = verify_bundle(bundle, ring.trust_bundle())
    assert report.outcome is Outcome.TAMPERED
    assert "record 0" in report.checks[-1].detail


@pytest.mark.parametrize("location", ["designer", "notary", "envelope"])
def test_only_head_signature_allows_empty_keyid_fallback(signed, location):
    from vatlog.notary import issue_receipt
    ring, log = signed
    bundle = build_bundle(log, reveal=[0])
    if location == "designer":
        opening = bundle.records[0].model_copy(deep=True)
        opening.user_session.user_id = ""
        opening.designer_statement.keyid = ""
        opening.designer_statement.signature = ring.designer.sign(opening.designer_statement.signing_payload())
        bundle.records[0] = opening
    elif location == "notary":
        receipt = issue_receipt(build_bundle(log), ring.notary, ring.trust_bundle())
        receipt.keyid = ""
        receipt.signature = ring.notary.sign(receipt.signing_payload())
        bundle.notary_receipt = receipt
    else:
        bundle.attestation.signatures[0]["keyid"] = ""
    assert verify_bundle(bundle, ring.trust_bundle()).outcome is Outcome.BAD_SIGNATURE


@pytest.mark.parametrize("value", [0, -1, 1.5, True])
def test_invalid_reader_budgets_rejected(value):
    with pytest.raises(ValueError):
        BundleLimits(max_manifest_bytes=value)


@pytest.mark.parametrize("head_only", [False, True])
def test_unsealed_or_partially_sealed_session_cannot_export(signed, tmp_path, head_only):
    _, source = signed
    log = SessionLog(tmp_path / "unsealed", source.session_uuid)
    log.append(source.records[0])
    if head_only:
        (log.dir / "head.json").write_text(source.head().model_dump_json())
    with pytest.raises(ValueError, match="not sealed"):
        build_bundle(log)


def test_keygen_shared_output_path_rejected_without_writing(tmp_path):
    from vatlog.cli import main
    path = tmp_path / "same-path"
    with pytest.raises(SystemExit) as error:
        main(["keygen", "--role", "designer", "--private", str(path), "--trust", str(path)])
    assert error.value.code == 2
    assert not path.exists()


def test_missing_leaf_sample_preserves_exact_total_and_excludes_revealed(tmp_path):
    from demo.sessions import build_session, THERAPEUTIC_OBJECTIVES
    ring = Keyring.generate()
    log, *_ = build_session(tmp_path, session_uuid="many", n_runs=130, purpose_tag="test",
                            objectives=THERAPEUTIC_OBJECTIVES, keyring=ring)
    reveal = [0, 2, 5, 129, 130]
    report = verify_bundle(build_bundle(log, reveal=reveal), ring.trust_bundle())
    missing = sorted(set(range(131)) - set(reveal))
    assert report.outcome is Outcome.INCOMPLETE
    assert report.unrevealed_leaves == len(missing)
    assert f"missing leaves {missing[:100]} (first 100 of {len(missing)} missing)" in report.checks[-1].detail
