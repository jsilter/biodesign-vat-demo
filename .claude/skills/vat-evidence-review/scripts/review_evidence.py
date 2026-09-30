"""Inspect VAT evidence directly through repository code, without an MCP transport."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path


FIXTURES = ("clean", "tampered", "partial")


def repository(path: Path) -> Path:
    """Return a validated biodesign-vat-demo repository root."""
    root = path.resolve()
    marker = root / "pyproject.toml"
    if not marker.is_file() or 'name = "biodesign-vat-demo"' not in marker.read_text(encoding="utf-8"):
        raise ValueError("--repo must identify the biodesign-vat-demo repository root")
    sys.path.insert(0, str(root / "src"))
    return root


def demonstration(fixture: str) -> dict:
    """Create and project one ephemeral demonstration fixture."""
    from demo.demo import prepare_fixture
    from runner.graph import project

    with tempfile.TemporaryDirectory(prefix="vat-skill-demo-") as directory:
        bundle, trust, sequence = prepare_fixture(fixture, Path(directory))
        return {"fixture": fixture, "graph": project(bundle, trust, sequence)}


def verify(bundle_path: Path, trust_path: Path, sequence: str | None, require_transparency: bool) -> dict:
    """Load owner-supplied public evidence and compute its bounded verification projection."""
    from runner.graph import VerifyPolicy, project
    from vatlog.bundle import Bundle
    from vatlog.roles import TrustBundle

    bundle = Bundle.load(bundle_path)
    trust = TrustBundle.load(trust_path)
    policy = VerifyPolicy()
    graph = project(bundle, trust, sequence, policy)
    if require_transparency:
        from vatlog.verify import verify_bundle

        graph["bundle_report"] = asdict(
            verify_bundle(bundle, trust, submitted_sequence=sequence, require_transparency=True)
        )
    return {"bundle": bundle_path.name, "graph": graph}


def parser() -> argparse.ArgumentParser:
    """Build the bounded command-line interface exposed by this skill."""
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--repo", type=Path, default=Path.cwd())
    commands = value.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="summarize the three generated demonstration fixtures")
    inspect = commands.add_parser("inspect", help="return the verification graph for one fixture")
    inspect.add_argument("fixture", choices=FIXTURES)
    supplied = commands.add_parser("verify", help="verify an owner-supplied local bundle")
    supplied.add_argument("bundle", type=Path)
    supplied.add_argument("--trust", type=Path, required=True)
    supplied.add_argument("--sequence")
    supplied.add_argument("--require-transparency", action="store_true")
    return value


def main() -> int:
    """Run one read-only evidence operation and emit structured JSON."""
    args = parser().parse_args()
    root = repository(args.repo)
    try:
        if args.command == "list":
            result = {
                "demos": [
                    {
                        "fixture": fixture,
                        "outcome": demonstration(fixture)["graph"]["bundle_report"]["outcome"],
                    }
                    for fixture in FIXTURES
                ]
            }
        elif args.command == "inspect":
            result = demonstration(args.fixture)
        else:
            bundle = (root / args.bundle).resolve() if not args.bundle.is_absolute() else args.bundle.resolve()
            trust = (root / args.trust).resolve() if not args.trust.is_absolute() else args.trust.resolve()
            if not bundle.is_relative_to(root) or not trust.is_relative_to(root):
                raise ValueError("bundle and trust files must remain under the repository")
            result = verify(bundle, trust, args.sequence, args.require_transparency)
    except (OSError, ValueError, TypeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
