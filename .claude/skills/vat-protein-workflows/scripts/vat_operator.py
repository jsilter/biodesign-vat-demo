"""Operate the VAT BenchBB demonstration directly, without an MCP transport."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def repository(path: Path) -> Path:
    """Return the development repository or this upload's bundled runtime root."""
    requested = path.resolve()
    if (requested / "pyproject.toml").is_file() and (requested / "src/vatlog").is_dir():
        return requested
    bundled = Path(__file__).resolve().parents[1] / "runtime"
    if (bundled / "src/vatlog").is_dir():
        return bundled
    raise ValueError("--repo must identify the biodesign-vat-demo repository root")


def environment(root: Path) -> dict[str, str]:
    """Build a child environment with the selected VAT source tree."""
    value = os.environ.copy()
    current = value.get("PYTHONPATH")
    value["PYTHONPATH"] = str(root / "src") + (os.pathsep + current if current else "")
    return value


def benchbb_command(root: Path, args: list[str]) -> int:
    """Run the reviewed resumable BenchBB operator and preserve its exit status."""
    script = root / "scripts/run_benchbb_reference.py"
    return subprocess.run(
        [sys.executable, str(script), *args], cwd=root, env=environment(root), check=False
    ).returncode


def publish(root: Path, output: Path) -> dict:
    """Notarize finalized BenchBB evidence and append it recoverably to the local log."""
    sys.path.insert(0, str(root / "src"))
    from runner.design_store import DesignStore
    from runner.publication import PublicationStore
    from vatlog.bundle import Bundle
    from vatlog.canon import sha256_hex
    from vatlog.notary import issue_receipt
    from vatlog.verify import verify_bundle

    output = output.resolve()
    archive = output / "bundle.zip"
    report = output / "report.json"
    state = json.loads((output / "operator-state.json").read_text(encoding="utf-8"))
    if state.get("finalized") is not True:
        raise ValueError("BenchBB evidence must be finalized before publication")
    store = DesignStore(output / "private")
    try:
        keys = store.keys()
        identity = state["session_id"]
        with PublicationStore(output / "publication", keys) as publication:
            existing = publication.db.execute(
                "SELECT request FROM publications WHERE id=?", (identity,)
            ).fetchone()
            if existing is None:
                bundle = Bundle.load(archive)
                if bundle.notary_receipt is None:
                    bundle.notary_receipt = issue_receipt(bundle, keys.notary, keys.trust_bundle())
                with tempfile.TemporaryDirectory(prefix="vat-skill-publish-") as directory:
                    prepared = Path(directory) / "bundle.zip"
                    bundle.save(prepared)
                    raw = prepared.read_bytes()
                request_digest = sha256_hex(raw)
                publication.prepare(identity, request_digest, raw, report.read_bytes())
            published = publication.publish(identity)
        notarized = Bundle.load(published / "bundle.zip")
        verification = verify_bundle(notarized, keys.trust_bundle(), require_transparency=True)
        return {
            "published_directory": str(published),
            "bundle_sha256": hashlib.sha256((published / "bundle.zip").read_bytes()).hexdigest(),
            "outcome": verification.outcome,
            "tree_size": notarized.transparency_evidence.checkpoint.tree_size,
            "notarization": "simulated",
            "transparency": "laptop-local public-log simulation",
        }
    finally:
        store.close()


def parser() -> argparse.ArgumentParser:
    """Build the direct operator interface."""
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--repo", type=Path, default=Path.cwd())
    commands = value.add_subparsers(dest="command", required=True)
    commands.add_parser("catalog")
    commands.add_parser("benchbb-preview")
    run = commands.add_parser("benchbb-run")
    run.add_argument("--output", type=Path, default=Path(".local/benchbb/run"))
    run.add_argument("--tool", action="append", choices=["esmfold2", "esmc"])
    run.add_argument("--max-new-calls", type=int, default=1)
    run.add_argument("--compute-budget-usd", type=float, required=True)
    run.add_argument("--authorized", action="store_true")
    finalize = commands.add_parser("benchbb-finalize")
    finalize.add_argument("--output", type=Path, default=Path(".local/benchbb/run"))
    publication = commands.add_parser("benchbb-publish")
    publication.add_argument("--output", type=Path, default=Path(".local/benchbb/run"))
    return value


def main() -> int:
    """Execute one bounded operator action."""
    args = parser().parse_args()
    try:
        root = repository(args.repo)
        panel = root / "fixtures/benchbb_bbf14_panel.json"
        if args.command == "catalog":
            sys.path.insert(0, str(root / "src"))
            from runner.tool_catalog import public_catalog

            print(json.dumps({"tools": public_catalog()}, indent=2, sort_keys=True))
            return 0
        if args.command == "benchbb-preview":
            return benchbb_command(root, ["--panel", str(panel)])
        output = args.output if args.output.is_absolute() else root / args.output
        if args.command == "benchbb-run":
            if not args.authorized:
                raise ValueError("Modal dispatch requires --authorized after explicit owner approval")
            if args.max_new_calls < 1:
                raise ValueError("authorized dispatch requires a positive --max-new-calls limit")
            command = [
                "--panel", str(panel), "--output", str(output), "--execute-paid",
                "--max-new-calls", str(args.max_new_calls),
                "--compute-budget-usd", str(args.compute_budget_usd),
            ]
            for tool in args.tool or ["esmfold2", "esmc"]:
                command.extend(["--tool", tool])
            return benchbb_command(root, command)
        if args.command == "benchbb-finalize":
            return benchbb_command(
                root, ["--panel", str(panel), "--output", str(output), "--finalize"]
            )
        print(json.dumps(publish(root, output), indent=2, sort_keys=True))
        return 0
    except (OSError, KeyError, TypeError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
