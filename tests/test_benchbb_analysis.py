import math

import pytest

from demo.benchbb_analysis import (
    describe_groups,
    exact_auc_permutation,
    roc_auc,
    summarize_fold_result,
    summarize_fold_metrics,
)


def valid_metrics():
    return {
        "plddt_raw_scale": "0–1",
        "plddt": [0.8, 0.6, 0.9],
        "mean_plddt": pytest.approx((0.8 + 0.6 + 0.9) / 3),
        "ptm": 0.7,
        "iptm": 0.65,
        "pae": [[0.0, 1.0, 4.0], [1.0, 0.0, 6.0], [2.0, 8.0, 0.0]],
    }


def test_fold_summary_uses_declared_chain_slices_directions_and_strict_contact_cutoff():
    metrics = valid_metrics()
    metrics["mean_plddt"] = (0.8 + 0.6 + 0.9) / 3
    result = summarize_fold_metrics(
        metrics,
        target_length=2,
        binder_length=1,
        target_ca=[(0.0, 0.0, 0.0), (10.0, 0.0, 0.0)],
        binder_ca=[(8.0, 0.0, 0.0)],
    )
    assert result == {
        "target_mean_plddt": pytest.approx(0.7),
        "binder_mean_plddt": pytest.approx(0.9),
        "iptm": 0.65,
        "target_to_binder_mean_pae": 5.0,
        "target_to_binder_min_pae": 4.0,
        "binder_to_target_mean_pae": 5.0,
        "binder_to_target_min_pae": 2.0,
        "symmetric_interface_mean_pae": 5.0,
        "ca_contacts_lt_8a": 1,
    }


def test_fold_result_parses_validated_cif_chain_order_end_to_end():
    from test_design_workflow import coordinates

    metrics = valid_metrics()
    metrics["mean_plddt"] = (0.8 + 0.6 + 0.9) / 3
    result = summarize_fold_result(metrics, coordinates({"A": "AC", "B": "A"}), "AC", "A")
    assert result["ca_contacts_lt_8a"] == 2
    with pytest.raises(ValueError, match="chain order"):
        summarize_fold_result(metrics, coordinates({"B": "A", "A": "AC"}), "AC", "A")


@pytest.mark.parametrize("change, message", [
    (lambda m: m.update(iptm=None), "no ipTM"),
    (lambda m: m.pop("pae"), "no PAE"),
    (lambda m: m.update(pae=[[0.0]]), "aligned-error"),
    (lambda m: m.update(plddt=[0.8, 0.6]), "confidence dimensions"),
])
def test_fold_summary_rejects_missing_or_malformed_model_outputs(change, message):
    metrics = valid_metrics()
    metrics["mean_plddt"] = (0.8 + 0.6 + 0.9) / 3
    change(metrics)
    with pytest.raises(ValueError, match=message):
        summarize_fold_metrics(
            metrics,
            target_length=2,
            binder_length=1,
            target_ca=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0)],
            binder_ca=[(2.0, 0.0, 0.0)],
        )


@pytest.mark.parametrize("target_ca,binder_ca,cutoff,message", [
    ([(0.0, 0.0, 0.0)], [(1.0, 0.0, 0.0)], 8.0, "counts"),
    ([(0.0, 0.0, 0.0), (math.nan, 0.0, 0.0)], [(1.0, 0.0, 0.0)], 8.0, "finite triples"),
    ([(0.0, 0.0, 0.0), (1.0, 0.0, 0.0)], [(2.0, 0.0, 0.0)], 0.0, "positive"),
])
def test_fold_summary_rejects_bad_coordinate_boundary(target_ca, binder_ca, cutoff, message):
    metrics = valid_metrics()
    metrics["mean_plddt"] = (0.8 + 0.6 + 0.9) / 3
    with pytest.raises(ValueError, match=message):
        summarize_fold_metrics(
            metrics,
            target_length=2,
            binder_length=1,
            target_ca=target_ca,
            binder_ca=binder_ca,
            contact_cutoff=cutoff,
        )


@pytest.mark.parametrize("target_length,binder_length", [(0, 1), (1, 0), (1.0, 1)])
def test_fold_summary_rejects_nonpositive_or_noninteger_chain_lengths(target_length, binder_length):
    metrics = valid_metrics()
    metrics["mean_plddt"] = (0.8 + 0.6 + 0.9) / 3
    with pytest.raises(ValueError, match="positive integers"):
        summarize_fold_metrics(
            metrics,
            target_length=target_length,
            binder_length=binder_length,
            target_ca=[],
            binder_ca=[],
        )


def test_auc_respects_direction_and_gives_half_credit_for_ties():
    labels = [True, True, False, False]
    assert roc_auc(labels, [4.0, 3.0, 2.0, 1.0], higher_is_better=True) == 1.0
    assert roc_auc(labels, [4.0, 3.0, 2.0, 1.0], higher_is_better=False) == 0.0
    assert roc_auc([True, False], [1.0, 1.0], higher_is_better=True) == 0.5


def test_exact_auc_permutation_enumerates_all_assignments_and_is_order_invariant():
    first = exact_auc_permutation(
        [True, True, False, False], [4.0, 3.0, 2.0, 1.0], higher_is_better=True
    )
    second = exact_auc_permutation(
        [False, True, False, True], [1.0, 4.0, 2.0, 3.0], higher_is_better=True
    )
    assert first == second == {"auc": 1.0, "two_sided_exact_p": pytest.approx(1 / 3), "permutations": 6}


@pytest.mark.parametrize("labels,scores,message", [
    ([True], [1.0], "positive and negative"),
    ([True, False], [1.0], "differ in length"),
    ([True, False], [math.inf, 1.0], "finite"),
    ([True, False], [True, 1.0], "finite"),
    ([1, 0], [2.0, 1.0], "booleans"),
])
def test_auc_rejects_undefined_or_malformed_inputs(labels, scores, message):
    with pytest.raises(ValueError, match=message):
        exact_auc_permutation(labels, scores, higher_is_better=True)


def test_group_description_reports_values_without_inference():
    assert describe_groups([True, False, False], [4.0, 1.0, 3.0]) == {
        "binders": {"n": 1, "median": 4.0, "min": 4.0, "max": 4.0},
        "non_binders": {"n": 2, "median": 2.0, "min": 1.0, "max": 3.0},
    }


def test_group_description_rejects_empty_or_nonfinite_groups():
    with pytest.raises(ValueError, match="nonempty"):
        describe_groups([True], [1.0])
    with pytest.raises(ValueError, match="finite"):
        describe_groups([True, False], [1.0, math.nan])
