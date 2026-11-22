"""File-based agent bus: messages between agents, user-facing output, status, triggers and events.

Everything lives under one directory (CHEETAH_BUS_DIR, default <repo>/var/agent_bus), so agent
threads in the worker process, a cron-started agent, and the API process all share it.

    inbox/<agent>/<ts>_<msg_id>.json     pending messages for <agent>
    done/<agent>/...                      acked (history, UI feed)
    dead/<agent>/...                      failed MAX_ATTEMPTS times; never retried silently
    status/<agent>.json                   written only by the scheduler (heartbeats, last run)
    runs/<agent>/<ts>_<run_id>.json       one record per run
    triggers/<agent>/<ts>_<id>.json       "run now" requests (API button, scripts)
    events/<ts>_<id>.json                 named events ("clock.tick", "sim.day_closed", ...)
    outbox/<kind>/<name>.json             user-facing output: briefs, discussions, fixes, decisions
    feedback/<fix_id>.json                human accept/reject of a fix (written only by the API)
    state/<key>.json                      small shared state (e.g. the simulated clock)
    locks/                                flock files (scheduler singleton, per-agent run lock)

Every write is atomic (write a temp file in the same directory, then os.replace), so readers in
other threads or processes never see a half-written file. One writer per file kind.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
MAX_ATTEMPTS = 3
_SAFE = re.compile(r"[^A-Za-z0-9_.:-]+")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime] = None) -> str:
    return (dt or utcnow()).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def default_root() -> Path:
    return Path(os.getenv("CHEETAH_BUS_DIR") or REPO_ROOT / "var" / "agent_bus")


def safe_name(s: str) -> str:
    """File-name-safe version of an id (agent names, fix ids, cycle ids)."""
    out = _SAFE.sub("_", str(s)).strip("._")
    if not out:
        raise ValueError(f"empty name from {s!r}")
    return out[:120]


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(data, default=str, indent=1))
    os.replace(tmp, path)


def read_json(path: Path) -> Optional[Any]:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _files(d: Path) -> List[Path]:
    """Visible JSON files, oldest first (names start with a sortable timestamp)."""
    try:
        return sorted(p for p in d.iterdir() if p.suffix == ".json" and not p.name.startswith("."))
    except FileNotFoundError:
        return []


def _stamp() -> str:
    return utcnow().strftime("%Y%m%dT%H%M%S%fZ")


@dataclass
class Message:
    sender: str
    to: str
    type: str
    payload: Dict[str, Any] = field(default_factory=dict)
    cycle_id: Optional[str] = None
    ref_ids: List[str] = field(default_factory=list)
    msg_id: str = field(default_factory=lambda: "msg_" + uuid.uuid4().hex[:12])
    ts: str = field(default_factory=iso)
    attempts: int = 0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["from"] = d.pop("sender")
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Message":
        d = dict(d)
        d["sender"] = d.pop("from")
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class AgentBus:
    def __init__(self, root: Optional[Path] = None, agents: Iterable[str] = ()):
        self.root = Path(root) if root else default_root()
        self.agents = set(agents)   # recipients of a broadcast (to="*")

    def ensure(self) -> "AgentBus":
        self.root.mkdir(parents=True, exist_ok=True)
        return self

    @property
    def exists(self) -> bool:
        return self.root.is_dir()

    def path(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)

    # ------------------------------------------------------------------ messages
    def post(self, msg: Message) -> List[str]:
        """Deliver to one inbox, or to every known agent except the sender for to='*'."""
        targets = sorted(self.agents - {msg.sender}) if msg.to == "*" else [msg.to]
        for t in targets:
            write_json(self.path("inbox", safe_name(t), f"{_stamp()}_{msg.msg_id}.json"), msg.to_dict())
        return targets

    def pending(self, agent: str) -> List[Path]:
        return _files(self.path("inbox", safe_name(agent)))

    def drain(self, agent: str) -> List[Message]:
        """Read (not remove) the inbox. Call ack() after a successful run, fail() otherwise."""
        out, seen = [], set()
        for p in self.pending(agent):
            d = read_json(p)
            if d is None:
                continue
            m = Message.from_dict(d)
            if m.msg_id in seen:          # at-least-once delivery: dedupe by msg_id
                p.unlink(missing_ok=True)
                continue
            seen.add(m.msg_id)
            out.append(m)
        return out

    def _inbox_file(self, agent: str, msg_id: str) -> Optional[Path]:
        return next((p for p in self.pending(agent) if p.stem.endswith(msg_id)), None)

    def ack(self, agent: str, msgs: Iterable[Message]) -> None:
        for m in msgs:
            p = self._inbox_file(agent, m.msg_id)
            if p is not None:
                dst = self.path("done", safe_name(agent), p.name)
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.replace(p, dst)

    def fail(self, agent: str, msgs: Iterable[Message], error: str) -> List[str]:
        """Count a failed attempt; after MAX_ATTEMPTS the message goes to dead/. Returns dead ids."""
        dead = []
        for m in msgs:
            p = self._inbox_file(agent, m.msg_id)
            if p is None:
                continue
            d = read_json(p) or m.to_dict()
            d["attempts"] = int(d.get("attempts", 0)) + 1
            d["last_error"] = error[:500]
            if d["attempts"] >= MAX_ATTEMPTS:
                write_json(self.path("dead", safe_name(agent), p.name), d)
                p.unlink(missing_ok=True)
                dead.append(m.msg_id)
            else:
                write_json(p, d)
        return dead

    def dead_count(self, agent: str) -> int:
        return len(_files(self.path("dead", safe_name(agent))))

    def history(self, limit: int = 50, type: Optional[str] = None,
                agent: Optional[str] = None) -> List[Dict[str, Any]]:
        """Newest first: acked + pending + dead messages, each tagged with its delivery state."""
        rows = []
        for state in ("done", "inbox", "dead"):
            base = self.path(state)
            if not base.is_dir():
                continue
            for d in sorted(base.iterdir()):
                if agent and d.name != safe_name(agent):
                    continue
                for p in _files(d):
                    rows.append((p.name, state, p))
        rows.sort(key=lambda r: r[0], reverse=True)
        out = []
        for _, state, p in rows:
            m = read_json(p)
            if m is None or (type and m.get("type") != type):
                continue
            out.append({**m, "delivery": state})
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------------ triggers & events
    def post_trigger(self, agent: str, reason: str = "manual", by: str = "api",
                     payload: Optional[Dict[str, Any]] = None) -> str:
        tid = "trg_" + uuid.uuid4().hex[:10]
        write_json(self.path("triggers", safe_name(agent), f"{_stamp()}_{tid}.json"),
                   {"trigger_id": tid, "agent": agent, "reason": reason, "by": by,
                    "payload": payload or {}, "ts": iso()})
        return tid

    def take_triggers(self) -> List[Dict[str, Any]]:
        out = []
        base = self.path("triggers")
        if not base.is_dir():
            return out
        for d in sorted(base.iterdir()):
            for p in _files(d):
                t = read_json(p)
                p.unlink(missing_ok=True)
                if t:
                    out.append(t)
        return out

    def post_event(self, name: str, payload: Optional[Dict[str, Any]] = None, source: str = "api") -> str:
        eid = "evt_" + uuid.uuid4().hex[:10]
        write_json(self.path("events", f"{_stamp()}_{eid}.json"),
                   {"event_id": eid, "name": name, "payload": payload or {}, "source": source, "ts": iso()})
        return eid

    def take_events(self, keep: int = 200) -> List[Dict[str, Any]]:
        """Consume pending events; the last `keep` stay in events_done/ for the UI."""
        out = []
        done = self.path("events_done")
        for p in _files(self.path("events")):
            e = read_json(p)
            if e:
                done.mkdir(parents=True, exist_ok=True)
                os.replace(p, done / p.name)
                out.append(e)
            else:
                p.unlink(missing_ok=True)
        if out:
            for old in _files(done)[:-keep]:
                old.unlink(missing_ok=True)
        return out

    def recent_events(self, limit: int = 50) -> List[Dict[str, Any]]:
        files = (_files(self.path("events")) + _files(self.path("events_done")))
        files.sort(key=lambda p: p.name, reverse=True)
        return [e for e in (read_json(p) for p in files[:limit]) if e]

    # ------------------------------------------------------------------ status & runs
    def write_status(self, agent: str, data: Dict[str, Any]) -> None:
        write_json(self.path("status", f"{safe_name(agent)}.json"), data)

    def read_status(self, agent: str) -> Optional[Dict[str, Any]]:
        return read_json(self.path("status", f"{safe_name(agent)}.json"))

    def write_run(self, agent: str, record: Dict[str, Any], keep: int = 200) -> None:
        d = self.path("runs", safe_name(agent))
        write_json(d / f"{record['started_at'].replace(':', '').replace('.', '')}_{record['run_id']}.json", record)
        files = _files(d)
        for p in files[:-keep]:
            p.unlink(missing_ok=True)

    def read_runs(self, agent: str, limit: int = 20) -> List[Dict[str, Any]]:
        files = _files(self.path("runs", safe_name(agent)))[::-1][:limit]
        return [r for r in (read_json(p) for p in files) if r]

    # ------------------------------------------------------------------ outbox / feedback / state
    def write_outbox(self, kind: str, name: str, data: Dict[str, Any]) -> Path:
        p = self.path("outbox", safe_name(kind), f"{safe_name(name)}.json")
        write_json(p, data)
        return p

    def read_outbox(self, kind: str, limit: int = 50) -> List[Dict[str, Any]]:
        files = _files(self.path("outbox", safe_name(kind)))[::-1][:limit]
        return [r for r in (read_json(p) for p in files) if r]

    def get_outbox(self, kind: str, name: str) -> Optional[Dict[str, Any]]:
        return read_json(self.path("outbox", safe_name(kind), f"{safe_name(name)}.json"))

    def write_feedback(self, fix_id: str, data: Dict[str, Any]) -> None:
        write_json(self.path("feedback", f"{safe_name(fix_id)}.json"), data)

    def read_feedback(self, fix_id: str) -> Optional[Dict[str, Any]]:
        return read_json(self.path("feedback", f"{safe_name(fix_id)}.json"))

    def get_state(self, key: str) -> Optional[Dict[str, Any]]:
        return read_json(self.path("state", f"{safe_name(key)}.json"))

    def set_state(self, key: str, data: Dict[str, Any]) -> None:
        write_json(self.path("state", f"{safe_name(key)}.json"), data)


def publish_event(name: str, payload: Optional[Dict[str, Any]] = None, source: str = "backend") -> Optional[str]:
    """Fire-and-forget event from backend code (e.g. the simulation reached a day close).

    No-op when the bus directory doesn't exist (agents not set up). Never raises: a monitoring
    side channel must not break the simulation that calls it.
    """
    try:
        bus = AgentBus()
        if not bus.exists or os.getenv("CHEETAH_AGENTS", "on").lower() in ("off", "0", "false"):
            return None
        return bus.post_event(name, payload, source=source)
    except Exception:
        return None
