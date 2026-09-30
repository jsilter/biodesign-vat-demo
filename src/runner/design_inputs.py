"""Validated protein-only inputs and precisely defined sequence metrics."""

from __future__ import annotations

import math
import hashlib
from pathlib import Path
from vatlog.node import safe_artifact_name
from typing import Literal
from pydantic import (BaseModel, ConfigDict, Field, StrictFloat, StrictInt, StrictBytes,
                      StrictStr, ValidationError, field_validator, model_validator)

AMINO_ACIDS = frozenset("ACDEFGHIKLMNPQRSTVWY")


class RemoteArtifact(BaseModel):
    """Scientific output bytes with a safe server-owned download name."""
    model_config = ConfigDict(extra="forbid")
    name: StrictStr
    kind: Literal["coordinates", "metric", "log", "aa_sequence", "fasta", "msa"]
    raw: StrictBytes
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    bytes_len: StrictInt | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def safe_name(self):
        if not safe_artifact_name(self.name):
            raise ValueError("unsafe output filename")
        if self.sha256 is not None and hashlib.sha256(self.raw).hexdigest() != self.sha256:
            raise ValueError("output digest mismatch")
        if self.bytes_len is not None and len(self.raw) != self.bytes_len:
            raise ValueError("output length mismatch")
        return self


class RemoteCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: StrictStr = Field(min_length=1, pattern=r"^[A-Za-z0-9_.-]+$")
    sequence: StrictStr = Field(min_length=1, pattern=r"^[ACDEFGHIKLMNPQRSTVWY]+$")
    rank: StrictInt | None = Field(default=None, ge=0)
    sample: StrictInt | None = Field(default=None, ge=1)
    chain: StrictStr | None = Field(default=None, pattern=r"^[A-Z]$")
    metrics: dict[str, StrictFloat | StrictInt] = Field(default_factory=dict)


class RemoteResult(BaseModel):
    """Reject malformed output envelopes before storing a successful attempt."""
    model_config = ConfigDict(extra="forbid")
    artifacts: list[RemoteArtifact]
    candidates: list[RemoteCandidate] = Field(default_factory=list)
    metrics: dict = Field(default_factory=dict)
    execution: dict = Field(default_factory=dict)
    error: Literal["EXECUTION_FAILED", "OUTPUT_INVALID", "TOOL_UNAVAILABLE", "DEPENDENCY_MISMATCH", "CANCELLED"] | None = None


def checked_result(value: object) -> dict:
    """Validate a remote scientific envelope without coercing text into artifact bytes."""
    try:
        result = RemoteResult.model_validate(value)
    except ValidationError as exc:
        raise DesignError("OUTPUT_INVALID", "Malformed scientific result envelope.") from exc
    return result.model_dump(exclude_none=True)


def validate_fold_metrics(metrics: dict, length: int, *, complex: bool) -> None:
    """Require raw per-residue confidence and consistent supported summary scales."""
    values = metrics.get("plddt")
    if (metrics.get("plddt_raw_scale") != "0–1" or not isinstance(values, list) or len(values) != length
            or any(type(v) not in {int, float} or not math.isfinite(v) or not 0 <= v <= 1 for v in values)):
        raise DesignError("OUTPUT_INVALID", "Invalid folding confidence dimensions or scale.")
    mean = metrics.get("mean_plddt")
    if type(mean) not in {int, float} or not math.isclose(mean, math.fsum(values)/length, abs_tol=1e-8):
        raise DesignError("OUTPUT_INVALID", "Folding mean confidence does not match its position scores.")
    for key in ["ptm", "iptm"]:
        value = metrics.get(key)
        if value is not None and (type(value) not in {int, float} or not math.isfinite(value) or not 0 <= value <= 1):
            raise DesignError("OUTPUT_INVALID", "Invalid global folding confidence.")
    if not complex and metrics.get("iptm") is not None:
        raise DesignError("OUTPUT_INVALID", "Binder-alone predictions must not contain interface confidence.")
    if "pae" in metrics:
        rows = metrics["pae"]
        if (not isinstance(rows, list) or len(rows) != length
                or any(not isinstance(row, list) or len(row) != length
                       or any(type(v) not in {int, float} or not math.isfinite(v) or v < 0 for v in row) for row in rows)):
            raise DesignError("OUTPUT_INVALID", "Invalid predicted aligned-error matrix.")


class DesignError(ValueError):
    """Safe design-boundary error with a stable user-visible code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class DesignSettings(BaseModel):
    """User-selected generation settings; frozen once submitted."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    count: StrictInt = Field(default=4, ge=1, le=100)
    min_length: StrictInt = Field(default=60, ge=10, le=500)
    max_length: StrictInt = Field(default=100, ge=10, le=500)
    seed: StrictInt = Field(default=0, ge=0, le=2**31 - 1)
    binding_residues: list[StrictInt] = Field(default_factory=list)

    @model_validator(mode="after")
    def ordered(self):
        if self.min_length > self.max_length:
            raise ValueError("binder length range is reversed")
        if len(set(self.binding_residues)) != len(self.binding_residues):
            raise ValueError("binding residues must be unique")
        return self


class ProteinMPNNSettings(BaseModel):
    """Bounded, deterministic standalone inverse-folding settings."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    count: StrictInt = Field(default=4, ge=1, le=100)
    sampling_temperature: StrictFloat = Field(default=0.1, ge=0.01, le=0.5)
    seed: StrictInt = Field(default=1, ge=1, le=2**31 - 1)
    model: Literal["v_48_020"] = "v_48_020"
    design_chains: list[StrictStr] = Field(min_length=1, max_length=26)

    @field_validator("design_chains")
    @classmethod
    def prepared_chains(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(len(chain) != 1 or chain not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" for chain in value):
            raise ValueError("design chains must be unique prepared IDs A–Z")
        return value


class BackboneChainMap(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    original_chain: StrictStr = Field(min_length=1, max_length=128)
    prepared_chain: StrictStr = Field(pattern=r"^[A-Z]$")


class BackboneResidueMap(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    prepared_chain: StrictStr = Field(pattern=r"^[A-Z]$")
    prepared_index: StrictInt = Field(ge=1)
    original_chain: StrictStr = Field(min_length=1, max_length=128)
    original_residue: StrictStr = Field(min_length=1, max_length=64)
    residue_name: StrictStr = Field(min_length=3, max_length=3)


class ProteinMPNNRequest(ProteinMPNNSettings):
    """Complete remote request after server-owned backbone preparation."""

    backbone_cif: StrictStr = Field(min_length=1, max_length=10 * 1024 * 1024)
    sequences: dict[StrictStr, StrictStr] = Field(min_length=1, max_length=26)
    chain_map: list[BackboneChainMap] = Field(min_length=1, max_length=26)
    mapping: list[BackboneResidueMap] = Field(min_length=1, max_length=2048)

    @model_validator(mode="after")
    def matches_preparation(self):
        prepared = [entry.prepared_chain for entry in self.chain_map]
        original = [entry.original_chain for entry in self.chain_map]
        if (len(set(prepared)) != len(prepared) or len(set(original)) != len(original)
                or set(prepared) != set(self.sequences)):
            raise ValueError("chain map and sequences disagree")
        for sequence in self.sequences.values():
            sequence_bytes(sequence)
        if not set(self.design_chains).issubset(self.sequences):
            raise ValueError("design chain is absent from prepared backbone")
        expected = {(chain, index) for chain, sequence in self.sequences.items()
                    for index in range(1, len(sequence) + 1)}
        observed = {(entry.prepared_chain, entry.prepared_index) for entry in self.mapping}
        if observed != expected or len(observed) != len(self.mapping):
            raise ValueError("residue map and sequences disagree")
        source_by_prepared = {entry.prepared_chain: entry.original_chain for entry in self.chain_map}
        source_residues = {(entry.original_chain, entry.original_residue) for entry in self.mapping}
        if (len(source_residues) != len(self.mapping)
                or any(entry.original_chain != source_by_prepared[entry.prepared_chain]
                       or gemmi_code(entry.residue_name) != self.sequences[entry.prepared_chain][entry.prepared_index - 1]
                       for entry in self.mapping)):
            raise ValueError("residue provenance does not match chain map and sequences")
        return self


def gemmi_code(residue_name: str) -> str:
    """Map one strict three-letter ordinary residue name without importing Gemmi at model import."""
    names = {"ALA": "A", "CYS": "C", "ASP": "D", "GLU": "E", "PHE": "F", "GLY": "G",
             "HIS": "H", "ILE": "I", "LYS": "K", "LEU": "L", "MET": "M", "ASN": "N",
             "PRO": "P", "GLN": "Q", "ARG": "R", "SER": "S", "THR": "T", "VAL": "V",
             "TRP": "W", "TYR": "Y"}
    return names.get(residue_name, "")


def sequence_bytes(sequence: str) -> bytes:
    """Require an already canonical, nonempty amino-acid sequence."""
    if not isinstance(sequence, str) or not sequence or any(c not in AMINO_ACIDS for c in sequence):
        raise DesignError("OUTPUT_INVALID", "Expected uppercase canonical amino acids without whitespace.")
    return sequence.encode("ascii")


def masked_metrics(log_probabilities: list[float]) -> dict:
    """Summarize one natural-log masked probability per amino-acid position."""
    if not log_probabilities or any(type(p) not in {int, float} or not math.isfinite(p) or p > 0 for p in log_probabilities):
        raise DesignError("OUTPUT_INVALID", "Invalid masked log probabilities.")
    mean = math.fsum(log_probabilities) / len(log_probabilities)
    try:
        perplexity = math.exp(-mean)
    except OverflowError as exc:
        raise DesignError("OUTPUT_INVALID", "Sequence pseudo-perplexity overflowed.") from exc
    return {"definition": "masked-pll/full-vocabulary/natural-log/1", "length": len(log_probabilities),
            "per_residue_log_probability": log_probabilities,
            "mean_masked_log_probability": mean, "pseudo_perplexity": perplexity}


def full_vocabulary_log_probability(logits: list[float], token: int) -> float:
    """Stable log-softmax for one original residue over every model output token."""
    if not logits or not 0 <= token < len(logits) or any(not math.isfinite(v) for v in logits):
        raise DesignError("OUTPUT_INVALID", "Invalid sequence-model logits or token identity.")
    maximum = max(logits)
    return (logits[token] - maximum) - math.log(math.fsum(math.exp(v-maximum) for v in logits))


def prepare_target(raw: bytes, format: str, chain: str, *, max_residues: int = 2048) -> dict:
    """Parse one ordinary protein chain; never silently repair chemistry or coordinates."""
    import gemmi

    if format not in {"pdb", "cif"} or not raw or len(raw) > 10 * 1024 * 1024:
        raise DesignError("INVALID_TARGET", "Upload a PDB or mmCIF structure up to 10 MiB.")
    try:
        text = raw.decode("utf-8")
        structure = (gemmi.read_pdb_string(text) if format == "pdb"
                     else gemmi.make_structure_from_block(gemmi.cif.read_string(text).sole_block()))
    except (ValueError, RuntimeError, UnicodeError) as exc:
        raise DesignError("INVALID_TARGET", "Could not parse the supplied structure.") from exc
    if len(structure) != 1:
        raise DesignError("INVALID_TARGET", "Supply a structure with exactly one coordinate model.")
    matches = [c for c in structure[0] if c.name == chain]
    if len(matches) != 1:
        raise DesignError("INVALID_TARGET", "Select one unambiguous existing chain.")
    selected = matches[0]
    residues, mapping = [], []
    identities = set()
    for residue in selected:
        info = gemmi.find_tabulated_residue(residue.name)
        aa = info.one_letter_code
        if not info.is_amino_acid() or aa not in AMINO_ACIDS or residue.het_flag != "A":
            raise DesignError("UNSUPPORTED_CHEMISTRY", "The selected chain must contain only ordinary amino acids; prepare modifications explicitly.")
        if not residue or not any(a.name == "CA" for a in residue):
            raise DesignError("INVALID_TARGET", "Each modeled residue must have a C-alpha coordinate.")
        seen = set()
        for atom in residue:
            if atom.has_altloc() or atom.name in seen or not all(math.isfinite(v) for v in (atom.pos.x, atom.pos.y, atom.pos.z)):
                raise DesignError("INVALID_TARGET", "Resolve alternate/duplicate atoms and invalid coordinates before upload.")
            seen.add(atom.name)
        identity = str(residue.seqid)
        if identity in identities:
            raise DesignError("INVALID_TARGET", "Duplicate residue identifier.")
        identities.add(identity)
        residues.append(aa)
        mapping.append({"index": len(residues), "original_chain": chain, "original_residue": identity,
                        "residue_name": residue.name})
    if not 1 <= len(residues) <= max_residues:
        raise DesignError("INVALID_TARGET", f"Target must contain 1–{max_residues} modeled residues.")
    # Prepared chain uses predictable canonical positions, with mapping retained separately.
    prepared = gemmi.Structure()
    model = gemmi.Model("1")
    target = gemmi.Chain("A")
    for index, residue in enumerate(selected, 1):
        clone = residue.clone()
        clone.seqid = gemmi.SeqId(index, " ")
        clone.subchain = "A"
        target.add_residue(clone)
    model.add_chain(target)
    prepared.add_model(model)
    prepared.setup_entities()
    return {"sequence": "".join(residues), "chain": "A", "mapping": mapping,
            "prepared_cif": prepared.make_mmcif_document().as_string(),
            "limits": "Modeled residues only; no missing-residue reconstruction or biological safety screen."}


def prepare_backbone(raw: bytes, format: str, *, max_residues: int = 2048) -> dict:
    """Canonicalize a multi-chain ordinary-protein backbone for standalone inverse folding."""
    import gemmi

    if format not in {"pdb", "cif"} or not raw or len(raw) > 10 * 1024 * 1024:
        raise DesignError("INVALID_TARGET", "Upload a PDB or mmCIF backbone up to 10 MiB.")
    try:
        text = raw.decode("utf-8")
        structure = (gemmi.read_pdb_string(text) if format == "pdb"
                     else gemmi.make_structure_from_block(gemmi.cif.read_string(text).sole_block()))
    except (ValueError, RuntimeError, UnicodeError) as exc:
        raise DesignError("INVALID_TARGET", "Could not parse the supplied backbone.") from exc
    if len(structure) != 1:
        raise DesignError("INVALID_TARGET", "Supply a backbone with exactly one coordinate model.")
    source_chains = list(structure[0])
    if not 1 <= len(source_chains) <= 26 or len({chain.name for chain in source_chains}) != len(source_chains):
        raise DesignError("INVALID_TARGET", "Backbone must have 1–26 distinctly named protein chains.")

    prepared = gemmi.Structure()
    model = gemmi.Model("1")
    chain_map, residue_map, sequences = [], [], {}
    total = 0
    for chain_index, source in enumerate(source_chains):
        if not source.name or not source:
            raise DesignError("INVALID_TARGET", "Backbone chains must be named and nonempty.")
        prepared_id = chr(ord("A") + chain_index)
        target = gemmi.Chain(prepared_id)
        sequence, identities = [], set()
        for residue_index, residue in enumerate(source, 1):
            info = gemmi.find_tabulated_residue(residue.name)
            aa = info.one_letter_code
            if not info.is_amino_acid() or aa not in AMINO_ACIDS or residue.het_flag != "A":
                raise DesignError("UNSUPPORTED_CHEMISTRY", "Backbones must contain only ordinary amino acids.")
            identity = str(residue.seqid)
            if identity in identities:
                raise DesignError("INVALID_TARGET", "Duplicate residue identifier in backbone chain.")
            identities.add(identity)
            seen = set()
            for atom in residue:
                if (atom.has_altloc() or atom.name in seen
                        or not all(math.isfinite(v) for v in (atom.pos.x, atom.pos.y, atom.pos.z))):
                    raise DesignError("INVALID_TARGET", "Resolve alternate/duplicate atoms and invalid coordinates before upload.")
                seen.add(atom.name)
            if any(sum(atom.name == required for atom in residue) != 1 for required in ("N", "CA", "C", "O")):
                raise DesignError("INVALID_TARGET", "Every residue requires exactly one N, CA, C and O backbone atom.")
            clone = residue.clone()
            clone.seqid = gemmi.SeqId(residue_index, " ")
            clone.subchain = prepared_id
            target.add_residue(clone)
            sequence.append(aa)
            total += 1
            residue_map.append({"prepared_chain": prepared_id, "prepared_index": residue_index,
                                "original_chain": source.name, "original_residue": identity,
                                "residue_name": residue.name})
        if not sequence:
            raise DesignError("INVALID_TARGET", "Backbone chains must contain modeled residues.")
        model.add_chain(target)
        chain_map.append({"original_chain": source.name, "prepared_chain": prepared_id})
        sequences[prepared_id] = "".join(sequence)
    if not 1 <= total <= max_residues:
        raise DesignError("INVALID_TARGET", f"Backbone must contain 1–{max_residues} modeled residues.")
    prepared.add_model(model)
    prepared.setup_entities()
    return {"sequences": sequences, "chain_map": chain_map, "mapping": residue_map,
            "prepared_cif": prepared.make_mmcif_document().as_string(),
            "prepared_pdb": prepared.make_pdb_string(),
            "limits": "Modeled ordinary residues only; no missing-atom repair or biological safety screen."}
