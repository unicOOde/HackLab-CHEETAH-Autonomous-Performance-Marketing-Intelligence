"""Graph overlooker: walks the Freecash graph on its own and decides which edges make sense.

It can add, remove and reweight edges, change node/edge `ag_*` properties, and confirm, reject or
delete what other agents wrote (Outage, Hypothesis, Signal). Every change goes through three gates:

  1. Reasoning + evidence: the overlooker must state why and give read-only Cypher queries whose
     results support the change.
  2. Reviewer subagent: a separate Claude call that sees only the proposal, the current state of
     the target and the evidence queries *re-run by us* (so numbers can't be invented), and must
     approve it.
  3. GraphStore rules: data-edge totals are read-only (removing a data edge soft-disables it),
     base nodes only take `ag_*`, every write is logged as a (:Change) and can be undone.

Other agents call `GraphOverlooker().run(focus=<their change ids>, trigger=<agent name>)` when
they finish, so the overlooker reviews (and may overrule) what they just wrote.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import anthropic

from graph_store import AGGREGATE_RELS, GraphStore, GraphWriteError, is_read_only
from llm import LLMUnavailable, base_params, claude_json, get_client

AGENT = "graph_overlooker"
MAX_ROWS = 100
MAX_RESULT_CHARS = 15000

OPS = ["add_edge", "remove_edge", "reweight_edge", "set_edge_props", "set_node_props",
       "set_status", "delete_node"]

_nullable = lambda t: {"type": [t, "null"]}

TOOLS = [
    {
        "name": "query_graph",
        "description": ("Run a read-only Cypher query against the graph and get rows back as JSON "
                        f"(max {MAX_ROWS} rows). Return scalars or maps (e.g. properties(n)), not node "
                        "objects. Writes are rejected."),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"cypher": {"type": "string"}},
            "required": ["cypher"],
            "additionalProperties": False,
        },
    },
    {
        "name": "propose_change",
        "description": (
            "Propose one change to the graph. It is applied only if an independent reviewer approves "
            "it after seeing your reasoning and the results of your evidence queries (which are re-run). "
            "Fields per op (others null): "
            "add_edge/remove_edge/reweight_edge/set_edge_props: src_label, src_id, rel, dst_label, dst_id "
            "(+ weight for add/reweight, props_json for set_edge_props); "
            "set_node_props: label, id, props_json (keys must start with ag_; null value removes); "
            "set_status: label (Outage|Hypothesis|Signal), id, status (open|resolved|confirmed|rejected); "
            "delete_node: label (Outage|Hypothesis|Signal), id."),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": OPS},
                "src_label": _nullable("string"), "src_id": _nullable("string"),
                "rel": _nullable("string"),
                "dst_label": _nullable("string"), "dst_id": _nullable("string"),
                "label": _nullable("string"), "id": _nullable("string"),
                "weight": _nullable("number"),
                "props_json": _nullable("string"),
                "status": _nullable("string"),
                "reasoning": {"type": "string"},
                "evidence_queries": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["op", "src_label", "src_id", "rel", "dst_label", "dst_id", "label", "id",
                         "weight", "props_json", "status", "reasoning", "evidence_queries"],
            "additionalProperties": False,
        },
    },
]

SYSTEM = f"""You are the graph overlooker for the Freecash performance-marketing graph (Neo4j).
You decide on your own which edges in the graph make sense and which don't, and you fix the graph.

Graph model (aggregated, no node per click or transaction):
- (Freecash)-[:RUNS {{tx, amount, commission, payout, margin}}]->(Ad)
- (Ad)-[:CONVERTS_IN]->(Country), -[:CLICKED_AT]->(ClickHour 0-23), -[:CONVERTS_AFTER]->(LagBucket
  '<1m','<1h','<1d','<7d','>7d'), -[:HAS_TYPE]->(TransactionType sale|lead), -[:ACTIVE_IN]->(Month):
  all carry the same totals {{tx, amount, commission, payout, margin}}.
- (Ad)-[:IN_VERTICAL]->(Vertical), -[:IN_CATEGORY]->(Category), -[:PAID_IN]->(Currency).
- Paid acquisition (simulated hourly telemetry, see paid_sim.py):
  (Ad)-[:PAID_IN_HOUR {{country, device_os, channel, auctions, impressions, clicks, spend, conversions,
  revenue, bid_mult, tracking, budget, planned_share}}]->(Hour {{id: '2026-10-14T09', day, hour, ts}})
  -[:IN_DAY]->(Day {{id, weekday}}); (PlannedEvent {{type, start, end, expected_lift}})-[:APPLIES_TO]->(Ad|Country).
- Metric graph: (Metric {{id, definition, cypher_value, unit}}) with -[:FORMULA {{formula}}]-> (exact
  decomposition) and -[:INFLUENCES {{prior, mechanism, status}}]-> (possible cause) edges down to 'Revenue'.
  Metrics onboarded by the graph engineer also carry origin='onboarding', source_query (read-only Cypher
  returning sums per slice) and monitoring config (min_sample, maturity_days, baseline, ...). An
  INFLUENCES edge validated from monitoring evidence has ag_status='validated' (+ ag_evidence); read
  the effective status as coalesce(ag_status, status).
- Agent nodes: (Outage)-[:AFFECTS]->(Country), (Hypothesis)-[:ABOUT]->(any node),
  (Signal {{issue_key, metric, severity, state, observations, hypotheses}})-[:ON_METRIC]->(Metric),
  -[:ABOUT]->(Ad), -[:IN_COUNTRY]->(Country): emitted by the pacing watcher.
- Agent properties start with ag_ (e.g. Ad.ag_risk, Country.ag_outage_status).
- (Change)-[:CHANGED]->(...) is the audit log of every agent write. Read it, never change it.

Rules:
- Data edges {sorted(AGGREGATE_RELS)} hold facts computed from transactions. Their totals are
  read-only. You can mark them irrelevant (remove_edge -> sets ag_disabled) or set ag_weight.
- You may add your own relationship types (UPPER_SNAKE_CASE) between existing nodes, e.g.
  SIMILAR_TO between ads with a similar country/hour/lag profile, or SUPPORTS / CONTRADICTS between
  a Hypothesis and an Outage. Weights are in [-1, 1].
- You may confirm, reject, resolve or delete Outage, Hypothesis and Signal nodes written by other agents,
  and change ag_* properties they set, when the data doesn't back them.
- Every proposal needs a clear reasoning and evidence_queries (read-only Cypher) whose results
  support it. An independent reviewer sees those results and decides. If rejected, don't repeat the
  same proposal; adjust it or move on.
- Prefer a few well-founded changes over many weak ones. Doing nothing is fine when the graph is
  consistent. Explore with query_graph before proposing.
When you are done, answer with a short plain-text summary of what you changed and why."""

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "approve": {"type": "boolean"},
        "reason": {"type": "string"},
        "concerns": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["approve", "reason", "concerns"],
    "additionalProperties": False,
}

REVIEW_SYSTEM = """You are an independent, skeptical reviewer of changes to a performance-marketing
graph (Neo4j, aggregated Freecash ad data). Another agent proposes a change with its reasoning.
You get the proposal, the current state of the target, and the evidence query results (re-run by
the system, so they are real). Approve only if the evidence actually supports the change and the
change is useful and proportionate. Reject when the reasoning relies on numbers not in the evidence,
on too few transactions to be meaningful, or when the change would hide real data. Be concise."""


@dataclass
class Decision:
    proposal: Dict[str, Any]
    approved: bool
    review: str
    change_id: Optional[str] = None
    error: Optional[str] = None


@dataclass
class OverlookerReport:
    trigger: str
    decisions: List[Decision] = field(default_factory=list)
    summary: str = ""

    @property
    def applied(self) -> List[Decision]:
        return [d for d in self.decisions if d.change_id]


def _dump(rows: Any) -> str:
    text = json.dumps(rows, default=str)
    return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + "... [truncated]"


class GraphOverlooker:
    def __init__(self, store: Optional[GraphStore] = None, max_steps: int = 30, max_changes: int = 10,
                 effort: str = "high"):
        self.store = store or GraphStore(agent=AGENT)
        self.max_steps = max_steps
        self.max_changes = max_changes
        self.effort = effort

    # ------------------------------------------------------------------ tools
    def _query(self, cypher: str) -> str:
        if not is_read_only(cypher):
            return "ERROR: only read-only Cypher is allowed in query_graph."
        try:
            rows = self.store.read(cypher)
        except Exception as e:
            return f"ERROR: {e}"
        return _dump(rows[:MAX_ROWS]) + (f"\n({len(rows)} rows, showing {MAX_ROWS})" if len(rows) > MAX_ROWS else "")

    def _target_state(self, p: Dict[str, Any]) -> Any:
        try:
            if p["op"] in ("set_node_props", "set_status", "delete_node"):
                return {"node": self.store.get(p["label"], p["id"])}
            src, dst = (p["src_label"], p["src_id"]), (p["dst_label"], p["dst_id"])
            return {"edge": self.store.get_edge(src, p["rel"], dst),
                    "src": self.store.get(*src), "dst": self.store.get(*dst)}
        except Exception as e:
            return {"error": str(e)}

    def _review(self, p: Dict[str, Any], evidence: List[Dict[str, str]]) -> Dict[str, Any]:
        prompt = (f"Proposed change:\n{json.dumps({k: v for k, v in p.items() if v is not None}, indent=1)}\n\n"
                  f"Current state of the target:\n{_dump(self._target_state(p))}\n\n"
                  "Evidence query results:\n" +
                  "\n".join(f"- {e['query']}\n  -> {e['result']}" for e in evidence))
        return claude_json(prompt, REVIEW_SCHEMA, system=REVIEW_SYSTEM, effort="high")

    def _apply(self, p: Dict[str, Any]) -> str:
        s, reason = self.store, p["reasoning"][:500]
        src, dst = (p["src_label"], p["src_id"]), (p["dst_label"], p["dst_id"])
        props = json.loads(p["props_json"]) if p.get("props_json") else {}
        if not isinstance(props, dict):
            raise GraphWriteError("props_json must be a JSON object")
        op = p["op"]
        if op == "add_edge":
            return s.add_edge(src, p["rel"], dst, weight=p.get("weight"), reason=reason)
        if op == "remove_edge":
            return s.remove_edge(src, p["rel"], dst, reason=reason)
        if op == "reweight_edge":
            if p.get("weight") is None:
                raise GraphWriteError("reweight_edge needs weight")
            return s.reweight_edge(src, p["rel"], dst, p["weight"], reason=reason)
        if op == "set_edge_props":
            return s.set_edge_props(src, p["rel"], dst, props, reason=reason)
        if op == "set_node_props":
            return s.set_props(p["label"], p["id"], props, reason=reason)
        if op == "set_status":
            return s.set_status(p["label"], p["id"], p["status"], reason=reason)
        if op == "delete_node":
            return s.delete_node(p["label"], p["id"], reason=reason)
        raise GraphWriteError(f"unknown op {op}")

    def _propose(self, p: Dict[str, Any], report: OverlookerReport) -> str:
        if len(report.applied) >= self.max_changes:
            return f"REJECTED: change limit for this run ({self.max_changes}) reached. Finish now."
        queries = [q for q in p.get("evidence_queries") or [] if q.strip()]
        if not queries:
            d = Decision(p, False, "no evidence queries")
            report.decisions.append(d)
            return "REJECTED: every proposal needs evidence_queries."
        evidence = [{"query": q, "result": self._query(q)} for q in queries[:5]]
        try:
            review = self._review(p, evidence)
        except LLMUnavailable as e:
            report.decisions.append(Decision(p, False, f"reviewer unavailable: {e}"))
            return f"NOT APPLIED: reviewer unavailable ({e})."
        d = Decision(p, bool(review.get("approve")), review.get("reason", ""))
        report.decisions.append(d)
        if not d.approved:
            concerns = "; ".join(review.get("concerns") or [])
            return f"REJECTED by reviewer: {d.review}" + (f" Concerns: {concerns}" if concerns else "")
        try:
            d.change_id = self._apply(p) or None
        except (GraphWriteError, ValueError, KeyError, TypeError) as e:
            d.error = str(e)
            return f"APPROVED but not applied, rule violation: {e}"
        except Exception as e:
            d.error = str(e)
            return f"APPROVED but write failed: {e}"
        return (f"APPLIED (change {d.change_id}). Reviewer: {d.review}" if d.change_id
                else "APPROVED but nothing changed (target already in that state).")

    # ------------------------------------------------------------------ context
    def _kickoff(self, focus: List[str], trigger: str) -> str:
        overview = self.store.read(
            "MATCH (n) WHERE NOT n:Change RETURN labels(n)[0] AS label, count(*) AS n ORDER BY n DESC")
        rels = self.store.read(
            "MATCH ()-[r]->() WHERE type(r) <> 'CHANGED' RETURN type(r) AS rel, count(*) AS n ORDER BY n DESC")
        parts = [f"Triggered by: {trigger}.", f"Nodes: {_dump(overview)}", f"Relationships: {_dump(rels)}"]
        if focus:
            changes = self.store.read(
                "MATCH (c:Change) WHERE c.id IN $ids RETURN c.agent AS agent, c.op AS op, "
                "c.target_label AS target_label, c.target_id AS target_id, c.after AS after, c.reason AS reason",
                ids=focus)
            parts.append("The triggering agent just made these changes. Check them first: are they backed "
                         f"by the data, and what else in the graph should change because of them?\n{_dump(changes)}")
        else:
            parts.append("No specific changes to review: do a general sweep of the graph.")
        return "\n\n".join(parts)

    # ------------------------------------------------------------------ loop
    def run(self, focus: Optional[List[str]] = None, trigger: str = "manual") -> OverlookerReport:
        report = OverlookerReport(trigger)
        client = get_client()
        params = base_params(self.effort)
        messages: List[Dict[str, Any]] = [{"role": "user", "content": self._kickoff(focus or [], trigger)}]

        for _ in range(self.max_steps):
            try:
                resp = client.beta.messages.create(system=SYSTEM, tools=TOOLS, messages=messages, **params)
            except anthropic.APIError as e:
                raise LLMUnavailable(f"overlooker API call failed: {e}") from e
            messages.append({"role": "assistant", "content": resp.content})  # keep thinking blocks
            if resp.stop_reason == "refusal":
                report.summary = "Stopped: model declined."
                break
            if resp.stop_reason != "tool_use":
                report.summary = "".join(b.text for b in resp.content if b.type == "text").strip()
                break
            results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                if block.name == "query_graph":
                    out = self._query(block.input.get("cypher", ""))
                elif block.name == "propose_change":
                    out = self._propose(dict(block.input), report)
                else:
                    out = f"ERROR: unknown tool {block.name}"
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": out,
                                **({"is_error": True} if out.startswith("ERROR") else {})})
            messages.append({"role": "user", "content": results})
        else:
            report.summary = f"Stopped after {self.max_steps} steps."
        return report


def run_after(agent: str, store: GraphStore) -> Optional[OverlookerReport]:
    """Hook for other agents: start the overlooker on what `store` just wrote."""
    try:
        return GraphOverlooker().run(focus=list(store.changes), trigger=agent)
    except LLMUnavailable as e:
        print(f"[overlooker] not started: {e}")
        return None


def format_report(r: OverlookerReport) -> str:
    lines = [f"Overlooker ({r.trigger}): {len(r.applied)} applied, "
             f"{sum(not d.approved for d in r.decisions)} rejected"]
    for d in r.decisions:
        p = d.proposal
        target = (f"{p.get('label')} {p.get('id')}" if p.get("label")
                  else f"({p.get('src_label')} {p.get('src_id')})-[:{p.get('rel')}]->({p.get('dst_label')} {p.get('dst_id')})")
        state = "APPLIED" if d.change_id else ("ERROR " + d.error if d.error else
                                               "approved/no-op" if d.approved else "REJECTED")
        lines.append(f"  {state}: {p['op']} {target} | {d.review[:160]}")
    if r.summary:
        lines.append(f"  Summary: {r.summary}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(format_report(GraphOverlooker().run(trigger="manual")))
