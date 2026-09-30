"""Typed, explicit producer-to-input bindings for vat-prov/1.

The module's schema has no execution/provider imports. Resolution imports the node
codec lazily so execution statements can use the same models as the verifier.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator, model_validator

PROFILE = "vat-prov/1"
DIGEST = r"^[0-9a-f]{64}$"


def pointer_tokens(pointer: str) -> list[str]:
    """Decode RFC 6901, rejecting invalid escape sequences and non-pointer strings."""
    if pointer == "":
        return []
    if not pointer.startswith("/") or re.search(r"~(?![01])", pointer):
        raise ValueError("Invalid JSON Pointer")
    return [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]


def select(value: Any, pointer: str) -> Any:
    """Select only existing JSON members; array indices are canonical decimal integers."""
    for part in pointer_tokens(pointer):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", part) and int(part) < len(value):
            value = value[int(part)]
        else:
            raise ValueError("JSON Pointer does not select an existing value")
    return value


class OutputSelector(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["return", "artifact"] = "return"
    pointer: StrictStr = ""
    name: StrictStr | None = Field(default=None, min_length=1, max_length=256)
    classification: Literal["output", "intermediate"] = "output"
    encoding: Literal["identity", "utf-8"] = "identity"

    @model_validator(mode="after")
    def coherent(self):
        pointer_tokens(self.pointer)
        if self.kind == "return":
            if self.name is not None or self.classification != "output" or self.encoding != "identity":
                raise ValueError("Return selectors select output values only")
        elif self.name is None or self.pointer:
            raise ValueError("Artifact selectors require a name and no pointer")
        return self


class InputBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    destination: StrictStr
    producer_node_id: StrictStr = Field(pattern=DIGEST)
    selector: OutputSelector
    codec: Literal["vat-json/1"] = "vat-json/1"
    sha256: StrictStr = Field(pattern=DIGEST)
    bytes_len: StrictInt = Field(ge=0)
    artifact_sha256: StrictStr | None = Field(default=None, pattern=DIGEST)
    artifact_bytes_len: StrictInt | None = Field(default=None, ge=0)

    @field_validator("destination")
    @classmethod
    def input_path(cls, value):
        tokens = pointer_tokens(value)
        if len(tokens) < 2 or tokens[0] not in {"args", "kwargs"}:
            raise ValueError("Binding destination must address an argument")
        return value

    @model_validator(mode="after")
    def artifact_commitment(self):
        if self.selector.kind == "artifact":
            if self.artifact_sha256 is None or self.artifact_bytes_len is None:
                raise ValueError("Artifact binding requires its byte commitment")
        elif self.artifact_sha256 is not None or self.artifact_bytes_len is not None:
            raise ValueError("Return binding cannot carry artifact metadata")
        return self


class ControlReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    producer_node_id: StrictStr = Field(pattern=DIGEST)
    pointer: StrictStr = ""
    call_id: StrictStr | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("pointer")
    @classmethod
    def valid_pointer(cls, value):
        pointer_tokens(value)
        return value


def validate_destinations(bindings: list[InputBinding]) -> None:
    paths = [pointer_tokens(binding.destination) for binding in bindings]
    for i, path in enumerate(paths):
        for other in paths[:i]:
            if path[:len(other)] == other or other[:len(path)] == path:
                raise ValueError("Binding destinations overlap")


def selected_output(node, selector: OutputSelector, *, limits=None) -> Any:
    from .node import node_result_value
    if selector.kind == "return":
        return select(node_result_value(node, limits=limits), selector.pointer)
    statement = node.statement()
    artifacts = statement.results if selector.classification == "output" else statement.intermediates
    artifact = next((a for a in artifacts if a.name == selector.name), None)
    if artifact is None:
        raise ValueError("Selected artifact is absent or has another classification")
    raw = node.raw_blobs()[artifact.sha256]
    if selector.encoding == "utf-8":
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Selected artifact is not valid UTF-8") from exc
    return raw


def selected_artifact_bytes(node, selector: OutputSelector) -> bytes:
    """Return the original artifact bytes independently of its input decoding."""
    statement = node.statement()
    artifacts = statement.results if selector.classification == "output" else statement.intermediates
    artifact = next((item for item in artifacts if item.name == selector.name), None)
    if artifact is None:
        raise ValueError("Selected artifact is absent or has another classification")
    return node.raw_blobs()[artifact.sha256]


def commitment(value: Any, *, limits=None):
    from .node import encode_document
    return encode_document(value, "binding.json", limits=limits).artifact


@dataclass(frozen=True)
class VatOutputRef:
    """Immutable evidence plus a selector; trust is supplied by the consuming recorder."""
    node_bytes: bytes
    selector: OutputSelector


def resolve_inputs(args: tuple, kwargs: dict, trust, *, limits):
    """Freeze input references and ordinary values before dispatch; never infer edges."""
    from .canon import parse_json, sha256_hex
    from .node import VatNode, NodeError, verify_node, encode_document
    bindings, producers = [], {}
    active = set()
    count = 0

    def walk(value, path, depth):
        nonlocal count
        count += 1
        if depth > limits.max_depth or count > limits.max_items:
            raise NodeError("LIMIT_EXCEEDED", "Input reference traversal exceeds limits")
        if isinstance(value, VatOutputRef):
            if len(value.node_bytes) > limits.max_node_bytes or len(bindings) >= 256:
                raise NodeError("LIMIT_EXCEEDED", "Input references exceed limits")
            node = VatNode.model_validate(parse_json(value.node_bytes))
            if not verify_node(node, trust, limits=limits).passed:
                raise NodeError("INVALID_SOURCE", "Input producer failed verification")
            chosen = selected_output(node, value.selector, limits=limits)
            digest = commitment(chosen, limits=limits)
            raw_artifact = selected_artifact_bytes(node, value.selector) if value.selector.kind == "artifact" else None
            extra = ({"artifact_sha256": sha256_hex(raw_artifact), "artifact_bytes_len": len(raw_artifact)}
                     if raw_artifact is not None else {})
            bindings.append(InputBinding(destination=path, producer_node_id=node.node_id,
                                         selector=value.selector, sha256=digest.sha256,
                                         bytes_len=digest.bytes_len, **extra))
            producers[node.node_id] = node
            return chosen
        if isinstance(value, (list, tuple, dict)):
            if id(value) in active:
                raise NodeError("INVALID_VALUE", "Cyclic input containers")
            active.add(id(value))
            try:
                if isinstance(value, dict):
                    if any(not isinstance(k, str) for k in value):
                        raise NodeError("INVALID_VALUE", "Input keys must be strings")
                    return {k: walk(v, path + "/" + k.replace("~", "~0").replace("/", "~1"), depth + 1)
                            for k, v in value.items()}
                return [walk(v, path + "/" + str(i), depth + 1) for i, v in enumerate(value)]
            finally:
                active.remove(id(value))
        return value

    resolved = walk({"args": list(args), "kwargs": kwargs}, "", 0)
    document = encode_document(resolved, "call.json", limits=limits)
    validate_destinations(bindings)
    return document, bindings, producers


@dataclass
class ProvenanceReport:
    outcome: str
    checks: list[dict]
    unspecified_inputs: int = 0

    @property
    def exit_code(self) -> int:
        return {"VALID": 0, "INVALID": 2, "UNRESOLVED": 3, "NOT_RECORDED": 4}[self.outcome]


def verify_provenance(nodes, trust, *, limits=None, unresolved: bool = False,
                      unrecorded_steps: int = 0) -> ProvenanceReport:
    """Verify recorded argument relationships independently of session membership."""
    from .node import VatLimits, verify_node, verify_node_signature, decode_document, node_result_value
    from .canon import sha256_hex
    limits = limits or VatLimits()
    by_id, executions, checks, verified = {}, {}, [], {}
    invalid, missing, unprofiled, profiled, unspecified = False, unresolved, unrecorded_steps, 0, 0
    edges = {}
    count = 0
    for node in nodes:
        count += 1
        if count > 10_000:
            return ProvenanceReport("INVALID", [{"status": "failed", "reason": "GRAPH_LIMIT"}])
        identifier = node.node_id
        if identifier in by_id:
            continue
        by_id[identifier] = node
        report = verify_node(node, trust, limits=limits)
        verified[identifier] = report.passed
        if not report.passed:
            signature = verify_node_signature(node, trust, limits=limits)
            if signature.passed:
                statement = node.statement()
                expected = {a.sha256: a.bytes_len for a in [*statement.inputs, *statement.results, *statement.intermediates]}
                try:
                    available = node.raw_blobs()
                except ValueError:
                    available = None
                if (available is not None and set(available) < set(expected)
                        and all(len(raw) == expected[digest] and sha256_hex(raw) == digest for digest, raw in available.items())):
                    missing = True
                    verified[identifier] = None
                    checks.append({"node_id": identifier, "status": "unchecked", "reason": "NODE_BYTES_UNAVAILABLE"})
                    continue
            invalid = True
            checks.append({"node_id": identifier, "status": "failed", "reason": "INVALID_NODE", "detail": report.detail})
            continue
        statement = node.statement()
        execution_id = str(statement.execution_id)
        if execution_id in executions and executions[execution_id] != identifier:
            invalid = True
            checks.append({"node_id": identifier, "status": "failed", "reason": "EXECUTION_CONFLICT"})
        executions[execution_id] = identifier
        if statement.provenance_profile != PROFILE:
            unprofiled += 1
        else:
            profiled += 1
    reference_count = 0
    for identifier, consumer in by_id.items():
        if not verified[identifier]:
            continue
        statement = consumer.statement()
        if statement.provenance_profile != PROFILE:
            continue
        document = next(a for a in statement.inputs if a.name == "call.json")
        byte_inputs = [a for a in statement.inputs if a.name.startswith("call-bytes-")]
        call = decode_document(document, byte_inputs, consumer.raw_blobs(), limits=limits)
        if (not isinstance(call, dict) or set(call) != {"args", "kwargs"}
                or not isinstance(call["args"], list) or not isinstance(call["kwargs"], dict)):
            invalid = True
            checks.append({"node_id": identifier, "status": "failed", "reason": "INVALID_CALL_SHAPE"})
            continue
        argument_paths = ["/args/" + str(i) for i in range(len(call["args"]))]
        argument_paths += ["/kwargs/" + k.replace("~", "~0").replace("/", "~1") for k in call["kwargs"]]
        bound_paths = {b.destination for b in statement.input_bindings}
        def unspecified_origin(value, path):
            if path in bound_paths:
                return False
            if isinstance(value, list) and value:
                return any(unspecified_origin(v, path + "/" + str(i)) for i, v in enumerate(value))
            if isinstance(value, dict) and value:
                return any(unspecified_origin(v, path + "/" + k.replace("~", "~0").replace("/", "~1"))
                           for k, v in value.items())
            return True
        unspecified += sum(unspecified_origin(select(call, p), p) for p in argument_paths)
        edges[identifier] = []
        for binding in [*statement.input_bindings, *statement.control_references]:
            reference_count += 1
            if reference_count > 100_000:
                return ProvenanceReport("INVALID", [{"status": "failed", "reason": "GRAPH_LIMIT"}])
            producer_id = binding.producer_node_id
            item = {"node_id": identifier, "producer_node_id": producer_id,
                    "kind": "data" if isinstance(binding, InputBinding) else "control",
                    "destination": getattr(binding, "destination", None)}
            producer = by_id.get(producer_id)
            if producer is None:
                missing = True
                checks.append({**item, "status": "unchecked", "reason": "PRODUCER_UNAVAILABLE"})
                continue
            if verified[producer_id] is None:
                missing = True
                checks.append({**item, "status": "unchecked", "reason": "NODE_BYTES_UNAVAILABLE"})
                continue
            if not verified[producer_id] or producer_id == identifier:
                invalid = True
                checks.append({**item, "status": "failed", "reason": "INVALID_PRODUCER"})
                continue
            try:
                if isinstance(binding, InputBinding):
                    chosen = selected_output(producer, binding.selector, limits=limits)
                    expected = commitment(chosen, limits=limits)
                    actual = commitment(select(call, binding.destination), limits=limits)
                    if (expected.sha256 != binding.sha256 or actual.sha256 != binding.sha256
                            or expected.bytes_len != binding.bytes_len or actual.bytes_len != binding.bytes_len):
                        raise ValueError("Captured source and destination differ")
                    raw_artifact = (selected_artifact_bytes(producer, binding.selector)
                                    if binding.selector.kind == "artifact" else None)
                    if raw_artifact is not None and (sha256_hex(raw_artifact) != binding.artifact_sha256
                                                     or len(raw_artifact) != binding.artifact_bytes_len):
                        raise ValueError("Artifact commitment differs")
                    edges[identifier].append(producer_id)
                else:
                    selected = select(node_result_value(producer, limits=limits), binding.pointer)
                    if binding.call_id is not None and (not isinstance(selected, dict) or selected.get("id") != binding.call_id):
                        raise ValueError("Control call identity differs")
            except (ValueError, KeyError, TypeError) as exc:
                invalid = True
                checks.append({**item, "status": "failed", "reason": "BINDING_MISMATCH", "detail": str(exc)})
            else:
                checks.append({**item, "status": "passed", "reason": "CONSISTENT"})
    # Iterative topological elimination avoids recursion on untrusted graph depth.
    remaining = {key: len(set(value)) for key, value in edges.items()}
    children = {}
    for child, parents in edges.items():
        for parent in set(parents):
            children.setdefault(parent, []).append(child)
            remaining.setdefault(parent, 0)
    ready = [key for key, degree in remaining.items() if degree == 0]
    visited = 0
    while ready:
        parent = ready.pop()
        visited += 1
        for child in children.get(parent, []):
            remaining[child] -= 1
            if remaining[child] == 0:
                ready.append(child)
    if visited != len(remaining):
        invalid = True
        checks.append({"status": "failed", "reason": "DATA_CYCLE"})
    outcome = ("INVALID" if invalid else "UNRESOLVED" if missing or (profiled and unprofiled)
               else "NOT_RECORDED" if not profiled else "VALID")
    return ProvenanceReport(outcome, checks, unspecified)


def bundle_provenance(bundle, trust) -> tuple[ProvenanceReport, list]:
    """Read only embedded evidence; never consult hidden records or the local journal."""
    from .node import VatNode, VatLimits
    from .canon import parse_json, sha256_hex
    nodes, missing, unprofiled, failures, lifecycle = [], False, 0, [], {}
    for record in sorted(bundle.records, key=lambda r: r.link.leaf_index):
        if record.params.hyperparams.get("capture_kind") == "execution_lifecycle":
            event = record.params.hyperparams
            if not isinstance(event.get("execution_id"), str) or not isinstance(event.get("state"), str):
                failures.append({"status": "failed", "reason": "INVALID_LIFECYCLE"})
            else:
                lifecycle[event["execution_id"]] = event["state"]
            continue
        artifacts = [a for a in record.intermediates if a.kind == "vat_node"]
        if not artifacts:
            if record.record_type.value in {"agent_call", "design_run", "function_call"} and not record.params.hyperparams.get("vat_node_id"):
                unprofiled += 1
            continue
        for artifact in artifacts:
            raw = bundle.blobs.get(artifact.sha256)
            if raw is None:
                missing = True
                continue
            try:
                if len(raw) > VatLimits().max_node_bytes:
                    raise ValueError("Embedded node exceeds the byte limit")
                if len(raw) != artifact.bytes_len or sha256_hex(raw) != artifact.sha256:
                    raise ValueError("Embedded node digest or length mismatch")
                nodes.append(VatNode.model_validate(parse_json(raw)))
            except ValueError as exc:
                failures.append({"status": "failed", "reason": "INVALID_EMBEDDED_NODE", "detail": str(exc)})
    terminal_ids = {str(node.statement().execution_id) for node in nodes}
    report = verify_provenance(nodes, trust, unresolved=missing or any(s != "completed" for s in lifecycle.values())
                               or bool(set(lifecycle) - terminal_ids),
                               unrecorded_steps=unprofiled)
    if failures:
        report.outcome = "INVALID"
        report.checks.extend(failures)
    return report, nodes
