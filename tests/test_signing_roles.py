"""Role trust, statement authentication and simulated receipt rejection paths."""

import base64
import itertools
import json
import os
from datetime import timedelta
from pathlib import Path
import subprocess
import sys

import pytest

from demo.demo import run
from demo.sessions import DESIGNS, build_clean
from vatlog.attest import pae, seal, verify_envelope
from vatlog.bundle import Bundle, build_bundle
from vatlog.head import verify_head
from vatlog.keys import generate_key, save_private, public_b64
from vatlog.notary import issue_receipt, verify_receipt
from vatlog.roles import Keyring, LocalSigner, Role, TrustBundle, TrustEntry
from vatlog.schema import RecordType
from vatlog.store import SessionLog
from vatlog.verify import Outcome, verify_bundle


@pytest.fixture
def signed(tmp_path):
    ring = Keyring.generate()
    log, *_ = build_clean(tmp_path, keyring=ring)
    bundle = build_bundle(log)
    bundle.notary_receipt = issue_receipt(bundle, ring.notary, ring.trust_bundle())
    from vatlog.transparency import TransparencyLog
    with TransparencyLog(tmp_path / "transparency.sqlite", ring.trust_bundle(),
                         signer=ring.transparency_log, log_id="test") as transparency:
        bundle.transparency_evidence = transparency.append(bundle)
    return ring, log, bundle


def resign_head(bundle, signer):
    head = bundle.signed_tree_head
    head.keyid = signer.keyid
    head.signature = signer.sign(head.signing_payload())


def rebuild(tmp_path, ring, bundle):
    log = SessionLog(tmp_path / "rebuilt", bundle.session_uuid)
    for data in bundle.blobs.values():
        log.blobs.put(data)
    for record in bundle.records:
        log.append(record)
    log.seal(ring.recorder, recorder_identity="test", environment=bundle.records[0].environment)
    return build_bundle(log)


def test_public_trust_round_trip_and_local_statuses(signed, tmp_path):
    ring, log, bundle = signed
    path = tmp_path / "trust.json"
    ring.trust_bundle().save(path)
    assert "private" not in path.read_text()
    bundle.save(tmp_path / "bundle.zip")
    report = verify_bundle(Bundle.load(tmp_path / "bundle.zip"), TrustBundle.load(path))
    assert report.outcome is Outcome.OK
    assert report.signer_roles == "role-bound"
    assert report.designer_identity == ring.designer.keyid
    assert report.notarization == "simulated (local-null-screen/1)"
    assert report.independent_timestamp == "unavailable (local clock)"
    assert log.records[0].record_type is RecordType.SESSION_OPEN
    assert log.records[0].designer_statement.keyid == ring.designer.keyid
    assert len({ring.designer.keyid, ring.recorder.keyid, ring.notary.keyid, ring.transparency_log.keyid}) == 4


@pytest.mark.parametrize("role", [Role.DESIGNER, Role.NOTARY, "unlisted"])
def test_wrong_role_or_unlisted_head_signer_rejected(signed, role):
    ring, _, bundle = signed
    signer = LocalSigner(Role.RECORDER, generate_key()) if role == "unlisted" else getattr(ring, role.value)
    resign_head(bundle, signer)
    assert verify_bundle(bundle, ring.trust_bundle()).outcome is Outcome.BAD_SIGNATURE


def test_recorder_cannot_sign_notary_receipt(signed):
    ring, _, bundle = signed
    receipt = bundle.notary_receipt
    receipt.keyid = ring.recorder.keyid
    receipt.signature = ring.recorder.sign(receipt.signing_payload())
    assert verify_bundle(bundle, ring.trust_bundle()).outcome is Outcome.BAD_SIGNATURE


@pytest.mark.parametrize("field,value", [
    ("session_uuid", "other-session"), ("tree_size", 99), ("root_hash", "ab" * 32),
])
def test_signed_attestation_must_agree_with_head(signed, field, value):
    ring, _, bundle = signed
    att = bundle.attestation.attestation().model_dump()
    att["environment"] = bundle.attestation.attestation().environment
    att[field] = value
    att.pop("assurance_tier")
    bundle.attestation = seal(ring.recorder, **att)
    assert verify_bundle(bundle, ring.trust_bundle()).outcome is Outcome.BAD_SIGNATURE


@pytest.mark.parametrize("field,value", [
    ("session_uuid", "other"), ("head_root", "ab" * 32), ("tree_size", 99),
    ("attestation_digest", "ab" * 32), ("policy_id", "imaginary-screen/1"),
])
def test_valid_receipt_signature_cannot_mask_wrong_binding(signed, field, value):
    ring, _, bundle = signed
    setattr(bundle.notary_receipt, field, value)
    bundle.notary_receipt.signature = ring.notary.sign(bundle.notary_receipt.signing_payload())
    assert verify_bundle(bundle, ring.trust_bundle()).outcome is Outcome.BAD_SIGNATURE


@pytest.mark.parametrize("attack", ["user_id", "statement_session", "signature", "missing", "type", "purpose", "misplaced"])
def test_designer_identity_rejected_even_when_recorder_reseals(signed, tmp_path, attack):
    ring, _, bundle = signed
    opening = bundle.records[0]
    if attack == "user_id":
        bundle.records[-1].user_session.user_id = ring.recorder.keyid
    elif attack == "statement_session":
        opening.designer_statement.session_uuid = "another"
        opening.designer_statement.signature = ring.designer.sign(opening.designer_statement.signing_payload())
    elif attack == "signature":
        opening.designer_statement.signature = base64.b64encode(b"x" * 64).decode()
    elif attack == "missing":
        opening.designer_statement = None
    elif attack == "type":
        opening.record_type = RecordType.DESIGN_RUN
    elif attack == "purpose":
        opening.user_session.purpose_tag = "different"
    else:
        bundle.records[-1].designer_statement = opening.designer_statement
    report = verify_bundle(rebuild(tmp_path, ring, bundle), ring.trust_bundle())
    assert report.outcome is Outcome.BAD_SIGNATURE
    assert report.checks[-1].number == 2


def test_withheld_opening_leaves_identity_unverified(signed):
    ring, log, _ = signed
    report = verify_bundle(build_bundle(log, reveal=[3]), ring.trust_bundle(), submitted_sequence=DESIGNS[2])
    assert report.outcome is Outcome.INCOMPLETE
    assert report.designer_identity == "unverified"
    assert all(c.passed for c in report.checks[:4])
    revealed = verify_bundle(build_bundle(log, reveal=[0, 3]), ring.trust_bundle())
    assert revealed.designer_identity == ring.designer.keyid


@pytest.mark.parametrize("attack", ["bad_second", "unlisted_second", "missing_keyid", "empty", "payload_type", "designer_only", "bad_payload"])
def test_all_envelope_signatures_and_payload_type_checked(signed, attack):
    ring, _, bundle = signed
    envelope = bundle.attestation
    payload = base64.b64decode(envelope.payload)
    if attack == "bad_second":
        envelope.signatures.append({"keyid": ring.designer.keyid, "sig": base64.b64encode(b"x" * 64).decode()})
    elif attack == "unlisted_second":
        signer = LocalSigner(Role.RECORDER, generate_key())
        envelope.signatures.append({"keyid": signer.keyid, "sig": signer.sign(pae(envelope.payloadType, payload))})
    elif attack == "missing_keyid":
        envelope.signatures[0].pop("keyid")
    elif attack == "empty":
        envelope.signatures = []
    elif attack == "payload_type":
        envelope.payloadType = "wrong/type"
        envelope.signatures[0]["sig"] = ring.recorder.sign(pae(envelope.payloadType, payload))
    elif attack == "bad_payload":
        envelope.payload = base64.b64encode(b"{}").decode()
        envelope.signatures[0]["sig"] = ring.recorder.sign(pae(envelope.payloadType, b"{}"))
    else:
        envelope.signatures = [{"keyid": ring.designer.keyid, "sig": ring.designer.sign(pae(envelope.payloadType, payload))}]
    assert verify_bundle(bundle, ring.trust_bundle()).outcome is Outcome.BAD_SIGNATURE


def test_valid_additional_listed_signature_passes(signed):
    ring, _, bundle = signed
    envelope = bundle.attestation
    envelope.signatures.append({"keyid": ring.designer.keyid, "sig": ring.designer.sign(
        pae(envelope.payloadType, base64.b64decode(envelope.payload)))})
    assert verify_bundle(bundle, ring.trust_bundle()).outcome is Outcome.OK


def test_empty_head_keyid_falls_back_and_is_reported(signed):
    ring, _, bundle = signed
    bundle.signed_tree_head.keyid = ""
    bundle.signed_tree_head.signature = ring.recorder.sign(bundle.signed_tree_head.signing_payload())
    report = verify_bundle(bundle, ring.trust_bundle())
    assert report.outcome is Outcome.OK
    assert "fallback" in report.head_key_resolution


@pytest.mark.parametrize("attack,expected", [
    ("clean", Outcome.OK), ("chain", Outcome.TAMPERED), ("proof", Outcome.NOT_INCLUDED),
    ("sequence", Outcome.SEQUENCE_MISMATCH), ("partial", Outcome.INCOMPLETE),
])
def test_role_bound_outcomes(signed, attack, expected):
    ring, log, bundle = signed
    sequence = DESIGNS[2]
    if attack == "chain":
        bundle.records[1].params.seeds = [99]
    elif attack == "proof":
        bundle = build_bundle(log, reveal=[3])
        bundle.records[0] = bundle.records[0].model_copy(deep=True)
        bundle.records[0].params.seeds = [99]
    elif attack == "sequence":
        sequence = "UNRECORDED"
    elif attack == "partial":
        bundle = build_bundle(log, reveal=[3])
    assert verify_bundle(bundle, ring.trust_bundle(), submitted_sequence=sequence).outcome is expected


@pytest.mark.parametrize("partial", [False, True])
def test_pinned_form_does_not_establish_identity(signed, partial):
    ring, log, _ = signed
    bundle = build_bundle(log, reveal=[3] if partial else None)
    report = verify_bundle(bundle, ring.recorder.key.public_key())
    assert report.outcome is (Outcome.INCOMPLETE if partial else Outcome.OK)
    assert report.designer_identity == "not established"
    assert "roles not distinguished" in report.signer_roles


@pytest.mark.parametrize("attack", ["chain", "last_leaf", "partial", "wrong_key", "pinned"])
def test_notary_refuses_invalid_policy_input(signed, attack):
    ring, log, bundle = signed
    signer, trust = ring.notary, ring.trust_bundle()
    if attack == "chain":
        bundle.records[1].params.seeds = [98]
    elif attack == "last_leaf":
        bundle.records[-1].params.seeds = [98]
    elif attack == "partial":
        bundle = build_bundle(log, reveal=[3])
    elif attack == "wrong_key":
        signer = ring.recorder
    else:
        trust = TrustBundle.pinned(ring.recorder.key.public_key())
    with pytest.raises(ValueError):
        issue_receipt(bundle, signer, trust)


def test_trust_rejects_duplicate_roles_keys_and_wrong_thumbprint(signed):
    ring, _, _ = signed
    entry = TrustEntry.from_signer(ring.designer)
    with pytest.raises(ValueError):
        TrustBundle(entries=[entry, entry])
    with pytest.raises(ValueError):
        TrustBundle(entries=[entry, entry.model_copy(update={"role": Role.NOTARY})])
    with pytest.raises(ValueError):
        TrustEntry(keyid="wrong", role=entry.role, public_key=entry.public_key)


def test_private_file_created_restrictively_and_never_overwritten(tmp_path):
    path = tmp_path / "designer.key"
    save_private(generate_key(), path)
    assert path.stat().st_mode & 0o777 == 0o600
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        save_private(generate_key(), path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("fixture,code", [("clean", 0), ("tampered", 2), ("doctored", 0), ("partial", 6)])
def test_demo_uses_role_bound_trust(tmp_path, capsys, fixture, code):
    assert run(fixture, tmp_path) == code
    output = capsys.readouterr().out
    if fixture != "tampered":
        assert "signer roles: role-bound" in output
        assert "notarization: simulated" in output


def test_separate_role_processes(tmp_path):
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(root / "src")}

    def command(*args):
        result = subprocess.run([sys.executable, *map(str, args)], env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    trust = tmp_path / "trust.json"
    for role in Role:
        command("-m", "vatlog.cli", "keygen", "--role", role.value,
                "--private", tmp_path / f"{role.value}.key", "--trust", trust)
    command("-m", "vatlog.cli", "sign-session", "--designer-key", tmp_path / "designer.key",
            "--session", "process-demo", "--purpose", "therapeutic lead", "--out", tmp_path / "statement.json")
    command(root / "scripts/role_recorder.py", "--recorder-key", tmp_path / "recorder.key",
            "--statement", tmp_path / "statement.json", "--root", tmp_path / "logs", "--out", tmp_path / "recorded.zip")
    command(root / "scripts/role_notary.py", tmp_path / "recorded.zip", "--notary-key", tmp_path / "notary.key",
            "--trust", trust, "--out", tmp_path / "notarized.zip")
    command("-m", "vatlog.cli", "log-init", "--store", tmp_path / "transparency.sqlite",
            "--log-id", "process-demo", "--log-key", tmp_path / "transparency_log.key", "--trust", trust)
    command(root / "scripts/role_transparency_log.py", tmp_path / "notarized.zip",
            "--store", tmp_path / "transparency.sqlite", "--log-key", tmp_path / "transparency_log.key",
            "--trust", trust, "--out", tmp_path / "anchored.zip")
    output = command(root / "scripts/role_verifier.py", tmp_path / "anchored.zip", "--trust", trust,
                     "--require-transparency")
    assert "=> OK" in output and "notarization: simulated" in output
    bundle = Bundle.load(tmp_path / "notarized.zip")
    for role in Role:
        assert (tmp_path / f"{role.value}.key").read_bytes() not in bundle.blobs.values()


@pytest.mark.parametrize("field", ["log_id", "session_uuid", "tree_size", "root_hash", "timestamp", "keyid"])
def test_every_head_field_is_covered_by_the_signature(signed, field):
    ring, _, bundle = signed
    head = bundle.signed_tree_head
    assert verify_head(head, ring.recorder.key.public_key())
    value = getattr(head, field)
    if field == "tree_size":
        value += 1
    elif field == "timestamp":
        value += timedelta(seconds=1)
    elif field == "root_hash":
        value = "ab" * 32
    else:
        value += "changed"
    setattr(head, field, value)
    assert not verify_head(head, ring.recorder.key.public_key())


@pytest.mark.parametrize("field", ["session_uuid", "keyid", "purpose_tag", "issued_at"])
def test_recorder_cannot_rewrite_any_designer_statement_field(signed, tmp_path, field):
    ring, _, bundle = signed
    statement = bundle.records[0].designer_statement
    value = getattr(statement, field)
    setattr(statement, field, value + timedelta(seconds=1) if field == "issued_at" else value + "changed")
    report = verify_bundle(rebuild(tmp_path, ring, bundle), ring.trust_bundle())
    assert report.outcome is Outcome.BAD_SIGNATURE
    assert report.designer_identity == "unverified"


@pytest.mark.parametrize("field", ["session_uuid", "head_root", "tree_size", "attestation_digest",
                                   "policy_id", "validation_result", "observed_at", "keyid",
                                   "simulated", "independent_timestamp"])
def test_every_receipt_claim_is_authenticated(signed, field):
    ring, _, bundle = signed
    receipt = bundle.notary_receipt
    trust = ring.trust_bundle()
    assert verify_receipt(receipt, bundle, trust)
    value = getattr(receipt, field)
    if type(value) is bool:
        value = not value
    elif field == "observed_at":
        value += timedelta(seconds=1)
    elif field == "tree_size":
        value += 1
    else:
        value += "changed"
    setattr(receipt, field, value)
    assert not verify_receipt(receipt, bundle, trust)


@pytest.mark.parametrize("field,value", [("simulated", False), ("independent_timestamp", True),
                                        ("validation_result", "failed")])
def test_unsupported_receipt_claims_rejected_on_archive_load(signed, tmp_path, field, value):
    _, _, bundle = signed
    setattr(bundle.notary_receipt, field, value)
    path = tmp_path / "unsupported.zip"
    bundle.save(path)
    with pytest.raises(ValueError):
        Bundle.load(path)


@pytest.mark.parametrize("target", ["head", "envelope", "designer", "receipt"])
@pytest.mark.parametrize("signature", ["", "%%%", "é", base64.b64encode(b"short").decode()])
def test_malformed_signatures_reject_cleanly(signed, target, signature):
    ring, _, bundle = signed
    if target == "head":
        bundle.signed_tree_head.signature = signature
    elif target == "envelope":
        bundle.attestation.signatures[0]["sig"] = signature
    elif target == "receipt":
        bundle.notary_receipt.signature = signature
    else:
        bundle.records = [bundle.records[0].model_copy(deep=True)]
        bundle.records[0].designer_statement.signature = signature
    report = verify_bundle(bundle, ring.trust_bundle())
    assert report.outcome is Outcome.BAD_SIGNATURE
    assert report.checks[-1].number == 2


def test_envelope_rejects_bad_signature_regardless_of_entry_order(signed):
    ring, _, bundle = signed
    envelope = bundle.attestation
    recorder = envelope.signatures[0]
    designer = {"keyid": ring.designer.keyid, "sig": ring.designer.sign(
        pae(envelope.payloadType, base64.b64decode(envelope.payload)))}
    invalid = {"keyid": ring.notary.keyid, "sig": base64.b64encode(b"x" * 64).decode()}
    for entries in itertools.permutations([recorder, designer]):
        envelope.signatures = list(entries)
        assert verify_envelope(envelope, ring.trust_bundle())
    for entries in itertools.permutations([recorder, designer, invalid]):
        envelope.signatures = list(entries)
        assert not verify_envelope(envelope, ring.trust_bundle())


def test_genuine_receipt_from_another_session_cannot_be_replayed(signed, tmp_path):
    ring, _, bundle = signed
    from demo.sessions import build_partial
    other, *_ = build_partial(tmp_path, keyring=ring)
    other_bundle = build_bundle(other)
    bundle.notary_receipt = issue_receipt(other_bundle, ring.notary, ring.trust_bundle())
    assert verify_bundle(other_bundle, ring.trust_bundle()).outcome is Outcome.OK
    assert verify_bundle(bundle, ring.trust_bundle()).outcome is Outcome.BAD_SIGNATURE


@pytest.mark.parametrize("role", list(Role))
def test_required_role_cannot_be_removed_from_trust(signed, role):
    ring, _, bundle = signed
    trust = TrustBundle(entries=[e for e in ring.trust_bundle().entries if e.role != role])
    assert verify_bundle(bundle, trust).outcome is Outcome.BAD_SIGNATURE


def test_designer_identity_requires_the_opening_inclusion_proof(signed):
    ring, _, bundle = signed
    bundle.inclusion_proofs = [p for p in bundle.inclusion_proofs if p["leaf_index"] != 0]
    report = verify_bundle(bundle, ring.trust_bundle())
    assert report.outcome is Outcome.NOT_INCLUDED
    assert report.designer_identity == "unverified"


@pytest.mark.parametrize("case", ["duplicate_role", "duplicate_key", "wrong_id", "private_field", "mixed_pinned"])
def test_untrusted_trust_json_cannot_bypass_role_validation(signed, tmp_path, case):
    ring, _, _ = signed
    data = ring.trust_bundle().model_dump(mode="json")
    if case == "duplicate_role":
        data["entries"][1]["role"] = "designer"
    elif case == "duplicate_key":
        data["entries"][1].update(keyid=data["entries"][0]["keyid"], public_key=data["entries"][0]["public_key"])
    elif case == "wrong_id":
        data["entries"][0]["keyid"] = ring.recorder.keyid
    elif case == "private_field":
        data["entries"][0]["private_key"] = "not allowed"
    else:
        data["pinned_key"] = public_b64(ring.recorder.key.public_key())
    path = tmp_path / "invalid-trust.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        TrustBundle.load(path)


def test_keygen_rejection_preserves_existing_key_and_trust(signed, tmp_path):
    from vatlog.cli import main
    ring, _, _ = signed
    trust = tmp_path / "trust.json"
    ring.trust_bundle().save(trust)
    key = tmp_path / "recorder.key"
    save_private(ring.recorder.key, key)
    before = key.read_bytes(), trust.read_bytes()
    with pytest.raises(SystemExit) as exc:
        main(["keygen", "--role", "recorder", "--private", str(key), "--trust", str(trust)])
    assert exc.value.code == 2
    assert (key.read_bytes(), trust.read_bytes()) == before


def test_private_key_creation_refuses_symlink_target(tmp_path):
    target = tmp_path / "existing"
    target.write_bytes(b"do not overwrite")
    link = tmp_path / "key"
    link.symlink_to(target)
    with pytest.raises(FileExistsError):
        save_private(generate_key(), link)
    assert target.read_bytes() == b"do not overwrite"


def test_failed_notarization_does_not_overwrite_output(signed, tmp_path):
    from vatlog.cli import main
    ring, log, _ = signed
    partial = tmp_path / "partial.zip"
    build_bundle(log, reveal=[3]).save(partial)
    trust = tmp_path / "trust.json"
    ring.trust_bundle().save(trust)
    key = tmp_path / "notary.key"
    save_private(ring.notary.key, key)
    output = tmp_path / "existing.zip"
    output.write_bytes(b"previous artifact")
    with pytest.raises(SystemExit) as exc:
        main(["notarize", str(partial), "--notary-key", str(key), "--trust", str(trust), "--out", str(output)])
    assert exc.value.code == 2
    assert output.read_bytes() == b"previous artifact"
