"""Recover exact signed publications across failed append responses and file writes."""

from datetime import datetime, timezone

import pytest

from runner.evidence import capture
from runner.publication import PublicationStore
from vatlog.bundle import Bundle
from vatlog.transparency import TransparencyLog


@pytest.mark.parametrize("boundary", ["prepared", "append", "output"])
def test_publication_recovery_keeps_one_leaf_and_original_bytes(tmp_path, monkeypatch, boundary):
    started = datetime.now(timezone.utc)
    if boundary == "prepared":
        original = PublicationStore.prepare
        def fail_after_prepare(self, *args, **kwargs):
            original(self, *args, **kwargs)
            raise OSError("injected after preparation")
        monkeypatch.setattr(PublicationStore, "prepare", fail_after_prepare)
    elif boundary == "append":
        original = TransparencyLog.append
        def fail_after_commit(self, *args, **kwargs):
            original(self, *args, **kwargs)
            raise OSError("injected after commit")
        monkeypatch.setattr(TransparencyLog, "append", fail_after_commit)
    else:
        import runner.publication as publication
        original = publication.atomic_write
        def fail_output(path, raw):
            if path.name == "report.txt":
                raise OSError("injected output failure")
            return original(path, raw)
        monkeypatch.setattr(publication, "atomic_write", fail_output)
    with pytest.raises(OSError):
        capture(tmp_path, "one", "prompt", [], "response", started)
    monkeypatch.undo()
    with PublicationStore(tmp_path / ".publication") as store:
        before = store.log.export()
        assert len(before.entries) == (0 if boundary == "prepared" else 1)
        raw = store.db.execute("SELECT prepared FROM publications").fetchone()[0]
        store.recover()
        if boundary != "prepared":
            assert store.log.export() == before
        before = store.log.export()
        assert len(before.entries) == 1
        assert store.db.execute("SELECT prepared FROM publications").fetchone()[0] == raw
    path = capture(tmp_path, "one", "prompt", [], "response", started)
    assert Bundle.load(path / "bundle.zip").transparency_evidence.entry == before.entries[0]
    with pytest.raises(ValueError, match="conflict"):
        capture(tmp_path, "one", "changed", [], "response", started)


def test_shared_key_survives_restart_and_missing_key_fails(tmp_path):
    started = datetime.now(timezone.utc)
    first = capture(tmp_path, "one", "prompt", [], "response", started)
    second = capture(tmp_path, "two", "prompt", [], "response", started)
    one, two = [Bundle.load(p / "bundle.zip").transparency_evidence for p in (first, second)]
    assert one.checkpoint.keyid == two.checkpoint.keyid
    assert two.checkpoint.tree_size == 2
    (tmp_path / ".publication/publication-keys/transparency_log.key").unlink()
    with pytest.raises(ValueError, match="missing"):
        PublicationStore(tmp_path / ".publication")


def test_missing_log_is_not_silently_recreated(tmp_path):
    capture(tmp_path, "one", "prompt", [], "response", datetime.now(timezone.utc))
    root = tmp_path / ".publication"
    original = (root / "transparency.sqlite").read_bytes()
    (root / "transparency.sqlite").unlink()
    with pytest.raises(ValueError, match="log is missing"):
        PublicationStore(root)
    assert not (root / "transparency.sqlite").exists()
    (root / "transparency.sqlite").write_bytes(original)
    with PublicationStore(root) as store:
        assert len(store.log.export().entries) == 1
