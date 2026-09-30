"""Provider-independent durable journal for recorded executions."""
from __future__ import annotations

import base64
import copy
import platform
import asyncio
import binascii
import fcntl
import os
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError

from .canon import canonical_bytes, parse_json, sha256_hex
from .capture import AdaptedResult, CaptureContext, adapter_identity
from .provenance import PROFILE, InputBinding, ControlReference, resolve_inputs
from .result import VatResult
from .node import (AdapterIdentity, FunctionIdentity, NodeError, VatLimits, VatNode,
                   encode_document, decode_document, verify_node, ExecutionStatement,
                   SafeException, safe_artifact_name)
from .roles import LocalSigner, Role, TrustBundle, TrustEntry, signer_for
from .schema import Artifact, AssuranceTier, EnvSnapshot, Timestamps

_scope = ContextVar("vat_execution_scope", default=None)

class VatStateError(NodeError):
    """Durable lifecycle error; never authorizes implicit resubmission."""

class VatObservationPending(VatStateError):
    """Accepted execution has no safely observed terminal outcome."""
    def __init__(self, code: str, message: str, execution_id: str | None = None):
        super().__init__(code, message)
        self.execution_id = execution_id

class _RequestSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    function: FunctionIdentity
    result_adapter: AdapterIdentity | None = None
    provenance_profile: Literal["vat-prov/1"] | None = None
    transport: Literal["local", "modal"] = "modal"
    input_bindings: list[InputBinding] = Field(default_factory=list, max_length=256)
    control_references: list[ControlReference] = Field(default_factory=list, max_length=256)
    scope_id: StrictStr | None = Field(default=None, max_length=256)
    dispatched_at: datetime
    inputs: list[Artifact]
    blobs: dict[StrictStr, StrictStr]
    call_sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")

    def raw_blobs(self) -> dict[str, bytes]:
        try:
            return {digest: base64.b64decode(value, validate=True) for digest, value in self.blobs.items()}
        except (ValueError, binascii.Error) as exc:
            raise VatStateError("JOURNAL_CORRUPT", "Stored request blobs are invalid.") from exc


class VatRecorder:
    """Single-owner durable journal and caller-side recorder signer."""

    def __init__(self, root: str | Path, signer: LocalSigner, *, limits: VatLimits | None = None):
        self.signer = signer_for(signer, Role.RECORDER)
        self.limits = limits or VatLimits()
        supplied = Path(root)
        if supplied.is_symlink():
            raise VatStateError("UNSAFE_PATH", "Recorder root cannot be a symlink.")
        self.root = supplied.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        lock_path = self.root / "owner.lock"
        if lock_path.is_symlink():
            raise VatStateError("UNSAFE_PATH", "Recorder owner lock cannot be a symlink.")
        self.lock = lock_path.open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.lock.close()
            raise VatStateError("STORE_BUSY", "Another recorder owns this directory.") from exc
        os.chmod(self.root / "owner.lock", 0o600)
        database = self.root / "state.sqlite"
        if database.is_symlink():
            self.lock.close()
            raise VatStateError("UNSAFE_PATH", "Recorder database cannot be a symlink.")
        self.db = sqlite3.connect(database)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS executions (
                id TEXT PRIMARY KEY, request BLOB NOT NULL, state TEXT NOT NULL,
                call_id TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0
            )
        """)
        self.db.execute("CREATE TABLE IF NOT EXISTS observations (id TEXT PRIMARY KEY, data BLOB NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS sources (id TEXT PRIMARY KEY, data BLOB NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS lifecycle (ordinal INTEGER PRIMARY KEY, execution_id TEXT, state TEXT, observed_at TEXT)")
        self.db.commit()
        self.pending = set()
        self.collection_locks = {}
        os.chmod(database, 0o600)
        self.trust = TrustBundle(entries=[TrustEntry.from_signer(self.signer)])

    def __enter__(self) -> "VatRecorder":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def close(self) -> None:
        """Release the journal and its exclusive owner lock."""
        if self.pending:
            raise VatStateError("SUBMISSIONS_PENDING", "Await recorder.drain() before closing pending submissions.")
        if getattr(self, "db", None) is not None:
            self.db.close()
            self.db = None
        if getattr(self, "lock", None) is not None:
            self.lock.close()
            self.lock = None

    async def drain(self) -> None:
        """Wait for shielded acceptance bookkeeping before releasing storage."""
        if self.pending:
            await asyncio.gather(*tuple(self.pending), return_exceptions=True)

    @contextmanager
    def scope(self, identifier: str):
        """Associate concurrent local/remote calls with one workflow, without global mutable state."""
        token = _scope.set((self, identifier))
        try:
            yield
        finally:
            _scope.reset(token)

    def _event(self, execution_id: str, state: str) -> None:
        self.db.execute("INSERT INTO lifecycle(execution_id,state,observed_at) VALUES (?,?,?)",
                        (execution_id, state, datetime.now(timezone.utc).isoformat()))

    def begin(self, identity: FunctionIdentity, document, *, execution_id: str | None = None,
              result_adapter: AdapterIdentity | None = None, provenance_profile=None,
              transport="modal", input_bindings=(), control_references=()) -> str:
        """Persist immutable dispatch evidence before a remote submission."""
        execution_id = str(UUID(execution_id)) if execution_id else str(uuid4())
        snapshot = _RequestSnapshot(
            function=identity, result_adapter=result_adapter, dispatched_at=datetime.now(timezone.utc),
            inputs=[document.artifact, *document.byte_artifacts],
            blobs={digest: base64.b64encode(raw).decode("ascii") for digest, raw in document.blobs.items()},
            call_sha256=document.artifact.sha256, provenance_profile=provenance_profile,
            transport=transport, input_bindings=list(input_bindings), control_references=list(control_references),
            scope_id=_scope.get()[1] if _scope.get() and _scope.get()[0] is self else None,
        )
        request = canonical_bytes(snapshot.model_dump(mode="json"))
        prior = self.db.execute("SELECT request FROM executions WHERE id=?", (execution_id,)).fetchone()
        if prior:
            existing = self.request(execution_id)[0]
            if existing.model_dump(exclude={"dispatched_at"}) != snapshot.model_dump(exclude={"dispatched_at"}):
                raise VatStateError("REQUEST_CONFLICT", "Execution identity has another prepared call")
            return execution_id
        with self.db:
            self.db.execute("INSERT INTO executions(id,request,state) VALUES (?,?,?)",
                            (execution_id, request, "prepared"))
            self._event(execution_id, "prepared")
        return execution_id

    def prepare_inputs(self, identity: FunctionIdentity, args: tuple, kwargs: dict, *,
                       execution_id: str | None = None, result_adapter=None,
                       transport: str = "modal", control_references=()) -> str:
        """Resolve evidence references before persisting an immutable call snapshot."""
        from .provenance import VatOutputRef, selected_output
        document, bindings, producers = resolve_inputs(args, kwargs, self.trust, limits=self.limits)
        controls = []
        for reference in control_references:
            if len(controls) >= 256:
                raise VatStateError("LIMIT_EXCEEDED", "Too many control references")
            if not isinstance(reference, VatOutputRef) or reference.selector.kind != "return":
                raise VatStateError("INVALID_SOURCE", "Control sources must be verified return references")
            if len(reference.node_bytes) > self.limits.max_node_bytes:
                raise VatStateError("LIMIT_EXCEEDED", "Control source exceeds limits")
            node = VatNode.model_validate(parse_json(reference.node_bytes))
            if not verify_node(node, self.trust, limits=self.limits).passed:
                raise VatStateError("INVALID_SOURCE", "Control source failed verification")
            selected_output(node, reference.selector, limits=self.limits)
            controls.append(ControlReference(producer_node_id=node.node_id, pointer=reference.selector.pointer))
            producers[node.node_id] = node
        if len(controls) > 256 or sum(len(n.canonical_bytes()) for n in producers.values()) > self.limits.max_node_bytes:
            raise VatStateError("LIMIT_EXCEEDED", "Prepared producer evidence exceeds limits")
        with self.db:
            for digest, node in producers.items():
                self.db.execute("INSERT OR IGNORE INTO sources VALUES (?,?)", (digest, node.canonical_bytes()))
        return self.begin(identity, document, execution_id=execution_id, result_adapter=result_adapter,
                          provenance_profile=PROFILE, transport=transport, input_bindings=bindings,
                          control_references=controls)

    def evidence_nodes(self, *, scope_id: str | None = None) -> list[VatNode]:
        """Return verified local observations and explicitly supplied producer evidence."""
        nodes = {}
        for digest, raw in self.db.execute("SELECT id,data FROM sources ORDER BY id"):
            if len(raw) > self.limits.max_node_bytes:
                raise VatStateError("JOURNAL_CORRUPT", "Stored source exceeds limits")
            node = VatNode.model_validate(parse_json(raw))
            if node.node_id != digest or not verify_node(node, self.trust, limits=self.limits).passed:
                raise VatStateError("JOURNAL_CORRUPT", "Stored source failed verification")
            nodes[digest] = node
        roots = []
        for execution in self.executions():
            node = self.completed(execution["execution_id"])
            if node:
                nodes[node.node_id] = node
                if scope_id is None or self.request(execution["execution_id"])[0].scope_id == scope_id:
                    roots.append(node.node_id)
        selected, pending, reference_count = {}, roots, 0
        while pending:
            digest = pending.pop()
            if digest in selected or digest not in nodes:
                continue
            selected[digest] = nodes[digest]
            if len(selected) > 10_000:
                raise VatStateError("LIMIT_EXCEEDED", "Too many producer nodes")
            statement = nodes[digest].statement()
            reference_count += len(statement.input_bindings) + len(statement.control_references)
            if reference_count > 100_000:
                raise VatStateError("LIMIT_EXCEEDED", "Too many producer references")
            pending.extend(b.producer_node_id for b in [*statement.input_bindings, *statement.control_references])
        return list(selected.values())

    def lifecycle(self, *, scope_id: str | None = None) -> list[dict]:
        """Observed state transitions for the selected scope, without argument bytes."""
        identifiers = {entry["execution_id"] for entry in self.executions()
                       if scope_id is None or self.request(entry["execution_id"])[0].scope_id == scope_id}
        return [{"execution_id": identifier, "state": state, "observed_at": timestamp}
                for identifier, state, timestamp in self.db.execute(
                    "SELECT execution_id,state,observed_at FROM lifecycle ORDER BY ordinal") if identifier in identifiers]

    def accepted(self, execution_id: str, call_id: str) -> None:
        """Persist the platform call identity before returning its handle."""
        execution_id = str(UUID(execution_id))
        if not isinstance(call_id, str) or not call_id or len(call_id) > 256:
            raise VatStateError("SUBMISSION_UNCERTAIN", "Modal returned no usable call identity.")
        with self.db:
            cursor = self.db.execute(
                "UPDATE executions SET state='running',call_id=? WHERE id=? AND state='submitting'",
                (call_id, execution_id),
            )
            if cursor.rowcount == 1:
                self._event(execution_id, "running")
        if cursor.rowcount != 1:
            raise VatStateError("REQUEST_CONFLICT", "Execution cannot accept another call identity.")

    def recovered_acceptance(self, execution_id: str, call_id: str) -> None:
        """Adopt an identity only after a provider recovery adapter validates its manifest."""
        _, state, prior, _ = self.request(execution_id)
        if prior:
            if prior != call_id:
                raise VatStateError("REQUEST_CONFLICT", "Recovered call identity differs.")
            return
        if (state not in {"submitting", "submission_uncertain"} or not isinstance(call_id, str)
                or not call_id or len(call_id) > 256):
            raise VatStateError("STATE_CONFLICT", "Execution cannot adopt recovered acceptance.")
        with self.db:
            self.db.execute("UPDATE executions SET state='running',call_id=? WHERE id=? AND state=?",
                            (call_id, str(UUID(execution_id)), state))
            self._event(str(UUID(execution_id)), "running")

    def set_state(self, execution_id: str, state: str) -> None:
        """Apply only an allowed compare-and-swap execution transition."""
        current = self.request(execution_id)[1]
        allowed = {
            "prepared": {"submitting"},
            "submitting": {"submission_uncertain"},
            "running": {"observation_pending"},
            "observation_pending": {"running"},
        }
        if state == current:
            return
        if state not in allowed.get(current, set()):
            raise VatStateError("STATE_CONFLICT", f"Invalid execution transition: {current} to {state}.")
        with self.db:
            changed = self.db.execute("UPDATE executions SET state=? WHERE id=? AND state=?",
                                      (state, str(UUID(execution_id)), current)).rowcount
            if changed == 1:
                self._event(str(UUID(execution_id)), state)
        if changed != 1:
            raise VatStateError("STATE_CONFLICT", "Execution state changed concurrently.")

    def request(self, execution_id: str) -> tuple[_RequestSnapshot, str, str | None, bool]:
        execution_id = str(UUID(execution_id))
        row = self.db.execute(
            "SELECT request,state,call_id,cancel_requested FROM executions WHERE id=?", (execution_id,)
        ).fetchone()
        if row is None:
            raise VatStateError("EXECUTION_NOT_FOUND", "Execution is not in this recorder.")
        try:
            if len(row[0]) > self.limits.max_node_bytes:
                raise ValueError("oversized request")
            snapshot = _RequestSnapshot.model_validate(parse_json(row[0]))
            if canonical_bytes(snapshot.model_dump(mode="json")) != row[0]:
                raise ValueError("noncanonical request")
        except (ValueError, ValidationError) as exc:
            raise VatStateError("JOURNAL_CORRUPT", "Stored execution request is invalid.") from exc
        return snapshot, row[1], row[2], bool(row[3])

    def request_cancel(self, execution_id: str) -> None:
        execution_id = str(UUID(execution_id))
        with self.db:
            cursor = self.db.execute("UPDATE executions SET cancel_requested=1 WHERE id=?", (execution_id,))
            if cursor.rowcount == 1:
                self._event(execution_id, "cancel_requested")
        if cursor.rowcount != 1:
            raise VatStateError("EXECUTION_NOT_FOUND", "Execution is not in this recorder.")

    def _node_path(self, execution_id: str, *, create: bool) -> Path:
        execution_id = str(UUID(execution_id))
        directory = self.root / execution_id
        if directory.is_symlink():
            raise VatStateError("UNSAFE_PATH", "Execution directory cannot be a symlink.")
        if create:
            directory.mkdir(mode=0o700, exist_ok=True)
            os.chmod(directory, 0o700)
        elif not directory.is_dir():
            raise VatStateError("JOURNAL_CORRUPT", "Completed execution directory is missing.")
        path = directory / "node.json"
        if path.is_symlink():
            raise VatStateError("UNSAFE_PATH", "Stored node cannot be a symlink.")
        return path

    def complete(self, execution_id: str, node: VatNode) -> None:
        """Persist exactly one verified terminal node and its completed state."""
        execution_id = str(UUID(execution_id))
        report = verify_node(node, self.trust, limits=self.limits)
        request, state, call_id, _cancel = self.request(execution_id)
        if state not in {"running", "observation_pending", "completed"}:
            raise VatStateError("STATE_CONFLICT", "Execution is not accepted.")
        statement = node.statement()
        if (not report.passed or str(statement.execution_id) != execution_id
                or statement.function != request.function or statement.inputs != request.inputs
                or statement.result_adapter != request.result_adapter
                or statement.dispatched_at != request.dispatched_at
                or statement.transport != request.transport
                or statement.input_bindings != request.input_bindings
                or statement.control_references != request.control_references
                or statement.provenance_profile != request.provenance_profile
                or statement.modal_call_id != (call_id if request.transport == "modal" else None)):
            raise VatStateError("INVALID_NODE", "Recorder refused an invalid or mismatched node.")
        path = self._node_path(execution_id, create=True)
        if path.exists():
            existing = VatNode.load(path, limits=self.limits)
            if existing.canonical_bytes() != node.canonical_bytes():
                raise VatStateError("REQUEST_CONFLICT", "Execution already has a different terminal node.")
        else:
            node.save(path, limits=self.limits)
        with self.db:
            cursor = self.db.execute(
                "UPDATE executions SET state='completed' WHERE id=? AND state!='submission_uncertain'",
                (execution_id,),
            )
            if state != "completed" and cursor.rowcount == 1:
                self._event(execution_id, "completed")
        if cursor.rowcount != 1:
            raise VatStateError("REQUEST_CONFLICT", "Execution state cannot accept a terminal node.")

    def completed(self, execution_id: str) -> VatNode | None:
        snapshot, state, call_id, _cancel = self.request(execution_id)
        path = self.root / str(UUID(execution_id)) / "node.json"
        if state != "completed" and not path.exists():
            return None
        path = self._node_path(execution_id, create=False)
        if not path.is_file():
            raise VatStateError("JOURNAL_CORRUPT", "Completed execution has no node.")
        try:
            node = VatNode.load(path, limits=self.limits)
            report = verify_node(node, self.trust, limits=self.limits)
        except (NodeError, ValueError) as exc:
            raise VatStateError("JOURNAL_CORRUPT", "Stored terminal node is invalid.") from exc
        statement = node.statement()
        if (not report.passed or str(statement.execution_id) != str(UUID(execution_id))
                or statement.function != snapshot.function or statement.inputs != snapshot.inputs
                or statement.result_adapter != snapshot.result_adapter
                or statement.dispatched_at != snapshot.dispatched_at
                or statement.transport != snapshot.transport
                or statement.input_bindings != snapshot.input_bindings
                or statement.control_references != snapshot.control_references
                or statement.provenance_profile != snapshot.provenance_profile
                or statement.modal_call_id != (call_id if snapshot.transport == "modal" else None)):
            raise VatStateError("JOURNAL_CORRUPT", "Stored terminal node failed verification.")
        if state != "completed":
            if state not in {"running", "observation_pending"}:
                raise VatStateError("JOURNAL_CORRUPT", "Unexpected terminal node for journal state.")
            with self.db:
                self.db.execute("UPDATE executions SET state='completed' WHERE id=?",
                                (str(UUID(execution_id)),))
                self._event(str(UUID(execution_id)), "completed")
        return node
    def executions(self) -> list[dict]:
        """List durable execution identities without exposing input bytes."""
        return [dict(execution_id=id, state=state, call_id=call_id, cancel_requested=bool(cancel))
                for id, state, call_id, cancel in self.db.execute(
                    "SELECT id,state,call_id,cancel_requested FROM executions ORDER BY rowid")]

    def observe(self, execution_id: str, value: dict) -> dict:
        """Persist the first terminal observation before adaptation or signing."""
        existing = self.observation(execution_id)
        if existing is not None:
            # Competing results must never silently replace the committed observation.
            # Collection and volume recovery can observe the same outcome at different times.
            # Compare the outcome, retaining the first observation's original timing metadata.
            fields = ("status", "value", "error")
            first = encode_document({key: existing[key] for key in fields}, "observation.json", limits=self.limits)
            second = encode_document({key: value[key] for key in fields}, "observation.json", limits=self.limits)
            if first.artifact.sha256 != second.artifact.sha256:
                raise VatStateError("OBSERVATION_CONFLICT", "Execution has a different terminal observation.")
            return existing
        document = encode_document(value, "observation.json", limits=self.limits)
        data = canonical_bytes({
            "artifact": document.artifact.model_dump(mode="json"),
            "byte_artifacts": [a.model_dump(mode="json") for a in document.byte_artifacts],
            "blobs": {k: base64.b64encode(v).decode("ascii") for k, v in document.blobs.items()},
        })
        with self.db:
            self.db.execute("INSERT INTO observations VALUES (?,?)", (str(UUID(execution_id)), data))
        return value

    def observation(self, execution_id: str) -> dict | None:
        row = self.db.execute("SELECT data FROM observations WHERE id=?", (str(UUID(execution_id)),)).fetchone()
        if row is None:
            return None
        try:
            if len(row[0]) > self.limits.max_node_bytes:
                raise ValueError("oversized observation")
            data = parse_json(row[0])
            if canonical_bytes(data) != row[0]:
                raise ValueError("noncanonical observation")
            blobs = {k: base64.b64decode(v, validate=True) for k, v in data["blobs"].items()}
            return decode_document(Artifact.model_validate(data["artifact"]),
                                   [Artifact.model_validate(a) for a in data["byte_artifacts"]],
                                   blobs, limits=self.limits)
        except (ValueError, KeyError, TypeError) as exc:
            raise VatStateError("JOURNAL_CORRUPT", "Stored observation failed validation.") from exc

    def finish(self, execution_id: str, observation: dict, result_adapter=None) -> VatResult:
        """Finalize persisted observations identically for local and Modal calls."""
        existing = self.completed(execution_id)
        observation = self.observe(execution_id, observation)
        if existing:
            return VatResult(existing, self.trust, self.limits)
        request, _, call_id, _ = self.request(execution_id)
        if adapter_identity(result_adapter) != request.result_adapter:
            raise VatStateError("EVIDENCE_FAILED", "Result adapter changed after preparation; observation is retained.")
        blobs = request.raw_blobs()
        results, intermediates = [], []
        adapted = AdaptedResult()
        recorded_adapter = request.result_adapter
        if observation["status"] == "succeeded":
            document = encode_document(observation["value"], "return.json", limits=self.limits)
            results = [document.artifact, *document.byte_artifacts]
            blobs.update(document.blobs)
            adapter = result_adapter
            if adapter is not None:
                call_doc = decode_document(request.inputs[0], request.inputs[1:], request.raw_blobs(),
                                           limits=self.limits)
                context = CaptureContext(execution_id, call_id, request.function,
                                         tuple(call_doc["args"]), call_doc["kwargs"])
                try:
                    adapted = adapter(copy.deepcopy(observation["value"]), context)
                    recorded_adapter = adapter_identity(result_adapter)
                    if recorded_adapter != request.result_adapter:
                        raise ValueError("Adapter identity changed after preparation.")
                except Exception as exc:
                    # This is a plugin boundary; preserve observation and never redispatch on adapter failure.
                    raise VatStateError("EVIDENCE_FAILED", "Result adapter failed; stored observation is retained.") from exc
            names = {a.name for a in request.inputs + results}
            for item in adapted.artifacts:
                if (not safe_artifact_name(item.name) or item.name in names
                        or item.name.startswith(("call-bytes-", "return-bytes-"))
                        or item.kind == "vat_node" or type(item.data) is not bytes
                        or item.classification not in {"output", "intermediate"}):
                    raise VatStateError("EVIDENCE_FAILED", "Invalid explicit artifact.")
                names.add(item.name)
                artifact = Artifact(name=item.name, kind=item.kind, sha256=sha256_hex(item.data),
                                    bytes_len=len(item.data))
                (results if item.classification == "output" else intermediates).append(artifact)
                blobs[artifact.sha256] = item.data
        stopped = datetime.fromisoformat(observation["observed_at"])
        statement = ExecutionStatement(
            execution_id=UUID(execution_id), status=observation["status"], function=request.function,
            modal_call_id=call_id if request.transport == "modal" else None,
            provenance_profile=request.provenance_profile, transport=request.transport,
            input_bindings=request.input_bindings, control_references=request.control_references,
            dispatched_at=request.dispatched_at, recorded_at=stopped,
            timestamps=Timestamps(stage_start=request.dispatched_at, stage_stop=stopped,
                                  monotonic_ns=observation["monotonic_ns"]),
            timestamp_basis="local_observation", inputs=request.inputs, results=results,
            intermediates=intermediates, domain_outcome=adapted.domain_outcome,
            result_adapter=recorded_adapter,
            exception=SafeException.model_validate(observation["error"]) if observation.get("error") else None,
            environment=EnvSnapshot(assurance_tier=AssuranceTier.SUBPROCESS_WRAPPER,
                                    python=platform.python_version(), platform=platform.platform(),
                                    modal_call_id=call_id if request.transport == "modal" else None),
        )
        node = VatNode.sign(statement, blobs, self.signer, limits=self.limits)
        self.complete(execution_id, node)
        return VatResult(node, self.trust, self.limits)
