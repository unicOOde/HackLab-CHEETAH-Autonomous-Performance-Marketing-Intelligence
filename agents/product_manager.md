# Product Manager Specification: Metric Discovery & Proposals

**Role:** decide **what is worth watching**. The Product Manager (human, or a future agent run
from this spec) looks at the data that exists right now, finds candidate metrics with a real
business lever behind them, measures them, and hands each one over as a structured proposal.
**Downstream:** `agents/graph_engineer.md` onboards each proposal. It validates the metric,
discovers its place in the graph and writes it through `GraphStore`.
**Not this role's job:** deciding where a metric sits in the graph, which edges it gets, or what
it causes. Per CLAUDE.md, *the contributor never specifies its place in the graph*.

```
Product Manager ──proposal──▶ Graph Engineer onboarding ──Metric node + edges──▶ monitor / investigator
 (what to watch, why,         (evaluate, FORMULA residual,                        (baselines, alerts,
  measured numbers)            INFLUENCES discovery, review)                       root-cause walk)
```

---

## 1. What exists to work with (discover, don't assume)

The graph changes over time, so every discovery run starts with the schema queries in
`graph_engineer.md` section 2 (labels, relationship types, existing Metric ids, the summed
properties on each aggregate edge). As of 2026-11-22:

| Source | What it has | Grain / limits |
|---|---|---|
| `(Freecash)-[:RUNS]->(Ad)` and `CONVERTS_IN` (Country), `CLICKED_AT` (ClickHour 0–23), `CONVERTS_AFTER` (LagBucket), `HAS_TYPE` (sale/lead), `ACTIVE_IN` (Month) | `{tx, amount, commission, payout, margin}` summed from **real** transactions | Month totals per Ad × one dimension. No day axis yet, and no combined slices such as Ad × Country × Hour |
| `IN_CATEGORY`, `IN_VERTICAL`, `PAID_IN` | Ad attributes | — |
| `(Ad)-[:PAID_IN_HOUR {country, device_os, channel, auctions, impressions, clicks, spend, conversions, revenue, bid_mult, tracking, budget, planned_share}]->(Hour)` | **Simulated** paid telemetry (`paid_sim.py`) | Hourly, 6 ads × 2 countries × 2 devices, 14 days |
| `PlannedEvent -[:APPLIES_TO]->(Ad, Country)` | Planned promotions | — |
| 14 `Metric` nodes (`paid_metrics.py`) | Paid-acquisition funnel down to `'Revenue'` | Computed from `PAID_IN_HOUR` |
| `data/Clicks/*.csv` (31 days), `data/OCT26_*.xlsx` | Real raw data | **Not in the graph** beyond the transaction aggregates above. Clicks aren't loaded at all |

Volume reality check (live graph): 104 ads, 2,991 transactions, 69 countries, €13,023 commission.
Only **17 ads have ≥ 30 tx**, and DE (1,677 tx) and US (642) carry most of the volume. Any metric
that needs hundreds of events per hour per slice is not feasible on the real data.

---

## 2. Rules

1. **Measure, never invent.** Every number in a proposal comes from a query you ran (graph read
   or raw file), and the query is included. No estimated coverage, latency or lift figures. If
   something can't be measured, write `unknown`.
2. **Ratios of sums.** Define a metric as an expression over **sums** of edge properties
   (`margin / commission`, not `avg(margin_rate)`), so every slice and the global value use the
   same definition. Guard zero denominators: 9 ads have `commission = 0`, and an unguarded ratio
   returns NaN, which breaks the `neo4j-cli` JSON output.
3. **Only data that exists.** A metric whose inputs aren't in the graph is not proposed for
   onboarding. It becomes a `needs_build` proposal naming the exact build change (which source
   file, which new summed property or edge), or a `reject` if the data doesn't exist anywhere.
4. **No placement, no edges, no ids of other metrics as parents.** You may mention which existing
   metrics you *expect* it to relate to and why, as a hint for the onboarding agent's discovery.
   The onboarding agent decides.
5. **Not a duplicate.** Check existing Metric ids and their `cypher_value`/`source_query`. A
   metric that is algebraically the same as an existing one (e.g. `1 - Instant Conversion Share`
   restated) is not new.
6. **Name the lever.** Every proposal points to at least one action from `FIXES.md` that someone
   would take when the metric moves: more ads where payout is low, button/landing changes when
   conversion time rises, price changes, reverting the last release, pausing on outage. A metric
   that moves but changes no decision is a vanity metric. If the only explanations are "not a
   fix" causes (bad internet, payment not accepted, app crash), say so; it can still be worth
   watching for diagnosis.
7. **One month of data.** Baselines can't use the prior month. Propose baselines that work inside
   October: peer comparison (ad vs. category median), or a trailing 7-day weekday-adjusted median
   once a day axis exists. Lag-dependent metrics need a cohort maturity window (conversions arrive
   up to and beyond 7 days after the click).
8. **Real vs. simulated.** Say which side a metric is computed from. Metrics on `PAID_IN_HOUR`
   are simulated telemetry and must never be presented as real performance.

---

## 3. Discovery process (the three lenses)

Run all three for every candidate. Each lens produces numbers that go into the proposal.

**Data Engineer: does the data hold up?**
- Which edge and which properties feed it? Do they exist on every relevant edge (`keys(e)`)?
- Coverage: how many ads/countries/categories have a non-null value, and how many have zero
  denominators?
- Completeness: does the metric depend on late-arriving conversions? How mature must a cohort
  be? Is the recent tail incomplete?
- For `needs_build` proposals: the exact source columns in the raw files, and the join keys
  (`Freecash ID`, `Freecash AdSpace ID`, `Game Placement ID`, `Country`, kept as strings).

**Data Analyst: where does it vary?**
- Global value, then the spread across every available slice (Ad, Country, Category, Vertical,
  ClickHour, TransactionType), restricted to slices with ≥ `min_sample` tx.
- Where does commission or margin concentrate in the extremes? That's where an alert would be
  worth money.

**Data Scientist: is it a signal?**
- Sample sizes: how many slices clear the minimum? Below it, report low confidence.
- Is the spread structural (a fixed contract term, e.g. a constant payout share) or behavioural?
  A metric that is constant by contract only detects contract changes, which is still useful
  but should be said explicitly.
- Expected relations to existing metrics, with the reasoning (a hint for onboarding, rule 4).
- When a time axis exists: day-to-day noise vs. the size of change worth alerting on.

---

## 4. Output: the proposal record

One record per candidate. Field names match `GraphStore.add_metric` in `graph_engineer.md`
section 5, so an accepted proposal can be passed straight through.

```yaml
verdict: propose | needs_build | reject
id: "Margin Rate"                  # human-readable, unique, like existing Metric ids
definition: "Freecash margin as a share of commission earned"
unit: ratio                        # ratio | count | EUR | seconds | x
domain: affiliate_economics        # real data; paid_acquisition = simulated
source_query: |                    # read-only Cypher returning SUMS per slice, optional $filters
  ...
cypher_value: "margin / commission"
config:                            # monitoring settings, become canonical Metric properties
  min_sample: 30
  min_sample_unit: tx
  maturity_days: 0
  baseline: category_median
  companions: []
  slice_by: [Ad, Country, Category, Vertical]
evidence:                          # every line quotes a measured number + the query that produced it
  - "..."
lever: "..."                       # FIXES.md action(s) this metric drives
expected_relations: "..."          # hint only; onboarding decides
uncertainties: ["..."]
build_change: null                 # needs_build only: exact source columns + new edge/property
```

Hand off only `verdict: propose` records to onboarding. `needs_build` records go to whoever owns
the build (`freecash_graph.py`). `reject` records are kept so the same idea isn't re-proposed.

---

## 5. Worked examples (numbers measured on the live graph, 2026-11-22)

### 5.1 `Margin Rate`: **propose**

- **Source:** `RUNS` (per ad) and `CONVERTS_IN` (per ad × country), real data.
- **Evidence:**
  - Globally €7,303.06 margin / €13,023.26 commission = **0.561**.
  - Of the 16 ads with ≥ 30 tx and commission > 0, the median is **0.300**, and the range runs
    from **0** to **0.951**.
  - By category (≥ 30 tx) it is bimodal: most categories sit at **0.30** (payout = 70% of
    commission), while Onlineshop (**0.951**, 1,190 tx), Food Delivery & Takeaway (**0.908**) and
    Hotels & Accommodation (**0.906**) keep almost all of it.
  - By country: DE **0.822** vs. US **0.489**, ZA **0.301**, GB **0.325**.
- **Data Scientist note:** the 0.30 cluster looks like a fixed contract split, so a move away from
  0.30 for an ad in that cluster means its payout terms changed. That's a strong, low-noise
  signal. The ~0.9 ads have low payout, which is the `FIXES.md` "more ads where payout is low"
  lever.
- **Data Engineer note:** `commission - payout - margin` over all ads = **−€8.82** (0.07% of
  commission), probably per-row rounding. This is relevant if onboarding tries
  `Margin = Commission - Payout` as a FORMULA: it would fail a strict residual tolerance.
  `Margin = Commission × Margin Rate` holds exactly by definition.
- **Lever:** shift ad inventory toward low-payout campaigns per country/category, and review payout
  terms when an ad leaves its cluster.
- **Expected relations (hint):** with `Commission` and `Margin` onboarded as count/EUR metrics, a
  FORMULA `Margin = Commission × Margin Rate`.
- **Config:** `min_sample 30 tx`, `maturity_days 0`, `baseline category_median`.
- **Uncertainties:** month totals only, so we can't yet see *when* a rate changed. 9 ads have zero
  commission and are excluded.

```cypher
MATCH (:Freecash)-[r:RUNS]->(a:Ad)
WHERE ($ad IS NULL OR a.id = $ad)
  AND ($category IS NULL OR EXISTS { (a)-[:IN_CATEGORY]->(:Category {id: $category}) })
RETURN a.id AS ad, sum(r.tx) AS tx, sum(r.commission) AS commission, sum(r.margin) AS margin
```

### 5.2 Conversion lag shares: **propose** (already specified)

`Fast Conversion Share`, `Instant Conversion Share` and `Late Conversion Share`, from
`CONVERTS_AFTER`. Full proposal, numbers and config are in `graph_engineer.md` section 3. Lever:
`FIXES.md` "button bigger/smaller … when conversion time is higher than usual".

### 5.3 `Click-to-Conversion Rate` by country: **needs_build**

- **Why:** the most direct funnel signal on real data (overall ~0.083% per CLAUDE.md). A drop in
  one country with clicks steady is the classic outage, payment or landing-page fault.
- **Blocker:** clicks are not in the graph. The transaction aggregates have `tx` but no click
  denominator.
- **Build change:** sum `data/Clicks/*.csv` per (`Game Placement ID` → Ad, `Country`), add
  `clicks` to `CONVERTS_IN`, or as a new `(Ad)-[:CLICKED_IN {clicks}]->(Country)` edge.
  Following the "no node per click" rule, it is summed into edges. It belongs on the planned
  `ON_DAY` axis as well, so the monitor gets a daily series.
- **Lever:** pause on outage, landing/button changes, check payment methods per country.

### 5.4 `Lead Share`: **reject**

- **Evidence:** 12 lead vs. 2,979 sale transactions (**0.4%**). Every slice is far below
  `min_sample`, so the metric can't show anything beyond noise.
- **Revisit:** if lead-type campaigns are added.

### 5.5 "For You" feed in-stock click share: **reject**

Needs feed impressions, inventory snapshots and a ranking-model version. None of these exist in
the Freecash data or the graph, so it can't be measured and there's no `needs_build` path from
the current sources. Kept as a rejected record so it isn't re-proposed.

---

## 6. Fix discussions

Besides proposing metrics, the PM moderates one **fix discussion** per issue in every cycle. The
rules are in `agents/fix_playbook.md`, and the mechanics are in `agents/orchestration_plan.md`
section 5.5. What matters for this role:

- **No fix is the normal outcome.** An external cause, a planned promotion, incomplete data or an
  unprovable lever all end as `no_fix` with the reason named, and that discussion is published
  like any fix. "The experts talked and found nothing" is a valid, visible result.
- **Sometimes only the graph should change** (`graph_only`): e.g. the investigation confirmed that a
  cause → metric edge is real. That becomes a normal proposal to the graph engineer.
- **A fix only with certainty:** a supported cause, a lever whose effect is measured in our own data,
  and an expected impact whose lower bound stays > 0 after side effects. The seven gates in
  `fix_playbook.md` §2 enforce this in code: a draft fix that fails one is published as `no_fix`.
- **Fixes are recommendations.** The brief's "ranked recommended actions" contain only
  `fix_proposed` fixes. A human accepts or rejects them in the UI. A rejection, with its reason,
  comes back as a `fix_feedback` message and blocks the same fix for the same issue until new
  evidence appears.

## 7. Checklist before handing off

- [ ] Schema discovery was run in this session, not copied from this document.
- [ ] Every evidence line quotes a number and the query that produced it.
- [ ] `source_query` is read-only (`graph_store.is_read_only`), returns sums, and runs.
- [ ] Zero denominators are handled. The sample sizes per slice are stated.
- [ ] It isn't a duplicate of an existing Metric.
- [ ] The lever is named, or "diagnostic only" is stated.
- [ ] It says whether the metric is computed from real or simulated data.
- [ ] It specifies no placement and no edges, only hints in `expected_relations`.
