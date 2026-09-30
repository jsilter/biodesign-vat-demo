"""VAT protocol and lazy optional execution integration."""

from .execution import VatRecorder
from .result import VatResult, VatExecutionError


def vat(function=None, **options):
    """Record ordinary Python or registered Modal functions through a shared journal."""
    import inspect
    def decorate(candidate):
        if inspect.isfunction(candidate):
            from .local import LocalFunction
            return LocalFunction(candidate, **options)
        from .modal import vat as modal_decorate
        return modal_decorate(candidate, **options)
    return decorate(function) if function is not None else decorate


def __getattr__(name):
    if name in {"VatFunction", "VatCall"}:
        from . import modal
        return getattr(modal, name)
    raise AttributeError(name)
