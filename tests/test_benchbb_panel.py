import copy
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from demo.benchbb_panel import (
    ARM_ORDER,
    PINNED_FILES,
    BenchBBPanel,
    SourceFile,
    panel_bytes,
    select_panel,
    selection_hash,
    verify_source_files,
)


FIXTURE = Path(__file__).parents[1] / "fixtures/benchbb_bbf14_panel.json"


def load_panel() -> BenchBBPanel:
    return BenchBBPanel.model_validate_json(FIXTURE.read_bytes())


def source_files() -> list[SourceFile]:
    return [SourceFile(path=path, bytes_len=size, sha256=digest)
            for path, (size, digest) in PINNED_FILES.items()]


def source_rows() -> list[dict]:
    rows = []
    for design in load_panel().designs:
        row = design.model_dump(exclude={"arm", "selection_hash"})
        row["target"] = "BBF-14"
        row["unused_predictor"] = 0.123
        rows.append(row)
    arm_sources = {
        "mythos_multi_target": ("Mythos Preview", "multi_target", "mythos_preview_multi_target"),
        "mythos_single_target": ("Mythos Preview", "single_target", "mythos_preview_single_target"),
        "opus_4_8_multi_target": ("Opus 4.8", "multi_target", "opus_4_8_multi_target"),
    }
    for arm_index, arm in enumerate(ARM_ORDER):
        model, campaign, prefix = arm_sources[arm]
        for offset in range(26):
            unique = arm_index * 26 + offset + 1
            rows.append({
                "uuid": f"{unique:08x}-0000-0000-0000-{unique:012x}",
                "full_name": f"{prefix}_bbf14_filler{offset:02d}",
                "design_model": model,
                "campaign": campaign,
                "target": "BBF-14",
                "rank": 100 + offset,
                "binder_length": 4,
                "sequence": "AAAA",
                "generator": "test fixture",
                "sequence_design_method": "test fixture",
                "adaptyv_binding": "not_tested",
                "adaptyv_kd_nM": None,
                "twist_binding": "not_tested",
                "twist_kd_nM": None,
                "binder_final": False,
                "kd_nM_final": None,
                "vendor_agreement": "not_tested",
                "ipsae_min_ef2fast": 0.5,
            })
    assert len(rows) == 90
    return rows


def test_checked_in_panel_has_exact_predeclared_composition_and_stable_bytes():
    panel = load_panel()
    assert sum(row.binder_final for row in panel.designs) == 3
    assert sum(not row.binder_final for row in panel.designs) == 9
    assert panel_bytes(panel) == FIXTURE.read_bytes()
    assert [row.full_name for row in panel.designs] == [
        "mythos_preview_multi_target_bbf14_rank27",
        "mythos_preview_single_target_bbf14_rank20",
        "opus_4_8_multi_target_bbf14_rank21",
        "mythos_preview_multi_target_bbf14_rank24",
        "mythos_preview_multi_target_bbf14_rank22",
        "mythos_preview_multi_target_bbf14_rank07",
        "mythos_preview_single_target_bbf14_rank19",
        "mythos_preview_single_target_bbf14_rank24",
        "mythos_preview_single_target_bbf14_rank06",
        "opus_4_8_multi_target_bbf14_rank22",
        "opus_4_8_multi_target_bbf14_rank20",
        "opus_4_8_multi_target_bbf14_rank10",
    ]


def test_panel_preserves_vendor_distinction_for_twist_only_binders():
    binders = [row for row in load_panel().designs if row.binder_final]
    assert [row.vendor_agreement for row in binders] == [
        "both_bind", "twist_only_tested", "twist_only_tested"
    ]
    assert [row.adaptyv_binding for row in binders] == ["binder", "not_tested", "not_tested"]


def test_selection_is_independent_of_source_order_and_unrelated_columns():
    rows = source_rows()
    first = select_panel(rows, source_files())
    for row in rows:
        row["unused_predictor"] = 999
        row["ipsae_min_ef2fast"] = 1.0 - row["ipsae_min_ef2fast"]
    second = select_panel(reversed(rows), list(reversed(source_files())))
    assert [row.full_name for row in first.designs] == [row.full_name for row in second.designs]
    assert [row.full_name for row in first.designs] == [row.full_name for row in load_panel().designs]


def test_selection_hash_is_public_seeded_sha256_and_changes_with_name():
    assert selection_hash("example") == "1a24fd99375f6e1732060fdd58089f7be1c0b3ab77e3c312304731627f3da080"
    assert selection_hash("example") != selection_hash("Example")


@pytest.mark.parametrize("mutation, message", [
    (lambda rows: rows.__setitem__(1, {**rows[1], "binder_final": False}), "binder labels changed"),
    (lambda rows: rows.__setitem__(2, {**rows[2], "uuid": rows[1]["uuid"]}), "identifiers must be unique"),
    (lambda rows: rows.__setitem__(3, {**rows[3], "target": "other"}), "exactly 90"),
])
def test_changed_source_identity_or_labels_are_rejected(mutation, message):
    rows = source_rows()
    mutation(rows)
    with pytest.raises(ValueError, match=message):
        select_panel(rows, source_files())


def test_invalid_sequence_and_non_ascii_name_are_rejected_before_selection():
    rows = source_rows()
    rows[0] = {**rows[0], "sequence": "AAAAZ", "binder_length": 5}
    with pytest.raises(ValidationError):
        select_panel(rows, source_files())
    rows = source_rows()
    rows[0] = {**rows[0], "full_name": "désign"}
    with pytest.raises(ValidationError):
        select_panel(rows, source_files())


def test_manifest_rejects_changed_source_digest_and_panel_label():
    value = json.loads(FIXTURE.read_text())
    value["source_files"][0]["sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="source file identities"):
        BenchBBPanel.model_validate(value)
    value = json.loads(FIXTURE.read_text())
    value["designs"][3].update({
        "binder_final": True,
        "adaptyv_binding": "binder",
        "adaptyv_kd_nM": 100.0,
        "twist_binding": "binder",
        "twist_kd_nM": 100.0,
        "kd_nM_final": 100.0,
        "vendor_agreement": "both_bind",
    })
    with pytest.raises(ValidationError, match="three predeclared binders"):
        BenchBBPanel.model_validate(value)


def test_manifest_rejects_duplicate_design_even_if_count_is_unchanged():
    value = json.loads(FIXTURE.read_text())
    value["designs"][-1] = copy.deepcopy(value["designs"][-2])
    with pytest.raises(ValidationError, match="identifiers must be unique"):
        BenchBBPanel.model_validate(value)


@pytest.mark.parametrize("mutation,message", [
    (lambda value: value["source_files"].append(copy.deepcopy(value["source_files"][0])),
     "identities must be unique"),
    (lambda value: value["designs"][0].update(selection_hash="0" * 64),
     "selection hash"),
    (lambda value: value["designs"][0].update(vendor_agreement="twist_only_tested"),
     "vendor agreement"),
    (lambda value: value["designs"][0].update(adaptyv_kd_nM=None),
     "Adaptyv affinity"),
])
def test_manifest_rejects_internally_inconsistent_source_claims(mutation, message):
    value = json.loads(FIXTURE.read_text())
    mutation(value)
    with pytest.raises(ValidationError, match=message):
        BenchBBPanel.model_validate(value)


def test_source_file_verification_hashes_bytes_not_metadata(tmp_path, monkeypatch):
    path = tmp_path / "source.bin"
    path.write_bytes(b"expected")
    monkeypatch.setattr(
        "demo.benchbb_panel.PINNED_FILES",
        {"source.bin": (8, hashlib.sha256(b"expected").hexdigest())},
    )
    assert verify_source_files(tmp_path)[0].sha256 == hashlib.sha256(b"expected").hexdigest()
    path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="pinned source mismatch"):
        verify_source_files(tmp_path)
