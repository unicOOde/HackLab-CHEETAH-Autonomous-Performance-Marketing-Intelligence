# 01 · Operations: setup, run, test

How to get the system running. What each process does is in [00-overview](00-overview.md); this
page only covers environments, configuration and commands.

## Python environments

Three dependency sets, kept apart on purpose (the generator pins an older Python, the forecast
extra pulls in torch).

| Venv | Python | Install | Used by |
|---|---|---|---|
| `.venv-backend` | 3.10+ (3.12) | `pip install -r backend/requirements.txt -r requirements.txt` (+ `backend/requirements-forecast.txt` for TabPFN) | API, worker, **and the agents** (they run as threads inside the worker) |
| `.venv-data` | 3.9+ | numpy, pandas, openpyxl, pyarrow (`pyproject.toml`) | `cheetah_synth` generator and `push-to-backend` |
| `.venv` (named `.venv-agents` in `CLAUDE.md`) | 3.10+ | `pip install -r requirements.txt` (anthropic, neo4j, networkx) | standalone agent scripts: `run_agents.py`, `paid_sim.py`, `python -m agents.<agent>` |

`app.py` needs Python 3.10+ (`X | None` hints). The worker imports `agents/`, `graph_store.py` and
`llm.py` from the repo root at runtime, so `.venv-backend` needs the root `requirements.txt`
installed for the agents to be anything but `not_configured`.

## Configuration (`.env` at the repo root)

Template: [`.env.example`](../.env.example). Never commit `.env`. `backend/env.py` loads it into the
API and worker without overriding variables that are already set (`CHEETAH_SKIP_DOTENV=1` skips it,
used by tests).

| Variable | Needed for | Without it |
|---|---|---|
| `ANTHROPIC_API_KEY` | investigator, outage scout, reviewers, narratives | those agents are `not_configured`; LLM-dependent decisions are `deferred` / `unknown` |
| `CLAUDE_MODEL_SMALL` / `_MEDIUM` / `_LARGE` | overriding model tiers | defaults: Haiku / Sonnet / Opus 5.5 |
| `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`, `NEO4J_DATABASE` | the graph (Aura) | `graph_store.py` falls back to `neo4j-cli` credential `test` |
| `TABPFN_TOKEN` | TabPFN-TS forecasts | runs use the naive band; tabpfn runs emit `forecast.unavailable` |
| `DATABASE_URL` | Postgres | SQLite `backend/cheetah_dev.db` (single worker only) |
| `CORS_ORIGINS` | non-default UI origin | `http://localhost:8000,http://127.0.0.1:8000` |
| `CHEETAH_AGENTS=off` | disabling the agent scheduler | — |
| `CHEETAH_AGENT_INTERVAL_S`, `CHEETAH_AGENT_TIME_SCALE` | agent cadence (default 1800 s, scale 60 → 30 s) | — |
| `CHEETAH_SIM_START` / `CHEETAH_SIM_END` | simulated agent clock range | `2026-10-08T06:00`..`2026-10-14T23:00` |
| `CHEETAH_BUS_DIR` | bus location | `var/agent_bus/` |
| `FORECAST_PROVIDER`, `SKIP_AUTO_PUSH`, `PORT` | `start_backend.sh` behaviour | auto-detect, auto-push on, port 3030 |

## Running locally (recommended)

```bash
# one-time: synthetic data for the demo run
PYTHONPATH=src .venv-data/bin/python -m cheetah_synth.cli generate --run-id full --scale 1.0 --days 31 --seed 42

# API + worker (+ agents) + auto-pushed live_events demo run
./start_backend.sh            # UI at http://127.0.0.1:3030/
```

`start_backend.sh` starts uvicorn (`--reload`) and `worker.py`, waits for `/api/v1/health`, picks
`tabpfn` if the forecast service reports `configured` (else `naive`), pushes the `full` run under a
fresh timestamped dataset id, and starts playback. `SKIP_AUTO_PUSH=1` skips the push.

Manual push (always use a new dataset id):

```bash
PYTHONPATH=src .venv-data/bin/python -m cheetah_synth.cli push-to-backend --run-id full --dataset-id synth-full-v2 --ingestion-mode live_events --start
```

## Loading the graph (Track B)

```bash
.venv/bin/python paid_sim.py > paid_graph.cypher                     # paid telemetry + metric graph, all scenarios
neo4j-cli query --rw --credential <cred> < freecash_graph.cypher     # real-data entity graph
neo4j-cli query --rw --credential <cred> < paid_graph.cypher
```

Note: `freecash_graph.py` (which generates `freecash_graph.cypher` and which `paid_sim.py` imports)
is **not on `main`**; it is on the `agents-neo4j-graph` branch. The committed
`freecash_graph.cypher` can be loaded as is. See [40-graph](40-graph.md#status).

## Docker Compose

```bash
docker compose up --build     # Postgres 16 + Neo4j 5 + api + worker, UI at http://127.0.0.1:8000/
```

Needs `POSTGRES_PASSWORD` and `NEO4J_AUTH` in `.env`. Limits of the image (`Dockerfile`): it copies
only `backend/` and the UI, so the agents can't import and show `not_configured`, `data/synthetic/`
is absent (mount it for `live_events`), and the Compose Neo4j is separate from the Aura instance the
agents use.

## Tests

Run suites by path, not a bare `pytest tests/` (a missing backend dependency aborts collection of
everything).

| Suite | Command | Venv |
|---|---|---|
| Backend core, live mode, reports | `pytest tests/test_backend.py tests/test_backend_live.py tests/test_backend_reports.py` | `.venv-backend` |
| Forecast service | `pytest tests/test_backend_forecast.py` | `.venv-backend` |
| Agent runtime, dispatcher | `pytest tests/test_agent_runtime.py tests/test_dispatcher.py` | `.venv-backend` |
| Agents (PM, graph engineer, pacing watcher) | `pytest tests/test_compass_cartographer.py tests/test_pacing_watcher.py` | `.venv` or `.venv-backend`; no Neo4j or LLM needed. `test_pacing_watcher.py` imports `paid_sim.py`, which needs `freecash_graph.py` (not on `main`) |
| Synthetic generator | `PYTHONPATH=src python -m pytest tests/cheetah_synth/` | `.venv-data` |

Forecast backtest: `.venv-backend/bin/python backend/forecast_backtest.py` (needs `TABPFN_TOKEN`).

## Checking health

- `GET /api/v1/health`: forecast provider status. Its `agent_provider` / `graph_provider` fields are
  static `not_configured` placeholders; use the agent status endpoint for the real state.
- `GET /api/v1/agents/status`: each agent's state (`running`, `active`, `degraded`, `down`,
  `never_run`, `not_configured`, `disabled`), derived from heartbeat age, not trusted flags.
- `var/agent_bus/status/<agent>.json`: the raw heartbeat and last-run summary.
