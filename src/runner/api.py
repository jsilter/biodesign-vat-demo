"""Loopback-only demo workflow server. Run with ``python -m runner.api``."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, field_validator

from .evidence import FILES, capture
from .provider import OpenRouter, ProviderError
from .agent import analyze
from .research import ResearchTools
from .tool_catalog import public_catalog
from .design import DesignService, DesignStart
from .design_inputs import DesignError
from .design_store import DesignStore

DESIGN_TOOLS = public_catalog()



def _port(name: str, default: int) -> int:
    """A loopback port from the environment; the launcher sets both for every child."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    if not raw.isdigit() or not 1 <= int(raw) <= 65535:
        raise ValueError(f"{name} must be a port number from 1 to 65535, not {raw!r}")
    return int(raw)


def api_port() -> int:
    return _port("VAT_API_PORT", 8000)


def ui_port() -> int:
    return _port("VAT_UI_PORT", 5173)

class Start(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    prompt: str
    model: str

    @field_validator("prompt")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prompt must not be empty")
        return value

    @field_validator("model")
    @classmethod
    def supported(cls, value: str) -> str:
        if not value.startswith(("openai/", "anthropic/")) or any(c.isspace() for c in value):
            raise ValueError("select an OpenAI or Anthropic model")
        return value


class TargetUpload(BaseModel):
    """Preview the selected modeled protein chain before any billed submission."""
    model_config = ConfigDict(extra="forbid")
    structure: str
    format: str
    chain: str


@dataclass
class Job:
    request: Start
    status: str = "running"
    logs: list[dict[str, str]] = field(default_factory=list)
    text: str = ""
    returned_model: str = ""
    directory: Path | None = None
    task: asyncio.Task | None = None

    def log(self, message: str) -> None:
        self.logs.append({"at": datetime.now(timezone.utc).isoformat(), "message": message})
        self.logs[:] = self.logs[-100:]

    def snapshot(self) -> dict:
        return {"id": str(self.request.id), "status": self.status, "logs": self.logs,
                "text": self.text, "returned_model": self.returned_model,
                "downloads": list(FILES) if self.status == "completed" else []}


def create_app(provider: OpenRouter | None = None, *, tools_factory=ResearchTools,
               design_root: Path | None = None, design_transport=None,
               workflow_transport=None, dynamic_transport=None) -> FastAPI:
    """Create an isolated demo app; injectable HTTP transport allows unpaid boundary tests."""
    jobs: dict[str, Job] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with tempfile.TemporaryDirectory(prefix="vat-workflows-") as storage:
            app.state.root = Path(storage)
            async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
                app.state.provider = provider or OpenRouter(os.environ.get("OPENROUTER_API_KEY", ""), client)
                root = design_root or Path(os.environ.get("VAT_WORKFLOW_DIR", str(Path(__file__).resolve().parents[2] / ".local/workflows")))
                store = DesignStore(root)
                from .publication import PublicationStore
                from .workflow_service import WorkflowService
                transport = workflow_transport(store) if callable(workflow_transport) else workflow_transport
                app.state.design = DesignService(store, app.state.provider,
                                                 design_transport or transport)
                app.state.science = WorkflowService(store, transport)
                from .dynamic_service import DynamicJobService
                generated = dynamic_transport(store) if callable(dynamic_transport) else dynamic_transport
                app.state.dynamic = DynamicJobService(store, generated)
                monitor = None
                try:
                    with PublicationStore(store.root, store.keys()) as publication:
                        publication.recover()
                    # Recovery remains user-visible and unpaid until Modal is enabled.
                    if app.state.design.transport.configured:
                        monitor = asyncio.create_task(app.state.design.monitor())
                    else:
                        monitor = None
                    if app.state.science.transport.configured:
                        await app.state.science.reconcile()
                    if app.state.dynamic.transport.configured:
                        await app.state.dynamic.reconcile()
                    yield
                finally:
                    if monitor:
                        monitor.cancel()
                        await asyncio.gather(monitor, return_exceptions=True)
                    await app.state.design.shutdown()
                    await app.state.science.shutdown()
                    await app.state.dynamic.shutdown()
                    store.close()
                for job in jobs.values():
                    if job.task and not job.task.done():
                        job.task.cancel()
                await asyncio.gather(*(job.task for job in jobs.values() if job.task), return_exceptions=True)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    from .evidence_api import evidence_routes
    from .workflow_api import workflow_routes
    app.include_router(evidence_routes())
    app.include_router(workflow_routes())

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        try:
            host = urlsplit("//" + request.headers.get("host", "")).hostname
        except ValueError:
            return JSONResponse({"detail": "Invalid local host."}, status_code=403)
        origin = request.headers.get("origin")
        allowed = {f"http://{name}:{port}" for name in ("127.0.0.1", "localhost") for port in (ui_port(), api_port())}
        if host not in {"127.0.0.1", "localhost", "::1"} or (origin and origin not in allowed):
            return JSONResponse({"detail": "Local demo access only."}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        # FastAPI's default includes rejected input, which may contain sensitive prompts.
        return JSONResponse({"detail": "Invalid workflow request."}, status_code=422)

    @app.exception_handler(DesignError)
    async def design_error(request: Request, exc: DesignError):
        status = 409 if exc.code == "REQUEST_CONFLICT" else 404 if exc.code in {"WORKFLOW_NOT_FOUND", "ARTIFACT_NOT_FOUND"} else 503 if exc.code == "TOOL_UNAVAILABLE" else 422
        return JSONResponse({"code": exc.code, "detail": str(exc)}, status_code=status)

    @app.get("/api/design/config")
    async def design_config():
        return {"configured": app.state.design.transport.configured,
                "settings": {"max_candidates": 100, "max_target_residues": 2048,
                             "max_upload_bytes": 10*1024*1024, "timeout_minutes": 30},
                "tools": [{**tool, "configured": (app.state.design.transport.configured
                                                    and tool["implemented"]
                                                    and tool["readiness"] != "blocked")}
                          for tool in DESIGN_TOOLS]}

    @app.post("/api/designs")
    async def design_start(data: DesignStart):
        return app.state.design.start(data)

    @app.post("/api/design/prepare")
    async def design_prepare(data: TargetUpload):
        from .design_inputs import prepare_target
        target = prepare_target(data.structure.encode(), data.format, data.chain)
        return {k: v for k, v in target.items() if k != "prepared_cif"}

    @app.get("/api/designs/{id}")
    async def design_status(id: UUID):
        return app.state.design.snapshot(str(id))

    @app.post("/api/designs/{id}/cancel")
    async def design_cancel(id: UUID):
        return await app.state.design.cancel(str(id))

    @app.get("/api/designs/{id}/artifacts/{digest}/{name}")
    async def design_artifact(id: UUID, digest: str, name: str):
        from fastapi.responses import Response
        if name in FILES and app.state.design.snapshot(str(id)).get("evidence_status") != "verified":
            raise HTTPException(404, "Verified evidence unavailable.")
        raw = app.state.design.store.read(str(id), digest, name)
        # Restrict filename header to a server manifest identity; no raw upload names.
        return Response(raw, media_type="application/octet-stream")

    async def run(job: Job) -> None:
        started = datetime.now(timezone.utc)
        try:
            if not app.state.provider.key:
                raise ProviderError("Set OPENROUTER_API_KEY on the backend, then restart it.")
            job.log("Starting public research APIs and PubMed MCP. This research job runs no binder design or sequence validation.")
            async with tools_factory(job.log) as tools:
                events, text, returned = await analyze(app.state.provider, tools,
                    job.request.prompt, job.request.model, job.log)
            job.log("Research response received. Capturing and verifying model/tool evidence.")
            # No await during finalization: cancel cannot leave a half-published successful job.
            directory = capture(app.state.root, str(job.request.id), job.request.prompt, events, text, started,
                                keys=app.state.design.store.keys(), publication_root=app.state.design.store.root)
            job.directory, job.text, job.returned_model = directory, text, returned
            job.status = "completed"
            job.log("Research workflow complete; locally signed model/tool evidence verified. No binder design or biological screening run.")
        except asyncio.CancelledError:
            job.status = "cancelled"
            job.log("Cancelled. Provider work or charges already incurred cannot be reversed.")
        except ProviderError as exc:
            job.status = "failed"
            job.log(str(exc))
        except (OSError, ValueError) as exc:
            logging.getLogger(__name__).error("Workflow evidence finalization failed (%s)", type(exc).__name__)
            job.status = "failed"
            job.log("Evidence finalization failed. No successful evidence is available.")

    def get_job(id: UUID) -> Job:
        job = jobs.get(str(id))
        if job is None:
            raise HTTPException(404, "Workflow not found; server restart discards demo jobs.")
        return job

    def finished(job: Job, task: asyncio.Task) -> None:
        # Observe unexpected task failures so callers never poll a dead task as running.
        if not task.cancelled() and task.exception() is not None:
            logging.getLogger(__name__).error("Workflow task failed (%s)", type(task.exception()).__name__)
            job.status = "failed"
            job.log("Internal workflow failure. No successful evidence is available.")

    @app.get("/")
    async def frontend():
        """Send visitors to the demo UI, rather than an API-only 404 page."""
        return RedirectResponse(f"http://127.0.0.1:{ui_port()}/", status_code=307)

    @app.get("/api/models")
    async def models():
        try:
            return {"models": await app.state.provider.models(), "configured": bool(app.state.provider.key)}
        except ProviderError as exc:
            raise HTTPException(503, str(exc)) from exc

    @app.post("/api/workflows")
    async def start(data: Start):
        existing = jobs.get(str(data.id))
        if existing:
            if existing.request != data:
                raise HTTPException(409, "Workflow id already used for another request.")
            return existing.snapshot()
        job = Job(data)
        jobs[str(data.id)] = job
        job.task = asyncio.create_task(run(job))
        job.task.add_done_callback(lambda task: finished(job, task))
        return job.snapshot()

    @app.get("/api/workflows/{id}")
    async def status(id: UUID):
        return get_job(id).snapshot()

    @app.post("/api/workflows/{id}/cancel")
    async def cancel(id: UUID):
        job = get_job(id)
        if job.status == "running" and job.task:
            job.task.cancel()
            await asyncio.gather(job.task, return_exceptions=True)
            if job.status == "running":
                job.status = "cancelled"
                job.log("Cancelled before provider call started.")
        return job.snapshot()

    @app.get("/api/workflows/{id}/files/{name}")
    async def download(id: UUID, name: str):
        job = get_job(id)
        if name not in FILES or job.status != "completed" or job.directory is None:
            raise HTTPException(404, "Evidence unavailable.")
        return FileResponse(job.directory / name, filename=name)

    return app


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(create_app(), host="127.0.0.1", port=api_port())
