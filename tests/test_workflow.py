"""Unpaid provider transport tests plus real signing/download/API regressions."""

import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from runner.api import create_app as real_create_app
from runner.provider import OpenRouter, ProviderError
from vatlog.bundle import Bundle
from vatlog.roles import TrustBundle
from vatlog.verify import verify_bundle


class EmptyResearch:
    """Explicit offline tool context for single-response API regressions."""
    tools = []
    def __init__(self, log):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass


def create_app(router):
    return real_create_app(router, tools_factory=EmptyResearch)


def completion(**changes):
    data = {"model": "openai/returned", "choices": [{"finish_reason": "stop", "message": {"content": "Analysis <script>untrusted</script>"}}]}
    data.update(changes)
    return data


def provider(handler):
    return OpenRouter("secret-key", httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_api_root_redirects_to_frontend_without_contacting_provider():
    def unexpected(request):
        pytest.fail("Landing page must not contact OpenRouter")
    with TestClient(create_app(provider(unexpected)), base_url="http://127.0.0.1:8000") as client:
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == "http://127.0.0.1:5173/"
        assert response.headers["cache-control"] == "no-store"
        assert client.get("/", headers={"Origin": "https://untrusted.example"}, follow_redirects=False).status_code == 403


def test_success_captures_actual_bytes_and_verifies_downloads(tmp_path):
    calls = []
    def upstream(request):
        calls.append(request)
        return httpx.Response(200, json=completion())
    with TestClient(create_app(provider(upstream)), base_url="http://127.0.0.1:8000") as client:
        id = str(uuid4())
        payload = {"id": id, "prompt": "Actual pasted prompt π", "model": "openai/selected"}
        assert client.post("/api/workflows", json=payload).status_code == 200
        # The mock transport has no awaits, so completion runs before the next ASGI request.
        snapshot = client.get(f"/api/workflows/{id}").json()
        assert snapshot["status"] == "completed"
        assert len(calls) == 1
        assert calls[0].headers["authorization"] == "Bearer secret-key"
        assert client.post("/api/workflows", json=payload).json()["id"] == id
        assert len(calls) == 1
        conflict = {**payload, "prompt": "another prompt"}
        assert client.post("/api/workflows", json=conflict).status_code == 409
        downloads = {}
        for name in snapshot["downloads"]:
            response = client.get(f"/api/workflows/{id}/files/{name}")
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            downloads[name] = response.content
        path = tmp_path / "bundle.zip"
        path.write_bytes(downloads["bundle.zip"])
        bundle = Bundle.load(path)
        trust = TrustBundle.model_validate_json(downloads["trust.json"])
        report = verify_bundle(bundle, trust)
        assert report.exit_code == 0
        assert report.tree_size == 2
        assert "no sequence" in report.checks[3].detail.lower()
        assert len({entry.keyid for entry in trust.entries}) == 4
        assert bundle.notary_receipt is not None
        artifacts = {a.name: bundle.blobs[a.sha256] for record in bundle.records
                     for a in record.inputs + record.results}
        assert artifacts["prompt.txt"] == payload["prompt"].encode()
        assert artifacts["request.json"] == calls[0].content
        assert json.loads(artifacts["response.json"]) == completion()
        assert "selected_model" in bundle.records[1].params.hyperparams
        assert b"secret-key" not in b"".join(downloads.values())
        assert client.get(f"/api/workflows/{id}/files/head.json").status_code == 404


@pytest.mark.parametrize("status", [401, 402, 429, 500])
def test_provider_errors_are_safe_not_retried_and_do_not_seal(status):
    calls = []
    def upstream(request):
        calls.append(request)
        return httpx.Response(status, text="secret-key private upstream error")
    with TestClient(create_app(provider(upstream)), base_url="http://localhost:8000") as client:
        id = str(uuid4())
        client.post("/api/workflows", json={"id": id, "prompt": "x", "model": "anthropic/model"})
        result = client.get(f"/api/workflows/{id}").json()
        assert result["status"] == "failed"
        assert result["downloads"] == []
        assert "secret-key" not in json.dumps(result)
        assert len(calls) == 1
        assert client.get(f"/api/workflows/{id}/files/bundle.zip").status_code == 404


@pytest.mark.parametrize("data", [
    {}, {"choices": []}, completion(model=None),
    completion(choices=[{"finish_reason": "stop", "message": None}]),
    completion(choices=[{"finish_reason": "length", "message": {"content": "partial"}}]),
    completion(choices=[{"finish_reason": "stop", "message": {"content": " "}}]),
    completion(choices=[{"finish_reason": "stop", "message": {"content": "x", "tool_calls": [{}]}}]),
    completion(error={"message": "secret-key"}),
])
def test_invalid_or_partial_provider_output_is_not_success(data):
    async def run():
        router = provider(lambda request: httpx.Response(200, json=data))
        with pytest.raises(ProviderError, match="invalid, incomplete"):
            await router.complete("x", "openai/x", lambda message: None)
        await router.client.aclose()
    asyncio.run(run())


def test_timeout_retries_three_times_and_can_succeed(monkeypatch):
    async def no_delay(seconds):
        pass
    monkeypatch.setattr("runner.provider.asyncio.sleep", no_delay)
    async def run():
        calls = []
        def upstream(request):
            calls.append(request)
            if len(calls) < 3:
                raise httpx.ReadTimeout("secret-key", request=request)
            return httpx.Response(200, json=completion())
        router = provider(upstream)
        logs = []
        result = await router.complete("x", "openai/x", logs.append)
        assert result[2] == completion()["choices"][0]["message"]["content"]
        assert len(calls) == 3
        assert sum("charges" in message for message in logs) == 2
        calls.clear()
        def timeout(request):
            calls.append(request)
            raise httpx.ReadTimeout("private", request=request)
        router = provider(timeout)
        with pytest.raises(ProviderError, match="three attempts"):
            await router.complete("x", "openai/x", logs.append)
        assert len(calls) == 3
    asyncio.run(run())


def test_cancel_interrupts_actual_pending_http_and_never_publishes_evidence():
    interrupted = []
    async def upstream(request):
        try:
            await asyncio.Event().wait()
        finally:
            interrupted.append(True)
    with TestClient(create_app(provider(upstream)), base_url="http://localhost:8000") as client:
        id = str(uuid4())
        client.post("/api/workflows", json={"id": id, "prompt": "x", "model": "openai/x"})
        result = client.post(f"/api/workflows/{id}/cancel").json()
        assert result["status"] == "cancelled"
        assert result["downloads"] == []
        assert interrupted == [True]
        assert client.post(f"/api/workflows/{id}/cancel").json()["status"] == "cancelled"


@pytest.mark.parametrize("payload", [
    {"id": "../bad", "prompt": "x", "model": "openai/x"},
    {"id": str(uuid4()), "prompt": " ", "model": "openai/x"},
    {"id": str(uuid4()), "prompt": "sensitive", "model": "other/x"},
    {"id": str(uuid4()), "prompt": "sensitive", "model": "openai/x", "key": "secret"},
])
def test_invalid_start_does_not_echo_input(payload):
    with TestClient(create_app(provider(lambda request: httpx.Response(200, json=completion()))), base_url="http://localhost:8000") as client:
        response = client.post("/api/workflows", json=payload)
        assert response.status_code == 422
        assert "sensitive" not in response.text and "secret" not in response.text


def test_loopback_origin_host_and_missing_jobs():
    router = provider(lambda request: httpx.Response(200, json={"data": [
        {"id": "anthropic/a", "name": "A"}, {"id": "openai/b"}, {"id": "other/c"},
        {"id": "openai/b:batch"}, {"id": "openai/audio", "architecture": {"output_modalities": ["audio"]}},
        {"id": "openai/no-tools", "supported_parameters": []}]}))
    with TestClient(create_app(router), base_url="http://localhost:8000") as client:
        models = client.get("/api/models").json()
        assert [item["id"] for item in models["models"]] == ["anthropic/a", "openai/b"]
        assert models["configured"] is True
        assert client.get("/api/models", headers={"Host": "evil.example"}).status_code == 403
        assert client.post("/api/workflows", json={}, headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.get(f"/api/workflows/{uuid4()}").status_code == 404


def test_missing_key_makes_no_billable_call():
    async def run():
        router = OpenRouter("", httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail("unexpected provider call"))))
        with pytest.raises(ProviderError, match="OPENROUTER_API_KEY"):
            await router.complete("x", "openai/x", lambda message: None)
        await router.client.aclose()
    asyncio.run(run())


def test_cancellation_during_timeout_backoff_prevents_retry():
    async def run():
        calls = []
        waiting = asyncio.Event()
        def upstream(request):
            calls.append(request)
            raise httpx.ReadTimeout("timeout", request=request)
        def log(message):
            if "retrying" in message:
                waiting.set()
        router = provider(upstream)
        task = asyncio.create_task(router.complete("x", "openai/x", log))
        await waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(calls) == 1
        await router.client.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("body", ["not json", '{"data": []}', '{"data": null}'])
def test_catalog_failures_are_safe_and_retryable(body):
    with TestClient(create_app(provider(lambda request: httpx.Response(200, text=body))), base_url="http://localhost:8000") as client:
        response = client.get("/api/models")
        assert response.status_code == 503
        assert "Retry" in response.json()["detail"]


def test_evidence_io_failure_does_not_publish_success(monkeypatch):
    def failed_capture(*args):
        raise OSError("sensitive file path")
    monkeypatch.setattr("runner.api.capture", failed_capture)
    with TestClient(create_app(provider(lambda request: httpx.Response(200, json=completion()))), base_url="http://localhost:8000") as client:
        id = str(uuid4())
        client.post("/api/workflows", json={"id": id, "prompt": "x", "model": "openai/x"})
        snapshot = client.get(f"/api/workflows/{id}").json()
        assert snapshot["status"] == "failed"
        assert snapshot["downloads"] == []
        assert "sensitive" not in json.dumps(snapshot)
