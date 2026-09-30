"""The deployment keeps static endpoints and the shared VAT result boundary."""

import asyncio
import copy
import hashlib
import importlib
import json
import sys
from types import SimpleNamespace
from uuid import uuid4

import modal

from runner.design_store import DesignStore, json_bytes
from runner.kit_workers import KIT_TOOL_IDS
from runner.modal_client import ModalTools
from runner.suite_executor import IMPLEMENTED_SUITE_TOOLS, execute_suite, recover_suite
from runner.tool_contracts import tool_request
from vatlog.modal import VatFunction
from vatlog.result import VatResult
from test_modal_vat import FakeCall


def test_modal_suite_registers_static_vat_endpoints_without_requiring_kit_sources():
    from runner import modal_tool_suite as suite

    endpoints = {"proteinmpnn-soluble": suite.proteinmpnn_soluble, "dssp": suite.dssp,
                 "ipsae": suite.ipsae, "dockq": suite.dockq, "tmalign": suite.tmalign,
                 "mmseqs2": suite.mmseqs2, "foldseek": suite.foldseek, "esm3": suite.esm3}
    assert set(endpoints) == IMPLEMENTED_SUITE_TOOLS - KIT_TOOL_IDS
    for tool, endpoint in endpoints.items():
        assert isinstance(endpoint, VatFunction)
        assert endpoint.identity.app_name == suite.APP_NAME
        assert endpoint.identity.function_name == tool


def test_anthropic_kit_deployment_registers_only_with_a_complete_source_checkout(tmp_path, monkeypatch):
    from runner.design_store import json_bytes
    from runner.kit_source import build_source_manifest
    from runner.tool_catalog import tool_spec

    (tmp_path / "common").mkdir()
    (tmp_path / "common/core.py").write_text("# shared\n")
    for kit in {tool_spec(tool).kit for tool in KIT_TOOL_IDS}:
        directory = tmp_path / kit
        (directory / "environment").mkdir(parents=True)
        (directory / "stock").mkdir()
        (directory / "environment/Dockerfile").write_text("FROM python:3.12-slim\n")
        (directory / "environment/requirements.lock").write_text("\n")
        (directory / "stock/PINS.json").write_text("{}\n")
    kits = sorted({tool_spec(tool).kit for tool in KIT_TOOL_IDS})
    manifest = build_source_manifest(tmp_path, kits)
    (tmp_path.parent / "uplifting-biomolecular-modeling.json").write_bytes(json_bytes(manifest))
    monkeypatch.setenv("VAT_ANTHROPIC_KIT_ROOT", str(tmp_path))
    sys.modules.pop("runner.modal_anthropic_kits", None)
    suite = importlib.import_module("runner.modal_anthropic_kits")
    endpoints = {tool: getattr(suite, tool.replace("-", "_")) for tool in KIT_TOOL_IDS}
    assert set(endpoints) == KIT_TOOL_IDS
    for tool, endpoint in endpoints.items():
        assert isinstance(endpoint, VatFunction)
        assert endpoint.identity.app_name == suite.APP_NAME
        assert endpoint.identity.function_name == tool


def test_suite_executor_persists_start_before_compute_and_recovers_exact_artifacts(tmp_path, monkeypatch):
    from runner import suite_workers

    id = str(uuid4())
    commits = []
    class Volume:
        def commit(self):
            commits.append((tmp_path / id / "start.json").exists())
    monkeypatch.setattr(modal, "current_function_call_id", lambda: "fc-suite")
    monkeypatch.setattr(suite_workers, "run_utility", lambda tool, payload, work, **kwargs: {
        "metrics": {"count": 1},
        "artifacts": [{"name": "assignments.dssp", "kind": "metric", "raw": b"raw-dssp"}]})
    request = tool_request("dssp", {"structures": ["data_test\n#\n"]})
    result = execute_suite("dssp", id, request, jobs=tmp_path, assets=tmp_path, volume=Volume())
    assert commits == [True, True]
    assert result["execution"]["call_id"] == "fc-suite"
    assert result["execution"]["request_sha256"] == hashlib.sha256(json_bytes(request)).hexdigest()
    recovered = recover_suite(id, jobs=tmp_path)
    assert recovered["start"]["tool"] == "dssp"
    assert recovered["result"]["artifacts"][0]["raw"] == b"raw-dssp"
    (tmp_path / id / "artifact-0").write_bytes(b"tampered")
    try:
        recover_suite(id, jobs=tmp_path)
    except ValueError as exc:
        assert "integrity" in str(exc)
    else:
        raise AssertionError("tampered suite artifact was accepted")


def test_suite_executor_returns_asset_failure_as_a_valid_domain_outcome(tmp_path, monkeypatch):
    from runner import suite_workers
    from runner.design_inputs import DesignError, checked_result

    def missing(*args, **kwargs):
        raise DesignError("ASSET_MISSING", "database absent")

    monkeypatch.setattr(modal, "current_function_call_id", lambda: "fc-suite")
    monkeypatch.setattr(suite_workers, "run_utility", missing)
    request = tool_request("dssp", {"structures": ["data_test\n#\n"]})

    result = execute_suite("dssp", str(uuid4()), request, jobs=tmp_path, volume=None)

    assert checked_result(result)["error"] == "DEPENDENCY_MISMATCH"


def test_new_suite_endpoint_returns_vatresult_through_common_client(tmp_path, monkeypatch):
    from runner import modal_tool_suite as suite

    calls = {}
    def execute(tool, id, request, **kwargs):
        value = {"metrics": {"assignments": 1}, "artifacts": [
            {"name": "assignments.dssp", "kind": "metric", "raw": b"dssp"}]}
        value["execution"] = {"id": id, "call_id": "fc-" + id, "tool": tool, "attempt": 1,
                              "request_sha256": hashlib.sha256(json_bytes(request)).hexdigest()}
        return value
    monkeypatch.setattr(suite, "execute_suite", execute)
    monkeypatch.setenv("VAT_MODAL_ENABLED", "1")

    def lookup(app, tool):
        assert (app, tool) == ("biodesign-vat-tool-suite", "dssp")
        endpoint = suite.dssp
        async def spawn(id, request):
            result = endpoint.modal_function.local(id, copy.deepcopy(request))
            call = FakeCall("fc-" + id, result)
            calls[call.object_id] = call
            return call
        return SimpleNamespace(spawn=SimpleNamespace(aio=spawn))
    monkeypatch.setattr(modal.Function, "from_name", lookup)

    store, transport = DesignStore(tmp_path), ModalTools()
    transport.bind_store(store)
    try:
        result = asyncio.run(transport.run.aio("dssp", tool_request("dssp", {
            "structures": ["data_test\n#\n"]})))
        assert isinstance(result, VatResult)
        assert result.value["metrics"] == {"assignments": 1}
        assert next(a for a in result.artifacts if a.name == "assignments.dssp").read_bytes() == b"dssp"
    finally:
        store.close()


def test_new_suite_recovery_uses_catalog_app(tmp_path, monkeypatch):
    id = str(uuid4())
    store, transport = DesignStore(tmp_path), ModalTools()
    transport.bind_store(store)
    transport.prepare("dssp", id, tool_request("dssp", {"structures": ["data_test\n#\n"]}))
    seen = []

    async def remote(execution_id):
        assert execution_id == id
        return {}

    def lookup(app, function):
        seen.append((app, function))
        return SimpleNamespace(remote=SimpleNamespace(aio=remote))

    monkeypatch.setattr(modal.Function, "from_name", lookup)
    try:
        assert asyncio.run(transport.recover(id)) == {}
        assert seen == [("biodesign-vat-tool-suite", "recover")]
    finally:
        store.close()
