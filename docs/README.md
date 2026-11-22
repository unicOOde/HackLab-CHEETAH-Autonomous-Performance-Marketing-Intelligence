# CHEETAH technical documentation

CHEETAH is an agentic monitoring system for a performance-marketing marketplace. It keeps a graph
of how the business works, detects meaningful changes, investigates them on its own, and reports
with evidence.

Start with **[00 · Overview](00-overview.md)**. It covers the structure, tech stack, how the
components connect, and one anomaly followed end to end. Every other page goes into one part.

## Reading paths

**Judges** (in the order of the judging criteria):
1. [00 · Overview](00-overview.md): what it is and where each criterion lives
2. [40 · Graph](40-graph.md): metric graph and automatic extension (40%)
3. [30 · Forecasting & detection](30-forecasting-detection.md) → [50 · Agents](50-agents.md): autonomous monitoring and investigation (40%)
4. [70 · Demo](70-demo.md): fault, innocent change, new metric (20%)
5. [Principles](principles.md): the rules that keep the output honest

**Developers:**
1. [00 · Overview](00-overview.md) → [01 · Operations](01-operations.md) (get it running)
2. The category doc for the area you're changing (table below)
3. [Principles](principles.md) before changing behaviour, [Glossary](glossary.md) for terms
4. [`CLAUDE.md`](../CLAUDE.md) for coding conventions

## Document map

Each doc follows the same template: purpose & judging relevance → where it lives → how it works →
interfaces → configuration → **status (built / stubbed / deferred)** → deep-dive links.

| Doc | Owns | Main code |
|---|---|---|
| [00 · Overview](00-overview.md) | system diagram, seams between components, end-to-end thread, tech stack, repo map | — |
| [01 · Operations](01-operations.md) | venvs, `.env`, start commands, Compose, tests, health checks | `start_backend.sh`, `docker-compose.yml` |
| [10 · Data](10-data.md) | real data and its quirks, synthetic generator, anomaly catalog, paid simulator | `src/cheetah_synth/`, `paid_sim.py` |
| [20 · Backend](20-backend.md) | playback modes, run lifecycle, SQL schema, API, outbox/SSE | `backend/` |
| [30 · Forecasting & detection](30-forecasting-detection.md) | forecast bands, deviation detection, incident routing, pacing watcher algorithm | `backend/forecasting.py`, `core.close_day`, `agent_runtime/dispatcher.py`, `agents/pacing_watcher.py` |
| [40 · Graph](40-graph.md) | Neo4j layers, metric semantics, write rules | `graph_store.py`, `paid_metrics.py`, `*.cypher` |
| [50 · Agents](50-agents.md) | agent roster, runtime, bus protocol, LLM tiers | `agents/`, `backend/agent_runtime/`, `llm.py` |
| [60 · Frontend](60-frontend.md) | screens, client state, live vs illustrative panels | `frontend/` |
| [70 · Demo](70-demo.md) | scenarios mapped to the brief, how to run them | — |
| [Principles](principles.md) | cross-cutting design rules | — |
| [Glossary](glossary.md) | domain and system terms | — |

Rule for editing these docs: each fact has one owner (above). Other pages link to it instead of
repeating it.

## Source documents

These docs are an entry layer. They summarise and link to the existing material rather than
replacing it:

| Document | What it holds | Note |
|---|---|---|
| [`DESCRIPTION.md`](../DESCRIPTION.md) | the hackathon brief | |
| [`CLAUDE.md`](../CLAUDE.md) | conventions, schema, agent rules | most up to date |
| [`ARCHITECTURE.md`](../ARCHITECTURE.md) | Track A dataflows, state machine, event types | §1, §2 and §10 predate Track B's merge into `main` |
| [`AGENTS.md`](../AGENTS.md) | agent what/why, bus, models | uses older display names (Chronos, Pacer, …) |
| [`backend/README.md`](../backend/README.md) | backend smoke test, ingestion modes, reports endpoints | |
| [`agents/orchestration_plan.md`](../agents/orchestration_plan.md) | agent runtime design and decisions | design record |
| [`agents/product_manager.md`](../agents/product_manager.md), [`fix_playbook.md`](../agents/fix_playbook.md), [`graph_engineer.md`](../agents/graph_engineer.md) | role specs | |
| [`data/synthetic/README.md`](../data/synthetic/README.md) | generator process and results | |
| [`PRD/Master_PDR_v2.0.pdf`](../PRD/Master_PDR_v2.0.pdf) | requirements and acceptance criteria (Track A) | design authority |
| [`PRD/PRD_findings_extension.md`](../PRD/PRD_findings_extension.md) | dated decisions after the PDR | |
| [`PRD/architecture_diagrams/`](../PRD/architecture_diagrams/README.txt) | target architecture diagrams | target, not current state |
