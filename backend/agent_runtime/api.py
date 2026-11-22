"""Read side for the HTTP API: everything the frontend sees about agents comes through here.

The API process never talks to the scheduler threads. It reads status/, runs/, outbox/ and the
message folders, and its only writes are: trigger files (run an agent now), event files
(something happened), and fix feedback. All of them go through AgentBus, so they're atomic.
If the bus directory doesn't exist, everything reports `not_configured` instead of inventing state.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .builtin import TRACK_A_SERVICES, default_specs
from .bus import AgentBus, Message, iso, utcnow
from .health import NOT_CONFIGURED, agent_health, scheduler_alive, worker_health

FEEDBACK_DECISIONS = {"accepted", "rejected"}


class NotFound(KeyError):
    pass


class Conflict(ValueError):
    pass


def known_agents() -> List[str]:
    return [s.name for s in default_specs()]


def status(bus: Optional[AgentBus] = None) -> Dict[str, Any]:
    bus = bus or AgentBus()
    now = utcnow()
    if not bus.exists:
        return {"status": NOT_CONFIGURED, "reason": f"agent bus {bus.root} does not exist",
                "scheduler": None, "agents": []}
    sched = bus.read_status("_scheduler")
    sched_ok = scheduler_alive(sched, now)
    agents: List[Dict[str, Any]] = []
    for spec in default_specs():
        st = bus.read_status(spec.name)
        agents.append({"agent": spec.name, "name": spec.display_name or spec.name, "role": spec.role,
                       "track": "B", "source": "agent_bus", "triggers": spec.trigger_text(),
                       "description": spec.description, "inbox": len(bus.pending(spec.name)),
                       **agent_health(st, sched_ok, now), "status": st})
    worker = bus.read_status("_worker")
    wh = worker_health(worker, now)
    for svc in TRACK_A_SERVICES:
        h = wh if svc["runs_in_worker"] else {"health": NOT_CONFIGURED, "reason": svc["reason"]}
        agents.append({"agent": svc["agent"], "name": svc["name"], "role": svc["role"],
                       "track": "A", "source": "worker_thread",
                       "description": svc.get("description", ""), **h})
    return {"status": "ok", "checked_at": iso(now),
            "scheduler": {**(sched or {}), "alive": sched_ok},
            "sim_clock": bus.get_state("sim_clock"), "agents": agents}


def runs(agent: str, limit: int = 20, bus: Optional[AgentBus] = None) -> List[Dict[str, Any]]:
    if agent not in known_agents():
        raise NotFound(agent)
    return (bus or AgentBus()).read_runs(agent, limit)


def trigger(agent: str, reason: str = "manual", payload: Optional[Dict[str, Any]] = None,
            bus: Optional[AgentBus] = None) -> Dict[str, Any]:
    if agent not in known_agents():
        raise NotFound(agent)
    bus = (bus or AgentBus()).ensure()
    tid = bus.post_trigger(agent, reason=reason, by="api", payload=payload)
    return {"trigger_id": tid, "agent": agent, "queued_at": iso()}


def event(name: str, payload: Optional[Dict[str, Any]] = None, bus: Optional[AgentBus] = None) -> Dict[str, Any]:
    bus = (bus or AgentBus()).ensure()
    return {"event_id": bus.post_event(name, payload, source="api"), "name": name}


def messages(limit: int = 50, type: Optional[str] = None, agent: Optional[str] = None,
             bus: Optional[AgentBus] = None) -> List[Dict[str, Any]]:
    return (bus or AgentBus()).history(limit=limit, type=type, agent=agent)


def events(limit: int = 50, bus: Optional[AgentBus] = None) -> List[Dict[str, Any]]:
    return (bus or AgentBus()).recent_events(limit)


def outbox(kind: str, limit: int = 30, bus: Optional[AgentBus] = None,
           **filters: Optional[str]) -> List[Dict[str, Any]]:
    bus = bus or AgentBus()
    rows = bus.read_outbox(kind, limit=10_000 if any(filters.values()) else limit)
    if kind == "fixes":
        rows = [_with_feedback(bus, r) for r in rows]
    for k, v in filters.items():
        if v:
            rows = [r for r in rows if r.get(k) == v]
    return rows[:limit]


def _with_feedback(bus: AgentBus, fix: Dict[str, Any]) -> Dict[str, Any]:
    fb = bus.read_feedback(fix.get("fix_id", ""))
    return {**fix, "feedback": fb, "status": fb["decision"] if fb else fix.get("status", "proposed")}


METRIC_UNITS = {"ratio", "count", "EUR", "seconds", "x"}   # same set as graph_store.METRIC_UNITS


def submit_metric(record: Dict[str, Any], bus: Optional[AgentBus] = None) -> Dict[str, Any]:
    """A contributor's metric goes to the graph engineer's inbox; it validates and places it."""
    if record.get("unit") not in METRIC_UNITS:
        raise ValueError(f"unit must be one of {sorted(METRIC_UNITS)}")
    bus = (bus or AgentBus()).ensure()
    msg = Message(sender="ui", to="graph_engineer", type="metric_submission", payload=record)
    bus.post(msg)
    return {"msg_id": msg.msg_id, "metric": record["id"], "queued_at": msg.ts}


def fix_feedback(fix_id: str, decision: str, reason: str = "", bus: Optional[AgentBus] = None) -> Dict[str, Any]:
    if decision not in FEEDBACK_DECISIONS:
        raise ValueError(f"decision must be one of {sorted(FEEDBACK_DECISIONS)}")
    bus = bus or AgentBus()
    fix = bus.get_outbox("fixes", fix_id)
    if fix is None:
        raise NotFound(fix_id)
    if bus.read_feedback(fix_id) is not None:
        raise Conflict(f"feedback for {fix_id} already recorded")
    fb = {"fix_id": fix_id, "decision": decision, "reason": reason, "ts": iso(),
          "executed": False, "note": "recommendation only; nothing is executed by the system"}
    bus.write_feedback(fix_id, fb)
    bus.post(Message(sender="ui", to="product_manager", type="fix_feedback",
                     payload={**fb, "issue_key": fix.get("issue_key"), "playbook": fix.get("playbook")},
                     cycle_id=fix.get("cycle_id")))
    return fb
