import pytest

from demo.disclosure import withhold_records
from demo.sessions import build_clean
from vatlog.bundle import Bundle, build_bundle
from vatlog.verify import Outcome, verify_bundle


def test_withholding_preserves_commitment_and_fails_only_completeness(tmp_path):
    log, key, *_ = build_clean(tmp_path)
    full = build_bundle(log)
    original = full.model_dump()
    partial = withhold_records(full, {2})
    assert full.model_dump() == original
    assert partial.signed_tree_head == full.signed_tree_head
    assert partial.attestation == full.attestation
    assert partial.revealed_indices() == [0, 1, 3]
    assert verify_bundle(full, key.public_key()).outcome is Outcome.OK
    report = verify_bundle(partial, key.public_key())
    assert report.outcome is Outcome.INCOMPLETE
    path = tmp_path / "partial.zip"
    partial.save(path)
    assert verify_bundle(Bundle.load(path), key.public_key()).outcome is Outcome.INCOMPLETE
    referenced = {a.sha256 for r in partial.records for a in [*r.inputs, *r.results, *r.intermediates]}
    assert set(partial.blobs) == referenced


@pytest.mark.parametrize("indices", [set(), {0}, {999}])
def test_withholding_rejects_invalid_positions(tmp_path, indices):
    log, *_ = build_clean(tmp_path)
    with pytest.raises(ValueError):
        withhold_records(build_bundle(log), indices)
