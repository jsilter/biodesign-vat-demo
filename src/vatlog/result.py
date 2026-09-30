"""Immutable execution results backed by canonical signed bytes."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

from .canon import parse_json, sha256_hex
from .node import (DomainOutcome, NodeError, NodeStatus, SafeException, VatLimits, NodeVerifyReport,
                   VatNode, append_node, node_result_value, verify_node)
from .roles import TrustBundle
from .provenance import VatOutputRef
from .schema import DataSource, DesignParams, UserSession, VatRecord
from .store import SessionLog

T = TypeVar("T")


class VatExecutionError(NodeError):
    """A terminal execution failure retaining its signed result."""
    def __init__(self, result: "VatResult"):
        super().__init__("EXECUTION_FAILED", "Failed execution nodes do not have a return value.")
        self.result = result


@dataclass(frozen=True)
class VatArtifact:
    name: str
    kind: str
    classification: str
    sha256: str
    bytes_len: int
    _bytes: bytes

    def read_bytes(self) -> bytes:
        if len(self._bytes) != self.bytes_len or sha256_hex(self._bytes) != self.sha256:
            raise NodeError("TAMPERED", "Artifact bytes failed verification.")
        return self._bytes


@dataclass(frozen=True, init=False)
class VatResult(Generic[T]):
    """One verified terminal observation; mutable accessors return independent copies."""
    _raw: bytes
    _trust_raw: str
    _limits: VatLimits

    def __init__(self, node: VatNode, trust: TrustBundle, limits: VatLimits | None = None):
        limits = limits or VatLimits()
        report = verify_node(node, trust, limits=limits)
        if not report.passed:
            raise NodeError("INVALID_NODE", report.detail)
        object.__setattr__(self, "_raw", node.canonical_bytes())
        object.__setattr__(self, "_trust_raw", trust.model_dump_json())
        object.__setattr__(self, "_limits", limits)

    @property
    def node(self) -> VatNode:
        return VatNode.model_validate(parse_json(self._raw))

    @property
    def node_id(self) -> str:
        return sha256_hex(self._raw)

    @property
    def execution_id(self) -> str:
        return str(self.node.statement().execution_id)

    @property
    def status(self) -> NodeStatus:
        return self.node.statement().status

    @property
    def error(self) -> SafeException | None:
        return self.node.statement().exception

    @property
    def domain_outcome(self) -> DomainOutcome | None:
        return self.node.statement().domain_outcome

    @property
    def value(self) -> T:
        if self.status is not NodeStatus.SUCCEEDED:
            raise VatExecutionError(self)
        return node_result_value(self.node, limits=self._limits)

    @property
    def artifacts(self) -> tuple[VatArtifact, ...]:
        node = self.node
        statement, blobs = node.statement(), node.raw_blobs()
        return tuple(VatArtifact(a.name, a.kind, classification, a.sha256, a.bytes_len, blobs[a.sha256])
                     for classification, entries in (("output", statement.results),
                                                      ("intermediate", statement.intermediates))
                     for a in entries)

    def verify(self, trust: TrustBundle) -> NodeVerifyReport:
        return verify_node(self.node, trust, limits=self._limits)

    def ref(self, pointer: str = "") -> VatOutputRef:
        """Select an immutable output value without losing its producer identity."""
        from .provenance import OutputSelector, VatOutputRef, selected_output
        selector = OutputSelector(pointer=pointer)
        selected_output(self.node, selector, limits=self._limits)
        return VatOutputRef(self._raw, selector)

    def artifact_ref(self, name: str, *, allow_intermediate: bool = False,
                     encoding: str = "identity") -> VatOutputRef:
        """Reference named output bytes; intermediate consumption requires opt-in."""
        from .provenance import OutputSelector, VatOutputRef, selected_output
        artifact = next((a for a in self.artifacts if a.name == name), None)
        if artifact is None or (artifact.classification == "intermediate" and not allow_intermediate):
            raise NodeError("INVALID_SOURCE", "Artifact is absent or requires intermediate opt-in")
        selector = OutputSelector(kind="artifact", name=name, classification=artifact.classification,
                                  encoding=encoding)
        selected_output(self.node, selector, limits=self._limits)
        return VatOutputRef(self._raw, selector)

    def save(self, path: str | Path) -> None:
        self.node.save(path, limits=self._limits)

    @classmethod
    def load(cls, path: str | Path, *, trust: TrustBundle, limits: VatLimits | None = None) -> "VatResult":
        return cls(VatNode.load(path, limits=limits), trust, limits)

    def append_to(self, log: SessionLog, *, user_session: UserSession,
                  params: DesignParams | None = None,
                  external: list[DataSource] | None = None) -> VatRecord:
        return append_node(log, self.node, TrustBundle.model_validate_json(self._trust_raw),
                           user_session=user_session, params=params, external=external,
                           limits=self._limits)
