"""Shared result adapters and sync/async invocation surface."""
from __future__ import annotations

import asyncio
import functools
from dataclasses import dataclass

from .node import AdapterIdentity, DomainOutcome, FunctionIdentity

@dataclass(frozen=True)
class VatOutputArtifact:
    """Explicit bytes classified by a caller-side result adapter."""
    name: str
    kind: str
    data: bytes
    classification: str = "output"


@dataclass(frozen=True)
class AdaptedResult:
    """Domain interpretation accompanying, never replacing, the captured return value."""
    artifacts: tuple[VatOutputArtifact, ...] = ()
    domain_outcome: DomainOutcome | None = None


@dataclass(frozen=True)
class CaptureContext:
    execution_id: str
    call_id: str
    function: FunctionIdentity
    args: tuple
    kwargs: dict


class dualmethod:
    """Expose one async implementation with SDK-style synchronous and .aio entrypoints."""
    def __init__(self, function):
        self.function = function
        functools.update_wrapper(self, function)

    def __get__(self, instance, owner=None):
        if instance is None:
            return self
        asynchronous = functools.partial(self.function, instance)

        @functools.wraps(self.function)
        def synchronous(*args, **kwargs):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return asyncio.run(asynchronous(*args, **kwargs))
            raise RuntimeError("Use .aio from an active event loop.")
        synchronous.aio = asynchronous
        return synchronous



def adapter_identity(adapter) -> AdapterIdentity | None:
    return AdapterIdentity(name=adapter.name, version=adapter.version) if adapter else None
