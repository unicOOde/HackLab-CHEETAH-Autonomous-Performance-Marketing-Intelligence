#!/usr/bin/env bash
# Starts the CHEETAH backend for local (non-Docker) development: the FastAPI API (uvicorn,
# --reload) and the worker that actually executes queued commands / advances simulations. Needs
# a Python 3.10+ venv at .venv-backend (see backend/README.md — app.py's `X | None` type hints
# require 3.10+; .venv-data is pinned to 3.9 for cheetah_synth, .venv-agents is for the Neo4j
# graph/agents track).
#
# Once the API is reachable, this also auto-pushes the `full` synthetic run as a fresh
# live_events dataset and starts playback, so there's always a running demo without a manual
# push-to-backend call. A fresh --dataset-id is generated every time (timestamped) so repeated
# restarts never collide with a prior run. Set SKIP_AUTO_PUSH=1 to skip this and push manually.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$REPO_ROOT/.venv-backend"
PORT="${PORT:-3030}"
API_URL="http://127.0.0.1:$PORT"

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

"$VENV/bin/uvicorn" app:app --reload --port "$PORT" &
UVICORN_PID=$!

"$VENV/bin/python" worker.py &
WORKER_PID=$!

echo "API:    $API_URL  (pid $UVICORN_PID)"
echo "Worker: running (pid $WORKER_PID)"

if [ "${SKIP_AUTO_PUSH:-0}" != "1" ]; then
  DATA_VENV="$REPO_ROOT/.venv-data"
  TIMELINE="$REPO_ROOT/data/synthetic/full/raw/event_timeline.parquet"
  if [ ! -x "$DATA_VENV/bin/python" ] || [ ! -f "$TIMELINE" ]; then
    echo "Auto-push skipped: need $DATA_VENV and $TIMELINE" >&2
    echo "  (generate the latter with: PYTHONPATH=src .venv-data/bin/python -m cheetah_synth.cli generate --run-id full --scale 1.0 --days 31 --seed 42)" >&2
  else
    echo "Waiting for the API to come up..."
    for _ in $(seq 1 30); do
      curl -sf "$API_URL/api/v1/health" >/dev/null 2>&1 && break
      sleep 1
    done
    DATASET_ID="synth-full-live-$(date +%Y%m%d%H%M%S)"
    echo "Auto-pushing dataset_id=$DATASET_ID (live_events, full run) and starting playback..."
    if PYTHONPATH="$REPO_ROOT/src" "$DATA_VENV/bin/python" -m cheetah_synth.cli push-to-backend \
         --data-dir "$REPO_ROOT/data" --out-dir "$REPO_ROOT/data/synthetic" \
         --backend-url "$API_URL" --run-id full --dataset-id "$DATASET_ID" --ingestion-mode live_events --start; then
      echo "Auto-push complete — open $API_URL/"
    else
      echo "Auto-push failed (see above) — API/worker are still running; push manually if needed." >&2
    fi
  fi
fi

echo "Press Ctrl+C to stop both."

# Exit (and trigger cleanup) as soon as either process dies, so a crash in one doesn't leave
# the other running silently in the background. Polling instead of `wait -n` since that needs
# bash 4.3+ and macOS ships bash 3.2 by default.
while kill -0 "$UVICORN_PID" 2>/dev/null && kill -0 "$WORKER_PID" 2>/dev/null; do
  sleep 1
done
