import hashlib
import json
import math
from pathlib import Path
from uuid import uuid4

import gemmi
import pytest

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
from runner.design_inputs import masked_metrics
from runner.design_store import json_bytes
from runner.vat_adapter import scientific_result_adapter
from vatlog import VatRecorder, vat
from vatlog.node import FunctionIdentity
from vatlog.provenance import verify_provenance
from vatlog.roles import Keyring


PANEL_PATH = Path(__file__).parents[1] / "fixtures/benchbb_bbf14_panel.json"
AA3 = dict(zip(
    "ACDEFGHIKLMNPQRSTVWY",
    "ALA CYS ASP GLU PHE GLY HIS ILE LYS LEU MET ASN PRO GLN ARG SER THR VAL TRP TYR".split(),
    strict=True,
))


def coordinates(sequences: dict[str, str]) -> bytes:
    structure = gemmi.Structure()
    model = gemmi.Model("1")
    for chain_index, (name, sequence) in enumerate(sequences.items()):
        chain = gemmi.Chain(name)
        for index, amino_acid in enumerate(sequence, 1):
            residue = gemmi.Residue()
            residue.name = AA3[amino_acid]
            residue.seqid = gemmi.SeqId(index, " ")
            residue.het_flag = "A"
            atom = gemmi.Atom()
            atom.name, atom.element = "CA", gemmi.Element("C")
            atom.pos = gemmi.Position(index * 3.8, chain_index * 4, 0)
            residue.add_atom(atom)
            chain.add_residue(residue)
        model.add_chain(chain)
    structure.add_model(model)
    structure.setup_entities()
    return structure.make_mmcif_document().as_string().encode()


def execution(tool: str, identifier: str, request: dict) -> dict:
    return {
        "id": identifier,
        "call_id": "local:" + identifier,
        "tool": tool,
        "attempt": 1,
        "request_sha256": hashlib.sha256(json_bytes(request)).hexdigest(),
        "started": "2026-09-18T12:00:00+00:00",
        "stopped": "2026-09-18T12:01:00+00:00",
        "hardware": "substituted",
    }


@vat(descriptor=FunctionIdentity(app_name="substituted", function_name="esmfold2"),
     result_adapter=scientific_result_adapter)
def substituted_fold(identifier: str, request: dict) -> dict:
    sequences = {"A": request["target_sequence"], "B": request["sequence"]}
    length = sum(map(len, sequences.values()))
    metrics = {
        "plddt_raw_scale": "0–1",
        "plddt": [0.75] * length,
        "mean_plddt": 0.75,
        "ptm": 0.6,
        "iptm": 0.55,
        "pae": [[0.0 if row == column else 5.0 for column in range(length)] for row in range(length)],
    }
    return {
        "metrics": metrics,
        "artifacts": [{"name": "prediction.cif", "kind": "coordinates", "raw": coordinates(sequences)}],
        "execution": execution("esmfold2", identifier, request),
    }


@vat(descriptor=FunctionIdentity(app_name="substituted", function_name="esmc"),
     result_adapter=scientific_result_adapter)
def substituted_esmc(identifier: str, request: dict) -> dict:
    metrics = {**masked_metrics([-1.0] * len(request["sequence"])), "model": "substituted"}
    return {"metrics": metrics, "artifacts": [], "execution": execution("esmc", identifier, request)}


def prepared_call(function, recorder, request):
    identifier = str(uuid4())
    return function.with_recorder(recorder).prepare_call(
        (identifier, request), {}, execution_id=identifier
    ).run()


def test_recorded_panel_to_report_path_has_real_reference_edges(tmp_path):
    keys = Keyring.generate()
    panel = BenchBBPanel.model_validate_json(PANEL_PATH.read_bytes())
    with VatRecorder(tmp_path, keys.recorder) as recorder:
        imported = import_benchbb_panel.with_recorder(recorder).call(PANEL_PATH.read_bytes())
        design_ref = imported.ref("/designs/0")
        target_ref = imported.ref("/target_sequence")

        fold_request = prepare_fold_request.with_recorder(recorder).call(design_ref, target_ref)
        fold = prepared_call(substituted_fold, recorder, fold_request.ref())
        fold_outcome = derive_fold_outcome.with_recorder(recorder).call(
            design_ref, fold.ref(), target_ref
        )

        esmc_request = prepare_esmc_request.with_recorder(recorder).call(design_ref)
        esmc = prepared_call(substituted_esmc, recorder, esmc_request.ref())
        esmc_outcome = derive_esmc_outcome.with_recorder(recorder).call(design_ref, esmc.ref())

        report = build_benchbb_report.with_recorder(recorder).call(
            imported.ref(), [fold_outcome.ref()], [esmc_outcome.ref()]
        )
        assert report.value["rows"][0]["fold"]["metrics"]["iptm"] == 0.55
        assert report.value["rows"][0]["esmc"]["metrics"]["pseudo_perplexity"] == pytest.approx(math.e)
        assert report.value["rows"][1]["fold"] == {"status": "unexecuted"}
        assert set(report.value["analyses"]) == {"published_ipsae_min_ef2fast"}
        nodes = recorder.evidence_nodes()
        assert len(nodes) == 8
        assert sum(len(node.statement().input_bindings) for node in nodes) == 13
        assert verify_provenance(nodes, recorder.trust).outcome == "VALID"
        assert len(fold.node.statement().input_bindings) == 1
        assert fold.value["artifacts"][0]["raw"].startswith(b"data_")
        assert panel.designs[0].full_name == fold_outcome.value["full_name"]


def test_failed_scientific_result_remains_an_explicit_outcome(tmp_path):
    panel = BenchBBPanel.model_validate_json(PANEL_PATH.read_bytes())
    design = panel.designs[0].model_dump(mode="json")
    identifier = str(uuid4())
    request = {"sequence": design["sequence"]}
    result = {
        "error": "EXECUTION_FAILED",
        "artifacts": [],
        "execution": execution("esmc", identifier, request),
    }
    with VatRecorder(tmp_path, Keyring.generate().recorder) as recorder:
        outcome = derive_esmc_outcome.with_recorder(recorder).call(design, result)
    assert outcome.value == {
        "full_name": design["full_name"],
        "status": "failed",
        "execution": execution("esmc", identifier, request),
        "error": "EXECUTION_FAILED",
    }


def test_report_rejects_duplicate_unknown_and_malformed_outcomes():
    panel = json.loads(PANEL_PATH.read_text())
    name = panel["designs"][0]["full_name"]
    valid = {"full_name": name, "status": "failed", "error": "EXECUTION_FAILED"}
    with pytest.raises(Exception, match="duplicated"):
        build_benchbb_report.function(panel, [valid, valid], [])
    with pytest.raises(Exception, match="unknown design"):
        build_benchbb_report.function(panel, [{**valid, "full_name": "unknown"}], [])
    with pytest.raises(Exception, match="invalid status"):
        build_benchbb_report.function(panel, [{**valid, "status": "maybe"}], [])


def test_report_runs_exact_220_permutations_only_for_complete_metric():
    panel = json.loads(PANEL_PATH.read_text())
    folds = [
        {"full_name": design["full_name"], "status": "succeeded",
         "metrics": {"iptm": float(index), "target_mean_plddt": 0.5,
                     "binder_mean_plddt": 0.5, "symmetric_interface_mean_pae": 5.0,
                     "ca_contacts_lt_8a": 1}}
        for index, design in enumerate(panel["designs"])
    ]
    report = build_benchbb_report.function(panel, folds, [])
    assert report["analyses"]["fresh_iptm"]["permutations"] == 220
    assert "esmc_pseudo_perplexity" not in report["analyses"]
    assert len(report["rows"]) == 12


def test_cost_ledger_starts_conservative_then_uses_observed_duration_and_never_duplicates():
    ledger = CostLedger(budget_usd=15.0)
    assert ledger.next_seconds("esmfold2") == 1800
    assert ledger.can_start("esmfold2")
    identifier = str(uuid4())
    ledger.add(identifier, "esmfold2", 100)
    assert ledger.next_seconds("esmfold2") == 125
    assert ledger.estimated_spend == pytest.approx(0.1)
    with pytest.raises(ValueError, match="already recorded"):
        ledger.add(identifier, "esmfold2", 100)
    ledger.budget_usd = 0.11
    assert not ledger.can_start("esmfold2")


def test_cost_ledger_rejects_tampered_estimate():
    with pytest.raises(Exception, match="does not match"):
        CostLedger.model_validate({
            "budget_usd": 15.0,
            "rates": {"esmfold2": 0.001, "esmc": 0.0005},
            "entries": [{
                "execution_id": str(uuid4()),
                "tool": "esmfold2",
                "elapsed_seconds": 100.0,
                "accounting_rate_usd_per_second": 0.001,
                "estimated_usd": 0.01,
            }],
        })


def test_cost_guard_is_optional_in_reusable_infrastructure():
    ledger = CostLedger()
    assert ledger.budget_usd is None
    ledger.add(str(uuid4()), "esmfold2", 1800)
    assert ledger.can_start("esmfold2")


@pytest.mark.parametrize("change,message", [
    ({"rates": {"esmfold2": 0.001}}, "exactly"),
    ({"rates": {"esmfold2": -0.001, "esmc": 0.0005}}, "finite and positive"),
    ({"rates": {"esmfold2": math.inf, "esmc": 0.0005}}, "finite number"),
    ({"budget_usd": math.inf}, "finite number"),
])
def test_cost_ledger_rejects_incomplete_or_nonfinite_accounting(change, message):
    with pytest.raises(Exception, match=message):
        CostLedger.model_validate(change)
