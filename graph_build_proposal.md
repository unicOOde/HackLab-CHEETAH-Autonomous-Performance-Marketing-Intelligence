# Proposal: building the real graph from OCT26 data in `graphwalk`

## 0. Core idea
Don't hand-code a new graph. Plug our data into the machinery `graphwalk` already has:
- `MetricGraph`: metrics with `compute` + `decomposition`; formula edges derived automatically
- `FactStore`: DuckDB, hourly sums of columns per dimension
- `Baseline` / `Monitor` / `Investigator`
- `extend.py`: onboarding that links new metrics with lag correlation + LLM semantics

The graph is **generated** from three things, so it is dynamic by construction:

1. **Data schema**: columns and dimensions are discovered from the CSV, not hardcoded.
2. **Entity registry**: one declarative spec per entity (id, compute, decomposition). Formula edges come out of it automatically.
3. **Learned edges**: causal and signature edges start as `hypothesis` and become `validated` / `rejected` from evidence. Weights are updated online.

Re-ingesting a new month (NOV26) or adding an entity rebuilds or extends the graph with no manual wiring.

---

## 1. Make every entity a "sum of columns" (the key trick)
`graphwalk` requires `compute` = an arithmetic expression over **summed** warehouse columns. Medians, distinct counts and "per click" values don't fit that directly. So the ingest step turns each transaction row into **additive indicator columns**, and every entity from `data_entity_ideas.md` becomes a ratio of sums.

### Fact table `conversions`
Grain: hour × country × placement × vertical × category × type.

| Column | Per-row value | Used by |
|---|---|---|
| `conv` | 1 | E5 Conversions |
| `sales`, `leads` | 1 if sale / lead | E6, E7, E20 |
| `amount`, `commission`, `payout`, `margin` | raw | E1–E4 |
| `sale_amount` | amount if sale | E8 AOV |
| `lag_h` | click→conversion hours | mean lag (replaces median; additive) |
| `log_lag` | ln(1 + lag_s) | robust mean lag (geometric) |
| `instant` | lag < 60 s | B1 |
| `subsec` | lag < 1 s | B3 |
| `late` | lag > 7 d | D4 |
| `batch` | Time in 00:00:00–00:00:10 | D1 |
| `zero_amt`, `zero_comm` | amount = 0 / commission = 0 | D5, D6 |
| `high_value` | sale and amount > rolling p90 | B5 |
| `first_of_click` | 1 for the first tx of a (click time, placement, country) | E18 converted clicks |
| `id_span` | ID delta to previous tx (allocated per row) | D7 network share |

Then, for example:
- `aov = sale_amount / sales`
- `instant_share = instant / conv`
- `multi_conv = 1 - first_of_click / conv`
- `mean_lag = lag_h / conv`
- `network_share = conv / id_span`

Everything stays additive, so it is **sliceable by every dimension for free**, and the existing contribution analysis (`formula.contributions`) works.

### Breadth table `coverage`
Distinct counts (Active Offers, Country / Offer Breadth) are not additive. Use a second table at grain hour × country × placement:
- `expected_active`: 1 if this pair converted in at least k of the previous 14 days
- `active`: 1 if the pair converted in this bucket

Then:
- `offer_breadth = active / expected_active` (sliced by placement)
- `country_breadth = active / expected_active` (sliced by country)
- `active_offers = active` (sliced by nothing)

All of these are ratios of sums again. This turns "Silent Offers" into an expression too.

## 2. Dynamic dimensions (auto-discovered)
At ingest, profile every non-numeric column:
- **Cardinality < 200** → dimension. Country, Placement, Type, Vertical and Category qualify. AdSpace (one value) is dropped automatically.
- **Hierarchies via functional dependency**: if A → B holds 100% (checked: placement → vertical and placement → category, 0 violations), B is a parent level of A. This gives drill-down paths `vertical → placement` and `category → placement`. Category ↛ vertical (8 categories span several verticals), so no hierarchy is created there. That's correct behaviour, not hardcoding.
- **Derived dimensions** from rules: region from country, "legacy" from a `(Old)` suffix, lag bucket, amount bucket.

`TABLE_DIMS` / `DIM_VALUES` in `config.py` are then filled from this profile instead of being constants.

## 3. Entity registry → graph
Replace `seed.py` with a data-specific registry (`seed_affiliate.py` or YAML). Example:
```
margin:        compute: margin                 decomposition: commission * (1 - payout_share)
commission:    compute: commission             decomposition: gmv * comm_rate
gmv:           compute: amount                 decomposition: sales * aov
sales:         compute: sales                  decomposition: active_offers * sales_per_offer
aov:           compute: sale_amount / sales
comm_rate:     compute: commission / amount
payout_share:  compute: payout / commission
instant_share: compute: instant / conv
...
```
- `MetricGraph.add_metric` turns every decomposition into formula edges (weight 1.0).
- **North star** = `margin` (or `gmv`) as a config value, not a constant.
- Formulas are **validated at build time** (this machinery already exists in `extend.py` step 3): recompute globally and per segment, and reject any entity whose residual is ≠ 0.

## 4. Three edge types, each with its own source

| Edge type | Where it comes from | Weight |
|---|---|---|
| `formula` | the decomposition string | 1.0, exact; contributions via Shapley split |
| `causal` | seeded priors (e.g. `instant_share → sales`, `offer_breadth → sales`) **plus** lag-correlation discovery from `extend.py`, run over all metric pairs per slice | prior × evidence, updated online (below) |
| `context` / **signature** | section 6 of the ideas file, as hypothesis nodes (`H_isp_outage`, `H_payment_fail`, `H_landing_slow`, `H_tracking_loss`, `H_advertiser_pause`, `H_release`, `H_fraud`, `H_data_incomplete`) | how many conditions currently hold (0–1) |

A signature node is a **rule over entities in a scope**, for example:
```
H_payment_fail(scope):
  sales.z < -3 AND leads.z > -1 AND high_value_share.z < -2 AND multi_conv.z > 2
```
- Each condition is an incoming edge from that entity to the hypothesis node.
- The investigator scores hypothesis nodes as it walks. They're never computed from data of their own, so the rule "every entity is resolvable from our data" holds.

### Dynamic weights
After each investigation, update every followed causal edge with what it actually explained:

  w_new = (1 − α)·w_old + α · (explained_share × sign_agreement), with α ≈ 0.2

Edges that keep explaining nothing decay towards 0 and become `rejected` below a threshold. New edges found by discovery enter as `hypothesis`. `recompute_importance()` already turns weights into monitoring priority.

## 5. Time & baseline adjustments for this data
- **Volume**: ~4 tx/hour, too sparse for hourly z-scores. Use a configurable `GRAIN` (6 h or 1 day; `WEEK` derived from it). Slices with < N expected events are skipped (Poisson noise). The baseline already scales by √volume.
- **History**: only one month, so the "same hour last 3 weeks" baseline can't work for October. Fall back to a trailing 7-day weekday-adjusted median, switching to the 3-week method once history exists.
- **Two time axes**: bucket outcomes by conversion `Time`, and bucket cohort entities (lag, instant share, converted clicks) by `Click Time`.
- **Lag completeness (D3)**: from the empirical lag CDF. It feeds the existing `completeness()` / `incomplete` status, so recent buckets aren't falsely alerted.

## 6. Live / dynamic loop
- **Replay source** replaces `sim/simulator.py`: streams OCT26 rows in conversion-time order into the `FactStore` at a chosen speed, so the existing Runtime / SSE / UI run unchanged.
- **Fault injection on replay** (scenarios): drop all DE rows for 12 h (ISP), drop sales but keep leads (payment), silence `00008F` (tracking), stretch lags for one offer (slow landing page). Innocent change: weekend dip / batch-posting burst.
- **New metric at runtime**: `POST /api/metrics` with an expression over the columns above. The `extend.py` onboarding places it, so it needs no manual edge wiring.
- **New data file**: the ingest adapter re-profiles columns. New numeric columns are offered to onboarding as candidate metrics, and new categorical columns as new dimensions.

## 7. Optional: semantic embedding of nodes
Embed each node's `name + definition` (text embeddings, or Claude as `extend.py` already does) and store the vector on the node. Uses:
- Faster candidate selection when onboarding a new metric (nearest semantic neighbours are correlation-tested first)
- Matching signature descriptions to entities
- Search in the UI

This is secondary: structure and weights stay explicit and explainable.

## 8. Build steps (when we implement)
1. `ingest/affiliate.py`: CSV → `conversions` + `coverage` tables (indicator columns, P join, dimension profile)
2. `config.py`: `GRAIN`, north star, dimensions from the profile
3. `graph/seed_affiliate.py`: entity registry + seeded causal priors + signature nodes
4. `sim/replay.py` + scenarios for the faults above
5. Hypothesis-node scoring + online weight update in `investigate/agent.py`
6. Tests: formula residuals = 0, reconciliation with the Performance Report, and a replay of Oct 7–25 that must surface `offer_breadth` / `active_offers` as the driver
