"""Phase 2 sealing attestation, in a DSSE envelope.

RAND calls the attestation "the primary artifact of the protocol": it binds the root hash
to the recorder process identity and hardware environment. DSSE (the envelope Sigstore and
in-toto use) is adopted for its shape only -- the pre-authentication encoding and the
envelope fields -- without pulling the in-toto dependency tree.
"""

from __future__ import annotations

import base64
from binascii import Error as Base64Error
from cryptography.exceptions import InvalidSignature
from datetime import datetime, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, ConfigDict

from .canon import canonical_bytes, parse_json
from .schema import AssuranceTier, EnvSnapshot
from .roles import LocalSigner, Role, TrustBundle, signer_for

PAYLOAD_TYPE = "application/vnd.biodesign-vat.attestation+json"


def pae(payload_type: str, payload: bytes) -> bytes:
    """DSSE Pre-Authentication Encoding."""
    return b"DSSEv1 %d %s %d %s" % (
        len(payload_type),
        payload_type.encode(),
        len(payload),
        payload,
    )


class Attestation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root_hash: str
    tree_size: int
    session_uuid: str
    recorder_identity: str
    assurance_tier: AssuranceTier
    environment: EnvSnapshot
    sealed_at: datetime


class DSSEEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payloadType: str = PAYLOAD_TYPE
    payload: str            # base64 of the canonical attestation bytes
    signatures: list[dict]  # [{"keyid": ..., "sig": base64}]

    def attestation(self) -> Attestation:
        return Attestation.model_validate(parse_json(base64.b64decode(self.payload, validate=True)))


def seal(
    key: Ed25519PrivateKey | LocalSigner,
    *,
    root_hash: str,
    tree_size: int,
    session_uuid: str,
    recorder_identity: str,
    environment: EnvSnapshot,
    sealed_at: datetime | None = None,
) -> DSSEEnvelope:
    signer = signer_for(key, Role.RECORDER)

    att = Attestation(
        root_hash=root_hash,
        tree_size=tree_size,
        session_uuid=session_uuid,
        recorder_identity=recorder_identity,
        assurance_tier=environment.assurance_tier,
        environment=environment,
        sealed_at=sealed_at or datetime.now(timezone.utc),
    )
    payload = canonical_bytes(att.model_dump(mode="json"))
    sig = signer.sign(pae(PAYLOAD_TYPE, payload))
    return DSSEEnvelope(
        payload=base64.b64encode(payload).decode(),
        signatures=[
            {
                "keyid": signer.keyid,
                "sig": sig,
            }
        ],
    )


def verify_envelope(env: DSSEEnvelope, pubkey: Ed25519PublicKey | TrustBundle) -> bool:
    """Verify every listed signature and require a recorder signature."""
    trust = pubkey if isinstance(pubkey, TrustBundle) else TrustBundle.pinned(pubkey)
    try:
        if env.payloadType != PAYLOAD_TYPE or not env.signatures:
            return False
        payload = base64.b64decode(env.payload, validate=True)
        recorder_seen = False
        for entry in env.signatures:
            keyid = entry["keyid"]
            if not isinstance(keyid, str) or not keyid:
                return False
            role = Role.RECORDER
            if trust.role_bound:
                registered = next((e for e in trust.entries if e.keyid == keyid), None)
                if registered is None:
                    return False
                role = registered.role
            trust.resolve(keyid, role).verify(
                base64.b64decode(entry["sig"], validate=True), pae(env.payloadType, payload)
            )
            recorder_seen |= role == Role.RECORDER
        return recorder_seen
    except (InvalidSignature, ValueError, Base64Error, KeyError, TypeError):
        return False
