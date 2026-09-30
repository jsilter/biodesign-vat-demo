"""Public Sigstore evidence: receipt binding, identity policy, hermetic publication, recovery."""

import base64
import inspect
import json
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sigstore_fakes import (IDENTITY, FakeSigstore, TestTsa, forged_identity_token, notarized_bundle,
                            other_tsa_chain)
from vatlog.bundle import Bundle
from vatlog.canon import canonical_record_bytes, sha256_hex
from vatlog.cli import PUBLICATION_UNKNOWN_EXIT, main
from vatlog.roles import Keyring
from vatlog.sigstore_transparency import (
    CLOCK_SKEW, DEFAULT_ISSUER, PublicationError, PublicationOutcomeUnknown, ProductionSigstoreServices,
    SigstoreTransparencyEvidence, anchor_receipt, mint_identity_token, receipt_payload_digest, recover_anchor,
    standard_bundle, verify_sigstore_inclusion,
)
from vatlog.verify import verify_bundle

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl binary needed to mint test RFC 3161 tokens")


@pytest.fixture(scope="session")
def tsa(tmp_path_factory):
    return TestTsa(tmp_path_factory.mktemp("tsa"))


@pytest.fixture(scope="session")
def ring():
    return Keyring.generate()


@pytest.fixture
def anchored(tmp_path, tsa, ring):
    fake = FakeSigstore(tsa)
    root = fake.trusted_root()
    source = notarized_bundle(tmp_path / "session", ring)
    bundle, inclusion = anchor_receipt(source, ring.trust_bundle(), fake, acknowledged=True, trusted_root=root,
                                       recovery_dir=tmp_path / "recovery", token_source=forged_identity_token)
    return source, bundle, fake, root, inclusion


def outcome(bundle, trust, root, identity=IDENTITY, **kwargs):
    return verify_bundle(bundle, trust, sigstore_trusted_root=root, sigstore_identity=identity, **kwargs).exit_code


# --- surface guards ---------------------------------------------------------------

def test_publisher_and_cli_expose_no_interactive_or_extra_credential_path(capsys):
    forbidden = re.compile(r"api[_-]?key|password|client[_-]?secret|oidc[_-]?login|browser|device[_-]?code", re.I)
    for func in (anchor_receipt, recover_anchor, ProductionSigstoreServices.__init__):
        assert not any(forbidden.search(name) for name in inspect.signature(func).parameters)
    with pytest.raises(SystemExit):
        main(["anchor-sigstore", "--help"])
    help_text = capsys.readouterr().out
    assert not any(forbidden.search(line) for line in help_text.splitlines() if line.strip().startswith("--"))
    assert "permanent" in help_text and "execution time" in help_text and "service-account email" in help_text
    for phrase in ("trusted execution", "safety screen", "proof of execution time"):
        assert phrase not in help_text.lower()


def test_service_account_key_must_be_owner_only_and_never_echoed(tmp_path):
    key = tmp_path / "sa.json"
    key.write_text(json.dumps({"type": "service_account", "client_email": "x@y.iam.gserviceaccount.com", "private_key": "SECRET-MARKER"}))
    key.chmod(0o644)
    with pytest.raises(PublicationError, match="owner-only") as exc:
        mint_identity_token(key)
    assert "SECRET-MARKER" not in str(exc.value) and str(key) not in str(exc.value)
    key.chmod(0o600)
    with pytest.raises(PublicationError) as exc:
        mint_identity_token(key)  # malformed key material; fails before any network use
    assert "SECRET-MARKER" not in str(exc.value) and str(key) not in str(exc.value)


# --- bytes, digest and submitted content ---------------------------------------------

def test_receipt_digest_covers_exact_signing_payload_and_is_sensitive(tmp_path, ring):
    receipt = notarized_bundle(tmp_path, ring).notary_receipt
    assert receipt_payload_digest(receipt) == sha256_hex(receipt.signing_payload())
    shifted = receipt.model_copy(update={"observed_at": receipt.observed_at + timedelta(seconds=1)})
    assert receipt_payload_digest(shifted) != receipt_payload_digest(receipt)
    assert receipt_payload_digest(receipt.model_copy(update={"signature": "AAAA"})) == receipt_payload_digest(receipt)


def test_submitted_material_is_digest_signature_and_certificate_only(anchored):
    source, bundle, fake, _, _ = anchored
    receipt = bundle.notary_receipt
    assert len(fake.requests) == 1 and len(fake.tokens) == 1
    body = fake.requests[0]["hashedRekordRequestV002"]
    assert set(body) == {"digest", "signature"} and set(body["signature"]["verifier"]) == {"x509Certificate", "keyDetails"}
    assert base64.b64decode(body["digest"]).hex() == receipt_payload_digest(receipt)
    serialized = json.dumps(fake.requests) + json.dumps(bundle.sigstore_evidence.bundle)
    for private in (source.session_uuid, receipt.head_root, receipt.attestation_digest, receipt.signature, *source.blobs):
        assert private not in serialized
    assert bundle.sigstore_evidence.identity == IDENTITY and bundle.sigstore_evidence.issuer == DEFAULT_ISSUER
    assert fake.tokens[0].identity == IDENTITY


# --- evidence object and archive compatibility --------------------------------------

def test_evidence_round_trips_and_leaves_records_unchanged(anchored, tmp_path, ring):
    source, bundle, _, root, _ = anchored
    assert source.sigstore_evidence is None
    assert [canonical_record_bytes(r) for r in bundle.records] == [canonical_record_bytes(r) for r in source.records]
    assert bundle.notary_receipt == source.notary_receipt
    bundle.save(tmp_path / "anchored.zip")
    loaded = Bundle.load(tmp_path / "anchored.zip")
    assert loaded.sigstore_evidence == bundle.sigstore_evidence
    assert standard_bundle(loaded.sigstore_evidence)._inner.to_dict() == loaded.sigstore_evidence.bundle
    assert outcome(loaded, ring.trust_bundle(), root, require_public_transparency=True) == 0


@pytest.mark.parametrize("case", ["extra", "media", "size", "dsse", "no_certificate", "issuer_scheme", "noncanonical"])
def test_strict_parsing_and_size_bounds(anchored, case):
    _, bundle, _, _, _ = anchored
    data = bundle.sigstore_evidence.model_dump(mode="json")
    if case == "extra":
        data["log_index"] = 1
    elif case == "media":
        data["bundle"]["mediaType"] = "application/vnd.dev.sigstore.bundle+json;version=0.1"
    elif case == "size":
        data["bundle"]["verificationMaterial"]["padding"] = "x" * (256 * 1024)
    elif case == "dsse":
        data["bundle"]["dsseEnvelope"] = {"payload": ""}
    elif case == "no_certificate":
        del data["bundle"]["verificationMaterial"]["certificate"]
    elif case == "issuer_scheme":
        data["issuer"] = "http://accounts.google.com"
    if case == "noncanonical":
        data["bundle"]["verificationMaterial"]["publicKey"] = None
        with pytest.raises(ValueError, match="canonical"):
            standard_bundle(SigstoreTransparencyEvidence.model_validate(data))
    else:
        with pytest.raises(ValueError):
            SigstoreTransparencyEvidence.model_validate(data)


# --- publication guards, ambiguity and recovery ---------------------------------------

def test_no_credential_read_and_no_request_without_acknowledgement_or_with_invalid_input(tmp_path, tsa, ring):
    fake = FakeSigstore(tsa)
    root = fake.trusted_root()
    trust = ring.trust_bundle()
    reads = []

    def token_source():
        reads.append(1)
        return forged_identity_token()

    source = notarized_bundle(tmp_path / "s", ring)
    with pytest.raises(PublicationError, match="acknowledgement"):
        anchor_receipt(source, trust, fake, acknowledged=False, trusted_root=root, recovery_dir=tmp_path, token_source=token_source)
    stripped = source.model_copy(deep=True)
    stripped.notary_receipt = None
    with pytest.raises(PublicationError, match="not publishable"):
        anchor_receipt(stripped, trust, fake, acknowledged=True, trusted_root=root, recovery_dir=tmp_path, token_source=token_source)
    with pytest.raises(PublicationError, match="not publishable"):
        anchor_receipt(source, Keyring.generate().trust_bundle(), fake, acknowledged=True, trusted_root=root,
                       recovery_dir=tmp_path, token_source=token_source)
    assert reads == [] and fake.requests == [] and not list(tmp_path.glob("*.submitted"))


def test_certificate_failure_stops_before_publication(tmp_path, tsa, ring):
    fake = FakeSigstore(tsa, fulcio_failure=PublicationError("Certificate issuance failed before publication: X"))
    with pytest.raises(PublicationError, match="Certificate issuance"):
        anchor_receipt(notarized_bundle(tmp_path / "s", ring), ring.trust_bundle(), fake, acknowledged=True,
                       trusted_root=fake.trusted_root(), recovery_dir=tmp_path / "rec", token_source=forged_identity_token)
    assert fake.requests == [] and not list((tmp_path / "rec").glob("*.submitted"))


@pytest.mark.parametrize("failure", [PublicationOutcomeUnknown("HTTP 502"), TimeoutError("read timed out")])
def test_ambiguous_submission_is_reported_and_never_resubmitted_silently(tmp_path, tsa, ring, failure):
    fake = FakeSigstore(tsa, rekor_failure=failure)
    root, trust = fake.trusted_root(), ring.trust_bundle()
    source = notarized_bundle(tmp_path / "s", ring)
    with pytest.raises(PublicationOutcomeUnknown):
        anchor_receipt(source, trust, fake, acknowledged=True, trusted_root=root, recovery_dir=tmp_path, token_source=forged_identity_token)
    marker = tmp_path / f"{receipt_payload_digest(source.notary_receipt)}.submitted"
    assert marker.exists() and (marker.stat().st_mode & 0o777) == 0o600 and len(fake.requests) == 1
    fake.rekor_failure = None
    with pytest.raises(PublicationError, match="already attempted"):
        anchor_receipt(source, trust, fake, acknowledged=True, trusted_root=root, recovery_dir=tmp_path, token_source=forged_identity_token)
    assert len(fake.requests) == 1
    bundle, _ = anchor_receipt(source, trust, fake, acknowledged=True, trusted_root=root, recovery_dir=tmp_path,
                               token_source=forged_identity_token, allow_duplicate=True)
    assert len(fake.requests) == 2 and outcome(bundle, trust, root, require_public_transparency=True) == 0


def test_definitive_rejection_clears_the_attempt_marker(tmp_path, tsa, ring):
    fake = FakeSigstore(tsa, rekor_failure=PublicationError("Rekor rejected the submission (HTTP 400)"))
    root, trust = fake.trusted_root(), ring.trust_bundle()
    source = notarized_bundle(tmp_path / "s", ring)
    with pytest.raises(PublicationError, match="rejected"):
        anchor_receipt(source, trust, fake, acknowledged=True, trusted_root=root, recovery_dir=tmp_path, token_source=forged_identity_token)
    assert not list(tmp_path.glob("*.submitted"))
    fake.rekor_failure = None
    anchor_receipt(source, trust, fake, acknowledged=True, trusted_root=root, recovery_dir=tmp_path, token_source=forged_identity_token)


def test_published_without_timestamp_is_an_unknown_outcome(tmp_path, tsa, ring):
    # The official client cannot build a Rekor v2 bundle without a timestamp, so a TSA outage
    # after submission surfaces as an exception once the entry already exists.
    fake = FakeSigstore(tsa, omit_timestamp=True)
    source = notarized_bundle(tmp_path / "s", ring)
    with pytest.raises(PublicationOutcomeUnknown, match="unknown"):
        anchor_receipt(source, ring.trust_bundle(), fake, acknowledged=True, trusted_root=fake.trusted_root(),
                       recovery_dir=tmp_path / "rec", token_source=forged_identity_token)
    assert (tmp_path / "rec" / f"{receipt_payload_digest(source.notary_receipt)}.submitted").exists()
    assert len(fake.requests) == 1


def test_recovery_reuses_saved_response_without_network(anchored, tmp_path, ring):
    source, bundle, fake, root, _ = anchored
    saved = tmp_path / "recovery" / f"{bundle.sigstore_evidence.receipt_digest}.sigstore.json"
    assert saved.exists() and (saved.stat().st_mode & 0o777) == 0o600
    assert json.loads(saved.read_bytes()) == bundle.sigstore_evidence.bundle
    recovered, _ = recover_anchor(source, ring.trust_bundle(), saved, root, identity=IDENTITY)
    assert recovered.sigstore_evidence == bundle.sigstore_evidence and len(fake.requests) == 1
    with pytest.raises(ValueError):
        recover_anchor(source, ring.trust_bundle(), saved, root, identity="someone-else@example.com")
    with pytest.raises(ValueError):
        recover_anchor(notarized_bundle(tmp_path / "other", ring), ring.trust_bundle(), saved, root, identity=IDENTITY)


# --- verification policy matrix ------------------------------------------------------

def test_policy_matrix_neither_local_public_both(anchored, tmp_path, ring):
    source, public, fake, root, _ = anchored
    trust = ring.trust_bundle()
    local_only = notarized_bundle(tmp_path / "local", ring, local_log=True)
    recorder_only = source.model_copy(deep=True)
    recorder_only.notary_receipt = None
    assert verify_bundle(recorder_only, trust).exit_code == 0
    assert verify_bundle(recorder_only, trust, require_transparency=True).exit_code == 4
    assert verify_bundle(recorder_only, trust, require_public_transparency=True).exit_code == 4
    assert verify_bundle(source, trust).exit_code == 4
    assert verify_bundle(local_only, trust, require_transparency=True).exit_code == 0
    assert verify_bundle(local_only, trust, require_public_transparency=True).exit_code == 4
    assert outcome(public, trust, root) == 0 and outcome(public, trust, root, require_transparency=True) == 0
    assert outcome(public, trust, root, require_public_transparency=True) == 0
    assert verify_bundle(public, trust, sigstore_identity=IDENTITY).exit_code == 4  # no trusted root
    assert verify_bundle(public, trust, sigstore_trusted_root=root).exit_code == 3  # no expected identity
    both, _ = anchor_receipt(local_only, trust, fake, acknowledged=True, trusted_root=root,
                             recovery_dir=tmp_path / "both", token_source=forged_identity_token)
    report = verify_bundle(both, trust, sigstore_trusted_root=root, sigstore_identity=IDENTITY,
                           require_transparency=True, require_public_transparency=True)
    assert report.exit_code == 0
    text = report.render()
    assert "transparency: simulated local log; inclusion verified" in text
    assert "public transparency: Sigstore Rekor inclusion verified" in text and "RFC 3161 timestamp:" in text
    assert f"public signing identity: {IDENTITY} (issuer {DEFAULT_ISSUER})" in text
    assert "local transparency inclusion verified; public Rekor inclusion verified" in text
    broken_local = both.model_copy(deep=True)
    broken_local.transparency_evidence.inclusion_proof["leaf_index"] = 5
    assert outcome(broken_local, trust, root) == 4
    broken_public = both.model_copy(deep=True)
    broken_public.sigstore_evidence.bundle["verificationMaterial"]["tlogEntries"][0]["logIndex"] = "0"
    assert outcome(broken_public, trust, root, require_transparency=True) == 4
    stripped = public.model_copy(deep=True)
    stripped.sigstore_evidence = None
    assert outcome(stripped, trust, root, require_public_transparency=True) == 4
    public_report = verify_bundle(public, trust, sigstore_trusted_root=root, sigstore_identity=IDENTITY)
    assert public_report.notarization.startswith("simulated") and public.notary_receipt.simulated is True
    assert "publication time" in public_report.independent_timestamp and "transparency: absent" in public_report.render()


@pytest.mark.parametrize("target", [
    "receipt", "expected_identity", "expected_issuer", "claimed_identity", "certificate_identity", "certificate_issuer",
    "signature", "digest", "fulcio_root", "ct_log", "rekor_key", "log_id", "log_index", "checkpoint", "inclusion_path",
    "body", "timestamp_token", "missing_tsa", "duplicate_tsa", "tsa_chain", "time_ordering", "future_time",
    "integrated_time_ignored",
])
def test_rejects_altered_public_evidence(anchored, tmp_path, tsa, ring, target):
    source, bundle, fake, root, inclusion = anchored
    bundle = bundle.model_copy(deep=True)
    evidence = bundle.sigstore_evidence
    material = evidence.bundle["verificationMaterial"]
    entry = material["tlogEntries"][0]
    trust, now, identity, expected = ring.trust_bundle(), None, IDENTITY, 4
    if target == "receipt":
        bundle.notary_receipt = notarized_bundle(tmp_path / "other", ring).notary_receipt
        expected = 3
    elif target == "expected_identity":
        identity, expected = "someone-else@example.com", 3
    elif target == "expected_issuer":
        report = verify_bundle(bundle, trust, sigstore_trusted_root=root, sigstore_identity=IDENTITY,
                               sigstore_issuer="https://token.actions.githubusercontent.com")
        assert report.exit_code == 3
        return
    elif target == "claimed_identity":
        bundle.sigstore_evidence = evidence.model_copy(update={"identity": "someone-else@example.com"})
        expected = 3
    elif target == "certificate_identity":
        other = FakeSigstore(tsa)
        other.rekor, other.fulcio = fake.rekor, fake.fulcio
        forged, _ = anchor_receipt(source, trust, other, acknowledged=True, trusted_root=root, recovery_dir=tmp_path / "f",
                                   token_source=lambda: forged_identity_token("someone-else@example.com"))
        bundle.sigstore_evidence = forged.sigstore_evidence.model_copy(update={"identity": IDENTITY})
    elif target == "certificate_issuer":
        other = FakeSigstore(tsa)
        other.rekor, other.fulcio = fake.rekor, fake.fulcio
        forged, _ = anchor_receipt(source, trust, other, acknowledged=True, trusted_root=root, recovery_dir=tmp_path / "f",
                                   token_source=lambda: forged_identity_token(IDENTITY, "https://oauth2.sigstore.dev/auth"))
        bundle.sigstore_evidence = forged.sigstore_evidence.model_copy(update={"issuer": DEFAULT_ISSUER})
    elif target == "signature":
        evidence.bundle["messageSignature"]["signature"] = base64.b64encode(b"\x30\x06\x02\x01\x01\x02\x01\x01").decode()
    elif target == "digest":
        evidence.bundle["messageSignature"]["messageDigest"]["digest"] = base64.b64encode(b"\x00" * 32).decode()
        expected = 3
    elif target == "fulcio_root":
        root = fake.trusted_root(fulcio_root=FakeSigstore(tsa).fulcio.root.public_bytes(
            __import__("cryptography.hazmat.primitives.serialization", fromlist=["Encoding"]).Encoding.DER))
    elif target == "ct_log":
        root = fake.trusted_root(ct_spki=FakeSigstore(tsa).fulcio.ct_key.public_key().public_bytes(
            __import__("cryptography.hazmat.primitives.serialization", fromlist=["Encoding"]).Encoding.DER,
            __import__("cryptography.hazmat.primitives.serialization", fromlist=["PublicFormat"]).PublicFormat.SubjectPublicKeyInfo))
    elif target == "rekor_key":
        root = fake.trusted_root(rekor_spki=FakeSigstore(tsa).rekor.spki)
    elif target == "log_id":
        entry["logId"]["keyId"] = base64.b64encode(b"\x01" * 32).decode()
    elif target == "log_index":
        entry["logIndex"] = entry["inclusionProof"]["logIndex"] = "0"
    elif target == "checkpoint":
        entry["inclusionProof"]["checkpoint"]["envelope"] = entry["inclusionProof"]["checkpoint"]["envelope"].replace("\n2\n", "\n3\n")
    elif target == "inclusion_path":
        entry["inclusionProof"]["hashes"][0] = base64.b64encode(b"\x00" * 32).decode()
    elif target == "body":
        entry["canonicalizedBody"] = base64.b64encode(json.dumps({"kind": "hashedrekord", "apiVersion": "0.0.2",
                                                                  "spec": {}}).encode()).decode()
    elif target == "timestamp_token":
        material["timestampVerificationData"]["rfc3161Timestamps"] = [
            {"signedTimestamp": base64.b64encode(tsa.timestamp(b"another message")).decode()}]
    elif target == "missing_tsa":
        material["timestampVerificationData"]["rfc3161Timestamps"] = []
    elif target == "duplicate_tsa":
        material["timestampVerificationData"]["rfc3161Timestamps"] *= 2
    elif target == "tsa_chain":
        root = fake.trusted_root(tsa_chain=other_tsa_chain())
    elif target == "time_ordering":
        future = notarized_bundle(tmp_path / "future", ring, observed_at=datetime.now(timezone.utc) + timedelta(days=30))
        with pytest.raises(PublicationOutcomeUnknown, match="precedes the receipt"):
            anchor_receipt(future, trust, fake, acknowledged=True, trusted_root=root, recovery_dir=tmp_path / "future",
                           token_source=forged_identity_token)
        return
    elif target == "future_time":
        now = inclusion.timestamp - CLOCK_SKEW - timedelta(seconds=1)
    elif target == "integrated_time_ignored":
        entry["integratedTime"] = str(int(datetime(2000, 1, 1, tzinfo=timezone.utc).timestamp()))
        report = verify_bundle(bundle, trust, sigstore_trusted_root=root, sigstore_identity=IDENTITY)
        assert report.exit_code == 0 and "2000" not in report.rfc3161_timestamp
        assert report.rfc3161_timestamp.startswith(inclusion.timestamp.isoformat())
        return
    report = verify_bundle(bundle, trust, sigstore_trusted_root=root, sigstore_identity=identity, now=now)
    assert report.exit_code == expected, report.render()
    if expected == 4:
        assert report.checks[-1].number == 3 and report.public_transparency == "absent"


def test_inclusion_verifier_reports_time_as_publication_not_execution(anchored, ring):
    _, bundle, _, root, inclusion = anchored
    result = verify_sigstore_inclusion(bundle.sigstore_evidence, bundle, ring.trust_bundle(), root)
    assert result == inclusion and result.log_index == 1 and result.tree_size == 2
    assert result.timestamp >= bundle.notary_receipt.observed_at - CLOCK_SKEW
    assert result.checkpoint_origin == "vat-fake-rekor - 20260920"


# --- CLI ------------------------------------------------------------------------------

def test_cli_anchor_verify_and_recover(tmp_path, tsa, ring, capsys, monkeypatch):
    fake = FakeSigstore(tsa)
    root = fake.trusted_root()
    root_file = tmp_path / "trusted_root.json"
    root_file.write_text(fake.trusted_root_json())
    source_path, trust_path = tmp_path / "notarized.zip", tmp_path / "trust.json"
    notarized_bundle(tmp_path / "session", ring).save(source_path)
    ring.trust_bundle().save(trust_path)
    token_file = tmp_path / "token.txt"
    token_file.write_text(forged_identity_token()._raw_token)
    before = source_path.read_bytes()
    output = tmp_path / "anchored.zip"
    base = ["anchor-sigstore", str(source_path), "--output", str(output), "--trust", str(trust_path),
            "--identity-token-file", str(token_file)]
    assert main(base, sigstore_services=fake, sigstore_trusted_root=root) == 2
    preview = capsys.readouterr().out
    digest = receipt_payload_digest(Bundle.load(source_path).notary_receipt)
    assert digest in preview and "no credential was read" in preview and "cannot be removed" in preview
    assert fake.requests == [] and fake.tokens == [] and not output.exists()
    with pytest.raises(SystemExit) as no_credential:
        main(base[:-2] + ["--acknowledge-publication"], sigstore_services=fake, sigstore_trusted_root=root)
    assert no_credential.value.code == 2 and fake.requests == []
    with pytest.raises(SystemExit) as collision:
        main(["anchor-sigstore", str(source_path), "--output", str(source_path), "--trust", str(trust_path),
              "--identity-token-file", str(token_file), "--acknowledge-publication"],
             sigstore_services=fake, sigstore_trusted_root=root)
    assert collision.value.code == 2 and fake.requests == []
    with pytest.raises(SystemExit) as mismatch:
        main(base + ["--acknowledge-publication", "--sigstore-identity", "someone-else@example.com"],
             sigstore_services=fake, sigstore_trusted_root=root)
    assert mismatch.value.code == 2 and fake.requests == [] and "differs" in capsys.readouterr().err
    assert main(base + ["--acknowledge-publication"], sigstore_services=fake, sigstore_trusted_root=root) == 0
    out = capsys.readouterr().out
    assert "RFC 3161 publication time" in out and "not execution time" in out and "remains simulated" in out
    assert IDENTITY in out and source_path.read_bytes() == before and len(fake.requests) == 1
    recovery = tmp_path / "anchored.zip.recovery"
    assert (recovery.stat().st_mode & 0o777) == 0o700
    assert main(["verify", str(output), "--trust", str(trust_path), "--require-public-transparency",
                 "--sigstore-identity", IDENTITY, "--sigstore-trust-root", str(root_file)]) == 0
    assert "Rekor log ID / index" in capsys.readouterr().out
    assert main(["verify", str(output), "--trust", str(trust_path), "--require-transparency",
                 "--sigstore-identity", IDENTITY], sigstore_trusted_root=root) == 0
    with pytest.raises(SystemExit) as no_identity:
        main(["verify", str(output), "--trust", str(trust_path)], sigstore_trusted_root=root)
    assert no_identity.value.code == 2
    with pytest.raises(SystemExit) as existing:
        main(base + ["--acknowledge-publication"], sigstore_services=fake, sigstore_trusted_root=root)
    assert existing.value.code == 2 and len(fake.requests) == 1
    recovered = tmp_path / "recovered.zip"
    assert main(["anchor-sigstore", str(source_path), "--output", str(recovered), "--trust", str(trust_path),
                 "--recover-from", str(recovery / f"{digest}.sigstore.json"), "--sigstore-identity", IDENTITY],
                sigstore_trusted_root=root) == 0
    assert Bundle.load(recovered).sigstore_evidence == Bundle.load(output).sigstore_evidence and len(fake.requests) == 1
    assert main(["verify", str(source_path), "--trust", str(trust_path), "--require-public-transparency",
                 "--sigstore-identity", IDENTITY], sigstore_trusted_root=root) == 4


def test_cli_reports_unknown_outcome_with_distinct_exit_code(tmp_path, tsa, ring, capsys):
    fake = FakeSigstore(tsa, rekor_failure=PublicationOutcomeUnknown("HTTP 503"))
    source_path, trust_path = tmp_path / "notarized.zip", tmp_path / "trust.json"
    notarized_bundle(tmp_path / "session", ring).save(source_path)
    ring.trust_bundle().save(trust_path)
    token_file = tmp_path / "token.txt"
    token_file.write_text(forged_identity_token()._raw_token)
    output = tmp_path / "anchored.zip"
    with pytest.raises(SystemExit) as unknown:
        main(["anchor-sigstore", str(source_path), "--output", str(output), "--trust", str(trust_path),
              "--identity-token-file", str(token_file), "--acknowledge-publication", "--recovery-dir", str(tmp_path / "rec")],
             sigstore_services=fake, sigstore_trusted_root=fake.trusted_root())
    assert unknown.value.code == PUBLICATION_UNKNOWN_EXIT
    assert "PUBLICATION_OUTCOME_UNKNOWN" in capsys.readouterr().err and not output.exists()
    assert list((tmp_path / "rec").glob("*.submitted"))
