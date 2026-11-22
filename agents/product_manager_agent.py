"""Product manager ("Compass"): triage, fix discussions and the business brief.

Spec: agents/product_manager.md (role), agents/fix_playbook.md (fixes + gates),
agents/orchestration_plan.md §5.2 / §5.5 (mechanics).

One run = one cycle over the inbox:
  1. collect reports (pacing, outage, investigator), feedback and graph-engineer decisions;
  2. one fix discussion per issue: frame -> playbook candidate -> one expert round (stances.py)
     -> seven deterministic gates. The model never decides an outcome; `no_fix` is the default;
  3. proposals to the graph engineer (validate an INFLUENCES edge the discussion relied on);
  4. a brief following CLAUDE.md's report contract, optionally with an LLM narrative whose
     numbers must all appear in the brief's own data.

Implemented playbook triggers: P1 (pacing bug, fully measured), P5 (competitor pull-back: day 1 is
`monitor`; the bid elasticity it needs isn't measured, so later days end `no_fix`), and the
not-fixable mappings (tracking loss, planned promotion, external outage, auction pressure up).
P2-P4, P6, P7 and A1 need multi-day scans that no watcher reports yet: they are never proposed.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Dict, List, Optional, Tuple

from graph_store import GraphStore
from agents.stances import (OBJECT, SUPPORT, Stance, graph_stance, investigator_stance, measured_metrics,
                            metric_edges, outage_stance, pacing_stance, side_effects)

AGENT = "product_manager"
LARGE_MODEL = os.getenv("CLAUDE_MODEL_LARGE", "claude-opus-5-5")
GATES = ("supported_cause", "fixable", "measured_lever", "positive_impact", "enough_data",
         "experts_agree", "still_relevant")
MIN_BASE_DAYS, MIN_CONVERSIONS, MIN_Z_FOR_EDGE = 5, 30, 3.0

SLICE_Q = """
MATCH (a:Ad {id: $ad})-[e:PAID_IN_HOUR]->(h:Hour)
WHERE e.country = $country AND h.ts >= $since AND h.ts < $until
RETURN h.day AS day, h.hour AS hour, sum(e.spend) AS spend, sum(e.clicks) AS clicks,
       sum(e.conversions) AS conversions, sum(e.revenue) AS revenue, max(e.budget) AS budget,
       sum(e.bid_mult * e.auctions) AS bm_x, sum(e.auctions) AS auctions
ORDER BY day, hour
"""


@dataclass
class PMResult:
    discussions: List[Dict[str, Any]] = field(default_factory=list)
    fixes: List[Dict[str, Any]] = field(default_factory=list)
    proposals: List[Dict[str, Any]] = field(default_factory=list)
    brief: Optional[Dict[str, Any]] = None
    uncertainties: List[str] = field(default_factory=list)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{datetime.now(timezone.utc):%Y%m%d%H%M%S}_{uuid.uuid4().hex[:6]}"


def _hyp(h: str) -> Tuple[float, str]:
    conf, _, text = h.partition("|")
    try:
        return float(conf), text.strip()
    except ValueError:
        return 0.0, h


def _gate(ok: bool, detail: str) -> Dict[str, Any]:
    return {"pass": bool(ok), "detail": detail}


# --------------------------------------------------------------------------- P1 measurement
def measure_p1(store: GraphStore, r: Dict[str, Any]) -> Dict[str, Any]:
    """Today vs. the slice's own previous days, all from PAID_IN_HOUR (simulated)."""
    now = datetime.strptime(r["sim_now"][:13], "%Y-%m-%dT%H")
    day0 = now.replace(hour=0)
    rows = store.read(SLICE_Q, ad=r["ad"], country=r["country"],
                      since=(day0 - timedelta(days=7)).strftime("%Y-%m-%dT%H:00:00Z"),
                      until=now.strftime("%Y-%m-%dT%H:00:00Z"))
    days: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        days.setdefault(row["day"], []).append(row)
    today = days.pop(day0.strftime("%Y-%m-%d"), [])

    def exhausted_at(hours: List[Dict[str, Any]]) -> Optional[int]:
        cum = 0.0
        for h in hours:
            cum += h["spend"] or 0
            if h["budget"] and cum >= 0.98 * h["budget"]:
                return h["hour"]
        return None

    def bid(hours: List[Dict[str, Any]]) -> Optional[float]:
        a = sum(h["auctions"] or 0 for h in hours)
        return sum(h["bm_x"] or 0 for h in hours) / a if a else None

    base = {d: hs for d, hs in days.items() if len(hs) == 24 and sum(h["spend"] or 0 for h in hs) > 0}
    out: Dict[str, Any] = {"base_days": len(base)}
    if not today or not base:
        return out
    spend_today = sum(h["spend"] or 0 for h in today)
    rev_today = sum(h["revenue"] or 0 for h in today)
    roas = [sum(h["revenue"] or 0 for h in hs) / sum(h["spend"] or 0 for h in hs) for hs in base.values()]
    exh = [exhausted_at(hs) for hs in base.values()]
    out.update(
        budget=today[0]["budget"], spend_today=round(spend_today, 2), revenue_today=round(rev_today, 2),
        exhausted_today=exhausted_at(today), normal_exhaustion=median(e if e is not None else 24 for e in exh),
        bid_today=bid(today), bid_base=median(b for b in (bid(hs) for hs in base.values()) if b is not None),
        conversions_base=sum(h["conversions"] or 0 for hs in base.values() for h in hs),
        roas_min=min(roas), roas_max=max(roas),
        # same budget at the baseline days' return on spend, minus what today's spend actually returned
        impact_low=round(min(roas) * spend_today - rev_today, 2),
        impact_high=round(max(roas) * spend_today - rev_today, 2))
    return out


# --------------------------------------------------------------------------- the agent
class ProductManager:
    def __init__(self, store: Optional[GraphStore] = None, llm: bool = True, llm_off_reason: str = "LLM disabled"):
        self.store = store or GraphStore(agent=AGENT)
        self.llm = llm
        self.llm_off_reason = llm_off_reason
        self._edges: Optional[List[Dict[str, Any]]] = None

    def edges(self) -> List[Dict[str, Any]]:
        if self._edges is None:
            self._edges = metric_edges(self.store)
        return self._edges

    # ------------------------------------------------------------------ cycle
    def run(self, messages: List[Dict[str, Any]], state: Dict[str, Any], now: Optional[str]) -> PMResult:
        res = PMResult()
        issues = state.setdefault("issues", {})
        rejections = state.setdefault("rejections", {})
        props = state.setdefault("proposals", {})
        investigations = state.setdefault("investigations", {})
        brief = {"sim_now": now, "issues": [], "resolved": [], "explained": [], "outages": [],
                 "investigations": [], "graph_decisions": [], "feedback": []}
        todo: Dict[str, Tuple[Dict[str, Any], List[str]]] = {}

        for m in messages:
            t, p = m["type"], m.get("payload") or {}
            if t == "clock_wrapped":   # the simulated month restarted: old issues are history now
                issues.clear()
                res.uncertainties.append(f"simulated clock wrapped to {p.get('now')}: open issues reset")
            elif t == "fix_feedback":
                brief["feedback"].append(p)
                if p.get("decision") == "rejected":
                    rejections[f"{p.get('issue_key')}|{p.get('playbook')}"] = {
                        "ts": p.get("ts"), "reason": p.get("reason"),
                        "observed": (issues.get(p.get("issue_key")) or {}).get("last", {}).get("observed")}
            elif t == "decision":
                brief["graph_decisions"].append({k: p.get(k) for k in ("proposal_id", "outcome", "target", "reason")})
                pr = props.get(p.get("edge_key") or "")
                if pr and pr["proposal_id"] == p.get("proposal_id"):
                    pr["status"] = p.get("outcome")
                if p.get("outcome") == "applied":   # an edge we waited on is validated now: discuss again
                    for key, iss in issues.items():
                        if p.get("proposal_id") in iss.get("waiting_on", []):
                            todo[key] = (iss["last"], [m["msg_id"]])
            elif t == "report" and p.get("source") == "investigator":
                tgt = p.get("target") or []
                brief["investigations"].append({"target": tgt, "error": p.get("error"),
                                                 "hypotheses": [{k: h.get(k) for k in ("text", "confidence", "evidence")}
                                                                for h in p.get("hypotheses") or []]})
                if len(tgt) == 2 and tgt[0] == "Ad" and not p.get("error"):
                    investigations[tgt[1]] = p.get("hypotheses") or []
                    for key, iss in issues.items():
                        if iss["last"].get("ad") == tgt[1] and iss.get("outcome") != "fix_proposed":
                            todo[key] = (iss["last"], [m["msg_id"]])
            elif t == "report" and p.get("kind") == "outage":
                brief["outages"].append({k: p.get(k) for k in ("service", "severity", "confidence", "countries",
                                                                "flagged", "commission_at_risk")})
                if p.get("flagged"):
                    todo[f"Outage|{p.get('service')}"] = (p, [m["msg_id"]])
            elif t == "report" and p.get("change") == "resolved":
                brief["resolved"].append(p.get("issue_key"))
                issues.pop(p.get("issue_key"), None)
                todo.pop(p.get("issue_key"), None)
            elif t == "report" and p.get("issue_key"):
                todo[p["issue_key"]] = (p, [m["msg_id"]])
            elif t == "cycle_summary":
                for text in p.get("explained") or []:
                    key = "Explained|" + text.split(":")[0]
                    day = (p.get("sim_now") or "")[:10]
                    if state.setdefault("explained", {}).get(key) != day:
                        state["explained"][key] = day
                        todo[key] = ({"source": "pacing_watcher", "kind": "explained", "issue_key": key,
                                      "sim_now": p.get("sim_now"), "text": text, "data_side": "simulated"},
                                     [m["msg_id"]])
                res.uncertainties += p.get("uncertainties") or []

        for key, (report, refs) in todo.items():
            d, fix = self.discuss(report, refs, issues, rejections, props, investigations, res)
            res.discussions.append(d)
            if fix:
                res.fixes.append(fix)
            brief["issues"].append({"issue_key": key, "what_changed": d["frame"]["what_changed"],
                                    "cause": d["frame"]["cause"], "impact": d["frame"]["impact"],
                                    "outcome": d["outcome"], "reason": d.get("reason"),
                                    "discussion_id": d["discussion_id"], "fix_id": d.get("fix_id")})
        res.brief = self._brief(brief, res)
        return res

    # ------------------------------------------------------------------ one discussion
    def discuss(self, r: Dict[str, Any], refs: List[str], issues: Dict[str, Any], rejections: Dict[str, Any],
                props: Dict[str, Any], investigations: Dict[str, Any], res: PMResult
                ) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        key, today = r["issue_key"], (r.get("sim_now") or "")[:10]
        iss = issues.setdefault(key, {"first_seen": today})
        iss["last"] = r
        hyps = sorted((_hyp(h) for h in r.get("hypotheses") or []), reverse=True)
        inv = [h for h in investigations.get(r.get("ad"), []) if (h.get("confidence") or 0) >= 0.7 and h.get("evidence")]
        d: Dict[str, Any] = {
            "discussion_id": _new_id("disc"), "issue_key": key, "sim_now": r.get("sim_now"),
            "data_side": r.get("data_side"), "ref_ids": refs,
            "frame": {"what_changed": self._what_changed(r), "cause": hyps[0][1] if hyps else r.get("text"),
                      "cause_confidence": hyps[0][0] if hyps else None,
                      "impact": [x for x in r.get("downstream") or [] if "€" in x or "risk" in x][:2]
                                + ([f"commission at risk €{r['commission_at_risk']}"] if r.get("commission_at_risk") else []),
                      "investigator": [h.get("text") for h in inv][:2]},
            "candidates": [], "stances": [], "gates": {}, "proposal_ids": []}

        def end(outcome: str, reason: Optional[str], summary: str):
            d.update(outcome=outcome, reason=reason, summary=summary)
            iss["outcome"] = outcome
            return d, None

        # -- not fixable / no playbook trigger (fix_playbook.md §5)
        kind, st = r.get("kind"), r.get("state") or ""
        if kind == "explained":
            return end("no_fix", "innocent_change", f"Matches a planned event: {r.get('text')}")
        if kind == "data_completeness":
            return end("no_fix", "data_issue", "Tracking loss: clicks/conversions are undercounted. Fix the "
                       "tracking, never cut budget because conversions 'dropped'.")
        if kind == "outage":
            return end("no_fix", "not_fixable_cause", f"Third-party outage ({r.get('service')}): wait for recovery. "
                       "A3 (pause during the outage) can't be evaluated: outages are wall-clock, paid "
                       "telemetry is simulated October, so conversions-down isn't measurable on one axis.")
        if st == "auction_pressure_up":
            return end("no_fix", "not_fixable_cause", "Competitors bid harder: an external auction change. "
                       "Raising bids to follow would buy the same clicks at a higher CPC.")
        if st == "ahead_of_pace":
            return end("monitor", None, "Spend runs ahead of plan but the budget isn't exhausted yet; "
                       "P1 needs an exhaustion at least 2 h earlier than normal.")
        if st == "exhausted":
            playbook, lever = "P1", "Bid Multiplier"
        elif st == "auction_pressure_down":
            if iss["first_seen"] == today:
                return end("monitor", None, "Possible competitor pull-back, first day: P5 is only proposed from "
                           "the second consecutive day.")
            playbook, lever = "P5", "Bid Multiplier"
        else:
            return end("no_fix", "insufficient_evidence", f"No playbook entry matches state '{st}'.")

        d["candidates"].append(playbook)
        # -- one expert round
        path = r.get("path_to_revenue") or []
        edges = self.edges()
        dossier = {"report": {k: r.get(k) for k in ("issue_key", "observed", "expected", "observations",
                                                     "hypotheses", "downstream")},
                   "investigator_hypotheses": inv}
        stances: List[Stance] = [pacing_stance(self.store, r, playbook),
                                 outage_stance(self.store, [r.get("country")]),
                                 investigator_stance({"issue_key": key, "fix": playbook}, dossier) if self.llm
                                 else Stance("investigator", "abstain", f"unknown: {self.llm_off_reason}", unknown=True),
                                 graph_stance(edges, path, lever)]
        d["stances"] = [s.to_dict() for s in stances]

        # -- gates
        g = d["gates"]
        top = hyps[0][0] if hyps else 0.0
        g["supported_cause"] = _gate(top >= 0.7 or bool(inv), f"top detector hypothesis {top:.2f}"
                                     + (f", {len(inv)} investigator hypothesis(es) >= 0.7" if inv else ""))
        g["fixable"] = _gate(True, f"{playbook} is a playbook lever")
        impact = None
        if playbook == "P1":
            m = measure_p1(self.store, r)
            d["measurement"] = m
            early = (m.get("exhausted_today") is not None and m.get("normal_exhaustion") is not None
                     and m["exhausted_today"] <= m["normal_exhaustion"] - 2)
            lever_ok = early and m.get("bid_today") and m.get("bid_base") and m["bid_today"] > m["bid_base"]
            g["measured_lever"] = _gate(lever_ok, (
                f"budget exhausted at {m.get('exhausted_today')}:00 vs normally {m.get('normal_exhaustion')}:00; "
                f"bid multiplier {m['bid_today']:.2f} today vs {m['bid_base']:.2f} on {m['base_days']} baseline days")
                if m.get("bid_today") and m.get("bid_base") else f"slice history incomplete ({m.get('base_days', 0)} baseline days)")
            if "impact_low" in m:
                impact = {"metric": "Revenue", "low": m["impact_low"], "high": m["impact_high"], "unit": "EUR",
                          "horizon": "per day the controller stays broken (same budget)"}
            g["positive_impact"] = _gate(bool(impact) and impact["low"] > 0, (
                f"€{m['spend_today']:.2f} spent today returned €{m['revenue_today']:.2f}; at the baseline ROAS "
                f"{m['roas_min']:.2f}..{m['roas_max']:.2f} the same spend returns €{impact['low']:+.2f}..€{impact['high']:+.2f} more")
                if impact else "impact not computable")
            g["enough_data"] = _gate(m.get("base_days", 0) >= MIN_BASE_DAYS and m.get("conversions_base", 0) >= MIN_CONVERSIONS,
                                     f"{m.get('base_days', 0)} baseline days (min {MIN_BASE_DAYS}), "
                                     f"{m.get('conversions_base', 0)} baseline conversions (min {MIN_CONVERSIONS})")
        else:   # P5
            g["measured_lever"] = _gate(False, "the effect of raising bids (bid -> CPC elasticity) isn't measured in our data")
            g["positive_impact"] = _gate(False, "no measured lever, so no impact range")
            g["enough_data"] = _gate(True, "second day of the effect")
        objections = [s for s in stances if s.stance == OBJECT]
        owner = next(s for s in stances if s.agent == "pacing_watcher")
        g["experts_agree"] = _gate(not objections and owner.stance == SUPPORT,
                                   "; ".join(f"{s.agent} objects: {s.argument}" for s in objections)
                                   or f"owner pacing_watcher: {owner.stance}")
        rej = rejections.get(f"{key}|{playbook}")
        rejected_same = bool(rej) and rej.get("observed") == r.get("observed")
        g["still_relevant"] = _gate(r.get("change") != "resolved" and not rejected_same,
                                    "rejected by a human with no new evidence since: " + str(rej.get("reason") or "")
                                    if rejected_same else "issue open" + (", new evidence since a rejection" if rej else ""))

        failed = [n for n in GATES if not g[n]["pass"]]
        if not failed:
            fix = self._fix(r, playbook, impact, d, stances, side_effects(edges, path, lever))
            d.update(outcome="fix_proposed", reason=None, fix_id=fix["fix_id"],
                     summary=f"{playbook}: {fix['action']}. Expected {impact['low']:+.2f}..{impact['high']:+.2f} EUR "
                             f"{impact['horizon']}.")
            iss["outcome"] = "fix_proposed"
            return d, fix
        # the only blocker is the graph engineer's unvalidated path: turn it into graph knowledge
        ge = next(s for s in stances if s.agent == "graph_engineer")
        if failed == ["experts_agree"] and objections == [ge]:
            props_out = self._edge_proposals(r, ge, props, d)
            if props_out:
                iss["waiting_on"] = [p["proposal_id"] for p in props_out]
                res.proposals += [p for p in props_out if p.get("new")]
                return end("graph_only", None, f"{playbook} passes every other gate, but its cause -> Revenue path "
                           "relies on unvalidated INFLUENCES edges. Asked the graph engineer to validate "
                           f"{len(props_out)} edge(s) with the measured evidence; the fix is re-discussed once they are.")
        reason = ("expert_objection" if failed[0] == "experts_agree" else
                  "previously_rejected" if failed[0] == "still_relevant" and rejected_same else
                  "already_resolving" if failed[0] == "still_relevant" else "insufficient_evidence")
        if failed[0] == "enough_data":
            return end("monitor", None, f"{playbook}: {g['enough_data']['detail']}")
        return end("no_fix", reason, f"{playbook} stopped at gate '{failed[0]}': {g[failed[0]]['detail']}")

    # ------------------------------------------------------------------ pieces
    @staticmethod
    def _what_changed(r: Dict[str, Any]) -> str:
        if r.get("kind") == "explained":
            return r.get("text", "")
        if r.get("kind") == "outage":
            return f"{r.get('service')} {r.get('severity')} outage in {', '.join(r.get('countries') or [])}"
        pct = f" ({r['pct_change']:+.0%})" if r.get("pct_change") is not None else ""
        return (f"{r.get('evidence_metric') or r.get('metric')} {r.get('observed')} vs expected {r.get('expected')}{pct} "
                f"for ad {r.get('ad')} in {r.get('country')}, {r.get('window')}")

    def _edge_proposals(self, r: Dict[str, Any], ge: Stance, props: Dict[str, Any],
                        d: Dict[str, Any]) -> List[Dict[str, Any]]:
        measured = measured_metrics(r)
        out = []
        for line in ge.evidence:
            m = re.match(r"(.+?) -INFLUENCES-> (.+?) \(", line)
            if not m:
                continue
            a, b = m.group(1), m.group(2)
            ma, mb = measured.get(a), measured.get(b)
            if not (ma and mb and abs(ma["z"]) >= MIN_Z_FOR_EDGE and abs(mb["z"]) >= MIN_Z_FOR_EDGE):
                continue   # an edge only gets proposed when both ends moved measurably in this issue
            edge_key = f"{a}->{b}"
            prev = props.get(edge_key)
            if prev and prev["status"] in ("pending", "applied"):
                out.append({"proposal_id": prev["proposal_id"]})
                continue
            pid = _new_id("prop")
            props[edge_key] = {"proposal_id": pid, "status": "pending", "issue_key": r["issue_key"]}
            out.append({"new": True, "proposal_id": pid, "kind": "graph_change", "op": "validate_influence",
                        "edge_key": edge_key, "src": a, "dst": b, "issue_key": r["issue_key"],
                        "signal_id": r.get("signal_id"), "ad": r.get("ad"), "country": r.get("country"),
                        "evidence": [ma["line"], mb["line"]], "discussion_id": d["discussion_id"],
                        "rationale": f"{a} and {b} both moved significantly in {r['issue_key']} "
                                     f"(z {ma['z']:+} and {mb['z']:+}), as the edge predicts"})
        d["proposal_ids"] = [p["proposal_id"] for p in out]
        return out

    @staticmethod
    def _fix(r: Dict[str, Any], playbook: str, impact: Dict[str, Any], d: Dict[str, Any],
             stances: List[Stance], side: List[str]) -> Dict[str, Any]:
        m = d.get("measurement", {})
        action = (f"Cap the bid multiplier at the baseline {m['bid_base']:.2f} (today {m['bid_today']:.2f}) and "
                  f"restore the pacing schedule for ad {r['ad']} in {r['country']} ({r.get('channel')})")
        return {"fix_id": _new_id("fix"), "playbook": playbook, "issue_key": r["issue_key"],
                "data_side": r.get("data_side", "simulated"), "action": action,
                "scope": {"ad": r.get("ad"), "country": r.get("country"), "channel": r.get("channel")},
                "expected_impact": impact,
                "side_effects": [f"{s} moves with the bid multiplier (back towards baseline)" for s in side],
                "evidence": [v["detail"] for v in d["gates"].values()] + (r.get("observations") or [])[:4],
                "gates": {k: "pass" for k in d["gates"]}, "stances": {s.agent: s.stance for s in stances},
                "discussion_id": d["discussion_id"], "status": "proposed", "sim_now": r.get("sim_now"),
                "observed": r.get("observed")}

    def _brief(self, b: Dict[str, Any], res: PMResult) -> Dict[str, Any]:
        outcomes: Dict[str, int] = {}
        for d in res.discussions:
            outcomes[d["outcome"]] = outcomes.get(d["outcome"], 0) + 1
        actions = sorted(res.fixes, key=lambda f: -(f["expected_impact"] or {}).get("low", 0))
        b["recommended_actions"] = [{"rank": i + 1, "fix_id": f["fix_id"], "playbook": f["playbook"],
                                     "action": f["action"], "expected_impact": f["expected_impact"],
                                     "data_side": f["data_side"]} for i, f in enumerate(actions)]
        b["outcome_counts"] = outcomes
        b["uncertainties"] = sorted(set(res.uncertainties))
        parts = [f"{n} {o.replace('_', ' ')}" for o, n in sorted(outcomes.items())]
        b["summary"] = (f"{len(res.discussions)} issue(s) discussed" + (f": {', '.join(parts)}" if parts else "")
                        + (f"; {len(b['resolved'])} resolved" if b["resolved"] else "")
                        + (f"; {len(res.proposals)} graph proposal(s)" if res.proposals else "")
                        + (f"; {len(b['graph_decisions'])} graph decision(s) received" if b["graph_decisions"] else "")
                        + (f"; {len(b['feedback'])} human fix feedback" if b["feedback"] else "") + ".")
        b["narrative"] = self._narrative(b) if self.llm and res.discussions else None
        return b

    def _narrative(self, b: Dict[str, Any]) -> Optional[str]:
        """Optional executive summary from the large model. Dropped if it quotes a number that
        isn't in the brief (numbers come from the graph, not from the model)."""
        try:
            from llm import LLMUnavailable, claude_json
        except Exception:
            return None
        context = json.dumps({k: v for k, v in b.items() if k != "narrative"}, default=str)
        schema = {"type": "object", "properties": {"narrative": {"type": "string"}},
                  "required": ["narrative"], "additionalProperties": False}
        try:
            text = claude_json("Write a 3-5 sentence executive summary of this monitoring cycle for a marketing lead. "
                               "Lead with what needs a decision. Use only numbers from the data.\n\n" + context,
                               schema, effort="medium", model=LARGE_MODEL)["narrative"]
        except LLMUnavailable:
            return None
        known = {float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", context.replace(",", ""))}
        used = {float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", text.replace(",", ""))}
        return text if used <= known else None
