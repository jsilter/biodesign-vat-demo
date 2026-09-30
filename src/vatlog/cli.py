"""``vat`` command line for keys, signing, inspection and verification.

Exit codes come from ``verify.EXIT_CODES`` so a caller can branch on INCOMPLETE (6) versus
TAMPERED (2) without parsing text.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from binascii import Error as Base64Error
from cryptography.exceptions import InvalidSignature
from dataclasses import asdict
import sys
from pathlib import Path
from datetime import datetime, timezone
from zipfile import BadZipFile

from .bundle import Bundle
from .keys import generate_key, load_private, public_from_b64, save_private
from .roles import LocalSigner, Role, TrustBundle, TrustEntry
from .schema import DesignerStatement
from .notary import issue_receipt
from .verify import EXIT_CODES, verify_bundle
from .sigstore_transparency import DEFAULT_ISSUER

_EXIT_HELP = "exit codes:\n" + "\n".join(
    f"  {code}  {outcome.value}" for outcome, code in sorted(EXIT_CODES.items(), key=lambda kv: kv[1])
)
PUBLICATION_UNKNOWN_EXIT = 7
_ANCHOR_HELP = (
    "Signs only the SHA-256 digest of the canonical notary receipt payload with Sigstore's keyless\n"
    "flow under a Google service-account identity, submits it to Sigstore's public Rekor log and\n"
    "requests an RFC 3161 timestamp over the signature. The entry is permanent and public and names\n"
    "the service-account email. Rekor establishes inclusion; the RFC 3161 token establishes\n"
    "publication time; neither proves execution time, scientific truth, safety, or independence of\n"
    "the simulated notary. No browser or interactive login occurs; the only credential is the\n"
    "service-account key file, which is never copied or echoed.\n"
    f"exit codes: 0 anchored; 2 refused or failed before publication; {PUBLICATION_UNKNOWN_EXIT} "
    "PUBLICATION_OUTCOME_UNKNOWN (a submission was sent; see --recovery-dir)"
)


def _sigstore_root(path: Path | None, injected, parser: argparse.ArgumentParser):
    """Load the verifier's cached trusted root, or refresh Sigstore's production root online."""
    if injected is not None:
        return injected
    from .sigstore_transparency import load_trusted_root
    try:
        return load_trusted_root(path)
    except (ValueError, OSError) as exc:
        parser.exit(2, f"vat: {exc}\n")


def _distinct_output(output: Path, *inputs: Path) -> None:
    if any(output.resolve() == p.resolve() or (output.exists() and p.exists() and output.samefile(p)) for p in inputs):
        raise ValueError("Output must be distinct from the input bundle, trust and recovery files")


def main(argv: list[str] | None = None, *, sigstore_services=None, sigstore_trusted_root=None) -> int:
    """``sigstore_services`` and ``sigstore_trusted_root`` exist so tests never reach the network."""
    parser = argparse.ArgumentParser(prog="vat")
    sub = parser.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser(
        "verify",
        help="run the five ordered checks over a bundle",
        epilog=_EXIT_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    v.add_argument("bundle", type=Path)
    verifier_trust = v.add_mutually_exclusive_group(required=True)
    verifier_trust.add_argument("--pubkey", help="base64 pinned Ed25519 public key; roles not distinguished")
    verifier_trust.add_argument("--trust", type=Path, help="public role trust bundle JSON")
    v.add_argument("--sequence", help="the sequence being submitted, for check 4")
    v.add_argument("--require-transparency", action="store_true",
                   help="require valid local or public transparency evidence for a notarized bundle")
    v.add_argument("--require-public-transparency", action="store_true",
                   help="require verified Sigstore Rekor inclusion and an RFC 3161 timestamp")
    v.add_argument("--sigstore-trust-root", type=Path, metavar="FILE",
                   help="cached Sigstore trusted root JSON; default refreshes the production root online")
    v.add_argument("--sigstore-identity", metavar="EMAIL",
                   help="expected certificate identity of public evidence (the service-account email)")
    v.add_argument("--sigstore-issuer", metavar="URL", default=DEFAULT_ISSUER,
                   help=f"expected OIDC issuer of public evidence (default {DEFAULT_ISSUER})")
    v.add_argument("--witness-checkpoint", type=Path)
    v.add_argument("--witness-consistency-proof", type=Path)

    a = sub.add_parser("anchor-sigstore", help="publish the notary receipt commitment to public Sigstore Rekor (irreversible)",
                       epilog=_ANCHOR_HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    a.add_argument("bundle", type=Path)
    a.add_argument("--output", type=Path, required=True, help="new bundle with public evidence; input is never modified")
    a.add_argument("--trust", type=Path, required=True, help="public role trust bundle JSON")
    a.add_argument("--acknowledge-publication", action="store_true",
                   help="confirm the permanent public disclosure printed by the preview; without it nothing is sent")
    credential = a.add_mutually_exclusive_group()
    credential.add_argument("--google-service-account-key", type=Path, metavar="FILE",
                            help="Google service-account JSON key; mints a Sigstore-audience token with no browser")
    credential.add_argument("--identity-token-file", type=Path, metavar="FILE",
                            help="pre-minted OIDC token with audience sigstore from another Fulcio-accepted issuer")
    a.add_argument("--sigstore-identity", metavar="EMAIL",
                   help="expected certificate identity; required with --recover-from, otherwise must match the token")
    a.add_argument("--sigstore-issuer", metavar="URL", default=DEFAULT_ISSUER)
    a.add_argument("--acknowledge-duplicate", action="store_true",
                   help="allow a second public entry after an earlier attempt for the same receipt")
    a.add_argument("--sigstore-trust-root", type=Path, metavar="FILE",
                   help="cached Sigstore trusted root JSON for verifying the response")
    a.add_argument("--recovery-dir", type=Path, metavar="DIR",
                   help="owner-only directory for attempt markers and saved responses (default: OUTPUT.recovery)")
    a.add_argument("--recover-from", type=Path, metavar="FILE",
                   help="rebuild output from a saved standard Sigstore bundle without any network submission")
    a.set_defaults(pubkey=None)

    s = sub.add_parser("show", help="print a bundle's records and objectives")
    s.add_argument("bundle", type=Path)

    p = sub.add_parser("provenance", help="verify recorded data/control relationships separately from the five bundle checks")
    p.add_argument("bundle", type=Path)
    p.add_argument("--trust", type=Path, required=True)
    p.add_argument("--prov", action="store_true", help="include the derived PROV-O JSON-LD projection")
    p.set_defaults(pubkey=None)

    k = sub.add_parser("keygen", help="create one role's private key and update public trust JSON")
    k.add_argument("--role", choices=[r.value for r in Role], required=True)
    k.add_argument("--private", type=Path, required=True)
    k.add_argument("--trust", type=Path, required=True)

    d = sub.add_parser("sign-session", help="sign a designer statement before recording")
    d.add_argument("--designer-key", type=Path, required=True)
    d.add_argument("--session", required=True)
    d.add_argument("--purpose", required=True)
    d.add_argument("--out", type=Path, required=True)

    n = sub.add_parser("notarize", help="apply simulated local-null-screen/1 policy")
    n.add_argument("bundle", type=Path)
    n.add_argument("--notary-key", type=Path, required=True)
    n.add_argument("--trust", type=Path, required=True)
    n.add_argument("--out", type=Path, required=True)

    from .transparency_cli import add_commands, run as run_log
    add_commands(sub)
    args = parser.parse_args(argv)
    if args.cmd.startswith("log-"):
        try:
            return run_log(args)
        except (ValueError, OSError, sqlite3.Error, InvalidSignature) as exc:
            parser.exit(2, f"vat: {exc}\n")
    if args.cmd in ("keygen", "sign-session"):
        try:
            if args.cmd == "keygen":
                if args.private.resolve() == args.trust.resolve():
                    raise ValueError("private key and public trust paths must differ")
                trust = TrustBundle.load(args.trust) if args.trust.exists() else TrustBundle(entries=[])
                if not trust.role_bound or any(e.role == Role(args.role) for e in trust.entries):
                    raise ValueError("trust must be role-bound with no existing key for this role")
                signer = LocalSigner(Role(args.role), generate_key())
                updated = TrustBundle(entries=[*trust.entries, TrustEntry.from_signer(signer)])
                save_private(signer.key, args.private)
                updated.save(args.trust)
            else:
                signer = LocalSigner(Role.DESIGNER, load_private(args.designer_key))
                statement = DesignerStatement(session_uuid=args.session, keyid=signer.keyid,
                                              purpose_tag=args.purpose, issued_at=datetime.now(timezone.utc))
                statement.signature = signer.sign(statement.signing_payload())
                args.out.write_text(statement.model_dump_json(indent=2))
        except (ValueError, OSError) as exc:
            parser.exit(2, f"vat: {exc}\n")
        return 0
    try:
        bundle = Bundle.load(args.bundle)
    except (ValueError, BadZipFile, OSError) as exc:
        parser.exit(2, f"vat: invalid bundle: {exc}\n")

    if args.cmd == "show":
        head = bundle.signed_tree_head
        print(f"session {bundle.session_uuid}  log {bundle.log_id}")
        print(f"head: tree_size={head.tree_size} root={head.root_hash[:16]}... at {head.timestamp}")
        print(f"revealed leaves: {bundle.revealed_indices()}")
        for rec in bundle.records:
            objs = ", ".join(
                f"{o.name}({o.direction} w={o.weight})" for o in rec.params.objectives
            )
            print(f"  [{rec.link.leaf_index}] {rec.record_type.value}")
            print(f"      purpose: {rec.user_session.purpose_tag}")
            print(f"      objectives: {objs or '(none)'}")
            for group in ("inputs", "intermediates", "results"):
                for artifact in getattr(rec, group):
                    availability = "present" if artifact.sha256 in bundle.blobs else "withheld (digest only)"
                    print(f"      {group}: {artifact.kind} {artifact.name} "
                          f"sha256={artifact.sha256[:16]}... bytes={artifact.bytes_len} {availability}")
        return 0

    if args.cmd == "anchor-sigstore":
        return _anchor_sigstore(args, bundle, parser, sigstore_services, sigstore_trusted_root)
    try:
        trust = TrustBundle.load(args.trust) if args.trust else TrustBundle.pinned(public_from_b64(args.pubkey))
        if args.cmd == "notarize":
            bundle.notary_receipt = issue_receipt(
                bundle, LocalSigner(Role.NOTARY, load_private(args.notary_key)), trust
            )
            from .transparency_cli import atomic_bundle
            atomic_bundle(bundle, args.out)
            print("notarization: simulated (local-null-screen/1); independent timestamp unavailable")
            return 0
    except (ValueError, OSError) as exc:
        parser.exit(2, f"vat: {exc}\n")
    if args.cmd == "provenance":
        from .provenance import bundle_provenance
        from .prov_export import export_prov
        report, nodes = bundle_provenance(bundle, trust)
        output = asdict(report)
        if args.prov:
            output["prov"] = export_prov(nodes)
        print(json.dumps(output, indent=2))
        return report.exit_code
    from .transparency import TransparencyCheckpoint, ConsistencyEvidence
    from .transparency_cli import read_public_object
    try:
        if bool(args.witness_checkpoint) != bool(args.witness_consistency_proof):
            raise ValueError("Witness checkpoint and consistency proof must be paired")
        witness = (TransparencyCheckpoint.model_validate(read_public_object(args.witness_checkpoint))
                   if args.witness_checkpoint else None)
        proof = (ConsistencyEvidence.model_validate(read_public_object(args.witness_consistency_proof))
                 if args.witness_consistency_proof else None)
    except (ValueError, OSError) as exc:
        parser.exit(2, f"vat: {exc}\n")
    root = None
    if bundle.sigstore_evidence is not None or args.require_public_transparency:
        if not args.sigstore_identity:
            parser.exit(2, "vat: --sigstore-identity is required to verify public transparency evidence\n")
        root = _sigstore_root(args.sigstore_trust_root, sigstore_trusted_root, parser)
    report = verify_bundle(bundle, trust, submitted_sequence=args.sequence,
        require_transparency=args.require_transparency,
        require_public_transparency=args.require_public_transparency, witness_checkpoint=witness,
        witness_consistency_proof=proof, sigstore_trusted_root=root,
        sigstore_identity=args.sigstore_identity, sigstore_issuer=args.sigstore_issuer)
    print(report.render())
    return report.exit_code


def _anchor_sigstore(args: argparse.Namespace, bundle: Bundle, parser: argparse.ArgumentParser,
                     services, injected_root) -> int:
    from .sigstore_transparency import (PublicationError, PublicationOutcomeUnknown, ProductionSigstoreServices,
                                        anchor_receipt, disclosure_preview, mint_identity_token,
                                        read_identity_token, recover_anchor)
    from .transparency_cli import atomic_bundle
    recovery_dir = args.recovery_dir or args.output.with_name(args.output.name + ".recovery")
    try:
        _distinct_output(args.output, args.bundle, args.trust, *([args.recover_from] if args.recover_from else []))
        if args.output.exists():
            raise ValueError("Output already exists; choose a new path")
        trust = TrustBundle.load(args.trust)
        if args.recover_from is None:
            if not (args.google_service_account_key or args.identity_token_file):
                raise ValueError("--google-service-account-key or --identity-token-file is required to publish")
            print(disclosure_preview(bundle, trust).render())
            if not args.acknowledge_publication:
                print("Nothing was sent and no credential was read. Re-run with --acknowledge-publication "
                      "to publish permanently.")
                return 2
        elif not args.sigstore_identity:
            raise ValueError("--sigstore-identity is required with --recover-from")
    except (ValueError, OSError) as exc:
        parser.exit(2, f"vat: {exc}\n")
    root = _sigstore_root(args.sigstore_trust_root, injected_root, parser)
    try:
        if args.recover_from is not None:
            anchored, inclusion = recover_anchor(bundle, trust, args.recover_from, root,
                                                 identity=args.sigstore_identity, issuer=args.sigstore_issuer)
        else:
            def token_source():
                token = (mint_identity_token(args.google_service_account_key) if args.google_service_account_key
                         else read_identity_token(args.identity_token_file))
                if args.sigstore_identity and (token.identity, token.issuer) != (args.sigstore_identity, args.sigstore_issuer):
                    raise PublicationError("Credential identity or issuer differs from --sigstore-identity/--sigstore-issuer")
                return token
            if services is None:
                services = ProductionSigstoreServices.production()
            anchored, inclusion = anchor_receipt(bundle, trust, services, acknowledged=True, trusted_root=root,
                                                 recovery_dir=recovery_dir, token_source=token_source,
                                                 allow_duplicate=args.acknowledge_duplicate)
        atomic_bundle(anchored, args.output)
    except PublicationOutcomeUnknown as exc:
        parser.exit(PUBLICATION_UNKNOWN_EXIT, f"vat: PUBLICATION_OUTCOME_UNKNOWN: {exc}\n"
                    f"vat: input preserved; any complete response is under {recovery_dir}; nothing was resubmitted\n")
    except (PublicationError, ValueError, OSError, InvalidSignature, Base64Error) as exc:
        saved = recovery_dir / f"{anchored.sigstore_evidence.receipt_digest}.sigstore.json" if "anchored" in locals() else None
        hint = f"; recover with --recover-from {saved}" if saved is not None and saved.exists() else ""
        parser.exit(2, f"vat: {exc}{hint}\n")
    print(f"public transparency: Sigstore Rekor inclusion verified (log {inclusion.log_id}, index {inclusion.log_index},"
          f" checkpoint origin {inclusion.checkpoint_origin})")
    print(f"RFC 3161 publication time: {inclusion.timestamp.isoformat()} (not execution time)")
    print(f"signing identity: {inclusion.identity} (issuer {inclusion.issuer}); "
          f"receipt digest: {anchored.sigstore_evidence.receipt_digest}")
    print("notarization remains simulated (local-null-screen/1); local execution time remains unverified")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
