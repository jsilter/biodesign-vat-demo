"""Offline Modal SDK result-protocol tests, not canned exception-only transport mocks.

The real installed SDK decodes serialized return values, failures and polling responses.
Only its network response boundary is substituted. No Modal calls or credentials are used.
"""
from types import SimpleNamespace

import pytest
from modal._functions import _Invocation
from modal._serialization import serialize
from modal_proto import api_pb2

from vatlog.execution import VatObservationPending, VatRecorder
from vatlog.modal import VatFunction
from vatlog.node import FunctionIdentity, NodeStatus, verify_node
from vatlog.roles import Keyring


class SDKResponseFunction:
    def __init__(self, response):
        self.response, self.spawns, self.polls = response, 0, []
        self.spawn = SimpleNamespace(aio=self.submit)

    async def submit(self, *args, **kwargs):
        self.spawns += 1
        # Private SDK objects are solely the test harness for the real public-get implementation.
        invocation = object.__new__(_Invocation)
        invocation.stub = invocation.client = None
        async def outputs(**options):
            assert options["clear_on_success"] is False
            self.polls.append(options["timeout"])
            return self.response
        invocation.pop_function_call_outputs = outputs
        return SimpleNamespace(object_id="fc-offline-protocol", get=SimpleNamespace(aio=invocation.poll_function))


def response(value=None, *, status=api_pb2.GenericResult.GENERIC_STATUS_SUCCESS):
    return api_pb2.FunctionGetOutputsResponse(outputs=[api_pb2.FunctionGetOutputsItem(
        result=api_pb2.GenericResult(status=status, data=serialize(value)), data_format=api_pb2.DATA_FORMAT_PICKLE)])


@pytest.mark.parametrize("value", [None, 144, {"error": "ordinary data"},
                                   {"binary": b"\x00\xff", "values": [True, 1.25, "λ"]}])
def test_real_sdk_deserialization_flows_into_verified_generic_results(tmp_path, value):
    transport = SDKResponseFunction(response(value))
    facade = VatFunction(transport, FunctionIdentity(app_name="offline", function_name="computation"))
    with VatRecorder(tmp_path, Keyring.generate().recorder) as recorder:
        result = facade.with_recorder(recorder).remote("declared input")
        assert result.value == value
        assert result.status is NodeStatus.SUCCEEDED
        assert verify_node(result.node, recorder.trust).passed
        assert transport.spawns == 1 and transport.polls == [None]


@pytest.mark.parametrize("exception", [ValueError("private user detail"), TimeoutError("private user deadline"),
                                       ZeroDivisionError("private user arithmetic")])
def test_real_sdk_rethrown_user_exceptions_become_safe_terminal_nodes(tmp_path, exception):
    transport = SDKResponseFunction(response(exception, status=api_pb2.GenericResult.GENERIC_STATUS_FAILURE))
    facade = VatFunction(transport, FunctionIdentity(app_name="offline", function_name="computation"))
    with VatRecorder(tmp_path, Keyring.generate().recorder) as recorder:
        result = facade.with_recorder(recorder).remote()
        assert result.status is NodeStatus.FUNCTION_ERROR
        assert result.error.type == type(exception).__name__
        assert str(exception).encode() not in result.node.canonical_bytes()
        assert recorder.observation(result.execution_id)["error"]["type"] == type(exception).__name__
        assert verify_node(result.node, recorder.trust).passed


def test_real_sdk_poll_timeout_and_worker_timeout_are_distinguished_by_unbounded_collection(tmp_path):
    transport = SDKResponseFunction(api_pb2.FunctionGetOutputsResponse(num_unfinished_inputs=1))
    facade = VatFunction(transport, FunctionIdentity(app_name="offline", function_name="computation"))
    with VatRecorder(tmp_path, Keyring.generate().recorder) as recorder:
        call = facade.with_recorder(recorder).spawn()
        with pytest.raises(VatObservationPending) as pending:
            call.get(timeout=0)
        assert pending.value.code == "PENDING"
        assert call.node() is None
        transport.response = response(TimeoutError("private timeout"), status=api_pb2.GenericResult.GENERIC_STATUS_FAILURE)
        # Identical exception types in a finite poll cannot prove which source produced them.
        with pytest.raises(VatObservationPending):
            call.get(timeout=0)
        assert recorder.observation(call.execution_id) is None
        result = call.get()
        assert result.status is NodeStatus.FUNCTION_ERROR
        assert result.error.type == "TimeoutError"
        assert transport.spawns == 1 and transport.polls == [0, 0, None]


def test_real_sdk_expired_output_is_not_fabricated_as_execution_failure(tmp_path):
    transport = SDKResponseFunction(api_pb2.FunctionGetOutputsResponse(num_unfinished_inputs=0))
    facade = VatFunction(transport, FunctionIdentity(app_name="offline", function_name="computation"))
    with VatRecorder(tmp_path, Keyring.generate().recorder) as recorder:
        call = facade.with_recorder(recorder).spawn()
        with pytest.raises(VatObservationPending) as pending:
            call.get()
        assert pending.value.code == "OBSERVATION_PENDING"
        assert recorder.request(call.execution_id)[1] == "observation_pending"
        assert call.node() is None
        result = call.collected({"recovered": 42})
        assert result.value == {"recovered": 42}
        assert transport.spawns == 1


def test_real_sdk_execution_timeout_is_terminal_not_a_poll_deadline(tmp_path):
    transport = SDKResponseFunction(response(status=api_pb2.GenericResult.GENERIC_STATUS_TIMEOUT))
    facade = VatFunction(transport, FunctionIdentity(app_name="offline", function_name="computation"))
    with VatRecorder(tmp_path, Keyring.generate().recorder) as recorder:
        call = facade.with_recorder(recorder).spawn()
        result = call.get(timeout=0)
        assert result.status is NodeStatus.EXECUTION_ERROR
        assert result.error.code == "EXECUTION_TIMEOUT"
        assert verify_node(result.node, recorder.trust).passed
