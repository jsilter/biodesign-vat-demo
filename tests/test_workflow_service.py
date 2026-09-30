"""Durable synthesized-workflow execution with real signed local VAT nodes."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from runner.design_inputs import DesignError
from runner.design_store import DesignStore
from runner.tool_contracts import tool_request
from runner.workflow_service import WorkflowService
from vatlog.execution import VatRecorder
from vatlog.local import LocalFunction
from vatlog.node import FunctionIdentity
from vatlog.result import VatResult


def scientific_worker(execution_id, request):
    sequence = request["payload"]["sequence"]
    return {"artifacts": [], "candidates": [{"source": request["tool"], "sequence": sequence}],
            "metrics": {"length": len(sequence)}, "execution_id": execution_id}


class LocalTransport:
    configured = True

    def __init__(self, store):
        self.recorder = VatRecorder(store.root / "executions", store.keys().recorder)
        store.execution_recorder = self.recorder
        self.prepared = {}
        self.results = {}
        self.submit_count = 0
        self.cancelled = []

    def _function(self, tool):
        return LocalFunction(scientific_worker, descriptor=FunctionIdentity(
            app_name="test-science", function_name=tool)).with_recorder(self.recorder)

    def prepare(self, tool, id, request, *, control_references=()):
        self.prepared[id] = self._function(tool).prepare_call(
            (id, request), {}, execution_id=id, control_references=control_references)

    async def submit(self, tool, id, request):
        self.submit_count += 1
        result = await self.prepared[id].run.aio()
        self.results[id] = result
        return "local:" + id

    async def poll(self, call_id):
        return self.results[call_id.removeprefix("local:")].value

    def result(self, id):
        return self.results.get(id)

    async def cancel(self, call_id):
        self.cancelled.append(call_id)

    async def drain(self):
        await self.recorder.drain()


def proposal(*, authorized=True):
    first, second, third = str(uuid4()), str(uuid4()), str(uuid4())
    request = tool_request("esmc", {"sequence": "AC", "task": "score", "seed": 1})
    return {"version": "vat-workflow/1", "id": str(uuid4()), "label": "fork and join",
            "purpose": "Verify exact producer references.", "nodes": [
        {"id": "source", "tool_id": "esmc", "execution_id": first,
         "request": request, "authorized": authorized},
        {"id": "branch", "tool_id": "esmc", "execution_id": second,
         "request": request, "authorized": authorized, "bindings": [{
             "destination": "/payload/sequence", "producer": "source",
             "selector": {"pointer": "/candidates/0/sequence"}}]},
        {"id": "join", "tool_id": "esmc", "execution_id": third,
         "request": request, "authorized": authorized, "controls": ["source", "branch"]},
    ]}


async def wait_terminal(service, id):
    for _ in range(100):
        state = service.snapshot(id)
        if state["status"] not in {"running", "approved"}:
            return state
        await asyncio.sleep(0.01)
    raise AssertionError("workflow did not terminate")


def test_approval_digest_and_spend_are_separate_gates(tmp_path):
    store = DesignStore(tmp_path)
    service = WorkflowService(store, LocalTransport(store))
    value = proposal()
    try:
        state = service.propose(value)
        with pytest.raises(DesignError) as mismatch:
            service.approve(value["id"], "0" * 64, spend_authorized=True)
        assert mismatch.value.code == "APPROVAL_MISMATCH"
        service.approve(value["id"], state["digest"], spend_authorized=False)
        with pytest.raises(DesignError) as spend:
            service.start(value["id"])
        assert spend.value.code == "SPEND_AUTHORIZATION_REQUIRED"
    finally:
        asyncio.run(service.shutdown())
        store.close()


def test_unapproved_node_cannot_dispatch(tmp_path):
    store = DesignStore(tmp_path)
    transport = LocalTransport(store)
    service = WorkflowService(store, transport)
    value = proposal(authorized=False)
    try:
        state = service.propose(value)
        with pytest.raises(DesignError) as rejected:
            service.approve(value["id"], state["digest"], spend_authorized=True)
        assert rejected.value.code == "AUTHORIZATION_REQUIRED"
        assert transport.submit_count == 0
    finally:
        asyncio.run(service.shutdown())
        store.close()


def test_execution_preserves_data_and_control_lineage_and_publishes_bundle(tmp_path):
    async def exercise():
        store = DesignStore(tmp_path)
        transport = LocalTransport(store)
        service = WorkflowService(store, transport)
        value = proposal()
        state = service.propose(value)
        service.approve(value["id"], state["digest"], spend_authorized=True)
        service.start(value["id"])
        terminal = await wait_terminal(service, value["id"])
        assert terminal["status"] == "completed"
        assert transport.submit_count == 3
        results = [transport.result(node["execution_id"]) for node in value["nodes"]]
        assert all(isinstance(result, VatResult) for result in results)
        binding, = results[1].node.statement().input_bindings
        assert binding.producer_node_id == results[0].node_id
        assert [reference.producer_node_id for reference in results[2].node.statement().control_references] == [
            results[0].node_id, results[1].node_id]
        assert service.snapshot(value["id"])["evidence_status"] == "verified"
        assert {item["name"] for item in store.artifacts(value["id"])} >= {"bundle.zip", "trust.json"}
        from vatlog.bundle import Bundle
        from vatlog.roles import TrustBundle
        from runner.graph import project
        bundle_ref = next(item for item in store.artifacts(value["id"]) if item["name"] == "bundle.zip")
        trust_ref = next(item for item in store.artifacts(value["id"]) if item["name"] == "trust.json")
        bundle_path = tmp_path / "published-bundle.zip"
        bundle_path.write_bytes(store.read(value["id"], bundle_ref["sha256"], bundle_ref["name"]))
        trust_path = tmp_path / "published-trust.json"
        trust_path.write_bytes(store.read(value["id"], trust_ref["sha256"], trust_ref["name"]))
        graph = project(Bundle.load(bundle_path), TrustBundle.load(trust_path))
        assert {edge["kind"] for edge in graph["edges"]} >= {"data", "control"}
        assert graph["provenance_report"]["outcome"] == "VALID"
        await service.shutdown()
        store.close()
    asyncio.run(exercise())


def test_reconcile_never_resubmits_unaccepted_preparation(tmp_path):
    async def exercise():
        store = DesignStore(tmp_path)
        transport = LocalTransport(store)
        service = WorkflowService(store, transport)
        value = proposal()
        state = service.propose(value)
        service.approve(value["id"], state["digest"], spend_authorized=True)
        _, durable = store.get_synthesized(value["id"])
        durable["status"] = "running"
        durable["nodes"][0]["status"] = "submitting"
        store.save_synthesized(value["id"], durable)
        await service.reconcile()
        assert service.snapshot(value["id"])["status"] == "needs_attention"
        assert transport.submit_count == 0
        await service.shutdown()
        store.close()
    asyncio.run(exercise())
