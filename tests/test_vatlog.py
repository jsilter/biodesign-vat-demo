"""Tests for the VAT log. The 1-of-9 case is the demo, asserted here so it cannot rot."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from demo.sessions import BASE_TIME, DESIGNS, build_clean, build_partial
from vatlog.bundle import Bundle, build_bundle
from vatlog.canon import canonical_bytes, canonical_record_bytes, record_digest
from vatlog.keys import generate_key
from vatlog.merkle import RecordTree, check_consistency, check_inclusion, leaf_hash
from vatlog.store import SessionLog
from vatlog.verify import Outcome, verify_bundle


# --- canonicalization ---------------------------------------------------------------

def test_canonical_bytes_ignore_key_order():
    a = {"b": 1, "a": {"z": [1, 2], "y": "x"}}
    b = {"a": {"y": "x", "z": [1, 2]}, "b": 1}
    assert canonical_bytes(a) == canonical_bytes(b)


def test_canonical_bytes_are_stable_across_calls():
    obj = {"seq": "MKV", "n": 3, "nested": {"k": [1, 2, 3]}}
    assert canonical_bytes(obj) == canonical_bytes(json.loads(json.dumps(obj)))


def test_record_digest_changes_when_any_field_changes(tmp_path):
    log, *_ = build_clean(tmp_path)
    rec = log.records[0]
    before = record_digest(rec)
    mutated = rec.model_copy(
        update={"user_session": rec.user_session.model_copy(update={"purpose_tag": "class"})}
    )
    assert record_digest(mutated) != before


# --- Merkle -------------------------------------------------------------------------

def test_merkle_rfc6962_leaf_hash_prefix():
    import hashlib

    assert leaf_hash(b"abc") == hashlib.sha256(b"\x00abc").digest()


def test_merkle_inclusion_and_rejection():
    leaves = [f"rec{i}".encode() for i in range(9)]
    tree = RecordTree.from_leaves(leaves)
    for i in range(9):
        assert check_inclusion(leaves[i], tree.root_hex, tree.prove_inclusion(i))
    assert not check_inclusion(b"forged", tree.root_hex, tree.prove_inclusion(3))


def test_merkle_consistency_between_heads():
    tree = RecordTree.from_leaves([f"rec{i}".encode() for i in range(9)])
    proof = tree.prove_consistency(5)
    assert check_consistency(tree.root_at(5), tree.root_hex, proof)
    assert not check_consistency("11" * 32, tree.root_hex, proof)


# --- chain --------------------------------------------------------------------------

def test_chain_links_are_assigned_by_the_log_not_the_caller(tmp_path):
    log, *_ = build_clean(tmp_path)
    assert [r.seq_no for r in log.records] == [0, 1, 2, 3]
    assert [r.link.leaf_index for r in log.records] == [0, 1, 2, 3]
    assert log.records[0].link.sha_prev == "00" * 32
    for prev, cur in zip(log.records, log.records[1:]):
        assert cur.link.sha_prev == record_digest(prev)


def test_reloaded_log_reproduces_the_same_root(tmp_path):
    log, _key, head, _att = build_clean(tmp_path)
    reloaded = SessionLog.load(tmp_path, "clean-0001")
    assert reloaded.tree.size == head.tree_size
    assert reloaded.tree.root_hex == head.root_hash


# --- verify: the five outcomes ------------------------------------------------------

def test_clean_bundle_passes_all_five_checks(tmp_path):
    log, key, *_ = build_clean(tmp_path)
    report = verify_bundle(
        build_bundle(log), key.public_key(), submitted_sequence=DESIGNS[2]
    )
    assert report.outcome is Outcome.OK
    assert [c.number for c in report.checks] == [1, 2, 3, 4, 5]
    assert all(c.passed for c in report.checks)
    assert report.exit_code == 0


def test_tampered_record_fails_check_1_and_names_the_index(tmp_path):
    log, key, *_ = build_clean(tmp_path)
    bundle = build_bundle(log)
    victim = bundle.records[1]
    bundle.records[1] = victim.model_copy(
        update={
            "user_session": victim.user_session.model_copy(
                update={"purpose_tag": "biochemistry class"}
            )
        }
    )
    report = verify_bundle(bundle, key.public_key())
    assert report.outcome is Outcome.TAMPERED
    failed = [c for c in report.checks if not c.passed]
    assert failed[0].number == 1 and "record 2" in failed[0].detail


def test_wrong_key_fails_check_2(tmp_path):
    log, _key, *_ = build_clean(tmp_path)
    report = verify_bundle(build_bundle(log), generate_key().public_key())
    assert report.outcome is Outcome.BAD_SIGNATURE


def test_record_swapped_under_a_valid_head_fails_check_3(tmp_path):
    """A record altered without touching adjacency still fails the inclusion proof."""
    log, key, *_ = build_clean(tmp_path)
    bundle = build_bundle(log, reveal=[2])
    victim = bundle.records[0]
    bundle.records[0] = victim.model_copy(
        update={"params": victim.params.model_copy(update={"seeds": [99999]})}
    )
    report = verify_bundle(bundle, key.public_key())
    assert report.outcome is Outcome.NOT_INCLUDED


def test_unrecorded_sequence_fails_check_4(tmp_path):
    log, key, *_ = build_clean(tmp_path)
    report = verify_bundle(
        build_bundle(log), key.public_key(), submitted_sequence="MKVEEAAAAAWRONG"
    )
    assert report.outcome is Outcome.SEQUENCE_MISMATCH


def test_implausible_head_timestamp_fails_check_3(tmp_path):
    log, key, *_ = build_clean(tmp_path)
    report = verify_bundle(
        build_bundle(log),
        key.public_key(),
        max_head_age=timedelta(hours=1),
        now=BASE_TIME + timedelta(days=400),
    )
    assert report.outcome is Outcome.NOT_INCLUDED


def test_one_of_ten_passes_checks_1_to_4_and_fails_completeness(tmp_path):
    """The demo. Every per-record check is honest; only the count gives it away."""
    log, key, head, _att = build_partial(tmp_path)
    bundle = build_bundle(log, reveal=[5])
    report = verify_bundle(
        bundle, key.public_key(), submitted_sequence=DESIGNS[4]
    )

    assert head.tree_size == 10  # session opening plus nine design runs
    assert report.outcome is Outcome.INCOMPLETE
    assert report.revealed == 1 and report.tree_size == 10
    assert report.exit_code == 6

    passed = {c.number for c in report.checks if c.passed}
    assert passed == {1, 2, 3, 4}, "checks 1-4 must pass; the bundle is honest per record"

    failure = next(c for c in report.checks if not c.passed)
    assert failure.number == 5
    assert "covers 1 of 10" in failure.detail
    assert "missing leaves [0, 1, 2, 3, 4, 6, 7, 8, 9]" in failure.detail


def test_incomplete_and_tampered_have_different_exit_codes(tmp_path):
    """A provider acts differently on a withheld run than on an altered bundle."""
    log, key, *_ = build_partial(tmp_path)
    incomplete = verify_bundle(build_bundle(log, reveal=[4]), key.public_key())
    assert incomplete.exit_code != 0
    assert incomplete.exit_code != Outcome.TAMPERED.value


def test_bundle_survives_a_zip_round_trip(tmp_path):
    log, key, *_ = build_partial(tmp_path)
    path = tmp_path / "bundle.zip"
    build_bundle(log, reveal=[4]).save(path)
    report = verify_bundle(Bundle.load(path), key.public_key())
    assert report.outcome is Outcome.INCOMPLETE
    assert report.tree_size == 10


def test_repeated_valid_record_cannot_hide_missing_records(tmp_path):
    log, key, *_ = build_clean(tmp_path)
    bundle = build_bundle(log, reveal=[2, 2, 2])
    report = verify_bundle(bundle, key.public_key(), submitted_sequence=DESIGNS[2])
    assert report.outcome is Outcome.TAMPERED
    assert "duplicate" in report.checks[-1].detail


@pytest.mark.parametrize("group", ["inputs", "intermediates", "results"])
def test_modified_blob_is_rejected(tmp_path, group):
    log, key, *_ = build_clean(tmp_path)
    # Include an intermediate artifact in the signed record, not just an unsigned blob.
    art = log.records[1].results[0]
    rec = log.records[1].model_copy(
        update={"inputs": [], "intermediates": [], "results": [], group: [art]}
    )
    extra = SessionLog(tmp_path, "blob-test")
    extra.append(rec)
    extra.blobs.put(log.blobs.get(art.sha256))
    extra.seal(key, recorder_identity="test", environment=rec.environment)
    bundle = build_bundle(extra)
    bundle.blobs[art.sha256] = b"altered contents"
    path = tmp_path / "altered.zip"
    bundle.save(path)
    report = verify_bundle(Bundle.load(path), key.public_key())
    assert report.outcome is Outcome.TAMPERED
    assert "blob digest mismatch" in report.checks[-1].detail


def test_digest_only_bundle_remains_supported(tmp_path):
    log, key, *_ = build_clean(tmp_path)
    report = verify_bundle(
        build_bundle(log, include_blobs=False), key.public_key(), submitted_sequence=DESIGNS[2]
    )
    assert report.outcome is Outcome.OK


@pytest.mark.parametrize("group", ["inputs", "intermediates", "results"])
def test_blob_length_must_match_signed_artifact_metadata(tmp_path, group):
    source, key, *_ = build_clean(tmp_path)
    rec = source.records[1]
    artifact = rec.results[0].model_copy(update={"bytes_len": 1})
    log = SessionLog(tmp_path, "bad-length")
    log.append(rec.model_copy(
        update={"inputs": [], "intermediates": [], "results": [], group: [artifact]}
    ))
    log.seal(key, recorder_identity="test", environment=rec.environment)
    report = verify_bundle(build_bundle(log), key.public_key())
    assert report.outcome is Outcome.TAMPERED
    assert "blob size mismatch" in report.checks[-1].detail


@pytest.mark.parametrize("index", [-1, 4])
def test_out_of_range_record_position_is_rejected(tmp_path, index):
    log, key, *_ = build_clean(tmp_path)
    bundle = build_bundle(log)
    rec = bundle.records[0]
    bundle.records[0] = rec.model_copy(
        update={"link": rec.link.model_copy(update={"leaf_index": index})}
    )
    assert verify_bundle(bundle, key.public_key()).outcome is Outcome.TAMPERED


def test_recreating_session_fails_without_changing_existing_files(tmp_path):
    log, key, *_ = build_clean(tmp_path)
    before = {p.name: p.read_bytes() for p in log.dir.iterdir()}
    with pytest.raises(FileExistsError):
        build_clean(tmp_path)
    assert {p.name: p.read_bytes() for p in log.dir.iterdir()} == before
    reloaded = SessionLog.load(tmp_path, log.session_uuid)
    assert verify_bundle(build_bundle(reloaded), key.public_key()).outcome is Outcome.OK


def test_explicit_load_can_resume_an_unsealed_session(tmp_path):
    source, key, *_ = build_clean(tmp_path)
    log = SessionLog(tmp_path, "resume")
    log.append(source.records[0])
    resumed = SessionLog.load(tmp_path, "resume")
    resumed.append(source.records[1])
    resumed.seal(key, recorder_identity="test", environment=source.records[0].environment)
    reloaded = SessionLog.load(tmp_path, "resume")
    assert len(reloaded.records) == 2
    assert verify_bundle(build_bundle(reloaded), key.public_key()).outcome is Outcome.OK


def test_reordered_complete_bundle_still_verifies(tmp_path):
    log, key, *_ = build_clean(tmp_path)
    bundle = build_bundle(log)
    bundle.records.reverse()
    bundle.inclusion_proofs.reverse()
    assert verify_bundle(bundle, key.public_key()).outcome is Outcome.OK


def test_loading_missing_session_does_not_create_it(tmp_path):
    root = tmp_path / "absent-root"
    with pytest.raises(FileNotFoundError):
        SessionLog.load(root, "missing")
    assert not root.exists()


def test_unicode_blob_round_trip_checks_byte_length(tmp_path):
    source, key, *_ = build_clean(tmp_path)
    log = SessionLog(tmp_path, "unicode")
    artifact = log.blobs.artifact("prompt", "prompt.txt", "Design α protein 🧬".encode())
    log.append(source.records[0].model_copy(update={"inputs": [artifact]}))
    log.seal(key, recorder_identity="test", environment=source.records[0].environment)
    path = tmp_path / "unicode.zip"
    build_bundle(log).save(path)
    assert verify_bundle(Bundle.load(path), key.public_key()).outcome is Outcome.OK


def test_binary_blob_export_preserves_original_bytes(tmp_path):
    source, key, *_ = build_clean(tmp_path)
    log = SessionLog(tmp_path, "binary")
    artifact = log.blobs.artifact("coordinates", "structure.bin", b"\xff\x00\xfe")
    log.append(source.records[0].model_copy(update={"intermediates": [artifact]}))
    log.seal(key, recorder_identity="test", environment=source.records[0].environment)
    path = tmp_path / "binary.zip"
    build_bundle(log).save(path)
    loaded = Bundle.load(path)
    assert loaded.blobs[artifact.sha256] == b"\xff\x00\xfe"
    assert verify_bundle(loaded, key.public_key()).outcome is Outcome.OK
    assert verify_bundle(build_bundle(log, include_blobs=False), key.public_key()).outcome is Outcome.OK


@pytest.mark.parametrize("attack", ["duplicate", "blob"])
def test_cli_rejects_corrupted_saved_bundle(tmp_path, attack):
    import os
    import subprocess
    import sys
    from pathlib import Path
    from vatlog.keys import public_b64

    log, key, *_ = build_clean(tmp_path)
    bundle = build_bundle(log)
    if attack == "duplicate":
        bundle.records = [bundle.records[-1]] * len(bundle.records)
    else:
        bundle.blobs[bundle.records[-1].results[0].sha256] = b"altered"
    path = tmp_path / "corrupt.zip"
    bundle.save(path)
    result = subprocess.run(
        [sys.executable, "-m", "vatlog.cli", "verify", str(path),
         "--pubkey", public_b64(key.public_key()), "--sequence", DESIGNS[2]],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        capture_output=True, text=True,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "=> TAMPERED" in result.stdout
    assert "Traceback" not in result.stderr


def test_verify_help_lists_every_exit_code():
    """The exit codes are the CLI's contract, so --help must name all of them.

    Generated from ``verify.EXIT_CODES``, so a new outcome cannot ship undocumented.
    """
    import os
    import subprocess
    import sys
    from pathlib import Path

    from vatlog.verify import EXIT_CODES

    result = subprocess.run(
        [sys.executable, "-m", "vatlog.cli", "verify", "--help"],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for outcome, code in EXIT_CODES.items():
        assert f"{code}  {outcome.value}" in result.stdout, outcome.value
