"""Strict proposal and output contracts for the fixed generated CPU launcher."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError, field_validator, model_validator

from vatlog.canon import canonical_bytes

from .design_inputs import DesignError

DYNAMIC_VERSION = "vat-dynamic-job/1"
OUTPUT_VERSION = "vat-dynamic-output/1"
ENTRYPOINT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
ALLOWED_PACKAGES: frozenset[str] = frozenset()
MAX_SOURCE_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
SCHEMA_KEYS = frozenset({"type", "properties", "required", "additionalProperties", "items",
                         "minLength", "maxLength", "minimum", "maximum"})


def _validate_schema(schema: object, value: object, depth: int = 0) -> None:
    """Validate the small declared JSON-schema subset supported by the launcher."""
    if depth > 16 or not isinstance(schema, dict) or set(schema) - SCHEMA_KEYS:
        raise ValueError("unsupported input schema")
    kind = schema.get("type")
    matches = {"object": isinstance(value, dict), "array": isinstance(value, list),
               "string": isinstance(value, str), "integer": type(value) is int,
               "number": type(value) in {int, float}, "boolean": type(value) is bool,
               "null": value is None}
    if kind not in matches or not matches[kind]:
        raise ValueError("input does not match its declared type")
    allowed_by_type = {
        "object": {"type", "properties", "required", "additionalProperties"},
        "array": {"type", "items"},
        "string": {"type", "minLength", "maxLength"},
        "integer": {"type", "minimum", "maximum"},
        "number": {"type", "minimum", "maximum"},
        "boolean": {"type"}, "null": {"type"},
    }
    if set(schema) - allowed_by_type[kind]:
        raise ValueError("schema keyword does not apply to its type")
    if kind == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        additional = schema.get("additionalProperties", True)
        if (not isinstance(properties, dict) or not isinstance(required, list)
                or any(not isinstance(item, str) for item in required)
                or not set(required).issubset(properties) or type(additional) is not bool):
            raise ValueError("invalid object schema")
        if not set(required).issubset(value) or (not additional and set(value) - set(properties)):
            raise ValueError("input object differs from its schema")
        for name, item in value.items():
            if name in properties:
                _validate_schema(properties[name], item, depth + 1)
    elif kind == "array":
        if "items" not in schema:
            raise ValueError("array schema requires items")
        for item in value:
            _validate_schema(schema["items"], item, depth + 1)
    elif kind == "string":
        minimum, maximum = schema.get("minLength", 0), schema.get("maxLength", 1024 * 1024)
        if type(minimum) is not int or type(maximum) is not int or not minimum <= len(value) <= maximum:
            raise ValueError("string input differs from its schema")
    elif kind in {"integer", "number"}:
        minimum, maximum = schema.get("minimum"), schema.get("maximum")
        if (minimum is not None and (type(minimum) not in {int, float} or value < minimum)
                or maximum is not None and (type(maximum) not in {int, float} or value > maximum)):
            raise ValueError("numeric input differs from its schema")


class DynamicResources(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    cpu: Literal[1] = 1
    memory_mb: StrictInt = Field(default=512, ge=128, le=2048)
    timeout_seconds: StrictInt = Field(default=300, ge=1, le=300)
    gpu: None = None
    network: Literal[False] = False
    secrets: Literal[False] = False
    volumes: Literal[False] = False


class DynamicProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal["vat-dynamic-job/1"] = DYNAMIC_VERSION
    id: StrictStr
    label: StrictStr = Field(min_length=1, max_length=128)
    purpose: StrictStr = Field(min_length=1, max_length=2048)
    source: StrictStr = Field(min_length=1)
    entrypoint: StrictStr
    input_schema: dict[str, Any]
    input: Any
    packages: list[StrictStr] = Field(default_factory=list, max_length=16)
    output_version: Literal["vat-dynamic-output/1"] = OUTPUT_VERSION
    resources: DynamicResources = Field(default_factory=DynamicResources)
    public_input: Literal[True] = True
    authorized: StrictBool = False

    @field_validator("id")
    @classmethod
    def job_uuid(cls, value: str) -> str:
        return str(UUID(value))

    @field_validator("entrypoint")
    @classmethod
    def entrypoint_name(cls, value: str) -> str:
        if not ENTRYPOINT.fullmatch(value):
            raise ValueError("invalid entrypoint")
        return value

    @field_validator("source")
    @classmethod
    def bounded_source(cls, value: str) -> str:
        if len(value.encode("utf-8")) > MAX_SOURCE_BYTES or "\x00" in value:
            raise ValueError("source exceeds its boundary")
        compile(value, "approved_job.py", "exec")
        return value

    @field_validator("packages")
    @classmethod
    def allowed_packages(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(package not in ALLOWED_PACKAGES for package in value):
            raise ValueError("dynamic jobs permit the standard library only")
        return value

    @model_validator(mode="after")
    def bounded_json(self):
        if len(canonical_bytes(self.input_schema)) > 64 * 1024:
            raise ValueError("input schema exceeds its boundary")
        if len(canonical_bytes(self.input)) > 1024 * 1024:
            raise ValueError("input exceeds its boundary")
        if self.input_schema.get("type") != "object" or not isinstance(self.input, dict):
            raise ValueError("dynamic jobs require object JSON input")
        _validate_schema(self.input_schema, self.input)
        return self

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_bytes(self.model_dump(mode="json"))).hexdigest()


def validate_dynamic_proposal(value: object) -> DynamicProposal:
    try:
        return DynamicProposal.model_validate(value)
    except (ValidationError, ValueError, SyntaxError) as exc:
        raise DesignError("INVALID_DYNAMIC_JOB", "Invalid generated CPU job proposal.") from exc


def validate_dynamic_output(value: object) -> dict:
    """Accept one finite JSON result plus explicit, bounded artifact bytes."""
    if not isinstance(value, dict) or set(value) != {"version", "result", "artifacts"}:
        raise DesignError("OUTPUT_INVALID", "Generated job returned an invalid envelope.")
    if value["version"] != OUTPUT_VERSION or not isinstance(value["artifacts"], list):
        raise DesignError("OUTPUT_INVALID", "Generated job returned an invalid envelope.")
    try:
        if len(canonical_bytes(value["result"])) > MAX_OUTPUT_BYTES:
            raise ValueError("result too large")
    except (TypeError, ValueError, OverflowError) as exc:
        raise DesignError("OUTPUT_INVALID", "Generated job result is not bounded finite JSON.") from exc
    total, names, artifacts = 0, set(), []
    for artifact in value["artifacts"]:
        if (not isinstance(artifact, dict) or set(artifact) != {"name", "raw"}
                or not isinstance(artifact["name"], str) or not isinstance(artifact["raw"], bytes)):
            raise DesignError("OUTPUT_INVALID", "Generated artifact is malformed.")
        name, raw = artifact["name"], artifact["raw"]
        from vatlog.node import safe_artifact_name
        if not safe_artifact_name(name) or name in names:
            raise DesignError("OUTPUT_INVALID", "Generated artifact name is unsafe or repeated.")
        total += len(raw)
        if total > MAX_OUTPUT_BYTES or len(raw) > MAX_OUTPUT_BYTES:
            raise DesignError("OUTPUT_INVALID", "Generated artifacts exceed their byte limit.")
        names.add(name)
        artifacts.append({"name": name, "raw": raw})
    return {"version": OUTPUT_VERSION, "result": value["result"], "artifacts": artifacts}
