#!/bin/bash
# Launch claude-science with the locally built bubblewrap (~/.local/bin/bwrap, 0.11.0)
# ahead of the system one (/usr/bin/bwrap, 0.6.1, lacks --disable-userns), plus the local VAT API.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="${CLAUDE_SCIENCE_BIN:-$HOME/.local/share/claude-science/claude-science}"
PYTHON="$ROOT/.venv/bin/python"
CLAUDE_SCIENCE_PORT="${CLAUDE_SCIENCE_PORT:-8765}"
VAT_API_PORT="${VAT_API_PORT:-8000}"
VAT_UI_PORT="${VAT_UI_PORT:-5173}"
VAT_BACKEND_URL="http://127.0.0.1:$VAT_API_PORT"
VAT_UI_URL="http://127.0.0.1:$VAT_UI_PORT"
export PATH="$HOME/.local/bin:$PATH"
export PYTHONPATH="$ROOT/src"
export VAT_BACKEND_URL VAT_API_PORT VAT_UI_PORT
export VAT_EVIDENCE_DIR="$ROOT"

if [[ ! -x "$BIN" ]]; then
  echo "claude-science binary not found or not executable: $BIN" >&2
  exit 1
fi
if [[ ! -x "$PYTHON" ]]; then
  echo "repository Python not found or not executable: $PYTHON" >&2
  exit 1
fi
# Claude Science runs the connector sandboxed: no network route to this backend and a seccomp
# filter that blocks AF_UNIX sockets. The connector therefore serves the evidence routes itself,
# from its own interpreter inside the repository, and needs the verification dependencies there.
CONNECTOR_PYTHON="$ROOT/.claude-science-venv/bin/python"
if [[ ! -x "$CONNECTOR_PYTHON" ]]; then
  echo "warning: connector interpreter missing: $CONNECTOR_PYTHON" >&2
elif ! "$CONNECTOR_PYTHON" -c "import sigstore, mcp, fastapi" 2>/dev/null; then
  echo "warning: the connector interpreter lacks verification dependencies. Install them with:" >&2
  echo "  $CONNECTOR_PYTHON -m pip install -e '$ROOT[workflow,public-transparency]'" >&2
fi
if [[ ! -f "$ROOT/web/dist-static/static.html" ]]; then
  echo "warning: the rendered evidence page template is missing; render_evidence_page will refuse. Build it with:" >&2
  echo "  npm --prefix '$ROOT/web' run build:static" >&2
fi
if ! bwrap --disable-userns --ro-bind / / --unshare-user -- /bin/true 2>/dev/null; then
  echo "warning: $(command -v bwrap) ($(bwrap --version)) failed the --disable-userns probe; sandbox may be unavailable" >&2
fi
if ! command -v socat >/dev/null; then
  echo "socat is required for sandbox networking. Install it: sudo apt-get install -y socat" >&2
  exit 1
fi
if ! command -v curl >/dev/null; then
  echo "curl is required for the VAT backend health check." >&2
  exit 1
fi
if [[ ! -f "$ROOT/web/node_modules/vite/bin/vite.js" ]]; then
  echo "the evidence UI needs its dependencies. Install them with: npm --prefix '$ROOT/web' ci" >&2
  exit 1
fi
echo "using bwrap: $(command -v bwrap) ($(bwrap --version))" >&2

backend_ready() {
  curl --silent --show-error --fail --max-time 1 \
    "$VAT_BACKEND_URL/api/science/catalog" >/dev/null 2>&1
}

ui_ready() {
  curl --silent --fail --max-time 1 "$VAT_UI_URL/" >/dev/null 2>&1
}

BACKEND_PID=""
cleanup() {
  if [[ -n "$BACKEND_PID" ]] && kill -0 "$BACKEND_PID" 2>/dev/null; then
    kill "$BACKEND_PID"
    wait "$BACKEND_PID" 2>/dev/null || true
  fi
}

# scripts/dev.py starts the API and the Vite evidence UI together (VAT_API_PORT, VAT_UI_PORT;
# 8000 and 5173 by default) and stops both on exit; the UI is where the presenter opens the
# evidence graph with every panel live.
start_backend() {
  if backend_ready; then
    echo "using existing VAT backend: $VAT_BACKEND_URL" >&2
    if ui_ready; then
      echo "using existing evidence UI: $VAT_UI_URL" >&2
    else
      echo "starting evidence UI only: $VAT_UI_URL" >&2
      (
        cd "$ROOT/web"
        exec node "$ROOT/web/node_modules/vite/bin/vite.js" --host 127.0.0.1 --port "$VAT_UI_PORT"
      ) &
      BACKEND_PID=$!
      trap cleanup EXIT INT TERM
    fi
    return
  fi
  echo "starting VAT backend (Modal enabled) and evidence UI: $VAT_BACKEND_URL, $VAT_UI_URL" >&2
  (
    cd "$ROOT"
    exec env VAT_MODAL_ENABLED=1 "$PYTHON" scripts/dev.py
  ) &
  BACKEND_PID=$!
  trap cleanup EXIT INT TERM
  # dev.py regenerates the UI fixtures before it starts the servers, so allow it a minute.
  for _ in {1..600}; do
    if backend_ready; then
      return
    fi
    if ! kill -0 "$BACKEND_PID" 2>/dev/null; then
      wait "$BACKEND_PID" || true
      echo "scripts/dev.py exited; check whether port $VAT_API_PORT or $VAT_UI_PORT is already in use." >&2
      exit 1
    fi
    sleep 0.1
  done
  echo "VAT backend did not become ready within 60 seconds." >&2
  exit 1
}

# No args starts the complete demo environment. An explicit `serve` does the same while
# preserving additional Claude Science flags. Other commands address the existing daemon only.
if [[ $# -eq 0 ]]; then
  start_backend
  "$BIN" serve --port "$CLAUDE_SCIENCE_PORT"
elif [[ "$1" == "serve" ]]; then
  shift
  start_backend
  "$BIN" serve --port "$CLAUDE_SCIENCE_PORT" "$@"
else
  exec "$BIN" "$@"
fi
