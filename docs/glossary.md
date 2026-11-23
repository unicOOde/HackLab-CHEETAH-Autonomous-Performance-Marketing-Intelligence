# Glossary

| Term | Meaning | Lives in | Doc |
|---|---|---|---|
| **Ad** | One Freecash game placement (`Game Placement ID`); "campaign" in UI language | Neo4j `(:Ad)`, SQL `game_placement_id` | [40-graph](40-graph.md) |
| **AdSpace** | The Freecash surface an ad runs on (`Freecash AdSpace ID`) | real data | [10-data](10-data.md) |
| **Aggregate edge** | Edge whose properties are sums over raw rows (`tx`, `amount`, `commission`, `payout`, `margin`); read-only for agents | Neo4j | [40-graph](40-graph.md) |
| **`ag_*`** | Prefix for every property an agent may write on base data | Neo4j | [40-graph](40-graph.md#write-rules) |
| **AgentTask** | One unit of agent work on an incident, with status (`queued`, `in_progress`, `completed`, `blocked_configuration`) | SQL `agent_tasks` | [20-backend](20-backend.md) |
| **Amount** | Customer net revenue excluding tax | SQL, real data | [principles](principles.md#9-data-contracts) |
| **Bus** | File-based message bus between agents, `var/agent_bus/` | filesystem | [50-agents](50-agents.md#communication) |
| **Change** | Audit node created by every `GraphStore` write; `undo(change_id)` reverts it | Neo4j `(:Change)` | [40-graph](40-graph.md) |
| **Command** | Queued simulation control (`start`, `pause`, `resume`, `stop`, `step`, `set_speed`) | SQL `commands` | [20-backend](20-backend.md) |
| **Commission / Payout / Margin** | Freecash's commission, the payout to the user, and their difference | real data, edge totals | [10-data](10-data.md) |
| **DailyAmount** | One immutable Amount per day per dataset | SQL `daily_amounts` | [20-backend](20-backend.md) |
| **Dataset id** | Version key for all inputs; immutable once written | SQL | [principles](principles.md#8-inputs-are-immutable-and-versioned) |
| **Discussion** | The product manager's per-issue fix deliberation (candidate, stances, gates, outcome) | bus `outbox/discussions` | [50-agents](50-agents.md) |
| **FORMULA** | Metric edge for an exact arithmetic decomposition; residual must be ≈ 0 | Neo4j | [40-graph](40-graph.md#metric-layer) |
| **ForecastReference** | The locked band (lower / point / upper) for day D, issued on D−1; what detection compares against | SQL `forecast_references` | [30-forecasting-detection](30-forecasting-detection.md) |
| **ForecastValue** | One day of a 7-day forecast outlook; informational, not used for detection | SQL `forecast_values` | [30-forecasting-detection](30-forecasting-detection.md) |
| **Hypothesis** | Investigator finding with confidence and quoted evidence, linked `ABOUT` its target | Neo4j `(:Hypothesis)` | [50-agents](50-agents.md) |
| **Incident** | A detected deviation with lifecycle `identified → in_progress → resolved` | SQL `incidents` | [30-forecasting-detection](30-forecasting-detection.md) |
| **INFLUENCES** | Metric edge for a possible cause, with weight and status `hypothesis` / `validated` / `rejected` | Neo4j | [40-graph](40-graph.md#metric-layer) |
| **Ingestion mode** | `preloaded_daily` (bulk load, reveal by day) or `live_events` (stream per event) | `Run.ingestion_mode` | [20-backend](20-backend.md#ingestion-modes) |
| **Metric** | Node with definition, unit and a `cypher_value` that evaluates it for any slice | Neo4j `(:Metric)` | [40-graph](40-graph.md#metric-layer) |
| **Outage** | External service disruption found by web research, `AFFECTS` countries | Neo4j `(:Outage)` | [50-agents](50-agents.md) |
| **Outbox** | Append-only per-run event stream behind SSE | SQL `event_outbox` | [20-backend](20-backend.md) |
| **PlannedEvent** | Known, intended change (e.g. promotion) that the watcher folds into its baseline; the "innocent change" | Neo4j `(:PlannedEvent)` | [30-forecasting-detection](30-forecasting-detection.md) |
| **Run** | One simulated playback of October 2026 over a dataset | SQL `runs` | [20-backend](20-backend.md) |
| **Signal** | Pacing watcher detection, deduplicated by issue key (metric + ad + country); linked `ON_METRIC`, `ABOUT` Ad, `IN_COUNTRY` | Neo4j `(:Signal)` | [30-forecasting-detection](30-forecasting-detection.md) |
| **SourceEvent** | One raw click or install row | SQL `source_events` | [20-backend](20-backend.md) |
| **Stance** | An agent's synchronous `support` / `object` / `abstain` on a proposal | `agents/stances.py` | [50-agents](50-agents.md) |
| **Track A / Track B** | Simulation + detection backend / Neo4j graph + Claude agents | repo | [00-overview](00-overview.md#2-two-tracks-one-system) |
