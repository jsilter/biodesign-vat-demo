"""Opt-in public evidence: keyless Sigstore signing of a notary receipt digest.

The public services receive only the SHA-256 digest of ``NotaryReceipt.signing_payload()``,
signed by an ephemeral key whose Fulcio certificate names a machine identity (a Google service account). Nothing here uploads
records, sequences, blobs or transcripts. The only credential is the service-account key,
read by path after local validation and never copied, logged or echoed.

Imports of the optional ``public-transparency`` dependency group (``sigstore``,
``rfc3161-client``, ``google-auth``) stay inside functions so that local verification keeps
working without it.
"""

from __future__ import annotations

import json
import os
import re
from binascii import Error as Base64Error
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal, Protocol

from cryptography.exceptions import InvalidSignature
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .canon import sha256_hex
from .notary import NotaryReceipt, verify_receipt
from .roles import TrustBundle
from .transparency import Digest

if TYPE_CHECKING:
    from .bundle import Bundle

EVIDENCE_FORMAT = "vat-sigstore-evidence/1"
BUNDLE_MEDIA_TYPE = "application/vnd.dev.sigstore.bundle.v0.3+json"
DEFAULT_ISSUER = "https://accounts.google.com"
TOKEN_AUDIENCE = "sigstore"
MAX_EVIDENCE_BYTES = 256 * 1024
MAX_TIMESTAMPS = 4
CLOCK_SKEW = timedelta(seconds=60)
DEPENDENCY_HINT = "install the public-transparency dependency group"


class PublicationError(ValueError):
    """Publication stopped before any irreversible request; retrying is safe."""


class PublicationOutcomeUnknown(RuntimeError):
    """A Rekor submission may have happened and its result is unknown; do not resubmit blindly."""


def _dependency(module: str) -> Any:
    import importlib
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise ValueError(f"Public transparency requires {module}; {DEPENDENCY_HINT}") from exc


# --- canonical receipt material ---------------------------------------------------

def receipt_payload_digest(receipt: NotaryReceipt) -> str:
    """Lowercase SHA-256 of the exact bytes the notary signature authenticates."""
    return sha256_hex(receipt.signing_payload())


def hashed_receipt(receipt: NotaryReceipt) -> Any:
    """The prehashed input handed to the Sigstore signer and verifier."""
    hashes = _dependency("sigstore.hashes")
    common = _dependency("sigstore_models.common.v1")
    return hashes.Hashed(algorithm=common.HashAlgorithm.SHA2_256, digest=bytes.fromhex(receipt_payload_digest(receipt)))


def disclosed_receipt_fields(receipt: NotaryReceipt) -> tuple[str, ...]:
    return tuple(sorted(receipt.model_dump(mode="json", exclude={"signature"})))


@dataclass(frozen=True)
class DisclosurePreview:
    """What a public log entry makes discoverable, shown before any upload."""

    receipt_digest: str
    disclosed_fields: tuple[str, ...]
    identity: str | None = None
    issuer: str | None = None

    def render(self) -> str:
        who = (f"{self.identity} (issuer {self.issuer})" if self.identity
               else "the service-account email in the supplied key (read only after acknowledgement)")
        return "\n".join([
            "Public, permanent disclosure to Sigstore Fulcio, Rekor and a public RFC 3161 TSA:",
            f"  receipt payload digest (SHA-256): {self.receipt_digest}",
            f"  signing identity: {who}",
            "  the digest commits to these receipt fields: " + ", ".join(self.disclosed_fields),
            "Not uploaded: records, sequences, prompts, transcripts, reasoning, artifact blobs, the VAT",
            "archive, the notary key or signature, and the service-account key.",
            "Rekor entries cannot be removed. Rekor establishes inclusion; the RFC 3161 token establishes",
            "publication time. Neither proves execution time, scientific truth, safety or notary independence.",
        ])


def disclosure_preview(bundle: Bundle, trust: TrustBundle, *, identity: str | None = None,
                       issuer: str | None = None) -> DisclosurePreview:
    """Validate the receipt under verifier-owned trust and describe what would be published."""
    receipt = bundle.notary_receipt
    if receipt is None:
        raise ValueError("Public anchoring requires a notary receipt")
    if not trust.role_bound:
        raise ValueError("Public anchoring requires role-bound trust")
    if not verify_receipt(receipt, bundle, trust):
        raise ValueError("Notary receipt does not verify under the supplied trust")
    return DisclosurePreview(receipt_digest=receipt_payload_digest(receipt),
                             disclosed_fields=disclosed_receipt_fields(receipt), identity=identity, issuer=issuer)


# --- evidence object ---------------------------------------------------------------

class SigstoreTransparencyEvidence(BaseModel):
    """A standard Sigstore bundle bound to one VAT notary receipt and one signing identity.

    ``bundle`` is the protobuf JSON form of ``dev.sigstore.bundle.v1.Bundle``. ``identity`` and
    ``issuer`` record what the publisher intended; a verifier confirms them against its own
    expected values and against the certificate, never trusting the bundle's word alone.
    """

    model_config = ConfigDict(extra="forbid")

    evidence_format: Literal["vat-sigstore-evidence/1"] = EVIDENCE_FORMAT
    receipt_digest: Digest
    identity: str = Field(min_length=1, max_length=320)
    issuer: str = Field(min_length=1, max_length=256, pattern=r"^https://")
    bundle: dict[str, Any]

    @field_validator("bundle")
    @classmethod
    def structural_checks(cls, value: dict[str, Any]) -> dict[str, Any]:
        if value.get("mediaType") != BUNDLE_MEDIA_TYPE:
            raise ValueError("Unsupported Sigstore bundle media type")
        if len(json.dumps(value, separators=(",", ":")).encode()) > MAX_EVIDENCE_BYTES:
            raise ValueError("Sigstore evidence exceeds the size bound")
        material = value.get("verificationMaterial")
        if ("dsseEnvelope" in value or not isinstance(value.get("messageSignature"), dict)
                or not isinstance(material, dict) or not isinstance(material.get("certificate"), dict)):
            raise ValueError("Sigstore bundle must carry a message signature and a signing certificate")
        return value

    def summary(self) -> dict:
        """Claimed public coordinates, read without verification, for display next to the report."""
        entries = self.bundle.get("verificationMaterial", {}).get("tlogEntries", [])
        entry = entries[0] if entries and isinstance(entries[0], dict) else {}
        log_id = entry.get("logId", {}).get("keyId") if isinstance(entry.get("logId"), dict) else None
        return {"evidence_format": self.evidence_format, "receipt_digest": self.receipt_digest,
                "claimed_identity": self.identity, "claimed_issuer": self.issuer,
                "claimed_log_index": entry.get("logIndex"), "claimed_log_key_id_base64": log_id,
                "claimed_integrated_time": entry.get("integratedTime"),
                "note": "Claims copied from the evidence; only bundle_report states what verified."}


def standard_bundle(evidence: SigstoreTransparencyEvidence) -> Any:
    """Parse the evidence with the official client and require exact canonical form."""
    models = _dependency("sigstore.models")
    try:
        parsed = models.Bundle.from_json(json.dumps(evidence.bundle))
    except models.InvalidBundle as exc:
        raise ValueError(f"Malformed Sigstore bundle: {exc}") from exc
    if parsed._inner.to_dict() != evidence.bundle:
        raise ValueError("Sigstore bundle is not in canonical protobuf JSON form")
    return parsed


# --- identity tokens ----------------------------------------------------------------

def mint_identity_token(key_path: Path) -> Any:
    """Mint a Sigstore-audience OIDC token from a Google service-account key; no browser."""
    oidc = _dependency("sigstore.oidc")
    if key_path.is_symlink() or not key_path.is_file():
        raise PublicationError("Service-account key path must be a regular file")
    if key_path.stat().st_mode & 0o077:
        raise PublicationError("Service-account key must be owner-only (chmod 600)")
    service_account = _dependency("google.oauth2.service_account")
    requests_transport = _dependency("google.auth.transport.requests")
    try:
        credentials = service_account.IDTokenCredentials.from_service_account_file(
            str(key_path), target_audience=TOKEN_AUDIENCE)
        credentials.refresh(requests_transport.Request())
        return oidc.IdentityToken(credentials.token)
    except (ValueError, KeyError, OSError, oidc.IdentityError) as exc:
        raise PublicationError(f"Could not mint an identity token from the service-account key: {type(exc).__name__}") from None
    except Exception as exc:  # google-auth transport errors
        raise PublicationError(f"Google token endpoint request failed: {type(exc).__name__}") from None


def read_identity_token(path: Path) -> Any:
    """A pre-minted token with audience ``sigstore`` from another Fulcio-accepted issuer."""
    oidc = _dependency("sigstore.oidc")
    with path.open("rb") as stream:
        raw = stream.read(16385)
    if len(raw) > 16384:
        raise PublicationError("Identity token file exceeds 16 KiB")
    try:
        return oidc.IdentityToken(raw.decode().strip())
    except (UnicodeDecodeError, oidc.IdentityError) as exc:
        raise PublicationError(f"Identity token unusable: {type(exc).__name__}") from None


# --- verification -----------------------------------------------------------------

def _b64(raw: bytes) -> str:
    import base64
    return base64.b64encode(raw).decode()


def verify_sigstore_bindings(evidence: SigstoreTransparencyEvidence, bundle: Bundle, trust: TrustBundle, *,
                             identity: str | None, issuer: str | None) -> None:
    """Check 2: the evidence names this receipt and the identity the verifier expects.

    Plain dictionary reads only, so a verifier without the optional dependency still gets the
    correct BAD_SIGNATURE outcome for a mismatched receipt or identity.
    """
    receipt = bundle.notary_receipt
    if receipt is None:
        raise ValueError("Public transparency evidence requires a notary receipt")
    if not trust.role_bound:
        raise ValueError("Public transparency requires role-bound trust")
    if not verify_receipt(receipt, bundle, trust):
        raise ValueError("notary receipt invalid")
    if identity is None:
        raise ValueError("Verifier must supply the expected Sigstore signing identity")
    if evidence.identity != identity or evidence.issuer != (issuer or DEFAULT_ISSUER):
        raise ValueError("Sigstore evidence names an unexpected signing identity or issuer")
    digest = receipt_payload_digest(receipt)
    if evidence.receipt_digest != digest:
        raise ValueError("Sigstore evidence binding mismatch")
    try:
        message = evidence.bundle["messageSignature"]["messageDigest"]
        bound = message["algorithm"] == "SHA2_256" and message["digest"] == _b64(bytes.fromhex(digest))
    except (KeyError, TypeError):
        bound = False
    if not bound:
        raise ValueError("Sigstore bundle message digest does not match the receipt")


@dataclass(frozen=True)
class PublicInclusion:
    """Facts established only after official-library verification."""

    identity: str
    issuer: str
    log_id: str
    log_index: int
    tree_size: int
    checkpoint_origin: str
    timestamp: datetime
    timestamps_verified: int


def verify_timestamp(token: bytes, message: bytes, trusted_root: Any) -> datetime:
    """Verify one RFC 3161 token over ``message`` under a trusted-root TSA chain."""
    rfc3161 = _dependency("rfc3161_client")
    try:
        response = rfc3161.decode_timestamp_response(token)
    except ValueError as exc:
        raise ValueError(f"Malformed RFC 3161 token: {exc}") from exc
    for authority in trusted_root.get_timestamp_authorities():
        certificates = authority.certificates(allow_expired=True)
        if len(certificates) < 2:
            continue
        builder = rfc3161.VerifierBuilder().tsa_certificate(certificates[0]).add_root_certificate(certificates[-1])
        for certificate in certificates[1:-1]:
            builder = builder.add_intermediate_certificate(certificate)
        try:
            builder.build().verify_message(response, message)
        except rfc3161.VerificationError:
            continue
        generated = response.tst_info.gen_time
        if generated.utcoffset() is None:
            generated = generated.replace(tzinfo=timezone.utc)
        end = authority.validity_period_end
        if authority.validity_period_start <= generated and (end is None or generated < end):
            return generated
    raise ValueError("RFC 3161 token does not verify under any trusted timestamp authority")


def verify_sigstore_inclusion(evidence: SigstoreTransparencyEvidence, bundle: Bundle, trust: TrustBundle,
                              trusted_root: Any, *, now: datetime | None = None) -> PublicInclusion:
    """Check 3: the official verifier (certificate, SCT, inclusion, entry body) plus VAT time rules."""
    receipt = bundle.notary_receipt
    if receipt is None:
        raise ValueError("Public transparency evidence requires a notary receipt")
    if trusted_root is None:
        raise ValueError("Public transparency evidence requires a Sigstore trusted root")
    verifier_module = _dependency("sigstore.verify.verifier")
    policy = _dependency("sigstore.verify.policy")
    errors = _dependency("sigstore.errors")
    checkpoint_module = _dependency("sigstore._internal.rekor.checkpoint")

    parsed = standard_bundle(evidence)
    try:
        verifier_module.Verifier(trusted_root=trusted_root).verify_artifact(
            hashed_receipt(receipt), parsed, policy.Identity(identity=evidence.identity, issuer=evidence.issuer))
    except errors.VerificationError as exc:
        raise ValueError(f"Sigstore verification failed: {exc}") from exc
    entry = parsed.log_entry._inner
    if entry.kind_version.kind != "hashedrekord" or entry.kind_version.version not in ("0.0.1", "0.0.2"):
        raise ValueError("Expected a Rekor hashedrekord entry")
    # Rekor v1 reports a global index across shards next to the in-shard proof index; only
    # v2 (tiles) defines the two as equal.
    if entry.kind_version.version == "0.0.2" and entry.log_index != entry.inclusion_proof.log_index:
        raise ValueError("Rekor entry index does not match its inclusion proof")
    material = parsed._inner.verification_material
    data = material.timestamp_verification_data
    tokens = [t.signed_timestamp for t in data.rfc3161_timestamps] if data is not None else []
    if not 1 <= len(tokens) <= MAX_TIMESTAMPS:
        raise ValueError(f"Expected between 1 and {MAX_TIMESTAMPS} RFC 3161 timestamps, found {len(tokens)}")
    if len(set(tokens)) != len(tokens):
        raise ValueError("Duplicate RFC 3161 timestamp")
    earliest = min(verify_timestamp(token, parsed.signature, trusted_root) for token in tokens)
    if earliest < receipt.observed_at - CLOCK_SKEW:
        raise ValueError("RFC 3161 time precedes the receipt's local observation time")
    if earliest > (now or datetime.now(timezone.utc)) + CLOCK_SKEW:
        raise ValueError("RFC 3161 time is in the future")
    origin = checkpoint_module.SignedCheckpoint.from_text(entry.inclusion_proof.checkpoint.envelope).checkpoint.origin
    return PublicInclusion(identity=evidence.identity, issuer=evidence.issuer, log_id=entry.log_id.key_id.hex(),
                           log_index=entry.log_index, tree_size=entry.inclusion_proof.tree_size,
                           checkpoint_origin=origin, timestamp=earliest, timestamps_verified=len(tokens))


def parse_trusted_root(raw: bytes) -> Any:
    """Parse a verifier-supplied trusted root document without touching the network."""
    models = _dependency("sigstore.models")
    trustroot = _dependency("sigstore_models.trustroot.v1")
    if len(raw) > MAX_EVIDENCE_BYTES * 4:
        raise ValueError("Trusted root document exceeds the size bound")
    try:
        return models.TrustedRoot(trustroot.TrustedRoot.from_json(raw))
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError(f"Invalid Sigstore trusted root: {type(exc).__name__}") from exc


def load_trusted_root(path: Path | None, *, offline: bool = False) -> Any:
    """A verifier-supplied cached trusted root, or Sigstore's production TUF-distributed one."""
    models = _dependency("sigstore.models")
    errors = _dependency("sigstore.errors")
    from pydantic import ValidationError
    try:
        if path is not None:
            if path.stat().st_size > MAX_EVIDENCE_BYTES * 4:
                raise ValueError("Trusted root file exceeds the size bound")
            return models.TrustedRoot.from_file(str(path))
        return models.ClientTrustConfig.production(offline=offline).trusted_root
    except (ValidationError, errors.Error) as exc:
        raise ValueError(f"Invalid Sigstore trusted root: {type(exc).__name__}") from exc


# --- publication --------------------------------------------------------------------

class SigstoreServices(Protocol):
    """The one network operation publication needs: keyless signing of a digest."""

    def sign_digest(self, hashed: Any, identity_token: Any) -> dict[str, Any]:
        """Obtain a certificate, sign, timestamp and submit to Rekor; return the bundle JSON."""


class ProductionSigstoreServices:
    """Sigstore's public Fulcio, Rekor and TSA from the TUF signing configuration.

    Certificate issuance happens in ``Signer.__init__`` (before anything irreversible); the
    Rekor submission happens in ``sign_artifact``. Errors are classified on that boundary, and
    upstream error bodies are never echoed.
    """

    def __init__(self, signing_context: Any) -> None:
        self.signing_context = signing_context

    @classmethod
    def production(cls, *, offline: bool = False) -> ProductionSigstoreServices:
        models = _dependency("sigstore.models")
        sign = _dependency("sigstore.sign")
        # The TUF signing configuration selects the Rekor log version.
        config = models.ClientTrustConfig.production(offline=offline)
        context = sign.SigningContext.from_trust_config(config)
        if not context._tsa_clients:
            raise PublicationError("Signing configuration lists no timestamp authority")
        return cls(context)

    def sign_digest(self, hashed: Any, identity_token: Any) -> dict[str, Any]:
        sign = _dependency("sigstore.sign")
        rekor = _dependency("sigstore._internal.rekor")
        try:
            signer = sign.Signer(identity_token, self.signing_context, cache=True)
        except Exception as exc:  # Fulcio, identity or network failure before any Rekor call
            raise PublicationError(f"Certificate issuance failed before publication: {type(exc).__name__}") from None
        try:
            bundle = signer.sign_artifact(hashed)
        except rekor.RekorClientError as exc:
            status = re.search(r"HTTP (\d{3})", str(exc))
            if status is not None and int(status.group(1)) >= 500:
                raise PublicationOutcomeUnknown(f"Rekor returned HTTP {status.group(1)} after submission") from None
            raise PublicationError("Rekor rejected the submission" + (f" (HTTP {status.group(1)})" if status else "")) from None
        except Exception as exc:
            raise PublicationOutcomeUnknown(f"Rekor submission outcome unknown: {type(exc).__name__}") from None
        return json.loads(bundle.to_json())


def _private_write(path: Path, raw: bytes) -> None:
    from .transparency_cli import atomic_write
    atomic_write(path, raw)


def anchor_receipt(bundle: Bundle, trust: TrustBundle, services: SigstoreServices, *, acknowledged: bool,
                   trusted_root: Any, recovery_dir: Path, token_source: Callable[[], Any],
                   allow_duplicate: bool = False, now: datetime | None = None) -> tuple[Bundle, PublicInclusion]:
    """Publish the receipt digest once under the token's identity and return a verified copy.

    Order: validate locally, read the credential, record the attempt, sign and submit, persist
    the complete response privately, then verify. The input bundle is not mutated. Recovery
    from a saved response never calls ``services`` again.
    """
    if not acknowledged:
        raise PublicationError("Public publication requires explicit acknowledgement")
    if bundle.sigstore_evidence is not None:
        raise PublicationError("Bundle already carries public transparency evidence")
    try:
        preview = disclosure_preview(bundle, trust)
    except (ValueError, InvalidSignature, Base64Error) as exc:
        raise PublicationError(f"Input bundle is not publishable: {exc}") from exc
    receipt = bundle.notary_receipt
    assert receipt is not None
    marker = recovery_dir / f"{preview.receipt_digest}.submitted"
    if marker.exists() and not allow_duplicate:
        raise PublicationError("A Rekor submission for this receipt was already attempted; use --recover-from with "
                               "the saved response, or --acknowledge-duplicate to create another public entry")
    token = token_source()
    identity, issuer = token.identity, token.issuer
    _private_write(marker, json.dumps({"receipt_digest": preview.receipt_digest, "identity": identity,
                                       "attempted_at": (now or datetime.now(timezone.utc)).isoformat()}).encode())
    try:
        raw = services.sign_digest(hashed_receipt(receipt), token)
    except PublicationError:
        marker.unlink(missing_ok=True)  # nothing was published
        raise
    except PublicationOutcomeUnknown:
        raise
    except Exception as exc:
        raise PublicationOutcomeUnknown(f"Rekor submission outcome unknown: {type(exc).__name__}") from None
    try:
        _private_write(recovery_dir / f"{preview.receipt_digest}.sigstore.json",
                       json.dumps(raw, separators=(",", ":")).encode())
    except (TypeError, ValueError):
        raise PublicationOutcomeUnknown("Sigstore returned a response that cannot be recorded") from None
    try:
        evidence = SigstoreTransparencyEvidence(receipt_digest=preview.receipt_digest, identity=identity,
                                                issuer=issuer, bundle=raw)
        return _verified_copy(bundle, trust, evidence, trusted_root, now=now)
    except (ValueError, InvalidSignature) as exc:
        raise PublicationOutcomeUnknown(f"Response saved but not verifiable: {exc}") from exc


def recover_anchor(bundle: Bundle, trust: TrustBundle, saved: Path, trusted_root: Any, *, identity: str,
                   issuer: str = DEFAULT_ISSUER, now: datetime | None = None) -> tuple[Bundle, PublicInclusion]:
    """Rebuild and verify evidence from a privately saved standard bundle; no network."""
    from .canon import parse_json
    receipt = bundle.notary_receipt
    if receipt is None:
        raise ValueError("Recovery requires the notarized input bundle")
    if bundle.sigstore_evidence is not None:
        raise ValueError("Bundle already carries public transparency evidence")
    with saved.open("rb") as stream:
        raw = stream.read(MAX_EVIDENCE_BYTES + 1)
    if len(raw) > MAX_EVIDENCE_BYTES:
        raise ValueError("Saved Sigstore bundle exceeds the size bound")
    payload = parse_json(raw)
    if not isinstance(payload, dict):
        raise ValueError("Saved Sigstore bundle must be a JSON object")
    evidence = SigstoreTransparencyEvidence(receipt_digest=receipt_payload_digest(receipt), identity=identity,
                                            issuer=issuer, bundle=payload)
    return _verified_copy(bundle, trust, evidence, trusted_root, now=now)


def _verified_copy(bundle: Bundle, trust: TrustBundle, evidence: SigstoreTransparencyEvidence,
                   trusted_root: Any, *, now: datetime | None) -> tuple[Bundle, PublicInclusion]:
    anchored = bundle.model_copy(deep=True)
    anchored.sigstore_evidence = evidence
    verify_sigstore_bindings(evidence, anchored, trust, identity=evidence.identity, issuer=evidence.issuer)
    inclusion = verify_sigstore_inclusion(evidence, anchored, trust, trusted_root, now=now)
    return anchored, inclusion
