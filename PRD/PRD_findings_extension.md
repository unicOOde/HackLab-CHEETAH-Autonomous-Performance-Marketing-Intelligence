# PRD Findings & Extensions

Supplementary findings and decisions reached after `Master_PDR_v2.0`, scoped to this repository's
current implementation state. Does not modify or supersede the master PDR — read alongside it.

## 2026-11-22 — Synthetic data as the official Track A demo dataset

**Decision:** For Track A's simulation/forecast/investigation loop, the existing
`cheetah_synth` synthetic journey dataset is adopted as the **official** demo dataset, rather than
building a separate real-October-2026 ingestion path. Track B's Neo4j graph build
(`freecash_graph.py`) is unaffected and continues to be built from the real CSVs/xlsx in `data/`.

**Rationale:**
- Master PDR §12 (current implementation audit) phrases the open requirement as "**Real
  historical/synthetic** October dataset and proven source coverage" — synthetic was always an
  accepted route, not a fallback.
- §13.2 (open engineering/product inputs) lists the blocking item as "pending full month source
  **or synthetic generation** with 2 negative/1 positive injected event definitions."
- The hackathon brief (`DESCRIPTION.md`) states no dataset is provided and explicitly invites a
  simulator ("create your own mock data or simulator... choose a manageable scope"), and the
  20%-weighted demo criterion requires injecting "a fault and an innocent change" — only possible
  with a simulator where ground truth is controlled, which real October data cannot provide (no
  injectable faults, no ground truth, conversions lag clicks by up to 7+ days making recent days
  incomplete, no prior month for a baseline).
- `cheetah_synth` is already built, calibrated against the real reports, validated, and wired
  end-to-end into the backend via `backend_adapter.py` / `push-to-backend`.

**Scope boundary:** real data stays authoritative for Track B's graph structure (business
relationships, aggregate totals — no anomalies needed there). Synthetic data is authoritative for
Track A's daily revenue simulation, forecast references, and the anomalies that drive
investigation. `source_kind="synthetic"` tagging (already implemented) must remain honest and
visible everywhere this data surfaces, per CLAUDE.md's non-fabrication rule — "official demo
dataset" does not mean presenting it as real.

### Anomaly catalog confirmed against CHEETAH's four target anomaly types

`src/cheetah_synth/anomalies.py` already implements exactly four anomaly types, one-to-one with
the agreed set:

| # | Required anomaly | Implementation | Mechanism |
|---|---|---|---|
| 1 | Click-volume spikes / click-farm bursts, often with abnormal (too-regular) timing | `traffic_burst` (`_apply_traffic_burst`) | Injects extra clicks for a segment-day scaled by `severity`; `params={"regular_timing": True}` spaces them at perfectly even microsecond intervals instead of the normal empirical hour-of-day sampling |
| 2 | Conversion-rate shocks (collapse or spike) | `cvr_shock` (`_apply_cvr_shock`) | Overrides the segment's calibrated CVR as `base_cvr * severity` (clipped to [0,1]); re-draws the conversion decision per row, materializing or clearing conversions — bidirectional |
| 3 | Payout/commission/margin shocks | `payout_shock` (`_apply_payout_shock`) | Scales `amount/commission/payout/margin` by `severity` for converting rows in the window, clipped at 0 — bidirectional |
| 4 | Conversion-latency blowouts (abnormally long or short vs. calibrated distribution) | `latency_blowout` (`_apply_latency_blowout`) | Scales `duration_seconds` by `severity`, shifts conversion time and funnel-stage timestamps proportionally, marks `censored` if pushed past Oct 31 — bidirectional |

`build_default_catalog()` already instantiates one of each against real calibrated segments, each
in its own 3-day window across October.

### Open follow-up

The backend's naive forecast-reference band (`cheetah_synth.backend_adapter.build_naive_references`)
is currently tuned to surface exactly **one** breach (Oct 7), not the PRD §13.2-required 2 negative
+ 1 positive. Still to decide: which 3 of the 4 anomaly types above (plus one genuine "innocent
change" carrying no `anomaly_id`) the band should be tuned to catch for the demo.

## 2026-11-22 — Track B's Neo4j graph stays on real data, not synthetic

**Decision:** Track B's graph build (`freecash_graph.py`) continues to ingest the real CSVs/xlsx
in `data/` and is **not** fed the synthetic dataset, even though `cheetah_synth`'s exports are
schema-identical to the real files (same column names/dtypes, built for ETL testing) and could
likely be ingested with no code changes.

**Rationale:**
- **No time axis yet.** Track B's graph currently only has monthly aggregate edges — the planned
  `(:Day)` nodes and per-day aggregate edges (CLAUDE.md, "Planned extensions") don't exist. The
  four injected anomalies are 3-day windows; without per-day edges, a 3-day shock dissolves into
  the monthly total and becomes statistically invisible. Track B's agents could not see or
  investigate what Track A's day-level deviation detector is built to catch, regardless of data
  source.
- **Judging weight on authentic structure.** The metric graph is 40% of the score, and Track B is
  explicitly scoped (CLAUDE.md) as built from the real data — its value is demonstrating the
  system understands the *actual* marketplace, not a calibrated reconstruction of it.
- **No provenance tagging mechanism.** `graph_store.py`'s schema has no `source_kind`/provenance
  field on aggregate edges the way Track A's DB has `source_kind="synthetic"` on `DailyAmount`
  rows — there would be no honest way to mark a synthetic edge total as such if one were mixed in,
  which runs against CLAUDE.md's non-fabrication rule for this track.

**Conclusion:** this is a scope decision reinforced by one real gap (missing Day nodes), not a hard
engineering wall. If Track B should eventually react to the same injected anomalies, the Day-node
extension needs to land first — then feeding it synthetic data becomes a live option rather than a
blocked one.

**Update (2026-11-22, later):** the Day-node extension has now landed for the streamed run. See
"Streamed day layer in Neo4j" below. The monthly build from the real files is unchanged.

## 2026-11-22 — Streamed day layer in Neo4j, written before each forecast

**Decision:** as each day closes, the backend worker (`backend/graph_sync.py`) writes the streamed run's
end-of-day metrics into the Track B graph:
- the marketplace day: `(Dataset)-[:CLOSED_DAY]->(Day)`
- each campaign: `(Ad)-[:ON_DAY]->(Day)`
- each campaign×country report row: `(Ad)-[:ON_DAY_IN {country}]->(Day)`

This happens after the close commits and **before** the D+1 TabPFN forecast, so the agents can investigate
exactly the day the detector evaluated.

**How it addresses the three earlier objections:**
- **Time axis:** the edges are per day, so a single-day shock stays visible.
- **Real structure:** the monthly aggregate edges from the real files are untouched. The day layer only
  MERGEs `Ad`/`Country`/`Vertical`/`Category`/`Day` by id and adds its own relationship types.
- **Provenance:** every day edge carries `dataset_id`, `run_id` and the day's `source_kind`, taken from
  the backend's `DailyAmount`.

**Scope choices (product owner):**
- Data comes from the events the backend actually received.
- One dataset at a time: a new dataset's first sync deletes the previous one's day layer.
- Only closed days, so the graph never runs ahead of the simulation clock.
- Grain: marketplace, campaign, campaign×country.
- Metrics per edge:
  - totals, conversion rate, EPC, ROAS, AOV, margin rate and share of the day
  - on marketplace and campaign edges, a trailing 7-day baseline (avg, % vs. baseline, day-over-day)
  - on the marketplace edge, also the locked forecast band, the classification and the incident id
- If Neo4j is missing or fails, an explicit `graph.sync_unavailable` is emitted and the run continues.

## 2026-11-22 — Reports screen goes live: known, deliberate limitations

**Decision:** The Reports screen's "Combined event feed"/"Click Events"/"Install Transaction
Report"/"Performance Report"/"Time Series" tabs were switched from a 12-row static sample to a
real paginated/filterable backend (`GET /runs/{id}/source_events`, `GET /runs/{id}/performance_report`),
live-updating via SSE. Three scope calls were made to keep this change contained rather than
open-ended, recorded here so they read as intentional rather than overlooked:

- **Campaign/Country filter dropdowns still list only the original 12 static sample values** —
  not a live-updating enumeration of every placement/country a run has actually seen. Building
  that would need a new "distinct values for this run" backend endpoint, which was out of scope
  for this pass. The free-text search box (`q`, matched server-side against `source_id`/
  `game_placement_id`/`country`) is the practical workaround for looking up a specific ID at live
  scale.
- **Performance Report's column headers are no longer click-to-sort.** The old static version's
  sort was a pure in-memory array sort over 3 rows; this tab's data is now fetched from a live
  aggregation endpoint, and wiring a generic re-sort-and-refetch mechanism for it felt like scope
  creep beyond "make the data live." The report still renders in a stable (group-by) order.
- **`q` free-text search is narrower than before.** The legacy client-side search matched the
  stringified value of every column (including amounts/dates) over an in-memory array of 9 rows.
  The live backend's `q` is scoped to `source_id`/`game_placement_id`/`country` only — a full
  cross-column text index over a potentially multi-million-row table was judged unnecessary for
  this round; use the explicit date-range (`event_day_from`/`event_day_to`) and kind filters for
  anything `q` doesn't cover.

None of these block the core ask (live data, not static samples) — they're narrower-than-ideal
UX/filtering completeness, not missing data or fabricated values.

## 2026-11-22 — TabPFN-TS forecast service (FC-01) for the aggregate Amount

**Decision:** The PDR's FC-01 is implemented online in the worker (`backend/forecasting.py`), using
TabPFN-TS in Prior Labs **cloud mode** (`TabPFNMode.CLIENT`, `TABPFN_TOKEN`), for runs created with
`forecast_provider='tabpfn'`. Existing runs and imported references are unchanged (`external` default).

- **One-day horizon first.** At the close of D only D+1 is forecast, and it is locked as the immutable
  reference (`FORECAST_HORIZON_DAYS=1`). The data model and adapter already support the PDR's 7-day
  outlook, so switching is a configuration change.
- **Warm-up = 7 days of history**, so the first TabPFN reference covers Oct 8. Earlier days are explicitly
  `forecast.unavailable` / `warm_up` (§6.4 "explicit warm-up … or unavailable"), not a substitute baseline.
- **No fallback on provider failure** (§6.4, §7.5, AT-121). A timeout or error emits `provider_error`, no
  reference is locked, and the next day is `reference_unavailable`. No incident is created and no healthy
  state is implied.
- **Forecast at close even when paused.** §6.2 step 6 says to issue the forecast "when permitted to
  continue". Because no fix actions exist yet, D's data cannot change between close and resume, so issuing
  at close is equivalent and keeps the reference ready. Revisit when sandbox interventions land.
- **Interval = P10–P90** (§6.3 [PROPOSED]), with P50 as the point. These are model output quantiles, not
  calibrated coverage: see the backtest.
- **Telemetry off.** The package's PostHog usage telemetry is disabled (`TABPFN_DISABLE_TELEMETRY=1`),
  so only the inference request leaves the backend.

### Backtest result (2026-11-22, `backend/forecast_backtest.py`, run `full`, 24 cutoffs Oct 7–30)

`tabpfn-ts-1.4.0/p10-p50-p90`, one-day horizon: **MAE €2,900, WAPE 35.0%, bias +€514, P10–P90 coverage
75%** (nominal 80%). About 3 s per cloud call. The interval is honest, but wide, because the synthetic
aggregate swings about 45% from day to day.

**None of the 6 breaches (Oct 11↓, 18↑, 23↓, 25↑, 29↑, 30↑) fall in an injected-anomaly window. None of
the four injected anomalies breaches the aggregate band.** Tracing each anomaly's revenue by conversion
day explains why:

| Anomaly | Effect on daily aggregate Amount | Visible to an aggregate Amount detector? |
|---|---|---|
| A-001 traffic_burst | 0 conversions: clicks only | No, by construction. Needs a click-volume signal |
| A-002 cvr_shock | 23–30% of the day's Amount, Oct 13–15 | Borderline. Oct 13 €10,182 vs upper €11,602 |
| A-003 payout_shock | ≤9% of the day's Amount | No |
| A-004 latency_blowout | 7 conversions, ≤2% | No. Needs a latency signal |

**Open decision:** the PDR §13.2 demo needs 2 negative + 1 positive detected events. The aggregate
Amount band can't deliver that with the current anomaly catalog. Ways forward:
- make the injected anomalies aggregate-visible (higher severity, or more segments);
- add segment- or signal-level detectors (traffic, CVR, payout and latency, per the ANOM01 PRD);
- or both.
The 6 unlabeled breaches are natural variance in the synthetic baseline. Each would pause the run.

### Follow-up (2026-11-22): 7-day horizon, 7-day context, visible trigger, connection status

- **Horizon is now 7 days** (`FORECAST_HORIZON_DAYS=7`), so the chart shows 7 closed days plus 7 forecast
  days. Only D+1 is locked as the reference; D+2..D+7 are outlook only.
- **Context is the last 7 closed days** (`FORECAST_CONTEXT_DAYS=7`), not the full history. The backtest
  uses the same window, so its numbers above (full-history context) need a rerun.
- **Trigger:** the forecaster starts when a new day's timestamp enters the stream, i.e. when the live
  clock rolls over. It emits `forecast.requested` (`trigger: day_rollover`) before the cloud call, and the
  UI shows "Forecasting…" until `forecast.updated` arrives.
- **The honest P10–P90 band is kept** on purpose, even though the design mock shows a much tighter
  illustrative band (about ±8%). On this data an honest band is about ±50%.
- **Connection status:** the backend loads `.env` at startup. A header pill shows whether the TabPFN token
  and package are loaded (dark blue) or not (light blue). The token is never sent to the browser.
- **The earlier "current" screenshot** was a naive-reference run, because `start_backend.sh` used to
  default to `naive`. It now defaults to `tabpfn` whenever `/health` reports the forecast service as
  configured.

**Backtest rerun with the 7-day context window** (same 24 cutoffs, D+1):

| | Full-history context | 7-day context (now live) |
|---|---|---|
| MAE | €2,900 | €3,043 |
| WAPE | 35.0% | 36.7% |
| Bias | +€514 | −€7 |
| P10–P90 coverage | 75% | 75% |
| Breaches | 6, none labeled | 6, one coinciding with a label (Oct 26, A-004) |

- **Accuracy:** about the same, and the bias is gone.
- **Volatility:** bands now move more from day to day, from €2.7k wide (Oct 17) to €15.9k wide (Oct 31).
  With only 7 points, one spike dominates: Oct 30's €18k pushed Oct 31's P50 to €16k.
- **The Oct 26 breach is a coincidence, not a detection.** A-004 contributes ≤2% of that day's Amount
  (see the table above), so the open anomaly-visibility decision is unchanged.
- **Latency:** one call took 28 s (Oct 29) against a typical ~3 s. That's within the 60 s timeout, but in
  live playback it stalls the run for that long at the rollover.
