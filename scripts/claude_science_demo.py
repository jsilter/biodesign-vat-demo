"""Generate canonical unpaid proposals used by the Claude Science demonstration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import uuid4

from runner.dynamic_contracts import validate_dynamic_proposal
from runner.tool_contracts import tool_request
from runner.workflow_contracts import dry_run


def sequence(path: Path) -> str:
    return "".join(line.strip() for line in path.read_text().splitlines() if not line.startswith(">"))


def registered(target_cif: str, target_sequence: str) -> dict:
    generation_id, prediction_id, score_id = (str(uuid4()) for _ in range(3))
    return {"version": "vat-workflow/1", "id": str(uuid4()), "label": "GFP binder dry-run",
            "purpose": "One BoltzGen proposal with fixed Boltz-2 and ESMC evaluations.", "nodes": [
        {"id": "generate", "tool_id": "boltzgen", "execution_id": generation_id,
         "request": tool_request("boltzgen", {"target_cif": target_cif, "target_chains": ["A"],
            "target_sequences": {"A": target_sequence}, "count": 1, "seed": 7,
            "min_length": 60, "max_length": 60, "hotspots": []}), "authorized": False},
        {"id": "predict", "tool_id": "boltz2", "execution_id": prediction_id,
         "request": tool_request("boltz2", {"sequences": {"A": target_sequence, "B": "AC"},
            "msa_mode": "none", "template_policy": "none", "count": 1, "seed": 7}),
         "bindings": [{"destination": "/payload/sequences/B", "producer": "generate",
                       "selector": {"pointer": "/candidates/0/sequence"}}], "authorized": False},
        {"id": "score", "tool_id": "esmc", "execution_id": score_id,
         "request": tool_request("esmc", {"sequence": "AC", "task": "score", "seed": 7}),
         "bindings": [{"destination": "/payload/sequence", "producer": "generate",
                       "selector": {"pointer": "/candidates/0/sequence"}}], "authorized": False},
    ]}


def dynamic(value: str) -> dict:
    source = '''def analyze(value, output_dir):
    sequence = value["sequence"]
    counts = {residue: sequence.count(residue) for residue in sorted(set(sequence))}
    fraction = sum(residue in "AVILMFWY" for residue in sequence) / len(sequence)
    return {"length": len(sequence), "counts": counts, "hydrophobic_fraction": fraction}
'''
    return {"version": "vat-dynamic-job/1", "id": str(uuid4()), "label": "GFP sequence summary",
            "purpose": "Deterministic descriptive sequence statistics.", "source": source,
            "entrypoint": "analyze", "input_schema": {"type": "object", "properties": {
                "sequence": {"type": "string", "minLength": 1, "maxLength": 2048}},
                "required": ["sequence"], "additionalProperties": False},
            "input": {"sequence": value}, "packages": [], "output_version": "vat-dynamic-output/1",
            "resources": {"cpu": 1, "memory_mb": 512, "timeout_seconds": 300, "gpu": None,
                          "network": False, "secrets": False, "volumes": False},
            "public_input": True, "authorized": False}


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="demo", required=True)
    registered_parser = subparsers.add_parser("registered")
    registered_parser.add_argument("--target-cif", type=Path, required=True)
    subparsers.add_parser("dynamic")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    value = sequence(root / "fixtures" / "gfp-wildtype.fasta")
    if args.demo == "registered":
        proposal = registered(args.target_cif.read_text(encoding="utf-8"), value)
        output = {"proposal": proposal, "dry_run": dry_run(proposal)}
    else:
        proposal = validate_dynamic_proposal(dynamic(value))
        output = {"proposal": proposal.model_dump(mode="json"), "digest": proposal.digest}
    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
