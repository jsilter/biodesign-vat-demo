"""One versioned request envelope with bounded family-specific payloads."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import (BaseModel, ConfigDict, Field, StrictFloat, StrictInt, StrictStr,
                      ValidationError, field_validator, model_validator)

from .design_inputs import DesignError, ProteinMPNNRequest, sequence_bytes
from .tool_catalog import ToolSpec, tool_spec

REQUEST_VERSION = "protein-tool/1"
CHAIN_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class FrozenPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GenerationPayload(FrozenPayload):
    target_cif: StrictStr = Field(min_length=1, max_length=10 * 1024 * 1024)
    target_chains: list[StrictStr] = Field(min_length=1, max_length=26)
    target_sequences: dict[StrictStr, StrictStr] = Field(min_length=1, max_length=26)
    count: StrictInt = Field(default=1, ge=1, le=100)
    seed: StrictInt = Field(ge=1, le=2**31 - 1)
    min_length: StrictInt = Field(default=60, ge=10, le=500)
    max_length: StrictInt = Field(default=100, ge=10, le=500)
    hotspots: list[StrictInt] = Field(default_factory=list, max_length=256)
    template_cif: StrictStr | None = Field(default=None, min_length=1, max_length=10 * 1024 * 1024)

    @field_validator("target_chains")
    @classmethod
    def chain_names(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(not CHAIN_ID.fullmatch(chain) for chain in value):
            raise ValueError("target chains must be unique nonempty identifiers")
        return value

    @model_validator(mode="after")
    def ordered(self):
        if self.min_length > self.max_length:
            raise ValueError("binder length range is reversed")
        if len(set(self.hotspots)) != len(self.hotspots) or any(index < 1 for index in self.hotspots):
            raise ValueError("hotspots must be unique positive prepared residue indices")
        if set(self.target_chains) != set(self.target_sequences):
            raise ValueError("target chains and target sequences disagree")
        for sequence in self.target_sequences.values():
            sequence_bytes(sequence)
        return self


class SequencePayload(FrozenPayload):
    backbone_cif: StrictStr = Field(min_length=1, max_length=10 * 1024 * 1024)
    sequences: dict[StrictStr, StrictStr] = Field(min_length=1, max_length=26)
    design_chains: list[StrictStr] = Field(min_length=1, max_length=26)
    count: StrictInt = Field(default=1, ge=1, le=100)
    seed: StrictInt = Field(ge=1, le=2**31 - 1)
    sampling_temperature: StrictFloat = Field(default=0.1, ge=0.01, le=1.0)
    conformers_cif: list[StrictStr] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def aligned(self):
        if (len(set(self.design_chains)) != len(self.design_chains)
                or not set(self.design_chains).issubset(self.sequences)):
            raise ValueError("design chains must be unique members of sequences")
        for chain, sequence in self.sequences.items():
            if not CHAIN_ID.fullmatch(chain):
                raise ValueError("sequence chain identifier is invalid")
            sequence_bytes(sequence)
        if sum(map(len, self.sequences.values())) > 4096:
            raise ValueError("sequence payload exceeds 4096 residues")
        return self


class TrackPayload(FrozenPayload):
    sequence: StrictStr = Field(min_length=1, max_length=2048)
    task: Literal["score", "generate_sequence", "generate_structure"] = "score"
    count: StrictInt = Field(default=1, ge=1, le=32)
    seed: StrictInt = Field(ge=1, le=2**31 - 1)
    temperature: StrictFloat = Field(default=1.0, gt=0, le=2.0)

    @field_validator("sequence")
    @classmethod
    def canonical_or_masked(cls, value: str) -> str:
        if any(c not in "ACDEFGHIKLMNPQRSTVWY_" for c in value):
            raise ValueError("sequence track contains unsupported tokens")
        return value


class PredictionPayload(FrozenPayload):
    sequences: dict[StrictStr, StrictStr] = Field(min_length=1, max_length=26)
    query_chain: StrictStr | None = Field(default=None, min_length=1, max_length=128)
    msa_mode: Literal["none", "provided"] = "none"
    msa_a3m: StrictStr | None = Field(default=None, max_length=20 * 1024 * 1024)
    template_policy: Literal["none", "provided"] = "none"
    templates_cif: list[StrictStr] = Field(default_factory=list, max_length=32)
    count: StrictInt = Field(default=1, ge=1, le=32)
    seed: StrictInt = Field(ge=0, le=2**31 - 1)

    @model_validator(mode="after")
    def coherent_features(self):
        for chain, sequence in self.sequences.items():
            if not CHAIN_ID.fullmatch(chain):
                raise ValueError("sequence chain identifier is invalid")
            sequence_bytes(sequence)
        if sum(map(len, self.sequences.values())) > 4096:
            raise ValueError("prediction payload exceeds 4096 residues")
        if (self.msa_mode == "provided") != (self.msa_a3m is not None):
            raise ValueError("provided MSA mode requires exactly one staged A3M document")
        if (self.template_policy == "provided") != bool(self.templates_cif):
            raise ValueError("provided template policy requires template structures")
        return self


class UtilityPayload(FrozenPayload):
    structures: list[StrictStr] = Field(default_factory=list, max_length=2)
    sequences: list[StrictStr] = Field(default_factory=list, max_length=1024)
    pae: list[list[StrictFloat | StrictInt]] | None = Field(default=None, max_length=4096)
    chain_map: dict[StrictStr, StrictStr] = Field(default_factory=dict, max_length=52)
    database: StrictStr | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,128}$")
    max_hits: StrictInt = Field(default=100, ge=1, le=1000)
    seed: StrictInt | None = Field(default=None, ge=1, le=2**31 - 1)
    pae_cutoff: StrictFloat = Field(default=10.0, gt=0, le=50)
    distance_cutoff: StrictFloat = Field(default=10.0, gt=0, le=50)

    @model_validator(mode="after")
    def bounded(self):
        if any(not structure or len(structure) > 10 * 1024 * 1024 for structure in self.structures):
            raise ValueError("utility structures must be nonempty and at most 10 MiB each")
        for sequence in self.sequences:
            sequence_bytes(sequence)
        if sum(map(len, self.sequences)) > 100_000:
            raise ValueError("utility query payload exceeds 100,000 residues")
        if self.pae is not None:
            size = len(self.pae)
            if not size or any(len(row) != size for row in self.pae):
                raise ValueError("PAE must be a nonempty square matrix")
            if any(type(value) not in {int, float} or not 0 <= value <= 100
                   for row in self.pae for value in row):
                raise ValueError("PAE entries must be finite values from 0 to 100")
        return self


class ToolRequest(BaseModel):
    """Transport-stable request shared by every standalone tool."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal["protein-tool/1"] = REQUEST_VERSION
    tool: StrictStr
    operation: StrictStr
    payload: dict[str, Any]


def _payload_model(spec: ToolSpec):
    if spec.family == "generation":
        return GenerationPayload
    if spec.family == "prediction":
        return PredictionPayload
    if spec.family == "sequence":
        if spec.id in {"proteinmpnn", "proteinmpnn-soluble"}:
            return ProteinMPNNRequest
        return TrackPayload if spec.id in {"esmc", "esm3"} else SequencePayload
    return UtilityPayload


def validate_tool_request(value: object, *, expected_tool: str | None = None) -> dict:
    """Validate an envelope against catalog identity and return canonical JSON-ready data."""
    try:
        request = ToolRequest.model_validate(value)
        spec = tool_spec(request.tool)
        if expected_tool is not None and request.tool != expected_tool:
            raise ValueError("request tool differs from endpoint identity")
        if request.operation != spec.operation:
            raise ValueError("request operation differs from catalog contract")
        payload = _payload_model(spec).model_validate(request.payload)
        if spec.id == "esmfold2":
            if payload.query_chain not in payload.sequences or len(payload.sequences) not in {1, 2}:
                raise ValueError("ESMFold2 requires a query chain and at most one target chain")
        if spec.family == "prediction" and spec.id != "esmfold2" and payload.query_chain is not None:
            raise ValueError(f"{spec.id} does not use a query-chain role")
        if spec.family == "prediction":
            if spec.id == "esmfold2-full":
                if payload.template_policy != "none":
                    raise ValueError("ESMFold2 Full adapter does not accept templates")
            elif spec.id == "af-unmasked":
                if (payload.msa_mode != "none" or payload.template_policy != "provided"
                        or len(payload.templates_cif) != payload.count or payload.seed != 0):
                    raise ValueError("AF_unmasked requires one supplied template per requested result and no MSA")
            elif payload.msa_mode != "none" or payload.template_policy != "none":
                raise ValueError(f"{spec.id} currently accepts sequence-only features")
        if spec.id in {"dssp", "ipsae"} and len(payload.structures) != 1:
            raise ValueError(f"{spec.id} requires exactly one structure")
        if spec.id in {"tmalign", "dockq"} and len(payload.structures) != 2:
            raise ValueError(f"{spec.id} requires exactly two structures")
        if spec.id == "ipsae" and payload.pae is None:
            raise ValueError("ipSAE requires an explicit PAE matrix")
        if spec.id == "ipsae" and (not payload.pae_cutoff.is_integer()
                                    or not payload.distance_cutoff.is_integer()):
            raise ValueError("ipSAE currently requires integer cutoffs")
        if spec.id == "dockq" and not payload.chain_map:
            raise ValueError("DockQ requires an explicit model-to-reference chain mapping")
        if spec.id == "dockq" and (any(len(key) != 1 or len(mapped) != 1
                                       for key, mapped in payload.chain_map.items())
                                    or len(set(payload.chain_map.values())) != len(payload.chain_map)):
            raise ValueError("DockQ requires a one-to-one mapping of one-character chain IDs")
        if spec.id in {"mmseqs2", "foldseek"} and payload.database is None:
            raise ValueError(f"{spec.id} requires an explicit staged database identity")
        if spec.id == "mmseqs2" and (not payload.sequences or payload.structures):
            raise ValueError("mmseqs2 requires query sequences and no structures")
        if spec.id == "foldseek" and (len(payload.structures) != 1 or payload.sequences):
            raise ValueError("foldseek requires exactly one query structure and no sequences")
        if spec.id == "esm3":
            if payload.task == "score":
                raise ValueError("esm3 exposes generation only")
            if payload.task == "generate_sequence" and "_" not in payload.sequence:
                raise ValueError("esm3 sequence generation requires a masked residue")
            if payload.task == "generate_structure" and "_" in payload.sequence:
                raise ValueError("esm3 structure generation requires a complete sequence")
        if spec.family == "generation":
            if len(payload.target_chains) != 1:
                raise ValueError("current generation adapters require exactly one prepared target chain")
            if len(payload.target_chains[0]) != 1:
                raise ValueError("current generation adapters require a one-character target chain")
            if payload.template_cif is not None:
                raise ValueError("current generation adapters do not accept a binder template")
            target_length = len(payload.target_sequences[payload.target_chains[0]])
            if any(index > target_length for index in payload.hotspots):
                raise ValueError("generation hotspot lies outside the prepared target chain")
            if spec.id in {"pxdesign", "mosaic"} and payload.min_length != payload.max_length:
                raise ValueError(f"{spec.id} requires one fixed binder length")
            if spec.id == "mosaic" and payload.count != 1:
                raise ValueError("mosaic exposes one design per immutable operation")
    except (ValidationError, ValueError) as exc:
        raise DesignError("INVALID_REQUEST", "Invalid scientific tool request.") from exc
    return {"version": request.version, "tool": request.tool, "operation": request.operation,
            "payload": payload.model_dump(mode="json", exclude_none=True)}


def tool_request(tool: str, payload: object) -> dict:
    """Build the one public request shape from an already prepared family payload."""
    try:
        spec = tool_spec(tool)
    except ValueError as exc:
        raise DesignError("INVALID_REQUEST", "Unknown scientific tool.") from exc
    return validate_tool_request({"version": REQUEST_VERSION, "tool": tool,
                                  "operation": spec.operation, "payload": payload},
                                 expected_tool=tool)


def worker_request(envelope: object, *, expected_tool: str) -> dict:
    """Map the common envelope to the BoltzGen, ProteinMPNN, ESMC and ESMFold2 worker inputs."""
    request = validate_tool_request(envelope, expected_tool=expected_tool)
    payload = request["payload"]
    if expected_tool == "boltzgen":
        if len(payload["target_sequences"]) != 1:
            raise DesignError("INVALID_REQUEST", "BoltzGen currently accepts one prepared target chain.")
        return {"target_cif": payload["target_cif"],
                "target_sequence": next(iter(payload["target_sequences"].values())),
                "count": payload["count"], "seed": payload["seed"],
                "min_length": payload["min_length"], "max_length": payload["max_length"],
                "binding_residues": payload["hotspots"]}
    if expected_tool == "proteinmpnn":
        return payload
    if expected_tool == "esmc":
        if payload["task"] != "score" or "_" in payload["sequence"]:
            raise DesignError("INVALID_REQUEST", "ESMC supports scoring one complete canonical sequence.")
        return {"sequence": payload["sequence"]}
    if expected_tool == "esmfold2":
        query = payload["query_chain"]
        others = [sequence for chain, sequence in payload["sequences"].items() if chain != query]
        return {"sequence": payload["sequences"][query], "mode": "complex" if others else "binder",
                "target_sequence": others[0] if others else "", "seed": payload["seed"],
                "num_loops": 10, "num_sampling_steps": 68, "num_diffusion_samples": payload["count"]}
    raise DesignError("INVALID_REQUEST", "No worker adapter exists for this tool.")
