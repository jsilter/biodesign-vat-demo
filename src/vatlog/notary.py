"""Simulated local receipts: integrity replay, with no biological sequence screen."""

from __future__ import annotations

import base64
from binascii import Error as Base64Error
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import BaseModel, ConfigDict

from .attest import DSSEEnvelope, verify_envelope
from .canon import canonical_bytes, canonical_record_bytes, record_digest, sha256_hex
from .head import verify_head
from .merkle import RecordTree
from .roles import LocalSigner, Role, TrustBundle, signer_for
from .schema import GENESIS_PREV

if TYPE_CHECKING:
    from .bundle import Bundle

POLICY_ID = "local-null-screen/1"


class NotaryReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_uuid: str
    head_root: str
    tree_size: int
    attestation_digest: str
    policy_id: str = POLICY_ID
    validation_result: Literal["passed"] = "passed"
    observed_at: datetime
    keyid: str
    signature: str = ""
    simulated: Literal[True] = True
    independent_timestamp: Literal[False] = False

    def signing_payload(self) -> bytes:
        """Bind the receipt claims, including its explicit simulation limits."""
        return canonical_bytes(self.model_dump(mode="json", exclude={"signature"}))


def attestation_digest(envelope: DSSEEnvelope) -> str:
    """SHA-256 of the decoded DSSE payload, not its transport encoding."""
    return sha256_hex(base64.b64decode(envelope.payload, validate=True))


def verify_recorder_commitment(bundle: Bundle, trust: TrustBundle) -> None:
    """Reject invalid recorder signatures or inconsistent session commitments."""
    head = bundle.signed_tree_head
    if not verify_envelope(bundle.attestation, trust):
        raise ValueError("DSSE signature or payload type invalid")
    if not verify_head(head, trust.resolve(head.keyid, Role.RECORDER)):
        raise ValueError("tree head signature invalid")
    att = bundle.attestation.attestation()
    if att.assurance_tier != att.environment.assurance_tier:
        raise ValueError("attested capture tier != environment capture tier")
    if (att.root_hash, att.tree_size, att.session_uuid) != (
        head.root_hash, head.tree_size, head.session_uuid
    ):
        raise ValueError("attestation root, size or session != head")
    if bundle.session_uuid != head.session_uuid or bundle.log_id != head.log_id:
        raise ValueError("bundle session or log != head")
    if any(r.session_uuid != head.session_uuid for r in bundle.records):
        raise ValueError("record session != head")


def local_null_screen(bundle: Bundle, trust: TrustBundle) -> None:
    """Replay a full committed chain and recorder signatures; apply no sequence screen."""
    if not trust.role_bound:
        raise ValueError("notary policy requires role-bound trust")
    verify_recorder_commitment(bundle, trust)
    records = sorted(bundle.records, key=lambda r: r.link.leaf_index)
    if (len(records) != bundle.signed_tree_head.tree_size
            or any(r.link.leaf_index != i for i, r in enumerate(records))):
        raise ValueError("notary policy requires the full record chain")
    previous = GENESIS_PREV
    for index, record in enumerate(records):
        if record.link.sha_prev != previous or record.seq_no != index:
            raise ValueError(f"broken chain at record {index}")
        previous = record_digest(record)
    tree = RecordTree.from_leaves([canonical_record_bytes(r) for r in records])
    if tree.root_hex != bundle.signed_tree_head.root_hash:
        raise ValueError("replayed root != head root")


def issue_receipt(
    bundle: Bundle, key: LocalSigner | Ed25519PrivateKey, trust: TrustBundle,
    *, observed_at: datetime | None = None,
) -> NotaryReceipt:
    """Apply the local policy and sign a simulated receipt on success."""
    signer = signer_for(key, Role.NOTARY)
    trust.resolve(signer.keyid, Role.NOTARY)
    local_null_screen(bundle, trust)
    head = bundle.signed_tree_head
    receipt = NotaryReceipt(
        session_uuid=head.session_uuid, head_root=head.root_hash, tree_size=head.tree_size,
        attestation_digest=attestation_digest(bundle.attestation),
        observed_at=observed_at or datetime.now(timezone.utc), keyid=signer.keyid,
    )
    receipt.signature = signer.sign(receipt.signing_payload())
    return receipt


def verify_receipt(receipt: NotaryReceipt, bundle: Bundle, trust: TrustBundle) -> bool:
    """Check the notary role, signature, policy and binding to this commitment."""
    try:
        if not receipt.keyid:
            return False
        trust.resolve(receipt.keyid, Role.NOTARY).verify(
            base64.b64decode(receipt.signature, validate=True), receipt.signing_payload()
        )
        head = bundle.signed_tree_head
        return (
            receipt.session_uuid == head.session_uuid and receipt.head_root == head.root_hash
            and receipt.tree_size == head.tree_size
            and receipt.attestation_digest == attestation_digest(bundle.attestation)
            and receipt.policy_id == POLICY_ID and receipt.validation_result == "passed"
            and receipt.simulated is True and receipt.independent_timestamp is False
        )
    except (InvalidSignature, ValueError, Base64Error):
        return False
