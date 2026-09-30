"""End-to-end demo. Runs without Modal, without a GPU, and without network.

    python -m demo.demo --fixture clean      all five checks pass
    python -m demo.demo --fixture tampered   check 1 fails, names the broken record
    python -m demo.demo --fixture doctored   all checks pass; the objectives are the finding
    python -m demo.demo --fixture partial    checks 1-4 pass, check 5 reports 1 of 10

Run them in that order: tamper detection first (the audience learns what the
log is), then the doctored notebook (the log is honest and the *content* is the problem),
then selective disclosure (the log is honest, the content is fine, and the *set* is the problem).
"""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from demo.sessions import DESIGNS, build_clean, build_doctored, build_partial
from vatlog.bundle import Bundle, build_bundle
from vatlog.roles import Keyring, Role, TrustBundle
from vatlog.notary import issue_receipt
from vatlog.verify import verify_bundle

RULE = "-" * 78


def _banner(title: str) -> None:
    print(f"\n{RULE}\n{title}\n{RULE}")


def prepare_fixture(fixture: str, root: Path, *, keyring=None, transparency=None) -> tuple[Bundle, TrustBundle, str]:
    """Generate a disclosed demo bundle, public role trust, and submitted sequence.

    The notary sees the full original chain before disclosure or post-seal tampering.
    Private keys stay in memory and are never returned to presentation code.
    """
    keyring = keyring or Keyring.generate()
    trust = keyring.trust_bundle()
    if fixture in ("clean", "tampered"):
        log, *_ = build_clean(root, keyring=keyring)
        submitted = DESIGNS[2]
    elif fixture == "doctored":
        log, *_ = build_doctored(root, keyring=keyring)
        submitted = DESIGNS[2]
    elif fixture == "partial":
        log, *_ = build_partial(root, keyring=keyring)
        submitted = DESIGNS[4]
    else:
        raise ValueError(f"unknown fixture {fixture}")

    receipt = issue_receipt(build_bundle(log), keyring.notary, trust)
    full = build_bundle(log)
    full.notary_receipt = receipt
    from vatlog.transparency import TransparencyLog
    if transparency is None:
        with TransparencyLog(root / "transparency.sqlite", trust, signer=keyring.transparency_log,
                             log_id="fixture-" + keyring.transparency_log.keyid) as local_log:
            evidence = local_log.append(full)
    else:
        evidence = transparency.append(full)
    reveal = [5] if fixture == "partial" else None
    bundle = build_bundle(log, reveal=reveal)
    bundle.notary_receipt = receipt
    bundle.transparency_evidence = evidence

    if fixture == "tampered":
        # Flip one field in one record, leaving every signature untouched. This is the
        # "designer edits the notebook after the fact" case.
        victim = bundle.records[1]
        bundle.records[1] = victim.model_copy(
            update={
                "user_session": victim.user_session.model_copy(
                    update={"purpose_tag": "biochemistry class"}
                )
            }
        )
    return bundle, trust, submitted


def run(fixture: str, root: Path) -> int:
    bundle, trust, submitted = prepare_fixture(fixture, root)
    head = bundle.signed_tree_head
    if fixture == "tampered":
        print("  (altered record 1's purpose tag after sealing)")

    _banner(f"fixture: {fixture}")
    print(f"session        {bundle.session_uuid}")
    print(f"signed head    tree_size={head.tree_size} root={head.root_hash[:16]}...")
    print(f"revealed       {bundle.revealed_indices()}")
    print(f"recorder key   {next(e.public_key for e in trust.entries if e.role == Role.RECORDER)}")

    print("\nrecorded objectives, per revealed run:")
    for rec in bundle.records:
        objs = ", ".join(f"{o.name} ({o.direction})" for o in rec.params.objectives)
        print(f"  [{rec.link.leaf_index}] {rec.record_type.value} purpose={rec.user_session.purpose_tag!r}")
        print(f"      optimized: {objs}")
        for artifact in rec.inputs + rec.results:
            if artifact.kind in ("prompt", "transcript", "reasoning"):
                print(f"      {artifact.kind}: {artifact.name} ({artifact.bytes_len} bytes; {artifact.sha256[:16]}...)")

    print("\nverification:")
    report = verify_bundle(bundle, trust, submitted_sequence=submitted, require_transparency=True)
    print(report.render())
    print(f"exit code: {report.exit_code}")

    if fixture == "doctored":
        print(
            "\nNote: every check passes. The finding is in the objectives above, not in the\n"
            "cryptography. That is the review-capacity gap RAND names, and the reason a\n"
            "reviewer reads the bundle at all."
        )
        print("The synthetic transcript artifacts also name the fixture's objectives. Their hashes\n"
              "commit to the supplied bytes, not to a complete or truthful account of agent activity.")
    if fixture == "partial":
        print(
            "\nNote: checks 1-4 pass because each revealed record is honest. The signed head\n"
            "says 10 (session opening + 9 runs); the bundle accounts for 1. Cardinality catches it."
        )
    return report.exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="demo")
    parser.add_argument(
        "--fixture", default="clean",
        choices=["clean", "tampered", "doctored", "partial"],
    )
    parser.add_argument("--root", type=Path, default=None, help="where to write the log")
    args = parser.parse_args(argv)

    if args.root:
        return run(args.fixture, args.root)
    with tempfile.TemporaryDirectory() as tmp:
        return run(args.fixture, Path(tmp))


if __name__ == "__main__":
    raise SystemExit(main())
