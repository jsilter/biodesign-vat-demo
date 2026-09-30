"""Single-owner durable design state and integrity-checked content-addressed artifacts."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from uuid import UUID

from .design_inputs import DesignError


def json_bytes(value: object) -> bytes:
    """Serialize strict JSON, rejecting nonfinite scientific values."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()


class DesignStore:
    """Keep immutable request identities, durable snapshots and artifact bytes."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.lock = (self.root / "owner.lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise DesignError("STORE_BUSY", "Another backend owns this workflow directory.")
        self.db = sqlite3.connect(self.root / "state.sqlite")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS workflows (id TEXT PRIMARY KEY, request BLOB NOT NULL, snapshot BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS synthesized_workflows (
                id TEXT PRIMARY KEY, request BLOB NOT NULL, snapshot BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS dynamic_jobs (
                id TEXT PRIMARY KEY, request BLOB NOT NULL, snapshot BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS artifacts (workflow TEXT, digest TEXT, name TEXT, kind TEXT,
                size INTEGER, PRIMARY KEY(workflow, digest, name));
        """)
        self.db.commit()
        os.chmod(self.root / "state.sqlite", 0o600)
        os.chmod(self.root / "owner.lock", 0o600)

    def close(self) -> None:
        """Release the database and exclusive owner lock."""
        if getattr(self, "execution_recorder", None) is not None:
            self.execution_recorder.close()
        self.db.close()
        self.lock.close()

    def keys(self):
        """Recover distinct local signing keys; private bytes never enter the artifact index."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        from vatlog.roles import Keyring, LocalSigner, Role

        directory = self.root / "keys"
        if directory.is_symlink():
            raise DesignError("EVIDENCE_FAILED", "Signing-key directory cannot be a symlink.")
        directory.mkdir(mode=0o700, exist_ok=True)
        signers = []
        for role in Role:
            path = directory / f"{role.value}.key"
            if path.is_symlink():
                raise DesignError("EVIDENCE_FAILED", "Signing-key path cannot be a symlink.")
            if not path.exists():
                if (self.root / "transparency.sqlite").exists() or (self.root / "publication.sqlite").exists():
                    raise DesignError("EVIDENCE_FAILED", "Existing transparency signing key is missing.")
                key = Ed25519PrivateKey.generate()
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                                   serialization.NoEncryption()))
                    stream.flush()
                    os.fsync(stream.fileno())
            os.chmod(path, 0o600)
            signers.append(LocalSigner(role, Ed25519PrivateKey.from_private_bytes(path.read_bytes())))
        return Keyring(*signers)

    def create(self, id: str, request: dict, snapshot: dict) -> bool:
        """Return False for an identical durable request; conflict on changed identity."""
        id = str(UUID(id))
        row = self.db.execute("SELECT request FROM workflows WHERE id=?", (id,)).fetchone()
        if row:
            if row[0] != json_bytes(request):
                raise DesignError("REQUEST_CONFLICT", "Workflow ID already belongs to a different request.")
            return False
        with self.db:
            self.db.execute("INSERT INTO workflows VALUES (?,?,?)", (id, json_bytes(request), json_bytes(snapshot)))
        return True

    def save(self, id: str, snapshot: dict) -> None:
        """Atomically replace a persisted workflow snapshot."""
        with self.db:
            cursor = self.db.execute("UPDATE workflows SET snapshot=? WHERE id=?", (json_bytes(snapshot), id))
            if cursor.rowcount != 1:
                raise DesignError("WORKFLOW_NOT_FOUND", "Workflow not found.")

    def get(self, id: str) -> tuple[dict, dict]:
        """Load request and progress without interpreting user-controlled paths."""
        row = self.db.execute("SELECT request,snapshot FROM workflows WHERE id=?", (str(UUID(id)),)).fetchone()
        if not row:
            raise DesignError("WORKFLOW_NOT_FOUND", "Workflow not found.")
        return json.loads(row[0]), json.loads(row[1])

    def all(self) -> list[tuple[dict, dict]]:
        """List persisted workflows for startup reconciliation."""
        return [(json.loads(a), json.loads(b)) for a, b in self.db.execute("SELECT request,snapshot FROM workflows")]

    def create_synthesized(self, id: str, request: dict, snapshot: dict) -> bool:
        """Persist one immutable synthesized-workflow proposal."""
        id = str(UUID(id))
        encoded = json_bytes(request)
        row = self.db.execute("SELECT request FROM synthesized_workflows WHERE id=?", (id,)).fetchone()
        if row:
            if row[0] != encoded:
                raise DesignError("REQUEST_CONFLICT", "Workflow ID already belongs to a different proposal.")
            return False
        with self.db:
            self.db.execute("INSERT INTO synthesized_workflows VALUES (?,?,?)",
                            (id, encoded, json_bytes(snapshot)))
        return True

    def save_synthesized(self, id: str, snapshot: dict) -> None:
        """Atomically update synthesized-workflow progress."""
        with self.db:
            cursor = self.db.execute("UPDATE synthesized_workflows SET snapshot=? WHERE id=?",
                                     (json_bytes(snapshot), str(UUID(id))))
            if cursor.rowcount != 1:
                raise DesignError("WORKFLOW_NOT_FOUND", "Workflow not found.")

    def get_synthesized(self, id: str) -> tuple[dict, dict]:
        """Load one proposal and its durable execution snapshot."""
        row = self.db.execute("SELECT request,snapshot FROM synthesized_workflows WHERE id=?",
                              (str(UUID(id)),)).fetchone()
        if not row:
            raise DesignError("WORKFLOW_NOT_FOUND", "Workflow not found.")
        return json.loads(row[0]), json.loads(row[1])

    def all_synthesized(self) -> list[tuple[dict, dict]]:
        """Return synthesized workflows for startup reconciliation."""
        return [(json.loads(request), json.loads(snapshot)) for request, snapshot in
                self.db.execute("SELECT request,snapshot FROM synthesized_workflows")]

    def create_dynamic(self, id: str, request: dict, snapshot: dict) -> bool:
        """Persist one immutable generated-job proposal."""
        id = str(UUID(id))
        encoded = json_bytes(request)
        row = self.db.execute("SELECT request FROM dynamic_jobs WHERE id=?", (id,)).fetchone()
        if row:
            if row[0] != encoded:
                raise DesignError("REQUEST_CONFLICT", "Job ID already belongs to another proposal.")
            return False
        with self.db:
            self.db.execute("INSERT INTO dynamic_jobs VALUES (?,?,?)",
                            (id, encoded, json_bytes(snapshot)))
        return True

    def save_dynamic(self, id: str, snapshot: dict) -> None:
        with self.db:
            cursor = self.db.execute("UPDATE dynamic_jobs SET snapshot=? WHERE id=?",
                                     (json_bytes(snapshot), str(UUID(id))))
            if cursor.rowcount != 1:
                raise DesignError("WORKFLOW_NOT_FOUND", "Generated job not found.")

    def get_dynamic(self, id: str) -> tuple[dict, dict]:
        row = self.db.execute("SELECT request,snapshot FROM dynamic_jobs WHERE id=?",
                              (str(UUID(id)),)).fetchone()
        if not row:
            raise DesignError("WORKFLOW_NOT_FOUND", "Generated job not found.")
        return json.loads(row[0]), json.loads(row[1])

    def all_dynamic(self) -> list[tuple[dict, dict]]:
        return [(json.loads(request), json.loads(snapshot)) for request, snapshot in
                self.db.execute("SELECT request,snapshot FROM dynamic_jobs")]

    def artifact(self, workflow: str, name: str, kind: str, raw: bytes) -> dict:
        """Commit artifact bytes before adding the download-manifest reference."""
        workflow = str(UUID(workflow))
        if Path(name).name != name or name in {".", ".."} or not name or "\\" in name or any(ord(c) < 32 for c in name):
            raise DesignError("OUTPUT_INVALID", "Unsafe artifact name.")
        digest = hashlib.sha256(raw).hexdigest()
        directory = self.root / workflow / "blobs"
        if directory.is_symlink() or directory.parent.is_symlink():
            raise DesignError("OUTPUT_INVALID", "Artifact directory cannot be a symlink.")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / digest
        if path.is_symlink():
            raise DesignError("OUTPUT_INVALID", "Artifact path cannot be a symlink.")
        if not path.exists():
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        elif path.read_bytes() != raw:
            raise DesignError("OUTPUT_INVALID", "Existing artifact is corrupt.")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO artifacts VALUES (?,?,?,?,?)",
                            (workflow, digest, name, kind, len(raw)))
        return {"sha256": digest, "name": name, "kind": kind, "bytes_len": len(raw)}

    def artifacts(self, workflow: str) -> list[dict]:
        """List only artifacts explicitly registered to this workflow."""
        return [{"sha256": d, "name": n, "kind": k, "bytes_len": s}
                for d, n, k, s in self.db.execute("SELECT digest,name,kind,size FROM artifacts WHERE workflow=?", (workflow,))]

    def read(self, workflow: str, digest: str, name: str) -> bytes:
        """Authenticate download membership and digest/length before returning bytes."""
        row = self.db.execute("SELECT size FROM artifacts WHERE workflow=? AND digest=? AND name=?",
                              (str(UUID(workflow)), digest, name)).fetchone()
        if row is None:
            raise DesignError("ARTIFACT_NOT_FOUND", "Artifact unavailable.")
        path = self.root / str(UUID(workflow)) / "blobs" / digest
        if path.is_symlink() or path.parent.is_symlink() or path.parent.parent.is_symlink():
            raise DesignError("OUTPUT_INVALID", "Artifact path cannot be a symlink.")
        raw = path.read_bytes()
        if len(raw) != row[0] or hashlib.sha256(raw).hexdigest() != digest:
            raise DesignError("OUTPUT_INVALID", "Artifact integrity check failed.")
        return raw
