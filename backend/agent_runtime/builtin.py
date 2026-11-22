"""The default agent registry: the shared October clock, adapters for the existing Track B agents,
and the product manager and graph engineer.

Pipeline (agents/orchestration_plan.md):

    sim_clock ──clock.tick──▶ pacing_watcher ─┐            ┌─▶ product_manager ─▶ graph_engineer
                          └─▶ outage_agent  ──┼─ report ───┘
                                              └─ investigate ─▶ investigator ─ report ─▶ product_manager
    contributors (UI) ── metric_submission ─▶ graph_engineer ── decision ─▶ product_manager

The adapters are thin: they call the existing agents unchanged (with the overlooker auto-start
off, the graph engineer will own that) and translate their dataclasses into bus messages. Every
number in a message comes from the agent's own result objects, i.e. from the graph. The product
manager and graph engineer live in agents/product_manager_agent.py and agents/graph_engineer_agent.py.
"""
from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from .bus import REPO_ROOT, AgentBus
from .scheduler import DEGRADED, OK, SKIPPED, AgentSpec, RunContext, RunResult

PM, GE, INVESTIGATOR = "product_manager", "graph_engineer", "investigator"
CLOCK_FMT = "%Y-%m-%dT%H:%M"
DEFAULT_SIM_START = "2026-10-08T06:00"   # first hour with a full 7-day baseline in paid_sim.py
DEFAULT_SIM_END = "2026-10-14T23:00"     # paid_sim.py DAYS = 14; set CHEETAH_SIM_END for --days 31


# --------------------------------------------------------------------------- environment
def _prepare_imports() -> None:
    """Make the repo-root agent code importable and load <repo>/.env (backend runs from backend/)."""
    root = str(REPO_ROOT)
    if root not in sys.path:
        sys.path.append(root)
    from graph_store import _load_dotenv
    _load_dotenv(str(REPO_ROOT / ".env"))


def graph_unavailable() -> Optional[str]:
    try:
        _prepare_imports()
    except Exception as exc:
        return f"graph_store not importable: {exc}"
    if os.getenv("NEO4J_URI") and os.getenv("NEO4J_PASSWORD"):   # same condition as GraphStore
        try:
            from neo4j import GraphDatabase
        except ImportError:
            return "NEO4J_URI is set but the neo4j driver isn't installed"
        try:   # the scheduler re-checks this every 60 s, so a recovered Aura comes back by itself
            with GraphDatabase.driver(os.environ["NEO4J_URI"],
                                      auth=(os.getenv("NEO4J_USERNAME", "neo4j"), os.environ["NEO4J_PASSWORD"]),
                                      connection_timeout=10) as driver:
                driver.verify_connectivity()
        except Exception as exc:
            return f"Neo4j at {os.environ['NEO4J_URI']} not reachable: {type(exc).__name__}: {str(exc)[:200]}"
        return None
    if shutil.which("neo4j-cli"):
        return None
    return "no Neo4j connection: set NEO4J_URI/NEO4J_USERNAME/NEO4J_PASSWORD or install neo4j-cli"


def llm_unavailable() -> Optional[str]:
    try:
        _prepare_imports()
        import anthropic  # noqa: F401
    except Exception as exc:
        return f"anthropic SDK not importable: {exc}"
    if not os.getenv("ANTHROPIC_API_KEY"):
        return "ANTHROPIC_API_KEY not set"
    return None


def _all(*checks: Callable[[], Optional[str]], module: Optional[str] = None) -> Callable[[], Optional[str]]:
    def check() -> Optional[str]:
        for c in checks:
            reason = c()
            if reason:
                return reason
        if module:
            try:
                _prepare_imports()
                __import__(module)
            except Exception as exc:
                return f"{module} not importable: {type(exc).__name__}: {exc}"
        return None
    return check


# --------------------------------------------------------------------------- simulated clock
def clock_bounds() -> Tuple[datetime, datetime]:
    start = datetime.strptime(os.getenv("CHEETAH_SIM_START", DEFAULT_SIM_START), CLOCK_FMT)
    end = datetime.strptime(os.getenv("CHEETAH_SIM_END", DEFAULT_SIM_END), CLOCK_FMT)
    return start, end


def clock_now(bus: AgentBus) -> Optional[str]:
    return (bus.get_state("sim_clock") or {}).get("now")


def run_sim_clock(ctx: RunContext) -> RunResult:
    """Advance the shared October clock by one hour and announce it (clock.tick).

    A manual trigger with payload {"set": "2026-10-14T09:00"} jumps to that hour instead, so a
    demo can go straight to a scenario.
    """
    start, end = clock_bounds()
    state = ctx.bus.get_state("sim_clock")
    jump = next((p["set"] for p in ctx.trigger.get("payloads", []) if p.get("set")), None)
    wrapped = False
    if jump:
        now = datetime.strptime(jump, CLOCK_FMT)
        if not start <= now <= end:
            return RunResult("failed", error=f"{jump} is outside the simulated range {start:%Y-%m-%dT%H:%M}..{end:%Y-%m-%dT%H:%M}")
    elif state is None:
        now = start
    else:
        now = datetime.strptime(state["now"], CLOCK_FMT) + timedelta(hours=1)
        if now > end:
            now, wrapped = start, True
    now_s = now.strftime(CLOCK_FMT)
    ctx.bus.set_state("sim_clock", {"now": now_s, "start": start.strftime(CLOCK_FMT),
                                    "end": end.strftime(CLOCK_FMT),
                                    "wraps": (state or {}).get("wraps", 0) + int(wrapped)})
    ctx.cycle_id = now_s
    ctx.emit("clock.tick", {"now": now_s, "wrapped": wrapped, "jumped": bool(jump)})
    if wrapped:   # so the PM doesn't read the jump back to the start as an incident
        ctx.post(PM, "clock_wrapped", {"now": now_s, "end": end.strftime(CLOCK_FMT)})
    return RunResult(OK, f"simulated clock at {now_s}" + (" (wrapped to start)" if wrapped else "")
                     + (" (jumped)" if jump else ""))


def clock_reached(ts: str) -> Callable[[AgentBus], bool]:
    """Condition helper: true once the simulated clock is at or after `ts` (YYYY-MM-DDTHH:MM)."""
    def cond(bus: AgentBus) -> bool:
        now = clock_now(bus)
        return now is not None and now >= ts
    return cond


# --------------------------------------------------------------------------- adapters
def _now_for(ctx: RunContext) -> Optional[str]:
    for e in ctx.trigger.get("events", []):
        if e.get("name") == "clock.tick":
            return e["payload"].get("now")
    return clock_now(ctx.bus)


def run_pacing_watcher(ctx: RunContext) -> RunResult:
    _prepare_imports()
    from agents.pacing_watcher import PacingWatcher, _parse

    now = _now_for(ctx)
    if now is None:
        return RunResult(SKIPPED, "simulated clock not started yet (trigger sim_clock first)")
    ctx.cycle_id = now
    w = PacingWatcher()
    try:
        cycle = w.run(_parse(now))
    finally:
        w.store.close()
    src = {"source": "pacing_watcher", "data_side": "simulated", "sim_now": cycle.now}
    for sid, d in cycle.new:
        ctx.post(PM, "report", {**src, "change": "new", "signal_id": sid, **d.props(cycle.now)})
    for sid, d, before in cycle.changed:
        ctx.post(PM, "report", {**src, "change": "changed", "previous": before, "signal_id": sid,
                                **d.props(cycle.now)})
    for sid, key in cycle.resolved:
        ctx.post(PM, "report", {**src, "change": "resolved", "signal_id": sid, "issue_key": key})
    ctx.post(PM, "cycle_summary", {**src, "ongoing": cycle.ongoing, "explained": cycle.explained,
                                   "skipped": cycle.skipped, "uncertainties": cycle.uncertainties})
    for h in cycle.handoff():
        ctx.post(INVESTIGATOR, "investigate", {**src, "targets": [list(t) for t in h["targets"]],
                                               "reason": h["issue_key"], "signal_id": h["signal"],
                                               "trace_from": h["trace_from"],
                                               "path_to_revenue": h["path_to_revenue"]})
    counts = {"new": len(cycle.new), "changed": len(cycle.changed), "resolved": len(cycle.resolved),
              "ongoing": len(cycle.ongoing), "explained": len(cycle.explained)}
    summary = (f"{now}: {counts['new']} new, {counts['changed']} changed, {counts['resolved']} resolved, "
               f"{counts['ongoing']} ongoing, {counts['explained']} explained")
    return RunResult(OK, summary, counts=counts, uncertainties=cycle.uncertainties)


def run_outage_agent(ctx: RunContext) -> RunResult:
    _prepare_imports()
    from agents.outage_agent import OutageAgent

    agent = OutageAgent(run_overlooker=False)
    try:
        impacts = agent.run()
    finally:
        agent.store.close()
    src = {"source": "outage_agent", "data_side": "real", "clock": "wall", "sim_now": _now_for(ctx)}
    flagged = 0
    for i in impacts:
        top = sorted(i.ads.items(), key=lambda kv: -kv[1])[:3]
        ctx.post(PM, "report", {**src, "kind": "outage", "service": i.service, "severity": i.severity,
                                "confidence": i.confidence, "countries": i.countries, "flagged": i.flagged,
                                "outage_id": i.outage_id, "commission_at_risk": i.commission_at_risk,
                                "top_ads": [{"ad": a, "commission": c} for a, c in top]})
        if i.flagged and i.outage_id:
            flagged += 1
            ctx.post(INVESTIGATOR, "investigate", {**src, "reason": f"outage {i.service}",
                                                   "targets": [["Outage", i.outage_id]] + [["Ad", a] for a, _ in top]})
    unknown = any("unknown" in u.lower() for u in agent.uncertainties)
    return RunResult(DEGRADED if unknown else OK,
                     f"{len(impacts)} outage(s), {flagged} flagged" + (" · some services unknown" if unknown else ""),
                     counts={"outages": len(impacts), "flagged": flagged}, uncertainties=agent.uncertainties)


def run_investigator(ctx: RunContext) -> RunResult:
    requests = ctx.messages_of("investigate")
    targets: List[Tuple[str, str]] = []
    for m in requests:
        for t in m.payload.get("targets", []):
            if len(t) == 2 and tuple(t) not in targets:
                targets.append((str(t[0]), str(t[1])))
    if not targets:
        return RunResult(OK, "no investigation requests")
    _prepare_imports()
    from agents.investigator_agent import InvestigatorAgent

    inv = InvestigatorAgent(run_overlooker=False)
    ref_ids = [m.msg_id for m in requests]
    llm_down = errors = 0
    try:
        for label, node_id in targets:
            try:
                res = inv.investigate(label, node_id)
            except Exception as exc:   # one bad target must not fail the others
                errors += 1
                ctx.post(PM, "report", {"source": INVESTIGATOR, "target": [label, node_id],
                                        "error": f"{type(exc).__name__}: {exc}"}, ref_ids=ref_ids)
                continue
            hyps = []
            for hid in res.hypotheses:
                h = inv.store.get("Hypothesis", hid) or {}
                hyps.append({"id": hid, "text": h.get("text"), "confidence": h.get("confidence"),
                             "evidence": h.get("evidence"), "status": h.get("status")})
            llm_down += any("LLM unavailable" in d for d in res.dropped)
            ctx.post(PM, "report", {"source": INVESTIGATOR, "data_side": "real", "target": [label, node_id],
                                    "hypotheses": hyps, "graph_changes": res.updates, "dropped": res.dropped},
                     ref_ids=ref_ids)
    finally:
        inv.store.close()
    status = DEGRADED if (llm_down or errors) else OK
    return RunResult(status, f"{len(targets)} target(s) investigated"
                     + (f", LLM unavailable for {llm_down}" if llm_down else "")
                     + (f", {errors} failed" if errors else ""),
                     counts={"targets": len(targets), "llm_unavailable": llm_down, "errors": errors})


def run_product_manager(ctx: RunContext) -> RunResult:
    """Compass: fix discussions + brief over everything in its inbox (agents/product_manager_agent.py)."""
    if not ctx.messages:
        return RunResult(OK, "inbox empty")
    _prepare_imports()
    from agents.product_manager_agent import ProductManager

    state = ctx.bus.get_state("product_manager") or {}
    llm_off = llm_unavailable()
    pm = ProductManager(llm=llm_off is None, llm_off_reason=f"LLM unavailable: {llm_off}")
    try:
        res = pm.run([m.to_dict() for m in ctx.messages], state, clock_now(ctx.bus))
    finally:
        pm.store.close()
    for d in res.discussions:
        ctx.publish("discussions", d["discussion_id"], d)
    for f in res.fixes:
        ctx.publish("fixes", f["fix_id"], f)
    for p in res.proposals:
        ctx.post(GE, "proposal", {k: v for k, v in p.items() if k != "new"}, ref_ids=[p["discussion_id"]])
    brief = {**res.brief, "brief_id": "brief_" + res.discussions[0]["discussion_id"][5:] if res.discussions
             else "brief_" + ctx.run_id, "ref_ids": [m.msg_id for m in ctx.messages]}
    ctx.publish("briefs", brief["brief_id"], brief)
    ctx.set_state("product_manager", state)
    counts = {**brief["outcome_counts"], "proposals": len(res.proposals)}
    return RunResult(OK, brief["summary"], counts=counts, uncertainties=brief["uncertainties"][:10])


def run_graph_engineer(ctx: RunContext) -> RunResult:
    """Cartographer: decide proposals, onboard submitted metrics; re-link when idle."""
    _prepare_imports()
    from agents.graph_engineer_agent import GraphEngineer

    state = ctx.bus.get_state("graph_engineer") or {}
    rejected = set(state.get("relink_rejected", []))
    llm_off = llm_unavailable()
    ge = GraphEngineer(llm=llm_off is None, llm_off_reason=f"LLM unavailable: {llm_off}")
    decisions = []
    try:
        for m in ctx.messages:
            p = m.payload
            if m.type == "metric_submission" or (m.type == "proposal" and p.get("kind") == "metric"):
                decisions.append(ge.onboard(p))
            elif m.type == "proposal" and p.get("op") == "validate_influence":
                decisions.append(ge.validate_influence(p))
            elif m.type == "proposal":
                decisions.append({**ge._decision(p, str(p.get("kind")), str(p.get("op"))),
                                  "reason": f"unsupported proposal {p.get('kind')}/{p.get('op')}"})
        if not ctx.messages:
            decisions += ge.relink(rejected)
    finally:
        ge.store.close()
    for d in decisions:
        d["from_message"] = next((m.msg_id for m in ctx.messages if m.payload.get("proposal_id") == d["proposal_id"]
                                  or m.payload.get("id") == d["target"]), None)
        ctx.publish("decisions", d["decision_id"], d)
        ctx.post(PM, "decision", d, ref_ids=[d["from_message"]] if d["from_message"] else [])
    ctx.set_state("graph_engineer", {"relink_rejected": sorted(rejected)})
    counts: Dict[str, int] = {}
    for d in decisions:
        counts[d["outcome"]] = counts.get(d["outcome"], 0) + 1
    if not decisions:
        return RunResult(OK, "re-link pass: nothing new" if not ctx.messages else "no proposals")
    deferred = counts.get("deferred", 0)
    return RunResult(DEGRADED if deferred and any("unavailable" in d["reason"] for d in decisions) else OK,
                     ", ".join(f"{n} {o}" for o, n in sorted(counts.items())) + " · "
                     + "; ".join(f"{d['target']}: {d['outcome']}" for d in decisions[:3]),
                     counts=counts, uncertainties=[u for d in decisions for u in d["uncertainties"]][:10])


# --------------------------------------------------------------------------- registry
def default_specs(interval_s: Optional[float] = None) -> List[AgentSpec]:
    interval = float(interval_s or os.getenv("CHEETAH_AGENT_INTERVAL_S", 1800))
    return [
        AgentSpec("sim_clock", run_sim_clock, track="B", interval_s=interval, timeout_s=30,
                  display_name="Chronos", role="Simulation clock",
                  run_on_start=AgentBus().get_state("sim_clock") is None,   # fresh bus: start ticking now
                  description="Advances the shared simulated October 2026 clock one hour and announces clock.tick"),
        AgentSpec("pacing_watcher", run_pacing_watcher, track="B", on_events=("clock.tick",),
                  display_name="Pacer", role="Pacing & auction watcher",
                  available=_all(graph_unavailable, module="agents.pacing_watcher"),
                  description="Paid pacing & auction monitor (deterministic, simulated telemetry)"),
        AgentSpec("outage_agent", run_outage_agent, track="B", on_events=("clock.tick",),
                  display_name="Sentinel", role="Outage scout",
                  available=_all(graph_unavailable, llm_unavailable, module="agents.outage_agent"),
                  description="Web research: are services our ads depend on down right now?"),
        AgentSpec(INVESTIGATOR, run_investigator, track="B", on_inbox=True,
                  display_name="Sleuth", role="Investigator",
                  available=_all(graph_unavailable, llm_unavailable, module="agents.investigator_agent"),
                  description="Investigates flagged ads/outages and writes hypotheses with evidence"),
        AgentSpec(PM, run_product_manager, track="B", on_inbox=True,
                  display_name="Compass", role="Product manager",
                  available=_all(graph_unavailable, module="agents.product_manager_agent"),
                  description="Triage, business brief, fix discussions, proposals"),
        AgentSpec(GE, run_graph_engineer, track="B", on_inbox=True, interval_s=interval,
                  display_name="Cartographer", role="Graph engineer",
                  available=_all(graph_unavailable, module="agents.graph_engineer_agent"),
                  description="Hears out every agent, decides and writes graph structure"),
    ]


# Track A services run inside backend/worker.py's loop, not as scheduled agents. Their state is
# the worker thread's state; unwired integrations are reported as not configured, never faked.
TRACK_A_SERVICES: List[Dict[str, Any]] = [
    {"agent": "deviation_detector", "name": "Tripwire", "role": "Deviation detector", "runs_in_worker": True,
     "description": "Daily amount vs. the locked forecast interval (runs in the worker loop)"},
    {"agent": "forecast_service", "name": "Oracle", "role": "Forecast service", "runs_in_worker": False,
     "reason": "TabPFN-TS not configured (TABPFN_TOKEN + backend/requirements-forecast.txt)"},
    {"agent": "track_a_investigator", "name": "Inspector", "role": "Incident investigator", "runs_in_worker": False,
     "reason": "Claude/graph/evidence integrations required (AgentTask blocked_configuration)"},
    {"agent": "evidence_reviewer", "name": "Auditor", "role": "Evidence reviewer", "runs_in_worker": False,
     "reason": "not configured"},
    {"agent": "fix_action", "name": "Mechanic", "role": "Fix actions (sandbox)", "runs_in_worker": False,
     "reason": "sandbox fix actions not configured"},
]
