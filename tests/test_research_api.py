"""End-to-end local API → OpenRouter loop → actual SDK transport → real evidence."""

import asyncio
import json
import threading
import time
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from runner.api import create_app
from runner.provider import OpenRouter
from runner.research import ResearchTools
from vatlog.bundle import Bundle
from vatlog.roles import TrustBundle
from vatlog.verify import verify_bundle
from test_research_agent import call, response
from test_research_mcp import mock_server


def factory(handler):
    @asynccontextmanager
    async def tools(log):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            async with ResearchTools(log, client=client) as research:
                yield research
    return tools


@pytest.mark.parametrize("tool_name", ["pubmed__search_articles", "europepmc__search_preprints", "biorxiv__get_preprint"])
def test_actual_api_tool_loop_downloads_verified_four_leaf_bundle(tmp_path, tool_name):
    from test_preprints import SEARCH, METADATA, DOI
    mcp_calls, model_calls = [], []
    arguments = json.dumps({"doi": DOI} if tool_name == "biorxiv__get_preprint" else {"query": "GFP"})
    def upstream(request):
        model_calls.append(json.loads(request.content))
        return httpx.Response(200, json=response([call(name=tool_name, arguments=arguments)]) if len(model_calls) == 1 else response())
    normal = mock_server(mcp_calls)
    direct_calls = []
    def research(request):
        if request.url.host in {"www.ebi.ac.uk", "api.biorxiv.org"}:
            direct_calls.append(request)
            return httpx.Response(200, json=METADATA if request.url.host == "api.biorxiv.org" else SEARCH)
        return normal(request)
    provider = OpenRouter("secret", httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    with TestClient(create_app(provider, tools_factory=factory(research)), base_url="http://localhost:8000") as client:
        id = str(uuid4())
        client.post("/api/workflows", json={"id": id, "prompt": "Research GFP", "model": "openai/test"})
        for _ in range(100):
            snapshot = client.get(f"/api/workflows/{id}").json()
            if snapshot["status"] != "running":
                break
            time.sleep(.01)
        assert snapshot["status"] == "completed"
        assert len(model_calls) == 2
        assert bool(direct_calls) == (tool_name != "pubmed__search_articles")
        assert any(body["method"] == "tools/call" for _, body in mcp_calls) == (tool_name == "pubmed__search_articles")
        assert any(tool_name in entry["message"] for entry in snapshot["logs"])
        tool_result = json.loads(model_calls[1]["messages"][-1]["content"])
        assert not tool_result["isError"]
        if direct_calls:
            import base64
            assert json.loads(base64.b64decode(tool_result["response_body_base64"])) == tool_result["structuredContent"]
        path = tmp_path / "bundle.zip"
        path.write_bytes(client.get(f"/api/workflows/{id}/files/bundle.zip").content)
        trust = TrustBundle.model_validate_json(client.get(f"/api/workflows/{id}/files/trust.json").content)
        report = verify_bundle(Bundle.load(path), trust)
        assert report.exit_code == 0 and report.tree_size == 4


def test_api_cancel_during_sdk_tool_call_closes_connections_without_success():
    entered = threading.Event()
    normal = mock_server([])
    async def mcp(request):
        if request.method == "POST" and json.loads(request.content).get("method") == "tools/call":
            entered.set()
            await asyncio.Event().wait()
        return normal(request)
    provider = OpenRouter("secret", httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=response([call()])))))
    with TestClient(create_app(provider, tools_factory=factory(mcp)), base_url="http://localhost:8000") as client:
        id = str(uuid4())
        client.post("/api/workflows", json={"id": id, "prompt": "Research GFP", "model": "openai/test"})
        assert entered.wait(timeout=5)
        snapshot = client.post(f"/api/workflows/{id}/cancel").json()
        assert snapshot["status"] == "cancelled"
        assert snapshot["downloads"] == []
        assert client.get(f"/api/workflows/{id}/files/bundle.zip").status_code == 404
