"""Outage agent: is a service our ads depend on down right now, and which ads lose commission?

Research (prompt, parsing, "unknown -> never act", country/region normalisation) is reused
unchanged from service_down_agent.ExternalOutageCampaignAgent; only the search backend (Claude +
web search instead of Gemini) and the graph side are new.

Writes (via GraphStore, all audited):
  (:Outage)-[:AFFECTS]->(:Country)               one per ongoing outage of a service
  Country.ag_outage_status / ag_outage_services  severity + services, while an outage is open
  Ad.ag_risk = 'outage', ag_risk_commission, ag_risk_countries, ag_risk_services
Outages no longer reported down are set to status 'resolved' and their flags are cleared.
When done, it starts the graph overlooker on the changes it just made.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

import networkx as nx

from graph_store import GraphStore
from llm import LLMUnavailable, claude_json
from service_down_agent import ExternalOutageCampaignAgent, OutageFinding

AGENT = "outage_agent"
MIN_CONFIDENCE = ExternalOutageCampaignAgent.MIN_CONFIDENCE_TO_PAUSE  # 0.6: below -> record, don't flag

# Same fields build_prompt() asks for.
OUTAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["operational", "degraded", "major_outage", "unknown"]},
        "ongoing": {"type": "boolean"},
        "is_global": {"type": "boolean"},
        "affected_countries": {"type": "array", "items": {"type": "string"}},
        "affected_regions": {"type": "array", "items": {"type": "string"}},
        "affected_products": {"type": "array", "items": {"type": "string"}},
        "started_at": {"type": ["string", "null"]},
        "confidence": {"type": "number"},
        "sources": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["status", "ongoing", "is_global", "affected_countries", "confidence", "sources", "summary"],
    "additionalProperties": False,
}


def claude_search(prompt: str) -> str:
    """search_fn for ExternalOutageCampaignAgent: prompt -> JSON text. Failures become 'unknown'."""
    try:
        return json.dumps(claude_json(prompt, OUTAGE_SCHEMA, web_search=True))
    except LLMUnavailable as e:
        return json.dumps({"status": "unknown", "summary": f"Not checked: {e}"})


@dataclass
class OutageImpact:
    service: str
    severity: str
    confidence: float
    countries: List[str]
    flagged: bool                       # confidence high enough to set ag_* flags
    ads: Dict[str, float] = field(default_factory=dict)   # ad id -> commission at risk
    outage_id: Optional[str] = None

    @property
    def commission_at_risk(self) -> float:
        return round(sum(self.ads.values()), 2)


class OutageAgent:
    def __init__(self, store: Optional[GraphStore] = None,
                 search_fn: Callable[[str], str] = claude_search,
                 services: Optional[Dict[str, str]] = None,
                 top_countries: int = 15, run_overlooker: bool = True):
        self.store = store or GraphStore(agent=AGENT)
        self.research = ExternalOutageCampaignAgent(
            nx.DiGraph(), search_fn=search_fn,
            services=services or dict(ExternalOutageCampaignAgent.DEFAULT_SERVICES))
        self.top_countries = top_countries
        self.run_overlooker = run_overlooker
        self.uncertainties: List[str] = []
        self.overlooker_report = None

    # ------------------------------------------------------------------ graph reads
    def countries_by_commission(self) -> List[str]:
        rows = self.store.read(
            "MATCH (:Ad)-[e:CONVERTS_IN]->(c:Country) "
            "RETURN c.id AS country, sum(e.commission) AS commission ORDER BY commission DESC")
        return [r["country"] for r in rows]

    def ads_in(self, countries: List[str]) -> Dict[str, float]:
        rows = self.store.read(
            "MATCH (a:Ad)-[e:CONVERTS_IN]->(c:Country) WHERE c.id IN $countries "
            "RETURN a.id AS ad, sum(e.commission) AS commission", countries=countries)
        return {r["ad"]: round(r["commission"], 2) for r in rows}

    def open_outages(self) -> Dict[str, Dict]:
        rows = self.store.read(
            "MATCH (o:Outage {status: 'open'}) OPTIONAL MATCH (o)-[:AFFECTS]->(c:Country) "
            "RETURN properties(o) AS o, collect(c.id) AS countries")
        return {r["o"]["service"]: {**r["o"], "countries": r["countries"]} for r in rows}

    # ------------------------------------------------------------------ run
    def run(self) -> List[OutageImpact]:
        all_countries = self.countries_by_commission()
        checked = all_countries[: self.top_countries]
        findings = self.research.search_outages(checked)

        unknown = [f.service_name for f in findings if f.status == "unknown"]
        if unknown:
            self.uncertainties.append("Not checkable (status unknown): " + ", ".join(unknown))

        existing = self.open_outages()
        impacts: List[OutageImpact] = []
        for f in findings:
            if not f.is_down:
                # only resolve on a definite "not down", never on "unknown"
                if f.service_name in existing and f.status != "unknown":
                    self.store.set_status("Outage", existing[f.service_name]["id"], "resolved",
                                          reason=f"{f.service_name} reported {f.status}")
                continue
            impacts.append(self._record(f, all_countries, existing.get(f.service_name)))

        self._sync_flags()
        if self.run_overlooker:   # hand over: the overlooker reviews (and may change) what we wrote
            from agents.graph_overlooker import run_after
            self.overlooker_report = run_after(AGENT, self.store)
        return impacts

    def _record(self, f: OutageFinding, graph_countries: List[str], existing: Optional[Dict]) -> OutageImpact:
        countries = [c for c in graph_countries if f.affects(c)]
        impact = OutageImpact(f.service_name, f.severity, f.confidence, countries,
                              flagged=f.confidence >= MIN_CONFIDENCE and bool(countries))
        if not countries:
            self.uncertainties.append(f"{f.service_name} down, but in none of our countries")
            return impact
        impact.ads = self.ads_in(countries)
        if existing and sorted(existing["countries"]) == sorted(countries):
            impact.outage_id = existing["id"]          # same outage still ongoing
            return impact
        if existing:                                   # scope changed: close old, open new
            self.store.set_status("Outage", existing["id"], "resolved", reason="superseded")
        impact.outage_id = self.store.add_outage(
            service=f.service_name, category=f.category, severity=f.severity,
            confidence=f.confidence, countries=countries, sources=f.sources,
            summary=f.summary, started_at=f.started_at)
        return impact

    def _sync_flags(self) -> None:
        """Make ag_* outage flags on Country/Ad match the open, confident Outage nodes."""
        rows = self.store.read(
            "MATCH (o:Outage {status: 'open'})-[:AFFECTS]->(c:Country) WHERE o.confidence >= $min "
            "RETURN c.id AS country, collect(o.service) AS services, collect(o.severity) AS severities",
            min=MIN_CONFIDENCE)
        rank = {"major": 2, "partial": 1}
        want_country = {r["country"]: {
            "ag_outage_status": max(r["severities"], key=lambda s: rank.get(s, 0)),
            "ag_outage_services": sorted(set(r["services"]))} for r in rows}

        want_ad: Dict[str, Dict] = {}
        if want_country:
            for r in self.store.read(
                    "MATCH (a:Ad)-[e:CONVERTS_IN]->(c:Country) WHERE c.id IN $countries "
                    "RETURN a.id AS ad, sum(e.commission) AS commission, collect(c.id) AS countries",
                    countries=list(want_country)):
                services = sorted({s for c in r["countries"] for s in want_country[c]["ag_outage_services"]})
                want_ad[r["ad"]] = {"ag_risk": "outage", "ag_risk_commission": round(r["commission"], 2),
                                    "ag_risk_countries": sorted(r["countries"]),
                                    "ag_risk_services": services}

        clear_country = {"ag_outage_status": None, "ag_outage_services": None}
        clear_ad = {"ag_risk": None, "ag_risk_commission": None, "ag_risk_countries": None,
                    "ag_risk_services": None}
        flagged_countries = {r["id"] for r in self.store.read(
            "MATCH (c:Country) WHERE c.ag_outage_status IS NOT NULL RETURN c.id AS id")}
        flagged_ads = {r["id"] for r in self.store.read(
            "MATCH (a:Ad) WHERE a.ag_risk = 'outage' RETURN a.id AS id")}

        for c in flagged_countries | set(want_country):
            self.store.set_props("Country", c, want_country.get(c, clear_country), reason="outage flags")
        for a in flagged_ads | set(want_ad):
            self.store.set_props("Ad", a, want_ad.get(a, clear_ad), reason="outage flags")


def summary(impacts: List[OutageImpact]) -> str:
    if not impacts:
        return "No ongoing outages of monitored services."
    lines = []
    for i in impacts:
        top = sorted(i.ads.items(), key=lambda kv: -kv[1])[:5]
        lines.append(f"{i.service} {i.severity} (conf {i.confidence:.2f}{'' if i.flagged else ', not flagged'}) "
                     f"in {','.join(i.countries) or '-'}: {len(i.ads)} ads, €{i.commission_at_risk} commission "
                     f"at risk; top: {', '.join(f'{a} €{c}' for a, c in top)}")
    return "\n".join(lines)


if __name__ == "__main__":
    from agents.graph_overlooker import format_report
    agent = OutageAgent()
    print(summary(agent.run()))
    for u in agent.uncertainties:
        print("  ?", u)
    if agent.overlooker_report:
        print(format_report(agent.overlooker_report))
