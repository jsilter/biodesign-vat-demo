"""The audit bundle: what a designer hands a verifier.

A bundle reveals a *subset* of the session's records. That is the point: an honest
designer reveals all of them, and a bundle that reveals fewer is exactly the selective
disclosure case. Nothing here prevents building a partial bundle -- ``verify`` check 5 is
what notices.
"""

from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass
import json
import re
from zipfile import ZipFile, ZIP_STORED, ZIP_DEFLATED
from zlib import error as DecompressionError

from pydantic import BaseModel, ConfigDict

from .attest import DSSEEnvelope
from .canon import canonical_record_bytes, parse_json
from .head import SignedTreeHead
from .notary import NotaryReceipt
from .transparency import TransparencyEvidence
from .sigstore_transparency import SigstoreTransparencyEvidence
from .schema import VatRecord
from .store import SessionLog

FORMAT = "vat-bundle/1"


@dataclass(frozen=True)
class BundleLimits:
    """Reader resource policy, independent of the signed protocol and artifact kinds."""

    max_archive_bytes: int = 64 * 1024**2
    max_manifest_bytes: int = 8 * 1024**2
    max_uncompressed_bytes: int = 256 * 1024**2
    max_members: int = 10_000

    def __post_init__(self) -> None:
        if any(type(value) is not int or value <= 0 for value in vars(self).values()):
            raise ValueError("bundle limits must be positive integers")


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


class Bundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    log_id: str
    session_uuid: str
    records: list[VatRecord]
    signed_tree_head: SignedTreeHead
    attestation: DSSEEnvelope
    notary_receipt: NotaryReceipt | None = None
    transparency_evidence: TransparencyEvidence | None = None
    sigstore_evidence: SigstoreTransparencyEvidence | None = None
    inclusion_proofs: list[dict]
    blobs: dict[str, bytes] = {}  # sha256 -> original file bytes

    def revealed_indices(self) -> list[int]:
        return sorted(r.link.leaf_index for r in self.records)

    def save(self, path: Path) -> None:
        """Write a portable ZIP with metadata and unmodified artifact files."""
        if not all(_valid_digest(digest) for digest in self.blobs):
            raise ValueError("invalid blob digest")
        manifest = self.model_dump(mode="json", exclude={"blobs"})
        manifest.update(bundle_format=FORMAT, blob_digests=sorted(self.blobs))
        with ZipFile(path, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest, indent=2))
            for digest, data in sorted(self.blobs.items()):
                archive.writestr(f"blobs/{digest}", data)

    @staticmethod
    def load(path: Path, *, limits: BundleLimits | None = None) -> "Bundle":
        """Read a ZIP without extraction, applying a configurable reader resource policy."""
        limits = limits or BundleLimits()
        if path.stat().st_size > limits.max_archive_bytes:
            raise ValueError("archive byte limit exceeded")
        with ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > limits.max_members:
                raise ValueError("archive member limit exceeded")
            if sum(info.file_size for info in members) > limits.max_uncompressed_bytes:
                raise ValueError("uncompressed byte limit exceeded")
            if any(info.flag_bits & 1 or info.compress_type not in (ZIP_STORED, ZIP_DEFLATED)
                   for info in members):
                raise ValueError("encrypted or unsupported ZIP compression")
            names = [info.filename for info in members]
            if len(names) != len(set(names)) or "manifest.json" not in names:
                raise ValueError("duplicate archive members or missing manifest")
            def read_member(name: str, cap: int) -> bytes:
                info = archive.getinfo(name)
                if info.file_size > cap:
                    raise ValueError(f"member byte limit exceeded: {name}")
                try:
                    with archive.open(info) as stream:
                        data = stream.read(cap + 1)
                except DecompressionError as exc:
                    raise ValueError("invalid compressed ZIP member") from exc
                if len(data) > cap or len(data) != info.file_size:
                    raise ValueError("ZIP member size mismatch or byte limit exceeded")
                return data

            manifest = parse_json(read_member("manifest.json", limits.max_manifest_bytes))
            if not isinstance(manifest, dict) or manifest.pop("bundle_format", None) != FORMAT:
                raise ValueError("unsupported bundle format")
            digests = manifest.pop("blob_digests", None)
            if (not isinstance(digests, list)
                    or not all(_valid_digest(d) for d in digests)
                    or len(digests) != len(set(digests))):
                raise ValueError("invalid or duplicate blob digests")
            expected = {"manifest.json", *(f"blobs/{d}" for d in digests)}
            if set(names) != expected or "blobs" in manifest:
                raise ValueError("unexpected or missing archive members or embedded blobs")
            bundle = Bundle.model_validate({
                **manifest,
                "blobs": {d: read_member(f"blobs/{d}", limits.max_uncompressed_bytes) for d in digests},
            })
            for record in bundle.records:
                canonical_record_bytes(record)
            return bundle


def build_bundle(
    log: SessionLog,
    reveal: list[int] | None = None,
    *,
    include_blobs: bool = True,
) -> Bundle:
    """Export a bundle revealing ``reveal`` (default: every record)."""
    head = log.head()
    att = log.attestation()
    if head is None or att is None:
        raise ValueError("session is not sealed; call SessionLog.seal first")

    indices = sorted(range(len(log.records)) if reveal is None else reveal)
    records = [log.records[i].model_copy(deep=True) for i in indices]
    proofs = [log.tree.prove_inclusion(i) for i in indices]

    blobs: dict[str, bytes] = {}
    if include_blobs:
        for rec in records:
            for art in list(rec.inputs) + list(rec.results) + list(rec.intermediates):
                if art.sha256 not in blobs:
                    blobs[art.sha256] = log.blobs.get(art.sha256)

    return Bundle(
        log_id=log.log_id,
        session_uuid=log.session_uuid,
        records=records,
        signed_tree_head=head,
        attestation=att,
        inclusion_proofs=proofs,
        blobs=blobs,
    )
