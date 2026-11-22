"""Paid-acquisition simulator anchored on the real Freecash ads.

The Freecash data has no paid telemetry (budget, bids, auctions, impressions, CTR, CPC, spend),
so this simulates it hourly for the top ads x their top countries x device_os:

  * Real anchors: which ads/countries (ranked by real commission), revenue per conversion
    (real commission / tx per ad x country). Volumes (auctions, clicks, budgets) are synthetic.
  * Seasonality: hour-of-day curve and weekday factor.
  * A working pacing controller per ad x country: it lowers the bid multiplier when spend runs ahead
    of the planned share of the daily budget and raises it when behind. The budget is a hard cap.
  * An auction model: win rate = 1 / (1 + (competition / bid_multiplier)^4), and CPC rises with
    bids and competition.
  * Tracking: tracked clicks/conversions = true x tracking completeness. Auctions, impressions and
    spend come from the ad platform and are complete.

Scenarios (injected on the last simulated day, slices picked by rank so ads are distinct):
  pacing_bug           fault: from 06:00 the controller stops pacing and pins bid_mult at 1.9
                       -> budget burnt by late morning
  competitor_pullback  fault: competition drops 40% all day -> win rate up, CPC down, bids flat
  planned_promo        innocent: planned promotion, auctions and budget x1.7 (PlannedEvent node)
  tracking_loss        data: tracking completeness 0.35 from 07:00 to 11:59

    .venv/bin/python paid_sim.py > paid_graph.cypher            # all scenarios
    .venv/bin/python paid_sim.py --scenarios none > paid_graph.cypher
    neo4j-cli query --rw --credential test < paid_graph.cypher
"""
from __future__ import annotations

import argparse
import csv
import math
import random
import sys
from collections import defaultdict
from datetime import date, timedelta
from typing import Dict, List, Tuple

from freecash_graph import TX_CSV, num, unwind
import paid_metrics

START = date(2026, 10, 1)
DAYS = 14
N_ADS = 6
COUNTRIES_PER_AD = 2
DEVICES = {"ios": 0.45, "android": 0.55}
DEVICE_CTR = {"ios": 0.022, "android": 0.017}
CHANNELS = ["google_ads", "meta", "tiktok"]
SCENARIOS = ["pacing_bug", "competitor_pullback", "planned_promo", "tracking_loss"]
SCENARIO_SLICE = {"pacing_bug": 0, "competitor_pullback": 2, "planned_promo": 4, "tracking_loss": 6}
PROMO_LIFT = 1.7

_curve = [0.012, 0.008, 0.006, 0.005, 0.006, 0.010, 0.020, 0.035, 0.048, 0.055, 0.058, 0.060,
          0.062, 0.060, 0.057, 0.055, 0.054, 0.056, 0.060, 0.065, 0.068, 0.062, 0.045, 0.028]
DIURNAL = [x / sum(_curve) for x in _curve]
PLANNED_SHARE = [sum(DIURNAL[: h + 1]) for h in range(24)]   # planned cumulative budget share by end of hour
WEEKDAY = [0.95, 0.95, 1.0, 1.0, 1.05, 1.15, 1.12]           # Mon..Sun
WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def real_slices() -> List[Dict]:
    """Top ads by real commission, each with its top countries by tx, plus real revenue/conversion."""
    agg: Dict[Tuple[str, str], List[float]] = defaultdict(lambda: [0, 0.0])
    with open(TX_CSV, newline="") as f:
        for r in csv.DictReader(f):
            a = agg[(r["Game Placement ID"], r["Country"])]
            a[0] += 1
            a[1] += num(r["Commission"])
    by_ad: Dict[str, float] = defaultdict(float)
    for (ad, _), (_, comm) in agg.items():
        by_ad[ad] += comm
    ads = sorted(by_ad, key=lambda a: -by_ad[a])[:N_ADS]
    slices = []
    for i, ad in enumerate(ads):
        countries = sorted((c for (a, c) in agg if a == ad), key=lambda c: -agg[(ad, c)][0])
        for c in countries[:COUNTRIES_PER_AD]:
            tx, comm = agg[(ad, c)]
            slices.append({"ad": ad, "country": c, "channel": CHANNELS[i % len(CHANNELS)],
                           "rev_per_conv": round(max(comm / tx, 0.5), 2)})
    return slices


def _round(x: float, rng: random.Random) -> int:
    """Stochastic rounding keeps small hourly counts unbiased."""
    lo = math.floor(x)
    return lo + (1 if rng.random() < x - lo else 0)


def simulate(scenarios: List[str], days: int = DAYS, seed: int = 7):
    rng = random.Random(seed)
    slices = real_slices()
    last_day = START + timedelta(days=days - 1)
    rows, events = [], []

    for i, s in enumerate(slices):
        active = {sc for sc in scenarios if SCENARIO_SLICE[sc] == i}
        clicks_day = 1400 * 0.8 ** (i // COUNTRIES_PER_AD) * rng.uniform(0.8, 1.2)
        cpc0 = rng.uniform(0.4, 1.2)
        ctr_factor = rng.uniform(0.8, 1.2)
        cvr = rng.uniform(0.02, 0.05)
        avg_ctr = sum(DEVICES[d] * DEVICE_CTR[d] for d in DEVICES) * ctr_factor
        auctions_day = 1.15 * clicks_day / (0.5 * avg_ctr)   # uncapped spend ~15% above budget at mult 1
        base_budget = round(clicks_day * cpc0, -1)

        if "planned_promo" in active:
            events.append({"id": f"promo_{s['ad']}_{s['country']}_{last_day}", "type": "promotion",
                           "name": f"Planned promotion {s['ad']} {s['country']}",
                           "start": f"{last_day}T00:00:00Z", "end": f"{last_day + timedelta(days=1)}T00:00:00Z",
                           "expected_lift": PROMO_LIFT, "ad": s["ad"], "country": s["country"],
                           "affects": ["Eligible Auctions", "Ad Impressions", "Paid Clicks", "Total Ad Spend",
                                       "Conversions", "Revenue"]})

        for d in range(days):
            day = START + timedelta(days=d)
            is_last = day == last_day
            wd = WEEKDAY[day.weekday()]
            promo = PROMO_LIFT if (is_last and "planned_promo" in active) else 1.0
            budget = base_budget * promo
            mult, spent = 1.0, 0.0
            for h in range(24):
                if h > 0:   # pacing controller
                    actual, target = spent / budget, PLANNED_SHARE[h - 1]
                    if actual > target * 1.05:
                        mult *= 0.88
                    elif actual < target * 0.95:
                        mult *= 1.08
                    mult = min(2.5, max(0.4, mult))
                if is_last and "pacing_bug" in active and h >= 6:
                    mult = 1.9                                   # controller stuck, ignores pacing
                comp = rng.uniform(0.95, 1.05) * (0.6 if is_last and "competitor_pullback" in active else 1.0)
                win = 1 / (1 + (comp / mult) ** 4)
                tracking = rng.uniform(0.97, 1.0)
                if is_last and "tracking_loss" in active and 7 <= h < 12:
                    tracking = 0.35

                hour = []
                for dev, share in DEVICES.items():
                    auctions = auctions_day * share * DIURNAL[h] * wd * promo * math.exp(rng.gauss(0, 0.08))
                    imps = auctions * win
                    clicks = imps * DEVICE_CTR[dev] * ctr_factor * math.exp(rng.gauss(0, 0.05))
                    cpc = cpc0 * mult ** 0.7 * comp ** 0.5 * math.exp(rng.gauss(0, 0.04))
                    hour.append([dev, auctions, imps, clicks, clicks * cpc])
                total = sum(x[4] for x in hour)
                cap = 1.0 if spent + total <= budget else max(0.0, (budget - spent) / total) if total else 0.0
                for dev, auctions, imps, clicks, spend in hour:
                    imps, clicks, spend = imps * cap, clicks * cap, spend * cap
                    spent += spend
                    conv = _round(clicks * cvr * math.exp(rng.gauss(0, 0.15)) * tracking, rng)
                    rows.append({
                        "ad": s["ad"], "country": s["country"], "device_os": dev, "channel": s["channel"],
                        "day": str(day), "hod": h, "hour_id": f"{day}T{h:02d}",
                        "auctions": _round(auctions, rng), "impressions": _round(imps, rng),
                        "clicks": _round(clicks * tracking, rng), "spend": round(spend, 2),
                        "conversions": conv, "revenue": round(conv * s["rev_per_conv"], 2),
                        "bid_mult": round(mult, 3), "tracking": round(tracking, 3),
                        "budget": round(budget, 2), "planned_share": round(PLANNED_SHARE[h], 4)})
    return rows, events, slices


def emit(rows, events, slices) -> None:
    for label in ("Day", "Hour", "Metric", "PlannedEvent"):
        print(f"CREATE CONSTRAINT IF NOT EXISTS FOR (n:{label}) REQUIRE n.id IS UNIQUE;")
    # full reload of simulated data, so a different scenario set doesn't leave stale rows
    print("MATCH ()-[e:PAID_IN_HOUR]->() DELETE e;")
    print("MATCH (p:PlannedEvent) DETACH DELETE p;\n")

    paid_metrics.emit_cypher(unwind)
    days = sorted({r["day"] for r in rows})
    unwind([{"id": d, "weekday": WEEKDAY_NAMES[date.fromisoformat(d).weekday()]} for d in days],
           "MERGE (d:Day {id: r.id}) SET d.weekday = r.weekday")
    unwind([{"id": f"{d}T{h:02d}", "day": d, "hour": h, "ts": f"{d}T{h:02d}:00:00Z"}
            for d in days for h in range(24)],
           "MATCH (d:Day {id: r.day})\n"
           "MERGE (h:Hour {id: r.id}) SET h.day = r.day, h.hour = r.hour, h.ts = r.ts\n"
           "MERGE (h)-[:IN_DAY]->(d)")
    unwind([{"ad": s["ad"], "channel": s["channel"]} for s in slices],
           "MATCH (a:Ad {id: r.ad}) SET a.channel = r.channel")
    keys = ["auctions", "impressions", "clicks", "spend", "conversions", "revenue", "bid_mult",
            "tracking", "budget", "planned_share", "channel"]
    unwind([{"ad": r["ad"], "hour": r["hour_id"], "country": r["country"], "device": r["device_os"],
             "m": {k: r[k] for k in keys}} for r in rows],
           "MATCH (a:Ad {id: r.ad}), (h:Hour {id: r.hour})\n"
           "MERGE (a)-[e:PAID_IN_HOUR {country: r.country, device_os: r.device}]->(h) SET e += r.m")
    unwind(events,
           "MATCH (a:Ad {id: r.ad}), (c:Country {id: r.country})\n"
           "MERGE (p:PlannedEvent {id: r.id}) SET p.type = r.type, p.name = r.name, p.start = r.start, "
           "p.end = r.end, p.expected_lift = r.expected_lift, p.affects = r.affects\n"
           "MERGE (p)-[:APPLIES_TO]->(a) MERGE (p)-[:APPLIES_TO]->(c)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenarios", default=",".join(SCENARIOS),
                    help=f"comma list of {SCENARIOS}, or 'none'")
    ap.add_argument("--days", type=int, default=DAYS)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    chosen = [] if a.scenarios == "none" else a.scenarios.split(",")
    bad = set(chosen) - set(SCENARIOS)
    if bad:
        sys.exit(f"unknown scenarios: {sorted(bad)}")
    rows, events, slices = simulate(chosen, a.days, a.seed)
    emit(rows, events, slices)
    for sc in chosen:
        s = slices[SCENARIO_SLICE[sc]]
        print(f"// {sc}: Ad {s['ad']} in {s['country']} on {START + timedelta(days=a.days - 1)}", file=sys.stderr)
    print(f"// {len(slices)} ad x country slices, {len(rows)} PAID_IN_HOUR edges, {len(events)} planned events",
          file=sys.stderr)
