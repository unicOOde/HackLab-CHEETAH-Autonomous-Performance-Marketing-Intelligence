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

# Pre-flight: a leftover worker from an earlier run (orphaned by a closed terminal, kill -9 or
# Ctrl+Z) keeps the agent scheduler lock, and the new worker then runs without agents (all show
# "down"). Stop whatever still holds that lock or listens on our port before starting.
SCHED_LOCK="${CHEETAH_BUS_DIR:-$REPO_ROOT/var/agent_bus}/locks/scheduler.lock"
stop_stale() {
  local pids="$1" what="$2" pid
  [ -n "$pids" ] || return 0
  for pid in $pids; do
    echo "Stopping stale $what: pid $pid ($(ps -p "$pid" -o command= 2>/dev/null || echo '?'))"
    kill -CONT "$pid" 2>/dev/null || true   # a suspended (Ctrl+Z) process can't handle TERM
    kill "$pid" 2>/dev/null || true
  done
  for _ in 1 2 3 4 5; do
    local alive=""
    for pid in $pids; do kill -0 "$pid" 2>/dev/null && alive="$alive $pid"; done
    [ -n "$alive" ] || return 0
    sleep 1
  done
  kill -9 $alive 2>/dev/null || true
}
[ -f "$SCHED_LOCK" ] && stop_stale "$(lsof -t "$SCHED_LOCK" 2>/dev/null || true)" "agent scheduler lock holder"
stop_stale "$(lsof -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)" "process on port $PORT"

UVICORN_PID="" WORKER_PID=""
cleanup() {
  echo
  echo "Stopping API and worker..."
  kill $UVICORN_PID $WORKER_PID 2>/dev/null || true
  wait $UVICORN_PID $WORKER_PID 2>/dev/null || true
}
trap cleanup EXIT INT TERM HUP

"$VENV/bin/uvicorn" app:app --reload --port "$PORT" &
UVICORN_PID=$!

"$VENV/bin/python" worker.py &
WORKER_PID=$!

echo "API:    $API_URL  (pid $UVICORN_PID)"
echo "Worker: running (pid $WORKER_PID)"

# Post-start check: fail loudly now rather than show "down" agents on stage.
case "$(echo "${CHEETAH_AGENTS:-on}" | tr '[:upper:]' '[:lower:]')" in
  off|0|false|no) ;;
  *)
    echo "Waiting for the agent scheduler..."
    SCHED_OK=0
    for _ in $(seq 1 20); do
      # alive AND owned by this worker (a stale heartbeat from a just-killed one stays "alive" for 30 s)
      STATUS="$(curl -sf "$API_URL/api/v1/agents/status" 2>/dev/null || true)"
      if echo "$STATUS" | grep -q '"alive":true' && echo "$STATUS" | grep -q "\"pid\":$WORKER_PID[,}]"; then
        SCHED_OK=1; break
      fi
      sleep 1
    done
    if [ "$SCHED_OK" != "1" ]; then
      echo "" >&2
      echo "############################################################" >&2
      echo "#  AGENT SCHEDULER NOT RUNNING — agents would show 'down'.  #" >&2
      echo "#  See the worker output above for the reason (lock holder  #" >&2
      echo "#  pid). Stopping. CHEETAH_AGENTS=off skips this check.     #" >&2
      echo "############################################################" >&2
      exit 1
    fi
    echo "Agent scheduler: alive"
    ;;
esac

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
    # The API and worker load the repo-root .env themselves (backend/env.py). Default the demo run to
    # TabPFN when the backend reports its forecast service configured (token loaded + package
    # installed), otherwise to the naive placeholder band. FORECAST_PROVIDER=naive|tabpfn overrides.
    if [ -z "${FORECAST_PROVIDER:-}" ]; then
      if curl -sf "$API_URL/api/v1/health" | grep -q '"forecast_provider":"configured"'; then
        FORECAST_PROVIDER=tabpfn
      else
        FORECAST_PROVIDER=naive
        echo "TabPFN forecast service not configured (TABPFN_TOKEN in .env + backend/requirements-forecast.txt) — using naive references." >&2
      fi
    fi
    DATASET_ID="synth-full-live-$(date +%Y%m%d%H%M%S)"
    echo "Auto-pushing dataset_id=$DATASET_ID (live_events, full run, forecast=$FORECAST_PROVIDER) and starting playback..."
    if PYTHONPATH="$REPO_ROOT/src" "$DATA_VENV/bin/python" -m cheetah_synth.cli push-to-backend \
         --data-dir "$REPO_ROOT/data" --out-dir "$REPO_ROOT/data/synthetic" \
         --backend-url "$API_URL" --run-id full --dataset-id "$DATASET_ID" --ingestion-mode live_events \
         --forecast-provider "$FORECAST_PROVIDER" --start; then
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
