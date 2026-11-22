"""Metric graph for paid acquisition: (:Metric) nodes + FORMULA / INFLUENCES edges.

FORMULA edges are exact arithmetic decompositions (each input -> output, `formula` on the edge).
INFLUENCES edges are possible causes with a prior weight and status 'hypothesis' until validated.
Every metric has a `cypher_value` expression over PAID_IN_HOUR edge properties, so any metric
can be evaluated for any slice (ad, country, device_os, hour window) the same way.

Emitted by paid_sim.py together with the telemetry; the watcher reads the graph back from Neo4j.
"""
from typing import Dict, List, Tuple

# id -> (definition, value over summed totals, unit)
METRICS: Dict[str, Tuple[str, str, str]] = {
    "Eligible Auctions": ("Auctions the campaign was eligible to enter", "auctions", "count"),
    "Budget & Pacing Controller": (
        "Pacing ratio: share of the daily budget spent so far vs. the share planned by this hour",
        "(sum(spend) since 00:00 / budget) / planned_share at that hour", "ratio"),
    "Bid Multiplier": ("Auction-weighted average bid multiplier set by the pacing controller",
                       "sum(bid_mult * auctions) / sum(auctions)", "x"),
    "Auction Win Rate": ("Won auctions / eligible auctions", "impressions / auctions", "ratio"),
    "Ad Impressions": ("Impressions served (= won auctions)", "impressions", "count"),
    "CTR": ("Clicks / impressions", "clicks / impressions", "ratio"),
    "Paid Clicks": ("Tracked clicks from paid ads = paid traffic", "clicks", "count"),
    "CPC": ("Spend / tracked clicks", "spend / clicks", "EUR"),
    "Total Ad Spend": ("Billed ad spend", "spend", "EUR"),
    "Conversion Rate": ("Tracked conversions / tracked clicks", "conversions / clicks", "ratio"),
    "Conversions": ("Tracked paid conversions", "conversions", "count"),
    "Revenue per Conversion": ("Revenue / conversions", "revenue / conversions", "EUR"),
    "Revenue": ("Revenue (commission) attributed to paid traffic", "revenue", "EUR"),
    "Data Freshness & Tracking Health": (
        "Share of expected tracking events that arrived (1.0 = complete)",
        "sum(tracking * auctions) / sum(auctions)", "ratio"),
}

# (src, dst, formula of dst)
FORMULA_EDGES: List[Tuple[str, str, str]] = [
    ("Eligible Auctions", "Ad Impressions", "Ad Impressions = Eligible Auctions * Auction Win Rate"),
    ("Auction Win Rate", "Ad Impressions", "Ad Impressions = Eligible Auctions * Auction Win Rate"),
    ("Ad Impressions", "Paid Clicks", "Paid Clicks = Ad Impressions * CTR"),
    ("CTR", "Paid Clicks", "Paid Clicks = Ad Impressions * CTR"),
    ("Paid Clicks", "Total Ad Spend", "Total Ad Spend = Paid Clicks * CPC"),
    ("CPC", "Total Ad Spend", "Total Ad Spend = Paid Clicks * CPC"),
    ("Paid Clicks", "Conversions", "Conversions = Paid Clicks * Conversion Rate"),
    ("Conversion Rate", "Conversions", "Conversions = Paid Clicks * Conversion Rate"),
    ("Conversions", "Revenue", "Revenue = Conversions * Revenue per Conversion"),
    ("Revenue per Conversion", "Revenue", "Revenue = Conversions * Revenue per Conversion"),
]

# (src, dst, prior weight, mechanism)
INFLUENCE_EDGES: List[Tuple[str, str, float, str]] = [
    ("Budget & Pacing Controller", "Bid Multiplier", 0.9,
     "controller lowers bids when ahead of plan, raises them when behind"),
    ("Budget & Pacing Controller", "Total Ad Spend", 0.8, "budget cap stops spend when exhausted"),
    ("Bid Multiplier", "Auction Win Rate", 0.8, "higher bids win more auctions"),
    ("Bid Multiplier", "CPC", 0.6, "higher bids raise the clearing price"),
    ("Total Ad Spend", "Budget & Pacing Controller", 0.7, "spend so far feeds back into pacing"),
    ("Data Freshness & Tracking Health", "Paid Clicks", 0.9, "lost tracking undercounts clicks"),
    ("Data Freshness & Tracking Health", "Conversions", 0.9, "lost tracking undercounts conversions"),
]


def emit_cypher(unwind) -> None:
    """unwind(rows, body) as in freecash_graph.py."""
    unwind([{"id": k, "definition": d, "value": v, "unit": u} for k, (d, v, u) in METRICS.items()],
           "MERGE (m:Metric {id: r.id}) SET m.definition = r.definition, m.cypher_value = r.value, "
           "m.unit = r.unit, m.domain = 'paid_acquisition'")
    unwind([{"a": a, "b": b, "f": f} for a, b, f in FORMULA_EDGES],
           "MATCH (a:Metric {id: r.a}), (b:Metric {id: r.b})\n"
           "MERGE (a)-[e:FORMULA]->(b) SET e.formula = r.f, e.status = 'validated', e.weight = 1.0")
    unwind([{"a": a, "b": b, "w": w, "m": m} for a, b, w, m in INFLUENCE_EDGES],
           "MATCH (a:Metric {id: r.a}), (b:Metric {id: r.b})\n"
           "MERGE (a)-[e:INFLUENCES]->(b) SET e.prior = r.w, e.mechanism = r.m, "
           "e.status = coalesce(e.status, 'hypothesis')")
