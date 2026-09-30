"""Offline checks for the operator's full-release acquisition script."""
import hashlib
import importlib.util
from pathlib import Path

import pytest

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")
pytest.importorskip("modal")
spec = importlib.util.spec_from_file_location(
    "opentargets_staging", Path(__file__).parents[1] / "scripts/stage_opentargets.py")
staging = importlib.util.module_from_spec(spec)
spec.loader.exec_module(staging)


def test_file_validation_preserves_source_bytes_and_reports_real_rows(tmp_path):
    path = tmp_path / "evidence.parquet"
    pq.write_table(pa.table({"target": ["A", "B"], "score": [0.7, None]}), path)
    original = path.read_bytes()
    result = staging.inspect_file(path, hashlib.sha1(original).hexdigest())
    assert result["rows"] == 2
    assert result["bytes"] == len(original)
    assert result["sha256"] == hashlib.sha256(original).hexdigest()
    assert path.read_bytes() == original
    assert pq.read_table(path).to_pydict()["score"] == [0.7, None]


def test_file_validation_rejects_bytes_that_do_not_match_publisher(tmp_path):
    path = tmp_path / "evidence.parquet"
    pq.write_table(pa.table({"score": [0.7]}), path)
    original_sha1 = hashlib.sha1(path.read_bytes()).hexdigest()
    pq.write_table(pa.table({"score": [0.8]}), path)
    with pytest.raises(ValueError, match="checksum mismatch"):
        staging.inspect_file(path, original_sha1)


def test_file_validation_rejects_non_parquet_even_with_matching_checksum(tmp_path):
    path = tmp_path / "evidence.parquet"
    raw = b"not a Parquet table"
    path.write_bytes(raw)
    with pytest.raises(pa.ArrowInvalid):
        staging.inspect_file(path, hashlib.sha1(raw).hexdigest())


def test_existing_verified_file_is_reused_without_network(tmp_path, monkeypatch):
    path = tmp_path / "output" / "target" / "part.parquet"
    path.parent.mkdir(parents=True)
    pq.write_table(pa.table({"id": ["one"]}), path)
    digest = hashlib.sha1(path.read_bytes()).hexdigest()
    def unexpected_network(*args, **kwargs):
        pytest.fail("Verified staged bytes should be reused without fetching")
    monkeypatch.setattr(staging.urllib.request, "urlopen", unexpected_network)
    result = staging.download_file(tmp_path, ("output/target/part.parquet", digest))
    assert result["rows"] == 1
    assert result["path"] == "output/target/part.parquet"


def test_download_rejects_symlink_destination(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "output").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="Symlink"):
        staging.download_file(tmp_path, ("output/target/part.parquet", "0" * 40))


def test_trusted_volume_mount_can_be_a_symlink(tmp_path):
    storage = tmp_path / "storage"
    path = storage / "output" / "target" / "part.parquet"
    path.parent.mkdir(parents=True)
    pq.write_table(pa.table({"id": ["one"]}), path)
    mount = tmp_path / "volume"
    mount.symlink_to(storage, target_is_directory=True)
    result = staging.download_file(mount, ("output/target/part.parquet",
                                           hashlib.sha1(path.read_bytes()).hexdigest()))
    assert result["rows"] == 1


def test_inventory_rejects_changed_publisher_list():
    with pytest.raises(ValueError, match="inventory changed"):
        staging.inventory(b"changed source inventory")
