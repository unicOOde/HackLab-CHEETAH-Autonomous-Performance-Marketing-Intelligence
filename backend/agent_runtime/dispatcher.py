"""Dispatcher ("Anomaly Orchestrator"): hands every detected incident to an agent and keeps its lifecycle.

    worker close_day ── sim.deviation_detected ──▶ dispatcher ── investigate ──▶ investigator
                                                       ▲                             │
                                                       └────────── report ───────────┘

identified   the worker found the day outside its locked forecast interval
in_progress  the dispatcher found the segment that drove the deviation and sent it to an agent
resolved     that agent reported back. Resolved is not a verified root cause: evidence_outcome says
             'hypotheses_proposed' or 'inconclusive'.

Routing: the day's install amount is compared per segment (placement, category, country) with the
segment's trailing median of the prior days. The narrowest segment that explains at least half of
the gap becomes the target; otherwise the segment with the largest share. Today only the
investigator takes targeted requests, so it is the agent picked. If it is unavailable (no Neo4j or
no LLM), the incident stays `identified` and the task says why. Nothing is faked.
"""
from __future__ import annotations

import json
import statistics
import time
import uuid
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from .scheduler import DEGRADED, OK, RunContext, RunResult

NAME = "dispatcher"
INVESTIGATOR = "investigator"
BASELINE_DAYS = 7
DOMINANT_SHARE = 0.5
# narrowest first: (SourceEvent column, investigator target label)
DIMENSIONS = (("game_placement_id", "Ad"), ("category", "Category"), ("country", "Country"))


def find_driver(db, dataset_id: str, day: str) -> Optional[Dict[str, Any]]:
    """The segment that explains most of the day's deviation from its trailing per-segment median."""
    from sqlalchemy import func, select
    from models import SourceEvent

    d = date.fromisoformat(day)
    prior = [(d - timedelta(days=i)).isoformat() for i in range(1, BASELINE_DAYS + 1)]
    best: Optional[Dict[str, Any]] = None
    for column, label in DIMENSIONS:
        col = getattr(SourceEvent, column)
        rows = db.execute(select(SourceEvent.event_day, col, func.sum(SourceEvent.amount))
                          .where(SourceEvent.dataset_id == dataset_id, SourceEvent.kind == "install",
                                 SourceEvent.event_day.in_([day] + prior), col.is_not(None))
                          .group_by(SourceEvent.event_day, col)).all()
        days_seen = {r[0] for r in rows if r[0] != day}
        if day not in {r[0] for r in rows} or not days_seen:
            continue
        by_seg: Dict[str, Dict[str, float]] = {}
        for ev_day, seg, amt in rows:
            by_seg.setdefault(str(seg), {})[ev_day] = float(amt or 0.0)
        deltas = {seg: v.get(day, 0.0) - statistics.median([v.get(p, 0.0) for p in sorted(days_seen)])
                  for seg, v in by_seg.items()}
        gap = sum(deltas.values())
        if abs(gap) < 1e-9:
            continue
        seg, delta = max(deltas.items(), key=lambda kv: kv[1] / gap)
        share = delta / gap
        cand = {"label": label, "id": seg, "dimension": column, "delta": round(delta, 2),
                "gap": round(gap, 2), "share": round(share, 3), "baseline_days": len(days_seen)}
        if share >= DOMINANT_SHARE:
            return cand
        if best is None or share > best["share"]:
            best = cand
    return best


def _investigator_unavailable() -> Optional[str]:
    from .builtin import graph_unavailable, llm_unavailable
    return graph_unavailable() or llm_unavailable()


def _emit_incident(db, inc) -> None:
    from core import emit
    emit(db, inc.run_id, "incident.updated",
         {"incident_id": inc.id, "lifecycle_status": inc.lifecycle_status, "evidence_outcome": inc.evidence_outcome,
          "assigned_agent": inc.assigned_agent, "investigation_summary": inc.investigation_summary},
         inc.detection_date)


def _emit_task(db, t) -> None:
    from core import emit
    emit(db, t.run_id, "agent.task_updated",
         {"task_id": t.id, "incident_id": t.incident_id, "agent": t.agent, "status": t.status,
          "evidence_outcome": t.evidence_outcome, "fallback": t.fallback})


def _wait_for_incident(db, incident_id: str, timeout_s: float = 10.0):
    """The event is published inside the worker's day-close transaction, before its commit."""
    from models import Incident
    deadline = time.monotonic() + timeout_s
    while True:
        inc = db.get(Incident, incident_id)
        if inc is not None or time.monotonic() > deadline:
            return inc
        time.sleep(0.2)
        db.expire_all()


def dispatch(ctx: RunContext, db, ev: Dict[str, Any]) -> str:
    from sqlalchemy import select
    from models import AgentTask, utcnow

    inc = _wait_for_incident(db, ev["incident_id"])
    if inc is None:
        return f"{ev['incident_id']}: incident not found"
    if inc.lifecycle_status != "identified" or inc.assigned_agent:
        return f"{inc.id}: already {inc.lifecycle_status}"
    own = db.scalars(select(AgentTask).where(AgentTask.incident_id == inc.id, AgentTask.agent == NAME)).first()
    if own is None:
        own = AgentTask(id="TASK-" + uuid.uuid4().hex[:10], run_id=inc.run_id, incident_id=inc.id, agent=NAME,
                        status="queued")
        db.add(own)
    driver = find_driver(db, ev.get("dataset_id") or "", inc.detection_date)
    own.status, own.ended_at = "completed", utcnow()
    if driver is None:
        own.evidence_outcome = "no_segment_driver"
        inc.investigation_summary = "No segment-level data to localise the deviation; not routed."
        _emit_task(db, own)
        _emit_incident(db, inc)
        db.commit()
        return f"{inc.id}: no driver, not routed"
    where = (f"{driver['label']} {driver['id']} explains {driver['share']:.0%} of the "
             f"{driver['gap']:+,.2f} gap vs. its {driver['baseline_days']}-day median")
    own.evidence_outcome = "routed"
    own.output_refs = json.dumps([driver])
    inc.assigned_agent = INVESTIGATOR
    task = AgentTask(id="TASK-" + uuid.uuid4().hex[:10], run_id=inc.run_id, incident_id=inc.id,
                     agent=INVESTIGATOR, input_refs=json.dumps([driver]))
    reason = _investigator_unavailable()
    if reason:
        task.status, task.fallback = "blocked_configuration", reason
        inc.investigation_summary = f"{where}. Investigator unavailable: {reason}"
    else:
        msg = ctx.post(INVESTIGATOR, "investigate",
                       {"source": NAME, "data_side": "simulated", "reply_to": NAME, "incident_id": inc.id,
                        "run_id": inc.run_id, "reason": f"{inc.direction} deviation on {inc.detection_date}: {where}",
                        "targets": [[driver["label"], driver["id"]]], "driver": driver})
        task.status, task.input_refs = "in_progress", json.dumps([msg.msg_id, driver])
        inc.lifecycle_status = "in_progress"
        inc.investigation_summary = f"{where}. Sent to the investigator."
    db.add(task)
    _emit_task(db, own)
    _emit_task(db, task)
    _emit_incident(db, inc)
    db.commit()
    return f"{inc.id}: {driver['label']} {driver['id']} -> {INVESTIGATOR} ({task.status})"


def close(db, msg) -> str:
    from sqlalchemy import select
    from models import AgentTask, Incident, utcnow

    p = msg.payload
    inc = db.get(Incident, p.get("incident_id") or "")
    if inc is None:
        return f"report for unknown incident {p.get('incident_id')}"
    task = db.scalars(select(AgentTask).where(AgentTask.incident_id == inc.id, AgentTask.agent == msg.sender,
                                              AgentTask.status == "in_progress")).first()
    hyps = sorted(p.get("hypotheses") or [], key=lambda h: -(h.get("confidence") or 0))
    if hyps:
        top = hyps[0]
        outcome = "hypotheses_proposed"
        finding = (f"{len(hyps)} hypothesis(es); top ({top.get('confidence')}): {top.get('text')}")
    else:
        outcome = "inconclusive"
        why = p.get("error") or "; ".join((p.get("dropped") or [])[:2]) or "no hypothesis survived validation"
        finding = f"No supported hypothesis ({why})"
    inc.lifecycle_status, inc.evidence_outcome = "resolved", outcome
    inc.investigation_summary = ((inc.investigation_summary or "") + f" Investigator: {finding}").strip()
    if task is not None:
        task.status = "failed" if p.get("error") else "completed"
        task.evidence_outcome, task.ended_at = outcome, utcnow()
        task.output_refs = json.dumps([msg.msg_id] + [h.get("id") for h in hyps if h.get("id")])
        _emit_task(db, task)
    _emit_incident(db, inc)
    db.commit()
    return f"{inc.id}: resolved ({outcome})"


def run_dispatcher(ctx: RunContext) -> RunResult:
    from models import SessionLocal

    events = [e["payload"] for e in ctx.trigger.get("events", []) if e.get("name") == "sim.deviation_detected"]
    reports = [m for m in ctx.messages_of("report") if m.payload.get("incident_id")]
    if not events and not reports:
        return RunResult(OK, "nothing to dispatch")
    lines: List[str] = []
    with SessionLocal() as db:
        lines += [dispatch(ctx, db, ev) for ev in events]
        lines += [close(db, m) for m in reports]
    blocked = sum("blocked_configuration" in l for l in lines)
    return RunResult(DEGRADED if blocked else OK, "; ".join(lines)[:500],
                     counts={"dispatched": len(events), "closed": len(reports), "blocked": blocked})
