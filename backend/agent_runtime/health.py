"""Derive what the UI shows from status files. Pure functions: no threads, no I/O.

The API never trusts `state` alone: a killed process can't clean up after itself, so liveness
comes from heartbeat age.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from .bus import parse_iso

RUNNING, ACTIVE, DEGRADED, DOWN = "running", "active", "degraded", "down"
NEVER_RUN, NOT_CONFIGURED, DISABLED = "never_run", "not_configured", "disabled"

HEARTBEAT_STALE_S = 30.0      # running agents and the scheduler heartbeat every 5 s
WORKER_STALE_S = 10.0         # the Track A worker loop writes every ~2 s


def _age(ts: Optional[str], now: datetime) -> Optional[float]:
    dt = parse_iso(ts)
    return None if dt is None else (now - dt).total_seconds()


def scheduler_alive(sched: Optional[Dict[str, Any]], now: datetime) -> bool:
    if not sched or sched.get("stopped"):
        return False
    age = _age(sched.get("heartbeat_at"), now)
    return age is not None and age <= HEARTBEAT_STALE_S


def agent_health(status: Optional[Dict[str, Any]], scheduler_ok: bool, now: datetime) -> Dict[str, str]:
    """-> {"health": ..., "reason": ...} for one scheduled agent."""
    if status is None:
        return {"health": NEVER_RUN if scheduler_ok else DOWN,
                "reason": "no run yet" if scheduler_ok else "scheduler not running"}
    state = status.get("state")
    if state == "not_configured":
        return {"health": NOT_CONFIGURED, "reason": status.get("unavailable_reason") or "not configured"}
    if state == "disabled":
        return {"health": DISABLED, "reason": "disabled"}
    hb_age = _age(status.get("heartbeat_at"), now)
    if state == "running":
        if status.get("timed_out"):
            return {"health": DOWN, "reason": status.get("last_error") or "timed out"}
        if hb_age is not None and hb_age <= HEARTBEAT_STALE_S and scheduler_ok:
            return {"health": RUNNING, "reason": f"since {status.get('started_at')}"}
        return {"health": DOWN, "reason": "heartbeat lost during a run (process died?)"}
    if not scheduler_ok:
        return {"health": DOWN, "reason": "scheduler not running"}
    last = status.get("last_result")
    if last is None:
        return {"health": NEVER_RUN, "reason": "no run yet"}
    if last == "failed":
        return {"health": DOWN, "reason": status.get("last_error") or "last run failed"}
    interval = status.get("interval_s")
    ok_age = _age(status.get("last_ok_at"), now)
    if interval and (ok_age is None or ok_age > 2 * interval):
        return {"health": DOWN, "reason": f"no successful run in {2 * interval:g}s"}
    if last == "degraded" or status.get("dead_letters"):
        return {"health": DEGRADED,
                "reason": status.get("last_summary") or f"{status.get('dead_letters')} dead-lettered message(s)"}
    return {"health": ACTIVE, "reason": status.get("last_summary") or "ok"}


def worker_health(worker: Optional[Dict[str, Any]], now: datetime) -> Dict[str, str]:
    """Track A services run inside worker.py's loop: their state is that thread's state."""
    if worker is None:
        return {"health": NEVER_RUN, "reason": "worker has not started"}
    age = _age(worker.get("heartbeat_at"), now)
    if worker.get("stopped") or age is None or age > WORKER_STALE_S:
        return {"health": DOWN, "reason": "worker loop not running"}
    return {"health": RUNNING, "reason": f"worker loop alive (pid {worker.get('pid')})"}
