"""Laptop-local transparency commitments; no independent clock or publication."""

from __future__ import annotations

import base64
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal, TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .canon import canonical_bytes, parse_json, sha256_hex
from .merkle import RecordTree, check_consistency, check_inclusion
from .roles import LocalSigner, Role, TrustBundle

if TYPE_CHECKING:
    from .bundle import Bundle


class ProtocolObject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="after")
    @classmethod
    def utc_time(cls, value: Any) -> Any:
        if isinstance(value, datetime) and (value.utcoffset() is None or value.utcoffset().total_seconds() != 0):
            raise ValueError("Transparency timestamps require UTC")
        return value

    def canonical(self) -> bytes:
        """Return the canonical public object, including any signature."""
        return canonical_bytes(self.model_dump(mode="json"))


Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Count = Annotated[int, Field(strict=True, ge=0, le=2**53 - 1)]


class TransparencyEntry(ProtocolObject):
    entry_format: Literal["vat-transparency-entry/1"] = "vat-transparency-entry/1"
    log_id: str = Field(min_length=1, max_length=256)
    leaf_index: Count
    accepted_at: datetime
    notary_receipt_digest: Digest
    attestation_digest: Digest
    session_uuid: str
    session_root: Digest
    session_tree_size: Count


class TransparencyCheckpoint(ProtocolObject):
    checkpoint_format: Literal["vat-transparency-checkpoint/1"] = "vat-transparency-checkpoint/1"
    log_id: str = Field(min_length=1, max_length=256)
    tree_size: Count
    root_hash: Digest
    timestamp: datetime
    keyid: Digest
    simulated: Literal[True] = True
    independent_timestamp: Literal[False] = False
    signature: str = ""

    def payload(self) -> bytes:
        """Canonical signed claims, excluding the signature itself."""
        return canonical_bytes(self.model_dump(mode="json", exclude={"signature"}))

    def verify(self, trust: TrustBundle) -> None:
        """Authenticate the explicit log role; raise on untrusted or invalid evidence."""
        type(self).model_validate(self.model_dump(mode="json"))
        if not trust.role_bound or not self.keyid:
            raise ValueError("Transparency requires role-bound trust")
        trust.resolve(self.keyid, Role.TRANSPARENCY_LOG).verify(
            base64.b64decode(self.signature, validate=True), self.payload())


class TransparencyEvidence(ProtocolObject):
    evidence_format: Literal["vat-transparency-evidence/1"] = "vat-transparency-evidence/1"
    entry: TransparencyEntry
    checkpoint: TransparencyCheckpoint
    inclusion_proof: dict
    prior_checkpoint: TransparencyCheckpoint | None = None
    consistency_proof: dict | None = None


class ConsistencyEvidence(ProtocolObject):
    proof_format: Literal["vat-transparency-consistency/1"] = "vat-transparency-consistency/1"
    from_checkpoint: TransparencyCheckpoint
    to_checkpoint: TransparencyCheckpoint
    proof: dict


class TransparencyExport(ProtocolObject):
    export_format: Literal["vat-transparency-export/1"] = "vat-transparency-export/1"
    log_id: str
    entries: list[TransparencyEntry]
    checkpoints: list[TransparencyCheckpoint]


def consistent(first: TransparencyCheckpoint, last: TransparencyCheckpoint, proof: dict) -> bool:
    """Check two authenticated checkpoint views, including the equal-size case."""
    if (first.log_id != last.log_id or first.keyid != last.keyid
            or first.timestamp > last.timestamp or first.tree_size > last.tree_size):
        return False
    if first.tree_size == last.tree_size:
        return first == last and proof == {}
    return check_consistency(first.root_hash, last.root_hash, proof,
                             earlier_size=first.tree_size, later_size=last.tree_size)


def verify_bindings(evidence: TransparencyEvidence, bundle: Bundle, trust: TrustBundle) -> None:
    """Authenticate checkpoints and bind a logged receipt to this session."""
    from .notary import attestation_digest
    receipt, entry, head = bundle.notary_receipt, evidence.entry, bundle.signed_tree_head
    evidence.checkpoint.verify(trust)
    if evidence.prior_checkpoint:
        evidence.prior_checkpoint.verify(trust)
    if (receipt is None or entry.notary_receipt_digest != sha256_hex(canonical_bytes(receipt.model_dump(mode="json")))
            or entry.attestation_digest != attestation_digest(bundle.attestation)
            or (entry.session_uuid, entry.session_root, entry.session_tree_size)
            != (head.session_uuid, head.root_hash, head.tree_size)
            or entry.log_id != evidence.checkpoint.log_id):
        raise ValueError("Transparency evidence binding mismatch")


def verify_membership(evidence: TransparencyEvidence) -> None:
    """Validate the log's entry inclusion and immediate checkpoint growth."""
    TransparencyEvidence.model_validate(evidence.model_dump(mode="json"))
    entry, current, prior = evidence.entry, evidence.checkpoint, evidence.prior_checkpoint
    if (entry.accepted_at > current.timestamp
            or evidence.inclusion_proof.get("leaf_index") != entry.leaf_index
            or not check_inclusion(entry.canonical(), current.root_hash, evidence.inclusion_proof,
                                   tree_size=current.tree_size)):
        raise ValueError("Invalid transparency inclusion or entry time")
    if current.tree_size == 1:
        if prior is not None or evidence.consistency_proof is not None:
            raise ValueError("First checkpoint cannot carry prior evidence")
    elif (prior is None or prior.tree_size != current.tree_size - 1
          or not consistent(prior, current, evidence.consistency_proof or {})
          or (entry.leaf_index >= prior.tree_size and entry.accepted_at < prior.timestamp)):
        raise ValueError("Invalid transparency consistency or time ordering")


class TransparencyLog:
    """Transactional entries and signed historical checkpoints in a private SQLite file."""

    def __init__(self, path: Path, trust: TrustBundle, *, signer: LocalSigner | None = None,
                 log_id: str | None = None):
        self.path, self.trust, self.signer = Path(path), trust, signer
        if self.path.is_symlink():
            raise ValueError("Transparency store cannot be a symlink")
        creating = not self.path.exists()
        if creating and (signer is None or log_id is None):
            raise ValueError("New transparency store requires explicit identity and signer")
        if signer is not None:
            if signer.role != Role.TRANSPARENCY_LOG or not trust.role_bound:
                raise ValueError("Expected transparency log signer")
            trust.resolve(signer.keyid, Role.TRANSPARENCY_LOG)
        if creating:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        self.db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            self.db.execute("PRAGMA synchronous=FULL")
            if creating:
                self.db.execute("BEGIN IMMEDIATE")
                self.db.execute("CREATE TABLE metadata (format TEXT, log_id TEXT, keyid TEXT)")
                self.db.execute("CREATE TABLE entries (position INTEGER PRIMARY KEY, attestation TEXT UNIQUE, receipt TEXT UNIQUE, entry BLOB, checkpoint BLOB)")
                self.db.execute("INSERT INTO metadata VALUES (?,?,?)", ("vat-transparency-log/1", log_id, signer.keyid))
                self.db.commit()
            row = self.db.execute("SELECT format,log_id,keyid FROM metadata").fetchall()
            if len(row) != 1 or row[0][0] != "vat-transparency-log/1":
                raise ValueError("Invalid transparency metadata")
            _, self.log_id, self.keyid = row[0]
            if (log_id is not None and log_id != self.log_id) or (signer and signer.keyid != self.keyid):
                raise ValueError("Transparency identity mismatch")
            if not trust.role_bound:
                raise ValueError("Transparency requires role-bound trust")
            trust.resolve(self.keyid, Role.TRANSPARENCY_LOG)
            self._read()
        except BaseException:
            self.db.close()
            raise

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> TransparencyLog:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _read(self):
        tree, entries, checkpoints = RecordTree(), [], []
        for index, attestation, receipt, raw, signed in self.db.execute("SELECT * FROM entries ORDER BY position"):
            entry = TransparencyEntry.model_validate(parse_json(raw))
            checkpoint = TransparencyCheckpoint.model_validate(parse_json(signed))
            checkpoint.verify(self.trust)
            if (raw != entry.canonical() or signed != checkpoint.canonical()
                    or index != len(entries) or entry.leaf_index != index
                    or entry.log_id != self.log_id or checkpoint.log_id != self.log_id
                    or checkpoint.keyid != self.keyid or attestation != entry.attestation_digest
                    or receipt != entry.notary_receipt_digest):
                raise ValueError("Corrupt transparency entry")
            tree.append(raw)
            if (checkpoint.tree_size != tree.size or checkpoint.root_hash != tree.root_hex
                    or checkpoint.timestamp != entry.accepted_at
                    or (checkpoints and checkpoint.timestamp < checkpoints[-1].timestamp)):
                raise ValueError("Corrupt transparency checkpoint")
            entries.append(entry)
            checkpoints.append(checkpoint)
        return tree, entries, checkpoints

    def append(self, bundle: Bundle, *, now: datetime | None = None) -> TransparencyEvidence:
        """Validate and atomically append, or return the existing leaf on exact retry."""
        from .notary import local_null_screen, verify_receipt, attestation_digest
        if self.signer is None:
            raise ValueError("Append requires private log signer")
        local_null_screen(bundle, self.trust)
        if bundle.notary_receipt is None or not verify_receipt(bundle.notary_receipt, bundle, self.trust):
            raise ValueError("Append requires a validated notary receipt")
        digest = attestation_digest(bundle.attestation)
        receipt = sha256_hex(canonical_bytes(bundle.notary_receipt.model_dump(mode="json")))
        self.db.execute("BEGIN IMMEDIATE")
        try:
            tree, entries, checkpoints = self._read()
            existing = next((e for e in entries if e.attestation_digest == digest), None)
            if existing:
                if existing.notary_receipt_digest != receipt:
                    raise ValueError("Conflicting receipt for existing attestation")
                index = existing.leaf_index
            else:
                stamp = now or datetime.now(timezone.utc)
                if checkpoints and stamp < checkpoints[-1].timestamp:
                    raise ValueError("Transparency clock moved backwards")
                index = tree.size
                head = bundle.signed_tree_head
                entry = TransparencyEntry(log_id=self.log_id, leaf_index=index, accepted_at=stamp,
                    notary_receipt_digest=receipt, attestation_digest=digest, session_uuid=head.session_uuid,
                    session_root=head.root_hash, session_tree_size=head.tree_size)
                tree.append(entry.canonical())
                checkpoint = TransparencyCheckpoint(log_id=self.log_id, tree_size=tree.size,
                    root_hash=tree.root_hex, timestamp=stamp, keyid=self.keyid)
                checkpoint.signature = self.signer.sign(checkpoint.payload())
                self.db.execute("INSERT INTO entries VALUES (?,?,?,?,?)",
                    (index, digest, receipt, entry.canonical(), checkpoint.canonical()))
                entries.append(entry)
                checkpoints.append(checkpoint)
            result = TransparencyEvidence(entry=entries[index], checkpoint=checkpoints[-1],
                inclusion_proof=tree.prove_inclusion(index),
                prior_checkpoint=checkpoints[-2] if tree.size > 1 else None,
                consistency_proof=tree.prove_consistency(tree.size - 1) if tree.size > 1 else None)
            self.db.commit()
            return result
        except BaseException:
            self.db.rollback()
            raise

    def export(self) -> TransparencyExport:
        """Export public observations; never private keys or original session contents."""
        _, entries, checkpoints = self._read()
        return TransparencyExport(log_id=self.log_id, entries=entries, checkpoints=checkpoints)

    def consistency(self, first: TransparencyCheckpoint, last: TransparencyCheckpoint) -> ConsistencyEvidence:
        """Prove growth between exact historical checkpoints even after later appends."""
        _, entries, checkpoints = self._read()
        for checkpoint in (first, last):
            checkpoint.verify(self.trust)
            if not 0 < checkpoint.tree_size <= len(checkpoints) or checkpoints[checkpoint.tree_size - 1] != checkpoint:
                raise ValueError("Checkpoint is not in this log")
        if first.tree_size > last.tree_size:
            raise ValueError("Reversed checkpoint sizes")
        tree = RecordTree.from_leaves([e.canonical() for e in entries[:last.tree_size]])
        proof = {} if first == last else tree.prove_consistency(first.tree_size)
        return ConsistencyEvidence(from_checkpoint=first, to_checkpoint=last, proof=proof)
