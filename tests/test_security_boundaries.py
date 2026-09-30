"""Adversarial inputs at sequence, proof and recorder filesystem boundaries.

Tests use real signatures and Merkle proofs. Mutations are applied after producing a
valid control bundle so rejection cannot be explained by an unrelated broken fixture.
"""

import copy
import hashlib
from itertools import combinations

import pytest

from demo.sessions import CallArtifacts, DESIGNS, build_clean
from vatlog.bundle import Bundle, build_bundle
from vatlog.merkle import RecordTree, check_inclusion
from vatlog.roles import Keyring
from vatlog.store import ChainError, SessionLog
from vatlog.verify import Outcome, verify_bundle


@pytest.fixture
def authenticated(tmp_path):
    ring = Keyring.generate()
    log, *_ = build_clean(tmp_path, keyring=ring)
    assert verify_bundle(build_bundle(log), ring.trust_bundle()).outcome is Outcome.OK
    return ring, log


@pytest.mark.parametrize("kind", ["transcript", "reasoning", "prompt", "log", "metric"])
@pytest.mark.parametrize("include_blobs", [False, True])
def test_nonsequence_result_cannot_satisfy_submitted_sequence(tmp_path, kind, include_blobs):
    ring = Keyring.generate()
    source, *_ = build_clean(tmp_path, keyring=ring)
    log = SessionLog(tmp_path / "capture", source.session_uuid)
    submitted = "ACDEFGHIKLMNPQRSTVWY"
    for index, record in enumerate(source.records):
        record = record.model_copy(deep=True)
        if index == 1:
            record.results.append(log.blobs.artifact(kind, "text.txt", submitted.encode()))
        for artifact in record.inputs + record.results:
            if source.blobs.has(artifact.sha256):
                log.blobs.put(source.blobs.get(artifact.sha256))
        log.append(record)
    log.seal(ring.recorder, recorder_identity="test", environment=source.records[0].environment)
    bundle = build_bundle(log, include_blobs=include_blobs)
    assert verify_bundle(bundle, ring.trust_bundle(), submitted_sequence=DESIGNS[0]).outcome is Outcome.OK
    report = verify_bundle(bundle, ring.trust_bundle(), submitted_sequence=submitted)
    assert report.outcome is Outcome.SEQUENCE_MISMATCH
    assert report.checks[-1].number == 4
    assert log.find_by_result_digest(hashlib.sha256(submitted.encode()).hexdigest(), kind="aa_sequence") == []


def test_transcript_match_does_not_shadow_actual_sequence_record(tmp_path):
    ring = Keyring.generate()
    log, *_ = build_clean(tmp_path, keyring=ring,
                         calls=[CallArtifacts(transcript=DESIGNS[0].encode())] * 3,
                         granularity="agent_call")
    assert log.find_by_result_digest(hashlib.sha256(DESIGNS[0].encode()).hexdigest(), kind="aa_sequence") == [2]
    report = verify_bundle(build_bundle(log), ring.trust_bundle(), submitted_sequence=DESIGNS[0])
    assert report.outcome is Outcome.OK
    assert report.checks[3].detail == "matches output of record 2"


@pytest.mark.parametrize("proof", [{}, {"leaf_index": []}, {"leaf_index": {}}, {"leaf_index": "0"},
                                   {"leaf_index": True}, {"leaf_index": None}, {"leaf_index": -1}])
def test_malformed_proof_index_is_rejected_without_exception(authenticated, tmp_path, proof):
    ring, log = authenticated
    bundle = build_bundle(log)
    bundle.inclusion_proofs[0] = proof
    path = tmp_path / "malformed.zip"
    bundle.save(path)
    report = verify_bundle(Bundle.load(path), ring.trust_bundle())
    assert report.outcome is Outcome.NOT_INCLUDED
    assert report.checks[-1].number == 3
    assert report.designer_identity == "unverified"


def test_duplicate_proof_indices_are_not_silently_overwritten(authenticated):
    ring, log = authenticated
    bundle = build_bundle(log)
    bundle.inclusion_proofs.append(copy.deepcopy(bundle.inclusion_proofs[0]))
    report = verify_bundle(bundle, ring.trust_bundle())
    assert report.outcome is Outcome.NOT_INCLUDED


@pytest.mark.parametrize("mutation", ["size", "security", "algorithm", "rule_extra", "path_extra",
                                       "subset", "boolean_rule", "rule_last"])
def test_proof_profile_and_shape_are_enforced(authenticated, mutation):
    ring, log = authenticated
    bundle = build_bundle(log)
    proof = bundle.inclusion_proofs[0]
    if mutation == "size":
        proof["metadata"]["size"] += 1
    elif mutation == "security":
        proof["metadata"]["security"] = 1
    elif mutation == "algorithm":
        proof["metadata"]["algorithm"] = "sha3_256"
    elif mutation == "rule_extra":
        proof["rule"].append(0)
    elif mutation == "path_extra":
        proof["path"].append("00" * 32)
    elif mutation == "subset":
        proof["subset"] = [1]
    elif mutation == "boolean_rule":
        proof["rule"][0] = False
    else:
        proof["rule"][-1] = 1
    assert verify_bundle(bundle, ring.trust_bundle()).outcome is Outcome.NOT_INCLUDED


@pytest.mark.parametrize("claimed_index", [0, 2, 3])
def test_membership_proof_cannot_be_relabelled_to_another_position(claimed_index):
    leaves = [b"first", b"second", b"third", b"fourth"]
    tree = RecordTree.from_leaves(leaves)
    proof = tree.prove_inclusion(1)
    assert check_inclusion(leaves[1], tree.root_hex, proof)
    proof["leaf_index"] = claimed_index
    assert not check_inclusion(leaves[1], tree.root_hex, proof)


def test_forged_smaller_tree_cannot_turn_partial_disclosure_into_complete(authenticated):
    ring, log = authenticated
    bundle = build_bundle(log, reveal=[0, 1])
    # A dishonest recorder signs a false count against the original larger root.
    head = bundle.signed_tree_head
    head.tree_size = 2
    head.signature = ring.recorder.sign(head.signing_payload())
    from vatlog.attest import seal
    att = bundle.attestation.attestation()
    bundle.attestation = seal(ring.recorder, root_hash=head.root_hash, tree_size=2,
                              session_uuid=head.session_uuid, recorder_identity=att.recorder_identity,
                              environment=att.environment)
    for proof in bundle.inclusion_proofs:
        proof["metadata"]["size"] = 2
    report = verify_bundle(bundle, ring.trust_bundle())
    assert report.outcome is Outcome.NOT_INCLUDED


def test_all_disclosure_subsets_preserve_authentication_and_counting(authenticated):
    ring, log = authenticated
    size = len(log.records)
    for count in range(size + 1):
        for subset in combinations(range(size), count):
            bundle = build_bundle(log, reveal=list(subset))
            bundle.records.reverse()
            bundle.inclusion_proofs.reverse()
            report = verify_bundle(bundle, ring.trust_bundle())
            assert report.outcome is (Outcome.OK if count == size else Outcome.INCOMPLETE), subset
            assert all(check.passed for check in report.checks[:4]), subset
            assert report.designer_identity == (ring.designer.keyid if 0 in subset else "unverified")
            assert report.unrevealed_leaves == size - count


@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 8, 9, 15, 16, 17, 31, 32, 33])
def test_every_leaf_verifies_at_balanced_and_unbalanced_tree_sizes(size):
    leaves = [f"leaf-{i}".encode() for i in range(size)]
    tree = RecordTree.from_leaves(leaves)
    for index, leaf in enumerate(leaves):
        assert check_inclusion(leaf, tree.root_hex, tree.prove_inclusion(index))
        assert not check_inclusion(leaf + b"changed", tree.root_hex, tree.prove_inclusion(index))


def test_inclusion_against_independently_constructed_three_leaf_tree():
    # Independent RFC 6962 hash expressions, not RecordTree's production helpers.
    a, b, c = [hashlib.sha256(b"\x00" + leaf).digest() for leaf in (b"a", b"b", b"c")]
    left = hashlib.sha256(b"\x01" + a + b).digest()
    root = hashlib.sha256(b"\x01" + left + c).hexdigest()
    proof = {"metadata": {"algorithm": "sha256", "security": True, "size": 3},
             "leaf_index": 2, "rule": [1, 0], "subset": [], "path": [c.hex(), left.hex()]}
    assert check_inclusion(b"c", root, proof)
    proof["leaf_index"] = 1
    assert not check_inclusion(b"c", root, proof)


@pytest.mark.parametrize("session", ["../escape", "nested/escape", "nested\\escape", ".", "..", ""])
def test_session_id_cannot_be_a_filesystem_path(tmp_path, session):
    root = tmp_path / "logs"
    with pytest.raises(ValueError, match="session"):
        SessionLog(root, session)
    assert not root.exists()
    assert not (tmp_path / "escape").exists()


def test_absolute_session_path_is_rejected_before_any_write(tmp_path):
    outside = tmp_path / "outside"
    with pytest.raises(ValueError, match="session"):
        SessionLog(tmp_path / "logs", str(outside))
    assert not outside.exists()


def test_session_load_cannot_escape_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "records.jsonl").write_bytes(b"")
    with pytest.raises(ValueError, match="session"):
        SessionLog.load(tmp_path / "logs", "../outside")
    assert not (tmp_path / "logs").exists()


def test_session_load_cannot_follow_symlink_outside_root(tmp_path):
    root, outside = tmp_path / "logs", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "records.jsonl").write_bytes(b"")
    (root / "linked-session").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="session"):
        SessionLog.load(root, "linked-session")
    assert not (root / "blobs").exists()


@pytest.mark.parametrize("loaded", [False, True])
@pytest.mark.parametrize("operation", ["append", "seal"])
def test_sealed_session_cannot_be_extended_or_resealed(authenticated, loaded, operation):
    ring, source = authenticated
    log = SessionLog.load(source.root, source.session_uuid) if loaded else source
    before = {p.name: p.read_bytes() for p in log.dir.iterdir()}
    root_before = log.tree.root_hex
    with pytest.raises(ChainError, match="sealed"):
        if operation == "append":
            log.append(source.records[-1])
        else:
            log.seal(ring.recorder, recorder_identity="replacement", environment=source.records[0].environment)
    assert {p.name: p.read_bytes() for p in log.dir.iterdir()} == before
    assert log.tree.root_hex == root_before
    assert verify_bundle(build_bundle(log), ring.trust_bundle()).outcome is Outcome.OK


def test_malformed_proof_has_a_clean_cli_exit(authenticated, tmp_path, capsys):
    from vatlog.cli import main
    ring, log = authenticated
    bundle = build_bundle(log)
    bundle.inclusion_proofs[0] = {"leaf_index": [0]}
    archive, trust = tmp_path / "invalid.zip", tmp_path / "trust.json"
    bundle.save(archive)
    ring.trust_bundle().save(trust)
    assert main(["verify", str(archive), "--trust", str(trust)]) == 4
    assert "=> NOT_INCLUDED" in capsys.readouterr().out


def test_nucleotide_output_remains_a_valid_sequence_binding(authenticated, tmp_path):
    ring, source = authenticated
    log = SessionLog(tmp_path / "nucleotides", source.session_uuid)
    submitted = "ACGTACGT"
    for index, record in enumerate(source.records):
        record = record.model_copy(deep=True)
        if index == 1:
            record.results = [log.blobs.artifact("nt_sequence", "design.nt", submitted.encode())]
        for artifact in record.inputs + record.results:
            if source.blobs.has(artifact.sha256):
                log.blobs.put(source.blobs.get(artifact.sha256))
        log.append(record)
    log.seal(ring.recorder, recorder_identity="test", environment=source.records[0].environment)
    assert verify_bundle(build_bundle(log), ring.trust_bundle(), submitted_sequence=submitted).outcome is Outcome.OK
