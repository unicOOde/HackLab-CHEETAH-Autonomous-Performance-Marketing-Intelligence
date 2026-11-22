"""Dispatcher: a detected incident is routed to an agent and moves identified -> in_progress -> resolved."""
import os, sys, tempfile, types, uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
_tmp = tempfile.TemporaryDirectory()
os.environ['CHEETAH_SKIP_DOTENV'] = '1'
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + str(Path(_tmp.name) / 'test_dispatch.db'))

import pytest  # noqa: E402
from models import SessionLocal, Run, Incident, AgentTask, SourceEvent, Event, initialize  # noqa: E402
initialize()
from agent_runtime import dispatcher  # noqa: E402
from agent_runtime import builtin  # noqa: E402
from agent_runtime.bus import AgentBus, Message  # noqa: E402
from agent_runtime.scheduler import RunContext  # noqa: E402

DAY = '2026-10-08'


def _seed(drop_ad='AD_DROP'):
    """7 flat days for three ads in two countries, then day 8 where one ad loses most of its revenue."""
    ds = 'ds-' + uuid.uuid4().hex[:8]
    rows = []
    for d in range(1, 9):
        day = f'2026-10-{d:02d}'
        for ad, country, amt in ((drop_ad, 'DE', 100.0), ('AD_B', 'DE', 50.0), ('AD_C', 'US', 50.0)):
            if day == DAY and ad == drop_ad:
                amt = 20.0
            rows.append(SourceEvent(dataset_id=ds, kind='install', source_id=uuid.uuid4().hex, event_time_utc=day + 'T12:00:00',
                                    event_day=day, game_placement_id=ad, country=country, amount=amt, category='Games'))
    run_id = 'RUN-' + uuid.uuid4().hex[:8]
    iid = 'INC-' + uuid.uuid4().hex[:8]
    with SessionLocal() as db:
        db.add_all(rows)
        db.add(Run(id=run_id, dataset_id=ds, status='paused'))
        db.add(Incident(id=iid, run_id=run_id, detection_date=DAY, direction='negative', actual_amount=120.0,
                        lower=180.0, upper=220.0, forecast_reference_id='REF-x'))
        db.commit()
    return ds, run_id, iid


def _ctx(tmp_path, event=None, messages=()):
    bus = AgentBus(tmp_path / 'bus').ensure()
    trig = {'events': [{'name': 'sim.deviation_detected', 'payload': event}]} if event else {'events': []}
    return RunContext(bus, dispatcher.NAME, 'run_test', trig, list(messages))


def test_find_driver_picks_the_narrowest_dominant_segment():
    ds, _, _ = _seed()
    with SessionLocal() as db:
        d = dispatcher.find_driver(db, ds, DAY)
    assert (d['label'], d['id']) == ('Ad', 'AD_DROP')
    assert d['share'] == 1.0 and d['delta'] == -80.0 and d['baseline_days'] == 7


def test_incident_moves_identified_in_progress_resolved(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatcher, '_investigator_unavailable', lambda: None)
    ds, run_id, iid = _seed()
    ctx = _ctx(tmp_path, {'incident_id': iid, 'run_id': run_id, 'dataset_id': ds, 'date': DAY})
    res = dispatcher.run_dispatcher(ctx)
    assert res.status == 'ok'
    with SessionLocal() as db:
        inc = db.get(Incident, iid)
        assert inc.lifecycle_status == 'in_progress' and inc.assigned_agent == 'investigator'
        assert 'Ad AD_DROP explains 100%' in inc.investigation_summary
        tasks = {t.agent: t.status for t in db.query(AgentTask).filter(AgentTask.incident_id == iid)}
        assert tasks == {'dispatcher': 'completed', 'investigator': 'in_progress'}
    [req] = ctx._posts
    assert req.to == 'investigator' and req.type == 'investigate'
    assert req.payload['targets'] == [['Ad', 'AD_DROP']] and req.payload['reply_to'] == 'dispatcher'

    # running again on the same event must not route twice
    dispatcher.run_dispatcher(_ctx(tmp_path, {'incident_id': iid, 'run_id': run_id, 'dataset_id': ds, 'date': DAY}))
    with SessionLocal() as db:
        assert db.query(AgentTask).filter(AgentTask.incident_id == iid, AgentTask.agent == 'investigator').count() == 1

    report = Message(sender='investigator', to='dispatcher', type='report',
                     payload={'incident_id': iid, 'target': ['Ad', 'AD_DROP'],
                              'hypotheses': [{'id': 'H1', 'text': 'postback lag', 'confidence': 0.4},
                                             {'id': 'H2', 'text': 'tracking break', 'confidence': 0.7}]})
    dispatcher.run_dispatcher(_ctx(tmp_path, messages=[report]))
    with SessionLocal() as db:
        inc = db.get(Incident, iid)
        assert (inc.lifecycle_status, inc.evidence_outcome) == ('resolved', 'hypotheses_proposed')
        assert 'tracking break' in inc.investigation_summary
        t = db.query(AgentTask).filter(AgentTask.incident_id == iid, AgentTask.agent == 'investigator').one()
        assert t.status == 'completed' and 'H2' in t.output_refs
        updates = [e.event_type for e in db.query(Event).filter(Event.run_id == run_id)]
        assert updates.count('incident.updated') == 2


def test_report_without_hypotheses_resolves_inconclusive(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatcher, '_investigator_unavailable', lambda: None)
    ds, run_id, iid = _seed()
    dispatcher.run_dispatcher(_ctx(tmp_path, {'incident_id': iid, 'run_id': run_id, 'dataset_id': ds, 'date': DAY}))
    report = Message(sender='investigator', to='dispatcher', type='report',
                     payload={'incident_id': iid, 'error': 'GraphWriteError: Ad AD_DROP not in graph'})
    dispatcher.run_dispatcher(_ctx(tmp_path, messages=[report]))
    with SessionLocal() as db:
        inc = db.get(Incident, iid)
        assert (inc.lifecycle_status, inc.evidence_outcome) == ('resolved', 'inconclusive')
        assert 'not in graph' in inc.investigation_summary
        assert db.query(AgentTask).filter(AgentTask.incident_id == iid, AgentTask.agent == 'investigator').one().status == 'failed'


def test_unavailable_investigator_blocks_instead_of_faking(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatcher, '_investigator_unavailable', lambda: 'ANTHROPIC_API_KEY not set')
    ds, run_id, iid = _seed()
    ctx = _ctx(tmp_path, {'incident_id': iid, 'run_id': run_id, 'dataset_id': ds, 'date': DAY})
    res = dispatcher.run_dispatcher(ctx)
    assert res.status == 'degraded' and ctx._posts == []
    with SessionLocal() as db:
        inc = db.get(Incident, iid)
        assert inc.lifecycle_status == 'identified' and inc.evidence_outcome == 'not_evaluated'
        t = db.query(AgentTask).filter(AgentTask.incident_id == iid, AgentTask.agent == 'investigator').one()
        assert t.status == 'blocked_configuration' and 'ANTHROPIC_API_KEY' in t.fallback


def test_investigator_replies_to_requester(tmp_path, monkeypatch):
    class FakeInv:
        def __init__(self, run_overlooker=False):
            self.store = types.SimpleNamespace(close=lambda: None, get=lambda label, hid: {'text': 'x', 'confidence': 0.5})

        def investigate(self, label, node_id):
            return types.SimpleNamespace(hypotheses=['H1'], updates=[], dropped=[])

    monkeypatch.setattr(builtin, '_prepare_imports', lambda: None)
    monkeypatch.setitem(sys.modules, 'agents.investigator_agent', types.SimpleNamespace(InvestigatorAgent=FakeInv))
    req = Message(sender='dispatcher', to='investigator', type='investigate',
                  payload={'targets': [['Ad', 'A1']], 'reply_to': 'dispatcher', 'incident_id': 'INC-1'})
    other = Message(sender='pacing_watcher', to='investigator', type='investigate', payload={'targets': [['Ad', 'A2']]})
    ctx = RunContext(AgentBus(tmp_path / 'bus').ensure(), 'investigator', 'r', {}, [req, other])
    builtin.run_investigator(ctx)
    replies = [m for m in ctx._posts if m.to == 'dispatcher']
    assert len(replies) == 1 and replies[0].payload['incident_id'] == 'INC-1'
    assert replies[0].payload['target'] == ['Ad', 'A1']
    assert sum(m.to == 'product_manager' for m in ctx._posts) == 2
