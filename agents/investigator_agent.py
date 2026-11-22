"""Investigator agent: why does an Ad / Country / Category / Outage look the way it does?

1. Gathers context from the graph (reads only) and computes deviations itself: the target's ad
   metrics vs. the median of its siblings (same Category) and of all ads, its country/hour/lag mix,
   open outages on its countries, and earlier hypotheses about it.
2. Claude turns those numbers into hypotheses (JSON).
3. Validation: targets exist, confidence in [0,1], only ag_* properties, and every evidence line
   must quote a number that is actually in the context. Anything else is dropped and logged.
4. Writes (:Hypothesis)-[:ABOUT]->(...) and ag_* properties via GraphStore, then starts the graph
   overlooker on what it wrote.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from statistics import median
from typing import Any, Callable, Dict, List, Optional, Tuple

from graph_store import BASE_LABELS, GraphStore, GraphWriteError
from llm import LLMUnavailable, claude_json

AGENT = "investigator_agent"
TARGET_LABELS = {"Ad", "Country", "Category", "Outage"}
METRICS = ["tx", "commission", "comm_per_tx", "margin_rate", "instant_share", "late_share",
           "lead_share", "countries", "top_country_share", "night_click_share"]

AD_METRICS_Q = """
MATCH (:Freecash)-[r:RUNS]->(a:Ad)
OPTIONAL MATCH (a)-[:IN_CATEGORY]->(cat:Category)
OPTIONAL MATCH (a)-[:IN_VERTICAL]->(v:Vertical)
OPTIONAL MATCH (a)-[l1:CONVERTS_AFTER]->(:LagBucket {id: '<1m'})
OPTIONAL MATCH (a)-[l7:CONVERTS_AFTER]->(:LagBucket {id: '>7d'})
OPTIONAL MATCH (a)-[ld:HAS_TYPE]->(:TransactionType {id: 'lead'})
CALL (a) { MATCH (a)-[e:CONVERTS_IN]->(c:Country) RETURN count(c) AS countries, max(e.tx) AS top_country_tx }
CALL (a) { OPTIONAL MATCH (a)-[e:CLICKED_AT]->(h:ClickHour) WHERE h.id < 6 RETURN sum(e.tx) AS night_tx }
RETURN a.id AS ad, cat.id AS category, v.id AS vertical, r.tx AS tx, round(r.commission, 2) AS commission,
       round(r.commission / r.tx, 2) AS comm_per_tx,
       CASE WHEN r.commission > 0 THEN round(r.margin / r.commission, 3) END AS margin_rate,
       round(coalesce(l1.tx, 0) * 1.0 / r.tx, 3) AS instant_share,
       round(coalesce(l7.tx, 0) * 1.0 / r.tx, 3) AS late_share,
       round(coalesce(ld.tx, 0) * 1.0 / r.tx, 3) AS lead_share,
       countries, round(top_country_tx * 1.0 / r.tx, 3) AS top_country_share,
       round(coalesce(night_tx, 0) * 1.0 / r.tx, 3) AS night_click_share
"""

HYPOTHESIS_SCHEMA = {
    "type": "object",
    "properties": {
        "hypotheses": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "confidence": {"type": "number"},
                "evidence": {"type": "array", "items": {"type": "string"}},
                "about": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"label": {"type": "string"}, "id": {"type": "string"}},
                    "required": ["label", "id"], "additionalProperties": False}},
            },
            "required": ["text", "confidence", "evidence", "about"], "additionalProperties": False}},
        "node_updates": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "label": {"type": "string"}, "id": {"type": "string"},
                "props": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"key": {"type": "string"},
                                   "value": {"type": ["string", "number", "boolean", "null"]}},
                    "required": ["key", "value"], "additionalProperties": False}},
                "reason": {"type": "string"},
            },
            "required": ["label", "id", "props", "reason"], "additionalProperties": False}},
    },
    "required": ["hypotheses", "node_updates"],
    "additionalProperties": False,
}

SYSTEM = """You investigate a performance-marketing graph of Freecash ads (Neo4j, aggregated per ad).
You get precomputed numbers for one target and its comparison groups. Explain what stands out and
what likely causes it. Rules:
- Only use numbers that are in the context. Every evidence line must quote at least one of them.
- Small samples (few transactions) deserve low confidence; say so.
- instant_share = share of conversions < 1 minute after the click (high values can mean fraud or
  postbacks); late_share = > 7 days; night_click_share = clicks 00-05h; top_country_share = share of
  the biggest country; margin_rate = margin / commission.
- 'about' must reference nodes from the context (labels: Ad, Country, Category, Vertical, ClickHour,
  LagBucket, TransactionType, Outage). Return [] if nothing stands out.
- open_paid_signals come from the pacing & auction watcher (simulated paid telemetry). Their
  observations are measured; their hypotheses are not yet confirmed. Trace them downstream along
  path_to_revenue and say whether the Freecash numbers support or contradict them.
- node_updates: optional ag_* properties on those nodes (e.g. ag_flag, ag_note, ag_score) when a
  finding should be visible on the node itself. Keys must start with ag_."""


@dataclass
class Investigation:
    target: Tuple[str, str]
    hypotheses: List[str] = field(default_factory=list)       # Hypothesis ids
    updates: List[str] = field(default_factory=list)          # Change ids
    dropped: List[str] = field(default_factory=list)          # why something was not written
    decision_log: List[Dict[str, Any]] = field(default_factory=list)


def _numbers(text: str) -> List[float]:
    return [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", text.replace(",", ""))]


class InvestigatorAgent:
    def __init__(self, store: Optional[GraphStore] = None, run_overlooker: bool = True):
        self.store = store or GraphStore(agent=AGENT)
        self.run_overlooker = run_overlooker
        self.overlooker_report = None
        self._ads: Optional[List[Dict[str, Any]]] = None

    # ------------------------------------------------------------------ context
    def ad_metrics(self) -> List[Dict[str, Any]]:
        if self._ads is None:
            self._ads = self.store.read(AD_METRICS_Q)
        return self._ads

    @staticmethod
    def _medians(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        out = {}
        for m in METRICS:
            vals = [r[m] for r in rows if r.get(m) is not None]
            out[m] = round(median(vals), 3) if vals else None
        out["n_ads"] = len(rows)
        return out

    def _ad_context(self, ad_id: str) -> Dict[str, Any]:
        ads = self.ad_metrics()
        me = next((a for a in ads if a["ad"] == ad_id), None)
        if me is None:
            raise GraphWriteError(f"Ad {ad_id} not found")
        siblings = [a for a in ads if a["category"] == me["category"] and a["ad"] != ad_id]
        mix = {}
        for rel, label in (("CONVERTS_IN", "Country"), ("CLICKED_AT", "ClickHour"),
                           ("CONVERTS_AFTER", "LagBucket"), ("HAS_TYPE", "TransactionType")):
            mix[label] = self.store.read(
                f"MATCH (a:Ad {{id: $id}})-[e:{rel}]->(t:{label}) "
                "RETURN t.id AS id, e.tx AS tx, round(e.commission, 2) AS commission ORDER BY tx DESC LIMIT 12",
                id=ad_id)
        return {
            "target": {"label": "Ad", **me, **self._ag_props("Ad", ad_id)},
            "category_siblings_median": self._medians(siblings),
            "all_ads_median": self._medians(ads),
            "mix": mix,
            "open_outages_on_its_countries": self.store.read(
                "MATCH (:Ad {id: $id})-[:CONVERTS_IN]->(c:Country)<-[:AFFECTS]-(o:Outage {status: 'open'}) "
                "RETURN o.id AS id, o.service AS service, o.severity AS severity, collect(c.id) AS countries",
                id=ad_id),
            "earlier_hypotheses": self._hypotheses("Ad", ad_id),
            "open_paid_signals": self._signals("Ad", ad_id),
        }

    def _country_context(self, country: str) -> Dict[str, Any]:
        rows = self.store.read(
            "MATCH (a:Ad)-[e:CONVERTS_IN]->(c:Country {id: $id}) MATCH (:Freecash)-[r:RUNS]->(a) "
            "RETURN a.id AS ad, e.tx AS tx, round(e.commission, 2) AS commission, "
            "round(e.commission / e.tx, 2) AS comm_per_tx, round(e.tx * 1.0 / r.tx, 3) AS share_of_ad_tx "
            "ORDER BY commission DESC LIMIT 25", id=country)
        totals = self.store.read(
            "MATCH (:Ad)-[e:CONVERTS_IN]->(c:Country) WITH c.id AS country, sum(e.tx) AS tx, "
            "sum(e.commission) AS commission RETURN country, tx, round(commission, 2) AS commission, "
            "round(commission / tx, 2) AS comm_per_tx ORDER BY commission DESC")
        me = next((t for t in totals if t["country"] == country), None)
        return {
            "target": {"label": "Country", **(me or {"country": country}), **self._ag_props("Country", country)},
            "country_rank_by_commission": next((i + 1 for i, t in enumerate(totals) if t["country"] == country), None),
            "median_country_comm_per_tx": round(median([t["comm_per_tx"] for t in totals]), 2),
            "top_ads_in_country": rows,
            "open_outages": self.store.read(
                "MATCH (o:Outage {status: 'open'})-[:AFFECTS]->(:Country {id: $id}) "
                "RETURN o.id AS id, o.service AS service, o.severity AS severity, o.confidence AS confidence, "
                "o.summary AS summary", id=country),
            "earlier_hypotheses": self._hypotheses("Country", country),
            "open_paid_signals": self._signals("Country", country),
        }

    def _category_context(self, category: str) -> Dict[str, Any]:
        ads = self.ad_metrics()
        members = [a for a in ads if a["category"] == category]
        return {"target": {"label": "Category", "id": category},
                "ads_in_category": members, "category_median": self._medians(members),
                "all_ads_median": self._medians(ads),
                "earlier_hypotheses": self._hypotheses("Category", category)}

    def _outage_context(self, outage_id: str) -> Dict[str, Any]:
        o = self.store.get("Outage", outage_id)
        if o is None:
            raise GraphWriteError(f"Outage {outage_id} not found")
        countries = [r["id"] for r in self.store.read(
            "MATCH (:Outage {id: $id})-[:AFFECTS]->(c:Country) RETURN c.id AS id", id=outage_id)]
        return {"target": {"label": "Outage", **o}, "affected_countries": countries,
                "countries": [self._country_context(c) for c in countries[:5]],
                "ads_at_risk": self.store.read(
                    "MATCH (a:Ad) WHERE a.ag_risk = 'outage' RETURN a.id AS ad, "
                    "a.ag_risk_commission AS commission, a.ag_risk_countries AS countries "
                    "ORDER BY commission DESC LIMIT 20")}

    def _ag_props(self, label: str, node_id: str) -> Dict[str, Any]:
        p = self.store.get(label, node_id) or {}
        return {k: v for k, v in p.items() if k.startswith("ag_")}

    def _signals(self, label: str, node_id: str) -> List[Dict[str, Any]]:
        """Open pacing/auction signals (pacing watcher) on this Ad/Country, with their numbers."""
        rel = "ABOUT" if label == "Ad" else "IN_COUNTRY"
        return self.store.read(
            f"MATCH (s:Signal {{status: 'open'}})-[:{rel}]->(:{label} {{id: $id}}) "
            "RETURN s.id AS id, s.metric AS metric, s.severity AS severity, s.state AS state, s.ad AS ad, "
            "s.country AS country, s.evidence_metric AS evidence_metric, s.observed AS observed, "
            "s.expected AS expected, s.pct_change AS pct_change, s.observations AS observations, "
            "s.hypotheses AS hypotheses, s.path_to_revenue AS path_to_revenue", id=node_id)

    def _hypotheses(self, label: str, node_id: str) -> List[Dict[str, Any]]:
        return self.store.read(
            f"MATCH (h:Hypothesis)-[:ABOUT]->(:{label} {{id: $id}}) "
            "RETURN h.text AS text, h.confidence AS confidence, h.status AS status, h.agent AS agent",
            id=node_id)

    def context(self, label: str, node_id: str) -> Dict[str, Any]:
        if label not in TARGET_LABELS:
            raise GraphWriteError(f"can investigate {sorted(TARGET_LABELS)}, not {label}")
        return {"Ad": self._ad_context, "Country": self._country_context,
                "Category": self._category_context, "Outage": self._outage_context}[label](node_id)

    # ------------------------------------------------------------------ validation
    def _valid_ref(self, label: str, node_id: str) -> bool:
        return label in BASE_LABELS | {"Outage"} and self.store.exists(label, node_id)

    @staticmethod
    def _grounded(evidence: List[str], context_numbers: set) -> bool:
        """Every evidence line quotes at least one number that is in the context."""
        def ok(line: str) -> bool:
            return any(n in context_numbers or round(n, 2) in context_numbers or round(n, 3) in context_numbers
                       for n in _numbers(line))
        return bool(evidence) and all(ok(e) for e in evidence)

    # ------------------------------------------------------------------ run
    def investigate(self, label: str, node_id: str,
                    on_step: Optional[Callable[[str, Dict[str, Any]], None]] = None) -> Investigation:
        """`on_step(kind, data)` reports progress as it happens (context, reasoning, hypothesis, dropped)."""
        step = on_step or (lambda kind, data: None)
        inv = Investigation((label, node_id))
        ctx = self.context(label, node_id)
        step("context", {"context": ctx})
        ctx_text = json.dumps(ctx, default=str)
        nums = {round(n, 3) for n in _numbers(ctx_text)} | {round(n, 2) for n in _numbers(ctx_text)}
        # shares are also commonly quoted as percentages
        nums |= {round(n * 100, 1) for n in _numbers(ctx_text) if 0 < abs(n) <= 1}

        step("reasoning", {})
        try:
            data = claude_json(f"Investigate this target.\n\nContext:\n{json.dumps(ctx, indent=1, default=str)}",
                               HYPOTHESIS_SCHEMA, system=SYSTEM, effort="high")
        except LLMUnavailable as e:
            inv.dropped.append(f"LLM unavailable: {e}")
            step("dropped", {"reason": f"LLM unavailable: {e}"})
            return inv

        for h in data.get("hypotheses", []):
            about = [(a["label"], a["id"]) for a in h.get("about", []) if self._valid_ref(a["label"], a["id"])]
            reason = None
            if not about:
                reason = "no valid 'about' target"
            elif not 0 <= float(h.get("confidence", -1)) <= 1:
                reason = "confidence out of [0,1]"
            elif not self._grounded(h.get("evidence", []), nums):
                reason = "evidence not grounded in context numbers"
            if reason:
                inv.dropped.append(f"hypothesis dropped ({reason}): {h.get('text', '')[:100]}")
                step("dropped", {"reason": reason, "text": h.get("text", "")})
                continue
            hid = self.store.add_hypothesis(h["text"], float(h["confidence"]), h["evidence"], about)
            inv.hypotheses.append(hid)
            step("hypothesis", {"id": hid, "text": h["text"], "confidence": float(h["confidence"]),
                                "evidence": h["evidence"], "about": about})
            inv.decision_log.append({"action": "add_hypothesis", "id": hid, "about": about})

        for u in data.get("node_updates", []):
            props = {p["key"]: p["value"] for p in u.get("props", [])}
            if not self._valid_ref(u["label"], u["id"]) or u["label"] == "Outage":
                inv.dropped.append(f"update dropped (bad target {u['label']} {u['id']})")
                continue
            try:
                cid = self.store.set_props(u["label"], u["id"], props, reason=u.get("reason", ""))
            except GraphWriteError as e:
                inv.dropped.append(f"update dropped ({e})")
                continue
            if cid:
                inv.updates.append(cid)
                inv.decision_log.append({"action": "set_props", "target": (u["label"], u["id"]), "props": props})
        return inv

    def run(self, targets: List[Tuple[str, str]]) -> List[Investigation]:
        results = [self.investigate(label, node_id) for label, node_id in targets]
        if self.run_overlooker:   # hand over: the overlooker reviews (and may change) what we wrote
            from agents.graph_overlooker import run_after
            self.overlooker_report = run_after(AGENT, self.store)
        return results


def summary(results: List[Investigation], store: GraphStore) -> str:
    lines = []
    for inv in results:
        lines.append(f"{inv.target[0]} {inv.target[1]}: {len(inv.hypotheses)} hypotheses, {len(inv.updates)} updates")
        for hid in inv.hypotheses:
            h = store.get("Hypothesis", hid) or {}
            lines.append(f"  - ({h.get('confidence')}) {h.get('text')}")
        for d in inv.dropped:
            lines.append(f"  ! {d}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    from agents.graph_overlooker import format_report
    targets = [tuple(t.split(":", 1)) for t in sys.argv[1:]] or [("Ad", "0003WS")]
    agent = InvestigatorAgent()
    print(summary(agent.run(targets), agent.store))
    if agent.overlooker_report:
        print(format_report(agent.overlooker_report))
