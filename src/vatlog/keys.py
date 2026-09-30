"""Ed25519 key handling.

RAND phase 1 wants a signing key inaccessible to the designer and the host OS, and calls
software-rooted trust (key protected only by OS access control) explicitly weaker but
acceptable in lower-risk contexts. This is the software-rooted case. Nothing here claims
hardware binding; ``EnvSnapshot.assurance_tier`` is where that is stated.
"""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


def generate_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def public_bytes(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )


def thumbprint(key: Ed25519PublicKey) -> str:
    """SHA-256 of a raw public key; the designer's id also identifies UserSession.user_id."""
    return hashlib.sha256(public_bytes(key)).hexdigest()


def save_private(key: Ed25519PrivateKey, path: Path) -> None:
    """Create a private key at mode 0600, refusing to overwrite an existing file."""
    data = key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def load_private(path: Path) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(path.read_bytes())


def public_b64(key: Ed25519PublicKey) -> str:
    return base64.b64encode(public_bytes(key)).decode()


def public_from_b64(data: str) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(base64.b64decode(data))
