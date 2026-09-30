"""Provider conversation contracts and cryptographic per-dispatch evidence."""

import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest

from runner.agent import analyze
from runner.evidence import capture
from runner.provider import OpenRouter, ProviderError
from vatlog.bundle import Bundle
from vatlog.roles import TrustBundle
from vatlog.verify import verify_bundle


class Tools:
    tools = [{"type": "function", "function": {"name": "pubmed__search_articles", "parameters": {"type": "object"}}}]
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"content": [{"type": "text", "text": "retrieved evidence"}]}
    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        return self.result


def call(id="call-1", arguments='{"query":"GFP"}', name="pubmed__search_articles"):
    return {"id": id, "type": "function", "function": {"name": name, "arguments": arguments}}


def response(calls=None, text="Evidence-based answer"):
    message = {"content": None if calls is not None else text}
    if calls is not None:
        message["tool_calls"] = calls
    return {"model": "anthropic/returned", "choices": [{"finish_reason": "tool_calls" if calls is not None else "stop", "message": message}]}


def test_model_tool_followup_and_real_evidence_preserve_each_event(tmp_path):
    async def run():
        requests = []
        def upstream(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json=response([call()]) if len(requests) == 1 else response())
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            tools = Tools()
            events, text, returned = await analyze(OpenRouter("private-key", client), tools, "Research GFP", "anthropic/selected", lambda message: None)
        assert tools.calls == [("pubmed__search_articles", {"query": "GFP"})]
        assert [event.name for event in events] == ["openrouter", "pubmed__search_articles", "openrouter"]
        assert requests[0]["tools"] == requests[1]["tools"] == tools.tools
        assert requests[1]["messages"][-1]["tool_call_id"] == "call-1"
        assert "retrieved evidence" in requests[1]["messages"][-1]["content"]
        assert requests[1]["messages"][-2]["tool_calls"] == [call()]
        directory = capture(tmp_path, "research", "Research GFP", events, text, datetime.now(timezone.utc))
        bundle = Bundle.load(directory / "bundle.zip")
        trust = TrustBundle.load(directory / "trust.json")
        report = verify_bundle(bundle, trust)
        assert report.exit_code == 0 and report.tree_size == 4
        assert report.leaf_counts["agent_call"] == 3
        for record, event in zip(bundle.records[1:], events):
            assert bundle.blobs[record.inputs[-1].sha256] == event.request
            assert bundle.blobs[record.results[0].sha256] == event.response
            assert record.timestamps.stage_start == event.started
            assert record.timestamps.stage_stop == event.stopped
        assert bundle.records[2].params.hyperparams["tool_call_id"] == "call-1"
        assert bundle.records[2].params.hyperparams["status"] == "ok"
        assert b"private-key" not in (directory / "bundle.zip").read_bytes()
        assert returned == "anthropic/returned"
    asyncio.run(run())


@pytest.mark.parametrize("calls", [
    [call(), call()], [call(arguments="not-json")], [call(arguments="[]")],
    [call(arguments='{"query":"a","query":"b"}')], [call(id="")],
    [{"id": "x", "type": "function", "function": {}}],
])
def test_malformed_tool_batch_fails_before_any_dispatch(calls):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response(calls)))) as client:
            tools = Tools()
            with pytest.raises(ProviderError, match="malformed or duplicate"):
                await analyze(OpenRouter("key", client), tools, "x", "openai/x", lambda message: None)
            assert tools.calls == []
    asyncio.run(run())


def test_tool_error_is_returned_to_model_and_included_in_sealed_evidence(tmp_path):
    async def run():
        requests = []
        def upstream(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json=response([call()]) if len(requests) == 1 else response(text="Source unavailable; cannot establish evidence."))
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            tools = Tools({"structuredContent": {"success": False, "error": "No preprint"}})
            events, text, _ = await analyze(OpenRouter("key", client), tools, "x", "openai/x", lambda message: None)
        assert events[1].metadata["status"] == "error"
        assert "No preprint" in requests[1]["messages"][-1]["content"]
        directory = capture(tmp_path, "errors", "x", events, text, events[0].started)
        bundle = Bundle.load(directory / "bundle.zip")
        assert verify_bundle(bundle, TrustBundle.load(directory / "trust.json")).exit_code == 0
        assert bundle.records[2].params.hyperparams["status"] == "error"
    asyncio.run(run())


def test_duplicate_call_id_across_turns_is_not_dispatched_twice():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response([call()])))) as client:
            tools = Tools()
            with pytest.raises(ProviderError, match="duplicate"):
                await analyze(OpenRouter("key", client), tools, "x", "openai/x", lambda message: None)
            assert len(tools.calls) == 1
    asyncio.run(run())
