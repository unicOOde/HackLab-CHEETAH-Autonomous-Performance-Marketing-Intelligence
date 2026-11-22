#!/usr/bin/env bash
# Starts the CHEETAH backend for local (non-Docker) development: the FastAPI API (uvicorn,
# --reload) and the worker that actually executes queued commands / advances simulations. Needs
# a Python 3.10+ venv at .venv-backend (see backend/README.md — app.py's `X | None` type hints
# require 3.10+; the repo's own .venv is pinned to 3.9 for cheetah_synth).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$REPO_ROOT/.venv-backend"

if [ ! -x "$VENV/bin/python" ]; then
  echo "No Python 3.10+ venv found at $VENV" >&2
  echo "Create one first:" >&2
  echo "  python3.10 -m venv .venv-backend && .venv-backend/bin/pip install -r backend/requirements.txt" >&2
  exit 1
fi

cd "$REPO_ROOT/backend"

cleanup() {
  echo
  echo "Stopping API and worker..."
  kill "$UVICORN_PID" "$WORKER_PID" 2>/dev/null || true
  wait "$UVICORN_PID" "$WORKER_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

"$VENV/bin/uvicorn" app:app --reload --port 8000 &
UVICORN_PID=$!

"$VENV/bin/python" worker.py &
WORKER_PID=$!

echo "API:    http://127.0.0.1:8000  (pid $UVICORN_PID)"
echo "Worker: running (pid $WORKER_PID)"
echo "Press Ctrl+C to stop both."

# Exit (and trigger cleanup) as soon as either process dies, so a crash in one doesn't leave
# the other running silently in the background. Polling instead of `wait -n` since that needs
# bash 4.3+ and macOS ships bash 3.2 by default.
while kill -0 "$UVICORN_PID" 2>/dev/null && kill -0 "$WORKER_PID" 2>/dev/null; do
  sleep 1
done
