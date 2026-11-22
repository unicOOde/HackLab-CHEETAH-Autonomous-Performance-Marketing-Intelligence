"""Agent runtime: bus, scheduler triggers, run semantics, health, simulated clock, HTTP API."""
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from agent_runtime.bus import AgentBus, Message, iso, publish_event  # noqa: E402
from agent_runtime.builtin import run_sim_clock  # noqa: E402
from agent_runtime.health import agent_health, scheduler_alive, worker_health  # noqa: E402
from agent_runtime.scheduler import AgentSpec, RunResult, Scheduler  # noqa: E402


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


@pytest.fixture
def bus(tmp_path):
    return AgentBus(tmp_path / "bus").ensure()


def make(bus, *specs, scale=1.0):
    clock = FakeClock()
    return Scheduler(list(specs), bus=bus, time_scale=scale, heartbeat_s=0.0, clock=clock), clock


def settle(s):
    """Tick until two consecutive ticks start no run (a fast run can finish inside one tick,
    so 'nothing is running right now' alone doesn't mean the pipeline is done)."""
    def runs_so_far():
        base = s.bus.path("runs")
        return sum(len(list(d.iterdir())) for d in base.iterdir()) if base.is_dir() else 0
    quiet = 0
    for _ in range(50):
        before = runs_so_far()
        s.tick()
        assert s.wait_idle(5)
        quiet = quiet + 1 if runs_so_far() == before and not s.running() else 0
        if quiet >= 2:
            return
    raise AssertionError("scheduler did not settle")


def recorder(calls, result=None, post=None):
    def run(ctx):
        calls.append(ctx.trigger)
        if post:
            ctx.post(*post)
        return result
    return run


# ------------------------------------------------------------------ bus
def test_bus_post_drain_ack_and_broadcast(bus):
    bus.agents = {"a", "b", "c"}
    bus.post(Message("a", "b", "report", {"x": 1}))
    assert bus.post(Message("a", "*", "decision")) == ["b", "c"]
    msgs = bus.drain("b")
    assert [m.type for m in msgs] == ["report", "decision"]
    bus.ack("b", msgs)
    assert bus.drain("b") == [] and len(bus.history(agent="b")) == 2


def test_bus_fail_dead_letters_after_three_attempts(bus):
    bus.post(Message("a", "b", "report"))
    for _ in range(2):
        assert bus.fail("b", bus.drain("b"), "boom") == []
    assert bus.drain("b")[0].attempts == 2
    assert len(bus.fail("b", bus.drain("b"), "boom")) == 1
    assert bus.drain("b") == [] and bus.dead_count("b") == 1


def test_publish_event_is_noop_without_bus(tmp_path, monkeypatch):
    monkeypatch.setenv("CHEETAH_BUS_DIR", str(tmp_path / "missing"))
    assert publish_event("sim.day_closed") is None
    assert not (tmp_path / "missing").exists()


# ------------------------------------------------------------------ triggers
def test_interval_trigger_runs_every_interval_scaled(bus):
    calls = []
    s, clock = make(bus, AgentSpec("w", recorder(calls), interval_s=60), scale=2.0)
    s.tick(); s.wait_idle()
    assert calls == []
    clock.t = 30          # 60 s at time scale 2
    settle(s)
    assert [c["kinds"] for c in calls] == [["interval"]]
    st = bus.read_status("w")
    assert st["last_result"] == "ok" and st["state"] == "idle" and st["interval_s"] == 30


def test_manual_trigger_from_another_process_via_bus(bus):
    calls = []
    s, _ = make(bus, AgentSpec("w", recorder(calls)))
    AgentBus(bus.root).post_trigger("w", reason="button", payload={"set": "x"})
    settle(s)
    assert calls[0]["kinds"] == ["manual"] and calls[0]["reasons"] == ["button"]
    assert calls[0]["payloads"] == [{"set": "x"}]


def test_event_trigger_matches_patterns(bus):
    calls = []
    s, _ = make(bus, AgentSpec("w", recorder(calls), on_events=("sim.*",)))
    bus.post_event("clock.tick")
    settle(s)
    assert calls == []
    bus.post_event("sim.day_closed", {"date": "2026-10-03"})
    settle(s)
    assert calls[0]["events"][0]["payload"] == {"date": "2026-10-03"}
    assert bus.recent_events()[0]["name"] == "sim.day_closed"


def test_inbox_trigger_acks_and_flushes_posts(bus):
    calls = []
    s, _ = make(bus, AgentSpec("w", recorder(calls, post=("other", "report", {"n": 1})),
                               on_inbox=True, inbox_debounce_s=0),
                AgentSpec("other", lambda ctx: None))
    bus.post(Message("x", "w", "investigate"))
    settle(s)
    assert calls[0]["kinds"] == ["inbox"]
    assert bus.pending("w") == []
    assert [m.payload for m in bus.drain("other")] == [{"n": 1}]


def test_condition_trigger(bus):
    calls, flag = [], {"on": False}
    s, clock = make(bus, AgentSpec("w", recorder(calls), condition=lambda b: flag["on"], condition_poll_s=1))
    settle(s)
    assert calls == []
    flag["on"], clock.t = True, 2
    s.tick(); s.wait_idle()
    assert calls[0]["kinds"] == ["condition"]


def test_failed_run_leaks_nothing_and_keeps_inbox(bus):
    def boom(ctx):
        ctx.post("other", "report")
        ctx.publish("fixes", "fix_1", {"fix_id": "fix_1"})
        raise RuntimeError("graph down")
    s, _ = make(bus, AgentSpec("w", boom), AgentSpec("other", lambda ctx: None))
    bus.post(Message("x", "w", "investigate"))
    s.trigger("w"); s.tick(); s.wait_idle()
    assert bus.drain("other") == [] and bus.read_outbox("fixes") == []
    assert bus.drain("w")[0].attempts == 1
    st = bus.read_status("w")
    assert st["last_result"] == "failed" and "graph down" in st["last_error"]
    run = bus.read_runs("w")[0]
    assert run["result"] == "failed" and "RuntimeError" in "".join(run["log"])


def test_triggers_during_a_run_coalesce_into_one_follow_up(bus):
    gate, calls = threading.Event(), []

    def slow(ctx):
        calls.append(ctx.trigger["kinds"])
        gate.wait(5)
    s, _ = make(bus, AgentSpec("w", slow))
    s.trigger("w"); s.tick()
    assert s.running() == ["w"]
    s.trigger("w"); s.trigger("w"); s.tick()
    gate.set(); s.wait_idle()
    settle(s)
    assert len(calls) == 2


def test_timeout_marks_down_and_discards_results(bus):
    gate = threading.Event()

    def slow(ctx):
        gate.wait(5)
        ctx.post("other", "report")
    s, clock = make(bus, AgentSpec("w", slow, timeout_s=10), AgentSpec("other", lambda ctx: None))
    s.trigger("w"); s.tick()
    clock.t = 11; s.tick()
    st = bus.read_status("w")
    assert st["timed_out"] and agent_health(st, True, datetime.now(timezone.utc))["health"] == "down"
    gate.set(); s.wait_idle()
    assert bus.read_status("w")["last_result"] == "failed" and bus.drain("other") == []


def test_unavailable_agent_never_runs_and_says_why(bus):
    calls = []
    s, _ = make(bus, AgentSpec("w", recorder(calls), on_inbox=True, inbox_debounce_s=0,
                               available=lambda: "ANTHROPIC_API_KEY not set"))
    bus.post(Message("x", "w", "investigate"))
    s.trigger("w")
    settle(s)
    st = bus.read_status("w")
    assert calls == [] and st["state"] == "not_configured" and "ANTHROPIC" in st["unavailable_reason"]
    assert len(bus.pending("w")) == 1     # kept for when it becomes available


def test_scheduler_is_a_singleton_per_bus(bus):
    a, _ = make(bus, AgentSpec("w", lambda ctx: None))
    b, _ = make(bus, AgentSpec("w", lambda ctx: None))
    assert a.start()
    try:
        assert not b.start()
    finally:
        a.stop()
    assert bus.read_status("_scheduler")["stopped"]


# ------------------------------------------------------------------ health
NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


def ago(s):
    return iso(NOW - timedelta(seconds=s))


@pytest.mark.parametrize("status,sched_ok,expected", [
    (None, True, "never_run"),
    (None, False, "down"),
    ({"state": "not_configured", "unavailable_reason": "x"}, True, "not_configured"),
    ({"state": "running", "heartbeat_at": ago(5)}, True, "running"),
    ({"state": "running", "heartbeat_at": ago(120)}, True, "down"),
    ({"state": "running", "heartbeat_at": ago(5)}, False, "down"),
    ({"state": "idle", "last_result": "ok", "last_ok_at": ago(100), "interval_s": 1800}, True, "active"),
    ({"state": "idle", "last_result": "ok", "last_ok_at": ago(4000), "interval_s": 1800}, True, "down"),
    ({"state": "idle", "last_result": "ok", "last_ok_at": ago(99999)}, True, "active"),   # event-driven
    ({"state": "idle", "last_result": "degraded", "last_ok_at": ago(10)}, True, "degraded"),
    ({"state": "idle", "last_result": "ok", "last_ok_at": ago(10), "dead_letters": 2}, True, "degraded"),
    ({"state": "idle", "last_result": "failed", "last_error": "x"}, True, "down"),
    ({"state": "idle", "last_result": "ok", "last_ok_at": ago(10)}, False, "down"),
])
def test_agent_health(status, sched_ok, expected):
    assert agent_health(status, sched_ok, NOW)["health"] == expected


def test_scheduler_and_worker_liveness():
    assert scheduler_alive({"heartbeat_at": ago(5)}, NOW)
    assert not scheduler_alive({"heartbeat_at": ago(60)}, NOW)
    assert not scheduler_alive({"heartbeat_at": ago(1), "stopped": True}, NOW)
    assert worker_health({"heartbeat_at": ago(2)}, NOW)["health"] == "running"
    assert worker_health({"heartbeat_at": ago(30)}, NOW)["health"] == "down"
    assert worker_health(None, NOW)["health"] == "never_run"


# ------------------------------------------------------------------ simulated clock
def test_sim_clock_ticks_wraps_and_jumps(bus, monkeypatch):
    monkeypatch.setenv("CHEETAH_SIM_START", "2026-10-08T06:00")
    monkeypatch.setenv("CHEETAH_SIM_END", "2026-10-08T07:00")
    ticks = []
    s, _ = make(bus, AgentSpec("sim_clock", run_sim_clock),
                AgentSpec("watcher", lambda ctx: ticks.append(ctx.trigger["events"][0]["payload"]),
                          on_events=("clock.tick",)),
                AgentSpec("product_manager", lambda ctx: None,
                          available=lambda: "not implemented yet"))
    for _ in range(3):
        s.trigger("sim_clock")
        settle(s)
    assert [t["now"] for t in ticks] == ["2026-10-08T06:00", "2026-10-08T07:00", "2026-10-08T06:00"]
    assert ticks[-1]["wrapped"] and bus.get_state("sim_clock")["wraps"] == 1
    assert [m.type for m in bus.drain("product_manager")] == ["clock_wrapped"]
    s.trigger("sim_clock", payload={"set": "2026-10-08T07:00"})
    settle(s)
    assert ticks[-1] == {"now": "2026-10-08T07:00", "wrapped": False, "jumped": True}
    s.trigger("sim_clock", payload={"set": "2026-10-20T00:00"})
    settle(s)
    assert bus.read_status("sim_clock")["last_result"] == "failed"


# ------------------------------------------------------------------ HTTP API
@pytest.fixture
def client(tmp_path, monkeypatch):
    import os
    os.environ.setdefault("DATABASE_URL", "sqlite:///" + str(tmp_path / "api.db"))
    from fastapi.testclient import TestClient
    from app import app
    monkeypatch.setenv("CHEETAH_BUS_DIR", str(tmp_path / "apibus"))
    return TestClient(app), AgentBus(tmp_path / "apibus")


def test_api_not_configured_without_bus(client):
    c, _ = client
    body = c.get("/api/v1/agents/status").json()
    assert body["status"] == "not_configured" and body["agents"] == []


def test_api_status_trigger_and_events(client):
    c, bus = client
    assert c.post("/api/v1/agents/nope/trigger").status_code == 404
    r = c.post("/api/v1/agents/sim_clock/trigger", json={"reason": "demo", "payload": {"set": "2026-10-14T09:00"}})
    assert r.status_code == 202
    t = bus.take_triggers()
    assert t[0]["agent"] == "sim_clock" and t[0]["payload"] == {"set": "2026-10-14T09:00"}
    assert c.post("/api/v1/agents/events", json={"name": "sim.day_closed"}).status_code == 202
    assert c.post("/api/v1/agents/events", json={"name": "bad name!"}).status_code == 422
    body = c.get("/api/v1/agents/status").json()
    names = {a["agent"]: a for a in body["agents"]}
    assert {"sim_clock", "pacing_watcher", "investigator", "product_manager", "deviation_detector"} <= set(names)
    assert names["forecast_service"]["health"] == "not_configured"
    assert names["deviation_detector"]["health"] == "never_run"   # no worker heartbeat yet
    assert body["scheduler"]["alive"] is False
    assert c.get("/api/v1/agents/sim_clock/runs").json() == {"runs": []}
    assert c.get("/api/v1/agents/nope/runs").status_code == 404


def test_api_fix_feedback_flow(client):
    c, bus = client
    assert c.post("/api/v1/agents/fixes/fix_x/feedback", json={"decision": "accepted"}).status_code == 404
    bus.write_outbox("fixes", "fix_1", {"fix_id": "fix_1", "issue_key": "k", "playbook": "P1", "status": "proposed"})
    assert c.post("/api/v1/agents/fixes/fix_1/feedback", json={"decision": "maybe"}).status_code == 422
    r = c.post("/api/v1/agents/fixes/fix_1/feedback", json={"decision": "rejected", "reason": "budget frozen"})
    assert r.status_code == 201 and r.json()["executed"] is False
    assert c.post("/api/v1/agents/fixes/fix_1/feedback", json={"decision": "accepted"}).status_code == 409
    fixes = c.get("/api/v1/agents/fixes?status=rejected").json()["fixes"]
    assert fixes[0]["feedback"]["reason"] == "budget frozen"
    pm = bus.drain("product_manager")
    assert pm[0].type == "fix_feedback" and pm[0].payload["decision"] == "rejected"
    assert c.get("/api/v1/agents/messages?type=fix_feedback").json()["messages"][0]["from"] == "ui"


# ------------------------------------------------------------------ built-in pipeline (stand-in agents)
def test_clock_to_pacing_to_investigator_pipeline(bus, monkeypatch):
    """sim_clock -> clock.tick -> pacing_watcher -> investigate -> investigator -> reports to the PM.

    The real adapters run; only the graph-backed agent classes are replaced, so nothing is written
    to Neo4j. The messages carry the agents' own dataclass numbers.
    """
    import dataclasses
    from agent_runtime import builtin
    builtin._prepare_imports()
    import agents.investigator_agent as inv_mod
    import agents.pacing_watcher as pw_mod

    class Store:
        def close(self):
            pass

        def get(self, label, node_id):
            return {"text": "bid multiplier pinned at 1.9", "confidence": 0.8, "evidence": ["bid_mult 1.9"],
                    "status": "open"}

    det = pw_mod.Detection(issue_key="Budget & Pacing Controller|0003WS|DE", kind="pacing",
                           metric="Budget & Pacing Controller", severity="major", state="exhausted",
                           ad="0003WS", country="DE", channel="google_ads", observed=1.0, expected=0.4,
                           pct_change=1.5, z=6.1, window="00:00-10:00",
                           path_to_revenue=["Budget & Pacing Controller", "Total Ad Spend", "Revenue"])
    seen = {}

    class FakeWatcher:
        def __init__(self):
            self.store = Store()

        def run(self, now):
            seen["now"] = now
            return pw_mod.CycleReport(now=now.strftime("%Y-%m-%dT%H:%M:%SZ"), new=[("signal_1", det)],
                                      explained=["promo 00008F/US"])

    class FakeInvestigator:
        def __init__(self, run_overlooker=True):
            assert run_overlooker is False      # the graph engineer owns the overlooker now
            self.store = Store()

        def investigate(self, label, node_id, on_step=None):
            if label == "Country":
                raise ValueError("no context")
            return inv_mod.Investigation(target=(label, node_id), hypotheses=["hyp_1"])

    monkeypatch.setattr(pw_mod, "PacingWatcher", FakeWatcher)
    monkeypatch.setattr(inv_mod, "InvestigatorAgent", FakeInvestigator)
    monkeypatch.setenv("CHEETAH_SIM_START", "2026-10-14T09:00")
    specs = [dataclasses.replace(s, available=None, inbox_debounce_s=0)
             for s in builtin.default_specs() if s.name in ("sim_clock", "pacing_watcher", "investigator")]
    specs.append(AgentSpec("product_manager", lambda ctx: None, available=lambda: "not implemented yet"))
    s, _ = make(bus, *specs)
    s.trigger("sim_clock", reason="demo click")
    settle(s)

    assert seen["now"].strftime("%Y-%m-%dT%H:%M") == "2026-10-14T09:00"
    pm = bus.drain("product_manager")
    by_type = {}
    for m in pm:
        by_type.setdefault(m.type, []).append(m)
    assert set(by_type) == {"report", "cycle_summary"}
    pacing = [m for m in by_type["report"] if m.sender == "pacing_watcher"][0]
    assert pacing.payload["change"] == "new" and pacing.payload["z"] == 6.1 and pacing.cycle_id == "2026-10-14T09:00"
    assert by_type["cycle_summary"][0].payload["explained"] == ["promo 00008F/US"]
    inv = [m.payload for m in by_type["report"] if m.sender == "investigator"]
    assert inv[0]["target"] == ["Ad", "0003WS"] and inv[0]["hypotheses"][0]["confidence"] == 0.8
    assert "no context" in inv[1]["error"]
    assert bus.read_status("investigator")["last_result"] == "degraded"      # one target failed
    assert bus.read_status("pacing_watcher")["last_summary"].startswith("2026-10-14T09:00: 1 new")
    assert bus.pending("investigator") == []                                 # acked
