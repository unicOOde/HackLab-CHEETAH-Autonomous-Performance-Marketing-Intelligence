# CHEETAH v1.5 backend foundation

This is a runnable **backend integration foundation**, not an assertion that the TabPFN, Claude, Neo4j or action adapters are connected. Those inputs were explicitly deferred. Missing providers show blocked/unavailable states; the system never fabricates successful agent outcomes.

## Quick local smoke test

From `backend/`: install `requirements.txt`. Run `uvicorn app:app --reload --port 8000` in one terminal and `python worker.py` in a second terminal. The UI appears at http://localhost:8000/ . Local development uses SQLite by default; for architecture-aligned persistent PostgreSQL set `DATABASE_URL` to a PostgreSQL DSN in `.env.example` (and export it in the shell). Never use SQLite for a multi-worker production deployment.

There's no migration tooling in this repo — `models.initialize()` (called on API/worker startup) only adds missing *tables*, it can't add missing *columns* to a table that already exists. For the local SQLite dev DB specifically (never Postgres), it also detects when the on-disk file predates the current model columns and automatically deletes+rebuilds it fresh, rather than crashing with `no such column` errors after a schema change — this is safe because that file is explicitly disposable dev state. A Postgres `DATABASE_URL` is never touched this way; schema drift there needs a real migration.

## Current implemented paths

- October 2026 31-day dataset ingestion, immutable dataset IDs, data validation.
- Externally supplied immutable next-day forecast references (only when provided).
- Persistent run creation, commands, worker execution, UTC event outbox and SSE reconnect.
- Daily aggregate Amount evaluation and automatic pause on upper/lower bound breach.
- Incident log and blocked agent audit task when Claude/context/evidence not available.
- UI-ready health, run snapshots, run list and read models for incidents.

## Deferred adapters / reasons

- Direct TabPFN-TS calls require API credentials, training-context data and forecast quantile validation. **Currently input references are accepted, but not inferred.**
- Claude investigation, bounded ReAct and Evidence Reviewer require model authentication, task/tool schema and scenario-specific evidence records.
- Neo4j MCP requires ontology and graph credentials. The context read endpoint returns an empty unavailable model.
- Sandbox Fix Action requires scenario-specific causal intervention controls, approvals and rollback policy.
- Learning and Fix Validation replay need checkpoint snapshots and approved graph/intervention versioning. Endpoints reject rather than fake a result.
- Source click/install live batching requires full click/install datasets. Original v1.4 sample records remain display-only reference fixtures. A synthetic (not real) stand-in dataset and a push adapter exist at `../src/cheetah_synth/` / `PYTHONPATH=src python -m cheetah_synth.cli push-to-backend` for demo/dev purposes — see the repo root `README.md`'s "Synthetic demo data" section.

## Ingestion modes: `preloaded_daily` vs `live_events`

A `Run`'s `ingestion_mode` controls how its revenue/event data reaches the database:

- **`preloaded_daily`** (default, unchanged): the caller bulk-POSTs the whole month's `DailyAmount`
  rows and a downsampled set of `SourceEvent` rows up front via `/api/v1/datasets/daily` /
  `/api/v1/datasets/events`, before the run even starts. The worker only reveals already-stored
  rows as `run.simulated_day` advances — this is a transmission simulation, not live ingestion.
- **`live_events`**: nothing is pre-loaded. `POST /api/v1/runs` instead takes an
  `event_source_path` pointing at a sorted Parquet file (one row per real click/install event —
  see `src/cheetah_synth/event_timeline.py`; `cheetah-synth push-to-backend --ingestion-mode
  live_events` builds and registers it automatically). The worker (`core.advance_run_live`) reads
  that file once, then streams events into `SourceEvent` live, one small batch per tick, as the
  simulated clock passes each event's real timestamp — the database genuinely grows during
  playback. `DailyAmount` for each day is computed by the worker itself (summed from that day's
  delivered `install` events) once the day's 24-hour window has fully elapsed, not supplied ahead
  of time. Forecast references still get pre-loaded in this mode too — they represent an external
  model's output issued before the day it evaluates, which is unrelated to this distinction.

Because `live_events` can mean genuinely high insert volume (the real Oct 2026 month has ~3.7M
clicks), `set_speed`'s valid range is **3–60 seconds/day** for this mode (vs. `0.5–60` for
`preloaded_daily`, which never does per-event inserts and has no such throughput concern); runs
default to **5 seconds/day** (full month in ~2.6 min). These numbers come from a direct benchmark
against the real full-scale `event_timeline.parquet` (~3.73M rows), not a guess: steady-state
insert ceiling ~129,000 events/sec, real peak-hour clustering ~1.84x the daily average. At the
3s/day floor that's ~73,700 events/sec needed at peak — 1.75x headroom; below 3s/day the margin
gets thin enough that playback visibly falls behind its own clock (it self-corrects over
subsequent ticks rather than losing data, but won't match the requested pace). This mode also
requires `pandas`/`pyarrow` (now in `requirements.txt`) and, for the local SQLite dev DB,
relies on `PRAGMA journal_mode=WAL`/`synchronous=NORMAL` (set automatically on connect in
`models.py`) — safe because this architecture is already explicitly single-writer, as above.

## Local backend requires Python 3.10+

`app.py` uses `X | None` type-hint syntax (PEP 604), which Pydantic can only resolve on Python
3.10+ at runtime — this fails on 3.9 even with `from __future__ import annotations` (verified: the
`eval_type_backport` package or rewriting the hints would be needed to support 3.9). The
`Dockerfile` already targets `python:3.12-slim`, so `docker compose up --build` is unaffected; for
a non-Docker local run, use a 3.10+ interpreter/venv, not whatever venv a 3.9-only component of
this repo (e.g. `cheetah_synth`) might already have set up.

## Example data delivery

`POST /api/v1/datasets/daily` accepts a JSON `{ "dataset_id": "oct2026-v1", "records": [{"day":"2026-10-01","amount":123.45,"source_kind":"historical"}, ...31 dates]}`. This is a schema illustration, not supplied data. Do not generate sample values to fill the month without user approval.

`POST /api/v1/forecasts/references` accepts a dataset ID and externally produced reference snapshots, each with `id`, `reference_day`, `issue_day`, `point`, `lower`, `upper`, `model_version`. Example day 2026-10-02 has issue_day 2026-10-01. The required Oct 1 reference would be issued Sep 30.

The actor responsible for ingesting data must provide provenance and a reporting timezone; this foundation defaults to date-only input and does not attempt monetary reconciliation of source transactions.
