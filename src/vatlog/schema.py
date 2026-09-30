"""Typed record schema for the VAT protocol.

Field groups follow the nine-group table in RAND PE-A4613-1 (see
``reference/rand-vat-spec-extract.md``). RAND specifies the groups and the typing rules;
the concrete field names, the envelope, and the versioning are ours.

Typing rules honored here:
  - sequences are stored as SHA-256 digests with the bytes in a side blob store,
  - parameters are typed key-value pairs,
  - timestamps are ISO 8601 UTC,
  - every field is machine-parsable (nothing is free text that a verifier must read).
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "vat/0.1"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AssuranceTier(str, enum.Enum):
    """RAND's capture ladder, plus the tier this project claims.

    RAND enumerates three (``reference/rand-vat-spec-extract.md``, "Capture configurations").
    ``serverless_hosted`` is the unenumerated fourth this build exists to test: the
    designer submits code but does not hold root on the host, so the attack that bounds
    ``subprocess_wrapper`` (substituting outputs post-run, pre-finalization) is not
    available to them. It is still below ``tee_coexec``; see README "The claim being tested".
    """

    TEE_COEXEC = "tee_coexec"
    KERNEL_MONITOR = "kernel_monitor"
    SUBPROCESS_WRAPPER = "subprocess_wrapper"
    SERVERLESS_HOSTED = "serverless_hosted"


class RecordType(str, enum.Enum):
    SESSION_OPEN = "session_open"
    AGENT_CALL = "agent_call"
    DESIGN_RUN = "design_run"
    FUNCTION_CALL = "function_call"
    SESSION_SEAL = "session_seal"


# --- group 1: user and session context ---------------------------------------------

class UserSession(_Model):
    user_id: str = Field(description="public-key thumbprint of the designer")
    oidc_sub: str | None = Field(default=None, description="institutional OIDC subject")
    purpose_tag: str = Field(description='e.g. "therapeutic lead" vs "biochemistry class"')
    login_ip: str | None = None
    geo_coarse: str | None = Field(default=None, description="coarse GeoIP, country or region")


class DesignerStatement(_Model):
    """Designer-signed session identity and purpose, committed in the opening leaf."""

    session_uuid: str
    keyid: str
    purpose_tag: str
    issued_at: datetime
    signature: str = ""

    def signing_payload(self) -> bytes:
        """Canonical statement fields, excluding the signature itself."""
        from .canon import canonical_bytes

        return canonical_bytes(self.model_dump(mode="json", exclude={"signature"}))


# --- group 2: timestamps ------------------------------------------------------------

class Timestamps(_Model):
    stage_start: datetime
    stage_stop: datetime
    monotonic_ns: int
    clock_skew_ms: float = 0.0


# --- groups 3, 7, 8: artifacts ------------------------------------------------------

class Artifact(_Model):
    kind: Literal[
        "fasta", "genbank", "pdb_id", "prompt", "msa", "aa_sequence",
        "nt_sequence", "coordinates", "qc_report", "metric", "log",
        "transcript", "reasoning",
        "vat_node",
    ]
    name: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes_len: int


# --- group 4: design parameters -----------------------------------------------------

class ObjectiveTerm(_Model):
    """One term of the objective function actually optimized.

    Typed rather than free text because this is the field the reviewer reads to answer
    "what did this run optimize", and RAND's stated biosecurity value for the group is
    "detect covert optimization for toxicity or immune evasion".
    """

    name: str
    weight: float
    direction: Literal["min", "max"]


class DesignParams(_Model):
    hyperparams: dict[str, float | int | str] = Field(default_factory=dict)
    objectives: list[ObjectiveTerm] = Field(default_factory=list)
    seeds: list[int] = Field(default_factory=list)


# --- group 5: external data sources -------------------------------------------------

class DataSource(_Model):
    name: str
    release_or_date: str
    digest: str | None = None


# --- group 6: environment snapshot --------------------------------------------------

class SelfAsserted(_Model):
    """Fields the platform does not sign.

    On Modal these are read from the function's own configuration, so a designer who
    authored that configuration asserts them. Segregating them means a verifier can see
    at a glance which fields carry independent weight and which do not. See
    ``VISION.md``, "Assurance Model and Limits" (the attestation gap).
    """

    image_digest: str | None = None
    restrict_modal_access: bool | None = None
    block_network: bool | None = None
    single_use_containers: bool | None = None


class EnvSnapshot(_Model):
    assurance_tier: AssuranceTier
    python: str
    platform: str
    cuda: str | None = None
    gpu_model: str | None = None
    sbom_sha256: str | None = None
    modal_call_id: str | None = Field(
        default=None, description="modal.current_function_call_id(), platform-issued"
    )
    self_asserted: SelfAsserted = Field(default_factory=SelfAsserted)


# --- group 9: hash-linking metadata -------------------------------------------------

GENESIS_PREV = "00" * 32


class HashLink(_Model):
    sha_prev: str = Field(pattern=r"^[0-9a-f]{64}$")
    leaf_index: int
    anchor_id: str | None = Field(
        default=None, description="external anchor (Rekor entry id); unused, local log only"
    )


# --- the envelope -------------------------------------------------------------------

class VatRecord(_Model):
    schema_version: str = SCHEMA_VERSION
    record_type: RecordType
    session_uuid: str
    seq_no: int

    user_session: UserSession
    designer_statement: DesignerStatement | None = None
    timestamps: Timestamps
    inputs: list[Artifact] = Field(default_factory=list)
    params: DesignParams = Field(default_factory=DesignParams)
    external: list[DataSource] = Field(default_factory=list)
    environment: EnvSnapshot
    intermediates: list[Artifact] = Field(default_factory=list)
    results: list[Artifact] = Field(default_factory=list)
    link: HashLink
