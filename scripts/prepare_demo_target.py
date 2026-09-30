"""Prepare a labelled AlphaFold DB modeling input; no model/GPU calls or chemistry repair."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from runner.design_inputs import prepare_target, sequence_bytes


def prepare(accession: str, expected: str, client: httpx.Client) -> tuple[bytes, dict]:
    """Fetch one public prediction and require exact identity with the supplied FASTA."""
    if not re.fullmatch(r"[A-Z0-9]{6,10}", accession):
        raise ValueError("Supply a UniProt accession, not a URL or path.")
    sequence_bytes(expected)
    url = f"https://alphafold.ebi.ac.uk/api/prediction/{accession}"
    response = client.get(url)
    response.raise_for_status()
    entries = response.json()
    if not isinstance(entries, list) or len(entries) != 1:
        raise ValueError("Expected exactly one AlphaFold DB prediction.")
    entry = entries[0]
    parsed = urlsplit(entry["cifUrl"])
    if parsed.scheme != "https" or parsed.hostname != "alphafold.ebi.ac.uk" or parsed.username or parsed.port not in {None, 443}:
        raise ValueError("Unexpected structure-download origin.")
    response = client.get(entry["cifUrl"])
    response.raise_for_status()
    raw = response.content
    target = prepare_target(raw, "cif", "A")
    if target["sequence"] != expected or entry.get("uniprotSequence") != expected:
        raise ValueError("Prediction does not match the supplied sequence exactly; no variant was substituted.")
    metadata = {"accession": accession, "source": entry["cifUrl"], "prediction": entry,
                "sha256": hashlib.sha256(raw).hexdigest(), "sequence_sha256": hashlib.sha256(sequence_bytes(expected)).hexdigest(),
                "residues": len(expected), "mapping": target["mapping"],
                "representation": "Predicted ordinary amino-acid model; no mature chromophore or modified-residue chemistry. No experimental resolved residues or reconstructed atoms.",
                "licence": "CC-BY-4.0", "licence_source": "https://alphafold.ebi.ac.uk/faq"}
    return raw, metadata


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("accession")
    parser.add_argument("fasta", type=Path)
    parser.add_argument("--label", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="anthropic/claude-opus-4.8")
    args = parser.parse_args()
    lines = args.fasta.read_text().splitlines()
    if sum(line.startswith(">") for line in lines) != 1:
        parser.error("Supply one labelled FASTA sequence.")
    sequence = "".join(line for line in lines if not line.startswith(">"))
    with httpx.Client(timeout=30, trust_env=False) as client:
        raw, metadata = prepare(args.accession, sequence, client)
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "target.cif").write_bytes(raw)
    (args.output / "source.json").write_text(json.dumps({"label": args.label, **metadata}, indent=2))
    config = {"prompt": f"Generate candidate protein binders to the supplied {args.label} target, then evaluate structures and sequence plausibility. "
                        f"Target source: {metadata['source']}. Representation: {metadata['representation']} "
                        "Summarize actual computational results and limitations; do not claim experimental binding or safety.",
              "model": args.model, "structure_path": "target.cif", "format": "cif", "chain": "A",
              "settings": {"count": 4, "min_length": 60, "max_length": 100, "seed": 0, "binding_residues": []}}
    (args.output / "demo.json").write_text(json.dumps(config, indent=2))
    print(f"Prepared {len(sequence)} residues. Inspect source.json; this is a predicted modeling input, not native chemistry.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
