"""Exercise the actual local API and manifest downloads using unpaid remote boundaries."""

import time
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from runner.api import create_app
from runner.provider import OpenRouter
from runner.tool_catalog import TOOLS
from test_design_workflow import Remote, payload


def test_target_preview_rejects_invalid_chemistry_before_any_dispatch(tmp_path):
    remote = Remote()
    provider = OpenRouter("", httpx.AsyncClient())
    with TestClient(create_app(provider, design_root=tmp_path, design_transport=remote), base_url="http://127.0.0.1:8000") as client:
        data = payload()
        result = client.post("/api/design/prepare", json={"structure": data.structure, "format": data.format, "chain": data.chain})
        assert result.status_code == 200
        assert result.json()["sequence"] == "AC"
        assert result.json()["mapping"][1]["index"] == 2
        result = client.post("/api/design/prepare", json={"structure": "private-invalid-structure", "format": "cif", "chain": "A"})
        assert result.status_code == 422
        assert "private-invalid-structure" not in result.text
        assert not remote.calls


def test_config_advertises_all_scientific_adapters_as_standalone(tmp_path):
    provider = OpenRouter("", httpx.AsyncClient())
    with TestClient(create_app(provider, design_root=tmp_path, design_transport=Remote()), base_url="http://127.0.0.1:8000") as client:
        config = client.get("/api/design/config").json()
        assert [tool["id"] for tool in config["tools"]] == [tool.id for tool in TOOLS]
        assert all(tool["standalone"] is True and tool["input_role"] for tool in config["tools"])
        assert next(tool for tool in config["tools"] if tool["id"] == "esmfold2")["readiness"] == "validated"
        assert next(tool for tool in config["tools"] if tool["id"] == "alphafold3")["configured"] is False


def test_live_download_manifest_checks_cross_workflow_membership_and_blob_integrity(tmp_path):
    remote = Remote()
    provider = OpenRouter("sentinel-key", httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
        httpx.Response(200, json={"model": "anthropic/test", "choices": [{"finish_reason": "stop",
            "message": {"content": "Computed results."}}]}))))
    with TestClient(create_app(provider, design_root=tmp_path, design_transport=remote), base_url="http://127.0.0.1:8000") as client:
        data = payload()
        result = client.post("/api/designs", json=data.model_dump(mode="json"))
        assert result.status_code == 200
        id = str(data.id)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = client.get(f"/api/designs/{id}").json()
            if state["status"] == "completed":
                break
            time.sleep(.01)
        assert state["status"] == "completed"
        ref = next(a for a in state["artifacts"] if a["kind"] == "aa_sequence")
        route = f'/api/designs/{id}/artifacts/{ref["sha256"]}/{ref["name"]}'
        result = client.get(route)
        assert result.status_code == 200 and result.content == b"A" * 60
        assert client.get(route.replace(id, str(uuid4()))).status_code == 404
        assert client.get(route.replace(ref["name"], "unregistered.txt")).status_code == 404
        (tmp_path / id / "blobs" / ref["sha256"]).write_bytes(b"C" + b"A" * 59)
        result = client.get(route)
        assert result.status_code == 422 and result.json()["code"] == "OUTPUT_INVALID"
        assert "sentinel-key" not in str(state)
