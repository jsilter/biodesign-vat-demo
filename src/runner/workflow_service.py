"""Durable execution of approved data-only scientific workflows."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from vatlog.node import NodeStatus, decode_document
from vatlog.provenance import resolve_inputs

from .agent import Event
from .design_inputs import DesignError
from .design_store import DesignStore, json_bytes
from .evidence import FILES, capture
from .modal_client import ModalTools
from .tool_contracts import validate_tool_request
from .workflow_contracts import WorkflowProposal, dry_run, replace_binding, validate_workflow


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkflowService:
    """The backend's single owner for proposal approval, dispatch and recovery."""

    def __init__(self, store: DesignStore, transport=None):
        self.store = store
        self.transport = transport or ModalTools()
        if isinstance(self.transport, ModalTools):
            self.transport.bind_store(store)
        self.tasks: dict[str, asyncio.Task] = {}
        self.submissions: dict[str, asyncio.Task] = {}

    def propose(self, value: object) -> dict:
        proposal = validate_workflow(value)
        state = {
            "id": proposal.id, "digest": proposal.digest, "status": "proposed",
            "approved_digest": None, "spend_authorized": False, "cancel_requested": False,
            "created": now(), "started": None, "finished": None, "evidence_status": "unpublished",
            "nodes": [{"id": node.id, "tool_id": node.tool_id, "execution_id": node.execution_id,
                       "status": "pending", "call_id": None, "node_id": None,
                       "error": None} for node in proposal.nodes],
        }
        created = self.store.create_synthesized(proposal.id, proposal.model_dump(mode="json"), state)
        if not created:
            _, state = self.store.get_synthesized(proposal.id)
        return self._public(state)

    def dry_run(self, value: object) -> dict:
        return dry_run(value)

    def approve(self, id: str, digest: str, *, spend_authorized: bool) -> dict:
        raw, state = self.store.get_synthesized(id)
        proposal = validate_workflow(raw)
        if digest != proposal.digest:
            raise DesignError("APPROVAL_MISMATCH", "Approval digest differs from the stored proposal.")
        if not all(node.authorized for node in proposal.nodes):
            raise DesignError("AUTHORIZATION_REQUIRED", "Every remote node requires explicit authorization.")
        if state["status"] not in {"proposed", "approved"}:
            raise DesignError("REQUEST_CONFLICT", "Workflow is already executing or terminal.")
        state.update(status="approved", approved_digest=digest,
                     spend_authorized=bool(spend_authorized))
        self.store.save_synthesized(id, state)
        return self._public(state)

    def start(self, id: str) -> dict:
        raw, state = self.store.get_synthesized(id)
        proposal = validate_workflow(raw)
        if state["approved_digest"] != proposal.digest:
            raise DesignError("AUTHORIZATION_REQUIRED", "Approve the exact workflow before execution.")
        if not state["spend_authorized"]:
            raise DesignError("SPEND_AUTHORIZATION_REQUIRED", "Separate owner spend authorization is required.")
        if not self.transport.configured:
            raise DesignError("TOOL_UNAVAILABLE", "Modal tools are not enabled.")
        if state["status"] == "running":
            return self._public(state)
        if state["status"] != "approved":
            raise DesignError("REQUEST_CONFLICT", "Workflow cannot be started from its current state.")
        state.update(status="running", started=now())
        self.store.save_synthesized(id, state)
        task = asyncio.create_task(self._run(proposal))
        self.tasks[id] = task
        task.add_done_callback(lambda completed: self._finished(id, completed))
        return self.snapshot(id)

    def snapshot(self, id: str) -> dict:
        _, state = self.store.get_synthesized(id)
        return self._public(state)

    @staticmethod
    def _public(state: dict) -> dict:
        return {key: value for key, value in state.items()
                if key not in {"approved_digest", "spend_authorized", "cancel_requested"}} | {
            "approved": state.get("approved_digest") == state.get("digest"),
            "spend_authorized": bool(state.get("spend_authorized")),
        }

    def _finished(self, id: str, task: asyncio.Task) -> None:
        if task.cancelled() or task.exception() is None:
            return
        _, state = self.store.get_synthesized(id)
        if state["status"] == "running":
            state.update(status="needs_attention", finished=now())
            self.store.save_synthesized(id, state)

    def _bound_request(self, proposal: WorkflowProposal, node) -> dict:
        request = node.request
        for binding in node.bindings:
            producer = next(item for item in proposal.nodes if item.id == binding.producer)
            result = self.transport.result(producer.execution_id)
            if result is None:
                raise DesignError("DEPENDENCY_UNAVAILABLE", "Producer result is unavailable.")
            reference = (result.ref(binding.selector.pointer) if binding.selector.kind == "return"
                         else result.artifact_ref(binding.selector.name,
                                                  allow_intermediate=binding.allow_intermediate,
                                                  encoding=binding.selector.encoding))
            request = replace_binding(request, binding.destination, reference)
        if isinstance(self.transport, ModalTools):
            document, _, _ = resolve_inputs((node.execution_id, request), {}, self.transport.recorder.trust,
                                             limits=self.transport.recorder.limits)
            decoded = decode_document(document.artifact, list(document.byte_artifacts),
                                      document.blobs, limits=self.transport.recorder.limits)
            resolved = decoded["args"][1]
            # Text scientific inputs may arrive as exact UTF-8 artifact bytes.
            resolved = _decode_text_inputs(resolved)
            validate_tool_request(resolved, expected_tool=node.tool_id)
        return request

    async def _run(self, proposal: WorkflowProposal) -> None:
        for node in proposal.nodes:
            _, state = self.store.get_synthesized(proposal.id)
            item = next(entry for entry in state["nodes"] if entry["id"] == node.id)
            if item["status"] in {"completed", "failed", "cancelled", "blocked"}:
                continue
            dependencies = {binding.producer for binding in node.bindings} | set(node.controls)
            failed = [entry for entry in state["nodes"]
                      if entry["id"] in dependencies and entry["status"] != "completed"]
            if failed:
                item.update(status="blocked", error="DEPENDENCY_UNAVAILABLE")
                self.store.save_synthesized(proposal.id, state)
                continue
            if state["cancel_requested"]:
                item.update(status="cancelled", error="CANCELLED")
                self.store.save_synthesized(proposal.id, state)
                continue
            try:
                request = self._bound_request(proposal, node)
                controls = []
                for producer_id in node.controls:
                    producer = next(candidate for candidate in proposal.nodes if candidate.id == producer_id)
                    result = self.transport.result(producer.execution_id)
                    if result is None:
                        raise DesignError("DEPENDENCY_UNAVAILABLE", "Control producer is unavailable.")
                    controls.append(result.ref())
                self.transport.prepare(node.tool_id, node.execution_id, request,
                                       control_references=controls)
                item.update(status="submitting")
                self.store.save_synthesized(proposal.id, state)
                submission = asyncio.create_task(
                    self.transport.submit(node.tool_id, node.execution_id, request))
                self.submissions[node.execution_id] = submission
                try:
                    call_id = await asyncio.shield(submission)
                except (OSError, TimeoutError, DesignError) as exc:
                    _, state = self.store.get_synthesized(proposal.id)
                    item = next(entry for entry in state["nodes"] if entry["id"] == node.id)
                    item.update(status="uncertain", error="SUBMISSION_UNCERTAIN")
                    state.update(status="needs_attention", finished=now())
                    self.store.save_synthesized(proposal.id, state)
                    raise DesignError("SUBMISSION_UNCERTAIN", "Submission acceptance remains uncertain.") from exc
                _, state = self.store.get_synthesized(proposal.id)
                item = next(entry for entry in state["nodes"] if entry["id"] == node.id)
                item.update(status="running", call_id=call_id)
                self.store.save_synthesized(proposal.id, state)
                while True:
                    result_value = await self.transport.poll(call_id)
                    if result_value is not None:
                        break
                    await asyncio.sleep(1)
                result = self.transport.result(node.execution_id)
                if result is None:
                    raise DesignError("EVIDENCE_FAILED", "Terminal execution has no signed VatResult.")
                _, state = self.store.get_synthesized(proposal.id)
                item = next(entry for entry in state["nodes"] if entry["id"] == node.id)
                domain_failed = (isinstance(result_value, dict) and bool(result_value.get("error")))
                status = "completed" if result.status is NodeStatus.SUCCEEDED and not domain_failed else "failed"
                item.update(status=status, node_id=result.node_id,
                            error=result_value.get("error") if domain_failed else None)
                self.store.save_synthesized(proposal.id, state)
            except DesignError:
                _, state = self.store.get_synthesized(proposal.id)
                item = next(entry for entry in state["nodes"] if entry["id"] == node.id)
                if item["status"] not in {"uncertain", "failed"}:
                    item.update(status="failed", error="EXECUTION_FAILED")
                    self.store.save_synthesized(proposal.id, state)
        _, state = self.store.get_synthesized(proposal.id)
        if state["status"] != "needs_attention":
            statuses = {item["status"] for item in state["nodes"]}
            state.update(status="cancelled" if state["cancel_requested"] else
                         "completed" if statuses == {"completed"} else "partial", finished=now())
            self.store.save_synthesized(proposal.id, state)
            self._publish(proposal)

    def _publish(self, proposal: WorkflowProposal) -> None:
        _, state = self.store.get_synthesized(proposal.id)
        events: list[Event] = []
        execution_nodes = {}
        for node, item in zip(proposal.nodes, state["nodes"], strict=True):
            result = self.transport.result(node.execution_id)
            if result is None:
                continue
            timestamp = datetime.fromisoformat(state["started"] or state["created"])
            events.append(Event(node.tool_id, json_bytes(node.request), json_bytes({
                "execution_id": node.execution_id, "node_id": result.node_id, "status": item["status"]}),
                timestamp, datetime.now(timezone.utc), {"tool": node.tool_id,
                "workflow_node": node.id, "status": item["status"]}))
            execution_nodes[len(events) - 1] = result
        if not events:
            return
        directory = capture(self.store.root / "published", proposal.id,
                            json_bytes(proposal.model_dump(mode="json")).decode(), events,
                            "Synthesized workflow evidence; scientific conclusions require separate review.",
                            datetime.fromisoformat(state["started"] or state["created"]),
                            purpose="Owner-approved synthesized scientific workflow",
                            keys=self.store.keys(), execution_nodes=execution_nodes,
                            publication_root=self.store.root)
        for name in FILES:
            self.store.artifact(proposal.id, name, "evidence", (directory / name).read_bytes())
        _, state = self.store.get_synthesized(proposal.id)
        state["evidence_status"] = "verified"
        self.store.save_synthesized(proposal.id, state)

    async def cancel(self, id: str) -> dict:
        _, state = self.store.get_synthesized(id)
        state["cancel_requested"] = True
        for item in state["nodes"]:
            if item["status"] == "running" and item["call_id"]:
                await self.transport.cancel(item["call_id"])
            elif item["status"] == "submitting" and not item["call_id"]:
                state["status"] = "needs_attention"
        self.store.save_synthesized(id, state)
        return self._public(state)

    async def reconcile(self) -> None:
        """Resume observation only for accepted calls; never replay an uncertain submit."""
        for raw, state in self.store.all_synthesized():
            if state["status"] not in {"running", "recovering"}:
                continue
            if any(item["status"] in {"submitting", "uncertain"} and not item["call_id"]
                   for item in state["nodes"]):
                state["status"] = "needs_attention"
                self.store.save_synthesized(state["id"], state)
                continue
            proposal = validate_workflow(raw)
            self.tasks[proposal.id] = asyncio.create_task(self._run(proposal))

    async def shutdown(self) -> None:
        await asyncio.gather(*(asyncio.shield(task) for task in self.submissions.values()),
                             return_exceptions=True)
        for task in self.tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        if hasattr(self.transport, "drain"):
            await self.transport.drain()


def _decode_text_inputs(value):
    """Decode bound UTF-8 artifacts only for request validation, preserving captured bytes."""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DesignError("INVALID_WORKFLOW", "Bound text artifact is not UTF-8.") from exc
    if isinstance(value, list):
        return [_decode_text_inputs(item) for item in value]
    if isinstance(value, dict):
        return {key: _decode_text_inputs(item) for key, item in value.items()}
    return value
