"""Modal transport adapter for the general caller-side VAT decorator."""
from __future__ import annotations

import asyncio
import inspect
import time
from datetime import datetime, timezone
from typing import Any, Generic, TypeVar
from uuid import UUID

try:
    import modal as modal_sdk
except ImportError as exc:
    raise ImportError("Install biodesign-vat-demo[modal] to use the Modal VAT adapter.") from exc

from .execution import VatObservationPending, VatRecorder, VatStateError
from .node import (AdapterIdentity, FunctionIdentity, SafeException, VatNode,
                   decode_document, encode_document)
from .result import VatResult
from .capture import adapter_identity, dualmethod

T = TypeVar("T")



def _error(code: str, exc: Exception | None = None) -> dict:
    return SafeException(code=code,
                         module=type(exc).__module__[:256] if exc else "vatlog",
                         type=type(exc).__name__[:256] if exc else code,
                         message="Remote function raised an exception." if code == "FUNCTION_ERROR"
                         else "Execution could not produce a successful result.").model_dump(mode="json")


class VatFunction(Generic[T]):
    """One registered Modal function with explicit local recording configuration."""

    def __init__(self, function, identity: FunctionIdentity, *, result_adapter=None):
        self.modal_function = function
        self.identity = identity
        self.result_adapter = result_adapter

    @classmethod
    def from_name(cls, app_name: str, function_name: str, *,
                  descriptor: FunctionIdentity | None = None, result_adapter=None) -> "VatFunction":
        identity = descriptor or FunctionIdentity(app_name=app_name, function_name=function_name)
        if identity.app_name != app_name or identity.function_name != function_name:
            raise ValueError("Deployment descriptor does not match the requested endpoint.")
        return cls(modal_sdk.Function.from_name(app_name, function_name), identity,
                   result_adapter=result_adapter)

    def with_recorder(self, recorder: VatRecorder) -> "BoundVatFunction[T]":
        return BoundVatFunction(self, recorder)

    def adapter_identity(self) -> AdapterIdentity | None:
        """Identity persisted before submission so recovery cannot silently change adapters."""
        return adapter_identity(self.result_adapter)

    def __getattr__(self, name):
        if name in {"remote", "spawn"}:
            raise AttributeError(f"{name} requires .with_recorder(recorder) first")
        raise AttributeError(f"{name} is not supported by the VAT facade")


class BoundVatFunction(Generic[T]):
    """A function bound to one caller-owned journal and signing key."""

    def __init__(self, function: VatFunction[T], recorder: VatRecorder):
        self.function, self.recorder = function, recorder

    def prepare(self, *args, **kwargs) -> "PreparedVatCall[T]":
        """Persist a call without reserving any keyword from the worker's signature."""
        return self.prepare_call(args, kwargs)

    def prepare_call(self, args: tuple, kwargs: dict, *,
                     execution_id: str | None = None, control_references=()) -> "PreparedVatCall[T]":
        """Prepare with caller-owned identity metadata separate from worker arguments."""
        id = self.recorder.prepare_inputs(self.function.identity, args, kwargs, execution_id=execution_id,
                                         result_adapter=self.function.adapter_identity(),
                                         control_references=control_references)
        return PreparedVatCall(self, id)

    @dualmethod
    async def spawn(self, *args, **kwargs) -> "VatCall[T]":
        return await self.prepare(*args, **kwargs).submit.aio()

    @dualmethod
    async def remote(self, *args, **kwargs) -> VatResult[T]:
        call = await self.spawn.aio(*args, **kwargs)
        return await call.get.aio()

    def resume(self, execution_id: str) -> "VatCall[T]":
        request, state, call_id, _ = self.recorder.request(execution_id)
        if (request.function != self.function.identity
                or request.result_adapter != self.function.adapter_identity()):
            raise VatStateError("REQUEST_CONFLICT", "Execution belongs to another function.")
        if not call_id or state in {"prepared", "submitting", "submission_uncertain"}:
            raise VatObservationPending("SUBMISSION_UNCERTAIN", "No accepted call identity is available.", execution_id)
        completed = self.recorder.completed(execution_id)
        call = None if completed else modal_sdk.FunctionCall.from_id(call_id)
        return VatCall(self, execution_id, call)


class PreparedVatCall(Generic[T]):
    """Persisted input identity; repeated submission is rejected before contacting Modal."""
    def __init__(self, bound: BoundVatFunction[T], execution_id: str):
        self.bound, self.execution_id = bound, execution_id

    @dualmethod
    async def submit(self) -> "VatCall[T]":
        recorder = self.bound.recorder
        request, state, _, _ = recorder.request(self.execution_id)
        if (request.function != self.bound.function.identity
                or request.result_adapter != self.bound.function.adapter_identity()):
            raise VatStateError("REQUEST_CONFLICT", "Prepared execution belongs to another function or adapter.")
        if state != "prepared":
            if state in {"running", "observation_pending", "completed"}:
                return self.bound.resume(self.execution_id)
            raise VatObservationPending("SUBMISSION_UNCERTAIN", "Submission already started.", self.execution_id)
        inputs = decode_document(request.inputs[0], request.inputs[1:], request.raw_blobs(),
                                 limits=recorder.limits)
        recorder.set_state(self.execution_id, "submitting")

        async def accept():
            try:
                call = await self.bound.function.modal_function.spawn.aio(*inputs["args"], **inputs["kwargs"])
                recorder.accepted(self.execution_id, call.object_id)
                return VatCall(self.bound, self.execution_id, call)
            except (modal_sdk.exception.Error, OSError, TimeoutError, AttributeError, VatStateError) as exc:
                recorder.set_state(self.execution_id, "submission_uncertain")
                raise VatObservationPending("SUBMISSION_UNCERTAIN", "Submission could not be confirmed.",
                                            self.execution_id) from exc

        task = asyncio.create_task(accept())
        recorder.pending.add(task)
        def finished(task):
            recorder.pending.discard(task)
            if not task.cancelled():
                task.exception()  # retrieved even when the submitting waiter was cancelled
        task.add_done_callback(finished)
        return await asyncio.shield(task)


class VatCall(Generic[T]):
    """Future-like handle for an accepted logical invocation."""
    def __init__(self, bound: BoundVatFunction[T], execution_id: str, call):
        self.bound, self.recorder = bound, bound.recorder
        self.execution_id, self.call = str(UUID(execution_id)), call

    @property
    def object_id(self) -> str:
        return self.recorder.request(self.execution_id)[2] or ""

    def node(self) -> VatNode | None:
        return self.recorder.completed(self.execution_id)

    def _finish(self, observation: dict) -> VatResult[T]:
        return self.recorder.finish(self.execution_id, observation, self.bound.function.result_adapter)

    def collected(self, value: Any) -> VatResult[T]:
        """Finalize an adapter-validated recovered value without executing again."""
        existing = self.recorder.observation(self.execution_id)
        if existing is not None:
            document = encode_document(value, "return.json", limits=self.recorder.limits)
            prior = encode_document(existing.get("value"), "return.json", limits=self.recorder.limits)
            if existing["status"] != "succeeded" or document.artifact.sha256 != prior.artifact.sha256:
                raise VatStateError("OBSERVATION_CONFLICT", "Recovered value conflicts with stored observation.")
            return self._finish(existing)
        return self._finish(self._observation("succeeded", value=value))

    @staticmethod
    def _observation(status: str, *, value=None, error=None) -> dict:
        return dict(status=status, value=value, error=error,
                    observed_at=datetime.now(timezone.utc).isoformat(), monotonic_ns=time.monotonic_ns())

    @dualmethod
    async def get(self, timeout: float | None = None) -> VatResult[T]:
        lock = self.recorder.collection_locks.setdefault(self.execution_id, asyncio.Lock())
        async with lock:
            existing = self.recorder.completed(self.execution_id)
            if existing:
                return VatResult(existing, self.recorder.trust, self.recorder.limits)
            observation = self.recorder.observation(self.execution_id)
            if observation is not None:
                return self._finish(observation)
            try:
                value = await self.call.get.aio(timeout=timeout)
            except modal_sdk.exception.FunctionTimeoutError:
                observation = self._observation("execution_error", error=_error("EXECUTION_TIMEOUT"))
            except modal_sdk.exception.InputCancellation:
                observation = self._observation("cancelled", error=_error("CANCELLED"))
            except (modal_sdk.exception.ConnectionError, modal_sdk.exception.ServiceError,
                    modal_sdk.exception.OutputExpiredError) as exc:
                self.recorder.set_state(self.execution_id, "observation_pending")
                raise VatObservationPending("OBSERVATION_PENDING", "Remote observation unavailable.",
                                            self.execution_id) from exc
            except modal_sdk.exception.TimeoutError as exc:
                raise VatObservationPending("PENDING", "Execution is still pending.", self.execution_id) from exc
            except TimeoutError as exc:
                if timeout is not None:
                    # Modal uses builtin TimeoutError both for expired polls and user exceptions.
                    # Only an unbounded get rules out a polling deadline through the public API.
                    raise VatObservationPending("PENDING", "Polling did not yield an unambiguous terminal outcome.",
                                                self.execution_id) from exc
                observation = self._observation("function_error", error=_error("FUNCTION_ERROR", exc))
            except (modal_sdk.exception.RemoteError, modal_sdk.exception.ExecutionError):
                observation = self._observation("execution_error", error=_error("EXECUTION_FAILED"))
            except modal_sdk.exception.Error as exc:
                raise VatObservationPending("OBSERVATION_PENDING", "Platform could not confirm the outcome.",
                                            self.execution_id) from exc
            except Exception as exc:
                # Modal re-raises arbitrary user exception types at this exact remote-result boundary.
                observation = self._observation("function_error", error=_error("FUNCTION_ERROR", exc))
            else:
                observation = self._observation("succeeded", value=value)
            return self._finish(observation)

    @dualmethod
    async def cancel(self) -> None:
        if self.recorder.completed(self.execution_id):
            return
        self.recorder.request_cancel(self.execution_id)
        try:
            await self.call.cancel.aio()
        except modal_sdk.exception.Error as exc:
            raise VatObservationPending("CANCELLATION_PENDING", "Cancellation could not be confirmed.",
                                        self.execution_id) from exc


def vat(function=None, *, descriptor: FunctionIdentity | None = None, result_adapter=None):
    """Wrap a registered Modal Function: @vat above @app.function(...)."""
    def decorate(registered):
        if not isinstance(registered, modal_sdk.Function):
            raise TypeError("@vat requires an inner @app.function(...) decorator.")
        raw = registered.get_raw_f()
        if (not inspect.isfunction(raw) or inspect.isgeneratorfunction(raw) or inspect.isasyncgenfunction(raw)
                or "." in raw.__qualname__):
            raise TypeError("@vat supports singular module-level functions, not generators or methods.")
        if not modal_sdk.is_local():
            return registered
        # tag remains public in the supported SDK range; no private callable/configuration access.
        app_name, function_name = registered.app.name, registered.tag
        if descriptor and (descriptor.app_name != app_name or descriptor.function_name != function_name):
            raise ValueError("Deployment descriptor does not match the registered endpoint.")
        identity = descriptor or FunctionIdentity(app_name=app_name, function_name=function_name)
        return VatFunction(registered, identity, result_adapter=result_adapter)
    return decorate(function) if function is not None else decorate
