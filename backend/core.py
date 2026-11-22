import json, uuid, calendar
from collections import OrderedDict
from datetime import datetime, timezone, timedelta
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from models import Run, Event, Command, DailyAmount, SourceEvent, ForecastReference, ForecastValue, Incident, Action, AgentTask, RunMetrics, RunCampaignSeen, utcnow
import forecasting

DAY_START='2026-10-01'
DAY_END='2026-10-31'
MONTH_DAYS=[f'2026-10-{i:02d}' for i in range(1,32)]
LIVE_TICK_ROW_CAP=20000
LIVE_TIMELINE_CACHE_CAPACITY=4  # bounds resident memory; a demo realistically has 1-2 concurrent live runs

def emit(db, run_id, event_type, payload, business_date=None):
    db.add(Event(run_id=run_id,event_type=event_type,payload=json.dumps(payload),business_date=business_date))

def serialize_run(run):
    return {'run_id':run.id,'status':run.status,'mode':run.mode,'speed_seconds':run.speed_seconds,'simulated_day':run.simulated_day,'simulation_date':MONTH_DAYS[run.simulated_day-1] if run.simulated_day else None,'dataset_id':run.dataset_id,'graph_version':run.graph_version,'paused_reason':run.paused_reason,'parent_run_id':run.parent_run_id,'replay_type':run.replay_type,'ingestion_mode':run.ingestion_mode,'forecast_provider':run.forecast_provider}

def reference_source(run):
    """tabpfn runs only ever evaluate against (and display) their own worker-issued references,
    never externally imported/naive ones that may share the same dataset_id."""
    return forecasting.SOURCE if run.forecast_provider=='tabpfn' else None

def check_31_days(db, dataset_id):
    if not dataset_id:return False
    days=set(db.scalars(select(DailyAmount.day).where(DailyAmount.dataset_id==dataset_id)).all())
    return set(MONTH_DAYS)==days

def forecast_for(db, dataset_id, day, source=None):
    if not dataset_id: return None
    stmt=select(ForecastReference).where(ForecastReference.dataset_id==dataset_id,ForecastReference.reference_day==day,ForecastReference.issue_day==prior(day))
    if source:stmt=stmt.where(ForecastReference.source==source)
    return db.scalars(stmt.order_by(ForecastReference.created_at.desc())).first()

def prior(day):
    d=datetime.fromisoformat(day).date()
    return (d-timedelta(days=1)).isoformat()

def _as_naive_utc(dt):
    """SQLite's DateTime(timezone=True) does not actually preserve tzinfo on read-back (it comes
    back naive) — so the live scheduler's clock fields (Run.sim_clock_utc/last_tick_wall_utc) are
    naive-but-implicitly-UTC in practice. Normalizing every external input the same way here (once,
    at the one entry point that takes a caller-supplied datetime) keeps all of advance_run_live's
    internal arithmetic consistently naive, instead of crashing on aware-vs-naive comparisons."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt

def close_day(db, run, day, amount):
    """Shared day-close logic: compare `amount` (anything with .amount/.commission/.payout/
    .margin/.source_kind) against the day's locked forecast reference, pause+incident on breach,
    report the day's 7-day outlook status, and advance run.simulated_day. Used by both the
    preloaded_daily run_step() (amount = a pre-existing DailyAmount row) and the live_events
    scheduler (amount = a DailyAmount row the worker just computed and is about to commit)."""
    metrics={'date':day,'amount':amount.amount,'commission':amount.commission,'payout':amount.payout,'margin':amount.margin,'source_kind':amount.source_kind,'finalized':True}
    emit(db,run.id,'revenue.daily_finalized',metrics,day)
    ref=forecast_for(db,run.dataset_id,day,reference_source(run))
    if not ref:
        emit(db,run.id,'forecast.reference_unavailable',{'date':day,'reason':'missing_prior_day_reference'},day)
    else:
        direction='negative' if amount.amount<ref.lower else ('positive' if amount.amount>ref.upper else None)
        emit(db,run.id,'forecast.evaluated',{'date':day,'actual_amount':amount.amount,'point':ref.point,'lower':ref.lower,'upper':ref.upper,'forecast_reference_id':ref.id,'classification':direction or 'within'},day)
        if direction:
            iid='INC-'+uuid.uuid4().hex[:10].upper()
            incident=Incident(id=iid,run_id=run.id,detection_date=day,direction=direction,actual_amount=amount.amount,lower=ref.lower,upper=ref.upper,forecast_reference_id=ref.id)
            db.add(incident)
            run.status='paused';run.paused_reason='anomaly_detected'
            emit(db,run.id,'deviation.detected',{'incident_id':iid,'scope':'aggregate','metric':'amount_total','direction':direction,'date':day,'actual_amount':amount.amount,'lower':ref.lower,'upper':ref.upper,'reference_forecast_id':ref.id},day)
            emit(db,run.id,'simulation.paused',{'reason':'anomaly_detected','incident_id':iid,'date':day,'checkpoint':'day_close'},day)
            # Actual LLM investigation is not fabricated without configured Claude/Neo4j/evidence inputs.
            db.add(AgentTask(id='TASK-'+uuid.uuid4().hex[:10],run_id=run.id,incident_id=iid,agent='Investigator',status='blocked_configuration',fallback='Claude/graph/evidence integrations required'))
            emit(db,run.id,'agent.task_updated',{'incident_id':iid,'agent':'Investigator','status':'blocked_configuration','fallback':'Inference and graph connections pending'},day)
    if run.forecast_provider!='tabpfn':  # tabpfn runs: forecast_after_close() issues and emits the outlook once this close is committed
        future=db.scalars(select(ForecastValue).where(ForecastValue.dataset_id==run.dataset_id,ForecastValue.issue_day==day).order_by(ForecastValue.target_day)).all()
        if len(future)==7:
            emit(db,run.id,'forecast.updated',{'issue_day':day,'horizon_days':7,'daily':[{'date':v.target_day,'point':v.point,'lower':v.lower,'upper':v.upper} for v in future],'source':future[0].source},day)
        else:
            emit(db,run.id,'forecast.unavailable',{'issue_day':day,'reason':'tabpfn_outlook_not_supplied'},day)
    run.simulated_day+=1
    if run.simulated_day==31 and run.status=='running':
        run.status='completed'
        _live_timelines.pop(run.id,None)  # no-op for preloaded_daily runs, which never populate this cache

def run_step(db, run):
    if run.status!='running':return
    if run.simulated_day>=31:
        run.status='completed';emit(db,run.id,'simulation.state_changed',serialize_run(run));db.commit();return
    day=MONTH_DAYS[run.simulated_day]
    amount=db.scalar(select(DailyAmount).where(DailyAmount.dataset_id==run.dataset_id,DailyAmount.day==day))
    if not amount:
        run.status='failed';run.paused_reason='missing_daily_revenue';emit(db,run.id,'simulation.state_changed',serialize_run(run),day);db.commit();return
    events=db.scalars(select(SourceEvent).where(SourceEvent.dataset_id==run.dataset_id,SourceEvent.event_day==day).order_by(SourceEvent.event_time_utc,SourceEvent.id)).all()
    if events:
        # Initial worker delivers compact event batches per simulated day; live_events runs stream per-event instead (see advance_run_live).
        payload=[{'kind':e.kind,'source_id':e.source_id,'event_time_utc':e.event_time_utc,'game_placement_id':e.game_placement_id,'country':e.country,'amount':e.amount,'commission':e.commission} for e in events]
        emit(db,run.id,'source.events_batch',{'date':day,'count':len(payload),'events':payload},day)
    close_day(db,run,day,amount)
    emit(db,run.id,'simulation.state_changed',serialize_run(run),day)
    db.commit()
    if run.forecast_provider=='tabpfn':forecast_after_close(db,run,day)

def forecast_after_close(db, run, day):
    """FC-01 hook: issue the D+1 forecast once day D's close is committed, so the (possibly slow)
    provider call never runs inside the day-close write transaction. Runs even when the close
    paused the run on an anomaly — nothing can change D's data between close and resume."""
    db.commit()
    forecasting.issue_forecast(db,run,day,emit)
    db.commit()

# ---- live_events scheduler ----
# Per-process, per-run cache of the staged event timeline (loaded once, scanned forward with a
# monotonic cursor) — see src/cheetah_synth/event_timeline.py for how the file is produced.
_live_timelines = OrderedDict()  # run_id -> state; LRU-bounded (see LIVE_TIMELINE_CACHE_CAPACITY) —
# eviction of a still-running run is safe, since _get_timeline's cache-miss path below already
# reconstructs next_idx/seen from the database (originally built for worker-restart recovery).

def _get_timeline(db, run):
    # numpy/pandas are only needed for live_events runs — imported locally so the unchanged
    # preloaded_daily path (and its tests) don't need these as hard dependencies.
    import pandas as pd
    state=_live_timelines.get(run.id)
    if state is not None:
        _live_timelines.move_to_end(run.id)
        return state
    if not run.event_source_path:raise ValueError('live_events run has no event_source_path')
    df=pd.read_parquet(run.event_source_path)
    # .to_numpy() on a tz-aware column keeps tz-aware Timestamp objects (pandas 2.x), not naive
    # datetime64[ns] — tz_convert(None) drops the tz label while preserving the UTC instant, giving
    # a real datetime64[ns] array comparable against the naive np.datetime64 values used below.
    times=df['event_time_utc'].dt.tz_convert(None).to_numpy()
    # Worker-restart / cache-eviction recovery: delivery is strictly append-only in timeline order
    # for a given dataset_id, so the count of already-inserted rows is exactly how far we'd progressed.
    next_idx=db.scalar(select(func.count()).select_from(SourceEvent).where(SourceEvent.dataset_id==run.dataset_id)) or 0
    next_idx=min(next_idx,len(df))
    seen=set(db.scalars(select(RunCampaignSeen.game_placement_id).where(RunCampaignSeen.run_id==run.id)).all())
    state={'df':df,'times':times,'next_idx':next_idx,'seen':seen}
    _live_timelines[run.id]=state
    _live_timelines.move_to_end(run.id)
    while len(_live_timelines)>LIVE_TIMELINE_CACHE_CAPACITY:
        _live_timelines.popitem(last=False)
    return state

def _deliver_rows(db, run, state, lo, hi):
    import pandas as pd
    df=state['df']
    chunk=df.iloc[lo:hi]
    has_meta='vertical' in df.columns  # backward-compat: older event_timeline.parquet files predate vertical/category
    rows=[{
        'dataset_id':run.dataset_id,'kind':r.kind,'source_id':r.source_id,
        'event_time_utc':r.event_time_utc.isoformat(),'event_day':r.event_day,
        'game_placement_id':r.game_placement_id,'country':r.country,
        'amount':None if pd.isna(r.amount) else float(r.amount),
        'commission':None if pd.isna(r.commission) else float(r.commission),
        'payout':None if pd.isna(r.payout) else float(r.payout),
        'margin':None if pd.isna(r.margin) else float(r.margin),
        'vertical':(None if pd.isna(r.vertical) else r.vertical) if has_meta else None,
        'category':(None if pd.isna(r.category) else r.category) if has_meta else None,
        'payload':'{}',
    } for r in chunk.itertuples()]
    db.bulk_insert_mappings(SourceEvent, rows)

    installs=chunk[chunk['kind']=='install']
    clicks=chunk[chunk['kind']=='click']
    new_campaigns=set(chunk['game_placement_id'].unique())-state['seen']

    metrics=db.get(RunMetrics,run.id)
    if metrics is None:
        # Column defaults (default=0.0/0) only apply at INSERT time, not on Python construction —
        # set them explicitly here since we mutate the object before any flush/commit.
        metrics=RunMetrics(run_id=run.id,total_amount=0.0,total_commission=0.0,total_payout=0.0,total_margin=0.0,total_transactions=0,distinct_campaigns=0)
        db.add(metrics)
    if len(installs):
        metrics.total_amount+=float(installs['amount'].sum())
        metrics.total_commission+=float(installs['commission'].sum())
        metrics.total_payout+=float(installs['payout'].sum())
        metrics.total_margin+=float(installs['margin'].sum())
        metrics.total_transactions+=len(installs)
    if new_campaigns:
        for gpid in new_campaigns:db.add(RunCampaignSeen(run_id=run.id,game_placement_id=gpid))
        state['seen']|=new_campaigns
        metrics.distinct_campaigns+=len(new_campaigns)
    metrics.updated_at=utcnow()

    # Outbox/SSE stays low-volume by design: full per-click fidelity lives in the database (above),
    # not in the human-watchable event stream. One batch event per tick, clicks capped, installs not
    # (installs are inherently rare — a few/day — so "full fidelity" there is essentially free).
    # `events` matches the exact row shape the preloaded_daily path's source.events_batch already
    # uses (kind/source_id/event_time_utc/game_placement_id/country/amount/commission) so the
    # frontend's single SSE handler (applyEvent -> p.events) works unmodified for both modes.
    sample_clicks=clicks.head(20)
    events=[{'kind':'click','source_id':r.source_id,'event_time_utc':r.event_time_utc.isoformat(),'game_placement_id':r.game_placement_id,'country':r.country,'amount':None,'commission':None} for r in sample_clicks.itertuples()]
    events+=[{'kind':'install','source_id':r.source_id,'event_time_utc':r.event_time_utc.isoformat(),'game_placement_id':r.game_placement_id,'country':r.country,'amount':None if pd.isna(r.amount) else float(r.amount),'commission':None if pd.isna(r.commission) else float(r.commission)} for r in installs.itertuples()]
    emit(db,run.id,'source.events_batch',{
        'date':chunk['event_day'].iloc[-1],
        'window_start':chunk['event_time_utc'].iloc[0].isoformat(),
        'window_end':chunk['event_time_utc'].iloc[-1].isoformat(),
        'click_count':len(clicks),'install_count':len(installs),
        'events':events,
    },chunk['event_day'].iloc[-1])

def _close_live_day(db, run, state, day):
    row=db.execute(select(
        func.coalesce(func.sum(SourceEvent.amount),0.0),
        func.coalesce(func.sum(SourceEvent.commission),0.0),
        func.coalesce(func.sum(SourceEvent.payout),0.0),
        func.coalesce(func.sum(SourceEvent.margin),0.0),
    ).where(SourceEvent.dataset_id==run.dataset_id,SourceEvent.kind=='install',SourceEvent.event_day==day)).one()
    amount=DailyAmount(dataset_id=run.dataset_id,day=day,amount=round(float(row[0]),2),commission=round(float(row[1]),2),payout=round(float(row[2]),2),margin=round(float(row[3]),2),source_kind='live_computed')
    db.add(amount)
    try:db.flush()
    except IntegrityError:
        db.rollback()
        amount=db.scalar(select(DailyAmount).where(DailyAmount.dataset_id==run.dataset_id,DailyAmount.day==day))
    close_day(db,run,day,amount)

def _advance_to(db, run, state, target, row_cap=LIVE_TICK_ROW_CAP):
    import numpy as np
    while run.status=='running' and run.simulated_day<31:
        day=MONTH_DAYS[run.simulated_day]
        day_end=datetime.fromisoformat(day)+timedelta(days=1)
        boundary=min(target,day_end)
        times=state['times']
        lo=state['next_idx']
        hi=int(np.searchsorted(times,np.datetime64(boundary.replace(tzinfo=None)),side='right'))
        capped=row_cap is not None and (hi-lo)>=row_cap
        if capped:hi=lo+row_cap
        if hi>lo:
            _deliver_rows(db,run,state,lo,hi)
            state['next_idx']=hi
        if capped:
            run.sim_clock_utc=_as_naive_utc(state['df']['event_time_utc'].iloc[hi-1].to_pydatetime())
            return
        run.sim_clock_utc=boundary
        if boundary<day_end:return
        _close_live_day(db,run,state,day)
        if run.forecast_provider=='tabpfn':
            forecast_after_close(db,run,day)
            # Inference wall-time must not become simulated time (same mechanism as resume/restart),
            # and day D+1 starts only on the next tick, after its reference is locked.
            run.last_tick_wall_utc=None
            return
        if run.status!='running':return
        if boundary>=target:return

def advance_run_live(db, run, now_wall):
    """Continuous-time counterpart to run_step() for ingestion_mode='live_events': converts
    elapsed wall-clock time to elapsed simulated time (via speed_seconds), delivers every staged
    event newly due since run.sim_clock_utc into SourceEvent in one bulk insert per tick, and
    calls close_day() (via _close_live_day) once a day's window fully closes — computing that
    day's DailyAmount from what was actually delivered, rather than reading a pre-supplied row."""
    if run.status!='running':return
    now_wall=_as_naive_utc(now_wall)
    state=_get_timeline(db,run)
    if run.last_tick_wall_utc is None:
        # First tick after start/resume/restart: establish the reference point without advancing —
        # otherwise elapsed time would include the paused/down duration as a catch-up burst.
        run.last_tick_wall_utc=now_wall
        db.commit();return
    elapsed_wall=(now_wall-run.last_tick_wall_utc).total_seconds()
    run.last_tick_wall_utc=now_wall
    if elapsed_wall>0:
        if run.sim_clock_utc is None:
            run.sim_clock_utc=datetime.fromisoformat(DAY_START)
        sim_seconds=elapsed_wall*(86400.0/run.speed_seconds)
        target=run.sim_clock_utc+timedelta(seconds=sim_seconds)
        _advance_to(db,run,state,target)
    emit(db,run.id,'simulation.state_changed',serialize_run(run))
    db.commit()

def _step_live_day(db, run):
    state=_get_timeline(db,run)
    if run.sim_clock_utc is None:
        run.sim_clock_utc=datetime.fromisoformat(DAY_START)
    day=MONTH_DAYS[run.simulated_day]
    day_end=datetime.fromisoformat(day)+timedelta(days=1)
    _advance_to(db,run,state,day_end,row_cap=None)

def execute_command(db,cmd):
    run=db.get(Run,cmd.run_id)
    if not run: raise ValueError('Run missing')
    payload=json.loads(cmd.payload)
    name=cmd.command
    live=run.ingestion_mode=='live_events'
    if name=='start':
        if live:
            if not run.event_source_path:raise ValueError('No event_source_path registered for this live run')
        elif not check_31_days(db,run.dataset_id): raise ValueError('October dataset must have exactly 31 daily records before playback')
        if run.status not in ('created','ready','paused'):raise ValueError('Cannot start from '+run.status)
        run.status='running';run.paused_reason=None
        if live:
            if run.sim_clock_utc is None:run.sim_clock_utc=datetime.fromisoformat(DAY_START)
            run.last_tick_wall_utc=None
    elif name=='pause':
        if run.status not in ('running','paused'): raise ValueError('Run is not running')
        run.status='paused';run.paused_reason='operator_pause'
    elif name=='resume':
        if run.status!='paused':raise ValueError('Run is not paused')
        run.status='running';run.paused_reason=None
        if live:run.last_tick_wall_utc=None  # paused duration is paused time, not a catch-up burst
    elif name=='set_speed':
        speed=float(payload.get('seconds_per_day',4))
        # Benchmarked against the real full-scale event_timeline.parquet (~3.73M rows): measured
        # steady-state insert ceiling ~129,000 events/sec; real peak-hour clustering only ~1.84x
        # the daily average (not the 2-4x originally assumed). 3s/day needs ~73,700 events/sec at
        # peak -> 1.75x headroom; below that the margin gets thin enough to visibly lag.
        floor=3.0 if live else 0.5
        if not (floor<=speed<=60):raise ValueError(f'Speed out of range ({floor}-60 seconds/day for this ingestion mode)')
        run.speed_seconds=speed
    elif name=='stop':
        run.status='stopped';run.paused_reason='operator_stop'
        _live_timelines.pop(run.id,None)
    elif name=='step':
        if run.status not in ('paused','ready'): raise ValueError('Step requires paused or ready')
        run.status='running'
        _step_live_day(db,run) if live else run_step(db,run)
        if run.status=='running':run.status='paused';run.paused_reason='manual_step'
    else:raise ValueError('Unknown command')
    emit(db,run.id,'simulation.state_changed',serialize_run(run))
    cmd.status='completed';cmd.completed_at=datetime.now(timezone.utc)
    db.commit()
