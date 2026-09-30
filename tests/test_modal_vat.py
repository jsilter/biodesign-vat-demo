"""Offline contract tests for the public Modal VAT facade and durable journal."""

from __future__ import annotations

import base64
import inspect
import sqlite3
import asyncio
from types import SimpleNamespace
from pathlib import Path

import modal
import pytest
from cryptography.hazmat.primitives import serialization

from vatlog.capture import AdaptedResult, VatOutputArtifact
from vatlog.modal import (VatObservationPending,
                          VatRecorder, VatStateError, VatFunction, vat)
from vatlog.node import DomainOutcome, FunctionIdentity, NodeStatus, verify_node
from vatlog.result import VatResult, VatExecutionError
from vatlog.roles import Keyring, LocalSigner


def add(left, right=0):
    return left + right


def raise_secret(_value):
    raise RuntimeError("private diagnostic that must not be stored")


def return_artifact(value):
    return value * 2


class MetricAdapter:
    name, version = "metric", "1"

    def __call__(self, value, context):
        return AdaptedResult((VatOutputArtifact("metric.bin", "metric", b"measured"),))


class FakeCall:
    def __init__(self, object_id, action):
        self.object_id = object_id
        self.action = action
        self.get_count = 0
        self.cancel_count = 0
        self.get = SimpleNamespace(aio=self.get_async)
        self.cancel = SimpleNamespace(aio=self.cancel_async)

    async def get_async(self, timeout=None):
        self.get_count += 1
        action = self.action.pop(0) if isinstance(self.action, list) else self.action
        if isinstance(action, BaseException):
            raise action
        value = action() if callable(action) else action
        return await value if inspect.isawaitable(value) else value

    async def cancel_async(self):
        self.cancel_count += 1


class FakeFunction:
    def __init__(self, wrapper, *, call_id="fc-test"):
        self.wrapper = wrapper
        self.call_id = call_id
        self.spawn_count = 0
        self.spawn_hook = None
        self.spawn_error = None
        self.calls = []
        self.spawn = SimpleNamespace(aio=self.spawn_async)

    async def spawn_async(self, *args, **kwargs):
        self.spawn_count += 1
        if self.spawn_hook:
            self.spawn_hook()
        if self.spawn_error:
            raise self.spawn_error
        call = FakeCall(self.call_id, lambda: self.wrapper(*args, **kwargs))
        self.calls.append(call)
        return call


def make_facade(function=add, *, adapter=None):
    app = SimpleNamespace(function_object=FakeFunction(function))
    identity = FunctionIdentity(app_name="fake-app", function_name=function.__name__)
    facade = VatFunction(app.function_object, identity, result_adapter=adapter)
    return app, facade


def test_modal_consumes_frozen_local_reference_and_preserves_signed_lineage(tmp_path):
    from vatlog.local import LocalFunction
    from vatlog.provenance import verify_provenance
    received = []
    def worker(value):
        received.append(value)
        return value + 3
    app, facade = make_facade(worker)
    with VatRecorder(tmp_path, Keyring.generate().recorder) as recorder:
        first = LocalFunction(add).with_recorder(recorder).call(10, 5)
        prepared = facade.with_recorder(recorder).prepare_call((first.ref(),), {})
        result = prepared.submit().get()
        assert received == [15]
        assert result.value == 18
        binding, = result.node.statement().input_bindings
        assert binding.producer_node_id == first.node_id
        assert binding.destination == '/args/0'
        assert app.function_object.spawn_count == 1
        assert verify_provenance(recorder.evidence_nodes(), recorder.trust).outcome == 'VALID'


def journal_row(root: Path):
    connection = sqlite3.connect(root / "state.sqlite")
    try:
        return connection.execute(
            "SELECT id,state,call_id,cancel_requested,request FROM executions"
        ).fetchone()
    finally:
        connection.close()


def test_real_modal_app_registration_uses_public_function_decorator():
    app = modal.App("vat-registration-test")
    registered = app.function(cpu=1.0, retries=0)(add)
    facade = vat(registered)
    assert isinstance(facade.modal_function, modal.Function)
    assert facade.modal_function is registered
    assert facade.identity.app_name == "vat-registration-test"
    assert facade.identity.function_name == "add"
    assert facade.identity.source_sha256 is None


def test_decorator_rejects_unsupported_surfaces():
    app, facade = make_facade(add)
    assert facade.identity.function_name == "add"
    with pytest.raises(TypeError, match="inner"):
        vat(add)
    with pytest.raises(AttributeError, match="with_recorder"):
        facade.remote(1)
    with pytest.raises(AttributeError, match="not supported"):
        facade.map([1])
    app = modal.App("declared-app")
    with pytest.raises(ValueError, match="descriptor"):
        vat(app.function()(add), descriptor=FunctionIdentity(app_name="other-app", function_name="add"))
    with pytest.raises(ValueError, match="descriptor"):
        VatFunction.from_name("declared-app", "add", descriptor=FunctionIdentity(
            app_name="declared-app", function_name="other-function"))


def test_success_persists_before_submit_signs_and_collects_idempotently(tmp_path):
    ring = Keyring.generate()
    app, facade = make_facade(return_artifact, adapter=MetricAdapter())
    with VatRecorder(tmp_path, ring.recorder) as recorder:
        observed = []
        app.function_object.spawn_hook = lambda: observed.append(journal_row(tmp_path)[1:3])
        call = facade.with_recorder(recorder).spawn(9)
        assert observed == [("submitting", None)]
        assert journal_row(tmp_path)[1:3] == ("running", "fc-test")
        result = call.get()
        assert result.value == 18
        assert result.status is NodeStatus.SUCCEEDED
        assert verify_node(result.node, recorder.trust).passed
        statement = result.node.statement()
        assert statement.modal_call_id == "fc-test"
        assert statement.modal_input_id is None
        assert statement.timestamp_basis == "local_observation"
        assert statement.input_bindings == []
        assert next(a for a in statement.results if a.name == "metric.bin").kind == "metric"
        same = call.get()
        assert same.node.canonical_bytes() == result.node.canonical_bytes()
        assert app.function_object.calls[0].get_count == 1
        assert journal_row(tmp_path)[1] == "completed"


def test_function_error_is_safe_and_contains_no_return_artifacts(tmp_path):
    ring = Keyring.generate()
    _app, facade = make_facade(raise_secret)
    with VatRecorder(tmp_path, ring.recorder) as recorder:
        result = facade.with_recorder(recorder).remote("input")
        statement = result.node.statement()
        assert result.status is NodeStatus.FUNCTION_ERROR
        assert statement.results == []
        assert statement.exception.code == "FUNCTION_ERROR"
        durable = (tmp_path / "state.sqlite").read_bytes() + result.node.canonical_bytes()
        assert b"private diagnostic" not in durable
        with pytest.raises(Exception, match="do not have"):
            _ = result.value


def test_poll_timeout_and_connection_error_never_resubmit(tmp_path, monkeypatch):
    ring = Keyring.generate()
    app, facade = make_facade(add)
    with VatRecorder(tmp_path, ring.recorder) as recorder:
        call = facade.with_recorder(recorder).spawn(2, 3)
        response = call.call.action()
        call.call.action = [TimeoutError("poll"), response]
        with pytest.raises(VatObservationPending) as pending:
            call.get(timeout=0.01)
        assert pending.value.code == "PENDING"
        assert journal_row(tmp_path)[1] == "running"
        assert call.get().value == 5
        assert app.function_object.spawn_count == 1

    other = tmp_path / "connection"
    with VatRecorder(other, ring.recorder) as recorder:
        call = facade.with_recorder(recorder).spawn(3, 4)
        call.call.action = modal.exception.ConnectionError("secret service detail")
        with pytest.raises(VatObservationPending) as pending:
            call.get()
        assert pending.value.code == "OBSERVATION_PENDING"
        assert journal_row(other)[1] == "observation_pending"
        assert b"secret service detail" not in (other / "state.sqlite").read_bytes()


def test_submission_uncertainty_records_intent_but_cannot_resume_or_retry(tmp_path):
    ring = Keyring.generate()
    app, facade = make_facade(add)
    app.function_object.spawn_error = modal.exception.ConnectionError("unknown submission")
    with VatRecorder(tmp_path, ring.recorder) as recorder:
        bound = facade.with_recorder(recorder)
        with pytest.raises(VatStateError) as uncertain:
            bound.spawn(1, 2)
        execution_id, state, call_id, *_ = journal_row(tmp_path)
        assert uncertain.value.code == "SUBMISSION_UNCERTAIN"
        assert (state, call_id, app.function_object.spawn_count) == ("submission_uncertain", None, 1)
        with pytest.raises(VatStateError, match="accepted call identity"):
            bound.resume(execution_id)
        assert app.function_object.spawn_count == 1


def test_missing_call_identity_is_submission_uncertain(tmp_path):
    ring = Keyring.generate()
    app, facade = make_facade(add)
    app.function_object.call_id = ""
    with VatRecorder(tmp_path, ring.recorder) as recorder:
        with pytest.raises(VatStateError) as uncertain:
            facade.with_recorder(recorder).spawn(1)
        assert uncertain.value.code == "SUBMISSION_UNCERTAIN"
        assert journal_row(tmp_path)[1:3] == ("submission_uncertain", None)


@pytest.mark.parametrize("error,status,code", [
    (modal.exception.FunctionTimeoutError("deadline"), NodeStatus.EXECUTION_ERROR, "EXECUTION_TIMEOUT"),
    (modal.exception.InputCancellation("cancelled"), NodeStatus.CANCELLED, "CANCELLED"),
    (modal.exception.RemoteError("remote"), NodeStatus.EXECUTION_ERROR, "EXECUTION_FAILED"),
])
def test_terminal_sdk_failures_become_safe_signed_nodes(tmp_path, error, status, code):
    ring = Keyring.generate()
    _app, facade = make_facade(add)
    with VatRecorder(tmp_path, ring.recorder) as recorder:
        call = facade.with_recorder(recorder).spawn(1)
        call.call.action = error
        result = call.get()
        assert result.status is status
        assert result.node.statement().exception.code == code
        assert verify_node(result.node, recorder.trust).passed
        assert str(error).encode() not in result.node.canonical_bytes()


def test_cancel_intent_is_durable_before_platform_call(tmp_path):
    ring = Keyring.generate()
    _app, facade = make_facade(add)
    with VatRecorder(tmp_path, ring.recorder) as recorder:
        call = facade.with_recorder(recorder).spawn(1)
        original = call.call.cancel.aio

        async def checked_cancel():
            assert journal_row(tmp_path)[3] == 1
            await original()

        call.call.cancel.aio = checked_cancel
        call.cancel()
        assert call.call.cancel_count == 1
        assert call.node() is None
        assert call.get().status is NodeStatus.SUCCEEDED


def test_error_dictionary_is_successful_data_not_an_execution_failure(tmp_path):
    ring = Keyring.generate()
    _app, facade = make_facade(add)
    with VatRecorder(tmp_path, ring.recorder) as recorder:
        call = facade.with_recorder(recorder).spawn(4)
        value = {"error": "DOMAIN_FAILED", "artifacts": [{"name": "log", "raw": b"diagnostic"}]}
        call.call.action = value
        result = call.get()
        assert result.status is NodeStatus.SUCCEEDED
        assert result.value == value
        assert result.domain_outcome is None


def test_restart_resume_uses_call_id_and_completed_resume_needs_no_modal_handle(tmp_path, monkeypatch):
    ring = Keyring.generate()
    app, facade = make_facade(add)
    first = VatRecorder(tmp_path, ring.recorder)
    call = facade.with_recorder(first).spawn(7, 8)
    execution_id = call.execution_id
    platform_call = call.call
    first.close()

    resumed_ids = []
    monkeypatch.setattr(modal.FunctionCall, "from_id",
                        lambda call_id: resumed_ids.append(call_id) or platform_call)
    second = VatRecorder(tmp_path, ring.recorder)
    result = facade.with_recorder(second).resume(execution_id).get()
    assert result.value == 15
    assert resumed_ids == ["fc-test"]
    second.close()

    monkeypatch.setattr(modal.FunctionCall, "from_id",
                        lambda _call_id: pytest.fail("completed resume consulted Modal"))
    with VatRecorder(tmp_path, ring.recorder) as third:
        assert facade.with_recorder(third).resume(execution_id).get().value == 15
    assert app.function_object.spawn_count == 1


def test_recovers_node_written_before_completed_state_without_recollecting(tmp_path):
    ring = Keyring.generate()
    app, facade = make_facade(add)
    with VatRecorder(tmp_path, ring.recorder) as recorder:
        call = facade.with_recorder(recorder).spawn(4, 5)
        expected = call.get().node.canonical_bytes()
        with recorder.db:
            recorder.db.execute("UPDATE executions SET state='running' WHERE id=?",
                                (call.execution_id,))
        call.call.get = lambda **_kwargs: pytest.fail("terminal call was collected twice")
        recovered = call.get()
        assert recovered.node.canonical_bytes() == expected
        assert journal_row(tmp_path)[1] == "completed"


def test_recorder_excludes_second_owner_and_rejects_symlinked_root(tmp_path):
    ring = Keyring.generate()
    first = VatRecorder(tmp_path / "owned", ring.recorder)
    try:
        with pytest.raises(VatStateError) as busy:
            VatRecorder(tmp_path / "owned", ring.recorder)
        assert busy.value.code == "STORE_BUSY"
    finally:
        first.close()
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(VatStateError) as unsafe:
        VatRecorder(link, ring.recorder)
    assert unsafe.value.code == "UNSAFE_PATH"

    unsafe_root = tmp_path / "unsafe-lock"
    unsafe_root.mkdir()
    lock_target = tmp_path / "lock-target"
    lock_target.write_text("do not touch")
    (unsafe_root / "owner.lock").symlink_to(lock_target)
    with pytest.raises(VatStateError) as unsafe:
        VatRecorder(unsafe_root, ring.recorder)
    assert unsafe.value.code == "UNSAFE_PATH"
    assert lock_target.read_text() == "do not touch"


def test_resume_rejects_other_function_and_corrupt_completed_state(tmp_path):
    ring = Keyring.generate()
    _app, facade = make_facade(add)
    with VatRecorder(tmp_path, ring.recorder) as recorder:
        call = facade.with_recorder(recorder).spawn(1)
        call.get()
        _other_app, other = make_facade(return_artifact)
        with pytest.raises(VatStateError) as conflict:
            other.with_recorder(recorder).resume(call.execution_id)
        assert conflict.value.code == "REQUEST_CONFLICT"
        node_path = tmp_path / call.execution_id / "node.json"
        node_path.write_bytes(b"{}")
        with pytest.raises(VatStateError):
            call.node()


def test_completed_node_cannot_be_substituted_between_executions(tmp_path):
    ring = Keyring.generate()
    _app, facade = make_facade(add)
    with VatRecorder(tmp_path, ring.recorder) as recorder:
        first = facade.with_recorder(recorder).spawn(1)
        first.get()
        second = facade.with_recorder(recorder).spawn(2)
        second.get()
        first_path = tmp_path / first.execution_id / "node.json"
        second_path = tmp_path / second.execution_id / "node.json"
        first_path.write_bytes(second_path.read_bytes())
        with pytest.raises(VatStateError) as corrupt:
            first.node()
        assert corrupt.value.code == "JOURNAL_CORRUPT"


def test_private_key_and_signer_are_absent_from_remote_closure_and_request(tmp_path):
    ring = Keyring.generate()
    app, facade = make_facade(add)
    closure_values = [cell.cell_contents for cell in app.function_object.wrapper.__closure__ or ()]
    assert not any(isinstance(value, (LocalSigner, VatRecorder)) for value in closure_values)
    private = ring.recorder.key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    with VatRecorder(tmp_path, ring.recorder) as recorder:
        call = facade.with_recorder(recorder).spawn(b"input", b"suffix")
        request_bytes = journal_row(tmp_path)[4]
        result = call.get()
        evidence = request_bytes + result.node.canonical_bytes()
        assert private not in evidence
        assert base64.b64encode(private) not in evidence
        assert inspect.getclosurevars(app.function_object.wrapper).nonlocals == {}


def test_real_sdk_container_import_resolves_callable_sync_and_async(monkeypatch):
    import importlib
    import sys
    from modal._runtime.user_code_imports import import_single_function_service
    from modal_proto import api_pb2
    # Private SDK loader is used only as an integration-test harness, never by VAT.
    monkeypatch.setattr(modal, "is_local", lambda: False)
    sys.modules.pop("modal_vat_fixture", None)
    module = importlib.import_module("modal_vat_fixture")
    try:
        for name in ("square", "async_square"):
            assert isinstance(getattr(module, name), modal.Function)
            service = import_single_function_service(api_pb2.Function(
                module_name="modal_vat_fixture", implementation_name=name,
                function_name=name, app_name="vat-container-test"), None)
            raw = service._user_defined_callable
            assert callable(raw)
            value = raw(9)
            assert (asyncio.run(value) if inspect.isawaitable(value) else value) == 81
    finally:
        sys.modules.pop("modal_vat_fixture", None)


def test_result_is_immutable_and_roundtrips_with_explicit_trust(tmp_path):
    ring = Keyring.generate()
    _, facade = make_facade(lambda: {"items": [1, b"exact"]}, adapter=MetricAdapter())
    with VatRecorder(tmp_path / "journal", ring.recorder) as recorder:
        result = facade.with_recorder(recorder).remote()
        node_id = result.node_id
        result.value["items"].append("mutation")
        result.node.blobs.clear()
        assert result.value == {"items": [1, b"exact"]}
        assert result.node_id == node_id
        assert next(a for a in result.artifacts if a.name == "metric.bin").read_bytes() == b"measured"
        path = tmp_path / "export.json"
        result.save(path)
        loaded = VatResult.load(path, trust=recorder.trust)
        assert loaded.node_id == node_id
        assert loaded.node.canonical_bytes() == result.node.canonical_bytes()
        assert not result.verify(Keyring.generate().trust_bundle()).passed
        with pytest.raises(ValueError, match="key|trust"):
            VatResult.load(path, trust=Keyring.generate().trust_bundle())


def test_adapter_failure_retains_observation_for_restart_without_recollection(tmp_path, monkeypatch):
    class RepairableAdapter(MetricAdapter):
        broken = True
        def __call__(self, value, context):
            if self.broken:
                raise RuntimeError("private adapter diagnostics")
            value["items"].append("adapter mutation")
            return super().__call__(value, context)

    ring, adapter = Keyring.generate(), RepairableAdapter()
    app, facade = make_facade(lambda: {"items": [1]}, adapter=adapter)
    with VatRecorder(tmp_path, ring.recorder) as recorder:
        call = facade.with_recorder(recorder).spawn()
        with pytest.raises(VatStateError, match="adapter failed"):
            call.get()
        execution_id = call.execution_id
        assert recorder.observation(execution_id)["value"] == {"items": [1]}
    adapter.broken = False
    monkeypatch.setattr(modal.FunctionCall, "from_id", lambda id: None)
    with VatRecorder(tmp_path, ring.recorder) as recorder:
        result = facade.with_recorder(recorder).resume(execution_id).get()
        assert result.value == {"items": [1]}
        assert result.node.statement().result_adapter.name == "metric"
    assert app.function_object.spawn_count == 1
    assert app.function_object.calls[0].get_count == 1


def test_async_concurrent_collectors_share_one_terminal_observation(tmp_path):
    async def run():
        async def worker(value):
            await asyncio.sleep(0)
            return value + 1
        app, facade = make_facade(worker)
        with VatRecorder(tmp_path, Keyring.generate().recorder) as recorder:
            call = await facade.with_recorder(recorder).spawn.aio(10)
            results = await asyncio.gather(call.get.aio(), call.get.aio(), call.get.aio())
            assert [r.value for r in results] == [11, 11, 11]
            assert len({r.node_id for r in results}) == 1
            assert app.function_object.calls[0].get_count == 1
            with pytest.raises(RuntimeError, match=".aio"):
                call.get()
    asyncio.run(run())


def test_cancelled_submission_waiter_does_not_lose_acceptance(tmp_path):
    async def run():
        app, facade = make_facade(add)
        entered, release = asyncio.Event(), asyncio.Event()
        original = app.function_object.spawn.aio
        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)
        app.function_object.spawn.aio = delayed
        with VatRecorder(tmp_path, Keyring.generate().recorder) as recorder:
            prepared = facade.with_recorder(recorder).prepare(3, 5)
            waiter = asyncio.create_task(prepared.submit.aio())
            await entered.wait()
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert recorder.request(prepared.execution_id)[1] == "submitting"
            release.set()
            call, = await asyncio.gather(*tuple(recorder.pending))
            assert recorder.request(prepared.execution_id)[1:3] == ("running", "fc-test")
            assert (await call.get.aio()).value == 8
            assert app.function_object.spawn_count == 1
    asyncio.run(run())


def test_prepared_snapshot_is_exact_and_worker_keywords_are_not_reserved(tmp_path):
    def worker(*, execution_id, values):
        return {"execution_id": execution_id, "values": values}
    _, facade = make_facade(worker)
    with VatRecorder(tmp_path, Keyring.generate().recorder) as recorder:
        values = [1]
        prepared = facade.with_recorder(recorder).prepare(execution_id="worker-id", values=values)
        values.append(2)
        assert prepared.submit().get().value == {"execution_id": "worker-id", "values": [1]}


def test_recovery_conflict_and_adapter_change_are_rejected(tmp_path):
    _, facade = make_facade(add, adapter=MetricAdapter())
    with VatRecorder(tmp_path, Keyring.generate().recorder) as recorder:
        call = facade.with_recorder(recorder).spawn(1)
        result = call.collected(1)
        with pytest.raises(VatStateError, match="conflicts"):
            call.collected(2)
        assert call.get().node_id == result.node_id
        _, incompatible = make_facade(add)
        with pytest.raises(VatStateError, match="another function"):
            incompatible.with_recorder(recorder).resume(call.execution_id)


@pytest.mark.parametrize("name,kind,classification", [
    ("return.json", "log", "output"), ("../escape", "log", "output"),
    ("nested.json", "vat_node", "output"), ("metric.bin", "metric", "unknown"),
    ("return-bytes-1", "log", "output"),
])
def test_invalid_adapter_artifacts_block_publication_without_redispatch(tmp_path, name, kind, classification):
    class InvalidAdapter(MetricAdapter):
        def __call__(self, value, context):
            return AdaptedResult((VatOutputArtifact(name, kind, b"bytes", classification),))
    app, facade = make_facade(add, adapter=InvalidAdapter())
    with VatRecorder(tmp_path, Keyring.generate().recorder) as recorder:
        call = facade.with_recorder(recorder).spawn(1)
        for _ in range(2):
            with pytest.raises(VatStateError, match="artifact"):
                call.get()
        assert recorder.observation(call.execution_id)["value"] == 1
        assert call.node() is None
        assert app.function_object.spawn_count == app.function_object.calls[0].get_count == 1


def test_failed_value_exception_retains_result_and_lineage_is_reserved(tmp_path):
    from pydantic import ValidationError
    from vatlog.node import ExecutionStatement
    _, facade = make_facade(raise_secret)
    with VatRecorder(tmp_path, Keyring.generate().recorder) as recorder:
        result = facade.with_recorder(recorder).remote(None)
        with pytest.raises(VatExecutionError) as error:
            _ = result.value
        assert error.value.result is result
        statement = result.node.statement().model_dump()
        statement["input_bindings"] = [{"node_id": "unverified"}]
        with pytest.raises(ValidationError):
            ExecutionStatement.model_validate(statement)


def test_signing_write_failure_retries_only_evidence(tmp_path, monkeypatch):
    app, facade = make_facade(add)
    with VatRecorder(tmp_path, Keyring.generate().recorder) as recorder:
        original = recorder.complete
        def unavailable(*args):
            raise OSError("disk unavailable")
        monkeypatch.setattr(recorder, "complete", unavailable)
        call = facade.with_recorder(recorder).spawn(2, 4)
        with pytest.raises(OSError):
            call.get()
        assert recorder.observation(call.execution_id)["value"] == 6
        monkeypatch.setattr(recorder, "complete", original)
        assert call.get().value == 6
        assert app.function_object.calls[0].get_count == 1


def test_core_and_research_transport_import_without_modal():
    import subprocess
    import sys
    code = '''
import sys
sys.modules["modal"] = None
from vatlog import VatResult, VatRecorder, vat
from runner.modal_client import ModalTools
assert callable(vat)
from tempfile import TemporaryDirectory
from vatlog.roles import Keyring
with TemporaryDirectory() as root:
    with VatRecorder(root, Keyring.generate().recorder) as recorder:
        result = vat(lambda value: value * 2).with_recorder(recorder).call(21)
        assert result.value == 42
        assert result.verify(recorder.trust).passed
try:
    from vatlog.modal import VatFunction
except ImportError as error:
    assert "[modal]" in str(error)
else:
    raise AssertionError("Modal adapter did not require optional dependency")
'''
    completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr


def test_worker_timeout_without_poll_deadline_is_a_terminal_function_error(tmp_path):
    def deadline():
        raise TimeoutError("private application deadline")
    app, facade = make_facade(deadline)
    with VatRecorder(tmp_path, Keyring.generate().recorder) as recorder:
        result = facade.with_recorder(recorder).remote()
        assert result.status is NodeStatus.FUNCTION_ERROR
        assert result.error.type == "TimeoutError"
        assert b"private application deadline" not in result.node.canonical_bytes()
        assert app.function_object.spawn_count == 1


def test_removing_adapter_after_dispatch_blocks_finalization_and_can_be_repaired(tmp_path):
    app, facade = make_facade(add, adapter=MetricAdapter())
    with VatRecorder(tmp_path, Keyring.generate().recorder) as recorder:
        call = facade.with_recorder(recorder).spawn(3, 5)
        facade.result_adapter = None
        with pytest.raises(VatStateError, match="adapter|Adapter"):
            call.get()
        assert recorder.observation(call.execution_id)["value"] == 8
        assert call.node() is None
        facade.result_adapter = MetricAdapter()
        result = call.get()
        assert next(a for a in result.artifacts if a.name == "metric.bin").read_bytes() == b"measured"
        assert app.function_object.calls[0].get_count == 1


@pytest.mark.parametrize("remote_value", [1, 2])
def test_concurrent_volume_recovery_and_collection_compare_observations(tmp_path, remote_value):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def worker():
            entered.set()
            await release.wait()
            return remote_value
        app, facade = make_facade(worker)
        with VatRecorder(tmp_path, Keyring.generate().recorder) as recorder:
            call = await facade.with_recorder(recorder).spawn.aio()
            collecting = asyncio.create_task(call.get.aio())
            await entered.wait()
            recovered = call.collected(1)
            release.set()
            if remote_value == 1:
                assert (await collecting).node_id == recovered.node_id
            else:
                with pytest.raises(VatStateError) as conflict:
                    await collecting
                assert conflict.value.code == "OBSERVATION_CONFLICT"
            assert call.node().node_id == recovered.node_id
            assert recorder.observation(call.execution_id)["value"] == 1
            assert app.function_object.spawn_count == 1
    asyncio.run(run())


def test_waiter_cancellation_during_collection_leaves_execution_resumable(tmp_path):
    async def run():
        app, facade = make_facade(add)
        entered = asyncio.Event()
        with VatRecorder(tmp_path, Keyring.generate().recorder) as recorder:
            call = await facade.with_recorder(recorder).spawn.aio(4, 6)
            original = call.call.get.aio
            async def blocked(timeout=None):
                entered.set()
                await asyncio.Event().wait()
            call.call.get.aio = blocked
            waiter = asyncio.create_task(call.get.aio())
            await entered.wait()
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert recorder.request(call.execution_id)[1:3] == ("running", "fc-test")
            assert recorder.observation(call.execution_id) is None
            assert call.call.cancel_count == 0
            call.call.get.aio = original
            assert (await call.get.aio()).value == 10
            assert app.function_object.spawn_count == 1
    asyncio.run(run())


def test_recorder_close_refuses_inflight_acceptance_until_drained(tmp_path):
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        app, facade = make_facade(add)
        original = app.function_object.spawn.aio
        async def blocked(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)
        app.function_object.spawn.aio = blocked
        recorder = VatRecorder(tmp_path, Keyring.generate().recorder)
        try:
            waiter = asyncio.create_task(facade.with_recorder(recorder).spawn.aio(7))
            await entered.wait()
            with pytest.raises(VatStateError) as error:
                recorder.close()
            assert error.value.code == "SUBMISSIONS_PENDING"
            assert recorder.executions()[0]["state"] == "submitting"
            release.set()
            await recorder.drain()
            call = await waiter
            assert recorder.pending == set()
            assert (await call.get.aio()).value == 7
        finally:
            recorder.close()
    asyncio.run(run())


def test_public_lazy_exports_and_unknown_attribute():
    import vatlog
    from vatlog.modal import VatFunction, VatCall
    assert vatlog.VatFunction is VatFunction
    assert vatlog.VatCall is VatCall
    with pytest.raises(AttributeError, match="unknown_api"):
        getattr(vatlog, "unknown_api")
