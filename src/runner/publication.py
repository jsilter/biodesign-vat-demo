"""Durable preparation and recovery of local notarized evidence publication."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

from vatlog.bundle import Bundle
from vatlog.canon import sha256_hex
from vatlog.keys import generate_key, save_private, load_private
from vatlog.roles import Keyring, LocalSigner, Role, TrustBundle
from vatlog.transparency import TransparencyLog
from vatlog.transparency_cli import atomic_bundle, atomic_write
from vatlog.verify import verify_bundle


class PublicationStore:
    """Keep prepared bundles private and resume publication without regenerating signatures."""

    def __init__(self, root: Path, keys: Keyring | None = None):
        self.root = Path(root)
        if self.root.is_symlink():
            raise ValueError("Publication directory cannot be a symlink")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        keydir = self.root / "publication-keys"
        if (self.root / "publication.sqlite").exists() and not (self.root / "transparency.sqlite").exists():
            raise ValueError("Existing publication transparency log is missing; restore its original state")
        existing = (self.root / "transparency.sqlite").exists() or (self.root / "publication.sqlite").exists()
        if keys is None:
            if keydir.is_symlink():
                raise ValueError("Publication key directory cannot be a symlink")
            keydir.mkdir(mode=0o700, exist_ok=True)
            signers = []
            for role in Role:
                path = keydir / f"{role.value}.key"
                if path.is_symlink() or (existing and not path.exists()):
                    raise ValueError("Existing publication key is missing or unsafe")
                if not path.exists():
                    save_private(generate_key(), path)
                signers.append(LocalSigner(role, load_private(path)))
            keys = Keyring(*signers)
        self.keys = keys
        self.trust = keys.trust_bundle()
        self.log = TransparencyLog(self.root / "transparency.sqlite", self.trust,
            signer=keys.transparency_log, log_id="local-" + keys.transparency_log.keyid)
        database = self.root / "publication.sqlite"
        if database.is_symlink():
            self.log.close()
            raise ValueError("Publication database cannot be a symlink")
        self.db = sqlite3.connect(database)
        os.chmod(database, 0o600)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS publications (id TEXT PRIMARY KEY, request TEXT, prepared BLOB, trust TEXT, response BLOB, anchored BLOB, complete INTEGER DEFAULT 0)")
        self.db.commit()

    def close(self) -> None:
        self.db.close()
        self.log.close()

    def __enter__(self) -> PublicationStore:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def prepared(self, identity: str, request_digest: str) -> bool:
        row = self.db.execute("SELECT request FROM publications WHERE id=?", (identity,)).fetchone()
        if row and row[0] != request_digest:
            raise ValueError("Publication identity conflicts with changed inputs")
        return row is not None

    def prepare(self, identity: str, request_digest: str, archive: bytes, response: bytes) -> None:
        """Commit the immutable submission before any transparency append."""
        if self.prepared(identity, request_digest):
            return
        with self.db:
            self.db.execute("INSERT INTO publications (id,request,prepared,trust,response) VALUES (?,?,?,?,?)",
                (identity, request_digest, archive, self.trust.model_dump_json(), response))

    def publish(self, identity: str) -> Path:
        """Recover an identical append, save its evidence, then publish all outputs."""
        row = self.db.execute("SELECT prepared,trust,response,anchored FROM publications WHERE id=?", (identity,)).fetchone()
        if row is None:
            raise ValueError("Publication has not been prepared")
        prepared, raw_trust, response, anchored = row
        trust = TrustBundle.model_validate_json(raw_trust)
        if trust != self.trust:
            raise ValueError("Prepared publication trust differs from current keys")
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.zip"
            atomic_write(source, anchored or prepared)
            bundle = Bundle.load(source)
            if anchored is None:
                bundle.transparency_evidence = self.log.append(bundle)
                atomic_bundle(bundle, source)
                anchored = source.read_bytes()
                with self.db:
                    self.db.execute("UPDATE publications SET anchored=? WHERE id=?", (anchored, identity))
            report = verify_bundle(bundle, trust, require_transparency=True)
            if report.exit_code:
                raise ValueError("Prepared publication verification failed")
        output = self.root / "published-evidence" / sha256_hex(identity.encode())
        for name, raw in (("bundle.zip", anchored), ("trust.json", raw_trust.encode()),
                          ("response.txt", response), ("report.txt", report.render().encode())):
            atomic_write(output / name, raw)
        with self.db:
            self.db.execute("UPDATE publications SET complete=1 WHERE id=?", (identity,))
        return output

    def recover(self) -> None:
        """Resume only prepared publications; never execute model or scientific work."""
        for (identity,) in self.db.execute("SELECT id FROM publications WHERE complete=0").fetchall():
            self.publish(identity)
