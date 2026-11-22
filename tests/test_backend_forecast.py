"""FC-01 (backend/forecasting.py) tests. The TabPFN provider call is replaced by a deterministic
test-only predictor; nothing here talks to Prior Labs. What is under test is the backend contract:
warm-up, cutoff safety, D+1 locking, source isolation, explicit unavailable states (AT-121) and
the live clock not bursting after inference."""
import os, tempfile, sys, uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
_tmp=tempfile.TemporaryDirectory()
os.environ['CHEETAH_SKIP_DOTENV']='1'  # never load real keys from .env in tests
os.environ['DATABASE_URL']='sqlite:///'+str(Path(_tmp.name)/'test_forecast.db')
os.environ.pop('TABPFN_TOKEN',None)
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from app import app
from models import SessionLocal, Run, Command, Incident, Event, ForecastReference, ForecastValue, initialize
initialize()
import forecasting
from core import execute_command, run_step, advance_run_live

client=TestClient(app)
START=date(2026,10,1)
DAYS=[(START+timedelta(days=i)).isoformat() for i in range(31)]


class FakeProvider:
    """Records every history it is given; returns a fixed 90–110 band around 100."""
    def __init__(self):self.calls=[]
    def __call__(self,history,horizon):
        self.calls.append(list(history))
        return [{'target_day':d,'point':100.0,'lower':90.0,'upper':110.0} for d in forecasting.target_days(history[-1][0],horizon)]


@pytest.fixture
def provider(monkeypatch):
    fake=FakeProvider()
    monkeypatch.setattr(forecasting,'predict_amount',fake)
    monkeypatch.setattr(forecasting,'config',lambda:{'status':'configured','token_present':True,'package_version':'test','warmup_days':7,'context_days':7,'horizon_days':7,'timeout_s':5.0})
    return fake


def _events(db,run_id,event_type):
    import json
    return [json.loads(e.payload) for e in db.query(Event).filter(Event.run_id==run_id,Event.event_type==event_type).order_by(Event.event_id).all()]


def _preloaded_run(amounts,naive_band=None):
    name='fc-'+uuid.uuid4().hex[:8]
    records=[{'day':d,'amount':a,'source_kind':'test_only'} for d,a in zip(DAYS,amounts)]
    assert client.post('/api/v1/datasets/daily',json={'dataset_id':name,'records':records}).status_code==201
    if naive_band:
        refs=[{'id':'REF-'+uuid.uuid4().hex,'reference_day':d,'issue_day':(date.fromisoformat(d)-timedelta(days=1)).isoformat(),'point':sum(naive_band)/2,'lower':naive_band[0],'upper':naive_band[1],'model_version':'naive-test','source':'synthetic_naive_baseline'} for d in DAYS[1:]]
        assert client.post('/api/v1/forecasts/references',json={'dataset_id':name,'references':refs}).status_code==201
    r=client.post('/api/v1/runs',json={'dataset_id':name,'forecast_provider':'tabpfn'})
    assert r.status_code==201 and r.json()['forecast_provider']=='tabpfn'
    run_id=r.json()['run_id']
    cmd=client.post('/api/v1/runs/'+run_id+'/commands',json={'command':'start'}).json()['command_id']
    with SessionLocal() as db:execute_command(db,db.get(Command,cmd))
    return name,run_id


def test_warm_up_then_locked_d_plus_1_reference_and_cutoff_safety(provider):
    amounts=[100.0]*31;amounts[11]=40.0  # Oct 12 breaches the 90–110 band
    # Naive references on the same dataset would breach every day — a tabpfn run must ignore them.
    name,run_id=_preloaded_run(amounts,naive_band=(500,600))
    with SessionLocal() as db:
        run=db.get(Run,run_id)
        for _ in range(12):run_step(db,run)
        assert run.status=='paused' and run.paused_reason=='anomaly_detected' and run.simulated_day==12
        unavailable=_events(db,run_id,'forecast.unavailable')
        assert [u['issue_day'] for u in unavailable]==DAYS[:6] and all(u['reason']=='warm_up' for u in unavailable)
        evaluated=_events(db,run_id,'forecast.evaluated')
        assert [e['date'] for e in evaluated]==DAYS[7:12]  # first TabPFN-evaluated day is Oct 8
        assert all(e['classification']=='within' for e in evaluated[:-1]) and evaluated[-1]['classification']=='negative'
        refs=db.query(ForecastReference).filter(ForecastReference.dataset_id==name,ForecastReference.source=='tabpfn_ts').all()
        assert sorted(r.reference_day for r in refs)==DAYS[7:13]  # Oct 8..13: the Oct 12 close still issues Oct 13 while paused
        assert db.query(ForecastValue).filter(ForecastValue.dataset_id==name,ForecastValue.source=='tabpfn_ts').count()==6*7  # 7-day horizon per issue
        # Trigger: every provider call is announced by forecast.requested (day rollover) before its result.
        kinds=[e.event_type for e in db.query(Event).filter(Event.run_id==run_id,Event.event_type.in_(['forecast.requested','forecast.updated'])).order_by(Event.event_id)]
        assert kinds==['forecast.requested','forecast.updated']*6
        req=_events(db,run_id,'forecast.requested')[0]
        assert req['trigger']=='day_rollover' and req['new_day']==DAYS[7] and req['context_days']==[DAYS[0],DAYS[6]]
        incident=db.query(Incident).filter(Incident.run_id==run_id).one()
        assert incident.detection_date==DAYS[11] and incident.lower==90.0
    # Cutoff + context window: each call saw exactly the 7 closed days ending at its issue day — never
    # a later day, even though all 31 DailyAmount rows were preloaded.
    for i,call in enumerate(provider.calls):
        issue=6+i  # index of the issue day (Oct 7 .. Oct 12)
        assert [d for d,_ in call]==DAYS[issue-6:issue+1]
    # Snapshot/forecast endpoints only expose the tabpfn source, never the naive band.
    snap=client.get('/api/v1/runs/'+run_id+'/snapshot').json()
    assert snap['references'] and all(r['lower']==90.0 for r in snap['references'])
    fc=client.get('/api/v1/runs/'+run_id+'/forecasts').json()
    assert fc['status']=='available' and fc['source']=='tabpfn_ts' and [v['date'] for v in fc['daily']]==DAYS[12:19]


def test_provider_error_is_explicit_unavailable_and_never_an_incident(provider,monkeypatch):
    def boom(history,horizon):raise ConnectionError('provider down')
    monkeypatch.setattr(forecasting,'predict_amount',boom)
    name,run_id=_preloaded_run([100.0]*31)
    with SessionLocal() as db:
        run=db.get(Run,run_id)
        for _ in range(9):run_step(db,run)
        assert run.status=='running' and run.simulated_day==9
        errors=[u for u in _events(db,run_id,'forecast.unavailable') if u['reason']=='provider_error']
        assert [e['issue_day'] for e in errors]==DAYS[6:9] and errors[0]['error_class']=='ConnectionError'
        assert errors[0]['last_valid_issue_day'] is None
        kinds=[e.event_type for e in db.query(Event).filter(Event.run_id==run_id,Event.event_type.in_(['forecast.requested','forecast.unavailable'])).order_by(Event.event_id)]
        assert kinds[6:]==['forecast.requested','forecast.unavailable']*3  # after 6 warm-up unavailables
        assert [e['date'] for e in _events(db,run_id,'forecast.reference_unavailable')]==DAYS[:9]
        assert _events(db,run_id,'forecast.evaluated')==[]
        assert db.query(Incident).filter(Incident.run_id==run_id).count()==0
        assert db.query(ForecastReference).filter(ForecastReference.dataset_id==name).count()==0


def test_not_configured_without_token(monkeypatch,tmp_path):
    assert forecasting.config()['status']=='not_configured'  # TABPFN_TOKEN removed at import
    health=client.get('/api/v1/health').json()
    assert health['forecast_provider']=='not_configured' and health['forecast']['token_loaded'] is False
    monkeypatch.setenv('CHEETAH_BUS_DIR',str(tmp_path))  # an existing (empty) agent bus
    status={a['agent']:a['health'] for a in client.get('/api/v1/agents/status').json()['agents']}
    assert status['forecast_service']=='not_configured'
    monkeypatch.setattr(forecasting,'config',lambda:{'status':'configured'})
    status={a['agent']:a['health'] for a in client.get('/api/v1/agents/status').json()['agents']}
    assert status['forecast_service']==status['deviation_detector']  # runs in the worker loop once configured
    monkeypatch.undo()
    name,run_id=_preloaded_run([100.0]*31)
    with SessionLocal() as db:
        run=db.get(Run,run_id)
        for _ in range(8):run_step(db,run)
        reasons=[u['reason'] for u in _events(db,run_id,'forecast.unavailable')]
        assert reasons==['warm_up']*6+['not_configured']*2
        assert 'TABPFN_TOKEN' not in str(_events(db,run_id,'forecast.unavailable'))


def test_second_run_on_same_dataset_reuses_the_immutable_vintage(provider):
    name,run_a=_preloaded_run([100.0]*31)
    with SessionLocal() as db:
        run=db.get(Run,run_a)
        for _ in range(8):run_step(db,run)
    calls_after_a=len(provider.calls)
    run_b=client.post('/api/v1/runs',json={'dataset_id':name,'forecast_provider':'tabpfn'}).json()['run_id']
    cmd=client.post('/api/v1/runs/'+run_b+'/commands',json={'command':'start'}).json()['command_id']
    with SessionLocal() as db:
        execute_command(db,db.get(Command,cmd))
        run=db.get(Run,run_b)
        for _ in range(8):run_step(db,run)
        assert len(provider.calls)==calls_after_a  # no new provider calls, no duplicate vintages
        assert db.query(ForecastReference).filter(ForecastReference.dataset_id==name).count()==2
        assert len(_events(db,run_b,'forecast.updated'))==2
        assert _events(db,run_b,'forecast.requested')==[]  # reused vintage: no provider call to announce


def test_invalid_forecast_provider_rejected():
    assert client.post('/api/v1/runs',json={'dataset_id':None,'forecast_provider':'magic'}).status_code==422


def test_live_forecast_tick_does_not_burst_into_next_day(provider,monkeypatch):
    monkeypatch.setattr(forecasting,'config',lambda:{'status':'configured','token_present':True,'package_version':'test','warmup_days':1,'context_days':7,'horizon_days':7,'timeout_s':5.0})
    rows=[]
    for i,d in enumerate(DAYS[:3]):
        rows.append({'event_time_utc':datetime(2026,10,i+1,1,0,0,tzinfo=timezone.utc),'kind':'install','source_id':f'i{i}','game_placement_id':'P1','country':'DE','amount':100.0,'commission':1.0,'payout':1.0,'margin':1.0,'event_day':d})
    df=pd.DataFrame(rows);df['event_time_utc']=pd.to_datetime(df['event_time_utc'],utc=True)
    path=str(Path(_tmp.name)/('live-'+uuid.uuid4().hex[:6]+'.parquet'));df.to_parquet(path,index=False)
    name='fc-live-'+uuid.uuid4().hex[:8]
    run_id=client.post('/api/v1/runs',json={'dataset_id':name,'ingestion_mode':'live_events','event_source_path':path,'forecast_provider':'tabpfn'}).json()['run_id']
    cmd=client.post('/api/v1/runs/'+run_id+'/commands',json={'command':'start'}).json()['command_id']
    with SessionLocal() as db:
        execute_command(db,db.get(Command,cmd))
        run=db.get(Run,run_id);run.speed_seconds=30.0;db.commit()
        t0=datetime(2030,1,1)
        advance_run_live(db,run,t0)                       # establishes the reference point
        advance_run_live(db,run,t0+timedelta(seconds=75)) # 2.5 simulated days requested in one tick
        # Day 1 closed, its forecast was issued, and the tick stopped there instead of running on
        # into day 2 before Oct 2's reference existed.
        assert run.simulated_day==1 and run.last_tick_wall_utc is None
        assert db.query(ForecastReference).filter(ForecastReference.dataset_id==name,ForecastReference.reference_day==DAYS[1]).count()==1
        advance_run_live(db,run,t0+timedelta(seconds=200))  # re-establishes reference point only
        assert run.simulated_day==1
        advance_run_live(db,run,t0+timedelta(seconds=232))  # +32s ≈ one more day
        assert run.simulated_day==2
        assert [e['date'] for e in _events(db,run_id,'forecast.evaluated')]==[DAYS[1]]


def test_health_reports_token_loaded_but_never_the_token(monkeypatch):
    monkeypatch.setenv('TABPFN_TOKEN','secret-test-token-value')
    body=client.get('/api/v1/health').text
    assert '"token_loaded":true' in body and 'secret-test-token-value' not in body


def test_load_env_sets_missing_and_never_overrides(monkeypatch,tmp_path):
    import env
    f=tmp_path/'.env'
    f.write_text('# comment\nCHEETAH_T_NEW="from-file"\nCHEETAH_T_SET=from-file\nCHEETAH_T_EMPTY=\nexport CHEETAH_T_EXP=x\n')
    monkeypatch.setenv('CHEETAH_T_SET','from-env')
    monkeypatch.delenv('CHEETAH_SKIP_DOTENV',raising=False)
    for k in ('CHEETAH_T_NEW','CHEETAH_T_EMPTY','CHEETAH_T_EXP'):monkeypatch.delenv(k,raising=False)
    loaded=env.load_env(f)
    assert os.environ['CHEETAH_T_NEW']=='from-file' and os.environ['CHEETAH_T_SET']=='from-env' and os.environ['CHEETAH_T_EXP']=='x'
    assert 'CHEETAH_T_EMPTY' not in os.environ and sorted(loaded)==['CHEETAH_T_EXP','CHEETAH_T_NEW']
    for k in loaded:monkeypatch.delenv(k)
    monkeypatch.setenv('CHEETAH_SKIP_DOTENV','1')
    assert env.load_env(f)==[]
