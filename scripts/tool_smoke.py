"""Run one real Modal adapter with durable identity and capture its observed result.

Uses the same input validation, transport, artifact store and VAT evidence as the UI.
Requires an enabled deployment and explicit confirmation of billed GPU work.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
from dotenv import load_dotenv
from pydantic import ValidationError

from runner.design import DesignService
from runner.design_inputs import (DesignError, DesignSettings, ProteinMPNNSettings,
                                  prepare_backbone, prepare_target, sequence_bytes)
from runner.design_store import DesignStore
from runner.provider import OpenRouter


async def smoke(tool: str, config: dict, output: Path) -> int:
    """Persist before submission; uncertainty is never automatically resubmitted."""
    output.mkdir(parents=True, exist_ok=False)
    store = DesignStore(output / "journal")
    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        service = DesignService(store, OpenRouter("", client))
        id = str(uuid4())
        started = datetime.now(timezone.utc).isoformat()
        request = {"prompt": f"Single-adapter operator canary: {tool}", "settings": config, "model": "none"}
        state = {"id": id, "status": "running", "started": started, "jobs": [], "candidates": [],
                 "events": [], "artifacts": [], "logs": [], "downloads": [], "text": "", "record_count": 1}
        store.create(id, request, state)
        (output / "run-id.txt").write_text(id)
        before = time.monotonic()
        try:
            result = await service.scientific(id, tool, config)
            _, state = store.get(id)
            state["status"] = "failed" if result.get("error") else "completed"
            store.save(id, state)
            service.publish(id)
            report = {"status": "FAIL" if result.get("error") else "PASS", "tool": tool,
                      "elapsed_seconds": time.monotonic()-before, "execution": result.get("execution"),
                      "cost_information": None, "error": result.get("error"),
                      "evidence_status": service.snapshot(id).get("evidence_status")}
            (output / "canary.json").write_text(json.dumps(report, indent=2, allow_nan=False))
            for artifact in service.snapshot(id)["artifacts"]:
                (output / artifact["name"]).write_bytes(store.read(id, artifact["sha256"], artifact["name"]))
            print(json.dumps(report, indent=2))
            return 0 if report["status"] == "PASS" and report["evidence_status"] == "verified" else 1
        except (DesignError, OSError, TimeoutError) as exc:
            print(str(exc) if isinstance(exc, DesignError) else "Connection unavailable; inspect the saved job before retrying.")
            return 1
        finally:
            await service.shutdown()
            store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tool", choices=["boltzgen", "proteinmpnn", "esmfold2", "esmc"])
    parser.add_argument("--sequence", type=Path, help="Canonical binder amino-acid bytes (not FASTA)")
    parser.add_argument("--target", type=Path)
    parser.add_argument("--backbone", type=Path, help="ProteinMPNN PDB/mmCIF backbone")
    parser.add_argument("--design-chain", action="append", help="Original backbone chain to redesign; repeatable")
    parser.add_argument("--chain", default="A")
    parser.add_argument("--mode", choices=["binder", "complex"], default="binder")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--temperature", type=float, default=.1)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--confirm-paid-work", action="store_true")
    args = parser.parse_args()
    if not args.confirm_paid_work:
        parser.error("Explicitly pass --confirm-paid-work after reviewing the GPU settings.")
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False, interpolate=False)
    try:
        settings = DesignSettings(count=args.count, seed=args.seed).model_dump()
    except ValidationError as exc:
        parser.error(f"Invalid count or seed: {exc.errors()[0]['msg']}.")
    target = None
    if args.target:
        target = prepare_target(args.target.read_bytes(), "pdb" if args.target.suffix.lower() == ".pdb" else "cif", args.chain)
    if args.tool == "boltzgen":
        if target is None:
            parser.error("BoltzGen requires --target.")
        config = {**settings, "target_cif": target["prepared_cif"], "target_sequence": target["sequence"]}
    elif args.tool == "proteinmpnn":
        if args.backbone is None or not args.design_chain:
            parser.error("ProteinMPNN requires --backbone and at least one --design-chain.")
        if args.seed < 1:
            parser.error("ProteinMPNN requires a nonzero --seed for deterministic sampling.")
        prepared = prepare_backbone(args.backbone.read_bytes(), "pdb" if args.backbone.suffix.lower() == ".pdb" else "cif")
        lookup = {entry["original_chain"]: entry["prepared_chain"] for entry in prepared["chain_map"]}
        if any(chain not in lookup for chain in args.design_chain):
            parser.error("Every --design-chain must name a chain in the uploaded backbone.")
        try:
            mpnn = ProteinMPNNSettings(count=args.count, sampling_temperature=args.temperature,
                                       seed=args.seed, design_chains=[lookup[chain] for chain in args.design_chain])
        except ValidationError as exc:
            parser.error(f"Invalid ProteinMPNN setting: {exc.errors()[0]['msg']}.")
        config = {**mpnn.model_dump(), "backbone_cif": prepared["prepared_cif"],
                  "sequences": prepared["sequences"], "chain_map": prepared["chain_map"],
                  "mapping": prepared["mapping"]}
    else:
        if args.sequence is None:
            parser.error("Scoring/prediction requires --sequence (canonical bytes, not FASTA).")
        sequence = args.sequence.read_bytes().decode("ascii")
        sequence_bytes(sequence)
        config = {"sequence": sequence}
        if args.tool == "esmfold2":
            if args.mode == "complex" and target is None:
                parser.error("Complex prediction requires --target.")
            config.update(mode=args.mode, seed=args.seed, target_sequence=target["sequence"] if target else "",
                          num_loops=10, num_sampling_steps=68, num_diffusion_samples=1)
    return asyncio.run(smoke(args.tool, config, args.output))


if __name__ == "__main__":
    raise SystemExit(main())
