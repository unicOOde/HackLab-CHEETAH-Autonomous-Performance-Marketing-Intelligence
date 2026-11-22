"""Review hooks: how the product manager (fix discussions) and the graph engineer (proposals) hear
out the other agents. One synchronous call per agent, `-> Stance`, no bus round-trip
(agents/orchestration_plan.md §5.3/§5.5).

Pacing, outage and graph-engineer stances are deterministic and read the graph. The investigator
stance is a small-tier LLM call; without the LLM it abstains as `unknown` (unknown never acts).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from graph_store import GraphStore

SUPPORT, OBJECT, ABSTAIN = "support", "object", "abstain"
SMALL_MODEL = os.getenv("CLAUDE_MODEL_SMALL", "claude-haiku-5-5")

# "Bid Multiplier 1.727 vs expected 0.9232 (+87%, z=+6.8)", "CPC €0.79 vs expected €0.51 (+56%, z=+4.9)"
_MEASURED = re.compile(r"^(?P<metric>[A-Za-z&][A-Za-z &]+?) €?(?P<obs>[\d,.]+) vs expected €?(?P<exp>[\d,.]+) "
                       r"\((?P<pct>[+-]?\d+)%, z=(?P<z>[+-]?[\d.]+)\)")


@dataclass
class Stance:
    agent: str
    stance: str                      # support | object | abstain
    argument: str
    evidence: List[str] = field(default_factory=list)
    unknown: bool = False            # abstained because something was unavailable

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def measured_metrics(report: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Metric -> {obs, exp, pct, z, line} from a pacing report: its own metric plus every
    'X a vs expected b (+p%, z=+z)' line. Only numbers the detector printed."""
    out: Dict[str, Dict[str, Any]] = {}
    if report.get("metric") and report.get("observed") is not None and report.get("z") is not None:
        out[report["metric"]] = {"obs": report["observed"], "exp": report["expected"],
                                 "pct": report.get("pct_change"), "z": report["z"],
                                 "line": f"{report['metric']} {report['observed']} vs expected "
                                         f"{report['expected']} (z={report['z']:+})"}
    for line in list(report.get("observations") or []) + list(report.get("downstream") or []):
        m = _MEASURED.match(line)
        if m:
            f = lambda k: float(m.group(k).replace(",", ""))   # noqa: E731
            out.setdefault(m.group("metric").strip(), {"obs": f("obs"), "exp": f("exp"),
                                                       "pct": f("pct") / 100, "z": f("z"), "line": line})
    return out


def metric_edges(store: GraphStore) -> List[Dict[str, Any]]:
    return store.read("MATCH (a:Metric)-[e:FORMULA|INFLUENCES]->(b:Metric) "
                      "RETURN a.id AS src, type(e) AS rel, b.id AS dst, "
                      "coalesce(e.ag_status, e.status) AS status, e.prior AS prior")


# --------------------------------------------------------------------------- deterministic hooks
def pacing_stance(store: GraphStore, report: Dict[str, Any], playbook: Optional[str]) -> Stance:
    """Owner of paid levers (P*). Supports only its own deterministic detection, and objects when a
    PlannedEvent on the slice makes the movement intended."""
    agent = "pacing_watcher"
    if report.get("source") != "pacing_watcher":
        return Stance(agent, ABSTAIN, "not a pacing detection; no paid lever involved")
    day = (report.get("sim_now") or "")[:10]
    events = store.read(
        "MATCH (p:PlannedEvent) WHERE p.start <= $t AND p.end > $t "
        "AND (EXISTS { (p)-[:APPLIES_TO]->(:Ad {id: $ad}) } OR EXISTS { (p)-[:APPLIES_TO]->(:Country {id: $c}) }) "
        "RETURN p.id AS id, p.name AS name, p.expected_lift AS lift",
        t=report.get("sim_now") or f"{day}T00:00:00Z", ad=report.get("ad"), c=report.get("country"))
    if events:
        e = events[0]
        return Stance(agent, OBJECT, f"planned event '{e['name']}' (expected x{e['lift']}) covers this slice: "
                                     "the extra spend is intended", [f"PlannedEvent {e['id']}"])
    if not playbook or not playbook.startswith("P"):
        return Stance(agent, ABSTAIN, "lever is not a paid-acquisition lever")
    hyps = report.get("hypotheses") or []
    return Stance(agent, SUPPORT, f"own detection {report.get('issue_key')}: {hyps[0] if hyps else 'no hypothesis'}",
                  (report.get("observations") or [])[:3])


def outage_stance(store: GraphStore, countries: Sequence[str]) -> Stance:
    """Is an open, confident outage on these countries the better (external) explanation?"""
    agent = "outage_agent"
    countries = [c for c in countries if c]
    rows = store.read(
        "MATCH (o:Outage {status: 'open'})-[:AFFECTS]->(c:Country) WHERE c.id IN $cs AND o.confidence >= 0.8 "
        "RETURN o.id AS id, o.service AS service, o.confidence AS conf, collect(c.id) AS countries",
        cs=countries)
    if rows:
        o = rows[0]
        return Stance(agent, OBJECT, f"open outage of {o['service']} (confidence {o['conf']}) affects "
                                     f"{', '.join(o['countries'])}: an external cause explains this better",
                      [f"Outage {o['id']}"])
    return Stance(agent, ABSTAIN, f"no open outage with confidence >= 0.8 on {', '.join(countries) or '-'}")


def graph_stance(edges: List[Dict[str, Any]], path: Sequence[str], lever_metric: Optional[str]) -> Stance:
    """Graph engineer: is every INFLUENCES edge on the cause -> Revenue path validated? Which other
    metrics does the lever touch (side effects)?"""
    agent = "graph_engineer"
    by_pair = {(e["src"], e["dst"]): e for e in edges}
    unvalidated, missing = [], []
    for a, b in zip(path, path[1:]):
        e = by_pair.get((a, b))
        if e is None:
            missing.append(f"{a} -> {b}")
        elif e["rel"] == "INFLUENCES" and e["status"] != "validated":
            unvalidated.append(f"{a} -INFLUENCES-> {b} ({e['status']}, prior {e['prior']})")
    side = side_effects(edges, path, lever_metric)
    note = f"; side effects via {lever_metric}: {', '.join(side)}" if side else ""
    if missing:
        return Stance(agent, OBJECT, "path to Revenue is not in the graph: " + "; ".join(missing) + note, missing)
    if unvalidated:
        return Stance(agent, OBJECT, "cause -> Revenue path relies on unvalidated edges" + note, unvalidated)
    if not path:
        return Stance(agent, ABSTAIN, "no metric path given")
    return Stance(agent, SUPPORT, "every edge on the path is validated or an exact FORMULA" + note,
                  [" -> ".join(path)])


def side_effects(edges: List[Dict[str, Any]], path: Sequence[str], lever_metric: Optional[str]) -> List[str]:
    return sorted({e["dst"] for e in edges if lever_metric and e["src"] == lever_metric and e["dst"] not in path})


# --------------------------------------------------------------------------- LLM hook
INVESTIGATOR_SCHEMA = {
    "type": "object",
    "properties": {"stance": {"type": "string", "enum": [SUPPORT, OBJECT, ABSTAIN]},
                   "argument": {"type": "string"},
                   "evidence": {"type": "array", "items": {"type": "string"}}},
    "required": ["stance", "argument", "evidence"], "additionalProperties": False,
}
INVESTIGATOR_SYSTEM = """You are the investigator in a fix discussion for a performance-marketing system.
You get an issue, the candidate fix and the evidence the other agents collected. Say whether the
cause behind the fix is supported by that evidence. Object only with evidence: every evidence line
must quote a number that appears in the dossier. Abstain when the dossier doesn't let you judge."""


def _numbers(text: str) -> List[float]:
    return [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", text.replace(",", ""))]


def investigator_stance(item: Dict[str, Any], dossier: Dict[str, Any]) -> Stance:
    agent = "investigator"
    try:
        from llm import LLMUnavailable, claude_json
    except Exception as exc:   # anthropic SDK missing
        return Stance(agent, ABSTAIN, f"unknown: {exc}", unknown=True)
    context = json.dumps({"item": item, "dossier": dossier}, default=str)
    try:
        ans = claude_json(context, INVESTIGATOR_SCHEMA, system=INVESTIGATOR_SYSTEM, effort="low", model=SMALL_MODEL)
    except LLMUnavailable as exc:
        return Stance(agent, ABSTAIN, f"unknown: LLM unavailable ({str(exc)[:120]})", unknown=True)
    known = set(_numbers(context))
    evidence = [e for e in ans.get("evidence", []) if _numbers(e) and set(_numbers(e)) <= known]
    stance = ans.get("stance") if ans.get("stance") in (SUPPORT, OBJECT, ABSTAIN) else ABSTAIN
    if stance == OBJECT and not evidence:   # an objection without checkable numbers doesn't count
        return Stance(agent, ABSTAIN, "objection dropped: no evidence line quoted numbers from the dossier: "
                                      + str(ans.get("argument", ""))[:300])
    return Stance(agent, stance, str(ans.get("argument", ""))[:600], evidence)
