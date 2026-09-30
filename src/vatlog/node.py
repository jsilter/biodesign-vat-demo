"""Placement-independent Basic VAT execution nodes and session append boundary."""

from __future__ import annotations

import base64
import enum
import math
import os
import tempfile
from binascii import Error as Base64Error
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import (BaseModel, ConfigDict, Field, StrictInt, StrictStr,
                      ValidationError, model_validator)

from .attest import pae
from .canon import canonical_bytes, parse_json, sha256_hex
from .roles import LocalSigner, Role, TrustBundle, UntrustedKeyError, signer_for
from .schema import (Artifact, AssuranceTier, DataSource, DesignParams, EnvSnapshot,
                     GENESIS_PREV, HashLink, RecordType, Timestamps, UserSession, VatRecord)
from .store import SessionLog
from .provenance import InputBinding, ControlReference, validate_destinations

NODE_FORMAT = "vat-node/1"
NODE_PAYLOAD_TYPE = "application/vnd.biodesign-vat.execution-node+json"
ASSURANCE_PROFILE = "basic-local-recorder/1"
UNCAPTURED = "filesystem, subprocess, logs, network, and external side effects are not captured"
MAX_SAFE_INTEGER = 2**53 - 1


@dataclass(frozen=True)
class VatLimits:
    """Resource limits for canonical values and portable nodes."""

    max_depth: int = 64
    max_items: int = 100_000
    max_value_bytes: int = 8 * 1024**2
    max_blob_bytes: int = 64 * 1024**2
    max_total_blob_bytes: int = 64 * 1024**2
    max_artifacts: int = 256
    max_node_bytes: int = 128 * 1024**2

    def __post_init__(self) -> None:
        if any(type(value) is not int or value <= 0 for value in vars(self).values()):
            raise ValueError("VAT node limits must be positive integers")


class NodeError(ValueError):
    """A stable failure at the node/value evidence boundary."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class NodeStatus(str, enum.Enum):
    SUCCEEDED = "succeeded"
    FUNCTION_ERROR = "function_error"
    EXECUTION_ERROR = "execution_error"
    CANCELLED = "cancelled"


class NodeOutcome(str, enum.Enum):
    OK = "OK"
    TAMPERED = "TAMPERED"
    BAD_SIGNATURE = "BAD_SIGNATURE"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FunctionIdentity(_Model):
    model_config = ConfigDict(extra="forbid", frozen=True)

    app_name: StrictStr = Field(min_length=1, max_length=64)
    function_name: StrictStr = Field(min_length=1, max_length=128)
    capture_version: Literal["caller-observation/1"] = "caller-observation/1"
    codec_version: Literal["vat-json/1"] = "vat-json/1"
    module: StrictStr | None = Field(default=None, min_length=1, max_length=512)
    qualname: StrictStr | None = Field(default=None, min_length=1, max_length=512)
    source_sha256: StrictStr | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class DomainOutcome(_Model):
    status: Literal["succeeded", "failed", "rejected"]
    code: StrictStr | None = Field(default=None, max_length=128)


class AdapterIdentity(_Model):
    name: StrictStr = Field(min_length=1, max_length=256)
    version: StrictStr = Field(min_length=1, max_length=64)


class SafeException(_Model):
    code: StrictStr = Field(min_length=1, max_length=64)
    module: StrictStr = Field(min_length=1, max_length=256)
    type: StrictStr = Field(min_length=1, max_length=256)
    message: StrictStr = Field(max_length=2048)


class RuntimeIdentity(_Model):
    modal_environment: StrictStr | None = Field(default=None, max_length=256)
    modal_image_id: StrictStr | None = Field(default=None, max_length=256)
    modal_region: StrictStr | None = Field(default=None, max_length=256)
    modal_cloud_provider: StrictStr | None = Field(default=None, max_length=256)
    modal_task_id: StrictStr | None = Field(default=None, max_length=256)


class ExecutionStatement(_Model):
    assurance_profile: Literal["basic-local-recorder/1"] = ASSURANCE_PROFILE
    execution_id: UUID
    attempt: StrictInt = Field(default=1, ge=1)
    status: NodeStatus
    function: FunctionIdentity
    modal_call_id: StrictStr | None = Field(default=None, min_length=1, max_length=256)
    modal_input_id: StrictStr | None = Field(default=None, min_length=1, max_length=256)
    dispatched_at: datetime
    recorded_at: datetime
    timestamps: Timestamps
    inputs: list[Artifact] = Field(default_factory=list, max_length=256)
    intermediates: list[Artifact] = Field(default_factory=list, max_length=256)
    results: list[Artifact] = Field(default_factory=list, max_length=256)
    exception: SafeException | None = None
    environment: EnvSnapshot
    runtime: RuntimeIdentity = Field(default_factory=RuntimeIdentity)
    domain_outcome: DomainOutcome | None = None
    result_adapter: AdapterIdentity | None = None
    provenance_profile: Literal["vat-prov/1"] | None = None
    transport: Literal["modal", "local"] = "modal"
    input_bindings: list[InputBinding] = Field(default_factory=list, max_length=256)
    control_references: list[ControlReference] = Field(default_factory=list, max_length=256)
    timestamp_basis: Literal["remote_observed", "local_observation"] = "local_observation"
    uncaptured: Literal[
        "filesystem, subprocess, logs, network, and external side effects are not captured"
    ] = UNCAPTURED

    @model_validator(mode="after")
    def consistent_outcome(self) -> "ExecutionStatement":
        if self.transport == "modal" and self.modal_call_id is None:
            raise ValueError("Modal execution requires a call identity")
        if self.transport == "local" and self.modal_call_id is not None:
            raise ValueError("Local execution cannot claim a Modal identity")
        if (self.input_bindings or self.control_references) and self.provenance_profile is None:
            raise ValueError("Bindings require a provenance profile")
        validate_destinations(self.input_bindings)
        if (self.status is NodeStatus.SUCCEEDED) != (self.exception is None):
            raise ValueError("successful nodes cannot have exceptions; failed nodes require one")
        if self.environment.assurance_tier is not AssuranceTier.SUBPROCESS_WRAPPER:
            raise ValueError("Basic VAT nodes use the subprocess-wrapper assurance tier")
        if self.environment.modal_call_id != self.modal_call_id:
            raise ValueError("environment and statement Modal call identities differ")
        return self


class NodeSignature(_Model):
    keyid: StrictStr = Field(min_length=1)
    sig: StrictStr = Field(min_length=1)


class VatNode(_Model):
    """A signed execution statement with its exact content-addressed bytes."""

    node_format: Literal["vat-node/1"] = NODE_FORMAT
    payload_type: Literal["application/vnd.biodesign-vat.execution-node+json"] = NODE_PAYLOAD_TYPE
    payload: StrictStr
    signatures: list[NodeSignature] = Field(min_length=1, max_length=1)
    blobs: dict[StrictStr, StrictStr] = Field(default_factory=dict)

    @classmethod
    def sign(cls, statement: ExecutionStatement, blobs: dict[str, bytes], signer: LocalSigner,
             *, limits: VatLimits | None = None) -> "VatNode":
        """Sign one canonical statement with a recorder key."""
        recorder = signer_for(signer, Role.RECORDER)
        _validate_statement(statement, blobs, limits or VatLimits())
        payload = canonical_bytes(statement.model_dump(mode="json"))
        node = cls(
            payload=base64.b64encode(payload).decode("ascii"),
            signatures=[NodeSignature(keyid=recorder.keyid,
                                      sig=recorder.sign(pae(NODE_PAYLOAD_TYPE, payload)))],
            blobs={digest: base64.b64encode(raw).decode("ascii") for digest, raw in blobs.items()},
        )
        if len(node.canonical_bytes()) > (limits or VatLimits()).max_node_bytes:
            raise NodeError("LIMIT_EXCEEDED", "Canonical node exceeds the byte limit.")
        return node

    def statement(self) -> ExecutionStatement:
        """Decode a strict canonical statement payload."""
        try:
            raw = base64.b64decode(self.payload, validate=True)
        except (ValueError, Base64Error) as exc:
            raise NodeError("INVALID_NODE", "Node payload is not valid base64.") from exc
        try:
            value = parse_json(raw)
            if canonical_bytes(value) != raw:
                raise NodeError("INVALID_NODE", "Node statement payload is not canonical JSON.")
            return ExecutionStatement.model_validate(value)
        except NodeError:
            raise
        except (ValueError, ValidationError) as exc:
            raise NodeError("INVALID_NODE", "Node statement is invalid.") from exc

    def raw_blobs(self) -> dict[str, bytes]:
        """Decode the node's base64 blob map without trusting its keys."""
        decoded = {}
        try:
            for digest, value in self.blobs.items():
                decoded[digest] = base64.b64decode(value, validate=True)
        except (ValueError, Base64Error) as exc:
            raise NodeError("INVALID_NODE", "Node blob is not valid base64.") from exc
        return decoded

    def canonical_bytes(self) -> bytes:
        """Return the deterministic portable representation."""
        return canonical_bytes(self.model_dump(mode="json"))

    @property
    def node_id(self) -> str:
        """Identify the exact signed node, including signature and carried bytes."""
        return sha256_hex(self.canonical_bytes())

    def save(self, path: str | Path, *, limits: VatLimits | None = None) -> None:
        """Atomically write canonical node JSON without following a destination symlink."""
        limits = limits or VatLimits()
        target = Path(path)
        if target.is_symlink() or target.parent.is_symlink():
            raise NodeError("UNSAFE_PATH", "Node path cannot be a symlink.")
        raw = self.canonical_bytes()
        if len(raw) > limits.max_node_bytes:
            raise NodeError("LIMIT_EXCEEDED", "Canonical node exceeds the byte limit.")
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @classmethod
    def load(cls, path: str | Path, *, limits: VatLimits | None = None) -> "VatNode":
        """Load only bounded, canonical JSON from a regular nonsymlink file."""
        limits = limits or VatLimits()
        source = Path(path)
        if source.is_symlink() or not source.is_file() or source.stat().st_size > limits.max_node_bytes:
            raise NodeError("INVALID_NODE", "Node path is unsafe, missing, or too large.")
        raw = source.read_bytes()
        try:
            value = parse_json(raw)
        except ValueError as exc:
            raise NodeError("INVALID_NODE", f"Portable node JSON is invalid: {exc}") from exc
        try:
            if canonical_bytes(value) != raw:
                raise NodeError("INVALID_NODE", "Portable node JSON is not canonical.")
            return cls.model_validate(value)
        except NodeError:
            raise
        except (ValueError, ValidationError) as exc:
            raise NodeError("INVALID_NODE", "Portable node JSON is invalid.") from exc


@dataclass(frozen=True)
class EncodedDocument:
    """One canonical JSON value plus any byte blobs referenced from it."""

    artifact: Artifact
    byte_artifacts: tuple[Artifact, ...]
    blobs: dict[str, bytes]
    encoded: Any


def safe_artifact_name(name: str) -> bool:
    return (isinstance(name, str) and 0 < len(name) <= 256 and Path(name).name == name
            and name not in {".", ".."} and "\\" not in name
            and not any(ord(character) < 32 for character in name))


def encode_document(value: Any, name: str, *, limits: VatLimits | None = None) -> EncodedDocument:
    """Encode a bounded deterministic value and extract bytes as addressed blobs."""
    limits = limits or VatLimits()
    if not safe_artifact_name(name) or not name.endswith(".json"):
        raise NodeError("INVALID_VALUE", "Canonical value artifact requires a safe .json name.")
    blobs: dict[str, bytes] = {}
    artifacts: list[Artifact] = []
    seen: set[int] = set()
    count = 0

    def walk(item: Any, depth: int) -> Any:
        nonlocal count
        count += 1
        if count > limits.max_items or depth > limits.max_depth:
            raise NodeError("LIMIT_EXCEEDED", "Canonical value exceeds its depth or item limit.")
        if item is None or type(item) is bool or isinstance(item, str):
            return item
        if type(item) is int:
            if not -MAX_SAFE_INTEGER <= item <= MAX_SAFE_INTEGER:
                raise NodeError("INVALID_VALUE", "Integer exceeds the interoperable JSON range.")
            return item
        if type(item) is float:
            if not math.isfinite(item):
                raise NodeError("INVALID_VALUE", "Nonfinite floats are not supported.")
            return item
        if isinstance(item, bytes):
            if len(item) > limits.max_blob_bytes:
                raise NodeError("LIMIT_EXCEEDED", "Byte value exceeds the per-blob limit.")
            digest = sha256_hex(item)
            if digest not in blobs:
                blobs[digest] = item
                artifact_name = f"{Path(name).stem}-bytes-{len(artifacts):04d}.bin"
                artifacts.append(Artifact(kind="log", name=artifact_name,
                                          sha256=digest, bytes_len=len(item)))
            return {"$vat": "bytes/1", "sha256": digest, "bytes_len": len(item)}
        if isinstance(item, (list, tuple, dict)):
            identity = id(item)
            if identity in seen:
                raise NodeError("INVALID_VALUE", "Cyclic values are not supported.")
            seen.add(identity)
            try:
                if isinstance(item, dict):
                    if any(not isinstance(key, str) or key.startswith("$vat") for key in item):
                        raise NodeError("INVALID_VALUE", "Dictionary keys must be strings outside the $vat namespace.")
                    return {key: walk(child, depth + 1) for key, child in item.items()}
                return [walk(child, depth + 1) for child in item]
            finally:
                seen.remove(identity)
        raise NodeError("INVALID_VALUE", f"Unsupported canonical value type: {type(item).__name__}.")

    encoded = walk(value, 0)
    raw = canonical_bytes(encoded)
    if (len(raw) > min(limits.max_value_bytes, limits.max_blob_bytes)
            or len(artifacts) + 1 > limits.max_artifacts
            or sum(map(len, blobs.values())) + len(raw) > limits.max_total_blob_bytes):
        raise NodeError("LIMIT_EXCEEDED", "Canonical value or its byte blobs exceed limits.")
    digest = sha256_hex(raw)
    blobs[digest] = raw
    return EncodedDocument(artifact=Artifact(kind="log", name=name, sha256=digest, bytes_len=len(raw)),
                           byte_artifacts=tuple(artifacts), blobs=blobs, encoded=encoded)


def decode_document(document: Artifact, byte_artifacts: list[Artifact], blobs: dict[str, bytes],
                    *, limits: VatLimits | None = None) -> Any:
    """Validate and decode one canonical document and its exact tagged byte references."""
    limits = limits or VatLimits()
    raw = blobs.get(document.sha256)
    if raw is None or len(raw) != document.bytes_len or sha256_hex(raw) != document.sha256:
        raise NodeError("TAMPERED", "Canonical value artifact is missing or inconsistent.")
    if len(raw) > min(limits.max_value_bytes, limits.max_blob_bytes):
        raise NodeError("LIMIT_EXCEEDED", "Canonical value exceeds its byte limit.")
    if len(byte_artifacts) + 1 > limits.max_artifacts:
        raise NodeError("LIMIT_EXCEEDED", "Canonical value has too many byte artifacts.")
    referenced_blobs = {document.sha256: raw}
    for artifact in byte_artifacts:
        blob = blobs.get(artifact.sha256)
        if blob is not None:
            if len(blob) > limits.max_blob_bytes:
                raise NodeError("LIMIT_EXCEEDED", "Tagged byte value exceeds its per-blob limit.")
            referenced_blobs[artifact.sha256] = blob
    if sum(map(len, referenced_blobs.values())) > limits.max_total_blob_bytes:
        raise NodeError("LIMIT_EXCEEDED", "Canonical value exceeds its total blob limit.")
    value = parse_json(raw)
    if canonical_bytes(value) != raw:
        raise NodeError("TAMPERED", "Canonical value artifact is noncanonical.")
    by_digest = {artifact.sha256: artifact for artifact in byte_artifacts}
    if len(by_digest) != len(byte_artifacts):
        raise NodeError("TAMPERED", "Tagged byte artifact digests must be unique.")
    referenced: set[str] = set()
    count = 0

    def walk(item: Any, depth: int) -> Any:
        nonlocal count
        count += 1
        if count > limits.max_items or depth > limits.max_depth:
            raise NodeError("LIMIT_EXCEEDED", "Canonical value exceeds its depth or item limit.")
        if isinstance(item, list):
            return [walk(child, depth + 1) for child in item]
        if isinstance(item, dict):
            if "$vat" in item:
                if set(item) != {"$vat", "sha256", "bytes_len"} or item["$vat"] != "bytes/1" \
                        or type(item["sha256"]) is not str or type(item["bytes_len"]) is not int:
                    raise NodeError("TAMPERED", "Malformed tagged byte value.")
                artifact = by_digest.get(item["sha256"])
                blob = blobs.get(item["sha256"])
                if (artifact is None or blob is None or artifact.bytes_len != item["bytes_len"]
                        or len(blob) != item["bytes_len"] or sha256_hex(blob) != item["sha256"]):
                    raise NodeError("TAMPERED", "Tagged byte value is missing or inconsistent.")
                referenced.add(item["sha256"])
                return blob
            if any(not isinstance(key, str) or key.startswith("$vat") for key in item):
                raise NodeError("TAMPERED", "Invalid canonical dictionary key.")
            return {key: walk(child, depth + 1) for key, child in item.items()}
        if item is None or type(item) in {bool, str}:
            return item
        if type(item) is int and -MAX_SAFE_INTEGER <= item <= MAX_SAFE_INTEGER:
            return item
        if type(item) is float and math.isfinite(item):
            return item
        raise NodeError("TAMPERED", "Invalid canonical scalar value.")

    decoded = walk(value, 0)
    if referenced != set(by_digest):
        raise NodeError("TAMPERED", "Tagged byte artifacts are missing or unreferenced.")
    return decoded


@dataclass(frozen=True)
class NodeVerifyReport:
    outcome: NodeOutcome
    detail: str
    node_id: str | None = None

    @property
    def passed(self) -> bool:
        return self.outcome is NodeOutcome.OK


def _validate_statement(statement: ExecutionStatement, blobs: dict[str, bytes], limits: VatLimits) -> None:
    artifacts = statement.inputs + statement.intermediates + statement.results
    if len(artifacts) > limits.max_artifacts:
        raise NodeError("LIMIT_EXCEEDED", "Node has too many artifacts.")
    if any(not safe_artifact_name(artifact.name) for artifact in artifacts):
        raise NodeError("TAMPERED", "Node contains an unsafe artifact name.")
    if len({artifact.name for artifact in artifacts}) != len(artifacts):
        raise NodeError("TAMPERED", "Node artifact names must be unique.")
    expected = {artifact.sha256 for artifact in artifacts}
    if set(blobs) != expected:
        raise NodeError("TAMPERED", "Node blob membership does not match its artifacts.")
    if sum(map(len, blobs.values())) > limits.max_total_blob_bytes:
        raise NodeError("LIMIT_EXCEEDED", "Node blobs exceed the total byte limit.")
    for artifact in artifacts:
        raw = blobs[artifact.sha256]
        if len(raw) != artifact.bytes_len or len(raw) > limits.max_blob_bytes \
                or sha256_hex(raw) != artifact.sha256:
            raise NodeError("TAMPERED", "Node artifact digest or length mismatch.")
    for timestamp in (statement.dispatched_at, statement.recorded_at,
                      statement.timestamps.stage_start, statement.timestamps.stage_stop):
        if timestamp.utcoffset() is None:
            raise NodeError("TAMPERED", "Node timestamps must be timezone-aware.")
    if statement.dispatched_at > statement.recorded_at \
            or statement.timestamps.stage_start > statement.timestamps.stage_stop:
        raise NodeError("TAMPERED", "Node timestamp ordering is invalid.")
    call = [artifact for artifact in statement.inputs if artifact.name == "call.json"]
    if len(call) != 1:
        raise NodeError("TAMPERED", "Node requires exactly one call.json input.")
    input_bytes = [artifact for artifact in statement.inputs if artifact.name.startswith("call-bytes-")]
    decode_document(call[0], input_bytes, blobs, limits=limits)
    returned = [artifact for artifact in statement.results if artifact.name == "return.json"]
    result_bytes = [artifact for artifact in statement.results if artifact.name.startswith("return-bytes-")]
    if statement.status is NodeStatus.SUCCEEDED:
        if len(returned) != 1:
            raise NodeError("TAMPERED", "Successful node requires exactly one return.json result.")
        decode_document(returned[0], result_bytes, blobs, limits=limits)
    elif returned or result_bytes:
        raise NodeError("TAMPERED", "Failed node cannot contain a returned value.")


def verify_node_signature(node: VatNode, trust: Ed25519PublicKey | TrustBundle,
                          *, limits: VatLimits | None = None) -> NodeVerifyReport:
    """Authenticate the statement only; artifact availability/integrity is a separate check."""
    limits = limits or VatLimits()
    configured = trust if isinstance(trust, TrustBundle) else TrustBundle.pinned(trust)
    try:
        if (node.node_format != NODE_FORMAT or node.payload_type != NODE_PAYLOAD_TYPE
                or len(node.signatures) != 1 or len(node.canonical_bytes()) > limits.max_node_bytes):
            raise ValueError("Invalid or oversized node envelope")
        node.statement()
        signature = node.signatures[0]
        configured.resolve(signature.keyid, Role.RECORDER).verify(
            base64.b64decode(signature.sig, validate=True),
            pae(node.payload_type, base64.b64decode(node.payload, validate=True)))
        return NodeVerifyReport(NodeOutcome.OK, "recorder statement signature verified", node.node_id)
    except (UntrustedKeyError, InvalidSignature, Base64Error):
        return NodeVerifyReport(NodeOutcome.BAD_SIGNATURE, "recorder signature is invalid or untrusted")
    except (ValueError, IndexError, TypeError) as exc:
        return NodeVerifyReport(NodeOutcome.TAMPERED, str(exc))


def verify_node(node: VatNode, trust: Ed25519PublicKey | TrustBundle,
                *, limits: VatLimits | None = None) -> NodeVerifyReport:
    """Verify a Basic node without trusting its embedded key identifier or bytes."""
    limits = limits or VatLimits()
    signature_report = verify_node_signature(node, trust, limits=limits)
    if not signature_report.passed:
        return signature_report
    try:
        statement = node.statement()
        blobs = node.raw_blobs()
        _validate_statement(statement, blobs, limits)
        return NodeVerifyReport(NodeOutcome.OK, "recorder signature and all artifact bytes verified", node.node_id)
    except (ValueError, IndexError, KeyError, TypeError) as exc:
        return NodeVerifyReport(NodeOutcome.TAMPERED, str(exc) or "node validation failed")


def node_result_value(node: VatNode, *, limits: VatLimits | None = None) -> Any:
    """Decode a successful node's return value after structural verification by its caller."""
    limits = limits or VatLimits()
    statement, blobs = node.statement(), node.raw_blobs()
    if statement.status is not NodeStatus.SUCCEEDED:
        raise NodeError("NO_RESULT", "Failed execution nodes do not have a return value.")
    returned = next(artifact for artifact in statement.results if artifact.name == "return.json")
    byte_artifacts = [artifact for artifact in statement.results if artifact.name.startswith("return-bytes-")]
    return decode_document(returned, byte_artifacts, blobs, limits=limits)


def append_node(log: SessionLog, node: VatNode, trust: Ed25519PublicKey | TrustBundle,
                *, user_session: UserSession, params: DesignParams | None = None,
                external: list[DataSource] | None = None,
                limits: VatLimits | None = None) -> VatRecord:
    """Append one verified node idempotently without involving its compute provider."""
    report = verify_node(node, trust, limits=limits)
    if not report.passed:
        raise NodeError("INVALID_NODE", f"Cannot append unverified node: {report.detail}")
    statement = node.statement()
    node_bytes = node.canonical_bytes()
    node_digest = sha256_hex(node_bytes)
    execution_id = str(statement.execution_id)
    for record in log.records:
        existing = next((artifact for artifact in record.intermediates
                         if artifact.kind == "vat_node"), None)
        if existing and existing.sha256 == node_digest:
            return record.model_copy(deep=True)
        if record.params.hyperparams.get("vat_execution_id") == execution_id:
            raise NodeError("APPEND_CONFLICT", "Execution ID is already bound to a different node.")
    supplied = params or DesignParams()
    reserved = {"vat_node_id", "vat_execution_id", "vat_status", "vat_function_source_sha256"}
    if reserved.intersection(supplied.hyperparams):
        raise NodeError("APPEND_CONFLICT", "Node identity hyperparameters are reserved.")
    blobs = node.raw_blobs()
    for raw in blobs.values():
        log.blobs.put(raw)
    embedded = log.blobs.artifact("vat_node", f"{execution_id}.vat-node.json", node_bytes)
    merged = supplied.model_copy(deep=True, update={"hyperparams": {
        **supplied.hyperparams,
        "vat_node_id": node.node_id,
        "vat_execution_id": execution_id,
        "vat_status": statement.status.value,
        "vat_function_source_sha256": statement.function.source_sha256 or "unobserved",
    }})
    record = VatRecord(
        record_type=RecordType.FUNCTION_CALL,
        session_uuid=log.session_uuid,
        seq_no=0,
        user_session=user_session,
        timestamps=statement.timestamps,
        inputs=statement.inputs,
        params=merged,
        external=external or [],
        environment=statement.environment,
        intermediates=[embedded, *statement.intermediates],
        results=statement.results,
        link=HashLink(sha_prev=GENESIS_PREV, leaf_index=0),
    )
    return log.append(record)
