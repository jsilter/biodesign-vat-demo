"""Verification: RAND's four ordered checks, plus completeness accounting.

RAND phase 4 lists four checks in order (reference/rand-vat-spec-extract.md):

  1. hash chain internally consistent
  2. attestation signature valid and from a recognized notary
  3. inclusion proof places the attestation in the log at a plausible time
  4. the submitted sequence matches the output recorded in the log

Check 5 is ours. ``signed_tree_head.tree_size`` is public and signed, so comparing it to
the number of revealed leaves detects a bundle that is honest per-record and incomplete as
a set. The construction is the cardinality binding described in
reference/selective-disclosure-litnote.md section 1 and bounded in ``VISION.md``.

Check 5 gets its own outcome and its own exit code because INCOMPLETE and TAMPERED mean
different things to a synthesis provider: one is a designer who withheld runs, the other is
a bundle that has been altered.
"""

from __future__ import annotations

import enum
import base64
from collections import Counter
from binascii import Error as Base64Error
from cryptography.exceptions import InvalidSignature
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .bundle import Bundle
from .canon import canonical_record_bytes, sha256_hex
from .merkle import check_inclusion
from .notary import verify_recorder_commitment, verify_receipt
from .roles import Role, TrustBundle
from .schema import RecordType


class Outcome(str, enum.Enum):
    OK = "OK"
    TAMPERED = "TAMPERED"
    BAD_SIGNATURE = "BAD_SIGNATURE"
    NOT_INCLUDED = "NOT_INCLUDED"
    SEQUENCE_MISMATCH = "SEQUENCE_MISMATCH"
    INCOMPLETE = "INCOMPLETE"


EXIT_CODES = {
    Outcome.OK: 0,
    Outcome.TAMPERED: 2,
    Outcome.BAD_SIGNATURE: 3,
    Outcome.NOT_INCLUDED: 4,
    Outcome.SEQUENCE_MISMATCH: 5,
    Outcome.INCOMPLETE: 6,
}


def _plural(n: int, singular: str, plural: str | None = None) -> str:
    return f"{n} {singular}" if n == 1 else f"{n} {plural or singular + 's'}"


@dataclass
class CheckResult:
    number: int
    name: str
    passed: bool
    detail: str = ""


@dataclass
class ArtifactCounts:
    """Counts over revealed records; mixed availability can count in both columns."""

    present: int = 0
    withheld: int = 0
    absent: int = 0


def _artifact_counts(bundle: Bundle, kind: str) -> ArtifactCounts:
    counts = ArtifactCounts()
    for record in bundle.records:
        artifacts = [a for a in record.inputs + record.results + record.intermediates if a.kind == kind]
        counts.present += any(a.sha256 in bundle.blobs for a in artifacts)
        counts.withheld += any(a.sha256 not in bundle.blobs for a in artifacts)
        counts.absent += not artifacts
    return counts


def _instruction_status(bundle: Bundle) -> str:
    opening = next((r for r in bundle.records
                    if r.link.leaf_index == 0 and r.record_type == RecordType.SESSION_OPEN), None)
    instructions = [] if opening is None else [a for a in opening.inputs if a.kind == "prompt"]
    if not instructions:
        return "absent"
    return "present" if all(a.sha256 in bundle.blobs for a in instructions) else "withheld"


@dataclass
class VerifyReport:
    outcome: Outcome
    checks: list[CheckResult] = field(default_factory=list)
    revealed: int = 0
    tree_size: int = 0
    signer_roles: str = "unverified"
    designer_identity: str = "unverified"
    notarization: str = "absent"
    independent_timestamp: str = "unavailable (local clock)"
    transparency: str = "absent"
    checkpoint_size: int | None = None
    checkpoint_key: str | None = None
    consistency: str = "unverified"
    public_transparency: str = "absent"
    public_signing_identity: str | None = None
    rekor_log: str | None = None
    rfc3161_timestamp: str | None = None
    capture_tier: str = "unverified"
    head_key_resolution: str = "unverified"
    leaf_counts: dict[str, int] = field(default_factory=dict)
    unrevealed_leaves: int = 0
    instructions: str = "absent"
    transcripts: ArtifactCounts = field(default_factory=ArtifactCounts)
    reasoning: ArtifactCounts = field(default_factory=ArtifactCounts)

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.outcome]

    def render(self) -> str:
        lines = []
        for c in self.checks:
            mark = "PASS" if c.passed else "FAIL"
            lines.append(f"  [{mark}] {c.number}. {c.name}" + (f" - {c.detail}" if c.detail else ""))
        for label, value in (
            ("signer roles", self.signer_roles), ("designer identity", self.designer_identity),
            ("notarization", self.notarization), ("independent timestamp", self.independent_timestamp),
            ("transparency", self.transparency), ("consistency", self.consistency),
            ("public transparency", self.public_transparency),
            ("public signing identity", self.public_signing_identity), ("Rekor log ID / index", self.rekor_log),
            ("RFC 3161 timestamp", self.rfc3161_timestamp),
            ("attested capture tier", self.capture_tier), ("head key", self.head_key_resolution),
        ):
            if value is not None:
                lines.append(f"  {label}: {value}")
        composition = ", ".join(f"{kind}={count}" for kind, count in sorted(self.leaf_counts.items()))
        lines.append(f"  revealed leaf types: {composition or '(none)'}; "
                     f"unrevealed={self.unrevealed_leaves} (types unknown); signed tree_size={self.tree_size}")
        lines.append(f"  session instructions: {self.instructions}")
        lines.append(f"  transcripts (revealed records): present={self.transcripts.present}, "
                     f"withheld={self.transcripts.withheld}, absent={self.transcripts.absent}")
        if self.reasoning.present or self.reasoning.withheld:
            lines.append(f"  reasoning (revealed records): present={self.reasoning.present}, "
                         f"withheld={self.reasoning.withheld}")
        lines.append(f"  => {self.outcome.value}")
        return "\n".join(lines)


def verify_bundle(
    bundle: Bundle,
    pubkey: Ed25519PublicKey | TrustBundle,
    *,
    submitted_sequence: str | None = None,
    max_head_age: timedelta | None = None,
    now: datetime | None = None,
    require_transparency: bool = False,
    require_public_transparency: bool = False,
    max_checkpoint_age: timedelta | None = None,
    witness_checkpoint=None,
    witness_consistency_proof=None,
    sigstore_trusted_root=None,
    sigstore_identity: str | None = None,
    sigstore_issuer: str | None = None,
) -> VerifyReport:
    """Run the five checks against explicit role trust or a single pinned public key.

    Local transparency evidence and public Sigstore evidence are independent objects: every
    supplied one must verify. ``require_transparency`` accepts either form;
    ``require_public_transparency`` demands the public one, verified under
    ``sigstore_trusted_root`` (an official ``TrustedRoot`` obtained independently of the bundle)
    and bound to the verifier's expected ``sigstore_identity`` and ``sigstore_issuer``.
    """
    trust = pubkey if isinstance(pubkey, TrustBundle) else TrustBundle.pinned(pubkey)
    checks: list[CheckResult] = []
    report = VerifyReport(
        outcome=Outcome.OK,
        checks=checks,
        revealed=len(bundle.records),
        tree_size=bundle.signed_tree_head.tree_size,
        leaf_counts=dict(Counter(r.record_type.value for r in bundle.records)),
        unrevealed_leaves=max(0, bundle.signed_tree_head.tree_size - len({r.link.leaf_index for r in bundle.records})),
        instructions=_instruction_status(bundle),
        transcripts=_artifact_counts(bundle, "transcript"),
        reasoning=_artifact_counts(bundle, "reasoning"),
    )

    def fail(outcome: Outcome) -> VerifyReport:
        report.outcome = outcome
        return report

    # --- check 1: hash chain internally consistent --------------------------------
    # A partial bundle cannot be walked contiguously, so adjacency is checked only where
    # two revealed records are consecutive. Every revealed record is still pinned to the
    # tree by check 3, which is what stops a substituted record in a partial bundle.
    ordered = sorted(bundle.records, key=lambda r: r.link.leaf_index)
    indices = [r.link.leaf_index for r in ordered]
    size = bundle.signed_tree_head.tree_size
    if len(set(indices)) != len(indices) or any(i < 0 or i >= size for i in indices):
        checks.append(CheckResult(1, "hash chain", False, "duplicate or out-of-range record position"))
        return fail(Outcome.TAMPERED)
    try:
        record_bytes = {r.link.leaf_index: canonical_record_bytes(r) for r in ordered}
    except (ValueError, UnicodeError):
        checks.append(CheckResult(1, "hash chain", False, "record cannot be canonicalized"))
        return fail(Outcome.TAMPERED)
    broken = None
    for prev, cur in zip(ordered, ordered[1:]):
        if cur.link.leaf_index == prev.link.leaf_index + 1:
            if cur.link.sha_prev != sha256_hex(record_bytes[prev.link.leaf_index]):
                broken = cur.link.leaf_index
                break
    if ordered and ordered[0].link.leaf_index == 0 and ordered[0].link.sha_prev != "00" * 32:
        broken = 0
    if broken is not None:
        checks.append(CheckResult(1, "hash chain", False, f"broken link at record {broken}"))
        return fail(Outcome.TAMPERED)
    checks.append(
        CheckResult(1, "hash chain", True, _plural(len(ordered), "revealed record") + ", links intact")
    )

    # --- check 2: attestation signature ------------------------------------------
    identity = "unverified" if trust.role_bound else "not established"
    try:
        verify_recorder_commitment(bundle, trust)
        if trust.role_bound:
            opening = next((r for r in ordered if r.link.leaf_index == 0), None)
            if any(r.designer_statement is not None and r.link.leaf_index != 0 for r in ordered):
                raise ValueError("designer statement is only valid on session_open at leaf 0")
            if opening is not None:
                statement = opening.designer_statement
                if opening.record_type != RecordType.SESSION_OPEN or statement is None:
                    raise ValueError("leaf 0 must be session_open with a designer statement")
                if not statement.keyid:
                    raise ValueError("designer statement requires an explicit key id")
                trust.resolve(statement.keyid, Role.DESIGNER).verify(
                    base64.b64decode(statement.signature, validate=True), statement.signing_payload()
                )
                if statement.session_uuid != bundle.signed_tree_head.session_uuid:
                    raise ValueError("designer statement session != head")
                if any(r.user_session.user_id != statement.keyid for r in ordered):
                    raise ValueError("record user_id != designer statement key")
                if opening.user_session.purpose_tag != statement.purpose_tag:
                    raise ValueError("opening purpose != designer statement purpose")
                identity = statement.keyid
        if bundle.notary_receipt is not None and not verify_receipt(bundle.notary_receipt, bundle, trust):
            raise ValueError("notary receipt invalid")
        from .transparency import verify_bindings
        if bundle.transparency_evidence is not None:
            verify_bindings(bundle.transparency_evidence, bundle, trust)
        if bundle.sigstore_evidence is not None:
            from .sigstore_transparency import verify_sigstore_bindings
            verify_sigstore_bindings(bundle.sigstore_evidence, bundle, trust,
                                     identity=sigstore_identity, issuer=sigstore_issuer)
        if witness_checkpoint is not None:
            witness_checkpoint.verify(trust)
        if witness_consistency_proof is not None:
            witness_consistency_proof.from_checkpoint.verify(trust)
            witness_consistency_proof.to_checkpoint.verify(trust)
    except (InvalidSignature, ValueError, Base64Error, TypeError) as exc:
        checks.append(CheckResult(2, "attestation signature", False, str(exc) or "designer signature invalid"))
        return fail(Outcome.BAD_SIGNATURE)
    att = bundle.attestation.attestation()
    report.signer_roles = "role-bound" if trust.role_bound else "single pinned key (roles not distinguished)"
    report.capture_tier = att.assurance_tier.value
    report.head_key_resolution = "explicit key id" if bundle.signed_tree_head.keyid else "fallback to registered recorder key"
    if bundle.notary_receipt is not None:
        report.notarization = f"simulated ({bundle.notary_receipt.policy_id})"
    checks.append(
        CheckResult(2, "attestation signature", True, f"assurance tier {att.assurance_tier.value}")
    )

    # --- check 3: inclusion proofs, at a plausible time ---------------------------
    proofs = {}
    for proof in bundle.inclusion_proofs:
        index = proof.get("leaf_index")
        if type(index) is not int or not 0 <= index < size or index in proofs:
            checks.append(CheckResult(3, "inclusion proof", False, "invalid or duplicate proof position"))
            return fail(Outcome.NOT_INCLUDED)
        proofs[index] = proof
    root = bundle.signed_tree_head.root_hash
    for rec in ordered:
        idx = rec.link.leaf_index
        proof = proofs.get(idx)
        if proof is None:
            checks.append(CheckResult(3, "inclusion proof", False, f"no proof for record {idx}"))
            return fail(Outcome.NOT_INCLUDED)
        if not check_inclusion(record_bytes[idx], root, proof, tree_size=size):
            checks.append(
                CheckResult(3, "inclusion proof", False, f"record {idx} not under signed root")
            )
            return fail(Outcome.NOT_INCLUDED)
    if max_head_age is not None:
        current = now or datetime.now(timezone.utc)
        if current.utcoffset() is None or bundle.signed_tree_head.timestamp.utcoffset() is None:
            checks.append(CheckResult(3, "inclusion proof", False, "head age requires timezone-aware timestamps"))
            return fail(Outcome.NOT_INCLUDED)
        age = current - bundle.signed_tree_head.timestamp
        if age > max_head_age or age < timedelta(seconds=-60):
            checks.append(
                CheckResult(3, "inclusion proof", False, f"head timestamp implausible ({age})")
            )
            return fail(Outcome.NOT_INCLUDED)
    try:
        from .transparency import verify_membership, consistent
        evidence, public = bundle.transparency_evidence, bundle.sigstore_evidence
        witnessed = witness_checkpoint is not None or witness_consistency_proof is not None
        if evidence is None and witnessed:
            raise ValueError("Witness comparison requires local transparency evidence")
        if public is None and require_public_transparency:
            raise ValueError("Required public transparency evidence is missing")
        if evidence is None and public is None and (require_transparency or bundle.notary_receipt is not None):
            raise ValueError("Required transparency evidence is missing")
        if (witness_checkpoint is None) != (witness_consistency_proof is None):
            raise ValueError("Witness checkpoint and proof must be paired")
        if evidence is not None:
            verify_membership(evidence)
            checkpoint = evidence.checkpoint
            if max_checkpoint_age is not None:
                age = (now or datetime.now(timezone.utc)) - checkpoint.timestamp
                if age > max_checkpoint_age or age < timedelta(seconds=-60):
                    raise ValueError("Transparency checkpoint timestamp implausible")
            if witness_checkpoint is not None:
                proof = witness_consistency_proof
                if (proof.from_checkpoint != witness_checkpoint or proof.to_checkpoint != checkpoint
                        or not consistent(witness_checkpoint, checkpoint, proof.proof)
                        or (evidence.entry.leaf_index >= witness_checkpoint.tree_size
                            and evidence.entry.accepted_at < witness_checkpoint.timestamp)):
                    raise ValueError("Invalid witness consistency or backdated entry")
            report.transparency = "simulated local log; inclusion verified"
            report.checkpoint_size, report.checkpoint_key = checkpoint.tree_size, checkpoint.keyid
            report.consistency = ("witness consistency verified" if witness_checkpoint is not None else
                                  "immediate consistency verified" if checkpoint.tree_size > 1 else "first checkpoint")
        if public is not None:
            from .sigstore_transparency import verify_sigstore_inclusion
            inclusion = verify_sigstore_inclusion(public, bundle, trust, sigstore_trusted_root, now=now)
            report.public_transparency = "Sigstore Rekor inclusion verified; RFC 3161 publication time verified"
            report.public_signing_identity = f"{inclusion.identity} (issuer {inclusion.issuer})"
            report.rekor_log = f"{inclusion.log_id} / {inclusion.log_index} (checkpoint origin {inclusion.checkpoint_origin})"
            report.rfc3161_timestamp = f"{inclusion.timestamp.isoformat()} (publication time, not execution time)"
            report.independent_timestamp = "RFC 3161 publication time only; execution time remains the local clock"
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        checks.append(CheckResult(3, "inclusion proof", False, str(exc)))
        return fail(Outcome.NOT_INCLUDED)
    checks.append(CheckResult(3, "inclusion proof", True,
        _plural(len(ordered), "leaf", "leaves") + " under signed root" +
        ("; local transparency inclusion verified" if bundle.transparency_evidence else "") +
        ("; public Rekor inclusion verified" if bundle.sigstore_evidence else "")))
    # Identity is authenticated only after its opening record is proved under the head.
    report.designer_identity = identity

    # --- check 4: submitted sequence matches a recorded output --------------------
    # Blobs are optional, but every supplied byte must match its content address.
    for digest, data in bundle.blobs.items():
        if sha256_hex(data) != digest:
            checks.append(CheckResult(4, "artifact integrity", False, f"blob digest mismatch: {digest}"))
            return fail(Outcome.TAMPERED)
    for rec in ordered:
        for art in rec.inputs + rec.intermediates + rec.results:
            if art.sha256 in bundle.blobs:
                if len(bundle.blobs[art.sha256]) != art.bytes_len:
                    checks.append(CheckResult(4, "artifact integrity", False, f"blob size mismatch: {art.name}"))
                    return fail(Outcome.TAMPERED)
    if submitted_sequence is not None:
        digest = sha256_hex(submitted_sequence.strip().encode())
        hit = next(
            (
                rec.link.leaf_index
                for rec in ordered
                if any(a.kind in ("aa_sequence", "nt_sequence") and a.sha256 == digest for a in rec.results)
            ),
            None,
        )
        if hit is None:
            checks.append(
                CheckResult(4, "sequence match", False, "submitted sequence is in no revealed record")
            )
            return fail(Outcome.SEQUENCE_MISMATCH)
        checks.append(CheckResult(4, "sequence match", True, f"matches output of record {hit}"))
    else:
        checks.append(CheckResult(4, "sequence match", True, "skipped, no sequence submitted"))

    # --- check 5: completeness accounting ----------------------------------------
    revealed, size = len(ordered), bundle.signed_tree_head.tree_size
    if revealed != size:
        missing = []
        cursor = 0
        for index in indices + [size]:
            missing.extend(range(cursor, min(index, cursor + 100 - len(missing))))
            cursor = index + 1
            if len(missing) == 100:
                break
        suffix = f" (first 100 of {size - revealed} missing)" if size - revealed > 100 else ""
        checks.append(
            CheckResult(
                5,
                "completeness",
                False,
                f"bundle covers {revealed} of {size}; missing leaves {missing}{suffix}",
            )
        )
        return fail(Outcome.INCOMPLETE)
    checks.append(CheckResult(5, "completeness", True, f"all {size} leaves revealed"))
    return report
