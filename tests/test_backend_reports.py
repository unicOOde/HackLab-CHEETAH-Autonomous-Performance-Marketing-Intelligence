"""Tests for the Reports screen's new backend endpoints: paginated/filterable source_events
listing + CSV export, and the performance_report aggregation rollup."""
import os, tempfile, sys, uuid
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
_tmp=tempfile.TemporaryDirectory()
os.environ['CHEETAH_SKIP_DOTENV']='1'  # never load real keys from .env in tests
os.environ['DATABASE_URL']='sqlite:///'+str(Path(_tmp.name)/'test_reports.db')
from fastapi.testclient import TestClient
from app import app
from models import SessionLocal, SourceEvent, initialize
initialize()

client=TestClient(app)


def _make_dataset():
    """P1/DE: 5 clicks + 2 installs (amount=100, commission=20 each). P2/FR: 3 clicks + 1 install
    (amount=50, commission=10). Vertical/category are constant per placement, as they are in
    reality (placement-level attributes)."""
    dataset_id='reports-'+uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        day='2026-10-01'
        for i in range(5):
            db.add(SourceEvent(dataset_id=dataset_id,kind='click',source_id=f'p1c{i}',event_time_utc=f'2026-10-01T00:00:0{i}+00:00',event_day=day,game_placement_id='P1',country='DE',vertical='Gaming',category='Action'))
        for i in range(2):
            db.add(SourceEvent(dataset_id=dataset_id,kind='install',source_id=f'p1i{i}',event_time_utc=f'2026-10-01T01:00:0{i}+00:00',event_day=day,game_placement_id='P1',country='DE',amount=100.0,commission=20.0,payout=10.0,margin=10.0,vertical='Gaming',category='Action'))
        for i in range(3):
            db.add(SourceEvent(dataset_id=dataset_id,kind='click',source_id=f'p2c{i}',event_time_utc=f'2026-10-01T02:00:0{i}+00:00',event_day=day,game_placement_id='P2',country='FR',vertical='Retail',category='Fashion'))
        db.add(SourceEvent(dataset_id=dataset_id,kind='install',source_id='p2i0',event_time_utc='2026-10-01T03:00:00+00:00',event_day=day,game_placement_id='P2',country='FR',amount=50.0,commission=10.0,payout=5.0,margin=5.0,vertical='Retail',category='Fashion'))
        db.commit()
    r=client.post('/api/v1/runs',json={'dataset_id':dataset_id})
    assert r.status_code==201
    return dataset_id, r.json()['run_id']


def test_source_events_total_count_and_filtering():
    _, run_id = _make_dataset()
    assert client.get(f'/api/v1/runs/{run_id}/source_events').json()['total_count']==11  # 5+2+3+1
    assert client.get(f'/api/v1/runs/{run_id}/source_events',params={'kind':'install'}).json()['total_count']==3
    assert client.get(f'/api/v1/runs/{run_id}/source_events',params={'game_placement_id':'P2'}).json()['total_count']==4
    assert client.get(f'/api/v1/runs/{run_id}/source_events',params={'country':'DE','kind':'click'}).json()['total_count']==5
    assert client.get(f'/api/v1/runs/{run_id}/source_events',params={'q':'p2i'}).json()['total_count']==1
    assert client.get(f'/api/v1/runs/{run_id}/source_events',params={'kind':'bogus'}).status_code==422


def test_source_events_pagination_cursor_covers_every_row_exactly_once():
    _, run_id = _make_dataset()
    seen=set(); cursor=None; pages=0
    while True:
        params={'limit':4}
        if cursor is not None:params['cursor']=cursor
        body=client.get(f'/api/v1/runs/{run_id}/source_events',params=params).json()
        for item in body['items']:
            assert item['id'] not in seen, 'a row was returned twice across pages'
            seen.add(item['id'])
        pages+=1
        cursor=body['next_cursor']
        if cursor is None:break
        assert pages<10  # safety net against an infinite loop
    assert len(seen)==11


def test_source_events_vertical_category_present():
    _, run_id = _make_dataset()
    body=client.get(f'/api/v1/runs/{run_id}/source_events',params={'game_placement_id':'P1','limit':1}).json()
    assert body['items'][0]['vertical']=='Gaming'
    assert body['items'][0]['category']=='Action'


def test_source_events_csv_export_matches_filtered_set():
    _, run_id = _make_dataset()
    r=client.get(f'/api/v1/runs/{run_id}/source_events',params={'kind':'install','format':'csv'})
    assert r.status_code==200
    lines=r.text.replace('\r\n','\n').strip('\n').split('\n')
    assert len(lines)==1+3  # header + 3 install rows (2 for P1, 1 for P2)
    assert lines[0].split(',')[0]=='id'


def test_source_events_empty_dataset_returns_empty_not_an_error():
    r=client.post('/api/v1/runs',json={})
    run_id=r.json()['run_id']
    body=client.get(f'/api/v1/runs/{run_id}/source_events').json()
    assert body=={'items':[],'next_cursor':None,'total_count':0}
    csv_r=client.get(f'/api/v1/runs/{run_id}/source_events',params={'format':'csv'})
    assert csv_r.text.replace('\r\n','\n').strip('\n')=='id,kind,source_id,event_time_utc,event_day,game_placement_id,country,amount,commission,payout,margin,vertical,category'


def test_performance_report_computes_conversion_rate_ecpc_roas():
    _, run_id = _make_dataset()
    body=client.get(f'/api/v1/runs/{run_id}/performance_report').json()
    rows={r['game_placement_id']:r for r in body['rows']}
    assert set(rows)=={'P1','P2'}

    p1=rows['P1']
    assert p1['amount']==200.0 and p1['commission']==40.0
    assert p1['transaction_count']==2
    assert p1['clicks']==5
    assert p1['vertical']=='Gaming' and p1['category']=='Action'
    assert abs(p1['conversion_rate']-(2/5))<1e-9
    assert abs(p1['ecpc']-(40.0/5))<1e-9
    assert abs(p1['roas']-(200.0/40.0))<1e-9
    assert p1['currency']=='EUR' and p1['transaction_type']=='install'
    assert p1['freecash_id'] is None and p1['ecpm'] is None and p1['ctr'] is None

    p2=rows['P2']
    assert abs(p2['conversion_rate']-(1/3))<1e-9
    assert abs(p2['ecpc']-(10.0/3))<1e-9
    assert abs(p2['roas']-(50.0/10.0))<1e-9
