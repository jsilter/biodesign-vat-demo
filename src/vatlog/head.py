"""The signed tree head.

``tree_size`` is the load-bearing field of the whole build. It is public and signed, so a
bundle revealing k leaves against a head that says N can be checked by arithmetic. That is
what makes within-session selective disclosure (`VISION.md`, "Assurance Model and Limits")
detectable within a session: per-record hashes are all honest, and the count is what gives
the omission away.

Cardinality has to be *bound and public*, which is why the zero-knowledge-set family is the
wrong primitive here -- those are engineered to leak nothing about |S|. See
reference/selective-disclosure-litnote.md section 1.
"""

from __future__ import annotations

from binascii import Error as Base64Error
from datetime import datetime, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, ConfigDict, Field

from .canon import canonical_bytes
from .roles import LocalSigner, Role, signer_for


class SignedTreeHead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    log_id: str
    session_uuid: str
    tree_size: int
    root_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    timestamp: datetime
    keyid: str = ""
    signature: str = ""  # base64, over the canonical bytes of everything above

    def signing_payload(self) -> bytes:
        body = self.model_dump(mode="json")
        body.pop("signature", None)
        return canonical_bytes(body)


def sign_head(
    key: Ed25519PrivateKey | LocalSigner,
    *,
    log_id: str,
    session_uuid: str,
    tree_size: int,
    root_hash: str,
    timestamp: datetime | None = None,
) -> SignedTreeHead:
    signer = signer_for(key, Role.RECORDER)
    head = SignedTreeHead(
        log_id=log_id,
        session_uuid=session_uuid,
        tree_size=tree_size,
        root_hash=root_hash,
        timestamp=timestamp or datetime.now(timezone.utc),
        keyid=signer.keyid,
    )
    head.signature = signer.sign(head.signing_payload())
    return head


def verify_head(head: SignedTreeHead, pubkey: Ed25519PublicKey) -> bool:
    import base64

    try:
        pubkey.verify(base64.b64decode(head.signature, validate=True), head.signing_payload())
        return True
    except (InvalidSignature, ValueError, Base64Error):
        return False
