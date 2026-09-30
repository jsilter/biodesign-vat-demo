"""Run a local observer workflow, preserving checkpoints across three appends."""

import argparse
import tempfile
from pathlib import Path

from demo.sessions import build_clean
from vatlog.bundle import build_bundle
from vatlog.notary import issue_receipt
from vatlog.roles import Keyring
from vatlog.transparency import TransparencyLog
from vatlog.transparency_cli import atomic_write
from vatlog.verify import verify_bundle


def run(root: Path) -> None:
    keys = Keyring.generate()
    trust = keys.trust_bundle()
    with TransparencyLog(root / "log.sqlite", trust, signer=keys.transparency_log, log_id="observer-demo") as log:
        bundles = []
        for index in range(3):
            session, *_ = build_clean(root / str(index), keyring=keys)
            bundle = build_bundle(session)
            bundle.notary_receipt = issue_receipt(bundle, keys.notary, trust)
            bundle.transparency_evidence = log.append(bundle)
            bundles.append(bundle)
        first, target = [b.transparency_evidence.checkpoint for b in bundles[:2]]
        proof = log.consistency(first, target)
        report = verify_bundle(bundles[1], trust, require_transparency=True,
                               witness_checkpoint=first, witness_consistency_proof=proof)
        assert report.exit_code == 0
        atomic_write(root / "first.json", first.canonical())
        atomic_write(root / "target.json", target.canonical())
        atomic_write(root / "proof.json", proof.canonical())
        bundles[1].save(root / "bundle.zip")
        trust.save(root / "trust.json")
        stripped = bundles[1].model_copy(deep=True)
        stripped.notary_receipt = stripped.transparency_evidence = None
        rejection = verify_bundle(stripped, trust, require_transparency=True)
        assert rejection.exit_code == 4
        print(f"Log size: {len(log.export().entries)}; observer proof: {first.tree_size} -> {target.tree_size}")
        print(report.render())
        print(f"Stripped evidence: {rejection.outcome.value} (exit {rejection.exit_code})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.out:
        run(args.out)
    else:
        with tempfile.TemporaryDirectory(prefix="vat-observer-") as temporary:
            run(Path(temporary))
