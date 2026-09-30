"""Ordinary Python computation through the same durable VAT recording boundary."""
from __future__ import annotations

import asyncio
import inspect
import time
from datetime import datetime, timezone

from .capture import adapter_identity, dualmethod
from .execution import VatObservationPending, VatRecorder, VatStateError
from .node import FunctionIdentity, SafeException, decode_document
from .result import VatResult
from .canon import sha256_hex


def observation(status: str, *, value=None, exception: BaseException | None = None) -> dict:
    """Preserve safe error identity, excluding potentially sensitive exception text."""
    error = None if exception is None else SafeException(
        code="CANCELLED" if status == "cancelled" else "FUNCTION_ERROR",
        module=type(exception).__module__[:256], type=type(exception).__name__[:256],
        message="Local execution did not return a successful value.").model_dump(mode="json")
    return {"status": status, "value": value, "error": error,
            "observed_at": datetime.now(timezone.utc).isoformat(), "monotonic_ns": time.monotonic_ns()}


class LocalFunction:
    """An instrumented callable; a recorder must be bound before invocation."""
    def __init__(self, function, *, descriptor=None, result_adapter=None):
        if not inspect.isfunction(function) or inspect.isgeneratorfunction(function) or inspect.isasyncgenfunction(function):
            raise TypeError("Local VAT capture requires a sync or async function, not a generator")
        self.function, self.result_adapter = function, result_adapter
        try:
            source = sha256_hex(inspect.getsource(function).encode())
        except (OSError, TypeError):
            source = None
        self.identity = descriptor or FunctionIdentity(app_name="local", function_name=function.__name__,
                                                      module=function.__module__, qualname=function.__qualname__,
                                                      source_sha256=source)

    def with_recorder(self, recorder: VatRecorder) -> "BoundLocalFunction":
        return BoundLocalFunction(self, recorder)


class BoundLocalFunction:
    def __init__(self, function: LocalFunction, recorder: VatRecorder):
        self.function, self.recorder = function, recorder

    def prepare_call(self, args: tuple, kwargs: dict, *, execution_id: str | None = None,
                     control_references=()) -> "PreparedLocalCall":
        identifier = self.recorder.prepare_inputs(self.function.identity, args, kwargs,
            execution_id=execution_id, result_adapter=adapter_identity(self.function.result_adapter),
            transport="local", control_references=control_references)
        return PreparedLocalCall(self, identifier)

    def prepare(self, *args, **kwargs) -> "PreparedLocalCall":
        return self.prepare_call(args, kwargs)

    @dualmethod
    async def call(self, *args, **kwargs) -> VatResult:
        return await self.prepare(*args, **kwargs).run.aio()

    def resume(self, execution_id: str) -> "PreparedLocalCall":
        self.recorder.request(execution_id)
        return PreparedLocalCall(self, execution_id)


class PreparedLocalCall:
    def __init__(self, bound: BoundLocalFunction, execution_id: str):
        self.bound, self.execution_id = bound, execution_id

    @dualmethod
    async def run(self) -> VatResult:
        recorder, function = self.bound.recorder, self.bound.function
        lock = recorder.collection_locks.setdefault(self.execution_id, asyncio.Lock())
        async with lock:
            request, state, _, _ = recorder.request(self.execution_id)
            if (request.transport != "local" or request.function != function.identity
                    or request.result_adapter != adapter_identity(function.result_adapter)):
                raise VatStateError("REQUEST_CONFLICT", "Prepared call belongs to another function")
            existing = recorder.completed(self.execution_id)
            if existing:
                return VatResult(existing, recorder.trust, recorder.limits)
            recorded = recorder.observation(self.execution_id)
            if recorded is not None:
                return recorder.finish(self.execution_id, recorded, function.result_adapter)
            if state != "prepared":
                raise VatObservationPending("OBSERVATION_PENDING", "Interrupted local execution is not replayed",
                                            self.execution_id)
            values = decode_document(request.inputs[0], request.inputs[1:], request.raw_blobs(), limits=recorder.limits)
            recorder.set_state(self.execution_id, "submitting")
            recorder.accepted(self.execution_id, "local:" + self.execution_id)
            try:
                value = function.function(*values["args"], **values["kwargs"])
                if inspect.isawaitable(value):
                    value = await value
            except asyncio.CancelledError as exc:
                recorder.request_cancel(self.execution_id)
                recorder.finish(self.execution_id, observation("cancelled", exception=exc), function.result_adapter)
                raise
            except Exception as exc:
                # Arbitrary user function boundary; preserve failure rather than its sensitive message.
                recorded = observation("function_error", exception=exc)
            else:
                recorded = observation("succeeded", value=value)
            return recorder.finish(self.execution_id, recorded, function.result_adapter)
