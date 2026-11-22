# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

CHEETAH is a hackathon submission: a fully agentic performance marketing intelligence system that
autonomously forecasts revenue, detects anomalies, investigates performance deviations, and
delivers actionable insights through coordinated AI agents. The full brief is in `DESCRIPTION.md`
— read it before any design decision. In short: build a fully agentic monitoring system for a
marketplace. It keeps a graph of how the business works, decides what to watch, detects
meaningful changes, investigates them on its own, and reports with evidence. Nobody has to ask it
questions.

Judging: 40% metric graph + automatic extension, 40% autonomous monitoring & investigation, 20%
working demo (inject a fault + an innocent change, add a new metric, the system reacts alone).

Language: English everywhere (code, comments, CLAUDE.md, agent reports).

**Two parallel implementation tracks currently coexist in this repo** for the same submission —
they have not been architecturally reconciled yet, and touch different directories so there's no
file overlap:
- **Track A — backend/frontend integration foundation** (`backend/`, `frontend/`,
  `src/cheetah_synth/`, root-level `docker-compose.yml`/`Dockerfile`): a FastAPI + SQLAlchemy
  simulation/event backend with a standalone HTML UI, plus a synthetic-data generator. See its own
  section below.
- **Track B — Neo4j graph + Claude agents** (`agents/`, `freecash_graph.py`, `graph_store.py`,
  `llm.py`, `.agents/skills/`): a Neo4j entity graph built from the real data, explored and
  extended by Claude-powered agents. See its own section below.

`PRD/Master_PDR_v2.0.pdf`/`.docx` is the design authority for Track A's requirements/acceptance
criteria (supersedes the older `CHEETAH_Product_and_Integration_Spec_v1.5.docx`, which no longer
exists in the repo). `PRD/PRD_findings_extension.md` is a supplementary, dated decision log for
things settled *after* the master PDR (synthetic-vs-real data scope, Track B staying on real data,
Reports-screen scope cuts) — read alongside it, doesn't modify or supersede it.
`PRD/architecture_diagrams/` (`.dot`/`.svg`/`.png`, indexed in that folder's `README.txt`) are
**target**-architecture diagrams for Track A — useful for understanding where it's headed, not a
claim it's all built yet.

## Real data

Real affiliate data from Freecash, October 2026, in `data/` (read-only inputs):
- `data/Clicks/*.csv` — 31 daily click logs. Columns: `row, Click ID, Time, Freecash ID, Freecash
  AdSpace ID, Game Placement ID, Country`. `Time` is ISO8601 but mixed sub-formats — parse with
  `pd.to_datetime(..., format="ISO8601")`.
- `data/OCT26_Transaction Report.xlsx` — ~3k real conversions (sheet `Sheet`). Columns:
  Transaction ID, Click Time, Time (conversion), Click to Conversion Duration (s), Freecash
  AdSpace ID, Game Placement ID, Transaction Type (install/sale/lead), Country, Amount,
  Commission, Payout, Margin.
- `data/OCT26_Performance-Report.xlsx` — one row per (AdSpace, Game Placement ID, Country),
  monthly rollup, with Vertical/Category/Currency labels.
- If working from a CSV re-export of the xlsx files (Track B's `freecash_graph.py` convention),
  note money values there use thousands separators (`"1,101.98"`) and need separator-aware parsing.

Shared facts: join keys are `Freecash ID`/`Freecash AdSpace ID`/`Game Placement ID`/`Country`;
overall real conversion rate ~0.083%. One month of data only — baselines must work without a prior
month (e.g. a trailing 7-day weekday-adjusted median). Recent days are incomplete because
conversions lag their clicks by up to (and sometimes more than) 7 days.

### Synthetic data (`data/synthetic/`)

A generated, calibrated-against-the-real-data journey dataset (clicks → invented funnel →
conversion), produced by Track A's `cheetah_synth` package. Gitignored, fully regenerable, not
source of truth. See `data/synthetic/README.md` for the generation process, current result, and
open next steps.

## Track A: backend/frontend integration foundation

The current code is the **v1.5 integration foundation**: a runnable backend/frontend skeleton
with simulation, event, and persistence plumbing in place, but with every AI/graph/forecasting
integration deliberately stubbed out pending credentials and real data (see "Deliberately not
implemented" below) — this is the system's own stated design, not a gap to quietly fill in.

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
- **Two ingestion modes** (`Run.ingestion_mode`): **`preloaded_daily`** (default, as above) bulk-
  loads the whole month before the run starts and only reveals already-stored rows as
  `simulated_day` advances. **`live_events`** instead reads a sorted per-event Parquet file
  (`event_source_path`, built by `cheetah_synth.event_timeline`) and genuinely streams rows into
  `SourceEvent` live, one small batch per tick, as a continuous `sim_clock_utc` cursor crosses each
  event's real timestamp (`core.advance_run_live`); `DailyAmount` for each day is computed by the
  worker itself from that day's delivered events once its 24h window closes, not pre-supplied.
  `set_speed`'s valid range is 3–60s/day for `live_events` (vs. 0.5–60 for `preloaded_daily`;
  default created speed is 5s/day, ~2.6min for the full month). Benchmarked against the real
  full-scale `event_timeline.parquet` (~3.73M rows): steady-state insert ceiling ~129,000
  events/sec, real peak-hour clustering ~1.84x the daily average — 3s/day needs ~73,700 events/sec
  at peak (1.75x headroom), below that the margin gets too thin and playback visibly lags.
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
  result, since Claude/Neo4j/evidence adapters aren't connected. `RunMetrics` (one row per run,
  incrementally updated on every live insert) and `RunCampaignSeen` back the dashboard's six KPI
  tiles (Amount/Commission/Payout/Margin/Transactions/Campaigns) so they read live, including the
  in-progress day, rather than only updating once per day-close. `SourceEvent` also carries
  `vertical`/`category` (placement-level attributes, looked up via `calibration.placement_meta` —
  knowable for any click regardless of conversion, not just converting rows).
- **Live-queryable endpoints beyond the snapshot/SSE path**: `GET /runs/{id}/source_events`
  (filterable by kind/campaign/country/date-range/text search, keyset-paginated on the row PK,
  `&format=csv` streams the *full* filtered set) and `GET /runs/{id}/performance_report` (a live
  `GROUP BY (game_placement_id, country)` rollup — `conversion_rate`/`ecpc`/`roas` are computed
  live from click/install counts using the same formulas as `cheetah_synth/export.py`'s
  `populate_derived_metrics`; note the *real* Performance Report leaves these NaN, so these are a
  genuine enhancement, not sourced parity — `ecpm`/`ctr`/`freecash_id`/`freecash_adspace_id` stay
  `null`, genuinely unavailable). These back the frontend's live Reports screen (all 5 tabs) and
  the live half of Marketplace (tile list + Commission/EPC/Country Revenue/per-country breakdown
  on the campaign detail page) — the Marketplace forecast-status pill and the whole Baseline &
  investigation panel stay hardcoded by design (MVP 2 / Investigator scope, not a gap).
- **`models.py` self-heals a stale local dev SQLite DB**: there's no migration tooling in this
  repo, so a schema change (new column/table) would otherwise crash an existing `cheetah_dev.db`
  with `no such column`. `_recreate_if_schema_stale()` (gated to SQLite only, never Postgres)
  detects the mismatch on startup and deletes+rebuilds the file fresh — safe since that file is
  explicitly disposable dev state.
- **`core._live_timelines`** (the in-memory per-run cache of a `live_events` run's loaded event
  timeline) is a capacity-bounded `OrderedDict` (`LIVE_TIMELINE_CACHE_CAPACITY`), evicted both on
  a run reaching a terminal state (`completed`/`stopped`) and via plain LRU — eviction of a still-
  running run is safe because the same COUNT-based recovery used for worker-restart also rebuilds
  an evicted run's cursor transparently on its next tick.
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
- **TabPFN-TS forecast service (FC-01)** is implemented in `backend/forecasting.py` for runs created with
  `forecast_provider='tabpfn'` (`push-to-backend --forecast-provider tabpfn`, or
  `FORECAST_PROVIDER=tabpfn ./start_backend.sh`, which picks `tabpfn` by default when `/health` reports it configured). It uses cloud mode,
  needs `TABPFN_TOKEN` and `backend/requirements-forecast.txt` (pulls torch), and warms up for 7 days. It is triggered at each
  day rollover (emits `forecast.requested`) and forecasts 7 days ahead (`FORECAST_HORIZON_DAYS`) from the last 7 closed days
  (`FORECAST_CONTEXT_DAYS`). Only D+1 is locked. It is unavailable (never a fallback) on error. `backend/env.py` loads the repo-root
  `.env` into `os.environ` at API/worker start (never overriding set vars; tests set `CHEETAH_SKIP_DOTENV=1`). Backtest: `.venv-backend/bin/python backend/forecast_backtest.py`.
  Importing `tabpfn` reads `.env` from the cwd and needs the certifi CA bundle, so it can't run in the Claude Code sandbox.
- **Neo4j day layer** (`backend/graph_sync.py`, via `core.graph_after_close`). Every closed day of every run, after the close commits
  and **before** the TabPFN forecast, it writes that day's metrics from the stored `SourceEvent`s:
  - `(Dataset)-[:CLOSED_DAY]->(Day)` (marketplace)
  - `(Ad)-[:ON_DAY]->(Day)` (campaign)
  - `(Ad)-[:ON_DAY_IN {country}]->(Day)` (report row)

  The edges carry totals, ratios and a trailing 7-day baseline. Ads, countries, verticals, categories and days
  are MERGEd by id onto the monthly graph. One dataset at a time: a new dataset's first sync deletes the old
  day layer. It needs `NEO4J_URI`/`NEO4J_PASSWORD` and the `neo4j` driver. It emits `graph.synced`, or
  `graph.sync_unavailable` and continues. `/health`'s `graph_provider` reflects its config. Tests:
  `tests/test_backend_graph_sync.py` (fake writer).
- **Context Explorer** (`backend/context_graph.py`, `GET /context/graph?label=&id=`, `GET /context/search?q=`): a read-only
  neighbourhood view of the live Neo4j graph. It runs fixed Cypher only, with allow-listed labels, and returns
  `unavailable` when not configured or on error. The UI screen draws the centre node with capped neighbour groups and
  a panel (properties, monthly totals, latest day, agent findings, relationship counts). The sample graph is gone.
  Tests: `tests/test_backend_context_graph.py`.
- **Deliberately not implemented** (per `README.md` / `backend/README.md` — don't build around
  these as if they're just missing, they're explicitly deferred by the product owner pending
  inputs): Claude Investigator/Evidence Reviewer (bounded ReAct), Neo4j/MCP
  graph, sandbox fix actions (causal intervention/rollback), Learning Replay and Fix Validation
  Replay (API returns `409` until checkpoint/graph-versioning adapters exist), real Oct 2026
  click/install source data (only isolated test fixtures exist, never loaded into the UI),
  production auth/concurrency hardening.

**Synthetic data generator** (`src/cheetah_synth/`): generates the calibrated synthetic
click→conversion journey dataset described above, for pipeline testing, anomaly-detection
testing, and a TabPFN-ready forecasting table — a different thing from the backend's own
`DailyAmount`/`SourceEvent` ingestion. See `data/synthetic/README.md` and the package's module
docstrings for implementation detail.

`cheetah_synth.backend_adapter` bridges the two. `push_run_to_backend()` (mode `preloaded_daily`)
pushes a generated run into the backend's own `/datasets/daily`, `/datasets/events`,
`/forecasts/references` endpoints (then creates a `Run`) so the full ingest→playback→deviation-
detection pipeline can be demoed without real data. `register_live_run()` (mode `live_events`)
instead only pre-loads forecast references and points a `Run` at `event_timeline.parquet`
(`cheetah_synth.event_timeline.build_event_timeline` — one row per real click/install, sorted by
timestamp, generated alongside `journeys.parquet`/`funnel_events.parquet`), letting the backend's
own worker do genuinely live, per-event ingestion instead (see Backend bullets above). Either way
the `ForecastReference` band is a deliberately naive trailing-7-day median ± max(4·MAD-sigma, 15%)
placeholder (`source="synthetic_naive_baseline"`, `naive_trailing_median_v1`) — not a forecasting
model. It is sized for the default **`flat` daily profile** (`--daily-profile flat`,
`journeys.flatten_daily_revenue`): daily clicks pinned to the calibrated monthly mean and daily
Amount pinned near the month median (~3% noise) by best-of-N resampling of real payout-pool rows.
Against that, the **default anomaly catalog** (`anomalies.build_default_catalog`) injects four
whole-day revenue anomalies resolved against the day's actual revenue mix — Oct 7
`postback_outage` (−45%, top country), Oct 14 `advertiser_rate_cut` (−40%, top vertical), Oct 20
`promo_surge` (+60%, top placement), Oct 27 `tracking_break` (−35%, top placement) — so the run
pauses on exactly those four days (3 below, 1 above). `--daily-profile calibrated` keeps the real
daily shape (~45% CV, which this band would over-flag); `--anomalies segment` restores the old four
single-segment anomalies (too small to move the aggregate curve). Everything it writes is clearly tagged
synthetic (`source_kind="synthetic"` / `"live_computed"`) so it can never be mistaken for real
revenue.

### Track A commands

Backend (from repo root):
```bash
docker compose up --build                   # Postgres + Neo4j + api + worker; UI at :8000
# or, without Docker:
python3.12 -m venv .venv-backend && .venv-backend/bin/pip install -r backend/requirements.txt  # one-time
./start_backend.sh                           # starts uvicorn (--reload) + worker.py together; Ctrl+C stops both
```
**`app.py` requires Python 3.10+** (`X | None` type hints — Pydantic can't resolve these on 3.9,
confirmed even with `from __future__ import annotations`). `cheetah_synth`'s own venv is
`.venv-data` (3.9), so `start_backend.sh` expects the separate `.venv-backend` shown above, not
`.venv-data`. `worker.py` is what actually executes queued commands and advances simulations —
nothing happens without it running alongside the API.

**`start_backend.sh` also auto-pushes a demo dataset once the API is reachable**: it generates a
fresh timestamped `--dataset-id` (so repeated restarts never collide with a prior run — dataset
IDs are immutable), pushes the `full` synthetic run in `live_events` mode, and sends `start` — so
there's always a running demo immediately after the script comes up, no manual push needed. Set
`SKIP_AUTO_PUSH=1` before running it to skip this and push manually instead. This requires
`data/synthetic/full/raw/event_timeline.parquet` to already exist (see the generator commands
below) and `.venv-data` to exist alongside `.venv-backend`.

To push manually instead — bulk `preloaded_daily` or genuinely live `live_events` (see Backend
bullets above); always use a dataset_id you haven't used before, since both the daily-amount rows
and the naive forecast-reference IDs are dataset-scoped and immutable:
```bash
PYTHONPATH=src python -m cheetah_synth.cli push-to-backend --run-id full --dataset-id synth-full-v1 --start
PYTHONPATH=src python -m cheetah_synth.cli push-to-backend --run-id full --dataset-id synth-full-v2 --ingestion-mode live_events --start
```
`event_timeline.parquet` is generated automatically by `cheetah-synth generate`; for an
already-generated run directory that predates it (or predates the `vertical`/`category` columns),
rebuild it with `PYTHONPATH=src python -m cheetah_synth.cli build-timeline --run-id full`.

Backend tests: `pytest tests/test_backend.py tests/test_backend_live.py tests/test_backend_reports.py`
(needs `backend/requirements.txt` installed — now includes `pandas`/`pyarrow`, required for the
`live_events` scheduler — not part of the top-level `pyproject.toml` deps, since the backend and
`cheetah_synth` have independent dependency sets). Run the suites with their separate scoped
paths, not a bare `pytest tests/` — that aborts the *entire* run at collection time if
`backend/requirements.txt` isn't installed (missing `fastapi` fails collecting `test_backend.py`,
which is a hard error, not a per-file skip).

Synthetic data generator (from repo root, `.venv-data` with `numpy`/`pandas`/`openpyxl`/`pyarrow`):
```bash
PYTHONPATH=src python -m cheetah_synth.cli calibrate
PYTHONPATH=src python -m cheetah_synth.cli generate --run-id full --scale 1.0 --days 31 --seed 42
PYTHONPATH=src python -m cheetah_synth.cli validate --run-id full
PYTHONPATH=src python -m pytest tests/cheetah_synth/
```
(`pip install -e .` would be the normal way to get the `cheetah-synth` console script, but `pip`
itself can't run in the Claude Code sandbox used so far — its bundled `cacert.pem` is blocked by
the filesystem policy. Run via `PYTHONPATH=src` until that's fixed.)

## Track B: Neo4j graph + Claude agents

The Neo4j graph is **the** graph. `graph_build_proposal.md` (a metric graph with
formula/causal/signature edges) and `../hacklab/graphwalk` are **reference only**: borrow ideas,
don't port the architecture.

Built by `freecash_graph.py` → `freecash_graph.cypher` (idempotent `UNWIND … MERGE`):

```
(Freecash)-[:RUNS {tx, amount, commission, payout, margin}]->(Ad)
(Ad)-[:CONVERTS_IN]->(Country)          all five carry the same totals
(Ad)-[:CLICKED_AT]->(ClickHour 0-23)    {tx, amount, commission, payout, margin}
(Ad)-[:CONVERTS_AFTER]->(LagBucket '<1m','<1h','<1d','<7d','>7d')
(Ad)-[:HAS_TYPE]->(TransactionType sale|lead)
(Ad)-[:ACTIVE_IN]->(Month)
(Ad)-[:IN_VERTICAL]->(Vertical)  (Ad)-[:IN_CATEGORY]->(Category)  (Ad)-[:PAID_IN]->(Currency)
```

Principles:
- **No node per transaction or click.** Rows are summed into edges, so graph size depends on
  #ads × #buckets, not on #rows. Keep it that way for every extension.
- The build is the only writer of base nodes and aggregate-edge totals. Re-running it must not
  wipe agent data (it only MERGEs nodes and SETs totals).

### Planned extensions (agreed, not built yet)
- **Time axis:** `(:Day)` nodes plus per-day aggregate edges (e.g. `Ad-[:ON_DAY {tx, commission, …}]->Day`,
  and Ad × Country × Day where monitoring needs it). Monitoring reads time series straight from
  Neo4j. *Partly built:* the streamed run's day layer (`ON_DAY` / `ON_DAY_IN` / `CLOSED_DAY`,
  written by `backend/graph_sync.py`, see Track A). A day axis from the real transaction files is still open. Bucket outcomes by conversion `Time` and cohort metrics (lag, instant share) by `Click Time`.
- **Metric nodes:** `(:Metric {name, definition, cypher|formula, unit})` with edges between metrics
  (e.g. `FORMULA` for exact arithmetic decompositions like `commission = gmv * comm_rate`, and
  `INFLUENCES` for possible causes with a weight and a status `hypothesis|validated|rejected`).
  Metrics are defined as ratios of sums over edge totals so they can be sliced by every dimension.
  Today they live as Cypher inside `agents/investigator_agent.py` (`AD_METRICS_Q`) and should move
  into Metric nodes.
- **Metric onboarding agent:** a contributor submits a name, a definition and a query/formula. The
  agent validates it (it must evaluate, and a formula's residual must be ≈ 0 globally and per
  segment), finds its connections (formula parsing, correlation on daily series, LLM semantics),
  and writes it as a Metric node. The contributor never specifies its place in the graph.
- New base labels/relationships must be added to `BASE_LABELS` / `AGGREGATE_RELS` /
  `AGENT_LABELS` in `graph_store.py` **and** to the graph model description in the overlooker's `SYSTEM` prompt.

### Write rules (enforced by `graph_store.py`)

Agents **never write Cypher**. All writes go through `GraphStore`:
- Base nodes: only `ag_*` properties.
- Aggregate edges: totals are read-only. Agents may set `ag_*` (e.g. `ag_weight`). "Removing" one
  sets `ag_disabled = true`.
- Agent edges (any other UPPER_SNAKE_CASE type, weight in [-1, 1]) and agent nodes (`Outage`,
  `Hypothesis`, status `open|resolved|confirmed|rejected`) are free to create, change and delete.
- Every write creates a `(:Change)` audit node and can be reverted with `undo(change_id)`. Never
  modify `Change` nodes.
- `GraphStore.read()` rejects any non-read-only Cypher. Return scalars/maps (`properties(n)`), not node objects.

### Agents (`agents/`)

`run_agents.py` runs one pass: outage → investigator → graph overlooker. The overlooker runs once
at the end over all collected change ids. Run standalone, each agent starts the overlooker itself.

- `outage_agent.py`: web research (Claude + web search) checks whether services the ads depend on
  are down now. It writes `(Outage)-[:AFFECTS]->(Country)` and `ag_risk*` flags on Ads, and
  resolves outages that are no longer reported.
- `investigator_agent.py`: gathers context for an Ad/Country/Category/Outage, computes deviations
  against peers (same category, all ads), and asks Claude for hypotheses. Every evidence line must
  quote a number that is actually in the context, otherwise it is dropped. It writes
  `(Hypothesis)-[:ABOUT]->(…)`.
- `graph_overlooker.py`: explores the graph with read-only Cypher and proposes edge/property/status
  changes. Each one needs reasoning + evidence queries, and an **independent reviewer call** sees
  the re-run evidence and must approve it.
- `product_manager_agent.py` ("Croc"): one fix discussion per reported issue: playbook candidate
  (`agents/fix_playbook.md`), one round of stances (`agents/stances.py`), then seven deterministic
  gates decide `fix_proposed` / `no_fix` + reason / `graph_only` / `monitor`. Read-only on the graph.
  Only P1 is fully measured; other playbook entries are never proposed until a watcher reports them.
- `graph_engineer_agent.py` ("Buffalo"): metric onboarding (`GraphStore.add_metric` /
  `link_metrics`; FORMULA only with residual <= 1e-6 globally and per slice), and validation of
  INFLUENCES edges as `ag_status='validated'` (read status as `coalesce(ag_status, status)`).
  INFLUENCES writes need the independent reviewer; without the LLM they are deferred.

These run as scheduled threads in the backend worker (`backend/agent_runtime/`, file bus in
`var/agent_bus/`, plan in `agents/orchestration_plan.md`), shown on the Agentic Monitoring tab.
Each incident's real trail is emitted as `investigation.step` SSE events (`dispatcher.emit_step` /
`record_step`; the investigator reports via `investigate(..., on_step=)`) and returned in the
snapshot's `steps`. The UI's two incident graphs are drawn only from these.

Still missing for the full loop (see DESCRIPTION.md): a continuous monitor for the real affiliate
data (the pacing watcher covers simulated paid telemetry), and the demo scenarios with fault injection.

### Agent conventions
- LLM calls go through `llm.py` (`claude_json`: one call → one JSON object matching a schema, with
  optional web search). Every call names a model tier, never a default: `"small"` (Haiku 5.5:
  summaries, stances), `"medium"` (Sonnet 5.5: search + extraction), `"large"` (Opus 5.5: actionable
  reasoning such as hypotheses, reviewers and graph decisions). Override with `CLAUDE_MODEL_SMALL` /
  `_MEDIUM` / `_LARGE` (`CLAUDE_MODEL` still sets large). Use the lightest tier that fits the task.
- **Unknown never acts.** `LLMUnavailable`, missing keys, unparsable answers and failed searches
  all become status `unknown`. They are recorded as an uncertainty and never trigger a flag or action.
- Numbers come from the graph, not from the model. Validate LLM output (node existence, confidence
  in [0,1], `ag_*` keys, quoted numbers) before writing.
- Small samples (few tx) → low confidence, and say so. Separate a supported cause from a hypothesis,
  and downstream symptoms from first causes.
- Reports must contain: what changed (numbers + baseline), the investigation trail, the business
  impact (commission/margin at risk), what remains uncertain, and **ranked recommended actions**
  per country / ad (campaign) / category with expected impact. Actions are recommendations only,
  never executed. Ideas for the action catalogue are in `FIXES.md` (pause on outage, reduce
  bid/cap, more ads where payout is low, landing/button changes when conversion lag rises, price
  changes, revert the last release). Not every cause has a fix (bad internet, payment not accepted, app crash).

### Legacy (don't extend)

`service_down_agent.py` (networkx + Gemini) and `metrics_monitor_agent.py` are the old stack.
Don't add features there. Exception: `agents/outage_agent.py` still imports the research logic
(prompt, parsing, country/region normalisation) from `service_down_agent.ExternalOutageCampaignAgent`.
Move that into `agents/` before deleting the file.

### Track B setup & commands

- Neo4j runs on **Aura**: set `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD` (optionally
  `NEO4J_DATABASE`) in `.env` (template: `.env.example`, never commit `.env`). Without them,
  `graph_store.py` falls back to `neo4j-cli` with credential `test`.
- `ANTHROPIC_API_KEY` in `.env` for the agents.
- Python venv: `.venv-agents` (anthropic, neo4j, networkx) — a **separate** venv from Track A's
  `.venv-data` (numpy/pandas/openpyxl/pyarrow for `cheetah_synth`) and `.venv-backend` (FastAPI
  backend); check which track you're working in before assuming what's installed.

```bash
.venv-agents/bin/python freecash_graph.py > freecash_graph.cypher   # rebuild graph from CSVs (prints check to stderr)
neo4j-cli query --rw --credential <aura-cred> < freecash_graph.cypher   # load (statements split on ';')
.venv-agents/bin/python run_agents.py                               # one full agent pass
.venv-agents/bin/python run_agents.py --target Ad:0003WS            # also investigate specific nodes
.venv-agents/bin/python run_agents.py --skip-outage --target "Category:Fashion & Apparel"
```

Neo4j skills (Cypher, modeling, GDS, Aura, drivers) live in `.agents/skills/`. Use them for
Cypher syntax and modeling questions.

## Available data sources

- `data/Clicks/*.csv`, `data/OCT26_Transaction Report.xlsx`, `data/OCT26_Performance-Report.xlsx`
  — real Oct 2026 data (see "Real data" above).
- `data/synthetic/` — Track A's generated, calibrated synthetic journey data. Gitignored, fully
  regenerable, not source of truth.

## Coding guidelines (Karpathy)

Behavioral guidelines to reduce common LLM coding mistakes, from
[andrej-karpathy-skills](https://github.com/multica-ai/andrej-karpathy-skills) (MIT). Also available
as the `karpathy-guidelines` skill in `.claude/skills/`. Project-specific rules above take precedence.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

### 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

### 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

### 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

### 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.
