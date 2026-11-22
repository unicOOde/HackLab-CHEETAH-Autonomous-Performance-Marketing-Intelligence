"""Graph engineer ("Buffalo"): the only agent that changes graph structure.

Spec: agents/graph_engineer.md (onboarding, re-link, fix stance), agents/orchestration_plan.md §5.3.

Inputs per run:
  * `proposal` from the product manager:
      - graph_change / validate_influence: promote an INFLUENCES edge to validated
        (written as ag_status, since metric edges are build-owned and only take ag_* properties);
      - metric: a product_manager.md §4 record -> onboarding;
  * `metric_submission` from a contributor (UI/API): same onboarding, nobody specifies placement.

Every proposal: hear out the other agents (stances.py), check deterministically, then
  - onboarding: add_metric + FORMULA edges are decided by deterministic checks (the query must
    evaluate, ratios must lie in [0,1], FORMULA residual <= 1e-6 globally and in every slice);
    INFLUENCES edges need an LLM mechanism and the independent reviewer;
  - validate_influence: the independent reviewer must approve, and no agent may object with
    evidence. Reviewer unavailable -> deferred (unknown never acts).
Runs without proposals do the re-link pass for onboarded metrics (graph_engineer.md §6).
"""
from __future__ import annotations

import ast
import json
import math
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from graph_store import METRIC_UNITS, GraphStore, GraphWriteError, is_read_only
from agents.stances import ABSTAIN, OBJECT, SUPPORT, Stance, investigator_stance, outage_stance

AGENT = "graph_engineer"
PAID_COLUMNS = ("auctions", "impressions", "clicks", "spend", "conversions", "revenue")
PAID_SOURCE_Q = ("MATCH (a:Ad)-[e:PAID_IN_HOUR]->(:Hour) RETURN a.id AS ad, e.country AS country, "
                 + ", ".join(f"sum(e.{c}) AS {c}" for c in PAID_COLUMNS))
MIN_CORR_SLICES, MIN_ABS_CORR, MAX_INFLUENCE_CANDIDATES = 8, 0.5, 3
_OPS = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Name, ast.Constant, ast.Load,
        ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{datetime.now(timezone.utc):%Y%m%d%H%M%S}_{uuid.uuid4().hex[:6]}"


# --------------------------------------------------------------------------- expressions
def parse_expr(expr: str) -> ast.Expression:
    """cypher_value over summed columns: names, numbers, + - * /, parentheses. Nothing else."""
    tree = ast.parse((expr or "").strip(), mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _OPS) or (isinstance(node, ast.Constant) and not isinstance(node.value, (int, float))):
            raise ValueError(f"unsupported expression element {type(node).__name__} in {expr!r}")
    return tree


def expr_names(tree: ast.Expression) -> List[str]:
    return sorted({n.id for n in ast.walk(tree) if isinstance(n, ast.Name)})


def evaluate(tree: ast.Expression, values: Dict[str, Any]) -> Optional[float]:
    def ev(n: ast.AST) -> Optional[float]:
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant):
            return float(n.value)
        if isinstance(n, ast.Name):
            v = values.get(n.id)
            return float(v) if isinstance(v, (int, float)) else None
        if isinstance(n, ast.UnaryOp):
            v = ev(n.operand)
            return None if v is None else -v
        a, b = ev(n.left), ev(n.right)
        if a is None or b is None:
            return None
        if isinstance(n.op, ast.Div):
            return None if b == 0 else a / b
        return a + b if isinstance(n.op, ast.Add) else a - b if isinstance(n.op, ast.Sub) else a * b
    return ev(tree)


def source_of(cypher_value: str, source_query: Optional[str]) -> Optional[str]:
    """The query a metric is evaluated on: its own source_query, or the paid telemetry when it is
    an expression over PAID_IN_HOUR columns. None = not evaluable here."""
    if source_query:
        return source_query
    try:
        names = expr_names(parse_expr(cypher_value))
    except (SyntaxError, ValueError):
        return None
    return PAID_SOURCE_Q if names and set(names) <= set(PAID_COLUMNS) else None


class Evaluation:
    """One metric evaluated globally (sums over all rows) and per slice (each returned row)."""

    def __init__(self, store: GraphStore, cypher_value: str, source_query: str,
                 cache: Optional[Dict[str, List[Dict[str, Any]]]] = None):
        self.tree = parse_expr(cypher_value)
        if cache is None or source_query not in cache:
            params = {p: None for p in re.findall(r"\$([A-Za-z_]\w*)", source_query)}   # optional slice filters
            rows = store.read(source_query, **params)
            if cache is not None:
                cache[source_query] = rows
        self.rows = rows if cache is None else cache[source_query]
        if not self.rows:
            raise ValueError("source_query returned no rows")
        cols = expr_names(self.tree)
        missing = [c for c in cols if c not in self.rows[0]]
        if missing:
            raise ValueError(f"source_query doesn't return {missing}")
        self.sums = {c: sum(r[c] or 0 for r in self.rows) for c in cols}
        self.value = evaluate(self.tree, self.sums)
        self.by_slice = {self._key(r): evaluate(self.tree, r) for r in self.rows}

    @staticmethod
    def _key(row: Dict[str, Any]) -> Tuple:
        return tuple((k, v) for k, v in sorted(row.items()) if isinstance(v, str))


def _pearson(xs: List[float], ys: List[float]) -> Optional[float]:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return None if sx == 0 or sy == 0 else sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


# --------------------------------------------------------------------------- LLM pieces
MECHANISM_SCHEMA = {
    "type": "object",
    "properties": {"links": {"type": "array", "items": {
        "type": "object",
        "properties": {"other": {"type": "string"}, "direction": {"type": "string", "enum": ["new_causes_other",
                                                                                            "other_causes_new", "none"]},
                       "mechanism": {"type": "string"}},
        "required": ["other", "direction", "mechanism"], "additionalProperties": False}}},
    "required": ["links"], "additionalProperties": False,
}


def _llm():
    try:
        from llm import LLMUnavailable, claude_json
        from agents.graph_overlooker import REVIEW_SCHEMA, REVIEW_SYSTEM
        return LLMUnavailable, claude_json, REVIEW_SCHEMA, REVIEW_SYSTEM
    except Exception:
        return None


def review(proposal: Dict[str, Any], evidence: List[str], stances: List[Stance]) -> Dict[str, Any]:
    """Independent reviewer (as in graph_overlooker.py): sees the proposal, the re-run evidence and
    every stance. Returns {approve, reason, concerns} or {'unknown': reason}."""
    tools = _llm()
    if tools is None:
        return {"unknown": "anthropic SDK / overlooker not importable"}
    LLMUnavailable, claude_json, schema, system = tools
    prompt = (f"Proposed change:\n{json.dumps(proposal, indent=1, default=str)}\n\n"
              "Evidence (re-read from the graph by the system, so the numbers are real):\n"
              + "\n".join(f"- {e}" for e in evidence)
              + "\n\nStances of the other agents:\n"
              + "\n".join(f"- {s.agent}: {s.stance}: {s.argument}" for s in stances))
    try:
        return claude_json(prompt, schema, system=system, effort="high", model="large")
    except LLMUnavailable as exc:
        return {"unknown": f"LLM unavailable: {str(exc)[:160]}"}


# --------------------------------------------------------------------------- the agent
class GraphEngineer:
    def __init__(self, store: Optional[GraphStore] = None, llm: bool = True, llm_off_reason: str = "LLM disabled"):
        self.store = store or GraphStore(agent=AGENT)
        self.llm = llm
        self.llm_off_reason = llm_off_reason
        self._rows: Dict[str, List[Dict[str, Any]]] = {}   # source query -> rows, per run

    def _eval(self, cypher_value: str, source_query: str) -> Evaluation:
        return Evaluation(self.store, cypher_value, source_query, self._rows)

    def _metrics(self) -> List[Dict[str, Any]]:
        return self.store.read("MATCH (m:Metric) RETURN m.id AS id, m.cypher_value AS cypher_value, "
                               "m.source_query AS source_query, m.unit AS unit, m.definition AS definition, "
                               "m.origin AS origin")

    def _decision(self, proposal: Dict[str, Any], kind: str, target: str) -> Dict[str, Any]:
        return {"decision_id": _new_id("dec"), "proposal_id": proposal.get("proposal_id"), "kind": kind,
                "target": target, "edge_key": proposal.get("edge_key"), "issue_key": proposal.get("issue_key"),
                "outcome": "rejected", "reason": "", "checks": [], "stances": [], "review": None,
                "change_ids": [], "links": [], "uncertainties": []}

    # ------------------------------------------------------------------ validate an INFLUENCES edge
    def validate_influence(self, p: Dict[str, Any]) -> Dict[str, Any]:
        a, b = p.get("src"), p.get("dst")
        d = self._decision(p, "graph_change", f"{a} -INFLUENCES-> {b}")
        edge = self.store.get_edge(("Metric", a), "INFLUENCES", ("Metric", b)) if a and b else None
        if edge is None:
            d["reason"] = "edge does not exist (no placeholders: nothing to validate)"
            return d
        status = edge.get("ag_status") or edge.get("status")
        if status == "validated":
            d.update(outcome="applied", reason="already validated; nothing to change")
            return d
        # re-read the evidence from the Signal node instead of trusting the proposal's text
        sig = self.store.get("Signal", p["signal_id"]) if p.get("signal_id") else None
        lines = (list((sig or {}).get("observations") or []) + list((sig or {}).get("downstream") or []))
        head = f"{(sig or {}).get('metric')} {(sig or {}).get('observed')} vs expected {(sig or {}).get('expected')}"
        verified = [e for e in p.get("evidence") or [] if e in lines or (sig and e.startswith(head))]
        d["checks"].append({"check": "evidence re-read from Signal " + str(p.get("signal_id")),
                            "pass": bool(verified) and len(verified) == len(p.get("evidence") or []),
                            "detail": f"{len(verified)}/{len(p.get('evidence') or [])} evidence lines found in the graph"})
        if sig is None:
            pacing = Stance("pacing_watcher", OBJECT, "the Signal this evidence comes from no longer exists",
                            [str(p.get("signal_id"))])
        elif sig.get("status") == "rejected":
            pacing = Stance("pacing_watcher", OBJECT, "the source Signal was rejected", [sig["id"]])
        else:
            pacing = Stance("pacing_watcher", SUPPORT, f"own detection ({sig.get('status')}) measured both ends", verified)
        stances = [pacing, outage_stance(self.store, [p.get("country")]),
                   investigator_stance(p, {"signal": {k: (sig or {}).get(k) for k in ("issue_key", "observations",
                                                                                     "downstream", "hypotheses")}})
                   if self.llm else Stance("investigator", ABSTAIN, f"unknown: {self.llm_off_reason}", unknown=True)]
        d["stances"] = [s.to_dict() for s in stances]
        if not d["checks"][0]["pass"]:
            d["reason"] = "evidence could not be re-read from the graph"
            return d
        objections = [s for s in stances if s.stance == OBJECT]
        if objections:
            d.update(outcome="deferred", reason="; ".join(f"{s.agent} objects: {s.argument}" for s in objections))
            return d
        rv = review({"op": "set ag_status=validated", "edge": d["target"], "current_status": status,
                     "prior": edge.get("prior"), "mechanism": edge.get("mechanism"),
                     "rationale": p.get("rationale")}, verified, stances) if self.llm else {"unknown": self.llm_off_reason}
        d["review"] = rv
        if "unknown" in rv:
            d.update(outcome="deferred", reason=f"independent reviewer unavailable ({rv['unknown']}): unknown never acts")
            return d
        if not rv.get("approve"):
            d.update(outcome="rejected", reason=f"reviewer: {rv.get('reason')}")
            return d
        chg = self.store.set_edge_props(("Metric", a), "INFLUENCES", ("Metric", b),
                                        {"ag_status": "validated", "ag_evidence": verified,
                                         "ag_validated_from": p.get("issue_key") or ""},
                                        reason=p.get("rationale") or "validated from monitoring evidence")
        d.update(outcome="applied", reason=f"reviewer approved: {rv.get('reason')}", change_ids=[c for c in [chg] if c])
        return d

    # ------------------------------------------------------------------ onboarding
    def onboard(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        mid = str(rec.get("id") or "").strip()
        d = self._decision(rec, "metric", mid)
        unit, cv, sq = rec.get("unit"), str(rec.get("cypher_value") or ""), rec.get("source_query") or None

        def fail(check: str, detail: str) -> Dict[str, Any]:
            d["checks"].append({"check": check, "pass": False, "detail": detail})
            d["reason"] = f"{check}: {detail}"
            return d

        existing = {m["id"]: m for m in self._metrics()}
        if not mid or mid in existing:
            return fail("unique id", f"Metric {mid!r} already exists" if mid else "id is required")
        if unit not in METRIC_UNITS:
            return fail("unit", f"{unit!r} not in {sorted(METRIC_UNITS)}")
        if sq and not is_read_only(sq):
            return fail("read-only source_query", "source_query must be read-only Cypher")
        src = source_of(cv, sq)
        if src is None:
            return fail("evaluable", f"{cv!r} needs a source_query, or must be an expression over "
                                     f"PAID_IN_HOUR columns {list(PAID_COLUMNS)}")
        try:
            ev = self._eval(cv, src)
        except Exception as exc:   # bad Cypher, missing columns, unsupported expression
            return fail("evaluates", f"{type(exc).__name__}: {str(exc)[:240]}")
        vals = [v for v in ev.by_slice.values() if v is not None]
        if ev.value is None:
            return fail("evaluates", "global value is undefined (zero denominator)")
        d["checks"].append({"check": "evaluates", "pass": True,
                            "detail": f"global {ev.value:.4g} over {len(ev.rows)} slices ({len(vals)} defined)"})
        if unit == "ratio" and not all(0 <= v <= 1 for v in vals + [ev.value]):
            return fail("ratio in [0,1]", f"values range {min(vals + [ev.value]):.4g}..{max(vals + [ev.value]):.4g}")
        config = {k: v for k, v in (rec.get("config") or {}).items()
                  if k in ("min_sample", "min_sample_unit", "maturity_days", "baseline", "companions", "slice_by")}
        try:
            chg = self.store.add_metric(mid, str(rec.get("definition") or ""), unit, cv, sq,
                                        str(rec.get("domain") or ("paid_acquisition" if src == PAID_SOURCE_Q else "")),
                                        config, reason=f"onboarding: {rec.get('definition') or mid}")
        except GraphWriteError as exc:
            return fail("add_metric", str(exc))
        d.update(outcome="applied", reason=f"onboarded: {cv} = {ev.value:.4g} globally", change_ids=[chg])
        self.link(mid, cv, sq, ev, existing, d)
        if not d["links"]:
            d["uncertainties"].append(f"{mid} isn't connected to the metric graph yet (no path to Revenue); "
                                      "the re-link pass retries on every graph engineer run")
        return d

    def link(self, mid: str, cv: str, sq: Optional[str], ev: Evaluation, existing: Dict[str, Dict[str, Any]],
             d: Dict[str, Any], rejected: Optional[set] = None) -> None:
        """Discover FORMULA (exact, deterministic) and INFLUENCES (correlation + LLM + reviewer) links."""
        same = {k: m for k, m in existing.items() if k != mid and (m.get("source_query") or None) == sq
                and source_of(m.get("cypher_value") or "", m.get("source_query")) is not None}
        linked = {(e["src"], e["dst"]) for e in self.store.read(
            "MATCH (a:Metric)-[:FORMULA|INFLUENCES]-(b:Metric) WHERE a.id = $m RETURN a.id AS src, b.id AS dst", m=mid)}
        linked |= {(b, a) for a, b in linked}
        # FORMULA: M = A / B  ->  A = M x B ;  M = A * B  ->  M = A x B
        body = ev.tree.body
        if isinstance(body, ast.BinOp) and isinstance(body.op, (ast.Div, ast.Mult)) and \
                isinstance(body.left, ast.Name) and isinstance(body.right, ast.Name):
            by_expr = {(m.get("cypher_value") or "").strip(): k for k, m in same.items()}
            A, B = by_expr.get(body.left.id), by_expr.get(body.right.id)
            if A and B:
                div = isinstance(body.op, ast.Div)
                lhs, rhs = (A, (mid, B)) if div else (mid, (A, B))
                residual = self._residual(ev, existing, lhs, rhs, sq)
                ins = [rhs[0], rhs[1]]
                formula = f"{lhs} = {rhs[0]} x {rhs[1]}"
                for src in ins:
                    if (src, lhs) in linked:
                        continue
                    try:
                        chg = self.store.link_metrics(src, "FORMULA", lhs, {"formula": formula, "residual": residual},
                                                      [f"max residual {residual:.2e} globally and over {len(ev.rows)} slices"],
                                                      reason=f"exact decomposition: {formula}")
                        d["links"].append({"rel": "FORMULA", "src": src, "dst": lhs, "formula": formula,
                                           "residual": residual, "change_id": chg})
                        d["change_ids"].append(chg)
                    except GraphWriteError as exc:
                        d["uncertainties"].append(f"FORMULA {src} -> {lhs} not written: {exc}")
        # INFLUENCES candidates: cross-sectional correlation over the shared slices
        cands = []
        for k, m in same.items():
            if (mid, k) in linked or any(l["src"] == k or l["dst"] == k for l in d["links"]) or \
                    (rejected and f"{mid}|{k}" in rejected):
                continue
            try:
                other = self._eval(m["cypher_value"], source_of(m["cypher_value"], m.get("source_query")))
            except Exception:
                continue
            pairs = [(v, other.by_slice.get(s)) for s, v in ev.by_slice.items()]
            pairs = [(x, y) for x, y in pairs if x is not None and y is not None]
            if len(pairs) >= MIN_CORR_SLICES:
                r = _pearson([x for x, _ in pairs], [y for _, y in pairs])
                if r is not None and abs(r) >= MIN_ABS_CORR:
                    cands.append({"other": k, "r": round(r, 3), "n": len(pairs), "definition": m.get("definition")})
        cands = sorted(cands, key=lambda c: -abs(c["r"]))[:MAX_INFLUENCE_CANDIDATES]
        if cands:
            self._influences(mid, cands, d)

    def _residual(self, ev: Evaluation, existing: Dict[str, Dict[str, Any]], lhs: str,
                  rhs: Tuple[str, str], sq: Optional[str]) -> float:
        evs = {}
        for k in {lhs, *rhs}:
            m = existing.get(k)
            evs[k] = ev if m is None else self._eval(m["cypher_value"], source_of(m["cypher_value"], sq))
        worst = 0.0
        for s in list(ev.by_slice) + [None]:
            get = (lambda k: evs[k].value) if s is None else (lambda k: evs[k].by_slice.get(s))   # noqa: E731
            l, r1, r2 = get(lhs), get(rhs[0]), get(rhs[1])
            if l is None or r1 is None or r2 is None:
                continue
            worst = max(worst, abs(l - r1 * r2) / max(abs(l), 1e-9))
        return worst

    def _influences(self, mid: str, cands: List[Dict[str, Any]], d: Dict[str, Any]) -> None:
        names = ", ".join(f"{c['other']} (r={c['r']}, n={c['n']})" for c in cands)
        tools = _llm() if self.llm else None
        if tools is None:
            d["uncertainties"].append(f"INFLUENCES candidates for {mid} not written ({self.llm_off_reason}; "
                                      f"unknown never acts): {names}")
            return
        LLMUnavailable, claude_json, _, _ = tools
        try:
            ans = claude_json(f"New metric {mid!r}. Correlated metrics (cross-sectional over ad x country slices): "
                              f"{json.dumps(cands)}. For each, say whether a causal mechanism plausibly links them "
                              "and in which direction; 'none' when the correlation is only shared volume or "
                              "definition overlap.", MECHANISM_SCHEMA, effort="high", model="large")
        except LLMUnavailable as exc:
            d["uncertainties"].append(f"INFLUENCES candidates for {mid} not written ({exc}): {names}")
            return
        by_name = {c["other"]: c for c in cands}
        for link in ans.get("links", []):
            c = by_name.get(link.get("other"))
            if c is None:
                continue
            if link.get("direction") == "none":   # recorded, so the re-link pass doesn't ask again
                d["links"].append({"rel": "INFLUENCES", "src": mid, "dst": c["other"], "r": c["r"],
                                   "rejected": True, "reason": f"no causal mechanism: {link.get('mechanism', '')[:200]}"})
                continue
            src, dst = (mid, c["other"]) if link["direction"] == "new_causes_other" else (c["other"], mid)
            evidence = [f"Pearson r={c['r']} between {mid} and {c['other']} over {c['n']} ad x country slices "
                        "(computed by the graph engineer from the metrics' own definitions)"]
            rv = review({"op": "link_metrics INFLUENCES", "src": src, "dst": dst, "mechanism": link["mechanism"],
                         "prior": abs(c["r"])}, evidence, [])
            d["links"].append({"rel": "INFLUENCES", "src": src, "dst": dst, "r": c["r"], "review": rv})
            if rv.get("approve"):
                try:
                    chg = self.store.link_metrics(src, "INFLUENCES", dst, {"prior": round(abs(c["r"]), 2),
                                                                         "mechanism": link["mechanism"][:400]},
                                                  evidence, reason=link["mechanism"][:200])
                    d["links"][-1]["change_id"] = chg
                    d["change_ids"].append(chg)
                except GraphWriteError as exc:
                    d["uncertainties"].append(f"INFLUENCES {src} -> {dst} not written: {exc}")
            else:
                d["links"][-1]["rejected"] = True

    # ------------------------------------------------------------------ re-link pass
    def relink(self, rejected: set) -> List[Dict[str, Any]]:
        """Onboarding steps 3-4 again for every onboarded metric; existing edges are never duplicated."""
        out = []
        metrics = self._metrics()
        existing = {m["id"]: m for m in metrics}
        for m in metrics:
            if m.get("origin") != "onboarding":
                continue
            src = source_of(m["cypher_value"] or "", m.get("source_query"))
            if src is None:
                continue
            d = self._decision({"proposal_id": None}, "relink", m["id"])
            try:
                ev = self._eval(m["cypher_value"], src)
            except Exception as exc:
                d.update(outcome="deferred", reason=f"metric no longer evaluates: {exc}")
                out.append(d)
                continue
            self.link(m["id"], m["cypher_value"], m.get("source_query") or None, ev, existing, d, rejected)
            for l in d["links"]:
                if l.get("rejected"):
                    rejected.add(f"{m['id']}|{l['dst'] if l['src'] == m['id'] else l['src']}")
            if d["links"]:
                d.update(outcome="applied" if d["change_ids"] else "rejected",
                         reason=f"{len(d['change_ids'])} new link(s)")
                out.append(d)
        return out
