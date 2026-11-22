"""Product manager (Compass) fix discussions and graph engineer (Cartographer) decisions, against an
in-memory fake graph. No Neo4j, no LLM: these tests show the gates decide, not a model."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest.importorskip("anthropic")   # llm.py imports it at module level

from agents.graph_engineer_agent import GraphEngineer, parse_expr
from agents.product_manager_agent import ProductManager

DAYS = [f"2026-10-0{d}" for d in range(1, 9)]   # 7 baseline days + today (Oct 8)


class FakeStore:
    def __init__(self, edges_validated=False, planned=False, outage=False):
        st = "validated" if edges_validated else "hypothesis"
        self.edges = [
            {"src": "Budget & Pacing Controller", "rel": "INFLUENCES", "dst": "Bid Multiplier", "status": st, "prior": 0.9},
            {"src": "Bid Multiplier", "rel": "FORMULA", "dst": "Revenue", "status": "validated", "prior": None},
            {"src": "Bid Multiplier", "rel": "INFLUENCES", "dst": "CPC", "status": "hypothesis", "prior": 0.6},
        ]
        self.planned, self.outage = planned, outage
        self.metrics = {"Revenue": {"cypher_value": "revenue"}, "Total Ad Spend": {"cypher_value": "spend"},
                        "CPC": {"cypher_value": "spend / clicks", "unit": "EUR"}}
        self.signal = None
        self.writes = []

    # -- reads
    def read(self, q, **p):
        if "FORMULA|INFLUENCES]->(b:Metric)" in q:
            return self.edges
        if "PlannedEvent" in q:
            return [{"id": "promo", "name": "Promo", "lift": 1.7}] if self.planned else []
        if "Outage" in q:
            return [{"id": "o1", "service": "PayPal", "conf": 0.9, "countries": ["DE"]}] if self.outage else []
        if "$ad" in q and "PAID_IN_HOUR" in q:   # one ad x country slice by hour
            rows = []
            for i, day in enumerate(DAYS):
                today = day == DAYS[-1]
                for h in range(12 if today else 24):
                    spend = 680 / 12 if today else 680 / 24
                    rows.append({"day": day, "hour": h, "spend": spend, "clicks": 50, "conversions": 2,
                                 "revenue": spend * (0.5 if today else 0.8 + 0.02 * i), "budget": 680.0,
                                 "bm_x": (1.8 if today else 0.9) * 1000, "auctions": 1000})
            return rows
        if "MATCH (m:Metric) RETURN" in q:
            return [{"id": k, "source_query": None, "origin": None, "definition": "", **v} for k, v in self.metrics.items()]
        if "sum(e.auctions) AS auctions" in q:   # paid source per ad x country
            return [{"ad": f"A{i}", "country": "DE", "auctions": 1000, "impressions": 500, "clicks": 10 + i,
                     "spend": 5.0 + i, "conversions": 1, "revenue": 10.0 + 3 * i} for i in range(10)]
        if "-[:FORMULA|INFLUENCES]-(b:Metric)" in q:
            return []
        raise AssertionError(f"unexpected query {q[:80]}")

    def get(self, label, node_id):
        if label == "Signal":
            return self.signal
        return self.metrics.get(node_id) if label == "Metric" else None

    def get_edge(self, src, rel, dst):
        e = next((e for e in self.edges if (e["src"], e["rel"], e["dst"]) == (src[1], rel, dst[1])), None)
        return {"status": e["status"], "prior": e["prior"]} if e else None

    # -- writes (recorded)
    def add_metric(self, mid, *a, **k):
        self.writes.append(("add_metric", mid))
        return "chg_m"

    def link_metrics(self, src, rel, dst, props, evidence, reason=""):
        self.writes.append(("link", src, rel, dst, props.get("residual")))
        return f"chg_{src}"

    def set_edge_props(self, *a, **k):
        self.writes.append(("set_edge_props",) + a)
        return "chg_e"


def p1_report(**over):
    r = {"source": "pacing_watcher", "data_side": "simulated", "change": "new", "signal_id": "sig1",
         "issue_key": "Budget & Pacing Controller|ad=A1|country=DE", "kind": "pacing", "state": "exhausted",
         "metric": "Budget & Pacing Controller", "severity": "major", "ad": "A1", "country": "DE",
         "channel": "google_ads", "observed": 0.99, "expected": 0.3, "pct_change": 2.3, "z": 9.0,
         "window": "2026-10-08 00:00-11:59", "sim_now": "2026-10-08T12:00:00Z",
         "observations": ["Bid Multiplier 1.8 vs expected 0.9 (+100%, z=+6.0)"],
         "hypotheses": ["0.80 | Pacing controller is not respecting its schedule"],
         "downstream": ["Budget gone from 11:00: ~€300 revenue normally earned after 12:00 is at risk"],
         "path_to_revenue": ["Budget & Pacing Controller", "Bid Multiplier", "Revenue"]}
    return {**r, **over}


def msg(payload, type="report", mid="m1", sender="pacing_watcher"):
    return {"type": type, "payload": payload, "msg_id": mid, "from": sender}


def discuss(store, *messages, state=None):
    state = {} if state is None else state
    return ProductManager(store=store, llm=False).run(list(messages), state, "2026-10-08T12:00"), state


# --------------------------------------------------------------------------- Compass
def test_p1_on_unvalidated_path_becomes_graph_only_with_edge_proposal():
    res, state = discuss(FakeStore(), msg(p1_report()))
    d = res.discussions[0]
    assert d["outcome"] == "graph_only" and not res.fixes
    assert [n for n, g in d["gates"].items() if not g["pass"]] == ["experts_agree"]
    assert [(p["src"], p["dst"]) for p in res.proposals] == [("Budget & Pacing Controller", "Bid Multiplier")]
    assert state["issues"][p1_report()["issue_key"]]["waiting_on"] == [res.proposals[0]["proposal_id"]]


def test_p1_with_validated_path_is_proposed_with_positive_lower_bound():
    res, _ = discuss(FakeStore(edges_validated=True), msg(p1_report()))
    d, fix = res.discussions[0], res.fixes[0]
    assert d["outcome"] == "fix_proposed" and all(g["pass"] for g in d["gates"].values())
    # baseline ROAS 0.80..0.92 on today's €680 spend that returned €340
    assert fix["expected_impact"]["low"] == pytest.approx(0.80 * 680 - 340, abs=0.01)
    assert fix["side_effects"] and "CPC" in fix["side_effects"][0]
    assert res.brief["recommended_actions"][0]["fix_id"] == fix["fix_id"]


def test_decision_applied_triggers_rediscussion():
    store = FakeStore()
    res, state = discuss(store, msg(p1_report()))
    pid = res.proposals[0]["proposal_id"]
    store.edges[0]["status"] = "validated"
    decision = {"proposal_id": pid, "outcome": "applied", "edge_key": res.proposals[0]["edge_key"], "target": "x"}
    res2, _ = discuss(store, msg(decision, type="decision", mid="m2", sender="graph_engineer"), state=state)
    assert res2.discussions[0]["outcome"] == "fix_proposed"


def test_rejected_fix_is_not_reproposed_without_new_evidence():
    store = FakeStore(edges_validated=True)
    res, state = discuss(store, msg(p1_report()))
    fb = {"fix_id": res.fixes[0]["fix_id"], "decision": "rejected", "reason": "budget is intentional",
          "issue_key": p1_report()["issue_key"], "playbook": "P1"}
    res2, _ = discuss(store, msg(fb, type="fix_feedback", mid="m2", sender="ui"), msg(p1_report(change="changed"), mid="m3"),
                      state=state)
    d = res2.discussions[0]
    assert (d["outcome"], d["reason"]) == ("no_fix", "previously_rejected") and not res2.fixes


@pytest.mark.parametrize("store_kw", [{"planned": True}, {"outage": True}])
def test_evidenced_objection_blocks_the_fix(store_kw):
    res, _ = discuss(FakeStore(edges_validated=True, **store_kw), msg(p1_report()))
    assert (res.discussions[0]["outcome"], res.discussions[0]["reason"]) == ("no_fix", "expert_objection")


def test_not_fixable_and_monitor_outcomes():
    tracking = p1_report(issue_key="T|A1|DE", kind="data_completeness", state="tracking_loss")
    up = p1_report(issue_key="W|A2|DE", kind="auction", state="auction_pressure_up")
    down = p1_report(issue_key="W|A3|DE", kind="auction", state="auction_pressure_down")
    summary = {"source": "pacing_watcher", "sim_now": "2026-10-08T12:00:00Z",
               "explained": ["A4/DE: Paid Clicks +70% -> matches planned event 'Promo'"]}
    res, state = discuss(FakeStore(), msg(tracking), msg(up, mid="m2"), msg(down, mid="m3"),
                         msg(summary, type="cycle_summary", mid="m4"))
    got = {d["issue_key"]: (d["outcome"], d.get("reason")) for d in res.discussions}
    assert got == {"T|A1|DE": ("no_fix", "data_issue"), "W|A2|DE": ("no_fix", "not_fixable_cause"),
                   "W|A3|DE": ("monitor", None), "Explained|A4/DE": ("no_fix", "innocent_change")}
    # P5 from the second day on: no measured bid elasticity -> no fix, never a guess
    res2, _ = discuss(FakeStore(), msg(p1_report(issue_key="W|A3|DE", kind="auction", state="auction_pressure_down",
                                                 sim_now="2026-10-09T12:00:00Z")), state=state)
    assert (res2.discussions[0]["outcome"], res2.discussions[0]["reason"]) == ("no_fix", "insufficient_evidence")


def test_resolved_report_closes_the_issue():
    res, state = discuss(FakeStore(), msg(p1_report()))
    res2, _ = discuss(FakeStore(), msg({"source": "pacing_watcher", "change": "resolved",
                                        "issue_key": p1_report()["issue_key"]}, mid="m2"), state=state)
    assert not res2.discussions and res2.brief["resolved"] == [p1_report()["issue_key"]]
    assert p1_report()["issue_key"] not in state["issues"]


# --------------------------------------------------------------------------- Cartographer
def test_expression_parser_rejects_anything_but_arithmetic():
    parse_expr("(a + b) / c")
    for bad in ("__import__('os')", "a.b", "a[0]", "'x'"):
        with pytest.raises(ValueError):
            parse_expr(bad)


def test_validate_influence_defers_without_reviewer():
    store = FakeStore()
    store.signal = {"id": "sig1", "status": "open", "metric": "Budget & Pacing Controller", "observed": 0.99,
                    "expected": 0.3, "observations": ["Bid Multiplier 1.8 vs expected 0.9 (+100%, z=+6.0)"]}
    p = {"proposal_id": "p1", "op": "validate_influence", "src": "Budget & Pacing Controller", "dst": "Bid Multiplier",
         "signal_id": "sig1", "country": "DE",
         "evidence": ["Budget & Pacing Controller 0.99 vs expected 0.3 (z=+9.0)",
                      "Bid Multiplier 1.8 vs expected 0.9 (+100%, z=+6.0)"]}
    d = GraphEngineer(store=store, llm=False).validate_influence(p)
    assert d["outcome"] == "deferred" and "unknown never acts" in d["reason"]
    assert d["checks"][0]["pass"] and not store.writes


def test_validate_influence_rejects_evidence_not_in_graph():
    store = FakeStore()
    store.signal = {"id": "sig1", "status": "open", "metric": "M", "observed": 1, "expected": 1, "observations": []}
    d = GraphEngineer(store=store, llm=False).validate_influence(
        {"proposal_id": "p1", "src": "Budget & Pacing Controller", "dst": "Bid Multiplier", "signal_id": "sig1",
         "evidence": ["Bid Multiplier 9.9 vs expected 0.1 (+9800%, z=+99)"]})
    assert d["outcome"] == "rejected" and not store.writes


def test_onboarding_writes_metric_and_exact_formula_links():
    store = FakeStore()
    d = GraphEngineer(store=store, llm=False).onboard(
        {"id": "ROAS", "definition": "revenue per euro spent", "unit": "x", "cypher_value": "revenue / spend"})
    assert d["outcome"] == "applied"
    links = [w for w in store.writes if w[0] == "link"]
    assert {(w[1], w[3]) for w in links} == {("ROAS", "Revenue"), ("Total Ad Spend", "Revenue")}
    assert all(w[4] <= 1e-9 for w in links)


@pytest.mark.parametrize("rec,check", [
    ({"id": "CPC", "unit": "EUR", "cypher_value": "spend / clicks"}, "unique id"),
    ({"id": "Clicks per euro", "unit": "ratio", "cypher_value": "clicks / spend"}, "ratio in [0,1]"),
    ({"id": "Margin", "unit": "EUR", "cypher_value": "margin"}, "evaluable"),
    ({"id": "X", "unit": "furlongs", "cypher_value": "spend"}, "unit"),
])
def test_onboarding_rejects_invalid_metrics(rec, check):
    store = FakeStore()
    d = GraphEngineer(store=store, llm=False).onboard({"definition": "d", **rec})
    assert d["outcome"] == "rejected" and d["reason"].startswith(check) and not store.writes
