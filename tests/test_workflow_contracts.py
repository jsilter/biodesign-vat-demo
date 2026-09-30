"""Data-only workflow proposal validation."""

from uuid import uuid4

import pytest

from runner.design_inputs import DesignError
from runner.tool_contracts import tool_request
from runner.workflow_contracts import dry_run, replace_binding, validate_workflow


def node(id: str, *, producer: str | None = None, authorized: bool = True, tool: str = "esmc") -> dict:
    request = tool_request(tool, {"sequence": "AC", "task": "score", "seed": 1})
    bindings = [] if producer is None else [{
        "destination": "/payload/sequence",
        "producer": producer,
        "selector": {"kind": "return", "pointer": "/candidates/0/sequence"},
    }]
    return {"id": id, "tool_id": tool, "execution_id": str(uuid4()), "request": request,
            "bindings": bindings, "authorized": authorized}


def proposal(nodes: list[dict]) -> dict:
    return {"version": "vat-workflow/1", "id": str(uuid4()), "label": "test graph",
            "purpose": "Exercise explicit recorded dependencies.", "nodes": nodes}


def test_workflow_digest_and_dry_run_are_canonical_and_side_effect_free():
    value = proposal([node("generate"), node("score-a", producer="generate"),
                      node("score-b", producer="generate", authorized=False)])
    parsed = validate_workflow(value)
    repeated = validate_workflow(parsed.model_dump(mode="json"))
    projection = dry_run(value)

    assert parsed.digest == repeated.digest == projection["digest"]
    assert projection["dispatchable"] is False
    assert projection["nodes"][1]["dependencies"] == ["generate"]
    assert projection["nodes"][1]["resource"] == "L4"


@pytest.mark.parametrize("mutate", [
    lambda nodes: nodes + [{**nodes[0], "execution_id": str(uuid4())}],
    lambda nodes: [node("consumer", producer="later"), node("later")],
    lambda nodes: [{**nodes[0], "tool_id": "boltz1"}],
    lambda nodes: [{**nodes[0], "tool_id": "dssp"}],
])
def test_workflow_rejects_duplicate_forward_blocked_and_mismatched_nodes(mutate):
    nodes = [node("first")]
    with pytest.raises(DesignError) as rejected:
        validate_workflow(proposal(mutate(nodes)))
    assert rejected.value.code == "INVALID_WORKFLOW"


def test_workflow_rejects_missing_destinations_overlap_and_unapproved_intermediates():
    base = node("consumer", producer="producer")
    producer = node("producer")
    for bindings in [
        [{**base["bindings"][0], "destination": "/payload/missing"}],
        [base["bindings"][0], {**base["bindings"][0], "destination": "/payload"}],
        [{**base["bindings"][0], "selector": {"kind": "artifact", "name": "x",
                                                "classification": "intermediate"}}],
    ]:
        with pytest.raises(DesignError):
            validate_workflow(proposal([producer, {**base, "bindings": bindings}]))


def test_replace_binding_copies_request_and_targets_one_existing_leaf():
    request = node("first")["request"]
    changed = replace_binding(request, "/payload/sequence", "DE")
    assert changed["payload"]["sequence"] == "DE"
    assert request["payload"]["sequence"] == "AC"
    with pytest.raises(DesignError):
        replace_binding(request, "/payload/absent", "DE")
