"""Tests for ingestion_mode='live_events': proof that events genuinely arrive in the database over
simulated time (not pre-loaded and merely revealed), that the daily close is computed by the
worker from what actually landed, and that pausing/resuming doesn't create a catch-up burst.

Tick-size notes: speed_seconds=30 means 30 wall-seconds == 1 simulated day, i.e. 2880 simulated
seconds per wall-second. Closing a day therefore needs >=30s of *cumulative* elapsed wall-time
since the run started (or since the day began) — there is no actual sleeping here, `advance_run_live`
is called directly with synthetic `now` timestamps, so a "32 second tick" completes instantly."""
import os, tempfile, sys, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
_tmp=tempfile.TemporaryDirectory()
os.environ['CHEETAH_SKIP_DOTENV']='1'  # never load real keys from .env in tests
os.environ['DATABASE_URL']='sqlite:///'+str(Path(_tmp.name)/'test_live.db')
import pandas as pd
from sqlalchemy import select
from fastapi.testclient import TestClient
from app import app
from models import SessionLocal, Run, Command, Incident, DailyAmount, SourceEvent, RunMetrics, initialize
initialize()
import core
from core import execute_command, advance_run_live

client=TestClient(app)

_FIXTURE_DIR=tempfile.TemporaryDirectory()

SPEED=30.0  # seconds of wall-clock time per simulated day, for every test run below


def _write_timeline(name, day2_amount):
    """A tiny 2-day timeline: day 1 has 3 installs totalling 300 (within a 270-330 reference
    band — no deviation); day 2 has 1 install of `day2_amount` (engineered to breach the same
    band when day2_amount is small, or stay within it when day2_amount is itself ~300)."""
    rows = [
        {"event_time_utc": datetime(2026,10,1,0,0,1,tzinfo=timezone.utc), "kind":"click","source_id":"c1","game_placement_id":"P1","country":"DE","amount":None,"commission":None,"payout":None,"margin":None,"event_day":"2026-10-01"},
        {"event_time_utc": datetime(2026,10,1,0,0,2,tzinfo=timezone.utc), "kind":"click","source_id":"c2","game_placement_id":"P1","country":"DE","amount":None,"commission":None,"payout":None,"margin":None,"event_day":"2026-10-01"},
        {"event_time_utc": datetime(2026,10,1,0,0,5,tzinfo=timezone.utc), "kind":"install","source_id":"i1","game_placement_id":"P1","country":"DE","amount":100.0,"commission":20.0,"payout":10.0,"margin":10.0,"event_day":"2026-10-01"},
        {"event_time_utc": datetime(2026,10,1,0,0,10,tzinfo=timezone.utc),"kind":"install","source_id":"i2","game_placement_id":"P1","country":"DE","amount":100.0,"commission":20.0,"payout":10.0,"margin":10.0,"event_day":"2026-10-01"},
        {"event_time_utc": datetime(2026,10,1,0,0,15,tzinfo=timezone.utc),"kind":"install","source_id":"i3","game_placement_id":"P2","country":"FR","amount":100.0,"commission":20.0,"payout":10.0,"margin":10.0,"event_day":"2026-10-01"},
        {"event_time_utc": datetime(2026,10,1,0,0,20,tzinfo=timezone.utc),"kind":"click","source_id":"c3","game_placement_id":"P1","country":"DE","amount":None,"commission":None,"payout":None,"margin":None,"event_day":"2026-10-01"},
        {"event_time_utc": datetime(2026,10,2,0,0,1,tzinfo=timezone.utc), "kind":"click","source_id":"c5","game_placement_id":"P1","country":"DE","amount":None,"commission":None,"payout":None,"margin":None,"event_day":"2026-10-02"},
        {"event_time_utc": datetime(2026,10,2,0,0,5,tzinfo=timezone.utc), "kind":"install","source_id":"i4","game_placement_id":"P1","country":"DE","amount":day2_amount,"commission":2.0,"payout":1.0,"margin":1.0,"event_day":"2026-10-02"},
        {"event_time_utc": datetime(2026,10,2,0,0,10,tzinfo=timezone.utc),"kind":"click","source_id":"c6","game_placement_id":"P1","country":"DE","amount":None,"commission":None,"payout":None,"margin":None,"event_day":"2026-10-02"},
    ]
    df = pd.DataFrame(rows)
    df["event_time_utc"] = pd.to_datetime(df["event_time_utc"], utc=True)
    path = Path(_FIXTURE_DIR.name) / f"{name}.parquet"
    df.to_parquet(path, index=False)
    return str(path)


def _push_references(dataset_id):
    refs = [
        {"id":"REF-"+uuid.uuid4().hex,"reference_day":"2026-10-01","issue_day":"2026-09-30","point":300,"lower":270,"upper":330,"model_version":"test"},
        {"id":"REF-"+uuid.uuid4().hex,"reference_day":"2026-10-02","issue_day":"2026-10-01","point":300,"lower":270,"upper":330,"model_version":"test"},
    ]
    assert client.post('/api/v1/forecasts/references',json={'dataset_id':dataset_id,'references':refs}).status_code==201


def _create_and_start(dataset_id, timeline_path, speed=SPEED):
    r=client.post('/api/v1/runs',json={'dataset_id':dataset_id,'ingestion_mode':'live_events','event_source_path':timeline_path})
    assert r.status_code==201
    run_id=r.json()['run_id']
    cmd=client.post('/api/v1/runs/'+run_id+'/commands',json={'command':'start'}).json()['command_id']
    with SessionLocal() as db:
        execute_command(db,db.get(Command,cmd))
        # Pin speed explicitly rather than relying on app.py's created-run default, which is a
        # production UX choice independent of this suite's hand-tuned tick-size arithmetic.
        run=db.get(Run,run_id)
        run.speed_seconds=speed
        db.commit()
    return run_id


def _event_count(db, dataset_id):
    return db.query(SourceEvent).filter(SourceEvent.dataset_id==dataset_id).count()


def test_source_events_grow_monotonically_and_are_not_preloaded():
    name='live-'+uuid.uuid4().hex[:8]
    timeline=_write_timeline(name, day2_amount=10.0)
    _push_references(name)
    run_id=_create_and_start(name, timeline)

    with SessionLocal() as db:
        run=db.get(Run,run_id)
        assert _event_count(db,name)==0, "no events should exist before the clock has advanced at all"

        t0=datetime(2026,1,1,tzinfo=timezone.utc)
        advance_run_live(db,run,t0)  # first tick after start only establishes the wall-clock reference
        assert _event_count(db,name)==0

        # Four small ticks (~5.76 simulated seconds each at SPEED=30) walk gradually through day
        # 1's ~20-second event window, one or two rows at a time.
        counts=[]
        t=t0
        for _ in range(4):
            t=t+timedelta(milliseconds=2)
            advance_run_live(db,run,t)
            counts.append(_event_count(db,name))
        assert counts==[3,4,5,6], counts
        assert counts==sorted(counts)

        # One larger tick (32s) closes day 1 and delivers all of day 2's rows too.
        t=t+timedelta(seconds=32)
        advance_run_live(db,run,t)
        final=_event_count(db,name)
        assert final==9
        assert final>counts[-1], "rows must keep accumulating across ticks, not appear all at once up front"


def test_daily_amount_only_appears_after_close_and_matches_delivered_events():
    name='live-'+uuid.uuid4().hex[:8]
    timeline=_write_timeline(name, day2_amount=10.0)
    _push_references(name)
    run_id=_create_and_start(name, timeline)

    with SessionLocal() as db:
        run=db.get(Run,run_id)
        t=datetime(2026,1,1,tzinfo=timezone.utc)
        advance_run_live(db,run,t)  # establish reference
        assert db.query(DailyAmount).filter(DailyAmount.dataset_id==name).count()==0

        t=t+timedelta(seconds=32)  # >=30s needed to cross day 1's full 24h boundary at SPEED=30
        advance_run_live(db,run,t)

        day1=db.scalar(select(DailyAmount).where(DailyAmount.dataset_id==name,DailyAmount.day=='2026-10-01'))
        assert day1 is not None
        assert day1.amount==300.0 and day1.commission==60.0 and day1.payout==30.0 and day1.margin==30.0
        assert day1.source_kind=='live_computed'
        assert db.query(DailyAmount).filter(DailyAmount.dataset_id==name,DailyAmount.day=='2026-10-02').count()==0, \
            "day 2 must not close until its own full window has elapsed, even though its (few) events are already delivered"


def test_deviation_on_live_computed_day_pauses_and_creates_incident():
    name='live-'+uuid.uuid4().hex[:8]
    timeline=_write_timeline(name, day2_amount=10.0)  # 10 is well below the 270-330 band
    _push_references(name)
    run_id=_create_and_start(name, timeline)

    with SessionLocal() as db:
        run=db.get(Run,run_id)
        t=datetime(2026,1,1,tzinfo=timezone.utc)
        advance_run_live(db,run,t)  # establish reference
        t=t+timedelta(seconds=65)  # >=60s needed to close both day 1 and day 2 at SPEED=30
        advance_run_live(db,run,t)

        run=db.get(Run,run_id)
        assert run.status=='paused' and run.paused_reason=='anomaly_detected'
        incidents=db.query(Incident).filter(Incident.run_id==run_id).all()
        assert len(incidents)==1 and incidents[0].direction=='negative' and incidents[0].detection_date=='2026-10-02'


def test_resume_does_not_deliver_a_backlog_burst():
    name='live-'+uuid.uuid4().hex[:8]
    timeline=_write_timeline(name, day2_amount=300.0)  # non-deviating: run keeps going so we can pause/resume manually
    _push_references(name)
    run_id=_create_and_start(name, timeline)

    with SessionLocal() as db:
        run=db.get(Run,run_id)
        t=datetime(2026,1,1,tzinfo=timezone.utc)
        advance_run_live(db,run,t)  # establish reference

        # A small tick that lands between two fixture events (8.64 simulated seconds — after c1,
        # c2, i1 at 1/2/5s, before i2 at 10s), leaving deliverable backlog at the moment of pause.
        t=t+timedelta(milliseconds=3)
        advance_run_live(db,run,t)
        count_before_pause=_event_count(db,name)
        assert count_before_pause==3, "sanity check: only c1/c2/i1 delivered so far, backlog remains"

    pause_cmd=client.post('/api/v1/runs/'+run_id+'/commands',json={'command':'pause'}).json()['command_id']
    with SessionLocal() as db:
        execute_command(db,db.get(Command,pause_cmd))

    with SessionLocal() as db:
        run=db.get(Run,run_id)
        # While paused, even a huge elapsed wall-time must not deliver anything.
        advance_run_live(db,run,t+timedelta(hours=5))
        assert _event_count(db,name)==count_before_pause

    resume_cmd=client.post('/api/v1/runs/'+run_id+'/commands',json={'command':'resume'}).json()['command_id']
    resume_wall=t+timedelta(hours=5)
    with SessionLocal() as db:
        execute_command(db,db.get(Command,resume_cmd))
        run=db.get(Run,run_id)
        assert run.last_tick_wall_utc is None, "resume must clear the tick reference so downtime isn't converted into simulated time"

        # First tick after resume only re-establishes the reference point — no delivery yet.
        advance_run_live(db,run,resume_wall)
        assert _event_count(db,name)==count_before_pause

        # A tick of the same small size as before resume must deliver a similarly small, bounded
        # number of new rows (i2, i3 — both land within the next ~8.64 simulated seconds), not
        # the whole remaining backlog (which also includes c3 and all of day 2).
        advance_run_live(db,run,resume_wall+timedelta(milliseconds=3))
        count_after=_event_count(db,name)
        assert count_after==5, count_after
        assert count_after>count_before_pause
        assert count_after<9, "must not dump the entire remaining backlog in the first post-resume tick"


def test_run_metrics_track_transactions_and_distinct_campaigns():
    name='live-'+uuid.uuid4().hex[:8]
    timeline=_write_timeline(name, day2_amount=300.0)
    _push_references(name)
    run_id=_create_and_start(name, timeline)

    with SessionLocal() as db:
        run=db.get(Run,run_id)
        t=datetime(2026,1,1,tzinfo=timezone.utc)
        advance_run_live(db,run,t)  # establish reference
        t=t+timedelta(seconds=65)  # closes both days, delivering every row
        advance_run_live(db,run,t)

    snap=client.get('/api/v1/runs/'+run_id+'/snapshot').json()
    assert snap['metrics']['transactions']==4  # i1,i2,i3,i4
    assert snap['metrics']['campaigns']==2     # P1, P2
    assert abs(snap['metrics']['amount']-600.0)<1e-6  # 300 (day 1) + 300 (day 2, non-deviating variant)


def test_cache_evicted_on_completion_and_stop():
    name='live-'+uuid.uuid4().hex[:8]
    timeline=_write_timeline(name, day2_amount=300.0)  # non-deviating, so it can run to completion
    _push_references(name)
    run_id=_create_and_start(name, timeline)

    with SessionLocal() as db:
        run=db.get(Run,run_id)
        t=datetime(2026,1,1,tzinfo=timezone.utc)
        advance_run_live(db,run,t)  # establish reference; also populates the timeline cache
        assert run_id in core._live_timelines
        t=t+timedelta(seconds=935)  # >=31*30s cumulative: enough to close every remaining day
        advance_run_live(db,run,t)
        run=db.get(Run,run_id)
        assert run.status=='completed'
    assert run_id not in core._live_timelines, "a completed run's timeline cache must be freed"

    name2='live-'+uuid.uuid4().hex[:8]
    timeline2=_write_timeline(name2, day2_amount=300.0)
    _push_references(name2)
    run_id2=_create_and_start(name2, timeline2)
    with SessionLocal() as db:
        run2=db.get(Run,run_id2)
        advance_run_live(db,run2,datetime(2026,1,1,tzinfo=timezone.utc))
        assert run_id2 in core._live_timelines

    stop_cmd=client.post('/api/v1/runs/'+run_id2+'/commands',json={'command':'stop'}).json()['command_id']
    with SessionLocal() as db:
        execute_command(db,db.get(Command,stop_cmd))
    assert run_id2 not in core._live_timelines, "a stopped run's timeline cache must be freed"


def test_cache_capacity_bound_and_eviction_is_safe():
    names, run_ids = [], []
    for _ in range(core.LIVE_TIMELINE_CACHE_CAPACITY + 2):
        name='live-'+uuid.uuid4().hex[:8]
        timeline=_write_timeline(name, day2_amount=300.0)
        _push_references(name)
        run_id=_create_and_start(name, timeline)
        names.append(name); run_ids.append(run_id)
        with SessionLocal() as db:
            run=db.get(Run,run_id)
            advance_run_live(db,run,datetime(2026,1,1,tzinfo=timezone.utc))  # populates the cache
        assert len(core._live_timelines)<=core.LIVE_TIMELINE_CACHE_CAPACITY

    assert run_ids[0] not in core._live_timelines, "oldest-touched run should have been LRU-evicted"
    assert run_ids[-1] in core._live_timelines, "most-recently-touched run should still be cached"

    # Touching an evicted-but-still-running run must transparently recover (via the same
    # COUNT-based path used for worker-restart recovery) and keep delivering correctly.
    with SessionLocal() as db:
        run=db.get(Run,run_ids[0])
        assert run.status=='running'
        before=db.query(SourceEvent).filter(SourceEvent.dataset_id==names[0]).count()
        t=run.last_tick_wall_utc+timedelta(seconds=32)  # enough to close day 1
        advance_run_live(db,run,t)
        after=db.query(SourceEvent).filter(SourceEvent.dataset_id==names[0]).count()
        assert after>before
        day1=db.scalar(select(DailyAmount).where(DailyAmount.dataset_id==names[0],DailyAmount.day=='2026-10-01'))
        assert day1 is not None and day1.amount==300.0
