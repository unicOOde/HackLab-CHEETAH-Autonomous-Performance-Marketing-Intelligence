"""General agent scheduler: spawns one thread per agent run, on any combination of triggers.

An agent is an `AgentSpec`: a name plus a `run(ctx) -> RunResult` callable. When it runs is
declared on the spec, never hardcoded in the scheduler:

    interval_s     every N seconds (scaled by time_scale, so 1800 s can become 30 s in a demo)
    on_events      when a named event is published ("clock.tick", "sim.*"), see bus.post_event
    on_inbox       when messages are waiting in its inbox (debounced, so a burst runs it once)
    condition      when a predicate over the bus becomes true ("a certain point is reached")
    manual         always possible: bus.post_trigger(name) (API button, scripts, other agents)

Triggers that arrive while the agent is running are coalesced into exactly one follow-up run.

Communication contract for a run (see RunContext):
    * input:  ctx.messages = the agent's inbox, ctx.trigger = why it runs
    * output: ctx.post() messages to other agents, ctx.emit() events, ctx.publish() user-facing
      outbox files. All three are BUFFERED and only written when the run succeeds (ok/degraded);
      on failure nothing leaks out and the inbox is kept for the next attempt (at-least-once,
      dead-lettered after MAX_ATTEMPTS).

The scheduler is a process-wide singleton per bus directory (flock), so uvicorn reloads or a
second worker can't double-run agents. Liveness is written as heartbeats into status/ files,
which is what the API reads (it never talks to the threads directly).
"""
from __future__ import annotations

import fnmatch
import os
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None

from .bus import AgentBus, Message, iso, parse_iso, safe_name

OK, DEGRADED, FAILED, SKIPPED = "ok", "degraded", "failed", "skipped"
AVAILABILITY_RECHECK_S = 60.0


@dataclass
class RunResult:
    status: str = OK                       # ok | degraded | failed | skipped
    summary: str = ""
    counts: Dict[str, int] = field(default_factory=dict)
    uncertainties: List[str] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class AgentSpec:
    name: str
    run: Callable[["RunContext"], Optional[RunResult]]
    description: str = ""
    display_name: str = ""            # what the dashboard calls it (e.g. "Impala")
    role: str = ""                    # one-line job title (e.g. "Pacing & auction watcher")
    track: str = "B"
    interval_s: Optional[float] = None
    on_events: Sequence[str] = ()
    on_inbox: bool = False
    inbox_debounce_s: float = 3.0
    condition: Optional[Callable[[AgentBus], bool]] = None
    condition_poll_s: float = 5.0
    run_on_start: bool = False
    timeout_s: float = 1500.0
    available: Optional[Callable[[], Optional[str]]] = None   # returns a reason when NOT available
    enabled: bool = True

    def __post_init__(self):
        if not self.name or self.name.startswith("_") or safe_name(self.name) != self.name:
            raise ValueError(f"bad agent name {self.name!r}")

    def trigger_text(self, time_scale: float = 1.0) -> List[str]:
        out = []
        if self.interval_s:
            out.append(f"every {self.interval_s / time_scale:g}s")
        out += [f"on event {e}" for e in self.on_events]
        if self.on_inbox:
            out.append("on inbox messages")
        if self.condition:
            out.append("on condition")
        out.append("manual")
        return out


class RunContext:
    """What an agent run sees. Outputs are buffered until the run succeeds."""

    def __init__(self, bus: AgentBus, agent: str, run_id: str, trigger: Dict[str, Any],
                 messages: List[Message]):
        self.bus = bus
        self.agent = agent
        self.run_id = run_id
        self.trigger = trigger
        self.messages = messages
        self.cancelled = threading.Event()
        self.started_at = datetime.now(timezone.utc)
        self.cycle_id = (bus.get_state("sim_clock") or {}).get("now")
        self._posts: List[Message] = []
        self._events: List[Dict[str, Any]] = []
        self._outbox: List[tuple] = []
        self._state: Dict[str, Dict[str, Any]] = {}
        self.log: List[str] = []

    def post(self, to: str, type: str, payload: Optional[Dict[str, Any]] = None,
             ref_ids: Optional[List[str]] = None) -> Message:
        m = Message(sender=self.agent, to=to, type=type, payload=payload or {},
                    cycle_id=self.cycle_id, ref_ids=list(ref_ids or []))
        self._posts.append(m)
        return m

    def emit(self, name: str, payload: Optional[Dict[str, Any]] = None) -> None:
        self._events.append({"name": name, "payload": payload or {}})

    def publish(self, kind: str, name: str, data: Dict[str, Any]) -> None:
        self._outbox.append((kind, name, {**data, "agent": self.agent, "run_id": self.run_id,
                                          "cycle_id": self.cycle_id}))

    def set_state(self, key: str, data: Dict[str, Any]) -> None:
        """Agent memory between runs; written with the other outputs, only if the run succeeds."""
        self._state[key] = data

    def messages_of(self, type: str) -> List[Message]:
        return [m for m in self.messages if m.type == type]

    def _flush(self) -> Dict[str, int]:
        for m in self._posts:
            self.bus.post(m)
        for e in self._events:
            self.bus.post_event(e["name"], e["payload"], source=self.agent)
        for kind, name, data in self._outbox:
            self.bus.write_outbox(kind, name, data)
        for key, data in self._state.items():
            self.bus.set_state(key, data)
        return {"out": len(self._posts), "events": len(self._events), "published": len(self._outbox)}


@dataclass
class _Slot:
    spec: AgentSpec
    next_due: Optional[float] = None
    last_cond_check: float = 0.0
    pending: List[Dict[str, Any]] = field(default_factory=list)
    thread: Optional[threading.Thread] = None
    ctx: Optional[RunContext] = None
    started_mono: float = 0.0
    timed_out: bool = False
    unavailable: Optional[str] = None
    availability_checked: float = -1e18


class Scheduler:
    def __init__(self, specs: Sequence[AgentSpec], bus: Optional[AgentBus] = None,
                 time_scale: float = 1.0, tick_s: float = 1.0, heartbeat_s: float = 5.0,
                 clock: Callable[[], float] = time.monotonic):
        names = [s.name for s in specs]
        if len(set(names)) != len(names):
            raise ValueError("duplicate agent names")
        self.bus = (bus or AgentBus()).ensure()
        self.bus.agents |= set(names)
        self.time_scale = max(float(time_scale), 1e-6)
        self.tick_s = tick_s
        self.heartbeat_s = heartbeat_s
        self.clock = clock
        self.id = "sched_" + uuid.uuid4().hex[:8]
        self.slots: Dict[str, _Slot] = {s.name: _Slot(s) for s in specs}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._loop: Optional[threading.Thread] = None
        self._lock_fh = None
        self._last_heartbeat = -1e18
        self.started_at = iso()
        now = self.clock()
        for slot in self.slots.values():
            self._init_due(slot, now)

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> bool:
        """Start the scheduling loop thread. False if another process already schedules this bus."""
        if fcntl is not None:
            path = self.bus.path("locks", "scheduler.lock")
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(path, "w")
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                fh.close()
                return False
            self._lock_fh = fh
        self._stop.clear()
        self._loop = threading.Thread(target=self._run_loop, name="agent-scheduler", daemon=True)
        self._loop.start()
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._loop:
            self._loop.join(timeout)
        self.wait_idle(timeout)
        if self._lock_fh:
            self._lock_fh.close()
            self._lock_fh = None
        self._write_scheduler_status(stopped=True)

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:   # the loop must survive anything a tick hits
                traceback.print_exc()
            self._stop.wait(self.tick_s)

    def wait_idle(self, timeout: float = 10.0) -> bool:
        end = time.monotonic() + timeout
        for slot in list(self.slots.values()):
            t = slot.thread
            if t is not None:
                t.join(max(0.0, end - time.monotonic()))
        return all(s.thread is None or not s.thread.is_alive() for s in self.slots.values())

    # ------------------------------------------------------------------ external API
    def trigger(self, name: str, reason: str = "manual", payload: Optional[Dict[str, Any]] = None) -> str:
        if name not in self.slots:
            raise KeyError(name)
        return self.bus.post_trigger(name, reason, by="in_process", payload=payload)

    def emit_event(self, name: str, payload: Optional[Dict[str, Any]] = None) -> str:
        return self.bus.post_event(name, payload, source="in_process")

    def running(self) -> List[str]:
        with self._lock:
            return [n for n, s in self.slots.items() if s.thread is not None and s.thread.is_alive()]

    # ------------------------------------------------------------------ one evaluation
    def tick(self) -> None:
        now = self.clock()
        with self._lock:
            for t in self.bus.take_triggers():
                slot = self.slots.get(t.get("agent"))
                if slot is not None:
                    slot.pending.append({"kind": "manual", "reason": t.get("reason"), "by": t.get("by"),
                                         "trigger_id": t.get("trigger_id"), "payload": t.get("payload")})
            for e in self.bus.take_events():
                for slot in self.slots.values():
                    if any(fnmatch.fnmatchcase(e["name"], pat) for pat in slot.spec.on_events):
                        slot.pending.append({"kind": "event", "event": e})
            for slot in self.slots.values():
                if self._availability(slot, now) is not None:
                    slot.pending.clear()   # unavailable: triggers are dropped, status says why
                    continue
                self._collect(slot, now)
                self._check_timeout(slot, now)
                busy = slot.thread is not None and slot.thread.is_alive()
                if slot.pending and not busy:
                    self._start_run(slot, now)
        if now - self._last_heartbeat >= self.heartbeat_s:
            self._heartbeat()
            self._last_heartbeat = now

    def _init_due(self, slot: _Slot, now: float) -> None:
        spec = slot.spec
        if not spec.interval_s:
            return
        interval = spec.interval_s / self.time_scale
        prev = self.bus.read_status(spec.name) or {}
        last = parse_iso(prev.get("last_started_at"))
        if spec.run_on_start:
            slot.next_due = now
        elif last is not None:
            elapsed = (datetime.now(timezone.utc) - last).total_seconds()
            slot.next_due = now + max(0.0, interval - elapsed)
        else:
            slot.next_due = now + interval

    def _collect(self, slot: _Slot, now: float) -> None:
        spec = slot.spec
        if spec.interval_s and slot.next_due is not None and now >= slot.next_due:
            slot.pending.append({"kind": "interval"})
            slot.next_due = now + spec.interval_s / self.time_scale
        busy = slot.thread is not None and slot.thread.is_alive()
        if spec.on_inbox and not busy and not slot.pending:
            files = self.bus.pending(spec.name)
            if files:
                newest = max(p.stat().st_mtime for p in files if p.exists())
                if time.time() - newest >= spec.inbox_debounce_s:
                    slot.pending.append({"kind": "inbox", "messages": len(files)})
        if spec.condition and now - slot.last_cond_check >= spec.condition_poll_s:
            slot.last_cond_check = now
            try:
                if spec.condition(self.bus):
                    slot.pending.append({"kind": "condition"})
            except Exception as exc:
                slot.pending.append({"kind": "condition_error", "error": str(exc)})

    def _availability(self, slot: _Slot, now: float) -> Optional[str]:
        spec = slot.spec
        if not spec.enabled:
            return "disabled"
        if spec.available is None:
            return None
        if now - slot.availability_checked >= AVAILABILITY_RECHECK_S:
            slot.availability_checked = now
            try:
                slot.unavailable = spec.available()
            except Exception as exc:
                slot.unavailable = f"availability check failed: {exc}"
        return slot.unavailable

    def _check_timeout(self, slot: _Slot, now: float) -> None:
        if slot.thread is not None and slot.thread.is_alive() and not slot.timed_out \
                and now - slot.started_mono > slot.spec.timeout_s:
            slot.timed_out = True
            if slot.ctx is not None:
                slot.ctx.cancelled.set()   # cooperative: Python threads can't be killed
            self._status(slot.spec, timed_out=True,
                         last_error=f"timed out after {slot.spec.timeout_s:g}s (still running)")

    # ------------------------------------------------------------------ runs
    def _merge_trigger(self, pending: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {"kinds": sorted({p["kind"] for p in pending}),
                "reasons": [p["reason"] for p in pending if p.get("reason")],
                "events": [p["event"] for p in pending if p.get("event")],
                "trigger_ids": [p["trigger_id"] for p in pending if p.get("trigger_id")],
                "payloads": [p["payload"] for p in pending if p.get("payload")]}

    def _start_run(self, slot: _Slot, now: float) -> None:
        spec, pending = slot.spec, slot.pending
        slot.pending = []
        reason = self._availability(slot, now)
        if reason is not None:
            # never run (and never fake) an agent whose integration is missing
            self._status(spec, state="disabled" if reason == "disabled" else "not_configured",
                         unavailable_reason=reason, last_trigger=self._merge_trigger(pending))
            return
        run_id = "run_" + uuid.uuid4().hex[:10]
        ctx = RunContext(self.bus, spec.name, run_id, self._merge_trigger(pending), [])
        slot.ctx, slot.started_mono, slot.timed_out = ctx, now, False
        slot.thread = threading.Thread(target=self._execute, args=(slot, ctx),
                                       name=f"agent-{spec.name}-{run_id}", daemon=True)
        self._status(spec, state="running", run_id=run_id, started_at=iso(), last_started_at=iso(),
                     heartbeat_at=iso(), finished_at=None, timed_out=False, unavailable_reason=None,
                     last_trigger=ctx.trigger)
        slot.thread.start()

    def _execute(self, slot: _Slot, ctx: RunContext) -> None:
        spec = slot.spec
        started = iso(ctx.started_at)
        lock_fh = None
        result: RunResult
        flushed: Dict[str, int] = {}
        try:
            lock_fh = self._agent_lock(spec.name)
            if lock_fh is False:
                result = RunResult(SKIPPED, "another process is running this agent")
            else:
                ctx.messages = self.bus.drain(spec.name)
                try:
                    result = spec.run(ctx) or RunResult()
                except Exception as exc:
                    result = RunResult(FAILED, error=f"{type(exc).__name__}: {exc}")
                    ctx.log.append(traceback.format_exc()[-4000:])
                if slot.timed_out and result.status != FAILED:
                    result = RunResult(FAILED, result.summary,
                                       error=f"timed out after {spec.timeout_s:g}s; results discarded")
                if result.status in (OK, DEGRADED):
                    flushed = ctx._flush()
                    self.bus.ack(spec.name, ctx.messages)
                elif result.status == FAILED:
                    dead = self.bus.fail(spec.name, ctx.messages, result.error or "failed")
                    if dead:
                        result.uncertainties.append(f"dead-lettered {len(dead)} message(s)")
        except Exception as exc:   # bus/IO failure outside the agent
            result = RunResult(FAILED, error=f"runtime: {type(exc).__name__}: {exc}")
        finally:
            if lock_fh:
                lock_fh.close()
        finished = iso()
        counts = {"in": len(ctx.messages), **flushed, **result.counts}
        record = {"run_id": ctx.run_id, "agent": spec.name, "started_at": started, "finished_at": finished,
                  "duration_s": round((datetime.now(timezone.utc) - ctx.started_at).total_seconds(), 3),
                  "result": result.status, "summary": result.summary, "error": result.error,
                  "counts": counts, "uncertainties": result.uncertainties, "trigger": ctx.trigger,
                  "cycle_id": ctx.cycle_id, "log": ctx.log[-20:]}
        self.bus.write_run(spec.name, record)
        upd: Dict[str, Any] = {"state": "idle", "finished_at": finished, "heartbeat_at": finished,
                               "last_result": result.status, "last_summary": result.summary,
                               "counts": counts, "run_id": ctx.run_id}
        if result.status in (OK, DEGRADED):
            upd["last_ok_at"] = finished
            upd["last_error"] = None
        elif result.status == FAILED:
            upd["last_error"] = result.error
        with self._lock:
            self._status(spec, **upd)

    def _agent_lock(self, name: str):
        """Cross-process run lock (e.g. a cron-started copy of the same agent). False when busy."""
        if fcntl is None:
            return None
        path = self.bus.path("locks", f"agent_{name}.lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "w")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except OSError:
            fh.close()
            return False

    # ------------------------------------------------------------------ status files
    def _status(self, spec: AgentSpec, **updates: Any) -> None:
        cur = self.bus.read_status(spec.name) or {}
        slot = self.slots[spec.name]
        interval = spec.interval_s / self.time_scale if spec.interval_s else None
        next_run = None
        if interval and slot.next_due is not None:
            next_run = iso(datetime.fromtimestamp(
                time.time() + max(0.0, slot.next_due - self.clock()), timezone.utc))
        base = {"agent": spec.name, "display_name": spec.display_name or spec.name, "role": spec.role,
                "track": spec.track, "description": spec.description,
                "interval_s": interval, "triggers": spec.trigger_text(self.time_scale),
                "timeout_s": spec.timeout_s, "next_run_at": next_run, "scheduler_id": self.id,
                "pending": len(slot.pending), "dead_letters": self.bus.dead_count(spec.name)}
        cur.update(base)
        cur.update(updates)
        cur.setdefault("state", "idle")
        self.bus.write_status(spec.name, cur)

    def _heartbeat(self) -> None:
        with self._lock:
            for slot in self.slots.values():
                running = slot.thread is not None and slot.thread.is_alive()
                if running:
                    self._status(slot.spec, heartbeat_at=iso())
                else:
                    cur = self.bus.read_status(slot.spec.name) or {}
                    reason = self._availability(slot, self.clock())
                    state = "idle" if reason is None else ("disabled" if reason == "disabled"
                                                           else "not_configured")
                    extra: Dict[str, Any] = {}
                    if cur.get("state") == "running":   # a run that died without finishing
                        extra = {"last_result": FAILED, "last_error": "run ended without a result"}
                    self._status(slot.spec, state=state, unavailable_reason=reason, **extra)
        self._write_scheduler_status()

    def _write_scheduler_status(self, stopped: bool = False) -> None:
        self.bus.write_status("_scheduler", {
            "scheduler_id": self.id, "pid": os.getpid(), "started_at": self.started_at,
            "heartbeat_at": iso(), "stopped": stopped, "time_scale": self.time_scale,
            "agents": sorted(self.slots), "running": self.running()})
