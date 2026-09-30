"""Anchor one synthetic demo receipt to Sigstore's public Rekor log, after an explicit gate.

Without ``--acknowledge-publication`` this prepares a notarized bundle whose only recorded
artifact is 64 random bytes under a random session UUID (no design content of any kind),
prints the disclosure preview and stops; no credential is read. With the flag and a Google
service-account key it runs ``vat anchor-sigstore`` exactly once against production Fulcio,
Rekor v2 and the public TSA (no browser or interactive login), verifies the result with
``--require-public-transparency``, and prints the public coordinates.

Three separate claims: local notarization is simulated (``local-null-screen/1``); Rekor
inclusion under the service-account identity is independently observable; the RFC 3161 token
supplies independent publication time. The identity and receipt digest become permanently
discoverable. Nothing about execution time, scientific truth or safety is established.
"""

from __future__ import annotations

import argparse
import json
import os
import uuid
from pathlib import Path

from demo.sessions import build_session
from vatlog.bundle import build_bundle
from vatlog.cli import main as vat
from vatlog.notary import issue_receipt
from vatlog.roles import Keyring
from vatlog.verify import verify_bundle


def prepare(root: Path) -> tuple[Path, Path]:
    keys = Keyring.generate()
    trust = keys.trust_bundle()
    session, *_ = build_session(root / "session", session_uuid=str(uuid.uuid4()), n_runs=0,
                                purpose_tag="sigstore canary: random bytes, no design content",
                                objectives=[], keyring=keys, instructions=os.urandom(64))
    bundle = build_bundle(session)
    bundle.notary_receipt = issue_receipt(bundle, keys.notary, trust)
    assert verify_bundle(bundle, trust).outcome.value == "NOT_INCLUDED", "receipt-only bundles are intermediate"
    root.mkdir(parents=True, exist_ok=True)
    bundle.save(root / "notarized.zip")
    trust.save(root / "trust.json")
    return root / "notarized.zip", root / "trust.json"


def run(root: Path, *, acknowledge: bool, key: Path | None, trusted_root: Path | None) -> int:
    notarized, trust = prepare(root)
    print(f"prepared {notarized} (one opening record over 64 random bytes; no design content)")
    anchored = root / "anchored.zip"
    argv = ["anchor-sigstore", str(notarized), "--output", str(anchored), "--trust", str(trust)]
    if key is not None:
        argv += ["--google-service-account-key", str(key)]
    if trusted_root is not None:
        argv += ["--sigstore-trust-root", str(trusted_root)]
    if not acknowledge or key is None:
        print("stopping before publication; re-run with --acknowledge-publication and "
              "--google-service-account-key to publish once, permanently")
        return vat(argv)
    code = vat(argv + ["--acknowledge-publication"])
    if code != 0:
        return code
    identity = json.loads(key.read_text())["client_email"]
    verify = ["verify", str(anchored), "--trust", str(trust), "--require-public-transparency",
              "--sigstore-identity", identity]
    if trusted_root is not None:
        verify += ["--sigstore-trust-root", str(trusted_root)]
    print("verifying the anchored bundle with --require-public-transparency:")
    code = vat(verify)
    print(f"search the public log for certificates issued to {identity}, or by the receipt digest above;")
    print("local notarization remains simulated and execution time remains unverified")
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=Path(".local/sigstore-demo"))
    parser.add_argument("--acknowledge-publication", action="store_true",
                        help="create exactly one permanent public Rekor entry for the synthetic receipt")
    parser.add_argument("--google-service-account-key", type=Path, help="service-account JSON key under .local/")
    parser.add_argument("--sigstore-trust-root", type=Path, help="cached trusted root for offline re-verification")
    args = parser.parse_args()
    raise SystemExit(run(args.out, acknowledge=args.acknowledge_publication, key=args.google_service_account_key,
                         trusted_root=args.sigstore_trust_root))
