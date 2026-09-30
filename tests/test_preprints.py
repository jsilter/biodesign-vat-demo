"""Real HTTP dispatch/validation over mock transport, without model charges."""

import asyncio
import base64

import httpx
import pytest

from runner.preprints import call_preprint
from runner.research import result_failed

SEARCH = {"hitCount": 1, "nextCursorMark": "next+cursor", "resultList": {
    "result": [{"source": "PPR", "title": "GFP nanobody", "doi": "10.1101/123"}]}}
DOI = "10.1101/2025.11.20.689494"
METADATA = {"messages": [{"status": "ok"}], "collection": [
    {"server": "bioRxiv", "doi": DOI, "title": "Version one", "version": "1"},
    {"server": "bioRxiv", "doi": DOI, "title": "Version two", "version": "2"}]}


def invoke(handler, name="europepmc__search_preprints", args=None):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await call_preprint(client, name, args if args is not None else {"query": "GFP nanobody"}, lambda _: None)
    return asyncio.run(run())


def test_keyword_request_cursor_and_exact_http_body_are_preserved():
    raw = b'{"hitCount":0, "resultList":{"result":[]}, "nextCursorMark":"*"}\n'
    def respond(request):
        assert request.method == "GET"
        assert request.url.host == "www.ebi.ac.uk"
        assert request.url.params["query"] == "(GFP & nanobody) AND SRC:PPR"
        assert request.url.params["cursorMark"] == "next+cursor"
        assert request.url.params["pageSize"] == "2"
        assert request.url.params["resultType"] == "core"
        assert "authorization" not in request.headers
        return httpx.Response(200, content=raw)
    result = invoke(respond, args={"query": "GFP & nanobody", "cursor": "next+cursor", "page_size": 2})
    assert not result_failed(result)
    assert base64.b64decode(result["response_body_base64"]) == raw
    assert result["structuredContent"]["hitCount"] == 0


def test_doi_lookup_returns_all_versions_without_fetching_links():
    requests = []
    def respond(request):
        requests.append(request)
        assert str(request.url) == f"https://api.biorxiv.org/details/biorxiv/{DOI}/na/json"
        assert "authorization" not in request.headers
        return httpx.Response(200, json=METADATA)
    result = invoke(respond, "biorxiv__get_preprint", {"doi": DOI})
    assert not result_failed(result)
    assert [r["version"] for r in result["structuredContent"]["collection"]] == ["1", "2"]
    assert len(requests) == 1


@pytest.mark.parametrize("name,args", [
    ("europepmc__search_preprints", {"query": " "}),
    ("europepmc__search_preprints", {"query": "GFP", "page_size": 101}),
    ("europepmc__search_preprints", {"query": "GFP", "url": "http://localhost"}),
    ("biorxiv__get_preprint", {"doi": "https://doi.org/" + DOI}),
    ("biorxiv__get_preprint", {"doi": "10.1101/../../secret"}),
    ("biorxiv__get_preprint", {"doi": DOI + "?x=1"}),
    ("biorxiv__get_preprint", {"doi": DOI + "\n"}),
])
def test_invalid_arguments_do_not_dispatch(name, args):
    def unexpected(request):
        pytest.fail("invalid arguments reached network")
    assert result_failed(invoke(unexpected, name, args))


@pytest.mark.parametrize("status,body", [(503, b"private upstream detail"), (302, b""),
    (200, b"not json"), (200, b'{"hitCount":0,"resultList":{"result":[{}]}}'),
    (200, b'{"hitCount":true,"resultList":{"result":[]}}'), (200, b'{"error":"private detail"}')])
def test_http_and_malformed_search_failures_are_explicit_and_body_is_audited(status, body):
    result = invoke(lambda request: httpx.Response(status, content=body))
    assert result_failed(result)
    assert base64.b64decode(result["response_body_base64"]) == body
    assert "private" not in result["error"]


@pytest.mark.parametrize("data", [{"messages": [{"status": "no posts found"}], "collection": []},
    {"messages": [{"status": "ok"}], "collection": []},
    {"messages": [{"status": "ok"}], "collection": [{"server": 5}]}])
def test_missing_doi_and_invalid_service_envelopes_are_errors(data):
    assert result_failed(invoke(lambda request: httpx.Response(200, json=data), "biorxiv__get_preprint", {"doi": DOI}))


def test_doi_response_must_match_requested_doi():
    assert result_failed(invoke(lambda request: httpx.Response(200, json=METADATA),
                               "biorxiv__get_preprint", {"doi": "10.1101/123"}))


@pytest.mark.parametrize("recover", [True, False])
def test_timeouts_retry_exactly_three_times(monkeypatch, recover):
    async def no_delay(_):
        pass
    monkeypatch.setattr("runner.preprints.asyncio.sleep", no_delay)
    requests = []
    def respond(request):
        requests.append(request)
        if not recover or len(requests) < 3:
            raise httpx.ReadTimeout("private detail", request=request)
        return httpx.Response(200, json=SEARCH)
    result = invoke(respond)
    assert len(requests) == 3
    assert result_failed(result) is not recover


def test_cancellation_interrupts_request_without_retry():
    async def run():
        entered, requests = asyncio.Event(), []
        async def respond(request):
            requests.append(request)
            entered.set()
            await asyncio.Event().wait()
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            task = asyncio.create_task(call_preprint(client, "europepmc__search_preprints", {"query": "GFP"}, lambda _: None))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert len(requests) == 1
    asyncio.run(run())
