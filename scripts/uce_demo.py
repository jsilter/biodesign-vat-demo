"""Build a bounded VAT dynamic job over a few ultraconserved elements from a published supplement.

Cummins et al. (2024, MBE, msae146) release their human UCE sets as GRCh38 coordinates, not
sequences. This script pins the publisher's supplement ZIP by digest, selects a small,
evenly spaced subset of the 279-element 100% set, fetches each sequence from NCBI one region
at a time, and writes a fixture. From that fixture it writes a `vat-dynamic-job/1` proposal
whose standard-library source recomputes the set's per-base composition and compares it with
the STREME background the paper reports (A/T 0.312, C/G 0.188).

The sealed job never touches the network: the sequences are its declared, public input, and
their bytes are what the record commits to. Re-fetching them is how a reviewer checks the input.

    PYTHONPATH=src .venv/bin/python scripts/uce_demo.py fetch --zip papers/msae146_supplementary_data.zip --count 10 --out fixtures/uce_100set_subset.json
    PYTHONPATH=src .venv/bin/python scripts/uce_demo.py run --fixture fixtures/uce_100set_subset.json
    PYTHONPATH=src .venv/bin/python scripts/uce_demo.py proposal --fixture fixtures/uce_100set_subset.json --out results/uce/proposal.json
    PYTHONPATH=src .venv/bin/python scripts/uce_demo.py dispatch --proposal results/uce/proposal.json \
        --approve-digest <digest> --authorized --output results/uce

`dispatch` is the paid step. It runs the job service the loopback backend runs, against its own
store under the output directory, so it works where the backend is unreachable (Claude Science
sandboxes the connector away from port 8000). It needs the fixed `biodesign-vat-dynamic` Modal
app deployed and refuses without both the exact digest and `--authorized`.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.request import urlopen
from uuid import uuid4

PAPER = "Cummins et al. 2024, Mol Biol Evol, msae146: The Evolution of Ultraconserved Elements in Vertebrates"
SET_FILE = "Supplementary_File_4_human_uce_100_percent_set.txt"
EXPECTED = {"at_per_base": 0.312, "cg_per_base": 0.188, "source": "STREME background, Supplementary File 10"}
EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
MAX_ELEMENTS = 279
REQUEST_INTERVAL = 0.4  # NCBI permits three requests per second without an API key.

JOB_SOURCE = '''
def summarize(value, output_dir):
    """Per-base composition of the declared UCE subset against the paper's reported background."""
    from pathlib import Path
    elements = value["elements"]
    rows, counts = [], {"A": 0, "C": 0, "G": 0, "T": 0}
    for element in elements:
        sequence = element["sequence"].upper()
        if set(sequence) - set("ACGT") or len(sequence) != element["end"] - element["start"]:
            raise ValueError("element sequence does not match its declared coordinates")
        for base in sequence:
            counts[base] += 1
        gc = (sequence.count("G") + sequence.count("C")) / len(sequence)
        rows.append((element["accession"], element["start"], element["end"], len(sequence), round(gc, 4)))
    total = sum(counts.values())
    at = (counts["A"] + counts["T"]) / total / 2
    cg = (counts["C"] + counts["G"]) / total / 2
    expected = value["expected"]
    lengths = sorted(row[3] for row in rows)
    Path(output_dir, "composition.tsv").write_text(
        "accession\\tstart\\tend\\tlength\\tgc_fraction\\n"
        + "".join("\\t".join(str(field) for field in row) + "\\n" for row in rows), encoding="utf-8")
    return {"elements": len(rows), "total_bp": total, "counts": counts,
            "at_per_base": round(at, 4), "cg_per_base": round(cg, 4),
            "expected": {"at_per_base": expected["at_per_base"], "cg_per_base": expected["cg_per_base"]},
            "deviation": {"at_per_base": round(at - expected["at_per_base"], 4),
                          "cg_per_base": round(cg - expected["cg_per_base"], 4)},
            "length": {"min": lengths[0], "median": lengths[len(lengths) // 2], "max": lengths[-1]}}
'''

INPUT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["paper", "supplement", "expected", "elements"],
    "properties": {
        "paper": {"type": "string", "minLength": 1, "maxLength": 256},
        "supplement": {"type": "object", "additionalProperties": False,
                       "required": ["zip_sha256", "file", "file_sha256", "selection"],
                       "properties": {"zip_sha256": {"type": "string", "minLength": 64, "maxLength": 64},
                                      "file": {"type": "string", "minLength": 1, "maxLength": 128},
                                      "file_sha256": {"type": "string", "minLength": 64, "maxLength": 64},
                                      "selection": {"type": "string", "minLength": 1, "maxLength": 256}}},
        "expected": {"type": "object", "additionalProperties": False,
                     "required": ["at_per_base", "cg_per_base", "source"],
                     "properties": {"at_per_base": {"type": "number", "minimum": 0, "maximum": 1},
                                    "cg_per_base": {"type": "number", "minimum": 0, "maximum": 1},
                                    "source": {"type": "string", "minLength": 1, "maxLength": 256}}},
        "elements": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["accession", "start", "end", "sequence"],
            "properties": {"accession": {"type": "string", "minLength": 1, "maxLength": 32},
                           "start": {"type": "integer", "minimum": 0},
                           "end": {"type": "integer", "minimum": 1},
                           "sequence": {"type": "string", "minLength": 1, "maxLength": 4096}}}},
    },
}


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_set(zip_path: Path) -> tuple[list[tuple[str, int, int]], str, str]:
    """The 100% set's coordinates (0-based half-open) plus the ZIP and file digests."""
    raw = zip_path.read_bytes()
    with zipfile.ZipFile(zip_path) as archive:
        text = archive.read(SET_FILE)
    rows = []
    for line in text.decode("utf-8").splitlines():
        if not line.strip():
            continue
        accession, start, end = line.split("\t")
        if int(end) <= int(start):
            raise ValueError(f"empty interval in {SET_FILE}: {line!r}")
        rows.append((accession, int(start), int(end)))
    if len(rows) != MAX_ELEMENTS:
        raise ValueError(f"{SET_FILE} holds {len(rows)} elements, expected {MAX_ELEMENTS}")
    return rows, sha256_hex(raw), sha256_hex(text)


def select_elements(rows: list, count: int) -> list:
    """Evenly spaced positions across the file, so the subset spans chromosomes and is reproducible."""
    if not 1 <= count <= len(rows):
        raise ValueError(f"count must be from 1 to {len(rows)}")
    return [rows[index * len(rows) // count] for index in range(count)]


def efetch_url(accession: str, start: int, end: int) -> str:
    """NCBI regions are 1-based and inclusive; the supplement is 0-based half-open."""
    return (f"{EFETCH}?db=nuccore&id={accession}&seq_start={start + 1}&seq_stop={end}"
            "&rettype=fasta&retmode=text")


def parse_fasta(text: str, expected_length: int) -> str:
    lines = text.strip().splitlines()
    if not lines or not lines[0].startswith(">"):
        raise ValueError("NCBI returned no FASTA record")
    sequence = "".join(line.strip() for line in lines[1:]).upper()
    if len(sequence) != expected_length or set(sequence) - set("ACGT"):
        raise ValueError(f"fetched sequence has {len(sequence)} bases, expected {expected_length} of ACGT")
    return sequence


def fetch_sequence(accession: str, start: int, end: int, opener=urlopen) -> str:
    with opener(efetch_url(accession, start, end), timeout=30) as response:
        return parse_fasta(response.read().decode("utf-8"), end - start)


def build_fixture(zip_path: Path, count: int, opener=urlopen, pause: float = REQUEST_INTERVAL) -> dict:
    rows, zip_digest, file_digest = read_set(zip_path)
    elements = []
    for accession, start, end in select_elements(rows, count):
        elements.append({"accession": accession, "start": start, "end": end,
                         "sequence": fetch_sequence(accession, start, end, opener)})
        time.sleep(pause)
    return {"paper": PAPER,
            "supplement": {"zip_sha256": zip_digest, "file": SET_FILE, "file_sha256": file_digest,
                           "selection": f"{count} of {len(rows)} elements at positions i*{len(rows)}//{count}"},
            "expected": dict(EXPECTED), "elements": elements}


def build_proposal(fixture: dict) -> dict:
    return {"version": "vat-dynamic-job/1", "id": str(uuid4()),
            "label": f"UCE composition check ({len(fixture['elements'])} elements)",
            "purpose": ("Recompute per-base composition of a declared subset of the published human "
                        "100% UCE set and compare it with the paper's reported STREME background. "
                        "Standard library only; no network; the sequences are public declared input."),
            "source": JOB_SOURCE, "entrypoint": "summarize", "input_schema": INPUT_SCHEMA,
            "input": fixture, "packages": [], "output_version": "vat-dynamic-output/1",
            "resources": {"cpu": 1, "memory_mb": 512, "timeout_seconds": 300, "gpu": None,
                          "network": False, "secrets": False, "volumes": False},
            "public_input": True, "authorized": True}


def run_locally(fixture: dict) -> dict:
    """Execute the job source here, the same way the launcher does, without Modal or sealing."""
    namespace: dict = {"__name__": "approved_dynamic_job"}
    exec(compile(JOB_SOURCE, "approved_job.py", "exec"), namespace, namespace)
    with tempfile.TemporaryDirectory(prefix="uce-demo-") as output_dir:
        return namespace["summarize"](fixture, output_dir)


def dispatch(proposal_value: dict, output: Path, *, approve_digest: str, authorized: bool,
             transport=None) -> dict:
    """Propose, approve the exact digest, run once, seal, verify, and write a tampered copy.

    A rerun on the same output directory resumes the existing job by identity; it never
    submits a replacement call. ``transport`` exists for offline tests only.
    """
    if not authorized:
        raise ValueError("Modal dispatch requires --authorized after explicit owner approval")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from runner.design_inputs import DesignError
    from runner.design_store import DesignStore
    from runner.dynamic_contracts import validate_dynamic_proposal
    from runner.dynamic_service import DynamicJobService
    from runner.graph import falsify
    from vatlog.bundle import Bundle
    from vatlog.roles import TrustBundle
    from vatlog.verify import verify_bundle

    proposal = validate_dynamic_proposal(proposal_value)
    if proposal.digest != approve_digest:
        raise ValueError(f"approval digest {approve_digest[:12]}… does not match the proposal's {proposal.digest[:12]}…")
    if output.is_symlink():
        raise ValueError("output directory cannot be a symlink")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    if transport is None:
        os.environ["VAT_MODAL_ENABLED"] = "1"
    store = DesignStore(output / "private")
    service = DynamicJobService(store, transport(store) if callable(transport) else transport)

    async def run() -> dict:
        state = service.propose(proposal_value)
        if state["status"] in {"proposed", "approved"}:
            service.approve(state["id"], approve_digest, spend_authorized=True)
            state = service.start(state["id"])
        elif state["status"] == "running":
            await service.reconcile()
        task = service.tasks.get(state["id"])
        if task is not None:
            try:
                await task
            except DesignError:
                pass  # The state carries the failure; nothing is resubmitted.
        return service.snapshot(state["id"])

    try:
        state = asyncio.run(run())
        summary = {"job_id": state["id"], "status": state["status"], "error": state.get("error"),
                   "digest": state["digest"], "node_id": state.get("node_id"), "output": str(output)}
        if state["status"] != "completed":
            return summary
        # The service registers the published evidence files as artifacts of the job.
        evidence = {entry["name"]: entry for entry in store.artifacts(state["id"]) if entry["kind"] == "evidence"}
        for name in ("bundle.zip", "trust.json"):
            (output / name).write_bytes(store.read(state["id"], evidence[name]["sha256"], name))
        bundle = Bundle.load(output / "bundle.zip")
        trust = TrustBundle.load(output / "trust.json")
        leaf = next(record.link.leaf_index for record in bundle.records
                    if record.params.hyperparams.get("vat_node_id") == state["node_id"])
        tampered, description = falsify(bundle, {"kind": "result", "leaf": leaf})
        tampered.save(output / "tampered-bundle.zip")
        original_report = verify_bundle(bundle, trust)
        tampered_report = verify_bundle(tampered, trust)
        result = service.transport.result(state["id"]).value["result"]
        (output / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        summary.update(result=result, bundle=str(output / "bundle.zip"), trust=str(output / "trust.json"),
                       tampered_bundle=str(output / "tampered-bundle.zip"), tampering=description,
                       bundle_outcome=original_report.outcome, bundle_exit_code=original_report.exit_code,
                       tampered_outcome=tampered_report.outcome, tampered_exit_code=tampered_report.exit_code)
        return summary
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch", help="select elements, fetch their sequences from NCBI, write the fixture")
    fetch.add_argument("--zip", type=Path, required=True, help="the publisher's supplement ZIP")
    fetch.add_argument("--count", type=int, default=10)
    fetch.add_argument("--out", type=Path, required=True)
    run = commands.add_parser("run", help="run the job source locally on a fixture; no Modal, nothing sealed")
    run.add_argument("--fixture", type=Path, required=True)
    proposal = commands.add_parser("proposal", help="write an unapproved vat-dynamic-job/1 proposal")
    proposal.add_argument("--fixture", type=Path, required=True)
    proposal.add_argument("--out", type=Path, required=True)
    paid = commands.add_parser("dispatch", help="approve one exact proposal digest and run it once on Modal (paid)")
    paid.add_argument("--proposal", type=Path, required=True)
    paid.add_argument("--approve-digest", required=True, help="the digest the owner reviewed and approved")
    paid.add_argument("--authorized", action="store_true", help="the owner authorized one paid execution")
    paid.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "dispatch":
        value = json.loads(args.proposal.read_text(encoding="utf-8"))
        print(json.dumps(dispatch(value, args.output, approve_digest=args.approve_digest,
                                  authorized=args.authorized), indent=2))
        return 0
    if args.command == "fetch":
        fixture = build_fixture(args.zip, args.count)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(fixture, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.out}: {len(fixture['elements'])} elements, zip sha256 {fixture['supplement']['zip_sha256']}")
        return 0
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    if args.command == "run":
        print(json.dumps(run_locally(fixture), indent=2))
        return 0
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from runner.dynamic_contracts import validate_dynamic_proposal
    value = build_proposal(fixture)
    validated = validate_dynamic_proposal(value)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.out}: proposal digest {validated.digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
