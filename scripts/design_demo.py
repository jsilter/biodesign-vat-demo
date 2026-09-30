"""Run a configured design test and verify a downloaded candidate's exact sequence.

Usage: .venv/bin/python scripts/design_demo.py path/to/demo.json
The JSON supplies prompt, model, structure_path, format, chain and optional settings.
This command starts billed model/GPU work when the backend's Modal tools are enabled.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from uuid import uuid4

import httpx

from vatlog.bundle import Bundle
from vatlog.roles import TrustBundle
from vatlog.verify import verify_bundle


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("configuration", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--confirm-paid-work", action="store_true")
    args = parser.parse_args()
    if not args.confirm_paid_work:
        parser.error("This starts paid work; explicitly pass --confirm-paid-work after reviewing resource settings.")
    config = json.loads(args.configuration.read_text())
    path = (args.configuration.resolve().parent / config.pop("structure_path")).resolve()
    request = {"id": str(uuid4()), **config, "structure": path.read_text()}
    args.output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    executions = []
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=30) as client:
        reply = client.post("/api/designs", json=request)
        reply.raise_for_status()
        state = reply.json()
        id = state["id"]
        print("Run ID:", id, flush=True)
        (args.output / "run-id.txt").write_text(id)
        try:
            while state["status"] in {"running", "recovering", "cancelling"}:
                print(state["status"], state["logs"][-1]["message"] if state["logs"] else "", flush=True)
                time.sleep(2)
                reply = client.get(f"/api/designs/{id}")
                reply.raise_for_status()
                state = reply.json()
        except KeyboardInterrupt:
            reply = client.post(f"/api/designs/{id}/cancel", json={})
            reply.raise_for_status()
            print("Cancellation status:", reply.json()["status"], flush=True)
            return 130
        (args.output / "summary.json").write_text(json.dumps(state, indent=2, allow_nan=False))
        for artifact in state["artifacts"]:
            reply = client.get(f'/api/designs/{id}/artifacts/{artifact["sha256"]}/{artifact["name"]}')
            reply.raise_for_status()
            (args.output / artifact["name"]).write_bytes(reply.content)
            if artifact["name"].startswith("response-") and artifact["name"].endswith(".json"):
                observed = json.loads(reply.content)
                if isinstance(observed, dict) and observed.get("execution"):
                    executions.append(observed["execution"])
    canary = {"workflow_id": id, "status": state["status"], "counts": state["counts"],
              "wall_seconds": time.monotonic() - started, "temperature": "unclassified",
              "cost": None, "cost_note": "Consult Modal and OpenRouter billing; no enforced dollar cap.",
              "executions": executions, "result": "FAIL"}
    (args.output / "canary.json").write_text(json.dumps(canary, indent=2, allow_nan=False))
    if state["status"] != "completed" or not state["candidates"]:
        print("No complete candidate demonstration:", state["status"])
        return 1
    bundle = Bundle.load(args.output / "bundle.zip")
    trust = TrustBundle.load(args.output / "trust.json")
    sequence = state["candidates"][0]["sequence"]
    report = verify_bundle(bundle, trust, submitted_sequence=sequence)
    print(report.render())
    (args.output / "selected-sequence.txt").write_text(sequence)
    (args.output / "sequence-verification.txt").write_text(report.render())
    # Choose an alteration that matches no other revealed sequence output.
    recorded = {a.sha256 for r in bundle.records for a in r.results if a.kind == "aa_sequence"}
    import hashlib
    altered = None
    for index in range(len(sequence)):
        for amino_acid in "ACDEFGHIKLMNPQRSTVWY":
            candidate = sequence[:index] + amino_acid + sequence[index+1:]
            if candidate != sequence and hashlib.sha256(candidate.encode()).hexdigest() not in recorded:
                altered = candidate
                break
        if altered:
            break
    if not altered:
        raise ValueError("No unrecorded one-residue alteration available.")
    (args.output / "altered-sequence.txt").write_text(altered)
    altered_report = verify_bundle(bundle, trust, submitted_sequence=altered)
    print(altered_report.render())
    (args.output / "altered-verification.txt").write_text(altered_report.render())
    passed = report.exit_code == 0 and altered_report.exit_code == 5
    canary.update(result="PASS" if passed else "FAIL", sequence_exit=report.exit_code,
                  altered_sequence_exit=altered_report.exit_code)
    (args.output / "canary.json").write_text(json.dumps(canary, indent=2, allow_nan=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
