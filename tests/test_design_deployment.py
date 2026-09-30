"""Exercise the local deployment boundary without contacting Modal."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def script():
    path = Path(__file__).resolve().parents[1] / "scripts/deploy_design_tools.py"
    spec = importlib.util.spec_from_file_location("deploy_design_tools", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_deployment_requires_confirmation_before_reading_secrets_or_starting_work(monkeypatch):
    module = script()
    monkeypatch.setattr("sys.argv", ["deploy_design_tools.py"])
    monkeypatch.setattr(module, "load_dotenv", lambda *a, **kw: pytest.fail("Read secrets before confirmation"))
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw: pytest.fail("Unconfirmed remote deployment"))
    with pytest.raises(SystemExit) as exc:
        module.main()
    assert exc.value.code == 2


def test_deployment_uses_module_mode_and_literal_env_without_credential_arguments(monkeypatch):
    module = script()
    monkeypatch.setattr("sys.argv", ["deploy_design_tools.py", "--confirm-deployment"])
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "sentinel-modal-secret")
    loaded, spawned = [], []
    monkeypatch.setattr(module, "load_dotenv", lambda *a, **kw: loaded.append((a, kw)))
    def spawn(command, **kwargs):
        spawned.append((command, kwargs))
        return SimpleNamespace(returncode=7)
    monkeypatch.setattr(module.subprocess, "run", spawn)
    assert module.main() == 7
    assert loaded[0][1] == {"override": False, "interpolate": False}
    command, options = spawned[0]
    assert command[1:] == ["-m", "modal", "deploy", "-m", "runner.modal_app"]
    assert "sentinel-modal-secret" not in " ".join(command)
    assert options["env"]["MODAL_TOKEN_SECRET"] == "sentinel-modal-secret"
    assert options["env"]["PYTHONPATH"] == str(options["cwd"] / "src")
