"""Scientific transport using the shared VAT invocation journal and Modal recovery."""
from __future__ import annotations

import hashlib
import os
from uuid import UUID, uuid4

from vatlog.capture import dualmethod
from vatlog.execution import VatObservationPending, VatRecorder, VatStateError
from vatlog.node import NodeStatus
from vatlog.result import VatResult
from .design_inputs import DesignError
from .design_store import DesignStore, json_bytes


class ModalTools:
    """Adapt domain recovery/progress to the shared recorded execution lifecycle."""
    def __init__(self, app: str = "biodesign-vat-tools"):
        self.app = app
        self.recorder: VatRecorder | None = None

    def bind_store(self, store: DesignStore) -> None:
        """Use the workflow's persistent recorder key and private execution journal."""
        if self.recorder is None:
            existing = getattr(store, "execution_recorder", None)
            self.recorder = existing or VatRecorder(store.root / "executions", store.keys().recorder)
            store.execution_recorder = self.recorder

    @property
    def configured(self) -> bool:
        return os.environ.get("VAT_MODAL_ENABLED") == "1"

    def _bound(self, tool):
        from vatlog.modal import VatFunction
        from .vat_adapter import scientific_result_adapter, tool_identity
        from .tool_catalog import CORE_APP, tool_spec
        if self.recorder is None:
            raise DesignError("EVIDENCE_FAILED", "Scientific recorder has not been bound.")
        try:
            spec = tool_spec(tool)
        except ValueError as exc:
            raise DesignError("TOOL_UNAVAILABLE", "Unknown scientific tool.") from exc
        app_name = spec.app_name if self.app == CORE_APP else self.app
        identity = tool_identity(tool, app_name)
        return VatFunction.from_name(app_name, tool, descriptor=identity,
                                     result_adapter=scientific_result_adapter).with_recorder(self.recorder)

    @dualmethod
    async def run(self, tool: str, request: object, *, execution_id: str | None = None) -> VatResult:
        """Execute one common request and return its immutable signed ``VatResult``."""
        from .tool_contracts import validate_tool_request
        from .tool_catalog import tool_spec

        if not self.configured:
            raise DesignError("TOOL_UNAVAILABLE", "Deploy and validate Modal tools, then set VAT_MODAL_ENABLED=1.")
        try:
            spec = tool_spec(tool)
        except ValueError as exc:
            raise DesignError("TOOL_UNAVAILABLE", "Unknown scientific tool.") from exc
        if not spec.implemented or spec.readiness == "blocked":
            raise DesignError("TOOL_UNAVAILABLE", spec.blocker or "Scientific tool is unavailable.")
        try:
            id = str(UUID(execution_id)) if execution_id else str(uuid4())
        except (TypeError, ValueError) as exc:
            raise DesignError("INVALID_REQUEST", "Execution identity must be a UUID.") from exc
        envelope = validate_tool_request(request, expected_tool=tool)
        call = self._bound(tool).prepare_call((id, envelope), {}, execution_id=id)
        return await (await call.submit.aio()).get.aio()

    def prepare(self, tool: str, id: str, request: dict, *, control_references=()) -> None:
        self._bound(tool).prepare_call((id, request), {}, execution_id=id,
                                       control_references=control_references)

    async def submit(self, tool: str, id: str, request: dict) -> str:
        from vatlog.modal import PreparedVatCall
        from vatlog.provenance import resolve_inputs
        if not self.configured:
            raise DesignError("TOOL_UNAVAILABLE", "Deploy and validate Modal tools, then set VAT_MODAL_ENABLED=1.")
        try:
            snapshot = self.recorder.request(id)[0]
            supplied, _, _ = resolve_inputs((id, request), {}, self.recorder.trust,
                                             limits=self.recorder.limits)
            if snapshot.call_sha256 != supplied.artifact.sha256:
                raise DesignError("REQUEST_CONFLICT", "Submission differs from the prepared request.")
            call = await PreparedVatCall(self._bound(tool), id).submit.aio()
            return call.object_id
        except DesignError:
            raise
        except (VatStateError, ValueError) as exc:
            raise DesignError("SUBMISSION_UNCERTAIN", "Modal submission could not be confirmed.") from exc

    def _execution(self, call_id):
        entries = [e for e in self.recorder.executions() if e["call_id"] == call_id]
        if len(entries) != 1:
            raise DesignError("EXECUTION_NOT_FOUND", "Call has no unique VAT execution; no new call was submitted.")
        id = entries[0]["execution_id"]
        request = self.recorder.request(id)[0]
        return id, self._bound(request.function.function_name)

    @staticmethod
    def _value(result):
        if result.status is NodeStatus.SUCCEEDED:
            return result.value
        return {"error": "CANCELLED" if result.status is NodeStatus.CANCELLED else "EXECUTION_FAILED",
                "artifacts": []}

    async def poll(self, call_id: str) -> dict | None:
        id, bound = self._execution(call_id)
        try:
            result = await bound.resume(id).get.aio(timeout=0)
            if result.status is not NodeStatus.SUCCEEDED:
                return (await self.recover(id, partials=True))["result"]
            return self._value(result)
        except VatObservationPending as exc:
            if exc.code == "PENDING":
                return None
            recovered = await self.recover(id)
            if "result" in recovered:
                return recovered["result"]
            raise DesignError("CONNECTION_LOST", "Remote outcome remains unresolved; retain the call identity.") from exc
        except (VatStateError, ValueError) as exc:
            raise DesignError("EVIDENCE_FAILED", "Execution evidence needs attention; no new call was submitted.") from exc

    def result(self, id: str) -> VatResult | None:
        node = self.recorder.completed(id)
        return VatResult(node, self.recorder.trust, self.recorder.limits) if node else None

    async def recover(self, id: str, *, partials: bool = False) -> dict:
        """Recover known identity/results; optional failure partials stay outside the signed node."""
        import modal
        id = str(UUID(id))
        try:
            request, _, prior, _ = self.recorder.request(id)
        except VatStateError as exc:
            raise DesignError("EXECUTION_NOT_FOUND", "Job has no VAT execution; no new call was submitted.") from exc
        from vatlog.node import decode_document
        call = decode_document(request.inputs[0], request.inputs[1:], request.raw_blobs(),
                               limits=self.recorder.limits)
        expected = {"id": id, "tool": request.function.function_name, "attempt": 1,
                    "request_sha256": hashlib.sha256(json_bytes(call["args"][1])).hexdigest()}
        existing = self.result(id)
        if existing and not partials:
            return {"start": {**expected, "call_id": prior}, "result": self._value(existing)}
        try:
            from .tool_catalog import CORE_APP, tool_spec
            spec = tool_spec(request.function.function_name)
            app_name = spec.app_name if self.app == CORE_APP else self.app
            recovered = await modal.Function.from_name(app_name, "recover").remote.aio(id)
        except modal.exception.Error as exc:
            if existing:
                # Terminal SDK evidence is already durable; unavailable partial files do not undo it.
                return {"start": {**expected, "call_id": prior}, "result": self._value(existing)}
            raise DesignError("SUBMISSION_UNCERTAIN", "Remote recovery is unavailable.") from exc
        if not isinstance(recovered, dict):
            raise DesignError("OUTPUT_INVALID", "Malformed recovery response.")
        start = recovered.get("start")
        if start:
            if (not isinstance(start, dict) or type(start.get("attempt")) is not int
                    or any(start.get(key) != value for key, value in expected.items())
                    or not start.get("call_id") or (prior and prior != start["call_id"])):
                raise DesignError("DEPENDENCY_MISMATCH", "Recovered identity differs from the recorded dispatch.")
            self.recorder.recovered_acceptance(id, start["call_id"])
            prior = start["call_id"]
        elif prior:
            recovered["start"] = {**expected, "call_id": prior}
        if existing:
            from .design_inputs import checked_result
            from .vat_adapter import scientific_result_adapter
            from vatlog.capture import CaptureContext
            if "result" in recovered:
                try:
                    scientific_result_adapter(recovered["result"], CaptureContext(
                        id, prior, request.function, tuple(call["args"]), call["kwargs"]))
                    artifacts = checked_result(recovered["result"])["artifacts"]
                except ValueError as exc:
                    raise DesignError("EVIDENCE_FAILED", "Recovered partial result failed validation.") from exc
            else:
                artifacts = checked_result({"artifacts": recovered.get("partial_artifacts", [])})["artifacts"]
            # These are separate recovered intermediates, never a replacement for the terminal node.
            return {"start": recovered.get("start"),
                    "result": {**self._value(existing), "artifacts": artifacts}}
        if "result" in recovered:
            if not prior:
                raise DesignError("DEPENDENCY_MISMATCH", "Recovery has no validated call identity.")
            try:
                recorded = self._bound(request.function.function_name).resume(id).collected(recovered["result"])
            except (VatStateError, ValueError) as exc:
                raise DesignError("EVIDENCE_FAILED", "Recovered result failed evidence validation.") from exc
            recovered["result"] = self._value(recorded)
        return recovered

    async def cancel(self, call_id: str) -> None:
        id, bound = self._execution(call_id)
        try:
            await bound.resume(id).cancel.aio()
        except VatObservationPending as exc:
            raise DesignError("CANCELLATION_PENDING", "Remote cancellation is not confirmed.") from exc

    async def drain(self) -> None:
        if self.recorder:
            await self.recorder.drain()
