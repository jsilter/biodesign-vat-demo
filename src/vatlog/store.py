"""The append-only session log: records, blobs, Merkle tree, sealing.

Local session backend. A simulated transparency log lives in transparency.py; optional
public Rekor and RFC 3161 anchoring lives in sigstore_transparency.py. The signed tree
head (head.py) is the trust anchor, which means this log is a *self-signed* claim by the
designer's recorder -- RAND's point that without an independent notary "the schema reduces
to a self-signed claim by the designer" applies in full. What
the local log still demonstrates is within-session completeness accounting, which is the
gap this build targets.

Layout on disk::

    <root>/<session_uuid>/records.jsonl   one canonical record per line, append order
    <root>/<session_uuid>/head.json       signed tree head, written at seal
    <root>/<session_uuid>/attestation.json
    <root>/blobs/<sha256>                 sequence and artifact bytes, content-addressed
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .attest import DSSEEnvelope, seal
from .canon import canonical_record_bytes, parse_json, record_digest, sha256_hex
from .head import SignedTreeHead, sign_head
from .merkle import RecordTree
from .roles import LocalSigner
from .schema import GENESIS_PREV, Artifact, EnvSnapshot, HashLink, VatRecord


class BlobStore:
    """Content-addressed side store. RAND's typing rule keeps sequences out of the record."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, data: bytes) -> str:
        digest = sha256_hex(data)
        path = self.root / digest
        if not path.exists():
            path.write_bytes(data)
        return digest

    def put_text(self, text: str) -> str:
        return self.put(text.encode("utf-8"))

    def get(self, digest: str) -> bytes:
        return (self.root / digest).read_bytes()

    def has(self, digest: str) -> bool:
        return (self.root / digest).exists()

    def artifact(self, kind: str, name: str, data: bytes) -> Artifact:
        return Artifact(kind=kind, name=name, sha256=self.put(data), bytes_len=len(data))


class ChainError(RuntimeError):
    pass


class SessionLog:
    """One design session. Append-only; ``append`` is the only mutator."""

    def __init__(
        self, root: Path, session_uuid: str, log_id: str = "local-0", *, _existing: bool = False
    ) -> None:
        if (not session_uuid or session_uuid in (".", "..")
                or any(char in session_uuid for char in ("/", "\\", "\x00"))):
            raise ValueError("session id must be a single nonempty path component")
        self.root = Path(root)
        self.session_uuid = session_uuid
        self.log_id = log_id
        self.dir = self.root / session_uuid
        if self.dir.is_symlink():
            raise ValueError("session directory must not be a symlink")
        if _existing:
            if not self.dir.is_dir():
                raise FileNotFoundError(self.dir)
        else:
            # Never append a second genesis chain to a previously created session.
            self.dir.mkdir(parents=True, exist_ok=False)
        self.blobs = BlobStore(self.root / "blobs")
        self.records: list[VatRecord] = []
        self.tree = RecordTree()
        self._path = self.dir / "records.jsonl"

    # -- writing ---------------------------------------------------------------------

    def _require_unsealed(self) -> None:
        # Either file is enough: an interrupted seal must not be silently replaced.
        if (self.dir / "head.json").exists() or (self.dir / "attestation.json").exists():
            raise ChainError("session is sealed; create a new session instead")

    def append(self, record: VatRecord) -> VatRecord:
        """Finalize ``record``'s seq_no and hash link, hash it, and append it.

        The caller supplies everything except ``seq_no`` and ``link``; those are assigned
        here so a caller cannot choose its own position in the chain.
        """
        self._require_unsealed()
        seq_no = len(self.records)
        sha_prev = GENESIS_PREV if seq_no == 0 else record_digest(self.records[-1])
        final = record.model_copy(
            deep=True,
            update={
                "seq_no": seq_no,
                "session_uuid": self.session_uuid,
                "link": HashLink(sha_prev=sha_prev, leaf_index=seq_no, anchor_id=None),
            }
        )
        leaf = canonical_record_bytes(final)
        with self._path.open("ab") as fh:
            fh.write(leaf + b"\n")
        index = self.tree.append(leaf)
        if index != seq_no:
            raise ChainError(f"tree index {index} != seq_no {seq_no}")
        self.records.append(final)
        return final.model_copy(deep=True)

    def seal(
        self,
        key: Ed25519PrivateKey | LocalSigner,
        *,
        recorder_identity: str,
        environment: EnvSnapshot,
        timestamp: datetime | None = None,
    ) -> tuple[SignedTreeHead, DSSEEnvelope]:
        """Phase 2. Root hash over the full log, plus the signed attestation."""
        self._require_unsealed()
        now = timestamp or datetime.now(timezone.utc)
        head = sign_head(
            key,
            log_id=self.log_id,
            session_uuid=self.session_uuid,
            tree_size=self.tree.size,
            root_hash=self.tree.root_hex,
            timestamp=now,
        )
        envelope = seal(
            key,
            root_hash=self.tree.root_hex,
            tree_size=self.tree.size,
            session_uuid=self.session_uuid,
            recorder_identity=recorder_identity,
            environment=environment,
            sealed_at=now,
        )
        (self.dir / "head.json").write_text(head.model_dump_json(indent=2))
        (self.dir / "attestation.json").write_text(envelope.model_dump_json(indent=2))
        return head, envelope

    # -- reading ---------------------------------------------------------------------

    @classmethod
    def load(cls, root: Path, session_uuid: str, log_id: str = "local-0") -> "SessionLog":
        log = cls(root, session_uuid, log_id, _existing=True)
        if log._path.exists():
            for line in log._path.read_bytes().splitlines():
                if not line.strip():
                    continue
                record = VatRecord.model_validate(parse_json(line))
                expected_prev = GENESIS_PREV if not log.records else record_digest(log.records[-1])
                if (record.seq_no != len(log.records) or record.link.leaf_index != len(log.records)
                        or record.session_uuid != session_uuid or record.link.sha_prev != expected_prev
                        or canonical_record_bytes(record) != line):
                    raise ValueError("stored record is noncanonical or has inconsistent chain metadata")
                log.records.append(record)
                log.tree.append(line)
        return log

    def head(self) -> SignedTreeHead | None:
        path = self.dir / "head.json"
        if not path.exists():
            return None
        return SignedTreeHead.model_validate_json(path.read_text())

    def attestation(self) -> DSSEEnvelope | None:
        path = self.dir / "attestation.json"
        if not path.exists():
            return None
        return DSSEEnvelope.model_validate_json(path.read_text())

    def entry(self, index: int) -> VatRecord:
        """Return an independent snapshot of a recorded event."""
        return self.records[index].model_copy(deep=True)

    def find_by_result_digest(self, digest: str, *, kind: str | None = None) -> list[int]:
        """Indices with matching result bytes, optionally restricted to an artifact kind."""
        return [
            i
            for i, rec in enumerate(self.records)
            if any(a.sha256 == digest and (kind is None or a.kind == kind) for a in rec.results)
        ]
