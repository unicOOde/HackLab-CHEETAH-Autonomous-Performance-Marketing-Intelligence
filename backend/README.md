# CHEETAH v1.5 backend foundation

This is a runnable **backend integration foundation**, not an assertion that the TabPFN, Claude, Neo4j or action adapters are connected. Those inputs were explicitly deferred. Missing providers show blocked/unavailable states; the system never fabricates successful agent outcomes.

## Quick local smoke test

From `backend/`: install `requirements.txt`. Run `uvicorn app:app --reload --port 8000` in one terminal and `python worker.py` in a second terminal. The UI appears at http://localhost:8000/ . Local development uses SQLite by default; for architecture-aligned persistent PostgreSQL set `DATABASE_URL` to a PostgreSQL DSN in `.env.example` (and export it in the shell). Never use SQLite for a multi-worker production deployment.

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
