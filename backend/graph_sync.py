"""Day layer for the Neo4j graph: once day D closes (and before the TabPFN forecast for D+1 runs),
write D's end-of-day marketplace, campaign and campaign x country metrics — computed from the
events the backend actually received — so the agents can investigate the day the detector flags.

    (:Dataset {id})-[:CLOSED_DAY {marketplace totals, baseline, forecast band, incident}]->(:Day {id})
    (:Ad {id: game_placement_id})-[:ON_DAY {dataset_id, campaign metrics, baseline}]->(:Day)
    (:Ad)-[:ON_DAY_IN {dataset_id, country, report-row metrics}]->(:Day)
    (:Ad)-[:IN_VERTICAL]->(:Vertical), (:Ad)-[:IN_CATEGORY]->(:Category), (:Country {id})

Ad/Country/Vertical/Category/Day are MERGEd by id, so the layer joins the monthly graph built
from the October files. One dataset at a time: the first sync of a new dataset deletes every
other dataset's day layer. Only closed days are ever written.

Non-fabrication: missing configuration and write failures emit an explicit
`graph.sync_unavailable` and the run carries on. Credentials are read from the environment only
and never emitted.
"""
import os, time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import date, datetime, timedelta, timezone
from sqlalchemy import select, func
from models import DailyAmount, SourceEvent, Incident

BATCH=1000
BASELINE_DAYS=7
WEEKDAYS=['Mon','Tue','Wed','Thu','Fri','Sat','Sun']  # same names paid_sim.py puts on Day nodes
DAY_RELS='ON_DAY|ON_DAY_IN|CLOSED_DAY'


def config():
    try:
        import neo4j  # noqa: F401
        driver=True
    except ImportError:
        driver=False
    creds=bool(os.getenv('NEO4J_URI') and os.getenv('NEO4J_PASSWORD'))
    return {'status':'configured' if (creds and driver) else 'not_configured','credentials_present':creds,
            'driver_installed':driver,'timeout_s':float(os.getenv('GRAPH_SYNC_TIMEOUT_S','30'))}


def _ratio(a, b, nd=6):
    return round(a/b,nd) if b else None


def _metrics(clicks, tx, amount, commission, payout, margin, total_amount):
    """Ratios use the same formulas as app.performance_report (conversion_rate, ecpc -> epc, roas)."""
    return {'clicks':clicks,'transactions':tx,'amount':round(amount,2),'commission':round(commission,2),
            'payout':round(payout,2),'margin':round(margin,2),
            'conversion_rate':_ratio(tx,clicks),'epc':_ratio(commission,clicks),'roas':_ratio(amount,commission),
            'aov':_ratio(amount,tx,2),'margin_rate':_ratio(margin,amount),'share_of_amount':_ratio(amount,total_amount)}


def daily_rollup(db, run, day, reference=None):
    """Day D's metrics from the stored SourceEvents. Marketplace money totals are the DailyAmount
    the detector evaluated (identical to the event sums for live_events runs); shares are of the
    summed campaign amount so they add up to 1."""
    ds=run.dataset_id
    base=(SourceEvent.dataset_id==ds,SourceEvent.event_day==day)
    seg={}
    def slot(gpid,country):
        return seg.setdefault((gpid,country or 'unknown'),{'clicks':0,'tx':0,'amount':0.0,'commission':0.0,'payout':0.0,'margin':0.0,'vertical':None,'category':None})
    for r in db.execute(select(SourceEvent.game_placement_id,SourceEvent.country,func.count(),func.max(SourceEvent.vertical),func.max(SourceEvent.category))
                        .where(*base,SourceEvent.kind=='click').group_by(SourceEvent.game_placement_id,SourceEvent.country)):
        s=slot(r[0],r[1]);s['clicks']=r[2];s['vertical']=r[3];s['category']=r[4]
    for r in db.execute(select(SourceEvent.game_placement_id,SourceEvent.country,func.count(),
                               func.coalesce(func.sum(SourceEvent.amount),0.0),func.coalesce(func.sum(SourceEvent.commission),0.0),
                               func.coalesce(func.sum(SourceEvent.payout),0.0),func.coalesce(func.sum(SourceEvent.margin),0.0),
                               func.max(SourceEvent.vertical),func.max(SourceEvent.category))
                        .where(*base,SourceEvent.kind=='install').group_by(SourceEvent.game_placement_id,SourceEvent.country)):
        s=slot(r[0],r[1]);s.update(tx=r[2],amount=float(r[3]),commission=float(r[4]),payout=float(r[5]),margin=float(r[6]))
        s['vertical']=s['vertical'] or r[7];s['category']=s['category'] or r[8]
    camp={}
    for (gpid,_),s in seg.items():
        c=camp.setdefault(gpid,{'clicks':0,'tx':0,'amount':0.0,'commission':0.0,'payout':0.0,'margin':0.0,'vertical':None,'category':None})
        for k in ('clicks','tx','amount','commission','payout','margin'):c[k]+=s[k]
        c['vertical']=c['vertical'] or s['vertical'];c['category']=c['category'] or s['category']
    total=sum(c['amount'] for c in camp.values())
    m=lambda s:_metrics(s['clicks'],s['tx'],s['amount'],s['commission'],s['payout'],s['margin'],total)
    daily=db.scalar(select(DailyAmount).where(DailyAmount.dataset_id==ds,DailyAmount.day==day))
    market=m({'clicks':sum(c['clicks'] for c in camp.values()),'tx':sum(c['tx'] for c in camp.values()),
              'amount':daily.amount if daily else total,
              'commission':(daily.commission if daily and daily.commission is not None else sum(c['commission'] for c in camp.values())),
              'payout':(daily.payout if daily and daily.payout is not None else sum(c['payout'] for c in camp.values())),
              'margin':(daily.margin if daily and daily.margin is not None else sum(c['margin'] for c in camp.values()))})
    market['share_of_amount']=None
    market['active_campaigns']=len(camp)
    if reference is not None:
        market.update(forecast_point=reference.point,forecast_lower=reference.lower,forecast_upper=reference.upper,
                      classification='negative' if market['amount']<reference.lower else ('positive' if market['amount']>reference.upper else 'within'))
    inc=db.scalar(select(Incident.id).where(Incident.run_id==run.id,Incident.detection_date==day))
    if inc:market['incident_id']=inc
    return {'dataset_id':ds,'run_id':run.id,'day':day,'ingestion_mode':run.ingestion_mode,
            'source_kind':daily.source_kind if daily else 'live_computed','market':market,
            'campaigns':[{'ad':g,'vertical':c['vertical'],'category':c['category'],'m':m(c)} for g,c in sorted(camp.items())],
            'campaign_countries':[{'ad':g,'country':ctry,'m':m(s)} for (g,ctry),s in sorted(seg.items())]}


# Trailing baseline over this dataset's previous BASELINE_DAYS written edges (`prev`), as ratios of
# sums; `y` is yesterday's edge. A day that was never synced only shrinks baseline_days.
_BASELINE_SET="""
WITH {anchor}, d, m, prev, size(prev) AS n, [x IN prev WHERE x.day = $prev_day][0] AS y,
     reduce(s=0.0, x IN prev | s + x.amount) AS amt, reduce(s=0, x IN prev | s + x.transactions) AS tx,
     reduce(s=0, x IN prev | s + x.clicks) AS clk
MERGE {pattern}
SET e += m, e.dataset_id=$ds, e.day=$day, e.run_id=$run_id, e.source_kind=$sk, e.baseline_days=n,
    e.amount_7d_avg = CASE WHEN n=0 THEN null ELSE round(amt/n, 2) END,
    e.transactions_7d_avg = CASE WHEN n=0 THEN null ELSE round(toFloat(tx)/n, 2) END,
    e.clicks_7d_avg = CASE WHEN n=0 THEN null ELSE round(toFloat(clk)/n, 2) END,
    e.conversion_rate_7d_avg = CASE WHEN clk=0 THEN null ELSE round(toFloat(tx)/clk, 6) END,
    e.amount_vs_7d_pct = CASE WHEN n=0 OR amt=0 THEN null ELSE round((m.amount/(amt/n)-1)*100, 2) END,
    e.amount_dod_pct = CASE WHEN y IS NULL OR y.amount=0 THEN null ELSE round((m.amount/y.amount-1)*100, 2) END
"""
Q_HEAD="""
MERGE (ds:Dataset {id:$ds}) SET ds.run_id=$run_id, ds.ingestion_mode=$mode, ds.source_kind=$sk,
    ds.last_closed_day=$day, ds.synced_at=$now
MERGE (d:Day {id:$day}) SET d.weekday=$weekday
"""
Q_CAMPAIGNS="""
UNWIND $rows AS r
MATCH (d:Day {id:$day})
MERGE (a:Ad {id:r.ad})
FOREACH (_ IN CASE WHEN r.vertical IS NULL THEN [] ELSE [1] END | MERGE (v:Vertical {id:r.vertical}) MERGE (a)-[:IN_VERTICAL]->(v))
FOREACH (_ IN CASE WHEN r.category IS NULL THEN [] ELSE [1] END | MERGE (c:Category {id:r.category}) MERGE (a)-[:IN_CATEGORY]->(c))
WITH a, d, r.m AS m
OPTIONAL MATCH (a)-[p:ON_DAY {dataset_id:$ds}]->(pd:Day) WHERE pd.id >= $base_from AND pd.id < $day
WITH a, d, m, collect(p) AS prev
"""+_BASELINE_SET.format(anchor='a',pattern='(a)-[e:ON_DAY {dataset_id:$ds}]->(d)')
Q_COUNTRIES="""
UNWIND $rows AS r
MATCH (d:Day {id:$day})
MERGE (a:Ad {id:r.ad})
MERGE (:Country {id:r.country})
MERGE (a)-[e:ON_DAY_IN {dataset_id:$ds, country:r.country}]->(d)
SET e += r.m, e.day=$day, e.run_id=$run_id, e.source_kind=$sk
"""
Q_MARKET="""
MATCH (ds:Dataset {id:$ds}), (d:Day {id:$day})
WITH ds, d, $market AS m
OPTIONAL MATCH (ds)-[p:CLOSED_DAY]->(pd:Day) WHERE pd.id >= $base_from AND pd.id < $day
WITH ds, d, m, collect(p) AS prev
"""+_BASELINE_SET.format(anchor='ds',pattern='(ds)-[e:CLOSED_DAY {dataset_id:$ds}]->(d)')
Q_OTHER_DATASETS="MATCH (x:Dataset) WHERE x.id <> $ds RETURN count(x) AS n"
Q_DELETE_OLD_EDGES=f"MATCH ()-[e:{DAY_RELS}]->() WHERE e.dataset_id <> $ds WITH e LIMIT 10000 DELETE e RETURN count(e) AS n"
Q_DELETE_OLD_DATASETS="MATCH (x:Dataset) WHERE x.id <> $ds DETACH DELETE x"

_driver=None


def _get_driver():
    global _driver
    if _driver is None:
        from neo4j import GraphDatabase
        _driver=GraphDatabase.driver(os.environ['NEO4J_URI'],auth=(os.getenv('NEO4J_USERNAME','neo4j'),os.environ['NEO4J_PASSWORD']),connection_timeout=10)
    return _driver


def _batches(rows):
    for i in range(0,len(rows),BATCH):yield rows[i:i+BATCH]


def write_day(rollup):
    """Idempotent: re-syncing a day overwrites the same edges. Retention runs first, in bounded
    batches; old Dataset nodes are deleted last, so an interrupted cleanup resumes on the next day."""
    drv=_get_driver();db_name=os.getenv('NEO4J_DATABASE') or None
    ds=rollup['dataset_id'];day=rollup['day'];d=date.fromisoformat(day)
    if drv.execute_query(Q_OTHER_DATASETS,{'ds':ds},database_=db_name).records[0]['n']:
        while drv.execute_query(Q_DELETE_OLD_EDGES,{'ds':ds},database_=db_name).records[0]['n']:pass
        drv.execute_query(Q_DELETE_OLD_DATASETS,{'ds':ds},database_=db_name)
    p={'ds':ds,'day':day,'run_id':rollup['run_id'],'sk':rollup['source_kind'],'mode':rollup['ingestion_mode'],
       'weekday':WEEKDAYS[d.weekday()],'now':datetime.now(timezone.utc).isoformat(timespec='seconds'),
       'prev_day':(d-timedelta(days=1)).isoformat(),'base_from':(d-timedelta(days=BASELINE_DAYS)).isoformat()}
    def tx_fn(tx):
        tx.run(Q_HEAD,p).consume()
        for rows in _batches(rollup['campaigns']):tx.run(Q_CAMPAIGNS,{**p,'rows':rows}).consume()
        for rows in _batches(rollup['campaign_countries']):tx.run(Q_COUNTRIES,{**p,'rows':rows}).consume()
        tx.run(Q_MARKET,{**p,'market':rollup['market']}).consume()
    with drv.session(database=db_name) as s:s.execute_write(tx_fn)


def sync_day(db, run, day, emit, reference=None, writer=None):
    """Called by the worker after day D's close is committed, before the D+1 forecast. Emits
    exactly one of graph.synced / graph.sync_unavailable. Returns True if a write was attempted
    (so the live scheduler doesn't count its wall time as simulated time). Does not commit."""
    cfg=config()
    base={'day':day,'dataset_id':run.dataset_id}
    if writer is None and cfg['status']!='configured':
        emit(db,run.id,'graph.sync_unavailable',{**base,'reason':'not_configured','credentials_present':cfg['credentials_present'],'driver_installed':cfg['driver_installed']},day)
        return False
    t0=time.monotonic()
    ex=ThreadPoolExecutor(max_workers=1)
    try:
        rollup=daily_rollup(db,run,day,reference)
        ex.submit(writer or write_day,rollup).result(timeout=cfg['timeout_s'])
    except FutureTimeout:
        emit(db,run.id,'graph.sync_unavailable',{**base,'reason':'provider_error','error_class':'Timeout'},day);return True
    except Exception as exc:
        emit(db,run.id,'graph.sync_unavailable',{**base,'reason':'provider_error','error_class':type(exc).__name__},day);return True
    finally:
        ex.shutdown(wait=False)
    emit(db,run.id,'graph.synced',{**base,'campaigns':len(rollup['campaigns']),'campaign_country_rows':len(rollup['campaign_countries']),
                                   'duration_ms':int((time.monotonic()-t0)*1000)},day)
    return True
