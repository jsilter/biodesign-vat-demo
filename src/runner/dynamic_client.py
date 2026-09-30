"""Durable caller for the one fixed generated-job Modal function."""

from __future__ import annotations

import os

from vatlog.execution import VatObservationPending, VatRecorder, VatStateError
from vatlog.modal import PreparedVatCall, VatFunction
from vatlog.node import FunctionIdentity, NodeStatus
from vatlog.provenance import resolve_inputs
from vatlog.result import VatResult

from .design_inputs import DesignError
from .design_store import DesignStore
from .dynamic_modal_app import APP_NAME, FUNCTION_NAME
from .dynamic_worker import dynamic_result_adapter


class DynamicModalTools:
    """A fixed-function transport; generated source never supplies its own adapter."""

    def __init__(self):
        self.recorder: VatRecorder | None = None

    def bind_store(self, store: DesignStore) -> None:
        existing = getattr(store, "execution_recorder", None)
        self.recorder = existing or VatRecorder(store.root / "executions", store.keys().recorder)
        store.execution_recorder = self.recorder

    @property
    def configured(self) -> bool:
        return os.environ.get("VAT_MODAL_ENABLED") == "1"

    def _bound(self):
        if self.recorder is None:
            raise DesignError("EVIDENCE_FAILED", "Generated-job recorder has not been bound.")
        identity = FunctionIdentity(app_name=APP_NAME, function_name=FUNCTION_NAME)
        return VatFunction.from_name(APP_NAME, FUNCTION_NAME, descriptor=identity,
                                     result_adapter=dynamic_result_adapter).with_recorder(self.recorder)

    def prepare(self, id: str, proposal: dict) -> None:
        self._bound().prepare_call((id, proposal), {}, execution_id=id)

    async def submit(self, id: str, proposal: dict) -> str:
        if not self.configured:
            raise DesignError("TOOL_UNAVAILABLE", "Generated Modal jobs are not enabled.")
        request = self.recorder.request(id)[0]
        supplied, _, _ = resolve_inputs((id, proposal), {}, self.recorder.trust,
                                         limits=self.recorder.limits)
        if request.call_sha256 != supplied.artifact.sha256:
            raise DesignError("REQUEST_CONFLICT", "Submission differs from the approved generated job.")
        try:
            call = await PreparedVatCall(self._bound(), id).submit.aio()
            return call.object_id
        except (VatStateError, ValueError) as exc:
            raise DesignError("SUBMISSION_UNCERTAIN", "Generated-job acceptance is uncertain.") from exc

    def _execution(self, call_id: str):
        entries = [entry for entry in self.recorder.executions() if entry["call_id"] == call_id]
        if len(entries) != 1:
            raise DesignError("EXECUTION_NOT_FOUND", "Generated call has no unique execution.")
        return entries[0]["execution_id"]

    async def poll(self, call_id: str) -> dict | None:
        id = self._execution(call_id)
        try:
            result = await self._bound().resume(id).get.aio(timeout=0)
        except VatObservationPending as exc:
            if exc.code in {"PENDING", "OBSERVATION_PENDING"}:
                return None
            raise DesignError("CONNECTION_LOST", "Generated call remains unresolved.") from exc
        except (VatStateError, ValueError) as exc:
            raise DesignError("EVIDENCE_FAILED", "Generated result failed VAT collection.") from exc
        if result.status is NodeStatus.SUCCEEDED:
            return result.value
        return {"error": "CANCELLED" if result.status is NodeStatus.CANCELLED else "EXECUTION_FAILED"}

    def result(self, id: str) -> VatResult | None:
        node = self.recorder.completed(id)
        return VatResult(node, self.recorder.trust, self.recorder.limits) if node else None

    async def cancel(self, call_id: str) -> None:
        id = self._execution(call_id)
        try:
            await self._bound().resume(id).cancel.aio()
        except VatObservationPending as exc:
            raise DesignError("CANCELLATION_PENDING", "Generated-job cancellation is unresolved.") from exc

    async def drain(self) -> None:
        await self.recorder.drain()
