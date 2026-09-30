"""Classify actual SDK exceptions so execution timeouts never look like pending polls."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from runner.design_inputs import DesignError
from runner.modal_client import ModalTools
from runner.design_store import DesignStore


@pytest.mark.parametrize("exception,code", [("TimeoutError", None), ("FunctionTimeoutError", "EXECUTION_FAILED"),
    ("RemoteError", "EXECUTION_FAILED"), ("ConnectionError", "SUBMISSION_UNCERTAIN"), ("AuthError", "SUBMISSION_UNCERTAIN")])
def test_modal_poll_distinguishes_pending_execution_failure_and_network_loss(tmp_path, monkeypatch, exception, code):
    modal = pytest.importorskip("modal")
    async def get(**kwargs):
        assert kwargs == {"timeout": 0}
        raise getattr(modal.exception, exception)("sentinel-private-diagnostic")
    monkeypatch.setattr(modal.FunctionCall, "from_id", lambda id: SimpleNamespace(get=SimpleNamespace(aio=get)))
    async def recovery(id):
        raise modal.exception.ConnectionError("sentinel-private-diagnostic")
    monkeypatch.setattr(modal.Function, "from_name", lambda *args: SimpleNamespace(
        remote=SimpleNamespace(aio=recovery)))
    store, transport = DesignStore(tmp_path), ModalTools()
    transport.bind_store(store)
    try:
        id = str(uuid4())
        transport.prepare("esmc", id, {"sequence": "AC"})
        transport.recorder.set_state(id, "submitting")
        transport.recorder.accepted(id, "known-call")
        if code is None:
            assert asyncio.run(transport.poll("known-call")) is None
            assert transport.result(id) is None
        elif code == "EXECUTION_FAILED":
            assert asyncio.run(transport.poll("known-call"))["error"] == code
            assert transport.result(id).status.value == "execution_error"
        else:
            with pytest.raises(DesignError) as error:
                asyncio.run(transport.poll("known-call"))
            assert error.value.code == code
            assert "sentinel-private-diagnostic" not in str(error.value)
        assert b"sentinel-private-diagnostic" not in (tmp_path / "executions" / "state.sqlite").read_bytes()
    finally:
        store.close()
