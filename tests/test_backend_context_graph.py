"""backend/context_graph.py + /api/v1/context/*: the Context Explorer's read-only Neo4j view.
Neo4j is replaced by a fake `_read`; nothing here opens a connection."""
import os, tempfile, sys, json
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
_tmp=tempfile.TemporaryDirectory()
os.environ['CHEETAH_SKIP_DOTENV']='1'
os.environ['DATABASE_URL']='sqlite:///'+str(Path(_tmp.name)/'test_context_graph.db')
for k in ('NEO4J_URI','NEO4J_PASSWORD'):os.environ.pop(k,None)
import pytest
from fastapi.testclient import TestClient
from app import app
import graph_sync, context_graph

client=TestClient(app)


class FakeNeo4j:
    """Answers each fixed query by recognising it; records every call."""
    def __init__(self):self.calls=[]
    def __call__(self,cypher,params):
        self.calls.append((cypher,params))
        if cypher==context_graph.Q_DEFAULT:return [{'f':'00006s','d':'ds-1'}]
        if cypher.endswith(context_graph.Q_NODE):
            return [{'props':{'id':params['id'],'ag_risk':0.4}}] if params['id']!='missing' else []
        if cypher.endswith(context_graph.Q_GROUPS):
            ads=[{'id':f'A{i:03d}','name':None,'edges':1,'edge':{'amount':100.0-i}} for i in range(102)]
            return [{'rel':'RUNS','outgoing':True,'label':'Ad','total':102,'top':ads[:params['limit']]},
                    {'rel':'CHANGED','outgoing':False,'label':'Change','total':3,'top':[]}]  # never shown
        if cypher.endswith(context_graph.Q_TOTALS['Freecash']):return [{'ads':102,'tx':3000,'amount':50000.0,'commission':5000.0,'payout':1.0,'margin':2.0}]
        if cypher.endswith(context_graph.Q_LATEST['Freecash']):return [{'day':'2026-10-03','edge':{'amount':1600.0,'amount_vs_7d_pct':-4.2}}]
        if cypher.endswith(context_graph.Q_FINDINGS):return [{'label':'Hypothesis','id':'h1','status':'open','text':'x','confidence':0.3,'severity':None}]
        if cypher.endswith(context_graph.Q_CHANGES):return []
        if cypher==context_graph.Q_SEARCH:
            return [{'label':'Change','id':'chg_1','name':None}]+[{'label':'Ad','id':f'DE{i}','name':None} for i in range(30)]
        raise AssertionError('unexpected query: '+cypher[:80])


@pytest.fixture
def neo(monkeypatch):
    monkeypatch.setattr(graph_sync,'config',lambda:{'status':'configured','credentials_present':True,'driver_installed':True,'timeout_s':5.0})
    f=FakeNeo4j();monkeypatch.setattr(context_graph,'_read',f);return f


def test_not_configured_is_explicit_and_empty():
    for path in ('/api/v1/context/graph','/api/v1/context/search?q=de'):
        b=client.get(path).json()
        assert b['status']=='unavailable' and b['reason']=='not_configured' and b['nodes']==[] and b['edges']==[]


def test_default_centre_groups_capped_and_panel(neo):
    b=client.get('/api/v1/context/graph?limit=12').json()
    assert b['status']=='ok' and b['center']['label']=='Freecash' and b['center']['id']=='00006s'
    [g]=b['groups']  # the Change group is dropped
    assert g['rel']=='RUNS' and g['direction']=='out' and g['total']==102 and len(g['top'])==12
    assert g['top'][0]['id']=='A000' and g['top'][0]['label']=='Ad'
    assert len(b['nodes'])==13 and len(b['edges'])==12 and b['edges'][0]=={'source':'00006s','target':'A000','rel':'RUNS'}
    p=b['panel']
    assert p['totals']['ads']==102 and p['latest_day']['day']=='2026-10-03' and p['findings'][0]['label']=='Hypothesis'
    assert p['relationships']==[{'rel':'RUNS','direction':'out','label':'Ad','total':102}]
    assert b['center']['props']['ag_risk']==0.4


def test_limit_is_clamped(neo):
    b=client.get('/api/v1/context/graph?limit=500').json()
    assert len(b['groups'][0]['top'])==context_graph.MAX_LIMIT


@pytest.mark.parametrize('label',['Change','Ad`) RETURN 1//','Foo'])
def test_bad_label_rejected_before_any_query(neo,label):
    r=client.get('/api/v1/context/graph',params={'label':label,'id':'x'})
    assert r.status_code==400 and neo.calls==[]


def test_unknown_node_is_404(neo):
    assert client.get('/api/v1/context/graph?label=Ad&id=missing').status_code==404


def test_provider_error_is_explicit_and_leaks_nothing(monkeypatch):
    monkeypatch.setattr(graph_sync,'config',lambda:{'status':'configured','credentials_present':True,'driver_installed':True,'timeout_s':5.0})
    monkeypatch.setenv('NEO4J_PASSWORD','secret-neo4j-pw')
    def boom(cypher,params):raise ConnectionError('auth failed with secret-neo4j-pw')
    monkeypatch.setattr(context_graph,'_read',boom)
    b=client.get('/api/v1/context/graph').json()
    assert b['status']=='unavailable' and b['reason']=='provider_error' and b['error_class']=='ConnectionError' and b['nodes']==[]
    assert 'secret-neo4j-pw' not in json.dumps(b)


def test_search_caps_and_excludes_change(neo):
    b=client.get('/api/v1/context/search?q=DE').json()
    assert b['status']=='ok' and len(b['results'])==20 and all(r['label']!='Change' for r in b['results'])
    assert neo.calls[-1][1]=={'q':'de'}
    assert client.get('/api/v1/context/search?q=').json()['results']==[]
