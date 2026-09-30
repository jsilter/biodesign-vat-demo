"""Generated CPU proposal and fixed launcher boundaries."""

import math
from pathlib import Path
from uuid import uuid4

import pytest

from runner.design_inputs import DesignError
from runner.dynamic_contracts import validate_dynamic_output, validate_dynamic_proposal
from runner.dynamic_worker import launch_dynamic
from runner.dynamic_worker import dynamic_result_adapter
from runner.dynamic_service import DynamicJobService
from runner.api import create_app
from runner.provider import OpenRouter
import httpx
from runner.design_store import DesignStore
from vatlog.execution import VatRecorder
from vatlog.local import LocalFunction
from vatlog.node import FunctionIdentity
from vatlog.result import VatResult


SOURCE = '''
def analyze(value, output_dir):
    sequence = value["sequence"]
    counts = {residue: sequence.count(residue) for residue in sorted(set(sequence))}
    fraction = sum(residue in "AVILMFWY" for residue in sequence) / len(sequence)
    from pathlib import Path
    Path(output_dir, "summary.txt").write_text(str(len(sequence)), encoding="utf-8")
    return {"length": len(sequence), "counts": counts, "hydrophobic_fraction": fraction}
'''


def proposal(**updates):
    value = {"version": "vat-dynamic-job/1", "id": str(uuid4()), "label": "sequence summary",
             "purpose": "Deterministic descriptive sequence statistics.", "source": SOURCE,
             "entrypoint": "analyze", "input_schema": {"type": "object", "properties": {
                 "sequence": {"type": "string", "minLength": 1, "maxLength": 2048}},
                 "required": ["sequence"], "additionalProperties": False},
             "input": {"sequence": "ACAV"}, "packages": [],
             "output_version": "vat-dynamic-output/1", "resources": {
                 "cpu": 1, "memory_mb": 512, "timeout_seconds": 300, "gpu": None,
                 "network": False, "secrets": False, "volumes": False},
             "public_input": True, "authorized": True}
    value.update(updates)
    return value


def test_dynamic_digest_binds_source_input_and_resources():
    first = validate_dynamic_proposal(proposal())
    same = validate_dynamic_proposal(first.model_dump(mode="json"))
    changed = validate_dynamic_proposal({**first.model_dump(mode="json"),
                                         "source": first.source + "\n# reviewed edit\n"})
    assert first.digest == same.digest
    assert first.digest != changed.digest


@pytest.mark.parametrize("updates", [
    {"packages": ["requests"]},
    {"resources": {"cpu": 1, "memory_mb": 4096, "timeout_seconds": 300, "gpu": None,
                   "network": False, "secrets": False, "volumes": False}},
    {"resources": {"cpu": 1, "memory_mb": 512, "timeout_seconds": 300, "gpu": None,
                   "network": True, "secrets": False, "volumes": False}},
    {"source": "def broken(: pass"},
    {"entrypoint": "../run"},
    {"input": {"wrong": "AC"}},
    {"input_schema": {"type": "object", "patternProperties": {".*": {"type": "string"}}}},
])
def test_dynamic_proposal_rejects_dependency_resource_and_source_escalation(updates):
    with pytest.raises(DesignError) as rejected:
        validate_dynamic_proposal(proposal(**updates))
    assert rejected.value.code == "INVALID_DYNAMIC_JOB"


def test_fixed_launcher_returns_independently_checkable_json_and_regular_artifact():
    output = launch_dynamic(proposal())
    assert output["result"]["length"] == 4
    assert output["result"]["counts"] == {"A": 2, "C": 1, "V": 1}
    assert math.isclose(output["result"]["hydrophobic_fraction"], 0.75)
    assert output["artifacts"] == [{"name": "summary.txt", "raw": b"4"}]


@pytest.mark.parametrize("source", [
    "def analyze(value, output_dir):\n from pathlib import Path\n Path(output_dir, '../escape').write_text('x')\n return {}",
    "def analyze(value, output_dir):\n from pathlib import Path\n Path(output_dir, 'link').symlink_to('/tmp')\n return {}",
])
def test_fixed_launcher_rejects_traversal_or_symlink_outputs(source, tmp_path):
    before = set(Path("/tmp").glob("escape"))
    with pytest.raises(DesignError):
        launch_dynamic(proposal(source=source))
    assert set(Path("/tmp").glob("escape")) == before


def test_dynamic_output_rejects_nonfinite_and_oversized_values():
    with pytest.raises(DesignError):
        validate_dynamic_output({"version": "vat-dynamic-output/1", "result": {"x": math.nan},
                                 "artifacts": []})
    with pytest.raises(DesignError):
        validate_dynamic_output({"version": "vat-dynamic-output/1", "result": {},
                                 "artifacts": [{"name": "x", "raw": b"x" * (4 * 1024 * 1024 + 1)}]})


def dynamic_worker(execution_id, value):
    return launch_dynamic(value)


class LocalDynamicTransport:
    configured = True

    def __init__(self, store):
        self.recorder = getattr(store, "execution_recorder", None) or VatRecorder(
            store.root / "executions", store.keys().recorder)
        store.execution_recorder = self.recorder
        self.prepared = {}
        self.results = {}
        self.submit_count = 0

    def prepare(self, id, value):
        function = LocalFunction(dynamic_worker, descriptor=FunctionIdentity(
            app_name="test-dynamic", function_name="dynamic-job"),
            result_adapter=dynamic_result_adapter).with_recorder(self.recorder)
        self.prepared[id] = function.prepare_call((id, value), {}, execution_id=id)

    async def submit(self, id, value):
        self.submit_count += 1
        self.results[id] = await self.prepared[id].run.aio()
        return "local:" + id

    async def poll(self, call_id):
        return self.results[call_id.removeprefix("local:")].value

    def result(self, id):
        return self.results.get(id)

    async def cancel(self, call_id):
        return None

    async def drain(self):
        await self.recorder.drain()


def test_dynamic_service_returns_vatresult_and_publishes_source_evidence(tmp_path):
    async def exercise():
        store = DesignStore(tmp_path)
        transport = LocalDynamicTransport(store)
        service = DynamicJobService(store, transport)
        value = proposal()
        state = service.propose(value)
        with pytest.raises(DesignError) as changed:
            service.approve(value["id"], "0" * 64, spend_authorized=True)
        assert changed.value.code == "APPROVAL_MISMATCH"
        service.approve(value["id"], state["digest"], spend_authorized=True)
        service.start(value["id"])
        for _ in range(100):
            state = service.snapshot(value["id"])
            if state["status"] != "submitting" and state["status"] != "running":
                break
            await __import__("asyncio").sleep(0.01)
        assert state["status"] == "completed" and state["evidence_status"] == "verified"
        result = transport.result(value["id"])
        assert isinstance(result, VatResult)
        assert result.value["result"]["counts"] == {"A": 2, "C": 1, "V": 1}
        names = {artifact.name for artifact in result.artifacts}
        assert {"approved-source.py", "dependency-lock.json", "image.json", "launcher.txt",
                "summary.txt"} <= names
        assert transport.submit_count == 1
        await service.shutdown()
        store.close()
    __import__("asyncio").run(exercise())


def test_dynamic_reconcile_never_replays_unacknowledged_submission(tmp_path):
    async def exercise():
        store = DesignStore(tmp_path)
        transport = LocalDynamicTransport(store)
        service = DynamicJobService(store, transport)
        value = proposal()
        state = service.propose(value)
        service.approve(value["id"], state["digest"], spend_authorized=True)
        _, durable = store.get_dynamic(value["id"])
        durable["status"] = "submitting"
        store.save_dynamic(value["id"], durable)
        await service.reconcile()
        assert service.snapshot(value["id"])["status"] == "needs_attention"
        assert transport.submit_count == 0
        await service.shutdown()
        store.close()
    __import__("asyncio").run(exercise())


def test_dynamic_loopback_api_runs_only_after_exact_approval_and_exports_node(tmp_path):
    async def exercise():
        provider = OpenRouter("", httpx.AsyncClient())
        app = create_app(provider, design_root=tmp_path,
                         dynamic_transport=lambda store: LocalDynamicTransport(store))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://127.0.0.1:8000") as client:
                value = proposal()
                dry = await client.post("/api/science/dynamic/dry-run", json=value)
                assert dry.status_code == 200 and dry.json()["resources"]["network"] is False
                state = (await client.post("/api/science/dynamic", json=value)).json()
                approved = await client.post(f'/api/science/dynamic/{value["id"]}/approve', json={
                    "digest": state["digest"], "spend_authorized": True})
                assert approved.status_code == 200
                assert (await client.post(f'/api/science/dynamic/{value["id"]}/start')).status_code == 200
                for _ in range(100):
                    state = (await client.get(f'/api/science/dynamic/{value["id"]}')).json()
                    if state["status"] not in {"submitting", "running"}:
                        break
                    await __import__("asyncio").sleep(0.01)
                assert state["status"] == "completed"
                node = await client.get(f'/api/science/dynamic/{value["id"]}/node')
                assert node.status_code == 200 and node.content.startswith(b'{"blobs"')
                source = next(item for item in state["artifacts"] if item["name"] == "approved-source.py")
                raw = await client.get(f'/api/science/dynamic/{value["id"]}/artifacts/'
                                       f'{source["sha256"]}/{source["name"]}')
                assert raw.content == SOURCE.encode()
        await provider.client.aclose()
    __import__("asyncio").run(exercise())
