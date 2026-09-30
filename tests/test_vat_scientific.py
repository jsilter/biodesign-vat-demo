"""Production scientific facade, journal and publication with unpaid computation stubs."""
import asyncio
import copy
import hashlib
from types import SimpleNamespace
from uuid import uuid4

import modal
import pytest

from runner.design_inputs import DesignError, masked_metrics
from runner.design_store import DesignStore, json_bytes
from runner.modal_client import ModalTools
from vatlog.bundle import Bundle
from vatlog.modal import VatFunction
from vatlog.node import verify_node
from vatlog.verify import verify_bundle
from test_design_workflow import coordinates, payload, setup, standalone_state
from test_modal_vat import FakeCall


def install_workers(monkeypatch, compute):
    from runner import modal_app
    calls, submitted = {}, []

    def execute(tool, id, request):
        value = copy.deepcopy(compute(tool, request))
        value["execution"] = {"id": id, "call_id": "fc-" + id, "tool": tool, "attempt": 1,
                              "request_sha256": hashlib.sha256(json_bytes(request)).hexdigest()}
        return value

    monkeypatch.setattr(modal_app, "execute", execute)
    monkeypatch.setenv("VAT_MODAL_ENABLED", "1")

    def lookup(app, tool):
        facade = getattr(modal_app, tool)
        assert isinstance(facade, VatFunction)
        async def spawn(id, request):
            submitted.append((tool, id))
            # Use the actual registered production entrypoint, only scientific compute is substituted.
            result = facade.modal_function.local(id, request)
            call = FakeCall("fc-" + id, result)
            calls[call.object_id] = call
            return call
        return SimpleNamespace(spawn=SimpleNamespace(aio=spawn))

    monkeypatch.setattr(modal.Function, "from_name", lookup)
    monkeypatch.setattr(modal.FunctionCall, "from_id", lambda id: calls[id])
    return calls, submitted


@pytest.fixture
def recorded_transport(tmp_path, monkeypatch):
    """Real recorder/store with only the remote invocation boundary substituted."""
    calls, submitted = install_workers(monkeypatch, outputs)
    store, transport = DesignStore(tmp_path), ModalTools()
    transport.bind_store(store)
    try:
        yield transport, calls, submitted
    finally:
        store.close()


def outputs(tool, request):
    if tool == "boltzgen":
        return {"candidates": [{"source": "generated", "sequence": "A" * 60}],
                "artifacts": [{"name": "generated.cif", "kind": "coordinates",
                               "raw": coordinates({"A": request["target_sequence"], "B": "A" * 60})}]}
    if tool == "esmfold2":
        sequences = {"B": request["sequence"]}
        if request["mode"] == "complex":
            sequences["A"] = request["target_sequence"]
        return {"metrics": {"mean_plddt": .75, "plddt_raw_scale": "0–1",
                            "plddt": [.75] * sum(map(len, sequences.values())), "ptm": .6},
                "artifacts": [{"name": "prediction.cif", "kind": "coordinates", "raw": coordinates(sequences)}]}
    return {"metrics": masked_metrics([-0.5] * len(request["sequence"])), "artifacts": []}


def published_bundle(service, id, tmp_path):
    snapshot = service.snapshot(id)
    assert snapshot["evidence_status"] == "verified"
    ref = next(a for a in snapshot["artifacts"] if a["name"] == "bundle.zip")
    path = tmp_path / "published.zip"
    path.write_bytes(service.store.read(id, ref["sha256"], ref["name"]))
    bundle = Bundle.load(path)
    assert snapshot["record_count"] == bundle.signed_tree_head.tree_size
    return bundle


def assert_exact_nodes(service, id, bundle):
    _, state = service.store.get(id)
    embedded = [a for r in bundle.records for a in r.intermediates if a.kind == "vat_node"]
    assert len(embedded) == len(state["jobs"])
    for job in state["jobs"]:
        result = service.transport.result(job["execution_id"])
        artifact, = [a for a in embedded if a.sha256 == result.node_id]
        assert bundle.blobs[artifact.sha256] == result.node.canonical_bytes()
        assert verify_node(result.node, service.store.keys().trust_bundle()).passed
    return embedded


def test_designer_uses_shared_nodes_with_actual_candidate_binding(tmp_path, monkeypatch):
    _, submitted = install_workers(monkeypatch, outputs)
    async def run():
        service = setup(tmp_path / "store", ModalTools())
        try:
            id = service.start(payload())["id"]
            await service.tasks[id]
            assert service.snapshot(id)["status"] == "completed"
            assert [tool for tool, _ in submitted] == ["boltzgen", "esmfold2", "esmfold2", "esmc"]
            bundle = published_bundle(service, id, tmp_path)
            assert_exact_nodes(service, id, bundle)
            trust = service.store.keys().trust_bundle()
            assert verify_bundle(bundle, trust, submitted_sequence="A" * 60).exit_code == 0
            assert verify_bundle(bundle, trust, submitted_sequence="C" * 60).exit_code == 5
            # Publishing twice is idempotent and never performs more computation.
            service.publish(id)
            assert len(submitted) == 4
        finally:
            await service.provider.client.aclose()
            service.store.close()
    asyncio.run(run())


@pytest.mark.parametrize("tool", ["esmc", "esmfold2", "proteinmpnn", "boltzgen"])
def test_all_standalone_tools_keep_domain_failures_and_partial_intermediates(tmp_path, monkeypatch, tool):
    _, submitted = install_workers(monkeypatch, lambda *_: {
        "error": "EXECUTION_FAILED", "artifacts": [{"name": "partial.log", "kind": "log", "raw": b"partial"}]})
    request = {"sequence": "AC"}
    if tool == "proteinmpnn":
        from runner.design_inputs import prepare_backbone
        from test_design_inputs import backbone
        prepared = prepare_backbone(backbone(), "cif")
        request = {"backbone_cif": prepared["prepared_cif"], "sequences": prepared["sequences"],
                   "chain_map": prepared["chain_map"], "mapping": prepared["mapping"],
                   "design_chains": ["A"], "count": 1, "seed": 1,
                   "sampling_temperature": .1, "model": "v_48_020"}
    async def run():
        service = setup(tmp_path / "store", ModalTools())
        id = str(uuid4())
        service.store.create(id, {"prompt": "Standalone", "settings": request, "model": "none"}, standalone_state(id))
        try:
            result = await service.scientific(id, tool, request)
            assert result["error"] == "EXECUTION_FAILED"
            _, state = service.store.get(id)
            state["status"] = "failed"
            service.store.save(id, state)
            service.publish(id)
            bundle = published_bundle(service, id, tmp_path)
            assert_exact_nodes(service, id, bundle)
            recorded = service.transport.result(state["jobs"][0]["id"])
            assert recorded.status.value == "succeeded"
            assert recorded.value["error"] == "EXECUTION_FAILED"
            assert recorded.domain_outcome.status == "failed"
            assert next(a for a in recorded.artifacts if a.name == "partial.log").classification == "intermediate"
            assert len(submitted) == 1 and service.snapshot(id)["candidates"] == []
        finally:
            await service.provider.client.aclose()
            service.store.close()
    asyncio.run(run())


def test_scientific_rejection_does_not_rewrite_success_or_accept_sequence(tmp_path, monkeypatch):
    def invalid(tool, request):
        value = outputs(tool, request)
        if tool == "boltzgen":
            value["candidates"][0]["sequence"] = "C" * 60  # contradicts the actual coordinates
        return value
    install_workers(monkeypatch, invalid)
    async def run():
        service = setup(tmp_path / "store", ModalTools())
        try:
            id = service.start(payload())["id"]
            await service.tasks[id]
            assert service.snapshot(id)["candidates"] == []
            bundle = published_bundle(service, id, tmp_path)
            assert_exact_nodes(service, id, bundle)
            _, state = service.store.get(id)
            recorded = service.transport.result(state["jobs"][0]["id"])
            assert recorded.status.value == "succeeded"
            assert recorded.value["candidates"][0]["sequence"] == "C" * 60
            assert state["jobs"][0]["result"]["error"] == "OUTPUT_INVALID"
            assert verify_bundle(bundle, service.store.keys().trust_bundle(), submitted_sequence="C" * 60).exit_code == 5
        finally:
            await service.provider.client.aclose()
            service.store.close()
    asyncio.run(run())


@pytest.mark.parametrize("wrong_identity", [False, True])
def test_expired_output_recovers_once_using_validated_manifest(tmp_path, monkeypatch, wrong_identity):
    calls, submitted = install_workers(monkeypatch, outputs)
    store, transport = DesignStore(tmp_path), ModalTools()
    transport.bind_store(store)
    id, request = str(uuid4()), {"sequence": "AC"}
    async def run():
        transport.prepare("esmc", id, request)
        call_id = await transport.submit("esmc", id, request)
        response = calls[call_id].action
        calls[call_id].action = modal.exception.OutputExpiredError("expired")
        start = {**response["execution"]}
        if wrong_identity:
            start["request_sha256"] = "0" * 64
        async def recovery(job_id):
            return {"start": start, "result": response}
        original = modal.Function.from_name
        monkeypatch.setattr(modal.Function, "from_name", lambda app, name:
            SimpleNamespace(remote=SimpleNamespace(aio=recovery)) if name == "recover" else original(app, name))
        if wrong_identity:
            with pytest.raises(DesignError) as mismatch:
                await transport.poll(call_id)
            assert mismatch.value.code == "DEPENDENCY_MISMATCH"
            assert transport.result(id) is None
        else:
            assert await transport.poll(call_id) == response
            before = transport.result(id).node_id
            assert (await transport.recover(id))["result"] == response
            assert transport.result(id).node_id == before
        assert len(submitted) == 1
    try:
        asyncio.run(run())
    finally:
        store.close()


def test_production_functions_load_in_actual_sdk_container_importer():
    import subprocess
    import sys
    code = '''
import modal
modal.is_local = lambda: False
from modal._runtime.user_code_imports import import_single_function_service
from modal_proto import api_pb2
from runner import modal_app
modal_app.execute = lambda tool, id, request: {"tool": tool, "value": request}
for tool in ("esmc", "esmfold2", "proteinmpnn", "boltzgen"):
    assert isinstance(getattr(modal_app, tool), modal.Function)
    service = import_single_function_service(api_pb2.Function(
        module_name="runner.modal_app", implementation_name=tool,
        function_name=tool, app_name="biodesign-vat-tools"), None)
    assert service._user_defined_callable("id", {"x": 1}) == {"tool": tool, "value": {"x": 1}}
'''
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_standalone_proteinmpnn_success_uses_same_capture_path(tmp_path, monkeypatch):
    from runner.design_inputs import prepare_backbone
    from runner.design_workers import parse_proteinmpnn_outputs
    from test_design_inputs import backbone
    from test_design_workers import proteinmpnn_bytes
    prepared = prepare_backbone(backbone(), "cif")
    request = {"backbone_cif": prepared["prepared_cif"], "sequences": prepared["sequences"],
               "chain_map": prepared["chain_map"], "mapping": prepared["mapping"],
               "design_chains": ["A", "B"], "count": 2, "sampling_temperature": .1,
               "seed": 1, "model": "v_48_020"}
    fasta, scores = proteinmpnn_bytes()
    fasta = fasta.replace(b"AC/CA", b"AC/AC", 1)
    candidates, metrics = parse_proteinmpnn_outputs(fasta, scores, request)
    _, submitted = install_workers(monkeypatch, lambda *_: {"candidates": candidates, "metrics": metrics,
        "artifacts": [{"name": "backbone.fa", "kind": "fasta", "raw": fasta},
                      {"name": "scores.npz", "kind": "metric", "raw": scores}]})
    async def run():
        service = setup(tmp_path / "store", ModalTools())
        id = str(uuid4())
        service.store.create(id, {"prompt": "Standalone ProteinMPNN", "settings": request, "model": "none"},
                             standalone_state(id))
        try:
            summary = await service.scientific(id, "proteinmpnn", request)
            assert summary["metrics"] == metrics
            _, state = service.store.get(id)
            state["status"] = "completed"
            service.store.save(id, state)
            service.publish(id)
            bundle = published_bundle(service, id, tmp_path)
            assert_exact_nodes(service, id, bundle)
            assert len(service.snapshot(id)["candidates"]) == 4
            assert verify_bundle(bundle, service.store.keys().trust_bundle(), submitted_sequence="AA").exit_code == 0
            assert len(submitted) == 1
        finally:
            await service.provider.client.aclose()
            service.store.close()
    asyncio.run(run())


def test_lost_acceptance_recovers_manifest_without_second_submission(tmp_path, monkeypatch):
    calls, submitted = install_workers(monkeypatch, outputs)
    original = modal.Function.from_name
    id, request = str(uuid4()), {"sequence": "AC"}
    async def recovery(job_id):
        value = calls["fc-" + id].action
        return {"start": value["execution"], "result": value}
    def lookup(app, name):
        if name == "recover":
            return SimpleNamespace(remote=SimpleNamespace(aio=recovery))
        function = original(app, name)
        spawn = function.spawn.aio
        async def lost_ack(*args, **kwargs):
            await spawn(*args, **kwargs)
            raise modal.exception.ConnectionError("ack lost after acceptance")
        function.spawn.aio = lost_ack
        return function
    monkeypatch.setattr(modal.Function, "from_name", lookup)
    store, transport = DesignStore(tmp_path), ModalTools()
    transport.bind_store(store)
    async def run():
        transport.prepare("esmc", id, request)
        with pytest.raises(DesignError, match="confirmed"):
            await transport.submit("esmc", id, request)
        assert transport.recorder.request(id)[1:3] == ("submission_uncertain", None)
        recovered = await transport.recover(id)
        assert recovered["start"]["call_id"] == "fc-" + id
        assert transport.result(id).value == recovered["result"]
        assert transport.recorder.request(id)[1:3] == ("completed", "fc-" + id)
        assert len(submitted) == 1
    try:
        asyncio.run(run())
    finally:
        store.close()


def test_terminal_cancellation_retains_recovered_partials_without_rewriting_node(tmp_path, monkeypatch):
    calls, submitted = install_workers(monkeypatch, outputs)
    store, transport = DesignStore(tmp_path), ModalTools()
    transport.bind_store(store)
    id = str(uuid4())
    async def run():
        transport.prepare("esmc", id, {"sequence": "AC"})
        call_id = await transport.submit("esmc", id, {"sequence": "AC"})
        start = calls[call_id].action["execution"]
        calls[call_id].action = modal.exception.InputCancellation("cancelled")
        async def recovery(job_id):
            return {"start": start, "partial_artifacts": [{"name": "partial.log", "kind": "log", "raw": b"partial"}]}
        original = modal.Function.from_name
        monkeypatch.setattr(modal.Function, "from_name", lambda app, name:
            SimpleNamespace(remote=SimpleNamespace(aio=recovery)) if name == "recover" else original(app, name))
        value = await transport.poll(call_id)
        result = transport.result(id)
        assert result.status.value == "cancelled"
        assert result.node.statement().results == []
        assert value["error"] == "CANCELLED" and value["artifacts"][0]["raw"] == b"partial"
        assert (await transport.poll(call_id)) == value
        assert transport.result(id).node_id == result.node_id
        assert len(submitted) == 1
    try:
        asyncio.run(run())
    finally:
        store.close()


@pytest.fixture
def local_worker_files(tmp_path, monkeypatch):
    import sys
    from pathlib import Path
    from runner import modal_app, design_workers
    jobs, weights, locks = tmp_path / "jobs", tmp_path / "weights", tmp_path / "locks"
    weights.mkdir()
    locks.mkdir()
    (weights / "deployment-lock.json").write_text('{}')
    for filename in ("esm.txt", "boltz.txt", "proteinmpnn.txt"):
        (locks / filename).write_text('test-dependency==1')
    translated = {"/jobs": jobs, "/weights": weights, "/opt/vat-locks": locks}
    monkeypatch.setattr(modal_app, "Path", lambda path: translated.get(str(path), Path(path)))
    monkeypatch.setattr(modal_app, "volume", SimpleNamespace(commit=lambda: None, reload=lambda: None))
    monkeypatch.setattr(modal, "current_function_call_id", lambda: "fc-persisted")
    monkeypatch.setattr(design_workers, "validate_weight_lock", lambda *args: None)
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)))
    return modal_app, jobs


@pytest.mark.parametrize("tool", ["boltzgen", "esmfold2", "esmc", "proteinmpnn"])
@pytest.mark.parametrize("failed", [False, True])
def test_worker_persistence_returns_exact_recoverable_value(local_worker_files, monkeypatch, tool, failed):
    from vatlog.node import encode_document
    modal_app, jobs = local_worker_files

    def compute(*args):
        if failed:
            raise RuntimeError("Private model failure")
        return {"metrics": {"actual": 1.0}, "artifacts": [
            {"name": "raw.bin", "kind": "metric", "raw": b"computed bytes"}]}

    runner = {"boltzgen": "run_boltzgen", "esmfold2": "run_fold", "esmc": "run_esmc", "proteinmpnn": "run_proteinmpnn"}
    monkeypatch.setattr(modal_app, runner[tool], compute)
    id = str(uuid4())
    returned = modal_app.execute(tool, id, {"sequence": "AC"})
    recovered = modal_app.recover.local(id)["result"]
    assert recovered == returned
    assert encode_document(returned, "return.json").artifact == encode_document(recovered, "return.json").artifact
    assert returned["execution"]["tool"] == tool
    expected_gpu = {"esmfold2": "A100-40GB", "boltzgen": "H100", "esmc": "L4", "proteinmpnn": "L4"}
    assert returned["execution"]["gpu_profile"] == expected_gpu[tool]
    assert returned["execution"]["call_id"] == "fc-persisted"
    if failed:
        assert returned["error"] == "EXECUTION_FAILED"
        assert "Private model failure" not in str(recovered)
    else:
        assert returned["artifacts"][0]["raw"] == b"computed bytes"
    original = (jobs / id / "result.json").read_bytes()
    with pytest.raises(ValueError, match="redelivery"):
        modal_app.execute(tool, id, {"sequence": "CC"})
    assert (jobs / id / "result.json").read_bytes() == original


@pytest.mark.parametrize("mutation", ["traversal", "symlink", "digest", "length"])
def test_worker_recovery_rejects_tampered_artifact_manifest(local_worker_files, mutation):
    import json
    modal_app, jobs = local_worker_files
    id = str(uuid4())
    work = jobs / id
    work.mkdir(parents=True)
    raw = b"original bytes"
    (work / "artifact-0").write_bytes(raw)
    ref = {"file": "artifact-0", "name": "result.log", "kind": "log",
           "sha256": hashlib.sha256(raw).hexdigest(), "bytes_len": len(raw)}
    if mutation == "traversal":
        ref["file"] = "../artifact-0"
    elif mutation == "symlink":
        (work / "link").symlink_to(work / "artifact-0")
        ref["file"] = "link"
    elif mutation == "digest":
        (work / "artifact-0").write_bytes(b"modified bytes")
    else:
        ref["bytes_len"] += 1
    (work / "result.json").write_text(json.dumps({"artifacts": [ref]}))
    with pytest.raises(ValueError, match="Unsafe|integrity"):
        modal_app.recover.local(id)


@pytest.mark.parametrize("tool", ["boltzgen", "proteinmpnn"])
def test_worker_recovery_retains_unfinished_outputs_without_inventing_result(local_worker_files, tool):
    import json
    modal_app, jobs = local_worker_files
    id = str(uuid4())
    assert modal_app.recover.local(id) == {}
    work = jobs / id
    partial = work / ("output/intermediate_designs/partial.cif" if tool == "boltzgen" else "proteinmpnn-output/seqs/backbone.fa")
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"incomplete scientific bytes")
    (work / "start.json").write_text(json.dumps({"tool": tool, "id": id}))
    recovered = modal_app.recover.local(id)
    assert "result" not in recovered
    assert recovered["start"] == {"tool": tool, "id": id}
    assert recovered["partial_artifacts"][0]["raw"] == b"incomplete scientific bytes"


def test_submit_rejects_changed_request_before_dispatch(tmp_path, monkeypatch):
    _, submitted = install_workers(monkeypatch, outputs)
    store, transport = DesignStore(tmp_path), ModalTools()
    transport.bind_store(store)
    id = str(uuid4())
    try:
        transport.prepare("esmc", id, {"sequence": "AC"})
        with pytest.raises(DesignError) as error:
            asyncio.run(transport.submit("esmc", id, {"sequence": "CC"}))
        assert error.value.code == "REQUEST_CONFLICT"
        assert transport.recorder.request(id)[1] == "prepared"
        assert submitted == []
    finally:
        store.close()


@pytest.mark.parametrize("malformed", [[], {"start": "not-a-manifest"}, {"start": {"attempt": True}}])
def test_recovery_rejects_malformed_identity_without_adoption(tmp_path, monkeypatch, malformed):
    install_workers(monkeypatch, outputs)
    store, transport = DesignStore(tmp_path), ModalTools()
    transport.bind_store(store)
    id = str(uuid4())
    transport.prepare("esmc", id, {"sequence": "AC"})
    transport.recorder.set_state(id, "submitting")
    transport.recorder.set_state(id, "submission_uncertain")
    async def recovery(id):
        return malformed
    monkeypatch.setattr(modal.Function, "from_name", lambda *_: SimpleNamespace(remote=SimpleNamespace(aio=recovery)))
    try:
        with pytest.raises(DesignError):
            asyncio.run(transport.recover(id))
        assert transport.recorder.request(id)[1:3] == ("submission_uncertain", None)
        assert transport.result(id) is None
    finally:
        store.close()


@pytest.mark.parametrize("scenario,code", [
    ("missing_result", "CONNECTION_LOST"), ("unavailable", "SUBMISSION_UNCERTAIN"),
    ("wrong_result_identity", "EVIDENCE_FAILED"), ("unknown_call", "EXECUTION_NOT_FOUND"),
])
def test_transport_failure_preserves_dispatch_and_never_resubmits(recorded_transport, monkeypatch, scenario, code):
    transport, calls, submitted = recorded_transport
    id, request = str(uuid4()), {"sequence": "AC"}

    async def run():
        transport.prepare("esmc", id, request)
        call_id = await transport.submit("esmc", id, request)
        if scenario == "wrong_result_identity":
            calls[call_id].action["execution"]["call_id"] = "fc-impostor"
        else:
            calls[call_id].action = modal.exception.OutputExpiredError("expired")

        async def recover(job_id):
            if scenario == "unavailable":
                raise modal.exception.ConnectionError("Private remote error")
            return {}

        monkeypatch.setattr(modal.Function, "from_name", lambda *_: SimpleNamespace(remote=SimpleNamespace(aio=recover)))
        with pytest.raises(DesignError) as error:
            await transport.poll("fc-unknown" if scenario == "unknown_call" else call_id)
        assert error.value.code == code
        assert transport.result(id) is None
        assert transport.recorder.request(id)[2] == call_id
        assert len(submitted) == 1
    asyncio.run(run())


@pytest.mark.parametrize("partial_result", ["valid", "invalid", "unavailable"])
def test_terminal_failure_recovery_cannot_replace_signed_outcome(recorded_transport, monkeypatch, partial_result):
    transport, calls, submitted = recorded_transport
    id, request = str(uuid4()), {"sequence": "AC"}

    async def run():
        transport.prepare("esmc", id, request)
        call_id = await transport.submit("esmc", id, request)
        value = copy.deepcopy(calls[call_id].action)
        value["artifacts"] = [{"name": "partial.log", "kind": "log", "raw": b"partial"}]
        if partial_result == "invalid":
            value["execution"]["id"] = str(uuid4())
        calls[call_id].action = modal.exception.InputCancellation("cancelled")

        async def recover(job_id):
            if partial_result == "unavailable":
                raise modal.exception.ConnectionError("Private error")
            return {"result": value}

        monkeypatch.setattr(modal.Function, "from_name", lambda *_: SimpleNamespace(remote=SimpleNamespace(aio=recover)))
        if partial_result == "invalid":
            with pytest.raises(DesignError) as error:
                await transport.poll(call_id)
            assert error.value.code == "EVIDENCE_FAILED"
        else:
            observed = await transport.poll(call_id)
            assert observed["error"] == "CANCELLED"
            assert observed["artifacts"] == (value["artifacts"] if partial_result == "valid" else [])
        result = transport.result(id)
        assert result.status.value == "cancelled"
        assert result.node.statement().results == []
        assert verify_node(result.node, transport.recorder.trust).passed
        assert len(submitted) == 1
    asyncio.run(run())


def test_transport_rejects_unbound_disabled_and_unknown_jobs_without_remote_calls(tmp_path):
    transport = ModalTools()
    id = str(uuid4())
    with pytest.raises(DesignError, match="not been bound"):
        transport.prepare("esmc", id, {"sequence": "AC"})
    store = DesignStore(tmp_path)
    transport.bind_store(store)
    try:
        with pytest.raises(DesignError) as error:
            asyncio.run(transport.submit("esmc", id, {"sequence": "AC"}))
        assert error.value.code == "TOOL_UNAVAILABLE"
        with pytest.raises(DesignError) as error:
            asyncio.run(transport.recover(id))
        assert error.value.code == "EXECUTION_NOT_FOUND"
        assert transport.recorder.executions() == []
    finally:
        store.close()
