"""Local operator commands and atomic public output writing."""

import os
import argparse
import tempfile
from pathlib import Path

from .bundle import Bundle
from .canon import parse_json
from .keys import load_private
from .roles import LocalSigner, Role, TrustBundle
from .transparency import TransparencyLog, TransparencyCheckpoint


def read_public_object(path: Path) -> object:
    """Bound observer checkpoint/proof input before JSON parsing."""
    with path.open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("Observer evidence exceeds 64 KiB")
    return parse_json(raw)


def atomic_write(path: Path, raw: bytes) -> None:
    """Durably replace one output and its directory entry."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        pending = Path(stream.name)
        try:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(pending, path)
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            pending.unlink(missing_ok=True)


def atomic_bundle(bundle: Bundle, path: Path) -> None:
    """Serialize using the normal ZIP writer, then durably publish the bytes."""
    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / "bundle.zip"
        bundle.save(archive)
        atomic_write(path, archive.read_bytes())


def add_commands(sub: argparse._SubParsersAction) -> None:
    for name in ("log-init", "log-append", "log-export", "log-consistency"):
        parser = sub.add_parser(name, help="operate the simulated local transparency log")
        parser.add_argument("--store", type=Path, required=True)
        parser.add_argument("--trust", type=Path, required=True)
        if name in {"log-init", "log-append"}:
            parser.add_argument("--log-key", type=Path, required=True)
        if name == "log-init":
            parser.add_argument("--log-id", required=True)
        else:
            parser.add_argument("--out", type=Path, required=True)
        if name == "log-append":
            parser.add_argument("bundle", type=Path)
        if name == "log-consistency":
            parser.add_argument("--from-checkpoint", type=Path, required=True)
            parser.add_argument("--to-checkpoint", type=Path, required=True)


def run(args: argparse.Namespace) -> int:
    trust = TrustBundle.load(args.trust)
    signer = LocalSigner(Role.TRANSPARENCY_LOG, load_private(args.log_key)) if hasattr(args, "log_key") else None
    if args.cmd == "log-init" and args.store.exists():
        raise ValueError("Transparency store already exists")
    if args.cmd != "log-init" and not args.store.exists():
        raise ValueError("Transparency store is not initialized")
    if hasattr(args, "out"):
        protected = [args.store, args.trust] + [getattr(args, n) for n in
                    ("log_key", "bundle", "from_checkpoint", "to_checkpoint") if hasattr(args, n)]
        if any(args.out.resolve() == p.resolve() or (args.out.exists() and p.exists() and args.out.samefile(p)) for p in protected):
            raise ValueError("Output must be distinct from input, trust, keys and store")
    with TransparencyLog(args.store, trust, signer=signer, log_id=getattr(args, "log_id", None)) as log:
        if args.cmd == "log-append":
            bundle = Bundle.load(args.bundle)
            bundle.transparency_evidence = log.append(bundle)
            atomic_bundle(bundle, args.out)
        elif args.cmd == "log-export":
            atomic_write(args.out, log.export().canonical())
        elif args.cmd == "log-consistency":
            first = TransparencyCheckpoint.model_validate(read_public_object(args.from_checkpoint))
            last = TransparencyCheckpoint.model_validate(read_public_object(args.to_checkpoint))
            atomic_write(args.out, log.consistency(first, last).canonical())
    return 0
