"""Actual MCP SDK sessions over mocked HTTP; no live APIs or billable model calls."""

import asyncio
import json

import httpx
import pytest

from runner.research import ResearchTools, check_schema, result_failed


def mock_server(calls, *, fail_pubmed=False, result=None):
    def respond(request):
        if request.method == "GET":
            return httpx.Response(405)
        body = json.loads(request.content)
        server = "pubmed" if "pubmed" in request.url.host else "biorxiv"
        calls.append((server, body))
        assert "authorization" not in request.headers
        if "id" not in body:
            return httpx.Response(202)
        method = body["method"]
        if method == "initialize":
            if fail_pubmed and server == "pubmed":
                return httpx.Response(503)
            data = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                    "serverInfo": {"name": server, "version": "1"}}
        elif method == "tools/list":
            names = ["search_articles", "delete_everything"] if server == "pubmed" else ["get_preprint", "get_categories"]
            data = {"tools": [{"name": name, "description": "Read public metadata", "inputSchema":
                    {"type": "object", "additionalProperties": False,
                     "properties": {"query": {"type": "string"}} if name == "search_articles" else {},
                     "required": ["query"] if name == "search_articles" else []}} for name in names]}
        elif method == "tools/call":
            data = result or {"content": [{"type": "text", "text": "public result"}], "isError": False}
        else:
            pytest.fail(f"Unexpected SDK method {method}")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": data})
    return respond


def test_actual_sdk_discovery_call_and_allowlist_schema_rejection():
    async def run():
        calls = []
        async with httpx.AsyncClient(transport=httpx.MockTransport(mock_server(calls))) as client:
            async with ResearchTools(lambda message: None, client=client) as tools:
                assert {t["function"]["name"] for t in tools.tools} == {
                    "pubmed__search_articles", "biorxiv__get_preprint", "europepmc__search_preprints"}
                assert {server for server, _ in calls} == {"pubmed"}
                result = await tools.call("pubmed__search_articles", {"query": "GFP"})
                assert result["content"][0]["text"] == "public result"
                assert not result_failed(result)
                before = len(calls)
                assert result_failed(await tools.call("pubmed__delete_everything", {}))
                assert result_failed(await tools.call("pubmed__search_articles", {"query": 4}))
                assert result_failed(await tools.call("pubmed__search_articles", {"query": "x", "url": "http://localhost"}))
                assert len(calls) == before
                dispatched = [body for _, body in calls if body["method"] == "tools/call"]
                assert dispatched[0]["params"]["name"] == "search_articles"
    asyncio.run(run())


def test_one_server_unavailable_preserves_other_tools_and_logs_limitation():
    async def run():
        logs, calls = [], []
        async with httpx.AsyncClient(transport=httpx.MockTransport(mock_server(calls, fail_pubmed=True))) as client:
            async with ResearchTools(logs.append, client=client) as tools:
                assert not tools.routes
                assert {tool["function"]["name"] for tool in tools.tools} == {
                    "europepmc__search_preprints", "biorxiv__get_preprint"}
                assert any("pubmed MCP unavailable" in entry for entry in logs)
                assert result_failed(await tools.call("pubmed__search_articles", {"query": "GFP"}))
    asyncio.run(run())


@pytest.mark.parametrize("result", [
    {"isError": True}, {"structuredContent": {"success": False, "error": "No preprint found"}},
    {"content": [{"type": "text", "text": '{"success":false,"error":"No preprint"}'}]},
])
def test_service_failure_is_recognized_even_without_protocol_error(result):
    assert result_failed(result)


def test_plain_text_and_successful_payload_are_not_error_results():
    assert not result_failed({"content": [{"type": "text", "text": "non JSON result"}]})
    assert not result_failed({"structuredContent": {"success": True, "error": None}})


@pytest.mark.parametrize("reference", ["http://127.0.0.1/private", "https://example.com/schema", "file:///etc/passwd"])
def test_external_schema_references_are_rejected_before_validation(reference):
    from jsonschema.exceptions import SchemaError
    with pytest.raises(SchemaError, match="External schema"):
        check_schema({"type": "object", "properties": {"query": {"$ref": reference}}})


def test_local_schema_definitions_remain_supported():
    check_schema({"$defs": {"query": {"type": "string"}}, "type": "object",
                  "properties": {"query": {"$ref": "#/$defs/query"}}})
    check_schema({"type": "object", "properties": {"$ref": {"type": "string"}}})


def test_cancel_interrupts_pending_actual_sdk_tool_request():
    async def run():
        calls, entered = [], asyncio.Event()
        normal = mock_server(calls)
        async def respond(request):
            body = json.loads(request.content) if request.method == "POST" else {}
            if body.get("method") == "tools/call":
                entered.set()
                await asyncio.Event().wait()
            return normal(request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            async with ResearchTools(lambda message: None, client=client) as tools:
                task = asyncio.create_task(tools.call("pubmed__search_articles", {"query": "x"}))
                await entered.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
    asyncio.run(run())


def test_actual_sdk_tool_http_failure_returns_explicit_error():
    async def run():
        calls = []
        normal = mock_server(calls)
        def respond(request):
            if request.method == "POST" and json.loads(request.content).get("method") == "tools/call":
                return httpx.Response(503)
            return normal(request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            async with ResearchTools(lambda message: None, client=client) as tools:
                result = await tools.call("pubmed__search_articles", {"query": "GFP"})
                assert result_failed(result)
                assert "protocol failed" in result["error"]
    asyncio.run(run())


def test_tool_timeout_retries_but_cancellation_does_not(monkeypatch):
    from mcp.types import CallToolResult, TextContent
    async def no_delay(seconds):
        pass
    monkeypatch.setattr("runner.research.asyncio.sleep", no_delay)
    class Session:
        attempts = 0
        async def call_tool(self, name, arguments):
            self.attempts += 1
            if self.attempts < 3:
                raise TimeoutError("private upstream detail")
            return CallToolResult(content=[TextContent(type="text", text="result")])
    async def run():
        tools = ResearchTools(lambda message: None)
        session = Session()
        tools.routes["pubmed__search_articles"] = (session, "search_articles", {"type": "object"})
        assert not result_failed(await tools.call("pubmed__search_articles", {}))
        assert session.attempts == 3
    asyncio.run(run())
