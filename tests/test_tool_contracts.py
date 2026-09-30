"""Common scientific request and immutable VatResult boundary."""

import asyncio
import copy
import hashlib
from types import SimpleNamespace

import modal
import pytest

from runner.design_inputs import DesignError
from runner.design_store import DesignStore, json_bytes
from runner.modal_client import ModalTools
from runner.tool_contracts import worker_request, tool_request, validate_tool_request
from vatlog.result import VatResult
from test_modal_vat import FakeCall


def test_family_requests_share_one_envelope_and_reject_unbounded_or_cross_tool_fields():
    generated = tool_request("pxdesign", {
        "target_cif": "data_target\n#\n", "target_chains": ["A"], "target_sequences": {"A": "AC"},
        "count": 2, "seed": 7, "min_length": 40, "max_length": 40, "hotspots": [1],
    })
    assert generated == {"version": "protein-tool/1", "tool": "pxdesign", "operation": "design",
                         "payload": {"target_cif": "data_target\n#\n", "target_chains": ["A"],
                                     "target_sequences": {"A": "AC"}, "count": 2, "seed": 7,
                                    "min_length": 40, "max_length": 40, "hotspots": [1]}}
    with pytest.raises(DesignError) as mismatch:
        validate_tool_request(generated, expected_tool="genie3")
    assert mismatch.value.code == "INVALID_REQUEST"
    with pytest.raises(DesignError):
        tool_request("pxdesign", {**generated["payload"], "command": "private-shell"})
    with pytest.raises(DesignError):
        tool_request("pxdesign", {**generated["payload"], "count": 101})
    with pytest.raises(DesignError):
        tool_request("pxdesign", {**generated["payload"], "target_sequences": {"B": "AC"}})
    with pytest.raises(DesignError):
        tool_request("pxdesign", {**generated["payload"], "template_cif": "data_template\n#\n"})
    with pytest.raises(DesignError) as unknown:
        tool_request("unknown", {})
    assert unknown.value.code == "INVALID_REQUEST"


def test_prediction_and_utility_contracts_make_features_and_databases_explicit():
    prediction = tool_request("esmfold2-full", {"sequences": {"A": "AC", "B": "DE"},
        "msa_mode": "provided", "msa_a3m": ">A\nAC\n", "template_policy": "none", "seed": 0})
    assert prediction["payload"]["msa_a3m"] == ">A\nAC\n"
    with pytest.raises(DesignError):
        tool_request("esmfold2-full", {"sequences": {"A": "AC"}, "msa_mode": "provided", "seed": 0})
    utility = tool_request("foldseek", {"structures": ["data_query\n#\n"],
                                        "database": "pdb-2026-09", "max_hits": 25})
    assert utility["payload"]["database"] == "pdb-2026-09"
    with pytest.raises(DesignError):
        tool_request("foldseek", {"structures": ["x"], "database": "../private"})
    with pytest.raises(DesignError):
        tool_request("boltz2", {"sequences": {"A": "AC"}, "msa_mode": "provided",
                                "msa_a3m": ">A\nAC\n", "template_policy": "none", "seed": 1})
    with pytest.raises(DesignError):
        tool_request("af-unmasked", {"sequences": {"A": "AC"}, "msa_mode": "none",
            "template_policy": "provided", "templates_cif": ["data_one\n#\n"], "count": 2, "seed": 1})


def test_existing_endpoints_have_deterministic_compatibility_mapping():
    esmc = tool_request("esmc", {"sequence": "AC", "task": "score", "seed": 1})
    assert worker_request(esmc, expected_tool="esmc") == {"sequence": "AC"}
    fold = tool_request("esmfold2", {"sequences": {"A": "AC", "B": "DE"}, "query_chain": "B",
                                             "msa_mode": "none", "template_policy": "none", "seed": 9})
    assert worker_request(fold, expected_tool="esmfold2") == {
        "sequence": "DE", "mode": "complex", "target_sequence": "AC", "seed": 9,
        "num_loops": 10, "num_sampling_steps": 68, "num_diffusion_samples": 1}


def test_common_run_returns_vatresult_for_domain_success_and_failure(tmp_path, monkeypatch):
    from runner import modal_app

    calls = {}
    def execute(tool, id, request):
        assert request == tool_request("esmc", {"sequence": "AC", "task": "score", "seed": 1})
        value = {"artifacts": [{"name": "scores.json", "kind": "metric", "raw": b"{}"}],
                 "metrics": {"mean": -0.5}}
        if execute.failed:
            value = {"error": "EXECUTION_FAILED", "artifacts": [
                {"name": "partial.log", "kind": "log", "raw": b"partial"}]}
        value["execution"] = {"id": id, "call_id": "fc-" + id, "tool": tool, "attempt": 1,
                              "request_sha256": hashlib.sha256(json_bytes(request)).hexdigest()}
        return value
    execute.failed = False
    monkeypatch.setattr(modal_app, "execute", execute)
    monkeypatch.setenv("VAT_MODAL_ENABLED", "1")

    def lookup(app, tool):
        assert app == "biodesign-vat-tools" and tool == "esmc"
        facade = modal_app.esmc
        async def spawn(id, request):
            result = facade.modal_function.local(id, copy.deepcopy(request))
            call = FakeCall("fc-" + id, result)
            calls[call.object_id] = call
            return call
        return SimpleNamespace(spawn=SimpleNamespace(aio=spawn))
    monkeypatch.setattr(modal.Function, "from_name", lookup)

    store, transport = DesignStore(tmp_path), ModalTools()
    transport.bind_store(store)
    request = tool_request("esmc", {"sequence": "AC", "task": "score", "seed": 1})
    try:
        result = transport.run("esmc", request)
        assert isinstance(result, VatResult)
        assert result.value["metrics"] == {"mean": -0.5}
        assert result.domain_outcome.status == "succeeded"
        assert next(a for a in result.artifacts if a.name == "scores.json").read_bytes() == b"{}"
        execute.failed = True
        failed = asyncio.run(transport.run.aio("esmc", request))
        assert isinstance(failed, VatResult) and failed.value["error"] == "EXECUTION_FAILED"
        assert failed.domain_outcome.status == "failed"
        assert next(a for a in failed.artifacts if a.name == "partial.log").classification == "intermediate"
    finally:
        store.close()


@pytest.mark.parametrize("tool", ["alphafold3", "boltz1", "mmseqs2", "unknown"])
def test_common_run_rejects_inventory_only_or_unstaged_tools_before_modal_lookup(tool, monkeypatch):
    monkeypatch.setenv("VAT_MODAL_ENABLED", "1")
    monkeypatch.setattr(modal.Function, "from_name", lambda *args: pytest.fail("Modal lookup occurred"))

    with pytest.raises(DesignError) as rejected:
        asyncio.run(ModalTools().run.aio(tool, {"version": "protein-tool/1", "tool": tool,
                                                "operation": "predict", "payload": {}}))

    assert rejected.value.code == "TOOL_UNAVAILABLE"


def test_common_run_rejects_invalid_execution_identity_before_preparing_call(monkeypatch):
    monkeypatch.setenv("VAT_MODAL_ENABLED", "1")
    request = tool_request("dssp", {"structures": ["data_test\n#\n"]})

    with pytest.raises(DesignError) as rejected:
        asyncio.run(ModalTools().run.aio("dssp", request, execution_id="not-a-uuid"))

    assert rejected.value.code == "INVALID_REQUEST"
