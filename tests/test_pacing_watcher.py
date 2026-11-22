"""Offline tests for the pacing watcher: simulator rows in, no Neo4j needed.

    .venv/bin/python -m pytest tests/        # or: .venv/bin/python tests/test_pacing_watcher.py
"""
import os
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.chdir(os.path.join(os.path.dirname(__file__), ".."))   # simulator reads the CSVs from the repo root

import paid_sim
from agents.pacing_watcher import PacingWatcher, format_report


class FakeStore:
    """The subset of GraphStore the watcher uses, in memory."""

    def __init__(self):
        self.signals = {}
        self.changes = []

    def read(self, cypher, **params):
        if "Signal" in cypher:
            return [{"s": dict(s)} for s in self.signals.values() if s["status"] == "open"]
        return []

    def exists(self, label, node_id):
        return True

    def add_signal(self, props, links):
        sid = f"signal_{uuid.uuid4().hex[:8]}"
        self.signals[sid] = {**props, "id": sid, "status": "open", "agent": "pacing_watcher"}
        return sid

    def update_agent_node(self, label, node_id, props, reason=""):
        self.signals[node_id].update(props)

    def set_status(self, label, node_id, status, reason=""):
        self.signals[node_id]["status"] = status


def watcher(scenarios):
    rows, events, slices = paid_sim.simulate(scenarios)
    ev = [{**e, "ads": [e["ad"]], "countries": [e["country"]]} for e in events]
    tel = lambda since, until: [r for r in rows if f"{r['hour_id']}:00:00Z" < until]
    w = PacingWatcher(store=FakeStore(), telemetry_fn=tel, events_fn=lambda s, u: ev)
    return w, slices


def at(h):
    return datetime(2026, 10, 14, h, tzinfo=timezone.utc)


def slice_of(slices, scenario):
    s = slices[paid_sim.SCENARIO_SLICE[scenario]]
    return s["ad"], s["country"]


def test_quiet_day_has_no_signals():
    w, _ = watcher([])
    for h in (9, 12, 18):
        r = w.run(at(h))
        assert not r.new, format_report(r)


def test_scenarios_detected_and_classified():
    w, slices = watcher(paid_sim.SCENARIOS)
    r = w.run(at(12))
    print(format_report(r))
    by = {(d.ad, d.country): d for _, d in r.new}

    bug = by[slice_of(slices, "pacing_bug")]
    assert bug.metric == "Budget & Pacing Controller" and bug.severity == "major"
    assert any("not respecting its schedule" in h for h in bug.hypotheses[:1])

    pull = by[slice_of(slices, "competitor_pullback")]
    assert pull.metric == "Auction Win Rate" and pull.state == "auction_pressure_down"
    assert "Competitor pull-back" in pull.hypotheses[0]

    trk = by[slice_of(slices, "tracking_loss")]
    assert trk.kind == "data_completeness"            # not a false pacing/auction fault

    promo = slice_of(slices, "planned_promo")
    assert promo not in by                            # innocent change
    assert any(e.startswith(f"{promo[0]}/{promo[1]}") for e in r.explained)
    assert len(r.new) == 3


def test_dedup_escalation_and_resolution():
    w, slices = watcher(paid_sim.SCENARIOS)
    first = w.run(at(9))
    keys = {d.issue_key for _, d in first.new}
    second = w.run(at(10))
    assert not [d for _, d in second.new if d.issue_key in keys]   # not re-reported
    assert second.ongoing or second.changed
    assert not w.run(at(14)).new                       # keys stay stable while the issues evolve
    w.run(at(14))                                      # hour 11 still in the 3h completeness window
    later = w.run(at(15))                              # tracking recovered at 12:00
    resolved = {k for _, k in later.resolved}
    assert any(k.startswith("Data Freshness") for k in resolved), format_report(later)
    bug = slice_of(slices, "pacing_bug")
    state = [s for s in w.store.signals.values()
             if s["ad"] == bug[0] and s["country"] == bug[1] and s["kind"] == "pacing"]
    assert state and state[0]["state"] == "exhausted"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
