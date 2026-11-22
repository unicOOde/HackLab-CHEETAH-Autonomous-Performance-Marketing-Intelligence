# Graph Engineer Specification: Metric Onboarding into the Freecash Graph

**Audience:** whoever (human or the metric onboarding agent) turns a Product Manager metric
proposal (`agents/product_manager.md`) into Metric nodes and metric edges in Neo4j.
**Target system:** the Track B Neo4j graph (Aura), written only through the build or `GraphStore`.
**Objective:** add a new metric idempotently and wire it to the rest of the graph **without
knowing in advance what the graph looks like**. The contributor supplies a name, a definition and
a value expression. Where the metric sits in the graph is discovered and validated, never
hardcoded.

---

## 1. The graph we write into (verify, don't assume)

The graph changes over time (new builds, simulated telemetry, agent edges), so step 0 of every
onboarding run is **schema discovery** (section 2). As of 2026-11-22 the live graph contains:

| Kind | Present |
|---|---|
| Base labels (unique on `id`) | `Freecash, Ad, Country, Category, Vertical, ClickHour, LagBucket, TransactionType, Currency, Month, Day, Hour, PlannedEvent, Metric` |
| Data (aggregate) edges | `RUNS, CONVERTS_IN, CLICKED_AT, CONVERTS_AFTER, HAS_TYPE, ACTIVE_IN, IN_VERTICAL, IN_CATEGORY, PAID_IN, PAID_IN_HOUR, IN_DAY, APPLIES_TO` |
| Metric edges | `FORMULA` (exact decomposition), `INFLUENCES` (possible cause) |
| Agent labels | `Outage, Hypothesis, Signal` (+ `Change` audit log) |

Metric model (from `paid_metrics.py`):

```
(:Metric {id, definition, cypher_value, unit, domain})
(:Metric)-[:FORMULA    {formula, status: 'validated', weight: 1.0}]->(:Metric)    input -> output
(:Metric)-[:INFLUENCES {prior, mechanism, status: hypothesis|validated|rejected}]->(:Metric)   cause -> effect
```

`Metric.id` is the human-readable name (`'Conversion Rate'`, `'Revenue'`). It is the only key.
`'Revenue'` is the root that the pacing watcher walks towards (path to revenue).

### Hard rules

1. **Key on `id`, never invent a second key.** Every label has `REQUIRE n.id IS UNIQUE`. A node
   without `id` is invisible to `GraphStore.get`, `METRIC_EDGES_Q` and every agent.
2. **No new labels or relationship types for metrics.** Dimensions are already nodes (`Country`,
   `Category`, `ClickHour`, …) or edge properties (`PAID_IN_HOUR {country, device_os, channel}`).
   No `Dimension`, `HAS_BREAKDOWN`, `SLICED_BY`, `COMPONENT_OF`, `POSSIBLE_CAUSE`. If a genuinely
   new label or type is ever needed, add it to `BASE_LABELS` / `AGGREGATE_RELS` / `AGENT_LABELS` in
   `graph_store.py` **and** to the overlooker's `SYSTEM` prompt in the same change.
3. **No placeholders or fallback anchors.** If a link target doesn't exist, no edge is written. The
   missing link is recorded as an uncertainty and retried on the next re-link pass (section 6).
   Inventing a node to hang a metric on breaks the non-fabrication rule.
4. **No node per row.** Metric values are computed from sums over existing aggregate edges. A
   metric that needs data the edges don't carry needs a **build** change (a new summed property
   or edge), not new per-event nodes.
5. **Agents never write Cypher.** Section 4 is the only place raw write Cypher appears, and it
   belongs to the build. Agents go through `GraphStore` (section 5), which writes a `(:Change)`
   audit node for every write and supports `undo(change_id)`.
6. **Canonical vs. override properties.** The owner (build or onboarding) always `SET`s canonical
   fields (`definition`, `cypher_value`, `source_query`, `unit`, …), so a corrected definition
   actually lands on re-run. Human or agent overrides (alert thresholds, learned weights) live in
   `ag_*` properties, which the build never touches. Never use `ON CREATE SET` for canonical
   fields.
7. **Never use non-deterministic values in a MERGE pattern.** `MERGE (a)-[:X {created_at:
   datetime()}]->(b)` creates a new edge on every run. Merge on the key, then `SET`.
8. **One statement per block.** `neo4j-cli` splits scripts on `;` and variables don't survive the
   split. Each write is one self-contained `UNWIND $rows AS r MATCH/MERGE …` statement.

---

## 2. Step 0: schema discovery (read-only, through `GraphStore.read`)

Run these before deciding anything. The answers, not this document, determine what can be linked.

```cypher
CALL db.labels() YIELD label RETURN collect(label) AS labels
```
```cypher
CALL db.relationshipTypes() YIELD relationshipType RETURN collect(relationshipType) AS rels
```
```cypher
// existing metrics and their current wiring
MATCH (m:Metric)
OPTIONAL MATCH (m)-[e:FORMULA|INFLUENCES]->(n:Metric)
RETURN m.id AS id, m.unit AS unit, m.domain AS domain,
       collect({rel: type(e), to: n.id, status: e.status}) AS out
```
```cypher
// which summed properties a source edge actually carries (repeat per relationship type)
MATCH ()-[e:CONVERTS_AFTER]->() RETURN DISTINCT keys(e) AS props LIMIT 5
```

If a relationship type or property the metric needs is missing, **stop**: the metric is reported
as `blocked_missing_data` with the missing piece named. Don't write a partial metric.

---

## 3. Metric specification: conversion lag shares (replaces "click-to-action latency")

### 3.1 Why this metric, mapped to our data

The PM proposal wanted average click-to-action latency per `(country, ad)`, with an
instant-bounce companion so that "faster" isn't misread. Our real data has no post-click
engagement events, but it does have **click-to-conversion duration** (`Click to Conversion
Duration (s)` in the transaction report), aggregated by the build into
`(Ad)-[:CONVERTS_AFTER {tx, amount, commission, payout, margin}]->(LagBucket)` with buckets
`'<1m', '<1h', '<1d', '<7d', '>7d'` (exclusive).

Live totals (October 2026, 2,991 tx): `<1m` 128 · `<1h` 725 · `<1d` 662 · `<7d` 845 · `>7d` 631.

A 30-minute cap like the PM proposal's would discard about 70% of our conversions. So instead of
an average, we onboard three bucket shares. They are ratios of sums, so they slice and aggregate
correctly:

| id | value | unit | Purpose |
|---|---|---|---|
| `Fast Conversion Share` | tx(`<1m`,`<1h`) / tx | ratio | Main signal: landing/checkout friction pushes conversions out of the first hour (live: 0.285) |
| `Instant Conversion Share` | tx(`<1m`) / tx | ratio | Companion: a jump here is a tracking/attribution artefact or fraud, not "users got faster" (live: 0.043) |
| `Late Conversion Share` | tx(`>7d`) / tx | ratio | Completeness: how much of a click cohort's conversions arrive after a week (live: 0.211) |

`Fast Conversion Share` and `Instant Conversion Share` together play the role of "latency plus
bounce companion" from the PM proposal: a rise in Fast share with Instant flat is a real
speed-up, while a rise driven by Instant is suspect.

### 3.2 Limits of the current data (say these in every report on these metrics)

- **Slicing:** `CONVERTS_AFTER` exists per Ad only. These metrics slice by Ad, and through the Ad
  by `Category`/`Vertical`, **not by Country or hour**. Slicing by country requires a build change
  (`country` as a property on `CONVERTS_AFTER`, the same pattern as `PAID_IN_HOUR`).
- **No time axis yet:** there is no `Day` dimension on `CONVERTS_AFTER`. Baselines are
  cross-sectional (ad vs. its category's median, as `investigator_agent.py` already does for
  `instant_share`/`late_share`), not trailing-7-day. Daily time series arrive with the planned
  `ON_DAY` edges (CLAUDE.md, "Planned extensions").
- **Cohort maturity:** lag is a click-time cohort metric. Cohorts younger than 7 days
  undercount `>7d` by construction (`maturity_days = 7`). Never alert on immature cohorts.
- **Small samples:** most ads have < 10 tx. Below `min_sample` (30 tx) the value is reported with
  low confidence and never triggers an alert.

### 3.3 Monitoring configuration (canonical properties on the Metric node)

| Property | Fast | Instant | Late |
|---|---|---|---|
| `min_sample` / `min_sample_unit` | 30 / `tx` | 30 / `tx` | 30 / `tx` |
| `maturity_days` | 1 | 0 | 7 |
| `baseline` | `category_median` | `category_median` | `category_median` |
| `companions` | `['Instant Conversion Share']` | `['Fast Conversion Share']` | `[]` |
| `slice_by` | `['Ad', 'Category', 'Vertical']` | same | same |

Overrides (e.g. a tuned threshold) go into `ag_alert_threshold`, `ag_min_sample`, …, which are
preserved across rebuilds.

### 3.4 How the value is defined: `source_query` + `cypher_value`

Existing metrics carry `cypher_value`, an expression over summed edge totals (implicitly from
`PAID_IN_HOUR`). Lag metrics come from a different edge, so onboarded metrics add
**`source_query`**: a read-only Cypher query that returns **sums per slice** (never pre-computed
ratios), with `cypher_value` evaluated over those sums. Any slice, and the global value, is then
`cypher_value` over the summed rows. `source_query` must pass `graph_store.is_read_only`.

```cypher
// source_query shared by all three lag metrics. $ad / $category are optional slice filters (null = all)
MATCH (a:Ad)-[e:CONVERTS_AFTER]->(l:LagBucket)
WHERE ($ad IS NULL OR a.id = $ad)
  AND ($category IS NULL OR EXISTS { (a)-[:IN_CATEGORY]->(:Category {id: $category}) })
RETURN a.id AS ad,
       sum(e.tx) AS tx,
       sum(CASE WHEN l.id = '<1m' THEN e.tx ELSE 0 END) AS instant_tx,
       sum(CASE WHEN l.id IN ['<1m', '<1h'] THEN e.tx ELSE 0 END) AS fast_tx,
       sum(CASE WHEN l.id = '>7d' THEN e.tx ELSE 0 END) AS late_tx
```

`cypher_value`: `fast_tx / tx`, `instant_tx / tx`, `late_tx / tx`.

---

## 4. Build path: Cypher emitted by the build (curated metrics only)

Use this when the metric is owned by the codebase, as the 14 paid-acquisition metrics are. Add the
metrics to a registry module and emit them the way `paid_metrics.emit_cypher` does
(`UNWIND [...] AS r …`, one statement each). The emitted Cypher is equivalent to:

```cypher
CREATE CONSTRAINT IF NOT EXISTS FOR (n:Metric) REQUIRE n.id IS UNIQUE;
```

```cypher
// 1. Upsert metrics: canonical fields always SET, ag_* never touched
UNWIND $metrics AS r
MERGE (m:Metric {id: r.id})
SET m.definition    = r.definition,
    m.cypher_value  = r.cypher_value,
    m.source_query  = r.source_query,
    m.unit          = r.unit,
    m.domain        = r.domain,
    m.origin        = 'build',
    m.min_sample    = r.min_sample,
    m.min_sample_unit = r.min_sample_unit,
    m.maturity_days = r.maturity_days,
    m.baseline      = r.baseline,
    m.companions    = r.companions,
    m.slice_by      = r.slice_by,
    m.created       = coalesce(m.created, toString(datetime()));
```

```cypher
// 2. Validated FORMULA edges only (residual ≈ 0 was checked before emitting).
//    MATCH, not MERGE, on the endpoints: a missing endpoint means no edge, never a placeholder.
UNWIND $formula_edges AS r
MATCH (a:Metric {id: r.src}), (b:Metric {id: r.dst})
MERGE (a)-[e:FORMULA]->(b)
SET e.formula = r.formula, e.status = 'validated', e.weight = 1.0;
```

```cypher
// 3. INFLUENCES edges: prior and mechanism are canonical. Status is preserved because validation
//    may have promoted or rejected the edge since.
UNWIND $influence_edges AS r
MATCH (a:Metric {id: r.src}), (b:Metric {id: r.dst})
MERGE (a)-[e:INFLUENCES]->(b)
SET e.prior = r.prior, e.mechanism = r.mechanism, e.status = coalesce(e.status, 'hypothesis');
```

Each block is a single statement, the endpoints of an edge are only ever `MATCH`ed, and no
timestamp appears in a MERGE pattern. Re-running is a no-op apart from refreshed canonical fields.

---

## 5. Agent path: metric onboarding agent through `GraphStore` (to be built)

Metrics submitted at runtime (the demo's "add a new metric, the system reacts alone") go through
`GraphStore`. **Today `GraphStore` blocks this**: `Metric` is a base label (agents may only set
`ag_*` on it), and `FORMULA`/`INFLUENCES` are in `AGGREGATE_RELS` (agents may not add them). Two
new methods are needed. Both validate their input, write a `Change` node and can be undone:

```python
store.add_metric(id, definition, unit, cypher_value, source_query, domain,
                 config: dict, reason) -> change_id
    # creates a Metric with origin='onboarding'; refuses if id exists with origin='build'
    # (build-owned metrics are never redefined by agents); runs source_query read-only first
    # and refuses if it fails, returns no rows, or the value is out of range for the unit.

store.link_metrics(src_id, rel, dst_id, props, evidence: list[str], reason) -> change_id
    # rel in {'FORMULA', 'INFLUENCES'}; both endpoints must exist (no placeholders);
    # FORMULA requires props.formula + a residual check result; INFLUENCES starts at
    # status='hypothesis' unless the evidence passed the independent reviewer.
```

`undo` for `create` already `DETACH DELETE`s the node, which removes its edges too. The
overlooker's `SYSTEM` prompt must describe the new Metric properties (`source_query`, `origin`,
monitoring config).

### 5.1 Onboarding procedure

1. **Discover** (section 2). Abort with `blocked_missing_data` if the source edge or properties
   are absent.
2. **Evaluate:** run `source_query` globally and per slice, compute `cypher_value` from the sums.
   It must return numbers. A ratio must lie in [0, 1].
3. **Find FORMULA links:** parse `cypher_value` and the definitions of existing metrics. A
   `FORMULA` edge is proposed only when **every** input is an existing Metric and the residual
   `|lhs - rhs| / max(|lhs|, ε)` is ≈ 0 (≤ 1e-6) **globally and in every slice with ≥
   `min_sample`**. A partial decomposition is not a FORMULA edge.
4. **Find INFLUENCES candidates:** correlate the new metric with every other metric over a shared
   axis that exists *at that moment* (cross-sectional per Ad today, daily once `ON_DAY` exists),
   then ask Claude (`llm.claude_json`) for a mechanism. Each candidate needs a mechanism, the
   evidence queries and their numbers. The independent reviewer re-runs the evidence and must
   approve it, as in `graph_overlooker.py`. Approved edges are written with `status='hypothesis'`
   and a `prior` equal to the reviewer-checked strength. `validated` only comes later, from
   monitoring evidence.
5. **No match is a valid outcome.** A metric without links is still monitored. Its report says it
   isn't connected to the path to `'Revenue'` yet, and the next re-link pass tries again.
6. **Unknown never acts:** `LLMUnavailable`, failed queries or unparsable answers produce no edge
   and are recorded as an uncertainty.

### 5.2 Expected candidates for the lag metrics (to be confirmed by step 4, not hardcoded)

These are what we expect discovery to propose. If the evidence doesn't support one, it isn't
written.

- `Instant Conversion Share` → `Data Freshness & Tracking Health` (INFLUENCES): a sudden rise in
  instant conversions is a classic tracking or attribution artefact.
- `Late Conversion Share` → `Conversions` / `Revenue` (INFLUENCES): with a high late share, recent
  days look low until their cohorts mature, which is a completeness explanation, not a fault.
- `Fast Conversion Share` → `Conversion Rate` (INFLUENCES): friction that delays conversions
  usually also loses some of them.

Caveat: `Conversions`, `Revenue` and `Conversion Rate` are computed from the **simulated**
`PAID_IN_HOUR` telemetry, while the lag metrics come from **real** transactions. Cross-domain
edges stay `hypothesis` until both sides share a time axis.

---

## 6. Re-link pass (dynamic graph)

Because links are discovered rather than listed, a re-link pass is simply onboarding steps 3–4
re-run for every `origin='onboarding'` metric. Trigger it when:

- a new Metric node appears (another contributor onboarded one), or
- a new relationship type or summed property appears in schema discovery (e.g. `ON_DAY`, or a
  `country` property on `CONVERTS_AFTER`), which unlocks new slices or a time axis.

Existing edges are never duplicated (`link_metrics` refuses if an edge exists; reweighting goes
through `set_edge_props`, also audited). Edges whose evidence no longer holds are set to
`status='rejected'`, not deleted, so the history stays readable.

---

## 7. Post-onboarding validation (read-only)

```cypher
// The metric exists, is keyed on id, and shows its wiring
MATCH (m:Metric) WHERE m.id IN $ids
OPTIONAL MATCH (m)-[o:FORMULA|INFLUENCES]->(out:Metric)
OPTIONAL MATCH (inc:Metric)-[i:FORMULA|INFLUENCES]->(m)
RETURN m.id AS id, m.origin AS origin, m.source_query IS NOT NULL AS has_source,
       collect(DISTINCT type(o) + ' -> ' + out.id + ' (' + coalesce(o.status, '') + ')') AS outgoing,
       collect(DISTINCT inc.id + ' -> ' + type(i)) AS incoming
```

```cypher
// Is there a path to Revenue (informational; no path is allowed, see 5.1 step 5)
MATCH (m:Metric) WHERE m.id IN $ids
OPTIONAL MATCH p = shortestPath((m)-[:FORMULA|INFLUENCES*..6]->(:Metric {id: 'Revenue'}))
RETURN m.id AS id, [n IN nodes(p) | n.id] AS path_to_revenue
```

```cypher
// Hygiene: nothing unlabelled, no Metric without id, no duplicate metric edges
CALL () { MATCH (n) WHERE size(labels(n)) = 0 RETURN count(n) AS unlabelled }
CALL () { MATCH (m:Metric) WHERE m.id IS NULL RETURN count(m) AS metric_without_id }
CALL () { MATCH (a:Metric)-[e:FORMULA|INFLUENCES]->(b:Metric)
          WITH a, b, type(e) AS t, count(*) AS c WHERE c > 1 RETURN count(*) AS duplicate_edges }
RETURN unlabelled, metric_without_id, duplicate_edges
```

All three hygiene counts must be 0.

## 8. The graph engineer in fix discussions

The product manager's fix discussions (`agents/fix_playbook.md`, `orchestration_plan.md` §5.5)
consult the graph engineer through the same `review()` hook protocol. Its stance is
deterministic and graph-only:

- **Is the cause → metric path `validated`, or only a `hypothesis`?** A fix built on a hypothesis
  edge gets an `object` stance with that evidence.
- **Side effects:** which downstream metrics the lever's metric reaches through `INFLUENCES` /
  `FORMULA` edges. These are named in the fix and subtracted in the impact gate.

A discussion that ends `graph_only` (e.g. "validate `Data Freshness & Tracking Health` →
`Conversions`") reaches the graph engineer as a normal `proposal` and goes through the usual
hear-out, reviewer and decision rule. The graph engineer never proposes or executes business fixes.

## 9. What the monitoring and investigation side must do with these metrics

1. **Completeness gate before any alert:** skip cohorts younger than `maturity_days`, and check
   `Data Freshness & Tracking Health` for the same window when it is linked.
2. **Read companions together:** an alert on `Fast Conversion Share` must quote
   `Instant Conversion Share` for the same slice. A move driven by instant conversions is
   reported as a probable tracking artefact, not a UX change.
3. **Sample gate:** below `min_sample`, report the value with low confidence and never alert.
4. **Traversal:** the watcher already reads `FORMULA|INFLUENCES` edges between Metrics
   (`METRIC_EDGES_Q`), so new edges join the root-cause walk with no code change.
   `hypothesis` edges are explanations to check, not established causes.

---

## 10. Rejected PM candidate: "For You" feed in-stock click share

The proposal assumed `feed_impressions`, `inventory_snapshots`, `model_version` and model or
deployment events. None of these exist in the Freecash data or in the graph (no feed surface, no
inventory, no ranking model). The metric can't be evaluated (section 5.1 step 2 fails), so it is
**not onboarded**. Creating placeholder anchors for it would fabricate structure. Revisit only if
a data source with those fields is added. It would then come in through the build (new aggregate
edges) and this onboarding procedure, like any other metric.
