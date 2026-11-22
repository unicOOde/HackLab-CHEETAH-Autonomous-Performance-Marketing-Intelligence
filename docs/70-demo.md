# 70 · Demo: fault, innocent change, new metric

## Purpose and judging relevance

The brief's demo (20%): "Introduce a fault and an innocent change, add a new metric, and let the
system respond autonomously." This page maps each requirement to the ingredient that exists today
and the reaction to look for. Setup is in [01-operations](01-operations.md).

## The ingredients

| Requirement | Ingredient | Where it's injected | Who reacts |
|---|---|---|---|
| **Fault** | `pacing_bug`: controller stops pacing at 06:00 and pins the bid multiplier at 1.9 → budget gone by late morning | `paid_sim.py`, last simulated day (Oct 14) | pacing watcher → product manager (P1 fix) |
| **Fault (external)** | `competitor_pullback`: competition −40% → win rate up, CPC down, bids flat | `paid_sim.py` | pacing watcher (external pressure, not bid-driven) → PM (`monitor`, then `no_fix`) |
| **Innocent change** | `planned_promo`: auctions and budget ×1.7 with a `PlannedEvent` node | `paid_sim.py` | pacing watcher folds the planned lift into its baseline → no alert |
| **Missing data** | `tracking_loss`: tracking completeness 0.35, 07:00–11:59 | `paid_sim.py` | pacing watcher emits a data-completeness signal and suspends other checks on that slice |
| **Revenue faults** | four whole-day anomalies (Oct 7, 14, 20, 27) | `cheetah_synth` default catalog | deviation detector → dispatcher → investigator |
| **New metric** | contributor submits `{id, definition, unit, cypher_value}` | UI form on Agentic Monitoring, or `POST /api/v1/agents/metrics` | graph engineer: evaluate, residual-test `FORMULA`s, propose `INFLUENCES`, write or reject |

## Running it

**Paid-acquisition story (Track B).**
1. Load `freecash_graph.cypher` and `paid_graph.cypher` (all scenarios) into Neo4j.
2. Start the backend with a fast agent clock, e.g. `CHEETAH_AGENT_TIME_SCALE=60`.
3. Jump the clock to the scenario day: trigger `sim_clock` with `{"set": "2026-10-14T09:00"}`
   (`POST /api/v1/agents/sim_clock/trigger` with body `{"payload": {"set": "2026-10-14T09:00"}}`).
4. Watch Agentic Monitoring: signals per slice, investigator hypotheses, the PM's discussions and
   brief, a P1 fix proposal for the pacing bug, `no_fix` / `monitor` for the others, nothing for the
   promo.

**Revenue story (Track A).**
1. `./start_backend.sh` auto-pushes and starts a `live_events` run.
2. The run pauses on each anomaly day; the incident appears in Identified, is routed to the
   segment that explains the gap, and ends Resolved with `hypotheses_proposed` or `inconclusive`.
   The trail graph shows each step. Resume to continue.

**New metric.** Submit, for example, `Cost per Conversion` = `spend / conversions` (unit `EUR`).
Look for the graph engineer's decision under Decisions: which `FORMULA` edges passed the residual
test, which `INFLUENCES` edges were proposed, and whether they were approved or deferred.

## What to point at

- Every number in a report traces to the graph or SQL ([principles](principles.md#3-numbers-come-from-data-not-from-the-model)).
- A hypothesis is labelled as one; "resolved" never means "root cause verified".
- The innocent change and the missing-data window produce *no* false alarm.
- Without `ANTHROPIC_API_KEY` the LLM-dependent agents say `not_configured` instead of pretending.

## Status

| Built | Stubbed / deferred |
|---|---|
| All scenarios above as data, the reacting agents, the UI panels | No single scripted demo that runs both stories end to end |
| | Track A has no notion of planned events: its `promo_surge` is flagged like a fault |
| | `paid_sim.py` needs `freecash_graph.py` (not on `main`) to regenerate `paid_graph.cypher` |
| | The real affiliate data has no continuous monitor yet |
