#!/usr/bin/env python3
"""Run the frozen BenchBB panel through recorded Modal tools and seal its evidence."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from demo.benchbb_panel import BenchBBPanel
from demo.benchbb_workflow import (
    CostLedger,
    build_benchbb_report,
    derive_esmc_outcome,
    derive_fold_outcome,
    import_benchbb_panel,
    prepare_esmc_request,
    prepare_fold_request,
)
from demo.disclosure import withhold_records
from runner.design_store import DesignStore
from runner.graph import falsify
from runner.modal_client import ModalTools
from runner.vat_adapter import scientific_result_adapter
from vatlog.execution import VatObservationPending, VatRecorder, VatStateError
from vatlog.modal import VatFunction
from vatlog.node import FunctionIdentity, NodeStatus
from vatlog.provenance import bundle_provenance
from vatlog.result import VatResult
from vatlog.session_export import publish_recorded
from vatlog.verify import verify_bundle


PANEL_SHA256 = "880ebce29826a9b0b52740bd86b91d69383cc5af878ee016272ed0de68c829fd"
FOLD_APP = "biodesign-vat-fold-canary"
ESMC_APP = "biodesign-vat-benchbb-esmc"


class DesignRun(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    full_name: str
    fold_request_id: str
    fold_execution_id: str
    fold_outcome_id: str
    esmc_request_id: str
    esmc_execution_id: str
    esmc_outcome_id: str


class OperatorState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["vat-benchbb-operator/1"] = Field(
        default="vat-benchbb-operator/1", alias="schema"
    )
    panel_sha256: Literal[PANEL_SHA256] = PANEL_SHA256
    session_id: str
    import_id: str
    report_id: str
    runs: list[DesignRun]
    cost: CostLedger = Field(default_factory=CostLedger)
    finalized: bool = False


def new_state(panel: BenchBBPanel, *, compute_budget_usd: float | None = None) -> OperatorState:
    return OperatorState(
        session_id=str(uuid4()),
        import_id=str(uuid4()),
        report_id=str(uuid4()),
        cost=CostLedger(budget_usd=compute_budget_usd),
        runs=[DesignRun(
            full_name=design.full_name,
            fold_request_id=str(uuid4()),
            fold_execution_id=str(uuid4()),
            fold_outcome_id=str(uuid4()),
            esmc_request_id=str(uuid4()),
            esmc_execution_id=str(uuid4()),
            esmc_outcome_id=str(uuid4()),
        ) for design in panel.designs],
    )


def atomic_write(path: Path, raw: bytes) -> None:
    """Replace one operator-owned file without following a destination symlink."""
    if path.is_symlink():
        raise ValueError(f"refusing symlink output: {path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def save_state(path: Path, state: OperatorState) -> None:
    atomic_write(path, (state.model_dump_json(indent=2, by_alias=True) + "\n").encode())


def load_or_create_state(
    path: Path, panel: BenchBBPanel, *, compute_budget_usd: float | None = None
) -> OperatorState:
    if path.exists():
        state = OperatorState.model_validate_json(path.read_bytes())
        if [run.full_name for run in state.runs] != [design.full_name for design in panel.designs]:
            raise ValueError("operator state belongs to another panel ordering")
        if compute_budget_usd is not None and state.cost.budget_usd != compute_budget_usd:
            raise ValueError("operator state has another run-specific compute budget")
        return state
    state = new_state(panel, compute_budget_usd=compute_budget_usd)
    save_state(path, state)
    return state


def run_local(function, recorder: VatRecorder, execution_id: str, *args) -> VatResult:
    return function.with_recorder(recorder).prepare_call(
        args, {}, execution_id=execution_id
    ).run()


async def run_local_aio(function, recorder: VatRecorder, execution_id: str, *args) -> VatResult:
    """Use the same prepared local call from an active orchestration event loop."""
    return await function.with_recorder(recorder).prepare_call(
        args, {}, execution_id=execution_id
    ).run.aio()


def remote_function(app: str, tool: str, recorder: VatRecorder):
    identity = FunctionIdentity(app_name=app, function_name=tool)
    return VatFunction.from_name(
        app, tool, descriptor=identity, result_adapter=scientific_result_adapter
    ).with_recorder(recorder)


async def collect_remote(
    app: str,
    tool: str,
    recorder: VatRecorder,
    execution_id: str,
    request_reference,
) -> tuple[VatResult, bool]:
    """Collect or submit once; unresolved submissions are recovered, never repeated."""
    bound = remote_function(app, tool, recorder)
    prepared = bound.prepare_call(
        (execution_id, request_reference), {}, execution_id=execution_id
    )
    _, state, _, _ = recorder.request(execution_id)
    newly_submitted = state == "prepared"
    if state == "completed":
        node = recorder.completed(execution_id)
        return VatResult(node, recorder.trust, recorder.limits), False
    if state == "prepared":
        call = await prepared.submit.aio()
    elif state in {"running", "observation_pending"}:
        call = bound.resume(execution_id)
    elif state in {"submitting", "submission_uncertain"}:
        recovery = ModalTools(app)
        recovery.recorder = recorder
        await recovery.recover(execution_id)
        if recorder.completed(execution_id):
            return recovery.result(execution_id), False
        _, recovered_state, call_id, _ = recorder.request(execution_id)
        if recovered_state != "running" or not call_id:
            raise VatObservationPending(
                "SUBMISSION_UNCERTAIN",
                "No accepted call identity was recovered; the execution was not resubmitted.",
                execution_id,
            )
        call = bound.resume(execution_id)
    else:
        raise VatStateError("STATE_CONFLICT", f"Unsupported remote state: {state}")
    return await call.get.aio(), newly_submitted


def elapsed_seconds(result: VatResult, fallback: float) -> float:
    """Prefer worker timestamps; retain a conservative local wait if unavailable."""
    if result.status is not NodeStatus.SUCCEEDED:
        return min(1800.0, max(0.0, fallback))
    execution = result.value.get("execution", {})
    try:
        started = datetime.fromisoformat(execution["started"])
        stopped = datetime.fromisoformat(execution["stopped"])
        elapsed = (stopped - started).total_seconds()
    except (KeyError, TypeError, ValueError):
        return min(1800.0, max(0.0, fallback))
    return min(1800.0, max(0.0, elapsed))


def add_cost_once(state: OperatorState, result: VatResult, tool: Literal["esmfold2", "esmc"], fallback: float) -> None:
    if any(entry.execution_id == result.execution_id for entry in state.cost.entries):
        return
    state.cost.add(result.execution_id, tool, elapsed_seconds(result, fallback))


def completed_result(recorder: VatRecorder, execution_id: str) -> VatResult | None:
    try:
        node = recorder.completed(execution_id)
    except VatStateError as exc:
        if exc.code == "EXECUTION_NOT_FOUND":
            return None
        raise
    return VatResult(node, recorder.trust, recorder.limits) if node else None


def needs_dispatch(recorder: VatRecorder, execution_id: str) -> bool:
    """Distinguish a new/prepared call from a previously submitted call to reconcile."""
    try:
        return recorder.request(execution_id)[1] == "prepared"
    except VatStateError as exc:
        if exc.code == "EXECUTION_NOT_FOUND":
            return True
        raise


def execution_order(panel: BenchBBPanel) -> list[int]:
    """Run the predeclared first non-binder canary, then stable manifest order."""
    canary = next(index for index, row in enumerate(panel.designs) if not row.binder_final)
    return [canary, *[index for index in range(len(panel.designs)) if index != canary]]


async def run_paid(
    panel_path: Path,
    output: Path,
    tools: set[str],
    max_new_calls: int,
    fold_app: str,
    esmc_app: str,
    compute_budget_usd: float | None,
) -> tuple[OperatorState, str]:
    raw = panel_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != PANEL_SHA256:
        raise ValueError("panel bytes differ from the reviewed frozen manifest")
    panel = BenchBBPanel.model_validate_json(raw)
    if output.is_symlink():
        raise ValueError("output directory cannot be a symlink")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_path = output / "operator-state.json"
    state = load_or_create_state(
        state_path, panel, compute_budget_usd=compute_budget_usd
    )
    if state.finalized:
        return state, "session is already finalized"

    store = DesignStore(output / "private")
    keys = store.keys()
    recorder = VatRecorder(output / "private" / "executions", keys.recorder)
    started_calls = 0
    reason = "requested work completed"
    try:
        with recorder.scope(state.session_id):
            imported = await run_local_aio(import_benchbb_panel, recorder, state.import_id, raw)
            for index in execution_order(panel):
                design = panel.designs[index]
                run = state.runs[index]
                design_ref = imported.ref(f"/designs/{index}")
                if "esmfold2" in tools:
                    request = await run_local_aio(
                        prepare_fold_request,
                        recorder,
                        run.fold_request_id,
                        design_ref,
                        imported.ref("/target_sequence"),
                    )
                    existing = completed_result(recorder, run.fold_execution_id)
                    if existing is None and needs_dispatch(recorder, run.fold_execution_id):
                        if started_calls >= max_new_calls:
                            reason = "new-call limit reached"
                            break
                        if not state.cost.can_start("esmfold2"):
                            reason = "compute budget guard stopped before the next fold"
                            break
                    before = time.monotonic()
                    fold, submitted = await collect_remote(
                        fold_app, "esmfold2", recorder, run.fold_execution_id, request.ref()
                    )
                    started_calls += int(submitted)
                    add_cost_once(state, fold, "esmfold2", time.monotonic() - before)
                    save_state(state_path, state)
                    if fold.status is not NodeStatus.SUCCEEDED:
                        reason = f"fold execution ended as {fold.status.value}; no dependent result was invented"
                        break
                    fold_outcome = await run_local_aio(
                        derive_fold_outcome,
                        recorder,
                        run.fold_outcome_id,
                        design_ref,
                        fold.ref(),
                        imported.ref("/target_sequence"),
                    )
                    if fold_outcome.value["status"] == "failed":
                        reason = "fold returned a recorded scientific failure; later calls were not started"
                        break

                if "esmc" in tools:
                    request = await run_local_aio(
                        prepare_esmc_request, recorder, run.esmc_request_id, design_ref
                    )
                    existing = completed_result(recorder, run.esmc_execution_id)
                    if existing is None and needs_dispatch(recorder, run.esmc_execution_id):
                        if started_calls >= max_new_calls:
                            reason = "new-call limit reached"
                            break
                        if not state.cost.can_start("esmc"):
                            reason = "compute budget guard stopped before the next ESMC score"
                            break
                    before = time.monotonic()
                    esmc, submitted = await collect_remote(
                        esmc_app, "esmc", recorder, run.esmc_execution_id, request.ref()
                    )
                    started_calls += int(submitted)
                    add_cost_once(state, esmc, "esmc", time.monotonic() - before)
                    save_state(state_path, state)
                    if esmc.status is not NodeStatus.SUCCEEDED:
                        reason = f"ESMC execution ended as {esmc.status.value}; no dependent result was invented"
                        break
                    esmc_outcome = await run_local_aio(
                        derive_esmc_outcome,
                        recorder,
                        run.esmc_outcome_id,
                        design_ref,
                        esmc.ref(),
                    )
                    if esmc_outcome.value["status"] == "failed":
                        reason = "ESMC returned a recorded scientific failure; later calls were not started"
                        break
            save_state(state_path, state)
    finally:
        await recorder.drain()
        recorder.close()
        store.close()
    return state, reason


def finalize(panel_path: Path, output: Path) -> dict:
    raw = panel_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != PANEL_SHA256:
        raise ValueError("panel bytes differ from the reviewed frozen manifest")
    panel = BenchBBPanel.model_validate_json(raw)
    state_path = output / "operator-state.json"
    state = load_or_create_state(state_path, panel)
    if state.finalized:
        raise ValueError("session is already finalized")
    evidence_root = output / "evidence"
    if (evidence_root / state.session_id).exists():
        raise ValueError("sealed session output already exists; inspect it before recovery")

    store = DesignStore(output / "private")
    keys = store.keys()
    recorder = VatRecorder(output / "private" / "executions", keys.recorder)
    try:
        with recorder.scope(state.session_id):
            imported = run_local(import_benchbb_panel, recorder, state.import_id, raw)
            fold_outcomes, esmc_outcomes = [], []
            completed_fold = None
            for run in state.runs:
                fold = completed_result(recorder, run.fold_outcome_id)
                esmc = completed_result(recorder, run.esmc_outcome_id)
                if fold:
                    fold_outcomes.append(fold.ref())
                    completed_fold = completed_fold or fold
                if esmc:
                    esmc_outcomes.append(esmc.ref())
            if completed_fold is None:
                raise ValueError(
                    "finalization requires at least one completed fold outcome for disclosure examples"
                )
            report = run_local(
                build_benchbb_report,
                recorder,
                state.report_id,
                imported.ref(),
                fold_outcomes,
                esmc_outcomes,
            )
            bundle = publish_recorded(
                recorder,
                evidence_root,
                keys,
                state.session_id,
                prompt="Retrospective BBF-14 score comparison on a frozen label-enriched panel",
                scope_id=state.session_id,
            )

        trust = keys.trust_bundle()
        bundle_report = verify_bundle(bundle, trust)
        provenance_report = bundle_provenance(bundle, trust)[0]
        if bundle_report.exit_code != 0 or provenance_report.outcome != "VALID":
            raise ValueError("complete evidence failed verification")
        bundle.save(output / "bundle.zip")
        trust.save(output / "trust.json")
        atomic_write(output / "report.json", (json.dumps(report.value, indent=2, allow_nan=False) + "\n").encode())
        atomic_write(
            output / "expected-head.json",
            (bundle.signed_tree_head.model_dump_json(indent=2) + "\n").encode(),
        )

        leaf = next(record.link.leaf_index for record in bundle.records
                    if record.params.hyperparams.get("vat_node_id") == completed_fold.node_id)
        partial = withhold_records(bundle, {leaf})
        tampered, description = falsify(bundle, {"kind": "result", "leaf": leaf})
        partial_report = verify_bundle(partial, trust)
        tampered_report = verify_bundle(tampered, trust)
        if partial_report.exit_code != 6 or tampered_report.exit_code != 2:
            raise ValueError("disclosure examples did not produce the required verifier outcomes")
        partial.save(output / "partial-bundle.zip")
        tampered.save(output / "tampered-bundle.zip")
        atomic_write(output / "disclosure.json", (json.dumps({
            "withheld_leaf": leaf,
            "partial": asdict(partial_report),
            "tampered": asdict(tampered_report),
            "tampering_action": description,
        }, indent=2, allow_nan=False) + "\n").encode())
        state.finalized = True
        save_state(state_path, state)
        return {
            "bundle_outcome": bundle_report.outcome,
            "provenance_outcome": provenance_report.outcome,
            "provenance_checks": len(provenance_report.checks),
            "partial_exit_code": partial_report.exit_code,
            "tampered_exit_code": tampered_report.exit_code,
            "tree_size": bundle.signed_tree_head.tree_size,
            "estimated_compute_usd": state.cost.estimated_spend,
        }
    finally:
        recorder.close()
        store.close()


def dry_run(panel_path: Path) -> dict:
    raw = panel_path.read_bytes()
    panel = BenchBBPanel.model_validate_json(raw)
    return {
        "panel_sha256": hashlib.sha256(raw).hexdigest(),
        "reviewed_panel": hashlib.sha256(raw).hexdigest() == PANEL_SHA256,
        "designs": len(panel.designs),
        "binders": sum(row.binder_final for row in panel.designs),
        "fold_app": FOLD_APP,
        "esmc_app": ESMC_APP,
        "first_call": panel.designs[execution_order(panel)[0]].full_name + ":esmfold2",
        "initial_accounting_reserve_usd": CostLedger().next_seconds("esmfold2") * CostLedger().rates["esmfold2"],
        "default_compute_budget_usd": None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--panel", type=Path, default=Path("fixtures/benchbb_bbf14_panel.json"))
    parser.add_argument("--output", type=Path, default=Path(".local/benchbb/run"))
    parser.add_argument("--tool", action="append", choices=["esmfold2", "esmc"])
    parser.add_argument("--max-new-calls", type=int, default=1)
    parser.add_argument("--fold-app", default=FOLD_APP)
    parser.add_argument("--esmc-app", default=ESMC_APP)
    parser.add_argument("--compute-budget-usd", type=float,
                        help="optional cap for this run; omit for no operator-side budget limit")
    parser.add_argument("--execute-paid", action="store_true")
    parser.add_argument("--finalize", action="store_true")
    args = parser.parse_args()
    if args.max_new_calls < 0:
        parser.error("--max-new-calls cannot be negative")
    if args.compute_budget_usd is not None and args.compute_budget_usd <= 0:
        parser.error("--compute-budget-usd must be positive")
    if args.finalize:
        result = finalize(args.panel, args.output)
    elif args.execute_paid:
        state, reason = asyncio.run(run_paid(
            args.panel,
            args.output,
            set(args.tool or ["esmfold2", "esmc"]),
            args.max_new_calls,
            args.fold_app,
            args.esmc_app,
            args.compute_budget_usd,
        ))
        result = {
            "status": reason,
            "estimated_compute_usd": state.cost.estimated_spend,
            "completed_calls": len(state.cost.entries),
            "finalized": state.finalized,
        }
    else:
        result = dry_run(args.panel)
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
