import env  # noqa: F401 — loads the repo-root .env before anything reads os.environ
import asyncio, csv, io, json, os, uuid
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Header
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import select, func, or_
from sqlalchemy.exc import IntegrityError
from models import initialize, SessionLocal, Run, Event, DailyAmount, SourceEvent, ForecastReference, ForecastValue, Command, Incident, Action, AgentTask, RunMetrics
from agent_runtime import api as agent_api
from core import serialize_run, emit, MONTH_DAYS, DAY_START, DAY_END, check_31_days, prior, reference_source
import forecasting, graph_sync

app=FastAPI(title='CHEETAH v1.5 MVP Backend',version='1.5.0')
app.add_middleware(CORSMiddleware,allow_origins=os.getenv('CORS_ORIGINS','http://localhost:8000,http://127.0.0.1:8000').split(','),allow_methods=['GET','POST','PATCH'],allow_headers=['Content-Type','Last-Event-ID','Idempotency-Key'])
HTML=Path(__file__).resolve().parent.parent/'frontend'/'CHEETAH_UI_Prototype_v1.5.html'

@app.on_event('startup')
def start():initialize()

@app.get('/')
def home():return FileResponse(HTML)

class RunRequest(BaseModel):
    dataset_id:str|None=None
    seed:int=42
    mode:str='manual'
    ingestion_mode:str='preloaded_daily'
    event_source_path:str|None=None
    forecast_provider:str='external'

class CommandRequest(BaseModel):
    command:str
    seconds_per_day:float|None=None

class DailyRecord(BaseModel):
    day:str
    amount:float=Field(ge=0)
    commission:float|None=None
    payout:float|None=None
    margin:float|None=None
    source_kind:str='historical'
    source_id:str|None=None

class DatasetRequest(BaseModel):
    dataset_id:str
    records:list[DailyRecord]

class ReferenceRecord(BaseModel):
    id:str
    reference_day:str
    issue_day:str
    point:float
    lower:float
    upper:float
    model_version:str
    source:str='provided_external_model'

class ReferencesRequest(BaseModel):
    dataset_id:str
    references:list[ReferenceRecord]

@app.get('/api/v1/health')
def health():return {'status':'ok','application':'CHEETAH v1.5','forecast_provider':forecasting.config()['status'],'forecast':forecasting.public_status(),'agent_provider':'not_configured','graph_provider':graph_sync.config()['status'],'source_data':'provided_later'}

@app.get('/api/v1/runs')
def list_runs():
    with SessionLocal() as db:
        return {'runs':[serialize_run(r) for r in db.scalars(select(Run).order_by(Run.created_at.desc()).limit(25)).all()]}

@app.post('/api/v1/runs',status_code=201)
def create_run(req:RunRequest):
    if req.mode not in ('manual','auto'):raise HTTPException(422,'Invalid mode')
    if req.ingestion_mode not in ('preloaded_daily','live_events'):raise HTTPException(422,'Invalid ingestion_mode')
    if req.forecast_provider not in ('external','tabpfn'):raise HTTPException(422,'Invalid forecast_provider')
    event_source_path=req.event_source_path
    if req.ingestion_mode=='live_events':
        if not req.dataset_id:raise HTTPException(422,'live_events runs require dataset_id')
        if not event_source_path:
            event_source_path=str(Path(__file__).resolve().parent.parent/'data'/'synthetic'/req.dataset_id/'raw'/'event_timeline.parquet')
        if not Path(event_source_path).exists():raise HTTPException(422,f'event_source_path not found: {event_source_path}')
    with SessionLocal() as db:
        speed=5.0 if req.ingestion_mode=='live_events' else 4.0  # benchmarked safe default (~2.9x headroom, full month in ~2.6min); floor is 3s/day for a faster/thinner-margin push (see core.execute_command)
        run=Run(id='RUN-'+uuid.uuid4().hex[:12].upper(),dataset_id=req.dataset_id,seed=req.seed,mode=req.mode,ingestion_mode=req.ingestion_mode,event_source_path=event_source_path,speed_seconds=speed,forecast_provider=req.forecast_provider)
        db.add(run);db.flush();emit(db,run.id,'simulation.state_changed',serialize_run(run));db.commit()
        return serialize_run(run)

def get_run_metrics(db,run):
    """The six dashboard KPI tiles. live_events runs maintain a RunMetrics row incrementally on
    every insert (core._deliver_rows) so this just reads it — ticking up live, including the
    in-progress day. preloaded_daily runs have deliberately small data volume (today's demo
    adapter pushes ~6k events total), so it's cheap and simpler-to-reason-about to recompute
    live on every call rather than cache a row that would go stale as the run advances."""
    if run.ingestion_mode=='live_events':
        m=db.get(RunMetrics,run.id)
        if m is None:return {'amount':0.0,'commission':0.0,'payout':0.0,'margin':0.0,'transactions':0,'campaigns':0}
        return {'amount':m.total_amount,'commission':m.total_commission,'payout':m.total_payout,'margin':m.total_margin,'transactions':m.total_transactions,'campaigns':m.distinct_campaigns}
    if not run.dataset_id:return None
    revealed=MONTH_DAYS[:run.simulated_day]
    if not revealed:return {'amount':0.0,'commission':0.0,'payout':0.0,'margin':0.0,'transactions':0,'campaigns':0}
    amt=db.execute(select(func.coalesce(func.sum(DailyAmount.amount),0.0),func.coalesce(func.sum(DailyAmount.commission),0.0),func.coalesce(func.sum(DailyAmount.payout),0.0),func.coalesce(func.sum(DailyAmount.margin),0.0)).where(DailyAmount.dataset_id==run.dataset_id,DailyAmount.day.in_(revealed))).one()
    tx=db.scalar(select(func.count()).select_from(SourceEvent).where(SourceEvent.dataset_id==run.dataset_id,SourceEvent.kind=='install',SourceEvent.event_day.in_(revealed))) or 0
    campaigns=db.scalar(select(func.count(func.distinct(SourceEvent.game_placement_id))).where(SourceEvent.dataset_id==run.dataset_id,SourceEvent.event_day.in_(revealed))) or 0
    return {'amount':float(amt[0]),'commission':float(amt[1]),'payout':float(amt[2]),'margin':float(amt[3]),'transactions':tx,'campaigns':campaigns}

@app.get('/api/v1/runs/{run_id}/snapshot')
def snapshot(run_id:str):
    with SessionLocal() as db:
        run=db.get(Run,run_id)
        if not run:raise HTTPException(404,'Run not found')
        entries=db.scalars(select(Incident).where(Incident.run_id==run_id).order_by(Incident.detection_date)).all()
        tasks=db.scalars(select(AgentTask).where(AgentTask.run_id==run_id)).all()
        cursor=db.scalar(select(func.max(Event.event_id)).where(Event.run_id==run_id)) or 0
        rev=db.scalars(select(DailyAmount).where(DailyAmount.dataset_id==run.dataset_id).order_by(DailyAmount.day)).all() if run.dataset_id else []
        src=reference_source(run)
        refs=db.scalars(select(ForecastReference).where(ForecastReference.dataset_id==run.dataset_id,*( [ForecastReference.source==src] if src else []))).all() if run.dataset_id else []
        source=db.scalars(select(SourceEvent).where(SourceEvent.dataset_id==run.dataset_id,SourceEvent.event_day<=MONTH_DAYS[run.simulated_day-1]).order_by(SourceEvent.event_time_utc.desc()).limit(150)).all() if run.dataset_id and run.simulated_day else []
        return {'run':serialize_run(run),'cursor':cursor,'incidents':[incident_dict(i) for i in entries],'tasks':[task_dict(t) for t in tasks], 'daily_data':[{'date':d.day,'amount':d.amount,'commission':d.commission,'payout':d.payout,'margin':d.margin,'source_kind':d.source_kind} for d in rev if d.day in MONTH_DAYS[:run.simulated_day]], 'data_ready':check_31_days(db,run.dataset_id), 'metrics':get_run_metrics(db,run), 'source_events':[{'kind':e.kind,'source_id':e.source_id,'event_time_utc':e.event_time_utc,'game_placement_id':e.game_placement_id,'country':e.country,'amount':e.amount,'commission':e.commission} for e in reversed(source)], 'references':[{'date':f.reference_day,'point':f.point,'lower':f.lower,'upper':f.upper,'forecast_reference_id':f.id} for f in refs if f.reference_day in MONTH_DAYS[:run.simulated_day]]}

def incident_dict(i):return {'incident_id':i.id,'run_id':i.run_id,'scope':i.scope,'metric':i.metric,'game_placement_id':i.game_placement_id,'detection_date':i.detection_date,'direction':i.direction,'actual_amount':i.actual_amount,'lower':i.lower,'upper':i.upper,'reference_forecast_id':i.forecast_reference_id,'lifecycle_status':i.lifecycle_status,'evidence_outcome':i.evidence_outcome}
def task_dict(t):return {'task_id':t.id,'run_id':t.run_id,'incident_id':t.incident_id,'agent':t.agent,'status':t.status,'evidence_outcome':t.evidence_outcome,'fallback':t.fallback}

# ---- Reports: paginated/filterable source events + CSV export + Performance Report rollup ----
SOURCE_EVENT_CSV_COLUMNS=['id','kind','source_id','event_time_utc','event_day','game_placement_id','country','amount','commission','payout','margin','vertical','category','surface']

def _source_event_filters(stmt,dataset_id,kind,game_placement_id,country,event_day_from,event_day_to,q,surface=None):
    stmt=stmt.where(SourceEvent.dataset_id==dataset_id)
    if kind:stmt=stmt.where(SourceEvent.kind==kind)
    if game_placement_id:stmt=stmt.where(SourceEvent.game_placement_id==game_placement_id)
    if country:stmt=stmt.where(SourceEvent.country==country)
    if surface:stmt=stmt.where(SourceEvent.surface==surface)
    if event_day_from:stmt=stmt.where(SourceEvent.event_day>=event_day_from)
    if event_day_to:stmt=stmt.where(SourceEvent.event_day<=event_day_to)
    if q:
        like=f'%{q}%'
        stmt=stmt.where(or_(SourceEvent.source_id.like(like),SourceEvent.game_placement_id.like(like),SourceEvent.country.like(like)))
    return stmt

def _source_event_dict(e):
    return {'id':e.id,'kind':e.kind,'source_id':e.source_id,'event_time_utc':e.event_time_utc,'event_day':e.event_day,'game_placement_id':e.game_placement_id,'country':e.country,'amount':e.amount,'commission':e.commission,'payout':e.payout,'margin':e.margin,'vertical':e.vertical,'category':e.category,'surface':e.surface}

def _stream_source_events_csv(dataset_id,kind,game_placement_id,country,event_day_from,event_day_to,q,surface=None):
    buf=io.StringIO();writer=csv.writer(buf)
    writer.writerow(SOURCE_EVENT_CSV_COLUMNS);yield buf.getvalue();buf.seek(0);buf.truncate(0)
    if not dataset_id:return
    with SessionLocal() as db:
        last_id=0
        while True:
            stmt=_source_event_filters(select(SourceEvent),dataset_id,kind,game_placement_id,country,event_day_from,event_day_to,q,surface).where(SourceEvent.id>last_id).order_by(SourceEvent.id.asc()).limit(5000)
            rows=db.scalars(stmt).all()
            if not rows:break
            for e in rows:writer.writerow([getattr(e,c) for c in SOURCE_EVENT_CSV_COLUMNS])
            yield buf.getvalue();buf.seek(0);buf.truncate(0)
            last_id=rows[-1].id
            if len(rows)<5000:break

@app.get('/api/v1/runs/{run_id}/source_events')
def list_source_events(run_id:str,kind:str|None=None,game_placement_id:str|None=None,country:str|None=None,event_day_from:str|None=None,event_day_to:str|None=None,q:str|None=None,cursor:int|None=None,limit:int=100,sort:str='desc',format:str|None=None,surface:str|None=None):
    """Paginated/filterable listing over the full source_events table for a run — unlike the
    snapshot endpoint's capped-150-rows tail or the SSE stream's recent-batch sampling, this can
    browse/filter/export the complete dataset (up to millions of rows). `q` matches source_id/
    game_placement_id/country only (not every column like the old static-sample UI's client-side
    search — free-text search across amounts/dates isn't practical server-side without a dedicated
    index). `format=csv` streams the full filtered set (not just one page) as a CSV download."""
    if kind is not None and kind not in ('click','install'):raise HTTPException(422,'kind must be click or install')
    if sort not in ('asc','desc'):raise HTTPException(422,'sort must be asc or desc')
    limit=max(1,min(limit,1000))
    with SessionLocal() as db:
        run=db.get(Run,run_id)
        if not run:raise HTTPException(404,'Run not found')
        dataset_id=run.dataset_id
    if format=='csv':
        return StreamingResponse(_stream_source_events_csv(dataset_id,kind,game_placement_id,country,event_day_from,event_day_to,q,surface),media_type='text/csv',headers={'Content-Disposition':f'attachment; filename="source_events_{run_id}.csv"'})
    with SessionLocal() as db:
        if not dataset_id:return {'items':[],'next_cursor':None,'total_count':0}
        base=_source_event_filters(select(SourceEvent),dataset_id,kind,game_placement_id,country,event_day_from,event_day_to,q,surface)
        total=db.scalar(select(func.count()).select_from(base.subquery()))
        stmt=base
        if cursor is not None:
            stmt=stmt.where(SourceEvent.id<cursor) if sort=='desc' else stmt.where(SourceEvent.id>cursor)
        order=SourceEvent.id.desc() if sort=='desc' else SourceEvent.id.asc()
        rows=db.scalars(stmt.order_by(order).limit(limit)).all()
        next_cursor=rows[-1].id if len(rows)==limit else None
        return {'items':[_source_event_dict(e) for e in rows],'next_cursor':next_cursor,'total_count':total}

@app.get('/api/v1/runs/{run_id}/performance_report')
def performance_report(run_id:str):
    """Live GROUP BY (game_placement_id, country) rollup, matching the real Performance Report's
    grain (one row per segment that has at least one transaction — segments with clicks but no
    conversions don't get a row, same as the real export). conversion_rate/ecpc/roas are computed
    with the exact formulas already established in cheetah_synth/export.py's
    populate_derived_metrics (installs/clicks, commission/clicks, amount/commission) — the real
    export itself leaves these NaN, so these are a genuine live enhancement, not sourced data.
    freecash_id/freecash_adspace_id/ecpm/ctr stay null: not pushed to the backend (former) or
    flatly uncomputable without impression data that doesn't exist anywhere, real or synthetic
    (latter)."""
    with SessionLocal() as db:
        run=db.get(Run,run_id)
        if not run:raise HTTPException(404,'Run not found')
        if not run.dataset_id:return {'rows':[],'currency':'EUR','month':'2026-10'}
        clicks_stmt=select(SourceEvent.game_placement_id,SourceEvent.country,func.count().label('clicks')).where(SourceEvent.dataset_id==run.dataset_id,SourceEvent.kind=='click').group_by(SourceEvent.game_placement_id,SourceEvent.country)
        clicks_map={(r.game_placement_id,r.country):r.clicks for r in db.execute(clicks_stmt)}
        installs_stmt=select(
            SourceEvent.game_placement_id,SourceEvent.country,
            func.count().label('transactions'),
            func.coalesce(func.sum(SourceEvent.amount),0.0).label('amount'),
            func.coalesce(func.sum(SourceEvent.commission),0.0).label('commission'),
            func.coalesce(func.sum(SourceEvent.payout),0.0).label('payout'),
            func.coalesce(func.sum(SourceEvent.margin),0.0).label('margin'),
            func.max(SourceEvent.vertical).label('vertical'),
            func.max(SourceEvent.category).label('category'),
        ).where(SourceEvent.dataset_id==run.dataset_id,SourceEvent.kind=='install').group_by(SourceEvent.game_placement_id,SourceEvent.country)
        rows=[]
        for r in db.execute(installs_stmt):
            clicks=clicks_map.get((r.game_placement_id,r.country),0)
            rows.append({
                'freecash_id':None,'freecash_adspace_id':None,
                'game_placement_id':r.game_placement_id,'currency':'EUR','country':r.country,
                'transaction_type':'install','month':'2026-10',
                'amount':r.amount,'commission':r.commission,'payout':r.payout,'margin':r.margin,
                'transaction_count':r.transactions,'clicks':clicks,
                'vertical':r.vertical,'category':r.category,
                'conversion_rate':(r.transactions/clicks) if clicks else None,
                'ecpc':(r.commission/clicks) if clicks else None,
                'ecpm':None,'ctr':None,
                'roas':(r.amount/r.commission) if r.commission else None,
            })
        return {'rows':rows,'currency':'EUR','month':'2026-10'}

@app.post('/api/v1/runs/{run_id}/commands',status_code=202)
def send_command(run_id:str,req:CommandRequest,idempotency_key:str|None=Header(default=None)):
    if req.command not in ('start','pause','resume','stop','step','set_speed'):raise HTTPException(422,'Unsupported command')
    with SessionLocal() as db:
        run=db.get(Run,run_id)
        if not run:raise HTTPException(404,'Run not found')
        cmd_id=idempotency_key or str(uuid.uuid4())
        existing=db.get(Command,cmd_id)
        if existing:
            if existing.run_id!=run_id:raise HTTPException(409,'Idempotency key reused for different run')
            return {'command_id':existing.command_id,'status':existing.status,'error':existing.error}
        payload={'seconds_per_day':req.seconds_per_day} if req.seconds_per_day is not None else {}
        db.add(Command(command_id=cmd_id,run_id=run_id,command=req.command,payload=json.dumps(payload)))
        db.commit();return {'command_id':cmd_id,'status':'queued'}

@app.get('/api/v1/runs/{run_id}/commands/{command_id}')
def command_status(run_id:str,command_id:str):
    with SessionLocal() as db:
        c=db.get(Command,command_id)
        if not c or c.run_id!=run_id:raise HTTPException(404,'Command not found')
        return {'command_id':c.command_id,'status':c.status,'error':c.error}

@app.get('/api/v1/runs/{run_id}/events')
async def events(run_id:str,request:Request,after:int=0):
    with SessionLocal() as db:
        if not db.get(Run,run_id):raise HTTPException(404,'Run not found')
    raw=request.headers.get('last-event-id')
    try:cursor=int(raw) if raw else after
    except ValueError:cursor=after
    async def stream():
        nonlocal cursor
        count=0
        while not await request.is_disconnected():
            with SessionLocal() as db:
                rows=db.scalars(select(Event).where(Event.run_id==run_id,Event.event_id>cursor).order_by(Event.event_id).limit(100)).all()
                for e in rows:
                    cursor=e.event_id
                    packet={'event_id':e.event_id,'schema_version':1,'run_id':run_id,'event_type':e.event_type,'simulation_at':e.business_date,'published_at':e.created_at.isoformat() if e.created_at else None,'source_kind':'simulator','payload':json.loads(e.payload)}
                    yield f'id: {e.event_id}\nevent: {e.event_type}\ndata: {json.dumps(packet)}\n\n'
            count+=1
            if count%20==0:yield ': keepalive\n\n'
            await asyncio.sleep(.5)
    return StreamingResponse(stream(),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

@app.post('/api/v1/datasets/daily',status_code=201)
def upload_dataset(req:DatasetRequest):
    if len(req.records)!=31 or {r.day for r in req.records}!=set(MONTH_DAYS):raise HTTPException(422,'October 2026 requires exactly one record on every date 01–31')
    with SessionLocal() as db:
        if db.scalar(select(func.count()).select_from(DailyAmount).where(DailyAmount.dataset_id==req.dataset_id)):
            raise HTTPException(409,'Immutable dataset ID already exists; create a new version')
        for r in req.records:
            db.add(DailyAmount(dataset_id=req.dataset_id,**r.model_dump()))
        db.commit()
        return {'dataset_id':req.dataset_id,'records':31,'data_ready':True,'note':'Dataset accepted; source provenance is caller-declared and has not been externally verified'}

class SourceEventInput(BaseModel):
    kind:str
    source_id:str
    event_time_utc:str
    game_placement_id:str
    country:str|None=None
    amount:float|None=None
    commission:float|None=None
    extra:dict={}

class SourceBatch(BaseModel):
    dataset_id:str
    events:list[SourceEventInput]

@app.post('/api/v1/datasets/events',status_code=201)
def upload_source_events(req:SourceBatch):
    if not req.events:raise HTTPException(422,'No events')
    with SessionLocal() as db:
        for e in req.events:
            if e.kind not in ('click','install'):raise HTTPException(422,'Source event kind must be click or install')
            if not e.source_id.strip() or not e.game_placement_id.strip():raise HTTPException(422,'Source identifiers must be non-empty strings')
            try:dt=datetime.fromisoformat(e.event_time_utc.replace('Z','+00:00'))
            except Exception:raise HTTPException(422,'Event timestamp must be ISO 8601')
            if dt.tzinfo is None or dt.utcoffset().total_seconds()!=0:raise HTTPException(422,'Event timestamps must be explicit UTC')
            event_day=dt.date().isoformat()
            db.add(SourceEvent(dataset_id=req.dataset_id,kind=e.kind,source_id=e.source_id,event_time_utc=dt.isoformat(),event_day=event_day,game_placement_id=e.game_placement_id,country=e.country,amount=e.amount,commission=e.commission,payload=json.dumps(e.extra)))
        try:db.commit()
        except IntegrityError:
            db.rollback();raise HTTPException(409,'Duplicate source event identity; imports must be idempotent')
    return {'accepted':len(req.events),'note':'Source events retained as separate click/install IDs; no attribution join inferred'}

@app.post('/api/v1/forecasts/references',status_code=201)
def upload_references(req:ReferencesRequest):
    if not req.references:raise HTTPException(422,'No references')
    for r in req.references:
        if r.reference_day not in MONTH_DAYS or r.issue_day!=prior(r.reference_day):raise HTTPException(422,'Each October reference must be issued the preceding calendar day')
        if not r.lower<=r.point<=r.upper:raise HTTPException(422,'Invalid bounds')
    with SessionLocal() as db:
        for r in req.references:db.add(ForecastReference(dataset_id=req.dataset_id,**r.model_dump()))
        try:db.commit()
        except IntegrityError:
            db.rollback();raise HTTPException(409,'Reference IDs/vintages already exist; references are immutable')
    return {'accepted':len(req.references),'note':'Externally supplied reference snapshots; TabPFN not connected'}

class OutlookValue(BaseModel):
    target_day:str
    point:float
    lower:float
    upper:float

class OutlookRequest(BaseModel):
    dataset_id:str
    issue_day:str
    model_version:str
    source:str='provided_external_model'
    daily:list[OutlookValue]

@app.post('/api/v1/forecasts/outlooks',status_code=201)
def upload_outlook(req:OutlookRequest):
    from datetime import date,timedelta
    issue=date.fromisoformat(req.issue_day)
    targets=[(issue+timedelta(days=i)).isoformat() for i in range(1,8)]
    if len(req.daily)!=7 or [v.target_day for v in req.daily]!=targets:
        raise HTTPException(422,'Exactly seven consecutive future daily forecasts required')
    if any(not v.lower<=v.point<=v.upper for v in req.daily):raise HTTPException(422,'Invalid forecast bounds')
    with SessionLocal() as db:
        try:
            locked=forecasting.store_outlook(db,req.dataset_id,req.issue_day,[v.model_dump() for v in req.daily],req.model_version,req.source)
            db.commit()
        except IntegrityError:
            db.rollback();raise HTTPException(409,'Forecast vintage already exists; no overwrite permitted')
    return {'accepted':7,'locked_next_day':locked,'source':req.source}

@app.get('/api/v1/runs/{run_id}/forecasts')
def get_run_forecast(run_id:str):
    with SessionLocal() as db:
        run=db.get(Run,run_id)
        if not run:raise HTTPException(404,'Run not found')
        if not run.dataset_id:return {'issue_day':None,'daily':[],'status':'unavailable'}
        issue=MONTH_DAYS[run.simulated_day-1] if run.simulated_day else '2026-09-30'
        src=reference_source(run)
        values=db.scalars(select(ForecastValue).where(ForecastValue.dataset_id==run.dataset_id,ForecastValue.issue_day==issue,*( [ForecastValue.source==src] if src else [])).order_by(ForecastValue.target_day)).all()
        return {'issue_day':issue,'horizon_days':len(values),'source':values[0].source if values else None,'model_version':values[0].model_version if values else None,'daily':[{'date':v.target_day,'point':v.point,'lower':v.lower,'upper':v.upper,'model_version':v.model_version,'source':v.source} for v in values], 'status':'available' if values else 'unavailable'}

@app.get('/api/v1/runs/{run_id}/incidents')
def get_incidents(run_id:str):
    with SessionLocal() as db:return {'incidents':[incident_dict(i) for i in db.scalars(select(Incident).where(Incident.run_id==run_id)).all()]}

@app.get('/api/v1/incidents/{incident_id}/tasks')
def get_tasks(incident_id:str):
    with SessionLocal() as db:return {'tasks':[task_dict(t) for t in db.scalars(select(AgentTask).where(AgentTask.incident_id==incident_id)).all()]}

@app.post('/api/v1/runs/{run_id}/replays')
def replay_not_ready(run_id:str):
    with SessionLocal() as db:
        if not db.get(Run,run_id):raise HTTPException(404,'Run not found')
    raise HTTPException(409,'Replay requires a verified checkpoint, approved graph version and/or configured sandbox intervention. These data adapters are not supplied yet.')

@app.get('/api/v1/runs/{run_id}/replays/comparison')
def comparison_not_ready(run_id:str):return {'status':'unavailable','reason':'No validated replay branch exists'}

@app.get('/api/v1/context/graph')
def graph_not_ready():return {'status':'unavailable','reason':'Neo4j context model pending','graph_version':None,'nodes':[],'edges':[]}

# ---------------------------------------------------------------------------- agents
# Agents run as scheduler threads in worker.py (agent_runtime). The API only reads their files
# (status/outbox/messages) and writes trigger/event/feedback files; it never touches the threads.

class AgentTriggerRequest(BaseModel):
    reason:str='manual'
    payload:dict|None=None

class AgentEventRequest(BaseModel):
    name:str=Field(min_length=1,max_length=120,pattern=r'^[A-Za-z0-9_.:-]+$')
    payload:dict|None=None

class FixFeedbackRequest(BaseModel):
    decision:str
    reason:str=''

class MetricSubmission(BaseModel):
    # a contributor names and defines a metric; the graph engineer decides where it goes in the graph
    id:str=Field(min_length=1,max_length=80)
    definition:str=Field(min_length=1,max_length=500)
    unit:str
    cypher_value:str=Field(min_length=1,max_length=300)
    source_query:str|None=Field(default=None,max_length=4000)
    domain:str|None=None
    config:dict|None=None

@app.get('/api/v1/agents/status')
def agent_status():
    configured=['forecast_service'] if forecasting.config()['status']=='configured' else []
    return agent_api.status(configured=configured)

@app.get('/api/v1/agents/messages')
def agent_messages(limit:int=50,type:str|None=None,agent:str|None=None):
    return {'messages':agent_api.messages(limit=min(limit,500),type=type,agent=agent)}

@app.get('/api/v1/agents/events')
def agent_events(limit:int=50):return {'events':agent_api.events(min(limit,500))}

@app.post('/api/v1/agents/events',status_code=202)
def agent_event(req:AgentEventRequest):return agent_api.event(req.name,req.payload)

@app.get('/api/v1/agents/briefs')
def agent_briefs(limit:int=10):return {'briefs':agent_api.outbox('briefs',min(limit,200))}

@app.get('/api/v1/agents/discussions')
def agent_discussions(limit:int=30,outcome:str|None=None):
    return {'discussions':agent_api.outbox('discussions',min(limit,500),outcome=outcome)}

@app.get('/api/v1/agents/fixes')
def agent_fixes(limit:int=50,status:str|None=None):
    return {'fixes':agent_api.outbox('fixes',min(limit,500),status=status)}

@app.get('/api/v1/agents/decisions')
def agent_decisions(limit:int=20):return {'decisions':agent_api.outbox('decisions',min(limit,500))}

@app.post('/api/v1/agents/metrics',status_code=202)
def agent_submit_metric(req:MetricSubmission):
    try:return agent_api.submit_metric(req.model_dump(exclude_none=True))
    except ValueError as e:raise HTTPException(422,str(e))

@app.post('/api/v1/agents/fixes/{fix_id}/feedback',status_code=201)
def agent_fix_feedback(fix_id:str,req:FixFeedbackRequest):
    try:return agent_api.fix_feedback(fix_id,req.decision,req.reason)
    except agent_api.NotFound:raise HTTPException(404,'Fix not found')
    except agent_api.Conflict as e:raise HTTPException(409,str(e))
    except ValueError as e:raise HTTPException(422,str(e))

@app.get('/api/v1/agents/{name}/runs')
def agent_runs(name:str,limit:int=20):
    try:return {'runs':agent_api.runs(name,min(limit,200))}
    except agent_api.NotFound:raise HTTPException(404,'Unknown agent')

@app.post('/api/v1/agents/{name}/trigger',status_code=202)
def agent_trigger(name:str,req:AgentTriggerRequest|None=None):
    req=req or AgentTriggerRequest()
    try:return agent_api.trigger(name,req.reason,req.payload)
    except agent_api.NotFound:raise HTTPException(404,'Unknown agent')
