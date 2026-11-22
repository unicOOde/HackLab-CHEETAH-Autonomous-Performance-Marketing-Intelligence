import os, tempfile, sys, uuid
from pathlib import Path
from datetime import date, timedelta

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
_tmp=tempfile.TemporaryDirectory()
os.environ['DATABASE_URL']='sqlite:///'+str(Path(_tmp.name)/'test.db')
from fastapi.testclient import TestClient
from app import app
from models import SessionLocal, Run, Command, Incident, Event, initialize
initialize()
from core import execute_command,run_step

client=TestClient(app)

def test_empty_run_does_not_fake_data():
    r=client.post('/api/v1/runs',json={'dataset_id':None})
    assert r.status_code==201
    run=r.json()['run_id']
    snap=client.get('/api/v1/runs/'+run+'/snapshot').json()
    assert snap['daily_data']==[] and not snap['data_ready']
    assert snap['incidents']==[]
    assert client.get('/api/v1/context/graph').json()['status']=='unavailable'

def test_aggregate_anomaly_pauses_without_creating_campaign_incident():
    name='oct-fixture-'+uuid.uuid4().hex[:8]
    start=date(2026,10,1)
    records=[{'day':(start+timedelta(days=i)).isoformat(),'amount':100.0,'source_kind':'test_only'} for i in range(31)]
    records[10]['amount']=60.0
    assert client.post('/api/v1/datasets/daily',json={'dataset_id':name,'records':records}).status_code==201
    refs=[]
    for i in range(1,31):
        d=start+timedelta(days=i)
        refs.append({'id':'REF-'+str(uuid.uuid4()),'reference_day':d.isoformat(),'issue_day':(d-timedelta(days=1)).isoformat(),'point':100,'lower':90,'upper':110,'model_version':'test-provided'})
    assert client.post('/api/v1/forecasts/references',json={'dataset_id':name,'references':refs}).status_code==201
    source=client.post('/api/v1/datasets/events',json={'dataset_id':name,'events':[{'kind':'click','source_id':'000000001234','event_time_utc':'2026-10-11T14:25:00Z','game_placement_id':'00035m','country':'DE'}]})
    assert source.status_code==201
    run_id=client.post('/api/v1/runs',json={'dataset_id':name}).json()['run_id']
    cmd=client.post('/api/v1/runs/'+run_id+'/commands',json={'command':'start'}).json()['command_id']
    with SessionLocal() as db:
        execute_command(db,db.get(Command,cmd))
        run=db.get(Run,run_id)
        for _ in range(11):run_step(db,run)
        assert run.status=='paused' and run.paused_reason=='anomaly_detected'
        assert run.simulated_day==11
        source_outbox=[e for e in db.query(Event).filter(Event.run_id==run_id,Event.event_type=='source.events_batch').all()]
        assert len(source_outbox)==1
        incidents=db.query(Incident).filter(Incident.run_id==run_id).all()
        assert len(incidents)==1
        assert incidents[0].scope=='aggregate'
        assert incidents[0].game_placement_id is None
        assert incidents[0].direction=='negative'
    snap=client.get('/api/v1/runs/'+run_id+'/snapshot').json()
    assert len(snap['incidents'])==1 and len(snap['daily_data'])==11
    assert any(t['status']=='blocked_configuration' for t in snap['tasks'])
    assert client.post('/api/v1/runs/'+run_id+'/replays',json={'type':'fix_validation'}).status_code==409
