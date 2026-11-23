"""Agent runtime: scheduler threads + file bus + status, built into the backend.

    backend/worker.py   start_runtime()  -> the scheduler thread runs next to the simulation loop
    backend/app.py      agent_runtime.api -> read status/outbox, post triggers/events/feedback

Env:
    CHEETAH_AGENTS=off               don't start the scheduler (endpoints still answer)
    CHEETAH_BUS_DIR=...              bus directory (default <repo>/var/agent_bus)
    CHEETAH_AGENT_INTERVAL_S=1800    base interval of the clock (every 30 min)
    CHEETAH_AGENT_TIME_SCALE=1       e.g. 60 -> a 30-minute interval runs every 30 s (demo)
    CHEETAH_SIM_START / CHEETAH_SIM_END   simulated October range (YYYY-MM-DDTHH:MM)
"""
from __future__ import annotations

import os
from typing import Optional, Sequence

from .bus import AgentBus, Message, publish_event
from .scheduler import AgentSpec, RunContext, RunResult, Scheduler

__all__ = ["AgentBus", "AgentSpec", "Message", "RunContext", "RunResult", "Scheduler",
           "publish_event", "start_runtime", "agents_enabled"]


def agents_enabled() -> bool:
    return os.getenv("CHEETAH_AGENTS", "on").lower() not in ("off", "0", "false", "no")


def start_runtime(specs: Optional[Sequence[AgentSpec]] = None, bus: Optional[AgentBus] = None,
                  time_scale: Optional[float] = None) -> Optional[Scheduler]:
    """Start the scheduler thread. None if disabled or another process already runs it."""
    if not agents_enabled():
        return None
    if specs is None:
        from .builtin import default_specs
        specs = default_specs()
    scale = float(time_scale if time_scale is not None else os.getenv("CHEETAH_AGENT_TIME_SCALE", 1))
    sched = Scheduler(specs, bus=bus, time_scale=scale)
    if not sched.start():
        holder = f" (pid {sched.lock_holder_pid})" if sched.lock_holder_pid else ""
        print(f"agent scheduler: another process{holder} already schedules this bus; not starting", flush=True)
        return None
    print(f"agent scheduler {sched.id} started: {', '.join(sorted(sched.slots))} "
          f"(time scale {scale:g}, bus {sched.bus.root})", flush=True)
    return sched
