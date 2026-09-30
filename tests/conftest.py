"""Keep API tests away from real persistent state, credentials and paid deployments."""

import pytest


@pytest.fixture(autouse=True)
def isolated_workflow_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("VAT_WORKFLOW_DIR", str(tmp_path / "workflows"))
    monkeypatch.setenv("VAT_MODAL_ENABLED", "0")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
