"""Frozen source selection for the BBF-14 BenchBB reference study."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from runner.design_inputs import sequence_bytes


DATASET_REPOSITORY = "Anthropic/claude-protein-binder-design"
DATASET_REVISION = "9e1b81696da46835e9e9cde9a3da976e0abc92ab"
DATASET_URL = (
    "https://huggingface.co/datasets/Anthropic/claude-protein-binder-design/"
    f"tree/{DATASET_REVISION}"
)
SELECTION_SEED = "vat-benchbb-bbf14-v1"
TARGET_SEQUENCE = (
    "MGTPLWALLGGPWRGTATYEDGTKVTLDYRYTRVSPDRLRADVTYTTPDGTTLEATVDLWKDANGVI"
    "RYHATYPDGTSADGTLTQLDADTLLATGTYDDGTKYTVTLTRV"
)

PINNED_FILES = {
    "data/tables/design_summary.parquet": (
        517810,
        "3e796869b8dc0c90d7ab0e12daf25308c0cf272d04c6a5e27281f903ecd18744",
    ),
    "data/tables/insilico/target_constructs.parquet": (
        17405,
        "df15c099ba81ebb98b2e86dfc81f15a9fd945ec588736ef1cc56f49fc0ae8c9d",
    ),
    "data/LICENSE.md": (
        771,
        "2e3fbdf3eeb6d70141dd55edb936f29eb916689661f04efae45ab56965cd924c",
    ),
    "data/README.md": (
        6091,
        "17896540adabb19f479335a7c3f34d6c24e858aff4d96f946ef911b1adc59cbb",
    ),
}

ARM_BY_SOURCE = {
    ("Mythos Preview", "multi_target"): "mythos_multi_target",
    ("Mythos Preview", "single_target"): "mythos_single_target",
    ("Opus 4.8", "multi_target"): "opus_4_8_multi_target",
}
ARM_ORDER = tuple(ARM_BY_SOURCE.values())
EXPECTED_BINDERS = {
    "mythos_preview_multi_target_bbf14_rank27",
    "mythos_preview_single_target_bbf14_rank20",
    "opus_4_8_multi_target_bbf14_rank21",
}


class SourceFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    path: str = Field(pattern=r"^[A-Za-z0-9_./-]+$")
    bytes_len: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class BenchBBDesign(BaseModel):
    """One source-backed design in the predeclared panel."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    uuid: str = Field(pattern=r"^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$")
    full_name: str = Field(pattern=r"^[a-z0-9_]+$")
    arm: Literal["mythos_multi_target", "mythos_single_target", "opus_4_8_multi_target"]
    design_model: Literal["Mythos Preview", "Opus 4.8"]
    campaign: Literal["multi_target", "single_target"]
    rank: int = Field(ge=1, le=30)
    binder_length: int = Field(ge=1, le=500)
    sequence: str = Field(pattern=r"^[ACDEFGHIKLMNPQRSTVWY]+$")
    generator: str = Field(min_length=1)
    sequence_design_method: str = Field(min_length=1)
    adaptyv_binding: Literal["binder", "non_binder", "not_tested"]
    adaptyv_kd_nM: float | None = Field(default=None, gt=0)
    twist_binding: Literal["binder", "non_binder", "not_tested"]
    twist_kd_nM: float | None = Field(default=None, gt=0)
    binder_final: bool
    kd_nM_final: float | None = Field(default=None, gt=0)
    vendor_agreement: Literal["both_bind", "twist_only_tested", "neither_bind"]
    ipsae_min_ef2fast: float = Field(ge=0, le=1)
    selection_hash: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def consistent(self):
        sequence_bytes(self.sequence)
        if len(self.sequence) != self.binder_length:
            raise ValueError("binder length does not match sequence")
        suffix = re.search(r"_rank(\d{2})$", self.full_name)
        if suffix is None or int(suffix.group(1)) != self.rank:
            raise ValueError("design rank does not match its source name")
        if ARM_BY_SOURCE.get((self.design_model, self.campaign)) != self.arm:
            raise ValueError("design arm does not match source model and campaign")
        if self.selection_hash != selection_hash(self.full_name):
            raise ValueError("selection hash does not match the public selection rule")
        agreement = (self.adaptyv_binding, self.twist_binding)
        expected_agreement = {
            ("binder", "binder"): "both_bind",
            ("not_tested", "binder"): "twist_only_tested",
            ("non_binder", "non_binder"): "neither_bind",
        }.get(agreement)
        if expected_agreement != self.vendor_agreement:
            raise ValueError("vendor agreement does not match the released assay calls")
        if (self.adaptyv_kd_nM is not None) != (self.adaptyv_binding == "binder"):
            raise ValueError("Adaptyv affinity does not match its assay call")
        if (self.twist_kd_nM is not None) != (self.twist_binding == "binder"):
            raise ValueError("Twist affinity does not match its assay call")
        if self.binder_final:
            if self.twist_binding != "binder" or self.kd_nM_final is None:
                raise ValueError("final binder lacks the released Twist call or affinity")
        elif not (
            self.adaptyv_binding == "non_binder"
            and self.twist_binding == "non_binder"
            and self.kd_nM_final is None
        ):
            raise ValueError("selected non-binder is not concordant across vendors")
        return self


class BenchBBPanel(BaseModel):
    """Compact, immutable import manifest for a label-enriched demonstration."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal["vat-benchbb-panel/1"] = Field(
        default="vat-benchbb-panel/1", alias="schema"
    )
    dataset_repository: Literal[DATASET_REPOSITORY] = DATASET_REPOSITORY
    dataset_revision: Literal[DATASET_REVISION] = DATASET_REVISION
    dataset_url: Literal[DATASET_URL] = DATASET_URL
    license: Literal["CC-BY-4.0"] = "CC-BY-4.0"
    source_files: list[SourceFile]
    target: Literal["BBF-14"] = "BBF-14"
    target_construct_id: Literal["bbf14_110"] = "bbf14_110"
    target_sequence: Literal[TARGET_SEQUENCE] = TARGET_SEQUENCE
    selection_seed: Literal[SELECTION_SEED] = SELECTION_SEED
    selection_method: Literal[
        "all final binders plus three concordant non-binders per arm by ascending seeded SHA-256"
    ] = "all final binders plus three concordant non-binders per arm by ascending seeded SHA-256"
    label_enriched: Literal[True] = True
    designs: list[BenchBBDesign] = Field(min_length=12, max_length=12)

    @model_validator(mode="after")
    def frozen_composition(self):
        if len({row.uuid for row in self.designs}) != 12 or len({row.full_name for row in self.designs}) != 12:
            raise ValueError("panel identifiers must be unique")
        binders = {row.full_name for row in self.designs if row.binder_final}
        if binders != EXPECTED_BINDERS:
            raise ValueError("panel does not contain the three predeclared binders")
        for arm in ARM_ORDER:
            rows = [row for row in self.designs if row.arm == arm]
            if len(rows) != 4 or sum(not row.binder_final for row in rows) != 3:
                raise ValueError("each arm must contain one binder and three non-binders")
        paths = {row.path: (row.bytes_len, row.sha256) for row in self.source_files}
        if len(paths) != len(self.source_files):
            raise ValueError("source file identities must be unique")
        if paths != PINNED_FILES:
            raise ValueError("source file identities do not match the pinned release")
        return self


class _SourceDesign(BaseModel):
    """Only source columns that may affect panel selection or checked-in evidence."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)
    uuid: str = Field(pattern=r"^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$")
    full_name: str = Field(pattern=r"^[a-z0-9_]+$")
    design_model: str
    campaign: str
    target: str
    rank: int
    binder_length: int
    sequence: str
    generator: str
    sequence_design_method: str
    adaptyv_binding: str
    adaptyv_kd_nM: float | None
    twist_binding: str
    twist_kd_nM: float | None
    binder_final: bool
    kd_nM_final: float | None
    vendor_agreement: str
    ipsae_min_ef2fast: float


def selection_hash(full_name: str) -> str:
    """Return the public deterministic order key used for non-binder sampling."""
    return hashlib.sha256(f"{SELECTION_SEED}:{full_name}".encode()).hexdigest()


def verify_source_files(root: Path) -> list[SourceFile]:
    """Hash source files and reject anything other than the pinned release bytes."""
    files = []
    for relative, (expected_length, expected_digest) in PINNED_FILES.items():
        path = root / relative
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if len(raw) != expected_length or digest != expected_digest:
            raise ValueError(f"pinned source mismatch: {relative}")
        files.append(SourceFile(path=relative, bytes_len=len(raw), sha256=digest))
    return files


def select_panel(rows: Iterable[dict], source_files: list[SourceFile]) -> BenchBBPanel:
    """Select the frozen panel from source rows without reading prediction scores."""
    bbf14 = [_SourceDesign.model_validate(row) for row in rows if row.get("target") == "BBF-14"]
    if len(bbf14) != 90:
        raise ValueError("pinned source must contain exactly 90 BBF-14 designs")
    if len({row.uuid for row in bbf14}) != 90 or len({row.full_name for row in bbf14}) != 90:
        raise ValueError("BBF-14 source identifiers must be unique")
    if any((row.design_model, row.campaign) not in ARM_BY_SOURCE for row in bbf14):
        raise ValueError("BBF-14 source contains an unexpected design arm")

    binders = [row for row in bbf14 if row.binder_final]
    if {row.full_name for row in binders} != EXPECTED_BINDERS:
        raise ValueError("released BBF-14 binder labels changed")

    chosen = sorted(binders, key=lambda row: ARM_ORDER.index(ARM_BY_SOURCE[(row.design_model, row.campaign)]))
    for arm in ARM_ORDER:
        eligible = [
            row
            for row in bbf14
            if ARM_BY_SOURCE[(row.design_model, row.campaign)] == arm
            and not row.binder_final
            and row.adaptyv_binding == "non_binder"
            and row.twist_binding == "non_binder"
        ]
        if len(eligible) < 3:
            raise ValueError(f"fewer than three concordant non-binders in {arm}")
        chosen.extend(sorted(eligible, key=lambda row: selection_hash(row.full_name))[:3])

    designs = [
        BenchBBDesign(
            **row.model_dump(exclude={"target"}),
            arm=ARM_BY_SOURCE[(row.design_model, row.campaign)],
            selection_hash=selection_hash(row.full_name),
        )
        for row in chosen
    ]
    return BenchBBPanel(source_files=source_files, designs=designs)


def build_pinned_panel(source_root: Path) -> BenchBBPanel:
    """Load and verify the two pinned Parquet inputs, then construct the panel."""
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - depends on optional operator environment
        raise RuntimeError("BenchBB source preparation requires the 'benchbb' extra") from exc

    files = verify_source_files(source_root)
    constructs = pq.read_table(
        source_root / "data/tables/insilico/target_constructs.parquet",
        columns=["target_construct_id", "target", "n_target_protein_chains", "target_chain_lengths",
                 "n_nucleic_chains", "n_designs", "stoichs", "target_chain_sequences"],
    ).to_pylist()
    target = [row for row in constructs if row["target_construct_id"] == "bbf14_110"]
    expected = {
        "target_construct_id": "bbf14_110",
        "target": "BBF-14",
        "n_target_protein_chains": 1,
        "target_chain_lengths": "110x1",
        "n_nucleic_chains": 0,
        "n_designs": 90,
        "stoichs": "1to1",
        "target_chain_sequences": f"{TARGET_SEQUENCE} x1",
    }
    if target != [expected]:
        raise ValueError("pinned BBF-14 target construct changed")

    columns = list(_SourceDesign.model_fields)
    rows = pq.read_table(source_root / "data/tables/design_summary.parquet", columns=columns).to_pylist()
    return select_panel(rows, files)


def panel_bytes(panel: BenchBBPanel) -> bytes:
    """Serialize a manifest deterministically for checking and evidence import."""
    return (panel.model_dump_json(indent=2, by_alias=True) + "\n").encode()
