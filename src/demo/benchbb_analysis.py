"""Pure, predeclared analysis for the BBF-14 reference panel."""

from __future__ import annotations

import itertools
import math
from statistics import median
from typing import Iterable

from runner.design_inputs import validate_fold_metrics


def summarize_fold_metrics(
    metrics: dict,
    *,
    target_length: int,
    binder_length: int,
    target_ca: list[tuple[float, float, float]],
    binder_ca: list[tuple[float, float, float]],
    contact_cutoff: float = 8.0,
) -> dict:
    """Validate one complex output and compute only predeclared summaries."""
    if type(target_length) is not int or type(binder_length) is not int:
        raise ValueError("chain lengths must be positive integers")
    if target_length <= 0 or binder_length <= 0:
        raise ValueError("chain lengths must be positive integers")
    total = target_length + binder_length
    validate_fold_metrics(metrics, total, complex=True)
    if metrics.get("iptm") is None:
        raise ValueError("complex result has no ipTM")
    if "pae" not in metrics:
        raise ValueError("complex result has no PAE matrix")
    if len(target_ca) != target_length or len(binder_ca) != binder_length:
        raise ValueError("C-alpha coordinate counts do not match input chains")
    if contact_cutoff <= 0 or not math.isfinite(contact_cutoff):
        raise ValueError("contact cutoff must be finite and positive")
    for point in [*target_ca, *binder_ca]:
        if len(point) != 3 or any(not math.isfinite(value) for value in point):
            raise ValueError("C-alpha coordinates must be finite triples")

    plddt = metrics["plddt"]
    pae = metrics["pae"]
    target_to_binder = [value for row in pae[:target_length] for value in row[target_length:]]
    binder_to_target = [value for row in pae[target_length:] for value in row[:target_length]]
    if not target_to_binder or not binder_to_target:
        raise ValueError("complex must contain two nonempty chains")

    cutoff_squared = contact_cutoff * contact_cutoff
    contacts = sum(
        (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2 < cutoff_squared
        for a in target_ca
        for b in binder_ca
    )
    directed_values = target_to_binder + binder_to_target
    return {
        "target_mean_plddt": math.fsum(plddt[:target_length]) / target_length,
        "binder_mean_plddt": math.fsum(plddt[target_length:]) / binder_length,
        "iptm": metrics["iptm"],
        "target_to_binder_mean_pae": math.fsum(target_to_binder) / len(target_to_binder),
        "target_to_binder_min_pae": min(target_to_binder),
        "binder_to_target_mean_pae": math.fsum(binder_to_target) / len(binder_to_target),
        "binder_to_target_min_pae": min(binder_to_target),
        "symmetric_interface_mean_pae": math.fsum(directed_values) / len(directed_values),
        "ca_contacts_lt_8a": contacts,
    }


def ca_coordinates(cif: bytes, expected_sequences: dict[str, str]) -> dict[str, list[tuple[float, float, float]]]:
    """Extract one C-alpha per residue after exact chain/sequence validation."""
    import gemmi

    from runner.design_workers import validate_cif_sequences

    validate_cif_sequences(cif, expected_sequences)
    structure = gemmi.make_structure_from_block(gemmi.cif.read_string(cif.decode()).sole_block())
    chains = list(structure[0])
    if [chain.name for chain in chains] != list(expected_sequences):
        raise ValueError("coordinate chain order does not match inference order")
    result = {}
    for chain in chains:
        result[chain.name] = [
            (residue["CA"][0].pos.x, residue["CA"][0].pos.y, residue["CA"][0].pos.z)
            for residue in chain
        ]
    return result


def summarize_fold_result(metrics: dict, cif: bytes, target_sequence: str, binder_sequence: str) -> dict:
    """Summarize one actual A/B ESMFold2-Fast result."""
    coordinates = ca_coordinates(cif, {"A": target_sequence, "B": binder_sequence})
    return summarize_fold_metrics(
        metrics,
        target_length=len(target_sequence),
        binder_length=len(binder_sequence),
        target_ca=coordinates["A"],
        binder_ca=coordinates["B"],
    )


def roc_auc(labels: Iterable[bool], scores: Iterable[float], *, higher_is_better: bool) -> float:
    """Compute rank AUC with half credit for ties and an explicit direction."""
    pairs = list(zip(labels, scores, strict=True))
    if any(type(label) is not bool for label, _ in pairs):
        raise ValueError("AUC labels must be booleans")
    positives = [score for label, score in pairs if label]
    negatives = [score for label, score in pairs if not label]
    if not positives or not negatives:
        raise ValueError("AUC requires positive and negative labels")
    if any(type(score) not in {int, float} or not math.isfinite(score) for _, score in pairs):
        raise ValueError("AUC scores must be finite numbers")
    favorable = (lambda a, b: a > b) if higher_is_better else (lambda a, b: a < b)
    wins = math.fsum(1 if favorable(a, b) else 0.5 if a == b else 0 for a in positives for b in negatives)
    return wins / (len(positives) * len(negatives))


def exact_auc_permutation(
    labels: Iterable[bool], scores: Iterable[float], *, higher_is_better: bool
) -> dict:
    """Return AUC and a two-sided exact label-permutation p-value."""
    label_values = list(labels)
    score_values = list(scores)
    if len(label_values) != len(score_values):
        raise ValueError("labels and scores differ in length")
    observed = roc_auc(label_values, score_values, higher_is_better=higher_is_better)
    positive_count = sum(label_values)
    distance = abs(observed - 0.5)
    extreme = 0
    total = 0
    for positive_indices in itertools.combinations(range(len(label_values)), positive_count):
        selected = set(positive_indices)
        auc = roc_auc(
            [index in selected for index in range(len(label_values))],
            score_values,
            higher_is_better=higher_is_better,
        )
        total += 1
        extreme += abs(auc - 0.5) >= distance - 1e-15
    return {"auc": observed, "two_sided_exact_p": extreme / total, "permutations": total}


def describe_groups(labels: Iterable[bool], values: Iterable[float]) -> dict:
    """Return transparent descriptive summaries for the two frozen groups."""
    pairs = list(zip(labels, values, strict=True))
    if any(type(value) not in {int, float} or not math.isfinite(value) for _, value in pairs):
        raise ValueError("summary values must be finite numbers")

    def summary(group: list[float]) -> dict:
        if not group:
            raise ValueError("both groups must be nonempty")
        return {"n": len(group), "median": median(group), "min": min(group), "max": max(group)}

    return {
        "binders": summary([value for label, value in pairs if label]),
        "non_binders": summary([value for label, value in pairs if not label]),
    }
