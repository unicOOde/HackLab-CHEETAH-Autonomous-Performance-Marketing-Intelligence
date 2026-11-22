"""Paid Pacing & Auction Watcher.

Monitors paid acquisition per Ad x Country (slices: device_os, hour_of_day, channel) and emits
(:Signal) nodes when pacing or auction behaviour deviates. Deterministic statistics only: every
number comes from the PAID_IN_HOUR telemetry in Neo4j, no LLM.

One cycle at simulated time `now` (data up to now, exclusive), per slice:

  1. Data completeness first. If tracking completeness in the last 3 hours is < 0.9, emit a
     data-completeness signal and run NO pacing/auction checks for that slice (and never resolve
     its open fault signals: unknown never acts). Earlier degraded hours today are excluded from
     the comparison windows (today and baseline alike).
  2. Expected behaviour. The same hour window (00:00..now) on the previous <= 7 days, as a median,
     times a weekday factor for volume metrics (same weekday last week vs. its trailing median,
     across all slices), times the expected lift of any active PlannedEvent on the slice.
     Spread = robust (MAD) spread of those days in log space, with a floor.
  3. Pacing. spend share of today's budget vs. the baseline share at this hour, budget-exhaustion
     hour vs. the normal one, and a projection from the last 3 hours. Checks whether the controller
     kept raising/holding bids while ahead of plan (= not respecting its schedule).
  4. Auction / efficiency. Spend = Eligible Auctions x Win Rate x CTR x CPC, decomposed in log space,
     so each component's share of the spend change is explicit. A win-rate move WITH a bid move in
     the same direction is bid-driven; WITHOUT one it is external auction pressure.
  5. Slices: device_os contribution and the onset hour of the primary metric.
  6. Dedup: issue key = metric + ad + country. Unchanged open signals are not re-reported; a change
     in severity/state updates the node; a slice that is evaluated cleanly resolves its signals.

    .venv/bin/python -m agents.pacing_watcher --now 2026-10-14T12:00
    .venv/bin/python -m agents.pacing_watcher --replay 2026-10-14T08:00 2026-10-14T16:00
"""
from __future__ import annotations

import argparse
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from statistics import median
from typing import Any, Callable, Dict, List, Optional, Tuple

import paid_metrics
from graph_store import GraphStore

AGENT = "pacing_watcher"
LOOKBACK_DAYS = 7
MIN_BASELINE_DAYS = 3
MIN_HOURS = 3                 # too early in the day -> pacing is noise
TRACKING_OK = 0.9
Z_MIN, PCT_MIN = 3.0, 0.20    # significant = both
BID_PCT_MIN = 0.15
MIN_EXPECTED_CLICKS = 30      # below: CTR/CPC/CVR checks skipped (Poisson noise)
MIN_WINDOW_SHARE = 0.15       # auction checks need >= 15% of a normal day's spend in the window
SPREAD_FLOOR = 0.08           # log-space floor: day-to-day noise MAD can't see in 3-7 baseline days
VOLUME_FLOOR_NO_WEEKDAY = 0.12  # volume metrics when the weekday factor is unknown (weekday swing ~ +-15%)

METRIC = {"auctions": "Eligible Auctions", "impressions": "Ad Impressions", "clicks": "Paid Clicks",
          "spend": "Total Ad Spend", "conversions": "Conversions", "revenue": "Revenue",
          "win_rate": "Auction Win Rate", "bid_mult": "Bid Multiplier", "ctr": "CTR", "cpc": "CPC",
          "cvr": "Conversion Rate"}
PACING, TRACKING = "Budget & Pacing Controller", "Data Freshness & Tracking Health"
VOLUME = {"auctions", "impressions", "clicks", "conversions", "revenue"}
SUMS = ("auctions", "impressions", "clicks", "spend", "conversions", "revenue", "bm_x", "tr_x")
SEVERITY_RANK = {"minor": 1, "major": 2}

TELEMETRY_Q = """
MATCH (a:Ad)-[e:PAID_IN_HOUR]->(h:Hour) WHERE h.ts >= $since AND h.ts < $until
RETURN a.id AS ad, e.country AS country, e.device_os AS device_os, coalesce(e.channel, a.channel) AS channel,
       h.day AS day, h.hour AS hod, e.auctions AS auctions, e.impressions AS impressions, e.clicks AS clicks,
       e.spend AS spend, e.conversions AS conversions, e.revenue AS revenue, e.bid_mult AS bid_mult,
       e.tracking AS tracking, e.budget AS budget, e.planned_share AS planned_share
"""
EVENTS_Q = """
MATCH (p:PlannedEvent) WHERE p.start < $until AND p.end > $since
OPTIONAL MATCH (p)-[:APPLIES_TO]->(a:Ad) OPTIONAL MATCH (p)-[:APPLIES_TO]->(c:Country)
RETURN properties(p) AS p, collect(DISTINCT a.id) AS ads, collect(DISTINCT c.id) AS countries
"""
METRIC_EDGES_Q = """
MATCH (a:Metric)-[e:FORMULA|INFLUENCES]->(b:Metric) RETURN a.id AS src, b.id AS dst, type(e) AS rel
"""
OPEN_SIGNALS_Q = "MATCH (s:Signal {status: 'open', agent: $agent}) RETURN properties(s) AS s"


# --------------------------------------------------------------------------- data shapes
@dataclass
class Detection:
    issue_key: str
    kind: str                     # data_completeness | pacing | auction
    metric: str
    severity: str                 # minor | major
    state: str                    # e.g. exhausted, ahead_of_pace, auction_pressure_up, tracking_loss
    ad: str
    country: str
    channel: str
    observed: float
    expected: float
    pct_change: float
    z: Optional[float]
    window: str
    evidence_metric: Optional[str] = None   # metric the numbers come from, if not `metric`
    onset_hour: Optional[int] = None
    devices: List[str] = field(default_factory=list)
    observations: List[str] = field(default_factory=list)   # supported, with numbers
    hypotheses: List[str] = field(default_factory=list)     # "0.70 | text", ranked
    downstream: List[str] = field(default_factory=list)
    path_to_revenue: List[str] = field(default_factory=list)
    uncertainties: List[str] = field(default_factory=list)

    def props(self, now: str) -> Dict[str, Any]:
        return {"issue_key": self.issue_key, "kind": self.kind, "metric": self.metric,
                "severity": self.severity, "state": self.state, "ad": self.ad, "country": self.country,
                "channel": self.channel, "observed": self.observed, "expected": self.expected,
                "pct_change": self.pct_change, "z": self.z, "window": self.window,
                "evidence_metric": self.evidence_metric or self.metric,
                "onset_hour": self.onset_hour, "devices": self.devices, "observations": self.observations,
                "hypotheses": self.hypotheses, "downstream": self.downstream,
                "path_to_revenue": self.path_to_revenue, "uncertainties": self.uncertainties,
                "last_changed": now}


@dataclass
class CycleReport:
    now: str
    new: List[Tuple[str, Detection]] = field(default_factory=list)       # (signal id, detection)
    changed: List[Tuple[str, Detection, str]] = field(default_factory=list)
    resolved: List[Tuple[str, str]] = field(default_factory=list)       # (signal id, issue key)
    ongoing: List[str] = field(default_factory=list)                    # issue keys, not re-reported
    explained: List[str] = field(default_factory=list)                  # innocent changes
    skipped: List[str] = field(default_factory=list)
    uncertainties: List[str] = field(default_factory=list)

    def handoff(self) -> List[Dict[str, Any]]:
        """New or changed major signals, with what an investigator needs to trace them to Revenue."""
        out = []
        for sid, d in [(s, d) for s, d in self.new] + [(s, d) for s, d, _ in self.changed]:
            if d.severity == "major" and d.kind != "data_completeness":
                out.append({"signal": sid, "issue_key": d.issue_key, "targets": [("Ad", d.ad), ("Country", d.country)],
                            "trace_from": d.metric, "path_to_revenue": d.path_to_revenue})
        return out


# --------------------------------------------------------------------------- stats helpers
def _tot(rows: List[Dict], hours: Optional[set] = None) -> Dict[str, float]:
    t = dict.fromkeys(SUMS, 0.0)
    for r in rows:
        if hours is not None and r["hod"] not in hours:
            continue
        for k in SUMS[:6]:
            t[k] += r[k] or 0
        t["bm_x"] += (r["bid_mult"] or 0) * (r["auctions"] or 0)
        t["tr_x"] += (r["tracking"] or 0) * (r["auctions"] or 0)
    return t


def _derive(t: Dict[str, float]) -> Dict[str, Optional[float]]:
    div = lambda a, b: a / b if b else None
    return {**{k: t[k] for k in SUMS[:6]},
            "win_rate": div(t["impressions"], t["auctions"]), "bid_mult": div(t["bm_x"], t["auctions"]),
            "ctr": div(t["clicks"], t["impressions"]), "cpc": div(t["spend"], t["clicks"]),
            "cvr": div(t["conversions"], t["clicks"]), "tracking": div(t["tr_x"], t["auctions"])}


def _robust(values: List[float], floor: float = SPREAD_FLOOR) -> Tuple[Optional[float], float]:
    """(median, MAD spread of log values). Floored, and inflated for small samples (n = 3..7 days)."""
    vals = [v for v in values if v is not None and v > 0]
    if not vals:
        return None, floor
    logs = [math.log(v) for v in vals]
    m = median(logs)
    mad = median(abs(x - m) for x in logs) * 1.4826
    return math.exp(m), max(mad, floor) * (1 + 1 / len(vals))


@dataclass
class Dev:
    key: str
    obs: Optional[float]
    exp: Optional[float]
    pct: Optional[float]
    z: Optional[float]

    @property
    def significant(self) -> bool:
        return self.z is not None and abs(self.z) >= Z_MIN and self.pct is not None and abs(self.pct) >= PCT_MIN

    @property
    def log_ratio(self) -> float:
        if not self.obs or not self.exp:
            return 0.0
        return math.log(self.obs / self.exp)

    def txt(self, money: bool = False) -> str:
        f = (lambda v: f"€{v:,.2f}") if money else (lambda v: f"{v:,.0f}" if abs(v) >= 100 else f"{v:.4g}")
        if self.obs is None or self.exp is None:
            return f"{METRIC.get(self.key, self.key)}: n/a"
        stats = f"{self.pct:+.0%}, z={self.z:+.1f}" if self.pct is not None and self.z is not None else "not tested"
        return f"{METRIC.get(self.key, self.key)} {f(self.obs)} vs expected {f(self.exp)} ({stats})"


def _fmt_pct(x: float) -> str:
    return f"{x:+.0%}"


# --------------------------------------------------------------------------- agent
class PacingWatcher:
    def __init__(self, store: Optional[GraphStore] = None,
                 telemetry_fn: Optional[Callable[[str, str], List[Dict]]] = None,
                 events_fn: Optional[Callable[[str, str], List[Dict]]] = None):
        self.store = store or GraphStore(agent=AGENT)
        self.telemetry_fn = telemetry_fn or (lambda s, u: self.store.read(TELEMETRY_Q, since=s, until=u))
        self.events_fn = events_fn or self._read_events
        self._metric_edges: Optional[Dict[str, List[str]]] = None

    # ------------------------------------------------------------------ reads
    def _read_events(self, since: str, until: str) -> List[Dict]:
        return [{**r["p"], "ads": r["ads"], "countries": r["countries"]}
                for r in self.store.read(EVENTS_Q, since=since, until=until)]

    def metric_edges(self) -> Dict[str, List[str]]:
        if self._metric_edges is None:
            rows = self.store.read(METRIC_EDGES_Q)
            if not rows:   # metric graph not loaded: fall back to the registry
                rows = ([{"src": a, "dst": b} for a, b, _ in paid_metrics.FORMULA_EDGES]
                        + [{"src": a, "dst": b} for a, b, _, _ in paid_metrics.INFLUENCE_EDGES])
            adj: Dict[str, List[str]] = defaultdict(list)
            for r in rows:
                if r["dst"] not in adj[r["src"]]:
                    adj[r["src"]].append(r["dst"])
            self._metric_edges = adj
        return self._metric_edges

    def path_to_revenue(self, metric: str) -> List[str]:
        adj, prev, queue = self.metric_edges(), {metric: None}, [metric]
        while queue:
            n = queue.pop(0)
            if n == "Revenue":
                path = []
                while n is not None:
                    path.append(n)
                    n = prev[n]
                return path[::-1]
            for m in adj.get(n, []):
                if m not in prev:
                    prev[m] = n
                    queue.append(m)
        return []

    # ------------------------------------------------------------------ run
    def run(self, now: datetime) -> CycleReport:
        now = now.replace(minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
        now_s = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        today = now.date()
        since = (now - timedelta(days=LOOKBACK_DAYS + 8)).strftime("%Y-%m-%dT00:00:00Z")
        report = CycleReport(now=now_s)

        rows = self.telemetry_fn(since, now_s)
        events = self.events_fn(f"{today}T00:00:00Z", now_s)
        data: Dict[Tuple[str, str], Dict[str, List[Dict]]] = defaultdict(lambda: defaultdict(list))
        for r in rows:
            data[(r["ad"], r["country"])][r["day"]].append(r)

        H = now.hour
        wf = self._weekday_factor(data, today, report)
        detections: Dict[str, Detection] = {}
        evaluated, degraded = set(), set()
        for (ad, country), days in sorted(data.items()):
            if str(today) not in days:
                report.skipped.append(f"{ad}/{country}: no telemetry today")
                continue
            lift = self._lift(events, ad, country)
            status, found = self._check_slice(ad, country, days, today, H, wf, lift, report)
            for d in found:
                detections[d.issue_key] = d
            {"degraded": degraded, "evaluated": evaluated}.get(status, set()).add((ad, country))

        self._persist(detections, evaluated, degraded, report)
        return report

    def _weekday_factor(self, data, today: date, report: CycleReport) -> Optional[float]:
        totals: Dict[str, float] = defaultdict(float)
        for days in data.values():
            for day, rows in days.items():
                if day != str(today):
                    totals[day] += sum(r["auctions"] or 0 for r in rows)
        ref = today - timedelta(days=7)
        trailing = [totals[str(ref - timedelta(days=i))] for i in range(1, 8) if str(ref - timedelta(days=i)) in totals]
        if str(ref) not in totals or len(trailing) < 5:
            report.uncertainties.append("Not enough same-weekday history: weekday factor unknown, "
                                        "volume metrics tested with a wider tolerance")
            return None
        return totals[str(ref)] / median(trailing)

    @staticmethod
    def _lift(events: List[Dict], ad: str, country: str) -> Dict[str, Any]:
        for e in events:
            if (not e["ads"] or ad in e["ads"]) and (not e["countries"] or country in e["countries"]):
                return {"lift": float(e.get("expected_lift") or 1.0), "affects": set(e.get("affects") or []),
                        "name": e.get("name") or e.get("id")}
        return {"lift": 1.0, "affects": set(), "name": None}

    # ------------------------------------------------------------------ per slice
    def _check_slice(self, ad, country, days, today, H, wf, lift, report) -> Tuple[str, List[Detection]]:
        """-> (status: evaluated | degraded | skipped, detections)."""
        tag = f"{ad}/{country}"
        today_rows = [r for r in days[str(today)] if r["hod"] < H]
        base_days = sorted((d for d in days if d < str(today)), reverse=True)[:LOOKBACK_DAYS]
        base_days = [d for d in base_days if len({r["hod"] for r in days[d]}) == 24]
        channel = today_rows[0]["channel"] if today_rows else ""
        window = f"{today} 00:00-{H - 1:02d}:59"
        if H < 1 or not today_rows:
            return "skipped", []
        if len(base_days) < MIN_BASELINE_DAYS:
            report.skipped.append(f"{tag}: only {len(base_days)} baseline days")
            return "skipped", []

        hourly_tr = {h: _derive(_tot(today_rows, {h}))["tracking"] or 0 for h in range(H)}
        recent = [hourly_tr[h] for h in range(max(0, H - 3), H)]
        bad_hours = sorted(h for h, v in hourly_tr.items() if v < TRACKING_OK)

        # 1. data completeness first
        if min(recent) < TRACKING_OK:
            report.skipped.append(f"{tag}: tracking degraded -> pacing/auction checks suppressed")
            return "degraded", [self._completeness(ad, country, channel, days, today_rows, base_days, bad_hours,
                                                   hourly_tr, wf, window)]
        if H < MIN_HOURS:
            return "evaluated", []
        if bad_hours:
            report.uncertainties.append(f"{tag}: hours {bad_hours} had tracking < {TRACKING_OK} and are "
                                        "excluded from click-based comparisons")

        # 2. expected behaviour over comparable hours
        capped = self._exhausted_at(today_rows)
        dec_hours = {h for h in range(H) if h not in bad_hours and (capped is None or h < capped)}
        obs = _derive(_tot(today_rows, dec_hours))
        base = [_derive(_tot(days[d], dec_hours)) for d in base_days]
        lift_for = lambda k: lift["lift"] if METRIC.get(k) in lift["affects"] else 1.0
        devs: Dict[str, Dev] = {}
        raw: Dict[str, Dev] = {}           # without the planned-event lift, to report innocent changes
        for k in ("auctions", "impressions", "clicks", "spend", "conversions", "revenue",
                  "win_rate", "bid_mult", "ctr", "cpc", "cvr"):
            m, spread = _robust([b[k] for b in base])
            if m is None or obs[k] is None:
                devs[k] = Dev(k, obs[k], m, None, None)
                continue
            if k in VOLUME:
                m *= wf or 1.0
                spread = max(spread, 1 / math.sqrt(max(m, 1)), 0 if wf else VOLUME_FLOOR_NO_WEEKDAY)
            for target, l in ((devs, lift_for(k)), (raw, 1.0)):
                e = m * l
                pct = obs[k] / e - 1 if e else None
                z = math.log(max(obs[k], 1e-9) / e) / spread if e else None
                target[k] = Dev(k, obs[k], e, pct, z)
        exp_clicks = devs["clicks"].exp or 0
        if exp_clicks < MIN_EXPECTED_CLICKS:
            for k in ("ctr", "cpc", "cvr"):
                devs[k] = Dev(k, obs[k], devs[k].exp, None, None)
            report.uncertainties.append(f"{tag}: only {exp_clicks:.0f} expected clicks, CTR/CPC/CVR not tested")

        if lift["name"]:
            moved = [k for k in raw if raw[k].significant and not devs[k].significant]
            if moved:
                report.explained.append(
                    f"{tag}: " + "; ".join(raw[k].txt(k in ('spend', 'revenue')) for k in moved)
                    + f" -> matches planned event '{lift['name']}' (expected x{lift['lift']:.2f})")

        out = []
        pacing = self._pacing(ad, country, channel, days, today_rows, base_days, H, capped, devs, wf, window)
        if pacing:
            out.append(pacing)
        normal_share = median(sum(r["spend"] or 0 for r in days[d] if r["hod"] < H) / (days[d][0]["budget"] or 1)
                              for d in base_days)
        auction = None
        if normal_share >= MIN_WINDOW_SHARE:
            auction = self._auction(ad, country, channel, days, today_rows, base_days, dec_hours, devs, window,
                                    pacing is not None)
        if auction:
            if pacing and auction.state.startswith("bid_driven"):
                pacing.observations += [o for o in auction.observations if o not in pacing.observations]
            else:
                out.append(auction)
        for d in out:
            d.downstream = self._downstream(devs, d, days, base_days, today_rows, H, capped, wf, lift_for)
            d.path_to_revenue = self.path_to_revenue(d.metric)
            d.uncertainties += [u for u in report.uncertainties if u.startswith(tag)] + [
                "No release/deploy data in the graph: a release as cause can't be tested.",
                "Times are UTC; local-time seasonality per country is approximated by the same-hour baseline."]
        return "evaluated", out

    @staticmethod
    def _exhausted_at(rows: List[Dict]) -> Optional[int]:
        if not rows:
            return None
        budget = rows[0]["budget"] or 0
        spent = 0.0
        for h in sorted({r["hod"] for r in rows}):
            spent += sum(r["spend"] or 0 for r in rows if r["hod"] == h)
            if budget and spent >= 0.98 * budget:
                return h
        return None

    # ------------------------------------------------------------------ checks
    def _completeness(self, ad, country, channel, days, today_rows, base_days, bad_hours, hourly_tr, wf, window):
        hrs = set(bad_hours)
        obs = _derive(_tot(today_rows, hrs))
        base = [_derive(_tot(days[d], hrs)) for d in base_days]
        exp_clicks = median(b["clicks"] for b in base) * (wf or 1.0)
        exp_imps = median(b["impressions"] for b in base) * (wf or 1.0)
        exp_tr = median(b["tracking"] for b in base if b["tracking"]) if base else 1.0
        worst = min(hourly_tr[h] for h in bad_hours)
        imps_pct = obs["impressions"] / exp_imps - 1 if exp_imps else 0
        clicks_pct = obs["clicks"] / exp_clicks - 1 if exp_clicks else 0
        d = Detection(
            issue_key=f"{TRACKING}|ad={ad}|country={country}", kind="data_completeness", metric=TRACKING,
            severity="major" if worst < 0.6 else "minor", state="tracking_loss", ad=ad, country=country,
            channel=channel, observed=round(obs["tracking"] or 0, 3), expected=round(exp_tr, 3),
            pct_change=round((obs["tracking"] or 0) / exp_tr - 1, 3), z=None, window=window,
            onset_hour=bad_hours[0], devices=sorted({r["device_os"] for r in today_rows}))
        d.observations = [
            f"Tracking completeness {obs['tracking']:.2f} (worst hour {worst:.2f}) in hours {bad_hours} "
            f"vs normal {exp_tr:.2f}",
            f"Tracked clicks in those hours {obs['clicks']:.0f} vs expected {exp_clicks:.0f} ({_fmt_pct(clicks_pct)})",
            f"Ad Impressions from the ad platform {obs['impressions']:.0f} vs expected {exp_imps:.0f} "
            f"({_fmt_pct(imps_pct)}): served traffic is "
            + ("normal, so the click drop is a measurement gap" if abs(imps_pct) < PCT_MIN else "also off"),
            "Pacing/auction checks for this slice are suppressed until tracking recovers"]
        d.hypotheses = [
            f"{0.7 if abs(imps_pct) < PCT_MIN else 0.4:.2f} | Tracking/attribution loss (SDK, postback endpoint, "
            "consent or pixel change): clicks and conversions undercounted",
            f"{0.1 if abs(imps_pct) < PCT_MIN else 0.4:.2f} | Real traffic drop"]
        d.path_to_revenue = self.path_to_revenue(TRACKING)
        d.downstream = ["CTR, CPC, Conversion Rate and Revenue for this slice are unreliable for these hours"]
        return d

    def _pacing(self, ad, country, channel, days, today_rows, base_days, H, capped, devs, wf, window):
        budget = today_rows[0]["budget"] or 0
        if not budget:
            return None
        spend = sum(r["spend"] or 0 for r in today_rows)
        share = spend / budget
        base_share = [sum(r["spend"] or 0 for r in days[d] if r["hod"] < H) / (days[d][0]["budget"] or 1)
                      for d in base_days]
        exp_share, spread = _robust(base_share)
        if not exp_share:
            return None
        ratio = share / exp_share
        z = math.log(max(share, 1e-9) / exp_share) / spread
        normal_exh = median([(self._exhausted_at(days[d]) if self._exhausted_at(days[d]) is not None else 24)
                             for d in base_days])
        last3 = sum(r["spend"] or 0 for r in today_rows if r["hod"] >= H - 3) / 3
        projected = capped if capped is not None else (H + (budget - spend) / last3 if last3 else 99)

        early = capped is not None and capped < normal_exh - 3
        ahead = ratio >= 1.4 and share >= 0.2 and z >= Z_MIN
        if not (early or ahead or (projected < normal_exh - 4 and ratio >= 1.25)):
            return None
        state = "exhausted" if capped is not None else "ahead_of_pace"
        severity = "major" if early or projected < 16 else "minor"

        # did the controller react? hours ahead of plan where the bid multiplier did not go down
        mult = {h: _derive(_tot(today_rows, {h}))["bid_mult"] for h in range(H)}
        base_cum = {h: median(sum(r["spend"] or 0 for r in days[d] if r["hod"] <= h) / (days[d][0]["budget"] or 1)
                              for d in base_days) for h in range(H)}
        cum, violations = 0.0, []
        for h in range(H):
            if h > 0 and cum > base_cum[h - 1] * 1.1 and mult[h] and mult[h - 1] and mult[h] >= mult[h - 1] - 1e-6:
                violations.append(h)
            cum += sum(r["spend"] or 0 for r in today_rows if r["hod"] == h) / budget
        bid = devs["bid_mult"]
        d = Detection(
            issue_key=f"{PACING}|ad={ad}|country={country}", kind="pacing", metric=PACING, severity=severity,
            state=state, ad=ad, country=country, channel=channel, observed=round(share, 3),
            expected=round(exp_share, 3), pct_change=round(ratio - 1, 3), z=round(z, 1), window=window,
            onset_hour=self._onset(days, today_rows, base_days, H, "pace"),
            devices=sorted({r["device_os"] for r in today_rows}))
        d.observations = [
            f"Spent €{spend:,.0f} = {share:.0%} of today's €{budget:,.0f} budget by {H:02d}:00; "
            f"normal by this hour: {exp_share:.0%} (pacing ratio {ratio:.2f}, z={z:+.1f})",
            (f"Budget exhausted at {capped:02d}:00 vs normally {normal_exh:.0f}:00" if capped is not None else
             f"Projected exhaustion ~{projected:.0f}:00 (last 3h: €{last3:,.0f}/h) vs normally {normal_exh:.0f}:00"),
            bid.txt()]
        if violations:
            d.observations.append(f"Bid multiplier did not decrease in {len(violations)} hours while ahead of plan "
                                  f"(hours {violations[:8]}); values {[round(mult[h], 2) for h in violations[:5]]}")
        hyps = []
        if len(violations) >= 3:
            hyps.append((0.8 if bid.significant and (bid.pct or 0) > 0 else 0.65,
                         "Pacing controller is not respecting its schedule: bids held/raised while spend ran ahead"))
        if devs["auctions"].significant and (devs["auctions"].pct or 0) > 0:
            hyps.append((0.55, f"More eligible auctions than expected ({_fmt_pct(devs['auctions'].pct)}): "
                               "unplanned traffic surge or targeting change"))
        win = devs["win_rate"]
        if win.significant and (win.pct or 0) > 0 and not (bid.significant and (bid.pct or 0) > 0):
            hyps.append((0.55, "Auction got easier without higher bids: competitor pull-back"))
        if not hyps:
            hyps.append((0.3, "Budget or pacing configuration changed (e.g. a smaller daily budget or a "
                              "different pacing curve)"))
        d.hypotheses = [f"{c:.2f} | {t}" for c, t in sorted(hyps, reverse=True)]
        return d

    def _auction(self, ad, country, channel, days, today_rows, base_days, dec_hours, devs, window, has_pacing):
        win, bid, cpc, ctr, auc, imp = (devs[k] for k in ("win_rate", "bid_mult", "cpc", "ctr", "auctions",
                                                          "impressions"))
        bid_moved = bid.pct is not None and abs(bid.pct) >= BID_PCT_MIN and bid.z is not None and abs(bid.z) >= Z_MIN
        win_followed = bid_moved and win.pct is not None and (win.pct > 0) == (bid.pct > 0) and win.significant
        if bid_moved and win_followed:
            key, state = "bid_mult", f"bid_driven_{'up' if bid.pct > 0 else 'down'}"
        elif bid_moved and (cpc.significant or win.significant):
            # bids moved but the win rate did not follow (flat or opposite): the auction itself changed.
            # bids down + win rate not down = auction got easier; bids up + win rate not up = harder
            key = "win_rate"
            state = f"auction_pressure_{'down' if bid.pct < 0 else 'up'}"
        elif win.significant:
            key, state = "win_rate", f"auction_pressure_{'down' if win.pct > 0 else 'up'}"
        elif cpc.significant:
            key, state = "cpc", f"cpc_{'up' if cpc.pct > 0 else 'down'}"
        elif ctr.significant:
            key, state = "ctr", f"click_quality_{'up' if ctr.pct > 0 else 'down'}"
        elif imp.significant or auc.significant:
            key = "impressions" if imp.significant else "auctions"
            state = f"volume_{'up' if devs[key].pct > 0 else 'down'}"
        else:
            return None
        # The issue key follows the classification (stable across cycles); the numbers on the signal
        # come from the strongest significant metric of that family.
        family = {"win_rate": ("win_rate", "cpc", "bid_mult"), "bid_mult": ("bid_mult", "win_rate"),
                  "cpc": ("cpc",), "ctr": ("ctr",), "impressions": ("impressions", "auctions"),
                  "auctions": ("auctions", "impressions")}[key]
        sig = [devs[k] for k in family if devs[k].significant] or [devs[key]]
        main = max(sig, key=lambda x: abs(x.z or 0))
        severity = "major" if abs(main.pct or 0) >= 0.5 or has_pacing else "minor"

        # spend decomposition: ln(spend) = ln(auctions) + ln(win) + ln(ctr) + ln(cpc)
        parts = [k for k in ("auctions", "win_rate", "ctr", "cpc") if devs[k].pct is not None]
        contrib = None
        if devs["spend"].pct is not None and len(parts) == 4:
            biggest = max(parts, key=lambda k: abs(devs[k].log_ratio))
            contrib = (f"Total Ad Spend {_fmt_pct(devs['spend'].pct)} = Eligible Auctions x Auction Win Rate x CTR x CPC "
                       f"= {' x '.join(_fmt_pct(devs[k].pct) for k in parts)}; largest factor: {METRIC[biggest]}")

        H = max(dec_hours) + 1 if dec_hours else 0
        dev_split = self._device_split(key, today_rows, days, base_days, dec_hours)
        d = Detection(
            issue_key=f"{METRIC[key]}|ad={ad}|country={country}", kind="auction", metric=METRIC[key],
            severity=severity, state=state, ad=ad, country=country, channel=channel,
            evidence_metric=METRIC[main.key],
            observed=round(main.obs, 4), expected=round(main.exp, 4), pct_change=round(main.pct, 3),
            z=round(main.z, 1), window=window, onset_hour=self._onset(days, today_rows, base_days, H, key),
            devices=[dv for dv, _ in dev_split[:1]] if dev_split and dev_split[0][1] >= 0.7 else
            sorted({r["device_os"] for r in today_rows}))
        d.observations = [devs[k].txt(k == "cpc") for k in ("bid_mult", "win_rate", "auctions", "impressions",
                                                            "ctr", "cpc")]
        d.observations.append(devs["spend"].txt(True))
        if contrib:
            d.observations.append(contrib)
        if dev_split:
            d.observations.append("By device_os (share of the deviation): "
                                  + ", ".join(f"{dv} {s:.0%}" for dv, s in dev_split))
        hyps = []
        if state.startswith("bid_driven"):
            hyps += [(0.7, "Bid Multiplier change (pacing controller or a manual/automated bid change) drives the "
                           "win-rate move"),
                     (0.2, "Recent release changed bidding logic (not testable: no release data)")]
        elif state == "auction_pressure_down":
            how = (f"bids {_fmt_pct(bid.pct)} yet win rate {_fmt_pct(win.pct or 0)}" if bid_moved
                   else f"win rate {_fmt_pct(win.pct or 0)} with bids flat")
            hyps += [(0.65 if (cpc.pct or 0) < 0 else 0.45,
                      f"Competitor pull-back / less auction competition: {how}"
                      + (f", CPC {_fmt_pct(cpc.pct)}" if cpc.pct is not None else "")
                      + (" (the pacing controller lowered bids because winning got cheaper)" if bid_moved else "")),
                     (0.25, "Ad platform auction/quality-score change"),
                     (0.1, "Pacing bug (unlikely: the controller moved bids in the expected direction)" if bid_moved
                      else "Pacing bug (unlikely: bid multiplier did not move)")]
        elif state == "auction_pressure_up":
            hyps += [(0.6, "New or more aggressive competitor: " + (
                f"bids {_fmt_pct(bid.pct)} yet win rate {_fmt_pct(win.pct or 0)}" if bid_moved
                else f"win rate {_fmt_pct(win.pct or 0)} with bids flat")),
                     (0.25, "Ad platform auction/quality-score change")]
        elif state.startswith("cpc"):
            hyps += [(0.5, "Clearing prices changed without a win-rate change: auction mix / placement shift")]
        elif state.startswith("click_quality"):
            hyps += [(0.45, "Creative or landing change, or audience/placement mix shift"),
                     (0.25, "Click tracking issue below the completeness threshold")]
        else:
            hyps += [(0.45, "Demand / targeting change not covered by a planned event")]
        d.hypotheses = [f"{c:.2f} | {t}" for c, t in sorted(hyps, reverse=True)]
        return d

    def _device_split(self, key, today_rows, days, base_days, hours) -> List[Tuple[str, float]]:
        devices = sorted({r["device_os"] for r in today_rows})
        dev_abs = {}
        for dv in devices:
            o = _derive(_tot([r for r in today_rows if r["device_os"] == dv], hours))[key]
            e, _ = _robust([_derive(_tot([r for r in days[d] if r["device_os"] == dv], hours))[key]
                            for d in base_days])
            if o and e:
                dev_abs[dv] = abs(math.log(o / e))
        tot = sum(dev_abs.values())
        return sorted(((dv, v / tot) for dv, v in dev_abs.items()), key=lambda x: -x[1]) if tot else []

    def _onset(self, days, today_rows, base_days, H, key) -> Optional[int]:
        """First hour from which the metric stays off its same-hour baseline (>= 2 of the next 3 hours)."""
        def val(rows, h):
            if key == "pace":
                b = rows[0]["budget"] or 1
                return sum(r["spend"] or 0 for r in rows if r["hod"] <= h) / b
            return _derive(_tot(rows, {h}))[key]
        off = []
        for h in range(H):
            e, _ = _robust([val(days[d], h) for d in base_days])
            o = val(today_rows, h)
            off.append(bool(e and o is not None and abs(o / e - 1) >= PCT_MIN))
        for h in range(H):
            if off[h] and sum(off[h:h + 3]) >= min(2, H - h):
                return h
        return None

    def _downstream(self, devs, d, days, base_days, today_rows, H, capped, wf, lift_for) -> List[str]:
        out = [devs[k].txt(k == "revenue") for k in ("clicks", "conversions", "revenue")]
        roas_o = (devs["revenue"].obs or 0) / devs["spend"].obs if devs["spend"].obs else None
        roas_e = (devs["revenue"].exp or 0) / devs["spend"].exp if devs["spend"].exp else None
        if roas_o is not None and roas_e:
            out.append(f"ROAS {roas_o:.2f} vs expected {roas_e:.2f}")
        if d.kind == "pacing":
            start = capped + 1 if capped is not None else H
            rest = median(sum(r["revenue"] or 0 for r in days[x] if r["hod"] >= start) for x in base_days)
            rest *= (wf or 1.0) * lift_for("revenue")
            out.append((f"Budget gone from {capped:02d}:00: " if capped is not None else "If budget runs out early: ")
                       + f"~€{rest:,.0f} revenue normally earned after {start:02d}:00 is at risk")
        return out

    # ------------------------------------------------------------------ persistence + dedup
    def _persist(self, detections: Dict[str, Detection], evaluated: set, degraded: set, report: CycleReport) -> None:
        open_rows = {r["s"]["issue_key"]: r["s"] for r in self.store.read(OPEN_SIGNALS_Q, agent=AGENT)}
        for key, d in detections.items():
            cur = open_rows.get(key)
            if cur is None:
                links = [("ON_METRIC", "Metric", d.metric), ("ABOUT", "Ad", d.ad), ("IN_COUNTRY", "Country", d.country)]
                links = [l for l in links if self.store.exists(l[1], l[2])]
                sid = self.store.add_signal({**d.props(report.now), "first_detected": report.now}, links)
                report.new.append((sid, d))
            elif (cur.get("severity"), cur.get("state")) != (d.severity, d.state):
                self.store.update_agent_node("Signal", cur["id"], d.props(report.now),
                                             reason=f"{cur.get('severity')}/{cur.get('state')} -> {d.severity}/{d.state}")
                report.changed.append((cur["id"], d, f"{cur.get('severity')}/{cur.get('state')}"))
            else:
                report.ongoing.append(key)
        for key, s in open_rows.items():
            if key in detections:
                continue
            slice_ = (s.get("ad"), s.get("country"))
            # resolve only on a clean evaluation; a degraded slice keeps its fault signals (unknown != fixed)
            if slice_ in evaluated or (s.get("kind") == "data_completeness" and slice_ not in degraded):
                self.store.set_status("Signal", s["id"], "resolved", reason=f"no longer detected at {report.now}")
                report.resolved.append((s["id"], key))


# --------------------------------------------------------------------------- output
def format_report(r: CycleReport, verbose: bool = True) -> str:
    lines = [f"== Pacing watcher @ {r.now}: {len(r.new)} new, {len(r.changed)} changed, "
             f"{len(r.resolved)} resolved, {len(r.ongoing)} ongoing (not re-reported)"]

    def block(tag: str, sid: str, d: Detection, extra: str = ""):
        lines.append(f"[{tag}] {d.severity.upper()} {d.metric} | Ad {d.ad} {d.country} ({d.channel}) | "
                     f"{d.state}{extra} | {d.evidence_metric or d.metric} {d.pct_change:+.0%} | onset {d.onset_hour if d.onset_hour is not None else '-'}h | "
                     f"devices {','.join(d.devices)} | {sid}")
        if not verbose:
            return
        lines.extend(f"    observed: {o}" for o in d.observations)
        lines.extend(f"    hypothesis {h}" for h in d.hypotheses)
        lines.extend(f"    downstream: {x}" for x in d.downstream)
        if d.path_to_revenue:
            lines.append(f"    graph path: {' -> '.join(d.path_to_revenue)}")
        lines.extend(f"    ? {u}" for u in d.uncertainties)

    for sid, d in r.new:
        block("NEW", sid, d)
    for sid, d, before in r.changed:
        block("CHANGED", sid, d, f" (was {before})")
    for sid, key in r.resolved:
        lines.append(f"[RESOLVED] {key} | {sid}")
    for e in r.explained:
        lines.append(f"[EXPLAINED] {e}")
    if verbose:
        lines.extend(f"[SKIPPED] {s}" for s in r.skipped)
    return "\n".join(lines)


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "")).replace(tzinfo=timezone.utc)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--now", help="simulated time, e.g. 2026-10-14T12:00")
    ap.add_argument("--replay", nargs=2, metavar=("FROM", "TO"), help="run hourly cycles FROM..TO")
    ap.add_argument("--brief", action="store_true", help="headlines only")
    a = ap.parse_args()
    w = PacingWatcher()
    if a.replay:
        t, end = _parse(a.replay[0]), _parse(a.replay[1])
        while t <= end:
            print(format_report(w.run(t), verbose=not a.brief), "\n")
            t += timedelta(hours=1)
    else:
        print(format_report(w.run(_parse(a.now or "2026-10-14T12:00")), verbose=not a.brief))
