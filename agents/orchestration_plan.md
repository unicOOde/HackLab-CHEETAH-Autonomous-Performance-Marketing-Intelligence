# Implementation Plan: Scheduled Agents, Agent Messaging & Agent Status UI

**Status:** built (2026-11-22): bus, scheduler, adapters, product manager (`product_manager_agent.py`),
graph engineer (`graph_engineer_agent.py`), endpoints and the agents panel. Not built: PM metric
discovery (§5.2 step 4 proposals of new metrics), the LLM verdict draft (the gates decide alone),
and the overlooker's free exploration inside the GE run. **Date:** 2026-11-22.
**Scope:** Track B agents (`agents/`) plus one backend endpoint and one frontend screen in
Track A (`backend/app.py`, `frontend/CHEETAH_UI_Prototype_v1.5.html`). This is the first place
the two tracks touch.

## 1. Target flow

```
            every 30 min (staggered)
 ┌──────────────┐  ┌──────────────┐
 │ outage_agent │  │pacing_watcher│ ── investigate requests ──┐
 └──────┬───────┘  └──────┬───────┘                           ▼
        │ report          │ report                  ┌──────────────────┐
        │                 │                         │ investigator     │
        ▼                 ▼                         └────────┬─────────┘
 ┌─────────────────────────────────────────────┐   report   │
 │ product_manager  (triage, business brief,    │◀───────────┘
 │ proposals: metric / graph change / config)   │
 └──────────────────────┬──────────────────────┘
                        │ proposals (+ refs to the source reports)
                        ▼
 ┌─────────────────────────────────────────────┐   consult (in-process review hooks)
 │ graph_engineer (onboarding + overlooker)     │──────────▶ outage / pacing / investigator
 │ hears out every agent, decides, writes graph │◀────────── stance: support | object | abstain
 └──────────────────────┬──────────────────────┘
                        │ decisions (broadcast)
                        ▼
                 all agents + UI
```

- Watchers (`outage`, `pacing`, `investigator`) **observe and report**. They keep writing their own
  agent nodes (`Outage`, `Signal`, `Hypothesis`) through `GraphStore`, as today.
- **Product Manager** is read-only on the graph. It turns reports into a business brief and
  structured proposals (`agents/product_manager.md`), and it moderates the **fix discussion** for
  every issue (section 5.5, `agents/fix_playbook.md`). Most discussions end with no fix, and that
  is the expected result.
- **Graph Engineer** is the only agent that changes graph *structure* (Metric nodes, `FORMULA` /
  `INFLUENCES`, edge weights and status, `ag_*` monitoring config). It absorbs today's
  `graph_overlooker.py` and adds metric onboarding (`agents/graph_engineer.md`).

## 2. Communication: a file-based message bus

Files are enough. There's one machine, low volume (a few messages per 30 minutes), and files are
easy to inspect while debugging and for the demo. Two adjustments to "just tmp files":

- **Not `/tmp`.** It's cleared on reboot, isn't shared between Docker containers, and the message
  history is part of the audit trail. Use `var/agent_bus/` in the repo (gitignored), overridable
  with `CHEETAH_BUS_DIR`, and mounted as a shared volume in Docker.
- **Behind a small interface** (`agent_bus.py`: `post`, `drain`, `ack`, `fail`, `history`), so
  the storage can move to a Postgres table later without touching the agents.

### Layout

```
var/agent_bus/
  inbox/<agent>/<ts>_<msg_id>.json     pending messages for <agent>
  done/<agent>/<ts>_<msg_id>.json      acked (kept for history and the UI feed)
  dead/<agent>/...                     failed 3 times, never retried silently
  status/<agent>.json                  heartbeat / last-run state (section 4)
  runs/<agent>/<run_id>.json           one record per run: timings, counts, error, summary
  locks/<agent>.lock                   flock, prevents overlapping runs
  outbox/briefs/<cycle_id>.json        PM: cycle brief                       ┐ user-facing output,
  outbox/discussions/<cycle_id>_<issue>.json  PM: fix-discussion transcript  │ read by the backend
  outbox/fixes/<fix_id>.json           PM: proposed fix (never rewritten)    │ (section 6)
  outbox/decisions/<ts>_<id>.json      GE: graph decisions + stances         ┘
  feedback/<fix_id>.json               BACKEND: human accept/reject of a fix
```

**One writer per file.** Agents never write `feedback/`, and the backend never writes anything
except `feedback/` and a `fix_feedback` message into `inbox/product_manager/`.

Atomic writes: write `*.tmp` in the same directory, then `os.replace()`. A reader never sees a
half-written file. An agent only acks its inbox messages **after** its run succeeded, so a crash
leaves them pending for the next run (at-least-once delivery). Receivers dedupe by `msg_id`.

### Message envelope

```json
{
  "msg_id": "msg_3f2a9c1e7b0d",
  "ts": "2026-11-22T10:30:04Z",
  "cycle_id": "2026-11-22T10:30",
  "from": "pacing_watcher",
  "to": "product_manager",
  "type": "report",
  "ref_ids": [],
  "payload": { }
}
```

| type | from → to | payload |
|---|---|---|
| `report` | outage / pacing / investigator → PM | findings: what changed (numbers + baseline), affected Ad/Country/Category, severity, state (new/persisting/resolved), graph `change_ids`, uncertainties |
| `investigate` | outage / pacing → investigator | `targets: [[label, id], …]`, reason |
| `proposal` | PM → GE | one of `metric` (product_manager.md §4 record), `graph_change` (edge status/weight), `monitoring_config` (`ag_*`), plus `rationale`, `evidence`, `ref_ids` = the source reports |
| `decision` | GE → PM + `*` | per proposal: `applied` / `deferred` / `rejected`, the stance of every consulted agent, the reviewer verdict, `change_ids` (undoable) |
| `fix_feedback` | backend → PM | `{fix_id, decision: accepted\|rejected, reason}` from the UI |

User-facing output (briefs, discussions, fixes, decisions, and `needs_build` items inside the
brief) isn't a message. It's written to `outbox/` (see the layout above), because the backend
reads it directly and nobody "consumes" it.

`to: "*"` means a copy goes into every agent's inbox. Agents reload the metric graph when they
get a `decision` that changed it.

## 3. Scheduling

### 3.1 One entrypoint per agent: `python -m agents.runner <agent>`

The runner wraps every agent the same way:

1. `flock` on `locks/<agent>.lock`, non-blocking. If a previous run is still going, write
   `skipped_overlap` to the run record and exit 0.
2. Status `running`, plus a heartbeat thread that touches `status/<agent>.json` every 30 s.
3. `drain()` its inbox and call the agent's adapter (section 5) with those messages.
4. `post()` the outgoing messages and `ack()` the inbox.
5. Status `ok` / `degraded` (it ran, but something was `unknown`, e.g. `LLMUnavailable`) /
   `failed` (exception, with the error kept). Runs are capped by a hard timeout of 25 min, so they
   can't overlap with the next slot.

Cron mode also sets `run_overlooker=False` on every agent, because the GE replaces the
"each agent starts the overlooker" behaviour. `run_agents.py` keeps working for one-shot manual
passes.

### 3.2 Staggered schedule (every 30 minutes)

```cron
# cron/cheetah.crontab  (paths relative to the repo; BUS dir via env)
0,30  * * * *  cd $REPO && .venv/bin/python -m agents.runner outage_agent
0,30  * * * *  cd $REPO && .venv/bin/python -m agents.runner pacing_watcher
5,35  * * * *  cd $REPO && .venv/bin/python -m agents.runner investigator
12,42 * * * *  cd $REPO && .venv/bin/python -m agents.runner product_manager
20,50 * * * *  cd $REPO && .venv/bin/python -m agents.runner graph_engineer
```

The offsets give a natural ordering, but correctness never depends on them: each agent just
consumes whatever is in its inbox. A late investigator only means the PM sees its report one
cycle later.

**Ways to trigger it:**
- **This machine:** `crontab` isn't installed (Arch/Omarchy), but systemd is. Use systemd *user*
  timers, generated from the same schedule by `cron/install_timers.sh`: one
  `cheetah-agent@<agent>.service` plus five `.timer` files with `OnCalendar=*:00,30` etc.
  Installing `cronie` and loading the crontab works equally well.
- **Docker:** a new `agents` service in `docker-compose.yml` that runs `supercronic
  cron/cheetah.crontab`, with the `agent_bus` volume shared with `api`.
- **Demo:** `CHEETAH_AGENT_INTERVAL=2m` switches to a fast schedule so a jury can watch a full
  cycle.

### 3.3 Clock: October 2026 (decided)

All agents run on one shared **simulated October 2026 clock**, never wall-clock time.
`var/agent_bus/state/sim_clock.json` holds the cursor. The runner advances it by one simulated
hour per cycle, and every agent in that cycle reads the same value. Its `cycle_id` is the
simulated hour (e.g. `2026-10-14T09`), so messages, briefs and the UI all speak in October time.

- **Start:** the first hour with a full 7-day baseline in the paid telemetry, `2026-10-08T06:00`.
- **End of data:** `paid_sim.py` currently covers 2026-10-01..14 (`DAYS = 14`). Generating it with
  `--days 31` covers the whole month. When the cursor passes the last telemetry hour, it wraps
  back to the start, and the runner posts a `clock_wrapped` message so the PM doesn't read the
  jump as an incident. The pacing watcher's open `Signal`s then resolve or reopen through its
  normal dedup.
- **Outage agent:** web search can only see the real present, so outages it finds are mapped
  onto the current simulated hour, and its reports carry `clock: "wall"` so the PM doesn't treat
  them as October facts.
- **Fast demo schedule:** the cursor still advances one hour per cycle, so a 2-minute schedule
  covers a simulated day in 48 minutes.

## 4. Agent status: heartbeats and derived health

`status/<agent>.json`, written only by the runner:

```json
{
  "agent": "pacing_watcher",
  "state": "running",
  "run_id": "run_8c1d",
  "started_at": "2026-11-22T10:30:00Z",
  "heartbeat_at": "2026-11-22T10:31:30Z",
  "finished_at": null,
  "last_ok_at": "2026-11-22T10:00:41Z",
  "last_result": "ok",
  "last_error": null,
  "interval_s": 1800,
  "next_run_at": "2026-11-22T11:00:00Z",
  "last_summary": "3 signals (1 new major: pacing exhausted 0003WS/DE)",
  "counts": {"in": 0, "out": 4, "graph_changes": 2, "uncertainties": 1}
}
```

The backend derives the health shown in the UI. It never trusts `state` alone, because a killed
process can't clean up after itself:

| UI health | Rule |
|---|---|
| **Running** | `state=running` and heartbeat younger than 90 s |
| **Active** | last run `ok`, finished within `2 × interval` |
| **Degraded** | last run `degraded` (ran, but LLM/search was unknown), or it has dead-lettered messages |
| **Down** | last run `failed`; **or** `state=running` with a heartbeat older than 90 s (crashed or killed); **or** no finished run within `2 × interval` (scheduler not firing) |
| **Never run** | status file missing, but the bus directory exists |
| **Not configured** | bus directory missing or unreadable. This is an explicit state, never fake data (CLAUDE.md non-fabrication rule) |

Heartbeat age, not PID checks, decides liveness: the API container can't see PIDs in the agents
container.

## 5. Agent changes

### 5.1 Adapters for the existing agents (thin, no internal rewrites)

| Agent | Input from inbox | Output |
|---|---|---|
| `outage_agent` | none (scans by itself) | `report` per flagged outage (`OutageImpact`), `investigate` for the affected Outage + its top ads (same logic as `run_agents.py` today) |
| `pacing_watcher` | `decision` (reload the metric map) | `report` per new/changed/resolved `Detection` (from `CycleReport`), `investigate` from `cycle.handoff()` |
| `investigator` | `investigate` messages (targets deduped across messages); with no requests, it idles (state `ok`, "no targets") | `report` per `Investigation`: hypotheses, quoted evidence, `Hypothesis` ids |

Each adapter is one function, `run(messages) -> list[Message]`, in `agents/adapters.py`. Numbers
in payloads are copied from the agents' own dataclasses, so they stay graph-sourced.

### 5.2 New: `agents/product_manager_agent.py`

1. **Collect** the reports in its inbox, grouped by issue key / target. Classify each as new,
   persisting or resolved using the `Signal` state and earlier briefs (dedup of repeated alerts:
   one of CLAUDE.md's "still missing" items).
2. **Deterministic pre-checks** (no LLM): sample size, cohort maturity, whether a `PlannedEvent`
   explains the move (innocent change).
3. **Fix discussion per issue** (section 5.5). This runs before the brief, so the brief can
   report each issue's outcome.
4. **One `claude_json` call**, with `product_manager.md` as instructions and the grouped reports
   plus discussion outcomes as context. Output:
   - a brief that follows CLAUDE.md's report contract, written to `outbox/briefs/`. Its "ranked
     recommended actions" are **only** the `fix_proposed` fixes; every other issue lists its
     outcome (`no_fix` + reason, `graph_only`, `monitor`);
   - `proposal`s. Examples: promote an `INFLUENCES` edge to `validated` when the investigator
     confirmed the mechanism; adjust `ag_min_sample` after repeated low-confidence alerts; propose
     a new metric (product_manager.md §4); `needs_build`.
5. **Validate** before posting: every number must appear in a source report (same rule as the
   investigator), every `ref_id` must exist, and metric proposals must pass the §6 checklist.
   Anything that fails is dropped and recorded as an uncertainty.
6. **Empty inbox → no LLM call.** The PM still writes a "quiet cycle" brief so the UI shows it ran.
7. **`fix_feedback` messages** from the inbox are stored as context: rejections feed gate 7 of the
   next discussions, and the human's reason is quoted to the model.

### 5.3 New: `agents/graph_engineer_agent.py` (absorbs `graph_overlooker.py`)

For each `proposal`:

1. **Hear out the other agents.** Build a dossier from:
   - (a) the **full** source reports in `ref_ids`, read from `done/`, so the PM's summary can't
     silently drop a dissenting detail;
   - (b) every other report from the last 2 cycles that touches the same nodes or metrics,
     including contradicting ones;
   - (c) **consultation:** call each affected agent's `review(proposal, dossier) -> {stance,
     reason, evidence}` hook in-process. These hooks are synchronous Python calls, not bus
     messages: a reply round-trip over a 30-minute bus would delay every decision by a cycle.
     - **pacing watcher** (deterministic): would the change break its metric map or the path to
       `'Revenue'`, and does its `Signal` history contradict the claim?
     - **investigator** (LLM): re-runs the evidence queries for the affected targets.
     - **outage agent** (deterministic): is an open `Outage` on the affected countries a better
       explanation?

   The same hook protocol is used in the PM's fix discussions (section 5.5), where the GE itself
   also gives a stance.
2. **Execute** using `graph_engineer.md`: metric onboarding (`add_metric` / `link_metrics`, still
   to be built in `GraphStore`), or the overlooker's edge/property changes.
3. **Independent reviewer call** (as in the overlooker today) sees the dossier, every stance and
   the re-run evidence.
4. **Decision rule:**

   | Condition | Outcome |
   |---|---|
   | Reviewer approves and no agent **objects with evidence** | `applied` |
   | Any evidenced objection | `deferred`, sent back to the PM with the objection |
   | Anything `unknown` | `deferred`. Unknown never acts |

   Every applied change carries its `change_id`, so `undo()` works.
5. Post a `decision` (to the PM and broadcast) that includes every stance. This is the "who said
   what" record the UI shows.

Also, on every run with no proposals, the GE runs the **re-link pass** (graph_engineer.md §6) and
the overlooker's own exploration, capped at one LLM loop per run.

### 5.4 Model tiers (decided: smaller model for smaller tasks)

`llm.claude_json(..., tier="small" | "large")` picks the model. `CLAUDE_MODEL_SMALL` defaults to
`claude-haiku-5-5` and `CLAUDE_MODEL_LARGE` to `claude-opus-5-5`. The existing `CLAUDE_MODEL`
stays as an override that forces one model for everything.

| Task | Tier | Why |
|---|---|---|
| Outage: web search + extract status JSON | small | extraction against a fixed schema; checked by country normalisation |
| PM: grouping/classification of reports, quiet-cycle brief | small | summarising numbers it is given |
| Investigator review hook (re-run evidence, give a stance), also in fix discussions | small | narrow yes/no on numbers already computed |
| Investigator: hypotheses for a target | large | open-ended reasoning over context |
| PM: brief with ranked actions + proposals | large | business judgement, output feeds graph changes |
| PM: fix-discussion verdict draft | large | the gates still decide; the draft must be good enough to pass them |
| GE: decision on a proposal, metric link discovery | large | writes to the graph |
| GE: independent reviewer | large | the last gate before a write; must not be weaker than what it reviews |

Rule: anything whose output can lead to a graph write is decided on the large tier. Small-tier
output only feeds into large-tier decisions or into reports.
**Fallback:** if the small model fails or returns unparsable JSON, retry once on large. If that
also fails, the result is `unknown`, as today.

### 5.5 Fix discussion ("fix council", inside the PM run)

The full rules, playbook and gates are in `agents/fix_playbook.md`. In short:

1. **Frame:** the PM states the issue: what changed (numbers + baseline), and the leading cause
   with its status (`supported` vs. `hypothesis`).
2. **Candidates:** 0–3 playbook entries whose trigger matches. No match → verdict `no_fix`.
3. **One expert round** through the `review(item, dossier) -> {stance, argument, evidence}` hooks:
   - **pacing** (deterministic): paid levers, budget/bid/ROAS numbers;
   - **investigator** (small tier): is the cause supported, and are the numbers right;
   - **outage** (deterministic): is an external cause the better explanation;
   - **graph engineer** (deterministic): is the cause → metric path `validated` or only a
     `hypothesis`, and which downstream metrics the lever touches (its side effects).
4. **Verdict:** the large model drafts it, then the **seven deterministic gates** decide. Exactly
   one outcome per issue:
   - `fix_proposed`;
   - `no_fix` + reason (`not_fixable_cause`, `innocent_change`, `data_issue`,
     `insufficient_evidence`, `expert_objection`, `already_resolving`, `previously_rejected`);
   - `graph_only`: a proposal goes to the GE;
   - `monitor`.

   **`no_fix` is the default.** The model can't bypass the gates: a draft `fix_proposed` that
   fails any gate is published as `no_fix` with the failing gate named.
5. **Publish:** every transcript (frame, candidates, stances, gate results, outcome) goes to
   `outbox/discussions/`, and every `fix_proposed` to `outbox/fixes/<fix_id>.json`, in the shape
   of `fix_playbook.md` §6. A discussion that ends in "no solution" is published just like a fix.

## 6. Backend and frontend

### 6.1 Endpoints (`backend/app.py` + a new `backend/agent_status.py`)

**Outward communication = the backend reads the bus files.** There are no new database tables.
The frontend never touches files: it only calls these endpoints. The backend reads
`status/`, `runs/`, `outbox/` and `done/`. Its **only** writes are `feedback/<fix_id>.json` plus a
`fix_feedback` message into `inbox/product_manager/`, using the same atomic write + rename as
the agents (`agent_bus.py`). Lists are sorted by filename (timestamp prefix), newest first, with a
limit. Fixes are served with `feedback/` merged in.

| Endpoint | Returns |
|---|---|
| `GET /api/v1/agents/status` | **replaces the current stub**: one entry per agent with derived health, timings, next run, last summary, counts, last error |
| `GET /api/v1/agents/{name}/runs?limit=20` | recent run records |
| `GET /api/v1/agents/messages?limit=50&type=` | message feed (envelope + short payload preview) from `done/` + `inbox/` |
| `GET /api/v1/agents/decisions?limit=20` | GE decisions with every agent's stance |
| `GET /api/v1/agents/briefs?limit=10` | cycle briefs (latest first) |
| `GET /api/v1/agents/discussions?outcome=&limit=30` | fix-discussion transcripts, filterable by outcome |
| `GET /api/v1/agents/fixes?status=proposed\|accepted\|rejected` | proposed fixes with any human feedback merged in |
| `POST /api/v1/agents/fixes/{fix_id}/feedback` | body `{decision: accepted\|rejected, reason}`. Writes `feedback/`, notifies the PM. `409` if feedback already exists, `404` for an unknown fix |

The env var `CHEETAH_BUS_DIR` defaults to `../var/agent_bus`, and the Docker `api` service mounts
the `agent_bus` volume read-write (it needs to write feedback). If the directory is missing, every endpoint returns
`{"status": "not_configured"}`.

**Track A services (decided: shown on the same Agents tab).** The current stub lists Forecast
Service, Deviation Detector, Investigator, Evidence Reviewer and Fix Action. These aren't separate
processes: they run inside Track A's `worker.py` loop. Their state is therefore **the state of the
worker thread they run in**:

- `worker.py` writes a heartbeat every loop iteration. It uses a one-row `WorkerHeartbeat` table
  (`worker_id, heartbeat_at, started_at, last_error`), so it works for both SQLite and Postgres
  and needs no shared volume.
- The service's health comes from the worker heartbeat, using the rules in section 4:
  heartbeat < 10 s → **Running**, older → **Down**, never written → **Never run**.
- A service whose integration isn't wired up (Forecast Service, Evidence Reviewer, Fix Action, and
  Track A's own Investigator, `AgentTask.status = 'blocked_configuration'`) shows **Not
  configured** regardless of the worker state. Showing it as Running would be fabricated.

In the API, every entry carries `track: "A" | "B"` and `source: "worker_thread" | "agent_bus"`.
The UI groups by track on the same tab.

### 6.2 Frontend: new "Agents" screen

`frontend/CHEETAH_UI_Prototype_v1.5.html`: add `<section id="screen-agents">` plus a nav entry,
following the existing `go('…')` screen pattern. It polls `/agents/status` every 5 s, and the feed
and decisions every 15 s. Polling is simpler here than the run-scoped SSE stream, and status is
not run-scoped.

```
┌ Agents ─────────────────────────────────────────────────────────────────┐
│ ● Outage          ACTIVE    last 10:30 (41s)  next 11:00  "no outages"   │
│ ◉ Pacing watcher  RUNNING   since 10:30:00 · heartbeat 4s ago            │
│ ● Investigator    DEGRADED  last 10:35  LLM unavailable → 2 unknown      │
│ ● Product manager ACTIVE    last 10:42  3 reports → 1 brief, 2 proposals │
│ ✕ Graph engineer  DOWN      last ok 09:50 · failed 10:20: Neo4j timeout  │
├ Flow (last cycle) ───────────────────────────────────────────────────────┤
│ outage ─┐                                                                │
│ pacing ─┼─▶ investigator ─▶ product manager ─▶ graph engineer           │
│ (counts on arrows, click → messages)                                     │
├ Decisions ───────────────────────────────────────────────────────────────┤
│ APPLIED  INFLUENCES Bid Multiplier→CPC: hypothesis→validated             │
│   pacing ✓ support · investigator ✓ support · outage – abstain · reviewer ✓│
│ DEFERRED new metric "Margin Rate": investigator ✗ "0 commission ads"     │
└──────────────────────────────────────────────────────────────────────────┘
```

Pills: Running (pulsing), Active (green), Degraded (amber), Down (red), Never run / Not
configured (grey). Clicking a card shows its last 20 runs and errors.

Below the status, the same tab has three more sections (polled every 15 s):

```
├ Latest brief (cycle 2026-10-14T09) ──────────────────────────────────────┤
│ What changed · impact · uncertainties · recommended actions (fixes only)  │
├ Discussions ─────────────────────────────────────────────────────────────┤
│ [FIX PROPOSED] pacing 0003WS/DE      P1 · all 7 gates ✓        ▸ transcript│
│ [NO FIX · innocent_change] promo 00008F/US  PlannedEvent ×1.7  ▸ transcript│
│ [NO FIX · data_issue] tracking 0000ur/DE  tracking 0.35        ▸ transcript│
│ [GRAPH ONLY] Data Freshness → Conversions: validate            ▸ transcript│
│ [MONITOR] auction 0003xV/GB  1 day only                        ▸ transcript│
├ Fixes ───────────────────────────────────────────────────────────────────┤
│ P1 Cap bid multiplier, restore pacing · 0003WS DE · simulated             │
│ Revenue +€low..high (rest of day) · side effects: spend −30..−45%         │
│ stances: pacing ✓ investigator ✓ outage – GE ✓        [Accept] [Reject…]  │
└──────────────────────────────────────────────────────────────────────────┘
```

An expanded transcript shows each agent's stance with its argument and evidence, plus the gate
checklist, so it's visible *why* there is or isn't a fix. "Reject" asks for a reason, which goes
back to the PM.

## 7. Phases

| # | Deliverable | Done when |
|---|---|---|
| 0 | Prerequisites: restore `freecash_graph.py` (only the `.pyc` is left; `paid_sim.py` imports it); decide on the venv name (`.venv` exists, CLAUDE.md says `.venv-agents`); `GraphStore.add_metric` / `link_metrics` (graph_engineer.md §5) | `paid_sim.py` runs; unit tests for the new store methods incl. `undo` |
| 1 | `agent_bus.py`, `agents/runner.py`, status files, shared October clock (`sim_clock.json`, wrap + `clock_wrapped`), `llm.py` tiers + fallback, crontab + systemd timer installer | pacing watcher runs every 30 min unattended. `kill -9` mid-run shows **Down** within 90 s, then recovers on the next slot; overlapping runs are skipped |
| 2 | Adapters for outage / pacing / investigator; overlooker auto-start off in cron mode | a cycle produces `report` + `investigate` messages; the investigator consumes the requests and reports back |
| 3 | Product manager agent | briefs follow the report contract; a quoted number not in the sources is dropped (test); empty inbox → no LLM call |
| 3b | Fix council: `fix_playbook.md` triggers + impact formulas, the 7 gates in code, review hooks for fix items, `outbox/` publishing, `fix_feedback` handling | table-driven gate tests per playbook entry (passing vs. failing evidence → expected outcome) with a stubbed LLM that always drafts `fix_proposed`, showing the gates override it; a rejected fix isn't re-proposed without new evidence |
| 4 | Graph engineer agent (onboarding + overlooker + review hooks + decision rule) | an evidenced objection defers the proposal (test); applied changes undo cleanly; decisions list every stance |
| 5 | Backend endpoints + Agents screen; `WorkerHeartbeat` in `worker.py` for Track A services | the UI shows all six health states using fixture status files (test), plus live during a run. Stopping `worker.py` turns the Track A services Down within 10 s; unconfigured services stay Not configured |
| 6 | Demo script | `paid_sim.py` with all four scenarios + a submitted "Margin Rate" proposal, on the fast schedule. With no manual steps, the UI shows: `pacing_bug` → **fix proposed** (P1); `planned_promo` → **no fix, innocent change**; `tracking_loss` → **no fix, data issue** + graph-only validation; `competitor_pullback` → **monitor**, then possibly P5; the metric onboarded by the GE. Accept/reject works from the UI |

**Tests:** bus atomicity and redelivery after a crash; health derivation (pure function, table
test); adapters against fixture dataclasses; PM and GE with a stubbed `claude_json`; endpoint
tests next to `tests/test_backend*.py`.

## 8. Decisions (2026-11-22)

1. **Clock:** a shared simulated October 2026 clock, advanced one hour per cycle (section 3.3).
2. **GE autonomy:** applies changes automatically. Every change is audited (`Change` node) and
   undoable. No human approval step.
3. **Track A services:** shown on the same Agents tab, with state taken from the worker thread
   they run in, and "Not configured" where the integration is missing (section 6.1).
4. **LLM cost:** two model tiers, small for extraction, summaries and review stances, large for
   anything that leads to a graph write (section 5.4). PM and GE skip the LLM on quiet cycles.
5. **Fixes:** discussed for every issue; `no_fix` is the default. A fix needs measured evidence
   (no simulation-based what-if) and must pass 7 deterministic gates. Recommendations only: a
   human accepts or rejects them in the UI, and the feedback goes back to the PM
   (`agents/fix_playbook.md`, section 5.5).
6. **Outward communication:** the backend reads the bus files (`outbox/`, `status/`, `runs/`) and
   serves REST to the frontend. Its only write is fix feedback. No notifications outside the
   frontend.
