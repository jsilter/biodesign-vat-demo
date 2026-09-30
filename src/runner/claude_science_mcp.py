"""Stdio MCP connector for bounded VAT evidence and workflow routes.

Claude Science runs connectors sandboxed, with no network route to the loopback backend
and a seccomp filter that blocks AF_UNIX sockets, so no transport reaches a separate
process. The read-only evidence and demo routes keep no state outside one process, so the
connector serves those itself. The workflow routes own a durable journal, recorder keys and
Modal dispatch, which must stay with the backend; in-process they refuse rather than become
a second owner. Summaries returned by MCP are not evidence."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from urllib.parse import quote
from urllib.parse import urlsplit

import httpx
from mcp.server.fastmcp import FastMCP

DEFAULT_BACKEND = "http://127.0.0.1:8000"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_TEXT_BYTES = 1024 * 1024


def backend_url() -> str:
    value = os.environ.get("VAT_BACKEND_URL", DEFAULT_BACKEND).rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.username:
        raise RuntimeError("VAT_BACKEND_URL must name the local HTTP backend.")
    return value


_LOCAL_APP = None


def local_app():
    """The bounded evidence and workflow routes, served inside this process.

    Claude Science runs connectors under a seccomp filter that blocks AF_UNIX sockets
    inside a network namespace with no route to the host, so no transport reaches a
    separate backend. These routes hold no cross-process state, so serving them here
    gives the same answers from the same verifier.
    """
    global _LOCAL_APP
    if _LOCAL_APP is None:
        from fastapi import FastAPI, Request
        from fastapi.exceptions import RequestValidationError
        from fastapi.responses import JSONResponse

        from .design_inputs import DesignError
        from .evidence_api import evidence_routes
        from .workflow_api import workflow_routes

        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        app.include_router(evidence_routes())
        app.include_router(workflow_routes())

        class Unowned:
            """Refuse the routes that own the durable journal, keys and Modal dispatch."""

            def __getattr__(self, name: str):
                raise DesignError("TOOL_UNAVAILABLE",
                                  "Workflow routes need the loopback backend; the sandboxed "
                                  "connector serves evidence and demo routes only.")

        app.state.science = app.state.dynamic = app.state.design = Unowned()

        @app.exception_handler(RequestValidationError)
        async def validation_error(request: Request, exc: RequestValidationError):
            return JSONResponse({"detail": "Invalid workflow request."}, status_code=422)

        @app.exception_handler(DesignError)
        async def design_error(request: Request, exc: DesignError):
            status = (409 if exc.code == "REQUEST_CONFLICT"
                      else 404 if exc.code in {"WORKFLOW_NOT_FOUND", "ARTIFACT_NOT_FOUND"}
                      else 503 if exc.code == "TOOL_UNAVAILABLE" else 422)
            return JSONResponse({"code": exc.code, "detail": str(exc)}, status_code=status)

        _LOCAL_APP = app
    return _LOCAL_APP


def in_process() -> bool:
    """Whether to serve the routes here rather than call a separate backend.

    In-process is the default because the connector normally runs sandboxed, where no
    transport reaches a backend. Setting VAT_BACKEND_URL opts back into the loopback
    backend, which is what an unsandboxed run with paid Modal transports wants.
    """
    mode = os.environ.get("VAT_BACKEND_MODE", "").strip().lower()
    if mode in {"in-process", "backend"}:
        return mode == "in-process"
    return not os.environ.get("VAT_BACKEND_URL")


def client() -> httpx.Client:
    """One bounded route client that never inherits proxy credentials.

    Starlette's test client is its supported synchronous driver for an ASGI application;
    it is an httpx.Client subclass, so both modes share one calling convention.
    """
    if in_process():
        from starlette.testclient import TestClient

        return TestClient(local_app(), raise_server_exceptions=False)
    return httpx.Client(base_url=backend_url(), timeout=120, trust_env=False)


def http_errors() -> tuple[type[BaseException], ...]:
    """Transport error bases for both clients.

    Starlette's test client uses whichever httpx distribution is installed, which may be a
    different module object from the one imported here, so collect both bases rather than
    letting one client's errors escape the bounded message below.
    """
    bases = {httpx.HTTPError}
    try:
        from starlette import testclient

        bases.add(testclient.httpx.HTTPError)
    except (ImportError, AttributeError):
        pass
    return tuple(bases)


def route_error(path: str, response) -> str:
    """The route's own refusal, which says what to do, rather than a bare status line."""
    detail = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            detail = str(body.get("detail") or body.get("code") or "")
    except ValueError:
        detail = ""
    suffix = f": {detail}" if detail else ""
    return f"VAT route {path} failed with status {response.status_code}{suffix}"


def call(method: str, path: str, payload: dict | None = None) -> dict:
    """Call one bounded route, in this process or on the loopback backend."""
    try:
        with client() as session:
            response = session.request(method, path, json=payload)
            if len(response.content) > MAX_RESPONSE_BYTES:
                raise RuntimeError("Route response exceeds the MCP summary limit.")
            if response.is_error:
                raise RuntimeError(route_error(path, response))
            value = response.json()
            if not isinstance(value, dict):
                raise RuntimeError("Route returned an invalid response.")
            return value
    except (*http_errors(), ValueError) as exc:
        raise RuntimeError(f"VAT route request failed: {exc}") from exc


def read_bytes(path: str) -> bytes:
    """Read one integrity-checked route resource with a strict response cap."""
    try:
        with client() as session:
            with session.stream("GET", path) as response:
                response.raise_for_status()
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise RuntimeError("Route resource exceeds the MCP resource limit.")
                    chunks.append(chunk)
                return b"".join(chunks)
    except http_errors() as exc:
        raise RuntimeError(f"VAT route resource request failed: {exc}") from exc


def evidence_root() -> Path:
    """The one directory the connector may read evidence files from (default: working directory)."""
    return Path(os.environ.get("VAT_EVIDENCE_DIR") or os.getcwd()).resolve()


def read_local(path: str, limit: int) -> bytes:
    """Read one regular file under the evidence directory; never echo its path or contents."""
    root = evidence_root()
    candidate = (root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise RuntimeError("Evidence files must be regular files under the evidence directory.")
    if candidate.stat().st_size > limit:
        raise RuntimeError("Evidence file exceeds the connector size limit.")
    return candidate.read_bytes()


def write_local(path: str, data: bytes) -> Path:
    """Write one file under the evidence directory; the caller chooses the name, never the root."""
    root = evidence_root()
    candidate = (root / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    if not candidate.is_relative_to(root) or candidate == root or candidate.suffix.lower() != ".html":
        raise RuntimeError("Rendered pages must be .html files under the evidence directory.")
    if not candidate.parent.is_dir() or candidate.is_symlink() or (candidate.exists() and not candidate.is_file()):
        raise RuntimeError("The page's directory must exist and the target must be a regular file or absent.")
    candidate.write_bytes(data)
    return candidate


def parse_text_object(raw: bytes) -> dict:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError("Evidence document is not a JSON object.") from exc
    if not isinstance(value, dict):
        raise RuntimeError("Evidence document is not a JSON object.")
    return value


mcp = FastMCP(
    "VAT protein workflows",
    instructions=("Validate and dry-run before execution. Approval binds the exact digest; "
                  "paid work also requires explicit spend authorization. Returned summaries are not evidence."),
    max_request_body_size=12 * 1024 * 1024,
)


@mcp.tool()
def list_protein_tools() -> dict:
    """List registered tools with readiness, license and required assets."""
    return call("GET", "/api/science/catalog")


@mcp.tool()
def prepare_protein_target(structure: str, format: str, chain: str) -> dict:
    """Validate and preview one uploaded protein chain without dispatching remote work."""
    return call("POST", "/api/design/prepare", {"structure": structure, "format": format, "chain": chain})


@mcp.tool()
def list_demo_evidence() -> dict:
    """Generate and verify the clean, tampered and partial local VAT demonstrations."""
    return call("GET", "/api/science/demos")


@mcp.tool()
def inspect_demo_evidence(fixture: str) -> dict:
    """Return the deterministic verification graph for one local demonstration fixture."""
    return call("GET", f"/api/science/demos/{quote(fixture, safe='')}")


@mcp.tool()
def experiment_demo_evidence(fixture: str, action: dict) -> dict:
    """Falsify a copy of disclosed demo evidence and rerun deterministic verification."""
    return call("POST", f"/api/science/demos/{quote(fixture, safe='')}/experiment", action)


@mcp.tool()
def load_evidence(archive_path: str, trust_path: str, sequence: str | None = None,
                  sigstore_identity: str | None = None, sigstore_issuer: str | None = None,
                  sigstore_trust_root_path: str | None = None,
                  require_public_transparency: bool = False) -> dict:
    """Load any VAT bundle and its public trust file from the evidence directory, then verify it.

    Paths are relative to VAT_EVIDENCE_DIR (default: the working directory). Supply the
    expected Sigstore signing identity (and optionally a cached trusted root) to verify public
    Rekor evidence; without an expected identity a bundle carrying public evidence fails check 2.
    Returns an evidence id for inspect/experiment/remove plus the verification graph.
    """
    payload = {"archive": base64.b64encode(read_local(archive_path, MAX_ARCHIVE_BYTES)).decode(),
               "trust": parse_text_object(read_local(trust_path, MAX_TEXT_BYTES))}
    if sequence is not None:
        payload["sequence"] = sequence
    if sigstore_identity is not None or require_public_transparency or sigstore_trust_root_path is not None:
        public = {"require_public_transparency": require_public_transparency,
                  "sigstore_identity": sigstore_identity, "sigstore_issuer": sigstore_issuer}
        if sigstore_trust_root_path is not None:
            public["sigstore_trusted_root"] = read_local(sigstore_trust_root_path, MAX_TEXT_BYTES).decode("utf-8", "strict")
        payload["public"] = public
    return call("POST", "/api/evidence", payload)


@mcp.tool()
def inspect_evidence(evidence_id: str) -> dict:
    """Re-project the original loaded evidence under the same verifier policy."""
    return call("GET", f"/api/evidence/{quote(evidence_id, safe='')}")


@mcp.tool()
def experiment_evidence(evidence_id: str, action: dict) -> dict:
    """Falsify a copy of loaded evidence and rerun deterministic verification.

    Actions: narrative, withhold, parameter, artifact, result, remove_binding, redirect_binding
    (each with a disclosed leaf), plus strip_public_evidence, retarget_public_evidence and
    substitute_public_identity (value: email) for bundles carrying public Sigstore evidence.
    """
    return call("POST", f"/api/evidence/{quote(evidence_id, safe='')}/experiment", action)


@mcp.tool()
def read_evidence_artifact(evidence_id: str, digest: str, action: dict | None = None) -> dict:
    """Preview disclosed artifact bytes (UTF-8 text when decodable) with an integrity result."""
    payload: dict = {"digest": digest}
    if action:
        payload["action"] = action
    return call("POST", f"/api/evidence/{quote(evidence_id, safe='')}/artifact", payload)


@mcp.tool()
def render_evidence_page(evidence_id: str, output_path: str, title: str = "VAT evidence") -> dict:
    """Write loaded evidence as a self-contained HTML page under the evidence directory.

    The page is the web viewer's read-only half (verdicts, graph, object inspection) with the
    verifier's graph embedded; it fetches nothing, so it opens in Claude Science's artifact
    preview. Copy it into the workspace to show it. The verdicts are those computed at render
    time; the page verifies nothing itself. Needs `npm --prefix web run build:static` once.
    """
    graph = call("GET", f"/api/evidence/{quote(evidence_id, safe='')}")["graph"]
    return _write_page(graph, output_path, title)


@mcp.tool()
def render_demo_evidence_page(fixture: str, output_path: str, title: str | None = None) -> dict:
    """Write one local demonstration fixture (clean, tampered, partial) as a self-contained HTML page."""
    graph = call("GET", f"/api/science/demos/{quote(fixture, safe='')}")["graph"]
    return _write_page(graph, output_path, title or f"VAT demo evidence: {fixture}")


def _write_page(graph: dict, output_path: str, title: str) -> dict:
    from .evidence_html import render
    try:
        page = render(graph, title).encode("utf-8")
    except (FileNotFoundError, ValueError) as exc:
        raise RuntimeError(str(exc)) from exc
    written = write_local(output_path, page)
    return {"path": str(written), "bytes": len(page), "outcome": graph["bundle_report"]["outcome"]}


@mcp.tool()
def remove_evidence(evidence_id: str) -> dict:
    """Release loaded evidence from the bounded workspace."""
    return call("DELETE", f"/api/evidence/{quote(evidence_id, safe='')}")


@mcp.tool()
def dry_run_workflow(proposal: dict) -> dict:
    """Validate and project a vat-workflow/1 proposal without dispatch or network work."""
    return call("POST", "/api/science/workflows/dry-run", proposal)


@mcp.tool()
def propose_workflow(proposal: dict) -> dict:
    """Persist one immutable vat-workflow/1 proposal for owner review."""
    return call("POST", "/api/science/workflows", proposal)


@mcp.tool()
def approve_workflow(workflow_id: str, digest: str, spend_authorized: bool = False) -> dict:
    """Record owner approval of an exact digest and an explicit, separate spend decision."""
    return call("POST", f"/api/science/workflows/{workflow_id}/approve",
                {"digest": digest, "spend_authorized": spend_authorized})


@mcp.tool()
def start_workflow(workflow_id: str) -> dict:
    """Start a previously approved workflow; this can incur Modal charges."""
    return call("POST", f"/api/science/workflows/{workflow_id}/start")


@mcp.tool()
def workflow_status(workflow_id: str) -> dict:
    """Read durable node states and signed node identities."""
    return call("GET", f"/api/science/workflows/{workflow_id}")


@mcp.tool()
def cancel_workflow(workflow_id: str) -> dict:
    """Request cancellation while preserving completed and uncertain calls."""
    return call("POST", f"/api/science/workflows/{workflow_id}/cancel")


@mcp.tool()
def dry_run_dynamic_job(proposal: dict) -> dict:
    """Validate generated source and fixed CPU limits without executing it."""
    return call("POST", "/api/science/dynamic/dry-run", proposal)


@mcp.tool()
def propose_dynamic_job(proposal: dict) -> dict:
    """Persist exact vat-dynamic-job/1 source, input and resource settings for review."""
    return call("POST", "/api/science/dynamic", proposal)


@mcp.tool()
def approve_dynamic_job(job_id: str, digest: str, spend_authorized: bool = False) -> dict:
    """Approve the exact generated-job digest and separately record the spend decision."""
    return call("POST", f"/api/science/dynamic/{job_id}/approve",
                {"digest": digest, "spend_authorized": spend_authorized})


@mcp.tool()
def start_dynamic_job(job_id: str) -> dict:
    """Dispatch approved source through the fixed no-network CPU launcher."""
    return call("POST", f"/api/science/dynamic/{job_id}/start")


@mcp.tool()
def dynamic_job_status(job_id: str) -> dict:
    """Read durable generated-job state and the signed node identity."""
    return call("GET", f"/api/science/dynamic/{job_id}")


@mcp.tool()
def cancel_dynamic_job(job_id: str) -> dict:
    """Request cancellation without replaying an uncertain generated call."""
    return call("POST", f"/api/science/dynamic/{job_id}/cancel")


@mcp.resource("vat://workflow/{workflow_id}/artifact/{digest}/{name}", mime_type="application/octet-stream")
def workflow_artifact(workflow_id: str, digest: str, name: str) -> bytes:
    """Read registered evidence bytes after backend membership and digest checks."""
    return read_bytes(f"/api/science/workflows/{quote(workflow_id, safe='')}/artifacts/"
                      f"{quote(digest, safe='')}/{quote(name, safe='')}")


@mcp.resource("vat://workflow/{workflow_id}/node/{execution_id}", mime_type="application/json")
def workflow_node(workflow_id: str, execution_id: str) -> bytes:
    """Read canonical vat-node/1 bytes for one workflow execution."""
    return read_bytes(f"/api/science/workflows/{quote(workflow_id, safe='')}/nodes/"
                      f"{quote(execution_id, safe='')}")


@mcp.resource("vat://dynamic/{job_id}/artifact/{digest}/{name}", mime_type="application/octet-stream")
def dynamic_artifact(job_id: str, digest: str, name: str) -> bytes:
    """Read one generated-job artifact after membership and digest checks."""
    return read_bytes(f"/api/science/dynamic/{quote(job_id, safe='')}/artifacts/"
                      f"{quote(digest, safe='')}/{quote(name, safe='')}")


@mcp.resource("vat://dynamic/{job_id}/node", mime_type="application/json")
def dynamic_node(job_id: str) -> bytes:
    """Read canonical vat-node/1 bytes for a generated job."""
    return read_bytes(f"/api/science/dynamic/{quote(job_id, safe='')}/node")


@mcp.resource("vat://trust", mime_type="application/json")
def trust_bundle() -> bytes:
    """Read public verification keys; no private signing bytes are exposed."""
    return read_bytes("/api/science/trust")


@mcp.resource("vat://demo/{fixture}/bundle", mime_type="application/zip")
def demo_bundle(fixture: str) -> bytes:
    """Read the generated canonical demo bundle."""
    return read_bytes(f"/api/science/demos/{quote(fixture, safe='')}/bundle")


@mcp.resource("vat://demo/{fixture}/trust", mime_type="application/json")
def demo_trust(fixture: str) -> bytes:
    """Read the generated demo's public role trust."""
    return read_bytes(f"/api/science/demos/{quote(fixture, safe='')}/trust")


@mcp.resource("vat://evidence/{evidence_id}/bundle", mime_type="application/zip")
def evidence_bundle(evidence_id: str) -> bytes:
    """Read the loaded bundle's canonical archive bytes."""
    return read_bytes(f"/api/evidence/{quote(evidence_id, safe='')}/bundle")


@mcp.resource("vat://evidence/{evidence_id}/trust", mime_type="application/json")
def evidence_trust(evidence_id: str) -> bytes:
    """Read the public role trust supplied with the loaded bundle."""
    return read_bytes(f"/api/evidence/{quote(evidence_id, safe='')}/trust")


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
