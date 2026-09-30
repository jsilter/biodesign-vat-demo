"""Approval-bound durable service for the fixed generated CPU launcher."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from .agent import Event
from .design_inputs import DesignError
from .design_store import DesignStore, json_bytes
from .dynamic_client import DynamicModalTools
from .dynamic_contracts import validate_dynamic_output, validate_dynamic_proposal
from .evidence import FILES, capture


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DynamicJobService:
    def __init__(self, store: DesignStore, transport=None):
        self.store = store
        self.transport = transport or DynamicModalTools()
        if isinstance(self.transport, DynamicModalTools):
            self.transport.bind_store(store)
        self.tasks: dict[str, asyncio.Task] = {}
        self.submissions: dict[str, asyncio.Task] = {}

    def propose(self, value: object) -> dict:
        proposal = validate_dynamic_proposal(value)
        state = {"id": proposal.id, "execution_id": proposal.id, "digest": proposal.digest,
                 "status": "proposed", "approved_digest": None, "spend_authorized": False,
                 "call_id": None, "node_id": None, "error": None, "artifacts": [],
                 "created": now(), "started": None, "finished": None, "evidence_status": "unpublished"}
        created = self.store.create_dynamic(proposal.id, proposal.model_dump(mode="json"), state)
        if not created:
            _, state = self.store.get_dynamic(proposal.id)
        return self._public(state)

    def dry_run(self, value: object) -> dict:
        proposal = validate_dynamic_proposal(value)
        return {"version": proposal.version, "job_id": proposal.id, "digest": proposal.digest,
                "source_sha256": __import__("hashlib").sha256(proposal.source.encode()).hexdigest(),
                "entrypoint": proposal.entrypoint, "packages": proposal.packages,
                "resources": proposal.resources.model_dump(mode="json"),
                "dispatchable": proposal.authorized,
                "image": {"family": "debian-slim", "python": "3.12"},
                "launcher": "vat-dynamic-launcher/1"}

    def approve(self, id: str, digest: str, *, spend_authorized: bool) -> dict:
        raw, state = self.store.get_dynamic(id)
        proposal = validate_dynamic_proposal(raw)
        if digest != proposal.digest:
            raise DesignError("APPROVAL_MISMATCH", "Approval digest differs from the generated-job proposal.")
        if not proposal.authorized:
            raise DesignError("AUTHORIZATION_REQUIRED", "Generated source requires explicit owner authorization.")
        if state["status"] not in {"proposed", "approved"}:
            raise DesignError("REQUEST_CONFLICT", "Generated job is already executing or terminal.")
        state.update(status="approved", approved_digest=digest,
                     spend_authorized=bool(spend_authorized))
        self.store.save_dynamic(id, state)
        return self._public(state)

    def start(self, id: str) -> dict:
        raw, state = self.store.get_dynamic(id)
        proposal = validate_dynamic_proposal(raw)
        if state["approved_digest"] != proposal.digest:
            raise DesignError("AUTHORIZATION_REQUIRED", "Approve the exact generated source and settings.")
        if not state["spend_authorized"]:
            raise DesignError("SPEND_AUTHORIZATION_REQUIRED", "Separate owner spend authorization is required.")
        if not self.transport.configured:
            raise DesignError("TOOL_UNAVAILABLE", "Generated Modal jobs are not enabled.")
        if state["status"] == "running":
            return self._public(state)
        if state["status"] != "approved":
            raise DesignError("REQUEST_CONFLICT", "Generated job cannot start from its current state.")
        self.transport.prepare(proposal.id, proposal.model_dump(mode="json"))
        state.update(status="submitting", started=now())
        self.store.save_dynamic(id, state)
        task = asyncio.create_task(self._execute(proposal))
        self.tasks[id] = task
        task.add_done_callback(lambda completed: self._finished(id, completed))
        return self.snapshot(id)

    def snapshot(self, id: str) -> dict:
        _, state = self.store.get_dynamic(id)
        return self._public(state)

    @staticmethod
    def _public(state: dict) -> dict:
        return {key: value for key, value in state.items()
                if key not in {"approved_digest", "spend_authorized"}} | {
            "approved": state.get("approved_digest") == state.get("digest"),
            "spend_authorized": bool(state.get("spend_authorized")),
        }

    def _finished(self, id: str, task: asyncio.Task) -> None:
        if task.cancelled() or task.exception() is None:
            return
        _, state = self.store.get_dynamic(id)
        if state["status"] in {"submitting", "running"}:
            state.update(status="needs_attention", error="UNEXPECTED_FAILURE", finished=now())
            self.store.save_dynamic(id, state)

    async def _execute(self, proposal) -> None:
        _, state = self.store.get_dynamic(proposal.id)
        if state["status"] == "submitting":
            submission = asyncio.create_task(self.transport.submit(
                proposal.id, proposal.model_dump(mode="json")))
            self.submissions[proposal.id] = submission
            try:
                call_id = await asyncio.shield(submission)
            except (OSError, TimeoutError, DesignError) as exc:
                _, state = self.store.get_dynamic(proposal.id)
                state.update(status="needs_attention", error="SUBMISSION_UNCERTAIN", finished=now())
                self.store.save_dynamic(proposal.id, state)
                raise DesignError("SUBMISSION_UNCERTAIN", "Generated-job acceptance remains uncertain.") from exc
            _, state = self.store.get_dynamic(proposal.id)
            state.update(status="running", call_id=call_id)
            self.store.save_dynamic(proposal.id, state)
        call_id = state["call_id"]
        while True:
            output = await self.transport.poll(call_id)
            if output is not None:
                break
            await asyncio.sleep(1)
        result = self.transport.result(proposal.id)
        if result is None:
            raise DesignError("EVIDENCE_FAILED", "Generated call has no signed VatResult.")
        _, state = self.store.get_dynamic(proposal.id)
        if isinstance(output, dict) and output.get("error"):
            state.update(status="cancelled" if output["error"] == "CANCELLED" else "failed",
                         error=output["error"], node_id=result.node_id, finished=now())
        else:
            validate_dynamic_output(output)
            refs = [self.store.artifact(proposal.id, artifact.name, artifact.kind,
                                        artifact.read_bytes()) for artifact in result.artifacts]
            state.update(status="completed", node_id=result.node_id, artifacts=refs, finished=now())
        self.store.save_dynamic(proposal.id, state)
        self._publish(proposal, result)

    def _publish(self, proposal, result) -> None:
        _, state = self.store.get_dynamic(proposal.id)
        started = datetime.fromisoformat(state["started"] or state["created"])
        event = Event("dynamic-job", json_bytes(proposal.model_dump(mode="json")),
                      json_bytes({"node_id": result.node_id, "status": state["status"]}),
                      started, datetime.now(timezone.utc), {"status": state["status"],
                      "launcher": "vat-dynamic-launcher/1"})
        directory = capture(self.store.root / "published-dynamic", proposal.id,
                            "Owner-approved generated CPU job.", [event],
                            "Generated CPU output captured; scientific validity was not inferred.",
                            started, purpose="Owner-approved bounded generated CPU job",
                            keys=self.store.keys(), execution_nodes={0: result},
                            publication_root=self.store.root)
        for name in FILES:
            self.store.artifact(proposal.id, name, "evidence", (directory / name).read_bytes())
        _, state = self.store.get_dynamic(proposal.id)
        state["evidence_status"] = "verified"
        self.store.save_dynamic(proposal.id, state)

    async def cancel(self, id: str) -> dict:
        _, state = self.store.get_dynamic(id)
        if state["status"] == "running" and state["call_id"]:
            await self.transport.cancel(state["call_id"])
        elif state["status"] == "submitting" and not state["call_id"]:
            state.update(status="needs_attention", error="SUBMISSION_UNCERTAIN")
            self.store.save_dynamic(id, state)
        return self.snapshot(id)

    async def reconcile(self) -> None:
        for raw, state in self.store.all_dynamic():
            if state["status"] == "submitting" and not state["call_id"]:
                state.update(status="needs_attention", error="SUBMISSION_UNCERTAIN")
                self.store.save_dynamic(state["id"], state)
            elif state["status"] == "running" and state["call_id"]:
                proposal = validate_dynamic_proposal(raw)
                self.tasks[proposal.id] = asyncio.create_task(self._execute(proposal))

    async def shutdown(self) -> None:
        await asyncio.gather(*(asyncio.shield(task) for task in self.submissions.values()),
                             return_exceptions=True)
        for task in self.tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        if hasattr(self.transport, "drain"):
            await self.transport.drain()
