"""Data-only contracts for reviewable, synthesized scientific workflows."""

from __future__ import annotations

import copy
import hashlib
import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, ValidationError, field_validator, model_validator

from vatlog.canon import canonical_bytes
from vatlog.provenance import OutputSelector, pointer_tokens, select

from .design_inputs import DesignError
from .tool_catalog import tool_spec
from .tool_contracts import validate_tool_request

WORKFLOW_VERSION = "vat-workflow/1"
LOCAL_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


class WorkflowBinding(BaseModel):
    """Replace one request value with an exact output from an earlier node."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    destination: StrictStr
    producer: StrictStr
    selector: OutputSelector
    allow_intermediate: StrictBool = False

    @field_validator("destination")
    @classmethod
    def request_pointer(cls, value: str) -> str:
        tokens = pointer_tokens(value)
        if len(tokens) < 2 or tokens[0] != "payload":
            raise ValueError("binding must select a value beneath the request payload")
        return value

    @field_validator("producer")
    @classmethod
    def producer_id(cls, value: str) -> str:
        if not LOCAL_ID.fullmatch(value):
            raise ValueError("invalid producer ID")
        return value

    @model_validator(mode="after")
    def intermediate_opt_in(self):
        if self.selector.classification == "intermediate" and not self.allow_intermediate:
            raise ValueError("intermediate artifacts require explicit opt-in")
        if self.allow_intermediate and self.selector.classification != "intermediate":
            raise ValueError("intermediate opt-in is only valid for an intermediate selector")
        return self


class WorkflowNode(BaseModel):
    """One immutable registered-tool invocation in a proposed workflow."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    id: StrictStr
    tool_id: StrictStr
    execution_id: StrictStr
    request: dict
    bindings: list[WorkflowBinding] = Field(default_factory=list, max_length=256)
    controls: list[StrictStr] = Field(default_factory=list, max_length=32)
    authorized: StrictBool = False

    @field_validator("id")
    @classmethod
    def local_id(cls, value: str) -> str:
        if not LOCAL_ID.fullmatch(value):
            raise ValueError("invalid workflow node ID")
        return value

    @field_validator("execution_id")
    @classmethod
    def execution_uuid(cls, value: str) -> str:
        return str(UUID(value))

    @field_validator("controls")
    @classmethod
    def control_ids(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(not LOCAL_ID.fullmatch(item) for item in value):
            raise ValueError("control references must be unique node IDs")
        return value

    @model_validator(mode="after")
    def coherent_request(self):
        spec = _available_tool(self.tool_id)
        if self.request.get("tool") != self.tool_id or self.request.get("operation") != spec.operation:
            raise ValueError("request identity differs from workflow tool")
        validate_tool_request(self.request, expected_tool=self.tool_id)
        paths = [pointer_tokens(binding.destination) for binding in self.bindings]
        for index, path in enumerate(paths):
            select(self.request, self.bindings[index].destination)
            for prior in paths[:index]:
                if path[:len(prior)] == prior or prior[:len(path)] == path:
                    raise ValueError("binding destinations overlap")
        return self


class WorkflowProposal(BaseModel):
    """Canonical, side-effect-free workflow proposal reviewed as a whole."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal["vat-workflow/1"] = WORKFLOW_VERSION
    id: StrictStr
    label: StrictStr = Field(min_length=1, max_length=128)
    purpose: StrictStr = Field(min_length=1, max_length=2048)
    nodes: list[WorkflowNode] = Field(min_length=1, max_length=32)

    @field_validator("id")
    @classmethod
    def workflow_uuid(cls, value: str) -> str:
        return str(UUID(value))

    @model_validator(mode="after")
    def ordered_graph(self):
        seen: set[str] = set()
        executions: set[str] = set()
        for node in self.nodes:
            if node.id in seen or node.execution_id in executions:
                raise ValueError("workflow node and execution identities must be unique")
            dependencies = [binding.producer for binding in node.bindings] + node.controls
            if node.id in dependencies or any(dependency not in seen for dependency in dependencies):
                raise ValueError("references must name earlier workflow nodes")
            seen.add(node.id)
            executions.add(node.execution_id)
        return self

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_bytes(self.model_dump(mode="json"))).hexdigest()


def _available_tool(tool_id: str):
    try:
        spec = tool_spec(tool_id)
    except ValueError as exc:
        raise ValueError("unknown workflow tool") from exc
    if not spec.implemented or spec.readiness == "blocked":
        raise ValueError("workflow tool is unavailable")
    return spec


def validate_workflow(value: object) -> WorkflowProposal:
    """Validate a workflow without opening a store, network connection or deployment."""
    try:
        return WorkflowProposal.model_validate(value)
    except (ValidationError, ValueError) as exc:
        raise DesignError("INVALID_WORKFLOW", "Invalid scientific workflow proposal.") from exc


def replace_binding(request: dict, destination: str, value: object) -> dict:
    """Return a copy with one already-validated JSON pointer replaced."""
    result = copy.deepcopy(request)
    tokens = pointer_tokens(destination)
    target: object = result
    for token in tokens[:-1]:
        if isinstance(target, dict) and token in target:
            target = target[token]
        elif isinstance(target, list) and token.isdecimal() and int(token) < len(target):
            target = target[int(token)]
        else:
            raise DesignError("INVALID_WORKFLOW", "Binding destination does not exist.")
    final = tokens[-1]
    if isinstance(target, dict) and final in target:
        target[final] = value
    elif isinstance(target, list) and final.isdecimal() and int(final) < len(target):
        target[int(final)] = value
    else:
        raise DesignError("INVALID_WORKFLOW", "Binding destination does not exist.")
    return result


def dry_run(value: object) -> dict:
    """Project review information without dispatching or inspecting credentials."""
    proposal = validate_workflow(value)
    return {
        "version": proposal.version,
        "workflow_id": proposal.id,
        "digest": proposal.digest,
        "dispatchable": all(node.authorized for node in proposal.nodes),
        "nodes": [{
            "id": node.id,
            "tool_id": node.tool_id,
            "execution_id": node.execution_id,
            "authorized": node.authorized,
            "dependencies": list(dict.fromkeys(
                [binding.producer for binding in node.bindings] + node.controls)),
            "bindings": [binding.model_dump(mode="json") for binding in node.bindings],
            "resource": tool_spec(node.tool_id).hardware,
            "readiness": tool_spec(node.tool_id).readiness,
            "licence": tool_spec(node.tool_id).licence,
        } for node in proposal.nodes],
    }
