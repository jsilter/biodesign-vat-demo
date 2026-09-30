"""Standalone operator CLI contracts without Modal or model calls."""

import importlib.util
from pathlib import Path

import pytest


def script():
    path = Path(__file__).resolve().parents[1] / "scripts/tool_smoke.py"
    spec = importlib.util.spec_from_file_location("tool_smoke", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_proteinmpnn_cli_prepares_explicit_chains_and_nonzero_seed(tmp_path, monkeypatch):
    from test_design_inputs import backbone

    module = script()
    structure = tmp_path / "backbone.cif"
    structure.write_bytes(backbone())
    observed = []
    async def smoke(tool, config, output):
        observed.append((tool, config, output))
        return 9
    monkeypatch.setattr(module, "smoke", smoke)
    monkeypatch.setattr(module, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr("sys.argv", ["tool_smoke.py", "proteinmpnn", "--backbone", str(structure),
                                    "--design-chain", "binder", "--count", "2", "--seed", "1",
                                    "--temperature", "0.2", "--output", str(tmp_path / "output"),
                                    "--confirm-paid-work"])
    assert module.main() == 9
    tool, config, output = observed[0]
    assert tool == "proteinmpnn" and output == tmp_path / "output"
    assert config["design_chains"] == ["B"]
    assert config["sequences"] == {"A": "AC", "B": "AC"}
    assert config["count"] == 2 and config["seed"] == 1 and config["sampling_temperature"] == .2
    assert "path" not in config and "command" not in config


def test_proteinmpnn_cli_refuses_nondeterministic_seed_before_remote_work(tmp_path, monkeypatch):
    module = script()
    monkeypatch.setattr(module, "smoke", lambda *args: pytest.fail("unvalidated remote work"))
    monkeypatch.setattr(module, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr("sys.argv", ["tool_smoke.py", "proteinmpnn", "--backbone", str(tmp_path / "x.cif"),
                                    "--design-chain", "A", "--output", str(tmp_path / "output"),
                                    "--confirm-paid-work"])
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 2
