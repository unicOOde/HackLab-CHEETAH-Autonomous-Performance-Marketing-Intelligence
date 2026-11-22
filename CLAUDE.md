# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

CHEETAH is a fully agentic performance marketing intelligence system that autonomously forecasts
revenue, detects anomalies, investigates performance deviations, and delivers actionable insights
through coordinated AI agents. The current code is the **v1.5 integration foundation**: a runnable
backend/frontend skeleton with the simulation, event, and persistence plumbing in place, but with
every AI/graph/forecasting integration deliberately stubbed out pending credentials and real data
(see "Deliberately not implemented" below) — this is the system's own stated design, not a gap to
quietly fill in.

`PRD/Master_PDR_v2.0.pdf` and `CHEETAH_Product_and_Integration_Spec_v1.5.docx` are the design
authority for requirements/acceptance criteria. `PRD/architecture_diagrams/` (`.dot`/`.svg`/`.png`,
indexed in that folder's `README.txt`) are the **target**-architecture diagrams embedded in the
PDR — useful for understanding where this is headed, but not a claim that it's all built yet.

## Architecture

**Frontend**: `frontend/CHEETAH_UI_Prototype_v1.5.html` is a single standalone HTML file (six
screens, simulation controls, Evidence Reviewer panel, governed-action panel, SSE/API integration)
served directly by the backend at `/` (`backend/app.py`'s `HTML` path). `frontend/assets/` holds a
logo image not yet referenced by the HTML itself.

**Backend** (`backend/`): FastAPI (`app.py`) + SQLAlchemy (`models.py`) + a polling worker
(`worker.py`) that advances simulations. Business/domain logic lives in `core.py`, not `app.py`.

- **Simulated daily playback**: a `Run` steps through October 2026 one day at a time
  (`core.MONTH_DAYS`, `core.run_step`). The worker polls for `running` runs and queued `Command`s
  (`start`/`pause`/`resume`/`stop`/`step`/`set_speed`) — there's no API-synchronous simulation
  advance; commands are queued, then executed asynchronously by whichever worker picks them up.
- **Event sourcing / outbox**: every state change is written to the `event_outbox` table
  (`core.emit`) and streamed to the frontend over SSE (`GET /api/v1/runs/{id}/events`), with
  `Last-Event-ID` resumption. The UI reconstructs state from `snapshot` + the event stream, not
  from direct polling of domain tables.
- **Domain model** (`models.py`): `DailyAmount` (one immutable amount per Oct day per
  `dataset_id`) and `SourceEvent` (raw click/install events) are the inputs; `ForecastReference`
  (the **locked** prior-day-issued actual-vs-predicted interval for one day — immutable, used for
  deviation detection) is distinct from `ForecastValue` (a 7-day rolling forecast **outlook**
  issued on a given day, TabPFN-shaped but TabPFN isn't wired up). A daily `Amount` outside its
  locked `ForecastReference` interval pauses the run and creates an `Incident`, which creates an
  `AgentTask` for the Investigator — immediately `blocked_configuration` rather than fabricating a
  result, since Claude/Neo4j/evidence adapters aren't connected.
- **Non-fabrication is a load-bearing design rule, not an implementation gap**: when an
  integration (TabPFN, Claude, Neo4j, sandbox fix actions, replay) isn't configured, the system
  must surface an explicit blocked/unavailable state (`AgentTask.status='blocked_configuration'`,
  `graph: {status:'unavailable'}`, replay endpoints returning `409`) rather than fake a result.
  `tests/test_backend.py::test_empty_run_does_not_fake_data` asserts exactly this. Preserve this
  pattern in any new endpoint/agent rather than stubbing in plausible-looking fake output.
- **ID fields are strings, not numbers**, in the backend's domain model: `Click ID`, `Transaction
  ID`, `Freecash ID`, `Freecash AdSpace ID`, `Game Placement ID` must round-trip as strings (some
  real values have leading zeros / letters). `Amount` is customer net revenue excluding tax — not
  spend or profit — and aggregate (`DailyAmount`) vs. transaction-level data must not be double
  counted.
- **Infra**: `docker-compose.yml` runs Postgres + Neo4j + `api` + `worker` (Neo4j is provisioned
  but not yet queried by the API). Env vars: `DATABASE_URL`, `POSTGRES_PASSWORD`, `NEO4J_AUTH`,
  `CORS_ORIGINS` — see `backend/.env.example`. Local dev defaults to SQLite
  (`sqlite:///./cheetah_dev.db`) and is explicitly single-worker only — never use it for a
  multi-worker/production deployment.
- **Deliberately not implemented** (per `README.md` / `backend/README.md` — don't build around
  these as if they're just missing, they're explicitly deferred by the product owner pending
  inputs): TabPFN-TS inference, Claude Investigator/Evidence Reviewer (bounded ReAct), Neo4j/MCP
  graph, sandbox fix actions (causal intervention/rollback), Learning Replay and Fix Validation
  Replay (API returns `409` until checkpoint/graph-versioning adapters exist), real Oct 2026
  click/install source data (only isolated test fixtures exist, never loaded into the UI),
  production auth/concurrency hardening.

**Synthetic data generator** (`src/cheetah_synth/`): generates a calibrated synthetic click→
conversion journey dataset from the real `data/` files, for pipeline testing, anomaly-detection
testing, and a TabPFN-ready forecasting table — a different thing from the backend's own
`DailyAmount`/`SourceEvent` ingestion. See `data/synthetic/README.md` for the process/result/next
steps, and the package's module docstrings for implementation detail.

`cheetah_synth.backend_adapter` bridges the two: it pushes a generated run into the backend's own
`/datasets/daily`, `/datasets/events`, `/forecasts/references` endpoints (then creates a `Run`)
so the full ingest→playback→deviation-detection pipeline can be demoed without real data. The
`ForecastReference` band it builds is a deliberately naive trailing-mean placeholder
(`source="synthetic_naive_baseline"`), tuned against the full run's own ~45% day-to-day amount
volatility to flag roughly one deviation across the month — not a forecasting model, and not
tuned to coincide with any particular injected anomaly. Everything it writes is clearly tagged
synthetic (`source_kind="synthetic"`) so it can never be mistaken for real revenue.

## Commands

Backend (from repo root):
```bash
docker compose up --build                   # Postgres + Neo4j + api + worker; UI at :8000
# or, without Docker:
python3.12 -m venv .venv-backend && .venv-backend/bin/pip install -r backend/requirements.txt  # one-time
./start_backend.sh                           # starts uvicorn (--reload) + worker.py together; Ctrl+C stops both
```
**`app.py` requires Python 3.10+** (`X | None` type hints — Pydantic can't resolve these on 3.9,
confirmed even with `from __future__ import annotations`). The repo's own `.venv` is 3.9 (set up
for `cheetah_synth`), so `start_backend.sh` expects the separate `.venv-backend` shown above, not
`.venv`. `worker.py` is what actually executes queued commands and advances simulations — nothing
happens without it running alongside the API.

Once the backend is running, push the synthetic demo dataset into it:
```bash
PYTHONPATH=src python -m cheetah_synth.cli push-to-backend --run-id full --dataset-id synth-full-v1 --start
```
Backend tests: `pytest tests/test_backend.py` (needs `backend/requirements.txt` installed —
not part of the top-level `pyproject.toml` deps, since the backend and `cheetah_synth` have
independent dependency sets). Run the two suites with their separate scoped paths, not a bare
`pytest tests/` — that aborts the *entire* run at collection time if `backend/requirements.txt`
isn't installed (missing `fastapi` fails collecting `test_backend.py`, which is a hard error, not
a per-file skip).

Synthetic data generator (from repo root, `.venv` with `numpy`/`pandas`/`openpyxl`/`pyarrow`):
```bash
PYTHONPATH=src python -m cheetah_synth.cli calibrate
PYTHONPATH=src python -m cheetah_synth.cli generate --run-id full --scale 1.0 --days 31 --seed 42
PYTHONPATH=src python -m cheetah_synth.cli validate --run-id full
PYTHONPATH=src python -m pytest tests/cheetah_synth/
```
(`pip install -e .` would be the normal way to get the `cheetah-synth` console script, but `pip`
itself can't run in the Claude Code sandbox used so far — its bundled `cacert.pem` is blocked by
the filesystem policy. Run via `PYTHONPATH=src` until that's fixed.)

## Available data sources

- `data/Clicks/*.csv` (31 real Oct 2026 daily click logs), `data/OCT26_Transaction Report.xlsx`,
  `data/OCT26_Performance-Report.xlsx` — real data, join keys `Freecash ID`/`Freecash AdSpace ID`/
  `Game Placement ID`/`Country`. Overall real conversion rate ~0.083%.
- `data/synthetic/` — generated, calibrated synthetic journey data (see above). Gitignored, fully
  regenerable, not source of truth.
