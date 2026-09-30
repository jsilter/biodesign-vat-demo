"""Recorded transformations and budget accounting for the BenchBB study."""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from runner.design_inputs import checked_result, masked_metrics
from vatlog import vat

from .benchbb_analysis import describe_groups, exact_auc_permutation, summarize_fold_result
from .benchbb_panel import BenchBBDesign, BenchBBPanel


@vat
def import_benchbb_panel(raw: bytes) -> dict:
    """Record exact supplied manifest bytes and return its validated representation."""
    return BenchBBPanel.model_validate_json(raw).model_dump(mode="json", by_alias=True)


@vat
def prepare_fold_request(design: dict, target_sequence: str) -> dict:
    """Build one fixed single-seed complex request from source-backed values."""
    parsed = BenchBBDesign.model_validate(design)
    if target_sequence != BenchBBPanel.model_fields["target_sequence"].default:
        raise ValueError("target sequence differs from the frozen construct")
    return {
        "sequence": parsed.sequence,
        "target_sequence": target_sequence,
        "mode": "complex",
        "seed": 1,
        "num_loops": 10,
        "num_sampling_steps": 68,
        "num_diffusion_samples": 1,
    }


@vat
def prepare_esmc_request(design: dict) -> dict:
    """Build one ESMC masked-scoring request from a source-backed sequence."""
    return {"sequence": BenchBBDesign.model_validate(design).sequence}


def _execution_summary(result: dict) -> dict:
    execution = result.get("execution", {})
    return {key: execution.get(key) for key in (
        "id", "call_id", "tool", "attempt", "started", "stopped", "hardware", "gpu_profile",
        "source_revision", "deployment", "dependency_lock_sha256", "request_sha256"
    ) if execution.get(key) is not None}


@vat
def derive_fold_outcome(design: dict, result: dict, target_sequence: str) -> dict:
    """Validate one returned fold and attach its summaries to the source design ID."""
    parsed_design = BenchBBDesign.model_validate(design)
    parsed_result = checked_result(result)
    outcome = {
        "full_name": parsed_design.full_name,
        "status": "failed" if parsed_result.get("error") else "succeeded",
        "execution": _execution_summary(parsed_result),
    }
    if parsed_result.get("error"):
        return {**outcome, "error": parsed_result["error"]}
    structures = [item for item in parsed_result["artifacts"]
                  if item["name"] == "prediction.cif" and item["kind"] == "coordinates"]
    if len(structures) != 1:
        raise ValueError("successful fold must contain exactly one prediction.cif artifact")
    return {
        **outcome,
        "metrics": summarize_fold_result(
            parsed_result["metrics"], structures[0]["raw"], target_sequence, parsed_design.sequence
        ),
    }


@vat
def derive_esmc_outcome(design: dict, result: dict) -> dict:
    """Validate masked-position scores and retain compact sequence metrics."""
    parsed_design = BenchBBDesign.model_validate(design)
    parsed_result = checked_result(result)
    outcome = {
        "full_name": parsed_design.full_name,
        "status": "failed" if parsed_result.get("error") else "succeeded",
        "execution": _execution_summary(parsed_result),
    }
    if parsed_result.get("error"):
        return {**outcome, "error": parsed_result["error"]}
    metrics = parsed_result["metrics"]
    expected = masked_metrics(metrics.get("per_residue_log_probability", []))
    if metrics.get("length") != len(parsed_design.sequence) or any(metrics.get(key) != value
                                                                    for key, value in expected.items()):
        raise ValueError("ESMC summary does not match its per-residue scores")
    return {
        **outcome,
        "metrics": {
            "mean_masked_log_probability": expected["mean_masked_log_probability"],
            "pseudo_perplexity": expected["pseudo_perplexity"],
        },
    }


METRICS = {
    "published_ipsae_min_ef2fast": ("published", "published_ipsae_min_ef2fast", True),
    "fresh_iptm": ("fold", "iptm", True),
    "fresh_target_mean_plddt": ("fold", "target_mean_plddt", True),
    "fresh_binder_mean_plddt": ("fold", "binder_mean_plddt", True),
    "fresh_symmetric_interface_mean_pae": ("fold", "symmetric_interface_mean_pae", False),
    "fresh_ca_contacts_lt_8a": ("fold", "ca_contacts_lt_8a", True),
    "esmc_mean_masked_log_probability": ("esmc", "mean_masked_log_probability", True),
    "esmc_pseudo_perplexity": ("esmc", "pseudo_perplexity", False),
}


def _outcome_map(values: list[dict], expected_names: set[str], kind: str) -> dict[str, dict]:
    mapped = {}
    for value in values:
        if not isinstance(value, dict) or value.get("full_name") not in expected_names:
            raise ValueError(f"{kind} outcome has an unknown design")
        if value["full_name"] in mapped:
            raise ValueError(f"{kind} outcome is duplicated")
        if value.get("status") not in {"succeeded", "failed"}:
            raise ValueError(f"{kind} outcome has an invalid status")
        mapped[value["full_name"]] = value
    return mapped


@vat
def build_benchbb_report(panel: dict, fold_outcomes: list[dict], esmc_outcomes: list[dict]) -> dict:
    """Build one honest complete-or-partial table and predeclared statistics."""
    parsed = BenchBBPanel.model_validate(panel)
    names = {design.full_name for design in parsed.designs}
    folds = _outcome_map(fold_outcomes, names, "fold")
    esmcs = _outcome_map(esmc_outcomes, names, "ESMC")
    rows = []
    for design in parsed.designs:
        fold = folds.get(design.full_name, {"status": "unexecuted"})
        esmc = esmcs.get(design.full_name, {"status": "unexecuted"})
        rows.append({
            "full_name": design.full_name,
            "arm": design.arm,
            "binder_final": design.binder_final,
            "adaptyv_binding": design.adaptyv_binding,
            "twist_binding": design.twist_binding,
            "kd_nM_final": design.kd_nM_final,
            "published_ipsae_min_ef2fast": design.ipsae_min_ef2fast,
            "fold": fold,
            "esmc": esmc,
        })

    analyses = {}
    for label, (source, key, higher_is_better) in METRICS.items():
        values = []
        for row in rows:
            if source == "published":
                value = row[key]
            else:
                outcome = row[source]
                value = outcome.get("metrics", {}).get(key) if outcome["status"] == "succeeded" else None
            values.append(value)
        if all(type(value) in {int, float} and math.isfinite(value) for value in values):
            labels = [row["binder_final"] for row in rows]
            analyses[label] = {
                **describe_groups(labels, values),
                **exact_auc_permutation(labels, values, higher_is_better=higher_is_better),
                "higher_is_better": higher_is_better,
            }

    return {
        "schema": "vat-benchbb-report/1",
        "question": "How well do named computational scores distinguish the released BBF-14 binders from selected tested non-binders?",
        "selection": "Label-enriched: all 3 final binders and 9 deterministically selected concordant non-binders.",
        "rows": rows,
        "analyses": analyses,
        "limitations": [
            "This small label-enriched panel does not estimate campaign hit rate or prospective performance.",
            "Two final binders were tested by Twist and not by Adaptyv.",
            "Fresh single-seed metrics are not interchangeable with the released five-seed-maximum ipSAE.",
            "Sequence plausibility is not binding confidence.",
        ],
    }


class CostEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)
    execution_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    tool: Literal["esmfold2", "esmc"]
    elapsed_seconds: float = Field(ge=0)
    accounting_rate_usd_per_second: float = Field(gt=0)
    estimated_usd: float = Field(ge=0)

    @model_validator(mode="after")
    def calculated(self):
        expected = self.elapsed_seconds * self.accounting_rate_usd_per_second
        if not math.isclose(self.estimated_usd, expected, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError("cost estimate does not match elapsed time and rate")
        return self


class CostLedger(BaseModel):
    """Conservative operator accounting; not a provider billing statement."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    budget_usd: float | None = Field(default=None, gt=0)
    rates: dict[Literal["esmfold2", "esmc"], float] = Field(
        default_factory=lambda: {"esmfold2": 0.001, "esmc": 0.0005}
    )
    entries: list[CostEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_rates(self):
        if set(self.rates) != {"esmfold2", "esmc"}:
            raise ValueError("cost rates must cover exactly esmfold2 and esmc")
        if any(not math.isfinite(rate) or rate <= 0 for rate in self.rates.values()):
            raise ValueError("cost rates must be finite and positive")
        return self

    @property
    def estimated_spend(self) -> float:
        return math.fsum(entry.estimated_usd for entry in self.entries)

    def next_seconds(self, tool: Literal["esmfold2", "esmc"]) -> float:
        observed = [entry.elapsed_seconds for entry in self.entries if entry.tool == tool]
        return min(1800.0, max(observed, default=1800.0) * (1.25 if observed else 1.0))

    def can_start(self, tool: Literal["esmfold2", "esmc"]) -> bool:
        return self.budget_usd is None or (
            self.estimated_spend + self.next_seconds(tool) * self.rates[tool] <= self.budget_usd
        )

    def add(self, execution_id: str, tool: Literal["esmfold2", "esmc"], elapsed_seconds: float) -> None:
        if any(entry.execution_id == execution_id for entry in self.entries):
            raise ValueError("execution cost was already recorded")
        rate = self.rates[tool]
        self.entries.append(CostEntry(
            execution_id=execution_id,
            tool=tool,
            elapsed_seconds=elapsed_seconds,
            accounting_rate_usd_per_second=rate,
            estimated_usd=elapsed_seconds * rate,
        ))
