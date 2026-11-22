# Design principles

Rules that hold across every component. Category docs link here instead of restating them. Each
rule names where it is enforced, so a change that breaks it fails a test or a guard.

## 1. Never fabricate

When an integration is missing (TabPFN, Claude, Neo4j, sandbox actions, replay), the system reports
an explicit state instead of a plausible-looking result:

| Missing piece | What the system shows |
|---|---|
| Forecast provider | `forecast.unavailable` with a reason (`warm_up`, `not_configured`, `provider_error`) |
| Investigator / evidence adapters | `AgentTask.status = 'blocked_configuration'` |
| Graph context in the API | `GET /context/graph` → `{status: 'unavailable'}` |
| Replays | `POST /runs/{id}/replays` → `409` |
| Agent dependencies (Neo4j, API key, import) | agent status `not_configured` with the reason, no run |

Enforced by `tests/test_backend.py::test_empty_run_does_not_fake_data` and the availability checks
in `backend/agent_runtime/builtin.py`.

## 2. Unknown never acts

`LLMUnavailable`, missing keys, unparsable model output and failed searches become status
`unknown`. They are recorded as an uncertainty and never raise a flag, resolve a signal, or trigger
a fix. Example: the pacing watcher skips all checks on a slice whose tracking completeness is low,
and never resolves its open signals while data is degraded.

## 3. Numbers come from data, not from the model

Every number in a hypothesis, brief or report must exist in the data the agent read. The
investigator drops evidence lines that quote a number not present in its context; the product
manager keeps an LLM narrative only if every number in it appears in the brief's own data.
Detection (pacing watcher, deviation detector) and fix decisions (the PM's gates) use no model.

## 4. Hypothesis ≠ cause, symptom ≠ first cause

Agents label what they found: `hypothesis` vs `validated`, and downstream symptom vs possible first
cause. A resolved incident says `hypotheses_proposed` or `inconclusive`, never "root cause found".
Small samples mean low confidence, stated explicitly.

## 5. Recommend, never execute

Fixes are recommendations with expected impact, ranked per country / ad / category. Nothing in the
system changes bids, budgets or campaigns. "No fix" is the default and expected outcome.

## 6. The graph has one write path

Agents never write Cypher. All writes go through `GraphStore`, which allows only `ag_*` properties
on base data, keeps aggregate totals read-only, and records every write as an undoable `(:Change)`.
`GraphStore.read()` rejects write Cypher. Details: [40-graph](40-graph.md#write-rules).

## 7. Aggregate, don't explode

No node per click or transaction. Rows are summed into edge totals, so graph size scales with
ads × buckets, not with rows. Every extension (days, hours, slices) follows this rule.

## 8. Inputs are immutable and versioned

Daily amounts, source events, forecast references and outlooks are insert-only per `dataset_id`;
a second insert returns `409`. A forecast reference must be issued the day before the day it
covers. To change data, create a new dataset id.

## 9. Data contracts

- **IDs are strings** (`Click ID`, `Transaction ID`, `Freecash ID`, `Freecash AdSpace ID`,
  `Game Placement ID`): some have leading zeros or letters.
- **`Amount`** is customer net revenue excluding tax, not spend or profit. Daily aggregates and
  transaction rows are never added together.
- **Provenance is tagged**: `source_kind` = `historical` / `synthetic` / `live_computed`, and
  synthetic forecast bands say `synthetic_naive_baseline`, so simulated numbers can't pass for real.

## 10. Use the lightest model that fits

Every LLM call names a tier (`small` / `medium` / `large`). Opus is reserved for reasoning that
leads to an action or a graph write. See [50-agents](50-agents.md#llm-access).

## 11. Async control plane

The UI never advances a simulation or runs an agent directly. It queues commands or writes
trigger files; the worker does the work; results come back through the outbox (SSE) or bus files.
