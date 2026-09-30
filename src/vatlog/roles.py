"""Local signing roles and verifier-owned public-key trust configuration."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, model_validator

from .keys import generate_key, public_b64, public_from_b64, thumbprint
from .canon import parse_json


class Role(str, Enum):
    DESIGNER = "designer"
    RECORDER = "recorder"
    NOTARY = "notary"
    TRANSPARENCY_LOG = "transparency_log"


class UntrustedKeyError(ValueError):
    """The supplied key identifier is not authorized for the required role."""


@dataclass(frozen=True)
class LocalSigner:
    """A software-rooted private key assigned to one signing role."""

    role: Role
    key: Ed25519PrivateKey

    @property
    def keyid(self) -> str:
        return thumbprint(self.key.public_key())

    def sign(self, payload: bytes) -> str:
        """Return a base64 Ed25519 signature over the supplied bytes."""
        return base64.b64encode(self.key.sign(payload)).decode()


def signer_for(key: LocalSigner | Ed25519PrivateKey, role: Role) -> LocalSigner:
    """Accept a raw key or enforce an explicit signer's role."""
    signer = key if isinstance(key, LocalSigner) else LocalSigner(role, key)
    if signer.role != role:
        raise ValueError(f"expected {role.value} signer")
    return signer


class TrustEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keyid: str
    role: Role
    public_key: str

    @model_validator(mode="after")
    def validate_keyid(self) -> "TrustEntry":
        if self.keyid != thumbprint(public_from_b64(self.public_key)):
            raise ValueError("key id does not match public key")
        return self

    @classmethod
    def from_signer(cls, signer: LocalSigner) -> "TrustEntry":
        """Export only the public part of a signer."""
        return cls(keyid=signer.keyid, role=signer.role,
                   public_key=public_b64(signer.key.public_key()))


class TrustBundle(BaseModel):
    """Public trust entries, supplied independently of an untrusted audit bundle."""

    model_config = ConfigDict(extra="forbid")
    entries: list[TrustEntry]
    pinned_key: str | None = None

    @model_validator(mode="after")
    def validate_entries(self) -> "TrustBundle":
        if self.pinned_key is not None:
            public_from_b64(self.pinned_key)
            if self.entries:
                raise ValueError("pinned trust cannot also contain role entries")
        if len({e.role for e in self.entries}) != len(self.entries):
            raise ValueError("only one key per role is supported")
        if len({e.keyid for e in self.entries}) != len(self.entries):
            raise ValueError("roles must use distinct keys")
        return self

    @property
    def role_bound(self) -> bool:
        return self.pinned_key is None

    @classmethod
    def pinned(cls, pubkey: Ed25519PublicKey) -> "TrustBundle":
        """Trust one key without distinguishing roles or establishing identity."""
        return cls(entries=[], pinned_key=public_b64(pubkey))

    def resolve(self, keyid: str, role: Role) -> Ed25519PublicKey:
        """Resolve a listed key in its role; empty ids select the role's sole key."""
        if self.pinned_key is not None:
            return public_from_b64(self.pinned_key)
        for entry in self.entries:
            if entry.role == role and (not keyid or entry.keyid == keyid):
                return public_from_b64(entry.public_key)
        raise UntrustedKeyError(f"untrusted {role.value} key: {keyid}")

    def save(self, path: Path) -> None:
        """Write public trust configuration as JSON."""
        path.write_text(self.model_dump_json(indent=2))

    @classmethod
    def load(cls, path: Path) -> "TrustBundle":
        """Validate public trust configuration from JSON."""
        return cls.model_validate(parse_json(path.read_bytes()))


@dataclass(frozen=True)
class Keyring:
    """Four distinct local signers for demo and test orchestration only."""

    designer: LocalSigner
    recorder: LocalSigner
    notary: LocalSigner
    transparency_log: LocalSigner

    def __post_init__(self) -> None:
        for role in Role:
            signer_for(getattr(self, role.value), role)
        self.trust_bundle()

    @classmethod
    def generate(cls) -> "Keyring":
        """Generate a different private key for every role."""
        return cls(*(LocalSigner(role, generate_key()) for role in Role))

    def trust_bundle(self) -> TrustBundle:
        """Export the public role mapping for verification."""
        return TrustBundle(entries=[TrustEntry.from_signer(getattr(self, r.value)) for r in Role])
