# CHEETAH: Autonomous Performance Marketing Intelligence

Hackathon project. The full brief is in `DESCRIPTION.md`, so read it before any design decision.
In short: build a fully agentic monitoring system for a marketplace. It keeps a graph of how the
business works, decides what to watch, detects meaningful changes, investigates them on its own,
and reports with evidence. Nobody has to ask it questions.

Judging: 40% metric graph + automatic extension, 40% autonomous monitoring & investigation,
20% working demo (inject a fault + an innocent change, add a new metric, the system reacts alone).

Language: English everywhere (code, comments, CLAUDE.md, agent reports).

## Data

Real affiliate data from Freecash, October 2026 (`OCT26_*.csv`, read-only inputs):
- `OCT26_Transaction Report.xlsx - Sheet.csv`: ~3k transactions. Columns: Transaction ID, Click Time,
  Time (conversion), Click to Conversion Duration (s), AdSpace ID, Game Placement ID (= Ad),
  Transaction Type (sale|lead), Country, Amount, Commission, Payout, Margin.
- `OCT26_Performance-Report.xlsx - Sheet.csv`: one row per Ad × Country × Type, with Vertical,
  Category and Currency labels. `freecash_graph.py` checks the transaction sums against it.
- Money values use thousands separators (`"1,101.98"`), so parse them with `num()` from `freecash_graph.py`.
- One month of data only. Baselines must work without the previous month (e.g. a trailing 7-day
  weekday-adjusted median). Recent days are incomplete because conversions lag their clicks by
  up to more than 7 days.

## Graph: Neo4j entity graph (canonical)

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
  Neo4j. Bucket outcomes by conversion `Time` and cohort metrics (lag, instant share) by `Click Time`.
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

## Write rules (enforced by `graph_store.py`)

Agents **never write Cypher**. All writes go through `GraphStore`:
- Base nodes: only `ag_*` properties.
- Aggregate edges: totals are read-only. Agents may set `ag_*` (e.g. `ag_weight`). "Removing" one
  sets `ag_disabled = true`.
- Agent edges (any other UPPER_SNAKE_CASE type, weight in [-1, 1]) and agent nodes (`Outage`,
  `Hypothesis`, status `open|resolved|confirmed|rejected`) are free to create, change and delete.
- Every write creates a `(:Change)` audit node and can be reverted with `undo(change_id)`. Never
  modify `Change` nodes.
- `GraphStore.read()` rejects any non-read-only Cypher. Return scalars/maps (`properties(n)`), not node objects.

## Agents (`agents/`)

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

Still missing for the full loop (see DESCRIPTION.md): a continuous monitor (what to watch,
baselines, seasonality, completeness, planned events), dedup of repeated alerts
(persisting vs. resolved issues), metric onboarding, and the demo scenarios with fault injection.

### Agent conventions
- LLM calls go through `llm.py` (`claude_json`: one call → one JSON object matching a schema, with
  optional web search). The default model is `claude-opus-5-5` (override with `CLAUDE_MODEL`).
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

## Legacy (don't extend)

`service_down_agent.py` (networkx + Gemini) and `metrics_monitor_agent.py` are the old stack.
Don't add features there. Exception: `agents/outage_agent.py` still imports the research logic
(prompt, parsing, country/region normalisation) from `service_down_agent.ExternalOutageCampaignAgent`.
Move that into `agents/` before deleting the file.

## Setup & commands

- Neo4j runs on **Aura**: set `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD` (optionally
  `NEO4J_DATABASE`) in `.env` (template: `.env.example`, never commit `.env`). Without them,
  `graph_store.py` falls back to `neo4j-cli` with credential `test`.
- `ANTHROPIC_API_KEY` in `.env` for the agents.
- Python venv: `.venv/` (anthropic, neo4j, networkx).

```bash
.venv/bin/python freecash_graph.py > freecash_graph.cypher   # rebuild graph from CSVs (prints check to stderr)
neo4j-cli query --rw --credential <aura-cred> < freecash_graph.cypher   # load (statements split on ';')
.venv/bin/python run_agents.py                               # one full agent pass
.venv/bin/python run_agents.py --target Ad:0003WS            # also investigate specific nodes
.venv/bin/python run_agents.py --skip-outage --target "Category:Fashion & Apparel"
```

Neo4j skills (Cypher, modeling, GDS, Aura, drivers) live in `.agents/skills/`. Use them for
Cypher syntax and modeling questions.
