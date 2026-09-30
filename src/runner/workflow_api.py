"""Bounded loopback routes for synthesized scientific workflows."""

from __future__ import annotations

from uuid import UUID
import tempfile
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, StrictBool, StrictStr

from .tool_catalog import public_catalog
from .workflow_contracts import WorkflowProposal
from .dynamic_contracts import DynamicProposal


class Approval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    digest: StrictStr
    spend_authorized: StrictBool = False


def workflow_routes() -> APIRouter:
    router = APIRouter(prefix="/api/science")
    demos = {}

    def get_demo(fixture: str):
        if fixture not in {"clean", "tampered", "partial"}:
            from .design_inputs import DesignError
            raise DesignError("WORKFLOW_NOT_FOUND", "Demonstration evidence not found.")
        if fixture not in demos:
            from demo.demo import prepare_fixture
            with tempfile.TemporaryDirectory(prefix="vat-science-demo-") as root:
                demos[fixture] = prepare_fixture(fixture, Path(root))
        return demos[fixture]

    @router.get("/catalog")
    async def catalog():
        return {"version": "protein-tool-catalog/1", "tools": public_catalog()}

    @router.get("/demos")
    async def demo_catalog():
        from runner.graph import project
        return {"demos": [{"fixture": fixture,
                            "outcome": project(*get_demo(fixture))["bundle_report"]["outcome"]}
                           for fixture in ("clean", "tampered", "partial")]}

    @router.get("/demos/{fixture}")
    async def demo_graph(fixture: str):
        from runner.graph import project
        return {"fixture": fixture, "graph": project(*get_demo(fixture))}

    @router.post("/demos/{fixture}/experiment")
    async def demo_experiment(fixture: str, action: dict):
        from runner.graph import falsify, project
        bundle, trust, sequence = get_demo(fixture)
        try:
            changed, description = falsify(bundle, action)
        except (ValueError, KeyError, TypeError) as exc:
            from .design_inputs import DesignError
            raise DesignError("INVALID_REQUEST", "Evidence experiment is invalid.") from exc
        return {"fixture": fixture, "action": action, "description": description,
                "graph": project(changed, trust, sequence)}

    @router.get("/demos/{fixture}/bundle")
    async def demo_bundle(fixture: str):
        bundle, _, _ = get_demo(fixture)
        with tempfile.TemporaryDirectory(prefix="vat-science-export-") as root:
            path = Path(root) / "bundle.zip"
            bundle.save(path)
            raw = path.read_bytes()
        return Response(raw, media_type="application/zip")

    @router.get("/demos/{fixture}/trust")
    async def demo_trust(fixture: str):
        _, trust, _ = get_demo(fixture)
        return Response(trust.model_dump_json(), media_type="application/json")

    @router.post("/workflows/dry-run")
    async def validate(data: WorkflowProposal, request: Request):
        return request.app.state.science.dry_run(data.model_dump(mode="json"))

    @router.post("/workflows")
    async def propose(data: WorkflowProposal, request: Request):
        return request.app.state.science.propose(data.model_dump(mode="json"))

    @router.post("/workflows/{id}/approve")
    async def approve(id: UUID, data: Approval, request: Request):
        return request.app.state.science.approve(str(id), data.digest,
                                                 spend_authorized=data.spend_authorized)

    @router.post("/workflows/{id}/start")
    async def start(id: UUID, request: Request):
        return request.app.state.science.start(str(id))

    @router.get("/workflows/{id}")
    async def status(id: UUID, request: Request):
        return request.app.state.science.snapshot(str(id))

    @router.post("/workflows/{id}/cancel")
    async def cancel(id: UUID, request: Request):
        return await request.app.state.science.cancel(str(id))

    @router.get("/workflows/{id}/artifacts/{digest}/{name}")
    async def artifact(id: UUID, digest: str, name: str, request: Request):
        raw = request.app.state.science.store.read(str(id), digest, name)
        return Response(raw, media_type="application/octet-stream")

    @router.get("/workflows/{id}/nodes/{execution_id}")
    async def node(id: UUID, execution_id: UUID, request: Request):
        raw, _ = request.app.state.science.store.get_synthesized(str(id))
        if str(execution_id) not in {item["execution_id"] for item in raw["nodes"]}:
            from .design_inputs import DesignError
            raise DesignError("ARTIFACT_NOT_FOUND", "Execution node is not part of this workflow.")
        result = request.app.state.science.transport.result(str(execution_id))
        if result is None:
            from .design_inputs import DesignError
            raise DesignError("ARTIFACT_NOT_FOUND", "Execution node is unavailable.")
        return Response(result.node.canonical_bytes(), media_type="application/json")

    @router.get("/trust")
    async def trust(request: Request):
        return Response(request.app.state.science.store.keys().trust_bundle().model_dump_json(),
                        media_type="application/json")

    @router.post("/dynamic/dry-run")
    async def dynamic_validate(data: DynamicProposal, request: Request):
        return request.app.state.dynamic.dry_run(data.model_dump(mode="json"))

    @router.post("/dynamic")
    async def dynamic_propose(data: DynamicProposal, request: Request):
        return request.app.state.dynamic.propose(data.model_dump(mode="json"))

    @router.post("/dynamic/{id}/approve")
    async def dynamic_approve(id: UUID, data: Approval, request: Request):
        return request.app.state.dynamic.approve(str(id), data.digest,
                                                 spend_authorized=data.spend_authorized)

    @router.post("/dynamic/{id}/start")
    async def dynamic_start(id: UUID, request: Request):
        return request.app.state.dynamic.start(str(id))

    @router.get("/dynamic/{id}")
    async def dynamic_status(id: UUID, request: Request):
        return request.app.state.dynamic.snapshot(str(id))

    @router.post("/dynamic/{id}/cancel")
    async def dynamic_cancel(id: UUID, request: Request):
        return await request.app.state.dynamic.cancel(str(id))

    @router.get("/dynamic/{id}/artifacts/{digest}/{name}")
    async def dynamic_artifact(id: UUID, digest: str, name: str, request: Request):
        raw = request.app.state.dynamic.store.read(str(id), digest, name)
        return Response(raw, media_type="application/octet-stream")

    @router.get("/dynamic/{id}/node")
    async def dynamic_node(id: UUID, request: Request):
        result = request.app.state.dynamic.transport.result(str(id))
        if result is None:
            from .design_inputs import DesignError
            raise DesignError("ARTIFACT_NOT_FOUND", "Generated execution node is unavailable.")
        return Response(result.node.canonical_bytes(), media_type="application/json")

    return router
