"""backend/graph_sync.py tests: the day rollup written to Neo4j and its place in the day close
(after the close, before the TabPFN forecast). Neo4j itself is replaced by a recording writer;
nothing here opens a connection."""
import os, tempfile, sys, uuid, json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
_tmp=tempfile.TemporaryDirectory()
os.environ['CHEETAH_SKIP_DOTENV']='1'  # never load real keys from .env in tests
os.environ['DATABASE_URL']='sqlite:///'+str(Path(_tmp.name)/'test_graph_sync.db')
for k in ('TABPFN_TOKEN','NEO4J_URI','NEO4J_PASSWORD'):os.environ.pop(k,None)
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from app import app
from models import SessionLocal, Run, Command, Event, initialize
initialize()
import forecasting, graph_sync
from core import execute_command, run_step, advance_run_live

client=TestClient(app)
DAYS=[(date(2026,10,1)+timedelta(days=i)).isoformat() for i in range(31)]


class Writer:
    def __init__(self,fail=False):self.rollups=[];self.fail=fail
    def __call__(self,rollup):
        if self.fail:raise ConnectionError('aura down')
        self.rollups.append(rollup)


def _events(db,run_id):
    return [(e.event_type,json.loads(e.payload)) for e in db.query(Event).filter(Event.run_id==run_id).order_by(Event.event_id).all()]


def _live_run(rows,provider='external'):
    df=pd.DataFrame(rows);df['event_time_utc']=pd.to_datetime(df['event_time_utc'],utc=True)
    path=str(Path(_tmp.name)/('live-'+uuid.uuid4().hex[:6]+'.parquet'));df.to_parquet(path,index=False)
    name='gs-'+uuid.uuid4().hex[:8]
    run_id=client.post('/api/v1/runs',json={'dataset_id':name,'ingestion_mode':'live_events','event_source_path':path,'forecast_provider':provider}).json()['run_id']
    cmd=client.post('/api/v1/runs/'+run_id+'/commands',json={'command':'start'}).json()['command_id']
    with SessionLocal() as db:execute_command(db,db.get(Command,cmd))
    return name,run_id


def _ev(day_i,kind,sid,gpid,country,amount=None,commission=None,hour=1):
    money=None if kind=='click' else amount
    return {'event_time_utc':datetime(2026,10,day_i+1,hour,0,0,tzinfo=timezone.utc),'kind':kind,'source_id':sid,'game_placement_id':gpid,'country':country,
            'amount':money,'commission':None if kind=='click' else commission,'payout':None if kind=='click' else 1.0,'margin':None if kind=='click' else 0.5,
            'vertical':'Gaming','category':'RPG','event_day':DAYS[day_i]}


def _advance_days(db,run,n):
    t0=datetime(2030,1,1);run.speed_seconds=30.0;db.commit()
    advance_run_live(db,run,t0)
    t=t0
    for _ in range(n):  # each tick closes at most one day (the graph write resets the reference point)
        t+=timedelta(seconds=31);advance_run_live(db,run,t)
        if run.last_tick_wall_utc is None:advance_run_live(db,run,t)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(graph_sync,'config',lambda:{'status':'configured','credentials_present':True,'driver_installed':True,'timeout_s':5.0})
    w=Writer();monkeypatch.setattr(graph_sync,'write_day',w);return w


def test_rollup_totals_ratios_and_hierarchy(configured):
    rows=[_ev(0,'click',f'c{i}','P1','DE') for i in range(4)]+[_ev(0,'click','c9','P1','FR'),_ev(0,'click','c8','P2','DE')]
    rows+=[_ev(0,'install','i1','P1','DE',100.0,10.0),_ev(0,'install','i2','P1','DE',50.0,5.0),_ev(0,'install','i3','P2','US',30.0,3.0)]
    name,run_id=_live_run(rows)
    with SessionLocal() as db:
        run=db.get(Run,run_id);_advance_days(db,run,1)
        assert run.simulated_day==1
    r=configured.rollups[0]
    assert r['dataset_id']==name and r['day']==DAYS[0] and r['source_kind']=='live_computed'
    camp={c['ad']:c for c in r['campaigns']}
    p1=camp['P1']['m']
    assert (p1['clicks'],p1['transactions'],p1['amount'],p1['commission'])==(5,2,150.0,15.0)
    assert p1['conversion_rate']==0.4 and p1['epc']==3.0 and p1['roas']==10.0 and p1['aov']==75.0 and p1['share_of_amount']==round(150/180,6)
    assert camp['P1']['vertical']=='Gaming' and camp['P1']['category']=='RPG'
    seg={(s['ad'],s['country']):s['m'] for s in r['campaign_countries']}
    assert set(seg)=={('P1','DE'),('P1','FR'),('P2','DE'),('P2','US')}
    assert seg[('P2','US')]['clicks']==0 and seg[('P2','US')]['conversion_rate'] is None and seg[('P2','US')]['epc'] is None  # installs without clicks that day
    assert seg[('P1','FR')]['transactions']==0 and seg[('P1','FR')]['aov'] is None and seg[('P1','FR')]['roas'] is None
    m=r['market']
    assert m['amount']==180.0==sum(c['m']['amount'] for c in r['campaigns']) and m['active_campaigns']==2 and m['clicks']==6 and m['transactions']==3


def test_synced_after_close_and_before_forecast(configured,monkeypatch):
    calls=[]
    monkeypatch.setattr(forecasting,'predict_amount',lambda h,n:calls.append(h) or [{'target_day':d,'point':100.0,'lower':0.0,'upper':1e9} for d in forecasting.target_days(h[-1][0],n)])
    monkeypatch.setattr(forecasting,'config',lambda:{'status':'configured','token_present':True,'package_version':'test','warmup_days':1,'context_days':7,'horizon_days':7,'timeout_s':5.0})
    rows=[_ev(i,'install',f'i{i}','P1','DE',100.0,1.0) for i in range(3)]
    name,run_id=_live_run(rows,provider='tabpfn')
    with SessionLocal() as db:
        run=db.get(Run,run_id);_advance_days(db,run,2)
        assert run.simulated_day==2
        types=[t for t,_ in _events(db,run_id)]
    for day_close in ('revenue.daily_finalized',):
        idx=[i for i,t in enumerate(types) if t==day_close]
        assert len(idx)==2
    synced=[i for i,t in enumerate(types) if t=='graph.synced']
    requested=[i for i,t in enumerate(types) if t=='forecast.requested']
    closed=[i for i,t in enumerate(types) if t=='revenue.daily_finalized']
    assert len(synced)==2 and len(requested)==2
    assert all(c<s<q for c,s,q in zip(closed,synced,requested))
    assert [r['day'] for r in configured.rollups]==DAYS[:2]
    # day 2's rollup carries the locked TabPFN reference it was evaluated against
    assert configured.rollups[1]['market']['forecast_point']==100.0 and configured.rollups[1]['market']['classification']=='within'


def test_not_configured_is_explicit_and_playback_continues():
    rows=[_ev(i,'install',f'i{i}','P1','DE',100.0,1.0) for i in range(3)]
    name,run_id=_live_run(rows)
    with SessionLocal() as db:
        run=db.get(Run,run_id);_advance_days(db,run,2)
        assert run.simulated_day==2
        un=[p for t,p in _events(db,run_id) if t=='graph.sync_unavailable']
    assert [u['day'] for u in un]==DAYS[:2] and all(u['reason']=='not_configured' for u in un)


def test_writer_error_is_explicit_and_never_blocks(monkeypatch):
    monkeypatch.setattr(graph_sync,'config',lambda:{'status':'configured','credentials_present':True,'driver_installed':True,'timeout_s':5.0})
    monkeypatch.setattr(graph_sync,'write_day',Writer(fail=True))
    monkeypatch.setenv('NEO4J_PASSWORD','secret-neo4j-pw')
    rows=[_ev(i,'install',f'i{i}','P1','DE',100.0,1.0) for i in range(3)]
    name,run_id=_live_run(rows)
    with SessionLocal() as db:
        run=db.get(Run,run_id);_advance_days(db,run,2)
        assert run.simulated_day==2 and run.status=='running'
        ev=_events(db,run_id)
    un=[p for t,p in ev if t=='graph.sync_unavailable']
    assert len(un)==2 and all(u['reason']=='provider_error' and u['error_class']=='ConnectionError' for u in un)
    assert not any(t=='graph.synced' for t,_ in ev)
    assert 'secret-neo4j-pw' not in json.dumps([p for _,p in ev])


def test_preloaded_run_syncs_each_closed_day(configured):
    name='gs-pre-'+uuid.uuid4().hex[:8]
    records=[{'day':d,'amount':100.0,'source_kind':'test_only'} for d in DAYS]
    assert client.post('/api/v1/datasets/daily',json={'dataset_id':name,'records':records}).status_code==201
    run_id=client.post('/api/v1/runs',json={'dataset_id':name}).json()['run_id']
    cmd=client.post('/api/v1/runs/'+run_id+'/commands',json={'command':'start'}).json()['command_id']
    with SessionLocal() as db:
        execute_command(db,db.get(Command,cmd))
        run=db.get(Run,run_id)
        for _ in range(3):run_step(db,run)
    assert [r['day'] for r in configured.rollups]==DAYS[:3]
    assert all(r['market']['amount']==100.0 and r['source_kind']=='test_only' and r['campaigns']==[] for r in configured.rollups)


def test_health_reports_graph_provider():
    assert client.get('/api/v1/health').json()['graph_provider']=='not_configured'
