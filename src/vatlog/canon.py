"""Canonical serialization. Every hash in the protocol is taken over these bytes.

RFC 8785 (JSON Canonicalization Scheme) via the ``rfc8785`` package. The fallback is a
sorted-key compact dump; it differs from JCS on number formatting and on non-ASCII
escaping, so it is a fallback, not an alternative -- a log written under one and verified
under the other will not agree. ``CANON_PROFILE`` records which was used.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

try:
    import rfc8785

    CANON_PROFILE = "rfc8785"
except ImportError:  # pragma: no cover - exercised only where the dep is absent
    rfc8785 = None
    CANON_PROFILE = "sorted-compact"


def canonical_bytes(obj: Any) -> bytes:
    """Serialize a JSON-able object to its canonical byte form."""
    if rfc8785 is not None:
        return rfc8785.dumps(obj)
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def parse_json(data: str | bytes) -> Any:
    """Read untrusted JSON without duplicate keys or non-JSON numeric constants."""
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError(f"invalid JSON constant: {value}")

    try:
        return json.loads(data, object_pairs_hook=unique_object, parse_constant=invalid_constant)
    except (RecursionError, UnicodeError) as exc:
        raise ValueError("invalid or excessively nested JSON") from exc


def canonical_record_bytes(record) -> bytes:
    """Canonical bytes of a pydantic record (``mode="json"`` so datetimes become ISO 8601)."""
    return canonical_bytes(record.model_dump(mode="json"))


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def record_digest(record) -> str:
    """SHA-256 over the canonical bytes of a record. This is what ``sha_prev`` points at."""
    return sha256_hex(canonical_record_bytes(record))
