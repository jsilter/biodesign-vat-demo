import asyncio
import hashlib
import runpy
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import modal
import pytest

from demo.benchbb_panel import BenchBBPanel
from demo.benchbb_workflow import (
    derive_fold_outcome,
    import_benchbb_panel,
    prepare_fold_request,
)
from runner.design_store import DesignStore
from runner.design_inputs import masked_metrics
from runner.design_store import json_bytes
from vatlog import VatRecorder
from vatlog.bundle import Bundle
from vatlog.execution import VatObservationPending
from vatlog.verify import verify_bundle
from test_benchbb_workflow import substituted_fold
from test_modal_vat import FakeCall


ROOT = Path(__file__).parents[1]
PANEL_PATH = ROOT / "fixtures/benchbb_bbf14_panel.json"
FUNCTIONS = runpy.run_path(str(ROOT / "scripts/run_benchbb_reference.py"))


def test_dry_run_declares_exact_canary_and_reviewed_manifest():
    result = FUNCTIONS["dry_run"](PANEL_PATH)
    assert result == {
        "panel_sha256": "880ebce29826a9b0b52740bd86b91d69383cc5af878ee016272ed0de68c829fd",
        "reviewed_panel": True,
        "designs": 12,
        "binders": 3,
        "fold_app": "biodesign-vat-fold-canary",
        "esmc_app": "biodesign-vat-benchbb-esmc",
        "first_call": "mythos_preview_multi_target_bbf14_rank24:esmfold2",
        "initial_accounting_reserve_usd": 1.8,
        "default_compute_budget_usd": None,
    }


def test_operator_state_round_trip_rejects_another_panel_order(tmp_path):
    panel = BenchBBPanel.model_validate_json(PANEL_PATH.read_bytes())
    path = tmp_path / "state.json"
    state = FUNCTIONS["load_or_create_state"](path, panel)
    assert FUNCTIONS["load_or_create_state"](path, panel) == state
    changed = panel.model_copy(update={"designs": list(reversed(panel.designs))})
    with pytest.raises(ValueError, match="another panel ordering"):
        FUNCTIONS["load_or_create_state"](path, changed)


def test_atomic_write_refuses_destination_symlink(tmp_path):
    destination = tmp_path / "state.json"
    destination.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ValueError, match="symlink"):
        FUNCTIONS["atomic_write"](destination, b"data")


def test_async_orchestrator_uses_async_local_vat_surface(tmp_path):
    panel = BenchBBPanel.model_validate_json(PANEL_PATH.read_bytes())
    store = DesignStore(tmp_path / "store")
    recorder = VatRecorder(tmp_path / "recorder", store.keys().recorder)
    try:
        result = asyncio.run(FUNCTIONS["run_local_aio"](
            import_benchbb_panel, recorder, str(uuid4()), PANEL_PATH.read_bytes()
        ))
        assert result.value["designs"][0]["full_name"] == panel.designs[0].full_name
    finally:
        recorder.close()
        store.close()


def test_remote_collection_is_idempotent_after_one_accepted_call(tmp_path, monkeypatch):
    submitted = []

    def lookup(app, tool):
        assert (app, tool) == ("test-app", "esmc")

        async def spawn(identifier, request):
            submitted.append(identifier)
            value = {
                "metrics": masked_metrics([-1.0] * len(request["sequence"])),
                "artifacts": [],
                "execution": {
                    "id": identifier,
                    "call_id": "fc-" + identifier,
                    "tool": tool,
                    "attempt": 1,
                    "request_sha256": hashlib.sha256(json_bytes(request)).hexdigest(),
                },
            }
            return FakeCall("fc-" + identifier, value)

        return SimpleNamespace(spawn=SimpleNamespace(aio=spawn))

    monkeypatch.setattr(modal.Function, "from_name", lookup)
    identifier = str(uuid4())
    store = DesignStore(tmp_path / "store")
    try:
        with VatRecorder(tmp_path / "recorder", store.keys().recorder) as recorder:
            first, first_submitted = asyncio.run(FUNCTIONS["collect_remote"](
                "test-app", "esmc", recorder, identifier, {"sequence": "AC"}
            ))
            second, second_submitted = asyncio.run(FUNCTIONS["collect_remote"](
                "test-app", "esmc", recorder, identifier, {"sequence": "AC"}
            ))
            assert first.node_id == second.node_id
            assert first_submitted and not second_submitted
    finally:
        store.close()
    assert submitted == [identifier]


def test_uncertain_submission_recovers_without_calling_spawn_again(tmp_path, monkeypatch):
    spawned = []

    def lookup(_app, tool):
        async def spawn(*_args):
            spawned.append(True)

        async def recover(_identifier):
            return {}

        if tool == "recover":
            return SimpleNamespace(remote=SimpleNamespace(aio=recover))
        return SimpleNamespace(spawn=SimpleNamespace(aio=spawn))

    monkeypatch.setattr(modal.Function, "from_name", lookup)
    identifier = str(uuid4())
    store = DesignStore(tmp_path / "store")
    recorder = VatRecorder(tmp_path / "recorder", store.keys().recorder)
    try:
        bound = FUNCTIONS["remote_function"]("test-app", "esmc", recorder)
        bound.prepare_call((identifier, {"sequence": "AC"}), {}, execution_id=identifier)
        recorder.set_state(identifier, "submitting")
        with pytest.raises(VatObservationPending, match="not resubmitted"):
            asyncio.run(FUNCTIONS["collect_remote"](
                "test-app", "esmc", recorder, identifier, {"sequence": "AC"}
            ))
        assert recorder.request(identifier)[1] == "submitting"
        assert spawned == []
    finally:
        recorder.close()
        store.close()


def test_finalize_offline_recording_creates_verified_original_partial_and_tampered_bundles(tmp_path):
    panel = BenchBBPanel.model_validate_json(PANEL_PATH.read_bytes())
    output = tmp_path / "run"
    output.mkdir()
    state = FUNCTIONS["new_state"](panel)
    FUNCTIONS["save_state"](output / "operator-state.json", state)
    store = DesignStore(output / "private")
    keys = store.keys()
    recorder = VatRecorder(output / "private" / "executions", keys.recorder)
    try:
        with recorder.scope(state.session_id):
            imported = FUNCTIONS["run_local"](
                import_benchbb_panel, recorder, state.import_id, PANEL_PATH.read_bytes()
            )
            run = state.runs[0]
            design = imported.ref("/designs/0")
            request = FUNCTIONS["run_local"](
                prepare_fold_request,
                recorder,
                run.fold_request_id,
                design,
                imported.ref("/target_sequence"),
            )
            remote = substituted_fold.with_recorder(recorder).prepare_call(
                (run.fold_execution_id, request.ref()), {}, execution_id=run.fold_execution_id
            ).run()
            FUNCTIONS["run_local"](
                derive_fold_outcome,
                recorder,
                run.fold_outcome_id,
                design,
                remote.ref(),
                imported.ref("/target_sequence"),
            )
    finally:
        recorder.close()
        store.close()

    summary = FUNCTIONS["finalize"](PANEL_PATH, output)
    assert summary["bundle_outcome"] == "OK"
    assert summary["provenance_outcome"] == "VALID"
    assert summary["provenance_checks"] > 0
    assert summary["partial_exit_code"] == 6
    assert summary["tampered_exit_code"] == 2
    trust = keys.trust_bundle()
    assert verify_bundle(Bundle.load(output / "bundle.zip"), trust).exit_code == 0
    assert verify_bundle(Bundle.load(output / "partial-bundle.zip"), trust).exit_code == 6
    assert verify_bundle(Bundle.load(output / "tampered-bundle.zip"), trust).exit_code == 2
    assert FUNCTIONS["OperatorState"].model_validate_json(
        (output / "operator-state.json").read_bytes()
    ).finalized
    with pytest.raises(ValueError, match="already finalized"):
        FUNCTIONS["finalize"](PANEL_PATH, output)
