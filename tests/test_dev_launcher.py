"""Exercise launcher lifecycle with real subprocesses, without starting paid workflows."""

import os
import runpy
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parents[1] / "scripts/dev.py"


@pytest.mark.parametrize("shutdown", [signal.SIGINT, signal.SIGTERM])
def test_shutdown_stops_both_owned_processes(tmp_path, shutdown):
    child = "import pathlib,os,time; pathlib.Path(__import__('sys').argv[1]).write_text(str(os.getpid())); time.sleep(60)"
    paths = [tmp_path / "first", tmp_path / "second"]
    commands = [[sys.executable, "-c", child, str(path)] for path in paths]
    code = f"import runpy,os; raise SystemExit(runpy.run_path({str(LAUNCHER)!r})['supervise']({commands!r}, {str(tmp_path)!r}, dict(os.environ)))"
    process = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    try:
        deadline = time.monotonic() + 10
        while not all(path.exists() for path in paths) and time.monotonic() < deadline:
            time.sleep(.02)
        assert all(path.exists() for path in paths)
        pids = [int(path.read_text()) for path in paths]
        assert all(os.getpgid(pid) == pid for pid in pids)
        process.send_signal(shutdown)
        assert process.wait(timeout=10) == 0
        for pid in pids:
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=10)


def test_server_exit_stops_peer_and_propagates_failure(tmp_path):
    supervise = runpy.run_path(str(LAUNCHER))["supervise"]
    before = time.monotonic()
    assert supervise([[sys.executable, "-c", "raise SystemExit(7)"],
                      [sys.executable, "-c", "import time; time.sleep(60)"]], tmp_path, dict(os.environ)) == 7
    assert time.monotonic() - before < 10


def test_startup_failure_cleans_up_already_started_peer(tmp_path):
    supervise = runpy.run_path(str(LAUNCHER))["supervise"]
    assert supervise([[sys.executable, "-c", "import time; time.sleep(60)"],
                      [str(tmp_path / "missing-command")]], tmp_path, dict(os.environ)) == 1


def test_dotenv_supports_quotes_comments_and_literal_values(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (tmp_path / ".env").write_text('export OPENROUTER_API_KEY="secret-${HOME}-$(not-executed)" # comment\nBARE_ENTRY\nEMPTY_ENTRY=\n', encoding="utf-8")
    env = runpy.run_path(str(LAUNCHER))["startup_environment"](tmp_path)
    assert env["OPENROUTER_API_KEY"] == 'secret-${HOME}-$(not-executed)'
    assert "BARE_ENTRY" not in env
    assert env["EMPTY_ENTRY"] == ""
    assert env["PYTHONPATH"] == str(tmp_path / "src")
    assert "OPENROUTER_API_KEY" not in os.environ
    # A real child receives the loaded key, without printing the secret.
    child = subprocess.run([sys.executable, "-c", "import os; assert os.environ['OPENROUTER_API_KEY'] == 'secret-${HOME}-$(not-executed)'"], env=env)
    assert child.returncode == 0


def test_existing_environment_overrides_dotenv_even_when_empty(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=file-value\nPYTHONPATH=wrong\n")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    env = runpy.run_path(str(LAUNCHER))["startup_environment"](tmp_path)
    assert env["OPENROUTER_API_KEY"] == ""
    assert env["PYTHONPATH"] == str(tmp_path / "src")


def test_missing_dotenv_preserves_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "existing")
    env = runpy.run_path(str(LAUNCHER))["startup_environment"](tmp_path)
    assert env["OPENROUTER_API_KEY"] == "existing"


def test_ports_prefer_flags_over_environment_over_defaults():
    ports = runpy.run_path(str(LAUNCHER))["ports"]
    assert ports([], {}) == {"VAT_API_PORT": "8000", "VAT_UI_PORT": "5173"}
    assert ports([], {"VAT_API_PORT": "8100", "VAT_UI_PORT": " 5273 "}) == {"VAT_API_PORT": "8100", "VAT_UI_PORT": "5273"}
    assert ports(["--api-port", "9000"], {"VAT_API_PORT": "8100"}) == {"VAT_API_PORT": "9000", "VAT_UI_PORT": "5173"}
    for argv, environ in (([], {"VAT_API_PORT": "eight"}), (["--ui-port", "0"], {}), (["--api-port", "70000"], {}),
                          (["--api-port", "5173"], {})):
        with pytest.raises(SystemExit):
            ports(argv, environ)


def test_api_reads_the_same_port_variables(monkeypatch):
    sys.path.insert(0, str(LAUNCHER.parents[1] / "src"))
    from runner import api
    monkeypatch.delenv("VAT_API_PORT", raising=False)
    monkeypatch.delenv("VAT_UI_PORT", raising=False)
    assert (api.api_port(), api.ui_port()) == (8000, 5173)
    monkeypatch.setenv("VAT_API_PORT", "8100")
    monkeypatch.setenv("VAT_UI_PORT", "5273")
    assert (api.api_port(), api.ui_port()) == (8100, 5273)
    monkeypatch.setenv("VAT_UI_PORT", "0")
    with pytest.raises(ValueError):
        api.ui_port()
