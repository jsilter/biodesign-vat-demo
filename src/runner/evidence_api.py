"""Bounded, read-only evidence workspace; no signer, tool client, or path API."""
from __future__ import annotations

import base64
import binascii
import tempfile
from pathlib import Path
from uuid import UUID, uuid4
from zipfile import BadZipFile

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response
from starlette.background import BackgroundTask

from vatlog.bundle import Bundle, BundleLimits
from vatlog.canon import parse_json, sha256_hex
from vatlog.roles import TrustBundle
from .graph import VerifyPolicy, falsify, project

PUBLIC_POLICY_FIELDS = {"require_public_transparency", "sigstore_identity", "sigstore_issuer", "sigstore_trusted_root"}


def verify_policy(data: object) -> VerifyPolicy:
    """Build verifier expectations from the upload's ``public`` object; the bundle never supplies them.

    ``sigstore_trusted_root`` is the JSON text of an official trusted root the verifier obtained
    independently. When absent and an identity is expected, the client library's production TUF
    cache is used without a network refresh.
    """
    if data is None:
        return VerifyPolicy()
    if not isinstance(data, dict) or set(data) - PUBLIC_POLICY_FIELDS:
        raise ValueError("Unknown public policy field")
    require = data.get("require_public_transparency", False)
    identity, issuer, root_text = data.get("sigstore_identity"), data.get("sigstore_issuer"), data.get("sigstore_trusted_root")
    if type(require) is not bool:
        raise ValueError("require_public_transparency must be a boolean")
    for value, limit in ((identity, 320), (issuer, 256), (root_text, 4 * 256 * 1024)):
        if value is not None and (not isinstance(value, str) or not 1 <= len(value) <= limit):
            raise ValueError("Invalid public policy value")
    root = None
    if identity is not None or require:
        from vatlog.sigstore_transparency import load_trusted_root, parse_trusted_root
        root = parse_trusted_root(root_text.encode()) if root_text is not None else load_trusted_root(None, offline=True)
    return VerifyPolicy(require_public_transparency=require, sigstore_identity=identity,
                        sigstore_issuer=issuer, sigstore_trusted_root=root)


def evidence_routes() -> APIRouter:
    """Create an isolated public-evidence workspace, capped at eight loaded archives."""
    router = APIRouter(prefix="/api/evidence")
    entries: dict[str, tuple[Bundle, TrustBundle, str | None, VerifyPolicy]] = {}
    retained_bytes = 0

    async def body(request: Request, limit: int) -> dict:
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > limit:
                raise HTTPException(413, "Evidence request exceeds size limit")
        try:
            value = parse_json(bytes(data))
            if not isinstance(value, dict):
                raise ValueError("Expected an object")
            return value
        except (ValueError, RecursionError) as exc:
            raise HTTPException(422, "Invalid evidence request") from exc

    def get(identifier: UUID):
        if str(identifier) not in entries:
            raise HTTPException(404, "Evidence is unavailable; upload it again")
        return entries[str(identifier)]

    @router.post("")
    async def upload(request: Request):
        nonlocal retained_bytes
        if len(entries) >= 8:
            raise HTTPException(409, "Evidence workspace is full; remove an upload first")
        data = await body(request, 90 * 1024**2)
        try:
            if set(data) - {"archive", "trust", "sequence", "public"}:
                raise ValueError("Unknown field")
            policy = verify_policy(data.get("public"))
            raw = base64.b64decode(data["archive"], validate=True)
            if len(raw) > BundleLimits().max_archive_bytes:
                raise HTTPException(413, "Archive exceeds size limit")
            trust = TrustBundle.model_validate(data["trust"])
            sequence = data.get("sequence") or None
            if sequence is not None and (not isinstance(sequence, str) or len(sequence) > 100_000):
                raise ValueError("Invalid sequence")
            with tempfile.TemporaryDirectory(prefix="vat-evidence-") as root:
                path = Path(root) / "bundle.zip"
                path.write_bytes(raw)
                bundle = Bundle.load(path)
            size = sum(len(value) for value in bundle.blobs.values())
            if len(entries) >= 8 or retained_bytes + size > 256 * 1024**2:
                raise HTTPException(413, "Evidence workspace byte limit reached")
            projection = project(bundle, trust, sequence, policy)
        except (ValueError, TypeError, KeyError, BadZipFile, binascii.Error) as exc:
            raise HTTPException(422, "Malformed or unsupported evidence") from exc
        identifier = str(uuid4())
        entries[identifier] = (bundle, trust, sequence, policy)
        retained_bytes += size
        return {"id": identifier, "graph": projection}

    @router.delete("/{identifier}")
    async def remove(identifier: UUID):
        nonlocal retained_bytes
        bundle, _, _, _ = get(identifier)
        retained_bytes -= sum(len(value) for value in bundle.blobs.values())
        del entries[str(identifier)]
        return {"removed": True}

    @router.get("/{identifier}")
    async def original(identifier: UUID):
        bundle, trust, sequence, policy = get(identifier)
        return {"id": str(identifier), "graph": project(bundle, trust, sequence, policy)}

    @router.get("/{identifier}/bundle")
    async def archive(identifier: UUID):
        bundle, _, _, _ = get(identifier)
        with tempfile.TemporaryDirectory(prefix="vat-evidence-export-") as root:
            path = Path(root) / "bundle.zip"
            bundle.save(path)
            raw = path.read_bytes()
        return Response(raw, media_type="application/zip")

    @router.get("/{identifier}/trust")
    async def trust_document(identifier: UUID):
        _, trust, _, _ = get(identifier)
        return Response(trust.model_dump_json(), media_type="application/json")

    @router.post("/{identifier}/experiment")
    async def experiment(identifier: UUID, request: Request):
        bundle, trust, sequence, policy = get(identifier)
        action = await body(request, 32 * 1024)
        try:
            changed, description = falsify(bundle, action)
            return {"id": str(identifier), "graph": project(changed, trust, sequence, policy),
                    "action": action, "description": description}
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(422, "Action cannot be applied to this disclosed evidence") from exc

    @router.post("/{identifier}/download")
    async def download(identifier: UUID, request: Request):
        bundle, _, _, _ = get(identifier)
        action = await body(request, 32 * 1024)
        try:
            changed = falsify(bundle, action)[0] if action else bundle
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(422, "Invalid experiment") from exc
        root = tempfile.TemporaryDirectory(prefix="vat-evidence-download-")
        path = Path(root.name) / "bundle.zip"
        try:
            changed.save(path)
        except OSError:
            root.cleanup()
            raise
        return FileResponse(path, filename="evidence.zip", background=BackgroundTask(root.cleanup))

    @router.post("/{identifier}/artifact")
    async def artifact(identifier: UUID, request: Request):
        bundle, _, _, _ = get(identifier)
        data = await body(request, 32 * 1024)
        try:
            changed = falsify(bundle, data["action"])[0] if data.get("action") else bundle
            digest = data.get("digest")
            if not isinstance(digest, str):
                raise ValueError("Invalid digest")
            references = [a for r in changed.records for a in [*r.inputs, *r.results, *r.intermediates] if a.sha256 == digest]
            if not references or digest not in changed.blobs:
                raise HTTPException(404, "Artifact bytes are not disclosed")
            raw = changed.blobs[digest]
            try:
                text = raw[:64 * 1024].decode('utf-8')
            except UnicodeDecodeError:
                text = None
            return {"digest": digest, "bytes_len": len(raw), "truncated": len(raw) > 64 * 1024,
                    "integrity": "passed" if sha256_hex(raw) == digest and all(a.bytes_len == len(raw) for a in references) else "failed",
                    "text": text}
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(422, "Invalid artifact request") from exc

    return router
