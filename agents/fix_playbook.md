# Fix Playbook

**Read by:** the product manager (fix discussion and verdict) and every agent's `review()` hook.
**Source of ideas:** `FIXES.md` (the team's raw list). This file is the vetted version: every fix
has a trigger, the evidence it needs, how its impact is computed, and when it must **not** be
proposed.
**Status:** spec. Enforced by the fix-council gates described in `agents/orchestration_plan.md`
section 5.5.

---

## 0. The attitude

- **Most issues end without a fix, and that is correct.** A discussion that concludes "the cause is
  external", "this is a planned promotion", "the data is incomplete" or "we can't prove a lever
  helps" is a good result. Say it plainly, with the reason.
- **Sometimes the right outcome is only a graph change.** For example, confirming that a cause →
  metric edge is real (`graph_only`). That's knowledge the next investigation can use.
- **A fix is proposed only when we are sure it helps:** a supported cause, a lever whose effect
  is measured in our own data, and an expected-impact range that stays positive at its lower bound
  after side effects. "It might help" is not a fix.
- **Fixes are recommendations.** Nothing here is ever executed by an agent. A human accepts or
  rejects them in the UI.
- **Real vs. simulated.** Paid-acquisition fixes (P*) are based on the simulated `PAID_IN_HOUR`
  telemetry and must be labelled as simulated. Affiliate fixes (A*) are based on real
  transactions.

## 1. Discussion outcomes

Each issue ends in exactly one outcome:

| Outcome | When |
|---|---|
| `fix_proposed` | a playbook fix passed **every** gate (section 2) |
| `no_fix` | discussed, no fix. The reason must be one of: `not_fixable_cause`, `innocent_change`, `data_issue`, `insufficient_evidence`, `expert_objection`, `already_resolving`, `previously_rejected` |
| `graph_only` | the outcome is knowledge. A proposal goes to the graph engineer (e.g. `INFLUENCES` hypothesis → validated) |
| `monitor` | too early to judge (immature cohort, small sample, single-day effect). Revisited next cycle |

When in doubt: `no_fix` or `monitor`, never `fix_proposed`.

## 2. Gates (all required for `fix_proposed`, checked in code, not by the model)

1. **Supported cause:** a deterministic detection (pacing watcher), or an investigator hypothesis
   with confidence ≥ 0.7 whose evidence lines survived number validation.
2. **Fixable cause:** not in section 5.
3. **Measured lever:** the lever's effect is shown in our data, either by peers (same
   country/category slice where the lever state differs) or by the slice's own pre-issue
   baseline. The numbers are quoted.
4. **Positive impact:** the expected impact is a range computed from those numbers with the
   entry's formula, and its **lower bound is > 0** after subtracting named side effects on
   downstream metrics (`INFLUENCES` edges from the lever's metric).
5. **Enough data:** sample ≥ the metric's `min_sample`, and the cohort is mature.
6. **Experts agree:** no expert objects with evidence, and the lever's domain owner supports it
   (pacing watcher for P*, investigator for A*).
7. **Still relevant:** the issue isn't already resolving, and the same fix for the same
   `issue_key` wasn't rejected by a human, unless there is new evidence since the rejection.

## 3. Paid acquisition fixes (simulated telemetry)

### P1. Restore pacing / cap the bid multiplier for ad × country
- **Trigger:** the controller held or raised `bid_mult` while spend was ahead of plan, and the
  budget was exhausted ≥ 2 h earlier than the slice's normal exhaustion hour.
- **Evidence:** `bid_mult` vs. baseline over the window, spend share vs. `planned_share` by hour,
  the exhaustion hour today vs. the baseline days.
- **Impact:** revenue of the hours lost after exhaustion, at baseline conversion rate and revenue
  per conversion, minus the extra CPC paid while overbidding. Range: use the baseline days'
  min..max.
- **Do not propose if:** an active `PlannedEvent` on the slice raises the planned spend (that
  burn is intended).

### P2. Shift budget between campaigns in the same country
- **Trigger:** ROAS (`revenue / spend`) gap > 30% between two campaigns in the same country for
  ≥ 3 days.
- **Evidence:** daily ROAS of both campaigns, and the receiver's win rate and CPC trend.
- **Impact:** moved spend × (ROAS_receiver − ROAS_donor), with the receiver's ROAS discounted
  by its CPC elasticity (CPC change per bid change in the data).
- **Do not propose if:** the receiver's win rate is already > 0.8 (saturated: more budget buys
  little), or tracking < 0.9 on either campaign.

### P3. Raise the budget for a country or campaign
- **Trigger:** budget-limited. The campaign exhausts its budget early on ≥ 3 days, with ROAS above
  the peer median and stable CPC.
- **Evidence:** exhaustion hours, ROAS vs. peers, CPC trend.
- **Impact:** the hours not captured × hourly revenue at current ROAS, lower bound at the peer
  median ROAS.
- **Do not propose if:** CPC is rising (more budget buys costlier clicks), or ROAS ≤ 1.

### P4. Lower the budget or pause a campaign
- **Trigger:** ROAS < 1 (spend > revenue) for ≥ 3 days.
- **Evidence:** daily spend and revenue, and tracking health over the same days.
- **Impact:** avoided loss = spend − revenue over the period.
- **Do not propose if:** tracking < 0.9 (revenue may be undercounted, so this would be a
  `data_issue`), or a `PlannedEvent` explains the spend.

### P5. Use a competitor pullback: temporarily raise bids or budget
- **Trigger:** win rate up and CPC down **without** our own bid change (external auction pressure
  fell), and ROAS up.
- **Evidence:** the pacing watcher's spend decomposition (win-rate move without a bid move).
- **Impact:** extra conversions bought at the lower CPC × revenue per conversion − spend, for the
  observed duration only.
- **Do not propose if:** the effect has lasted only one day. That's `monitor`; propose from the
  second consecutive day.

### P6. Device bid modifier
- **Trigger:** one `device_os` explains ≥ 70% of a CTR or conversion-rate drop.
- **Evidence:** the per-device contribution from the pacing watcher's slice analysis.
- **Impact:** the device's spend share × the gap to the other device's rate × revenue per
  conversion.
- **Do not propose if:** the device sample is below the minimum.

### P7. Dayparting: move budget to higher-converting hours
- **Trigger:** stable hour-of-day conversion-rate differences over ≥ 5 days, plus early budget
  exhaustion.
- **Evidence:** CVR by hour over the baseline days, with spread.
- **Impact:** moved spend × CVR gap × revenue per conversion. The lower bound uses the gap minus
  one spread.
- **Do not propose if:** fewer than 5 days of data, or the weekday factor is unknown.

## 4. Affiliate fixes (real transactions)

### A1. Check or correct payout terms for an ad
- **Trigger:** the ad's `Margin Rate` leaves its contract cluster (most ads sit at 0.30, i.e.
  payout = 70% of commission) with ≥ 30 tx.
- **Evidence:** the ad's margin rate vs. its own history and vs. its cluster.
- **Impact:** (cluster rate − observed rate) × commission over the period.
- **Do not propose if:** there's no history for the ad's rate (new contract, not a deviation).

### A2. Steer placements toward low-payout, high-margin campaigns
- **Trigger:** a margin-rate gap between comparable campaigns in the same country and category.
- **Status:** **blocked.** Without clicks in the graph there are no conversion rates, so the
  impact of shifting traffic can't be measured. Record it as `no_fix: insufficient_evidence`, and
  point to the `needs_build` proposal "Click-to-Conversion Rate" (`product_manager.md` §5.3).

### A3. Pause or redirect a campaign during a confirmed outage
- **Trigger:** an `Outage` with confidence ≥ 0.8 on a service the ad depends on, in an affected
  country, with conversions down there.
- **Evidence:** the outage node (sources, confidence) and conversions before vs. during it.
- **Impact:** avoided wasted spend while conversions are impossible (paid side only).
- **Do not propose if:** the outage is resolving, confidence < 0.8, or conversions aren't
  actually down.

### A4. Landing page / button layout change when conversion time rises
- **Trigger:** `Fast Conversion Share` well below the category peers.
- **Status:** **never `fix_proposed`.** A layout change's effect can't be quantified from
  observational data. Record it as `no_fix: insufficient_evidence`, with "suggest an A/B test on
  <ad>" in the transcript. That is a suggestion, not a fix.

## 5. Not fixable: straight to `no_fix`

| Cause | Reason code | Note |
|---|---|---|
| Third-party outage (beyond pausing via A3) | `not_fixable_cause` | wait for recovery |
| Bad internet, payment method not accepted, app crash | `not_fixable_cause` | from `FIXES.md` |
| Tracking loss / incomplete data | `data_issue` | **never** cut budget because conversions "dropped". Fix the tracking, not the campaign |
| Planned promotion (`PlannedEvent`) | `innocent_change` | expected movement |
| Seasonality, weekday swing | `innocent_change` | within the seasonal band |
| Small sample, immature cohort | → `monitor` | revisit |
| "Revert the last release" | `insufficient_evidence` | no release data exists. Unavailable until it does |

## 6. What a published fix contains

The shape is fixed. The values below are illustrative, not measured.

```yaml
fix_id: fix_…
playbook: P1
issue_key: "Budget & Pacing Controller|0003WS|DE"
data_side: simulated            # simulated | real
action: "Cap bid multiplier at 1.1 and restore pacing for ad 0003WS in DE"
scope: {ad: "0003WS", country: "DE", channel: "google_ads"}
expected_impact: {metric: "Revenue", low: 120.0, high: 210.0, unit: EUR, horizon: "rest of day"}
side_effects: ["Total Ad Spend −30..−45% for the slice"]
evidence: ["bid_mult 1.90 vs baseline 1.05 (…query…)", "…"]
gates: {supported_cause: pass, fixable: pass, measured_lever: pass, positive_impact: pass,
        enough_data: pass, experts_agree: pass, still_relevant: pass}
stances: {pacing_watcher: support, investigator: support, outage_agent: abstain, graph_engineer: support}
status: proposed                # human feedback lives in feedback/<fix_id>.json
```
