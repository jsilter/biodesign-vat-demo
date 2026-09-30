"""Loopback API and thin MCP contract for synthesized workflows."""

import asyncio
from uuid import uuid4

import httpx

from runner.api import create_app
from runner.claude_science_mcp import mcp
from runner.provider import OpenRouter
from runner.tool_contracts import tool_request
from test_workflow_service import LocalTransport


def proposal():
    return {"version": "vat-workflow/1", "id": str(uuid4()), "label": "API test",
            "purpose": "Verify bounded loopback execution.", "nodes": [{
        "id": "score", "tool_id": "esmc", "execution_id": str(uuid4()),
        "request": tool_request("esmc", {"sequence": "AC", "task": "score", "seed": 1}),
        "authorized": True,
    }]}


def test_catalog_dry_run_approval_execution_and_canonical_resources(tmp_path):
    async def exercise():
        provider = OpenRouter("", httpx.AsyncClient())
        app = create_app(provider, design_root=tmp_path,
                         workflow_transport=lambda store: LocalTransport(store))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://127.0.0.1:8000") as client:
                catalog = await client.get("/api/science/catalog")
                assert catalog.status_code == 200
                assert next(tool for tool in catalog.json()["tools"]
                            if tool["id"] == "boltz2")["implemented"] is True
                value = proposal()
                projected = await client.post("/api/science/workflows/dry-run", json=value)
                assert projected.status_code == 200 and projected.json()["dispatchable"] is True
                assert (await client.get(f'/api/science/workflows/{value["id"]}')).status_code == 404
                state = (await client.post("/api/science/workflows", json=value)).json()
                refused = await client.post(f'/api/science/workflows/{value["id"]}/approve', json={
                    "digest": "0" * 64, "spend_authorized": True})
                assert refused.status_code == 422 and refused.json()["code"] == "APPROVAL_MISMATCH"
                approved = await client.post(f'/api/science/workflows/{value["id"]}/approve', json={
                    "digest": state["digest"], "spend_authorized": True})
                assert approved.status_code == 200
                assert (await client.post(f'/api/science/workflows/{value["id"]}/start')).status_code == 200
                for _ in range(100):
                    terminal = (await client.get(f'/api/science/workflows/{value["id"]}')).json()
                    if terminal["status"] != "running":
                        break
                    await asyncio.sleep(0.01)
                assert terminal["status"] == "completed"
                execution_id = value["nodes"][0]["execution_id"]
                node = await client.get(f'/api/science/workflows/{value["id"]}/nodes/{execution_id}')
                assert node.status_code == 200 and node.content.startswith(b'{"blobs"')
                trust = await client.get("/api/science/trust")
                assert trust.status_code == 200 and "private" not in trust.text
                evidence = next(item for item in app.state.science.store.artifacts(value["id"])
                                if item["name"] == "bundle.zip")
                artifact = await client.get(f'/api/science/workflows/{value["id"]}/artifacts/'
                                            f'{evidence["sha256"]}/{evidence["name"]}')
                assert artifact.status_code == 200 and artifact.content.startswith(b"PK")
        await provider.client.aclose()
    asyncio.run(exercise())


def test_mcp_declares_only_bounded_workflow_tools_and_resource_templates():
    tools = asyncio.run(mcp.list_tools())
    assert [tool.name for tool in tools] == [
        "list_protein_tools", "prepare_protein_target", "list_demo_evidence",
        "inspect_demo_evidence", "experiment_demo_evidence",
        "load_evidence", "inspect_evidence", "experiment_evidence", "read_evidence_artifact",
        "render_evidence_page", "render_demo_evidence_page", "remove_evidence",
        "dry_run_workflow", "propose_workflow", "approve_workflow",
        "start_workflow", "workflow_status", "cancel_workflow", "dry_run_dynamic_job",
        "propose_dynamic_job", "approve_dynamic_job", "start_dynamic_job",
        "dynamic_job_status", "cancel_dynamic_job"]
    templates = asyncio.run(mcp.list_resource_templates())
    assert {template.uriTemplate for template in templates} == {
        "vat://workflow/{workflow_id}/artifact/{digest}/{name}",
        "vat://workflow/{workflow_id}/node/{execution_id}",
        "vat://dynamic/{job_id}/artifact/{digest}/{name}",
        "vat://dynamic/{job_id}/node",
        "vat://demo/{fixture}/bundle",
        "vat://demo/{fixture}/trust",
        "vat://evidence/{evidence_id}/bundle",
        "vat://evidence/{evidence_id}/trust",
    }
    resources = asyncio.run(mcp.list_resources())
    assert [str(resource.uri) for resource in resources] == ["vat://trust"]


def test_api_validation_redacts_rejected_payload_and_does_not_dispatch(tmp_path):
    async def exercise():
        provider = OpenRouter("", httpx.AsyncClient())
        app = create_app(provider, design_root=tmp_path,
                         workflow_transport=lambda store: LocalTransport(store))
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://localhost:8000") as client:
                response = await client.post("/api/science/workflows/dry-run", json={
                    "private": "sentinel-secret-that-must-not-return"})
                assert response.status_code == 422
                assert "sentinel-secret" not in response.text
        await provider.client.aclose()
    asyncio.run(exercise())


def test_demo_evidence_routes_report_actual_outcomes_and_falsify_a_copy(tmp_path):
    async def exercise():
        provider = OpenRouter("", httpx.AsyncClient())
        app = create_app(provider, design_root=tmp_path)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://127.0.0.1:8000") as client:
                catalog = (await client.get("/api/science/demos")).json()
                assert {item["fixture"]: item["outcome"] for item in catalog["demos"]} == {
                    "clean": "OK", "tampered": "TAMPERED", "partial": "INCOMPLETE"}
                clean = (await client.get("/api/science/demos/clean")).json()
                record = next(item for item in clean["graph"]["records"] if item["artifacts"])
                artifact = record["artifacts"][0]
                changed = await client.post("/api/science/demos/clean/experiment", json={
                    "kind": "artifact", "leaf": record["index"], "digest": artifact["sha256"]})
                assert changed.status_code == 200
                assert changed.json()["graph"]["bundle_report"]["outcome"] == "TAMPERED"
                bundle = await client.get("/api/science/demos/clean/bundle")
                trust = await client.get("/api/science/demos/clean/trust")
                assert bundle.content.startswith(b"PK") and "private" not in trust.text
        await provider.client.aclose()
    asyncio.run(exercise())
