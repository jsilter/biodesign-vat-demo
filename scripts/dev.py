"""Start the local demo servers together; Ctrl+C stops their process groups."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def startup_environment(root: Path) -> dict[str, str]:
    """Read root .env literally; existing process variables override file values."""
    from dotenv import dotenv_values

    path = root / ".env"
    try:
        with path.open(encoding="utf-8") as stream:
            values = dotenv_values(stream=stream, interpolate=False)
    except FileNotFoundError:
        values = {}
    return {**{key: value for key, value in values.items() if value is not None},
            **os.environ, "PYTHONPATH": str(root / "src")}


def ports(argv: list[str] | None = None, environ: dict[str, str] | None = None) -> dict[str, str]:
    """Resolve the API and UI ports: flags first, then VAT_API_PORT / VAT_UI_PORT, then 8000 / 5173."""
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description="Start the VAT API and the evidence UI together.")
    parser.add_argument("--api-port", type=int, default=None, help="loopback port for the API (default: $VAT_API_PORT or 8000)")
    parser.add_argument("--ui-port", type=int, default=None, help="loopback port for the Vite UI (default: $VAT_UI_PORT or 5173)")
    args = parser.parse_args(argv)
    chosen = {}
    for flag, name, default in ((args.api_port, "VAT_API_PORT", 8000), (args.ui_port, "VAT_UI_PORT", 5173)):
        value = flag if flag is not None else (environ.get(name, "").strip() or default)
        try:
            value = int(value)
        except ValueError:
            parser.error(f"{name} must be a port number, not {value!r}")
        if not 1 <= value <= 65535:
            parser.error(f"{name} must be from 1 to 65535, not {value}")
        chosen[name] = str(value)
    if chosen["VAT_API_PORT"] == chosen["VAT_UI_PORT"]:
        parser.error("the API and the UI need different ports")
    return chosen


def supervise(commands: list[list[str]], cwd: Path, env: dict[str, str]) -> int:
    """Stop every child group on interruption, startup failure, or any child exit."""
    children: list[subprocess.Popen] = []
    previous = signal.getsignal(signal.SIGTERM)

    def terminate(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    try:
        for command in commands:
            children.append(subprocess.Popen(command, cwd=cwd, env=env, start_new_session=True))
        while True:
            for child in children:
                code = child.poll()
                if code is not None:
                    print("A demo server exited; stopping both servers.", flush=True)
                    return code if code > 0 else 1
            time.sleep(.1)
    except KeyboardInterrupt:
        print("\nStopping demo servers…", flush=True)
        return 0
    except OSError as exc:
        print(f"Could not start demo servers: {exc.strerror}", file=sys.stderr)
        return 1
    finally:
        # Ignore further interruptions while cleaning up every owned process group.
        old_int = signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        for child in children:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 5
        while any(child.poll() is None for child in children) and time.monotonic() < deadline:
            time.sleep(.05)
        for child in children:
            # Also remove grandchildren whose parent exited before shutdown.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, previous)


def main() -> int:
    """Use installed local dependencies, without installing packages or prompting for keys."""
    python = ROOT / ".venv/bin/python"
    vite = ROOT / "web/node_modules/vite/bin/vite.js"
    if not python.is_file() or not vite.is_file():
        print("Install dependencies first: .venv/bin/python -m pip install -e '.[dev,workflow]' and cd web && npm ci", file=sys.stderr)
        return 1
    try:
        env = {**startup_environment(ROOT), **ports()}
    except ImportError:
        print("Install workflow dependencies: .venv/bin/python -m pip install -e '.[dev,workflow]'", file=sys.stderr)
        return 1
    except (OSError, UnicodeError):
        print("Could not read repository .env; check permissions and UTF-8 encoding.", file=sys.stderr)
        return 1
    try:
        result = subprocess.run([str(python), "-m", "demo.export_ui"], cwd=ROOT, env=env)
    except KeyboardInterrupt:
        return 0
    if result.returncode:
        return result.returncode
    print(f"Demo: http://127.0.0.1:{env['VAT_UI_PORT']}/ (API on {env['VAT_API_PORT']}) · Ctrl+C stops both servers", flush=True)
    return supervise([[str(python), "-m", "runner.api"],
                      ["node", str(vite), "--host", "127.0.0.1", "--port", env["VAT_UI_PORT"]]], ROOT / "web", env)


if __name__ == "__main__":
    raise SystemExit(main())
