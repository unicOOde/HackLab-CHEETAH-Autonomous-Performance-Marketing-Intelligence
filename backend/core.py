import json, uuid, calendar
from datetime import datetime, timezone
from sqlalchemy import select, func
from models import Run, Event, Command, DailyAmount, SourceEvent, ForecastReference, ForecastValue, Incident, Action, AgentTask

DAY_START='2026-10-01'
DAY_END='2026-10-31'
MONTH_DAYS=[f'2026-10-{i:02d}' for i in range(1,32)]

def emit(db, run_id, event_type, payload, business_date=None):
    db.add(Event(run_id=run_id,event_type=event_type,payload=json.dumps(payload),business_date=business_date))

def serialize_run(run):
    return {'run_id':run.id,'status':run.status,'mode':run.mode,'speed_seconds':run.speed_seconds,'simulated_day':run.simulated_day,'simulation_date':MONTH_DAYS[run.simulated_day-1] if run.simulated_day else None,'dataset_id':run.dataset_id,'graph_version':run.graph_version,'paused_reason':run.paused_reason,'parent_run_id':run.parent_run_id,'replay_type':run.replay_type}

def check_31_days(db, dataset_id):
    if not dataset_id:return False
    days=set(db.scalars(select(DailyAmount.day).where(DailyAmount.dataset_id==dataset_id)).all())
    return set(MONTH_DAYS)==days

def forecast_for(db, dataset_id, day):
    if not dataset_id: return None
    return db.scalars(select(ForecastReference).where(ForecastReference.dataset_id==dataset_id,ForecastReference.reference_day==day,ForecastReference.issue_day==prior(day)).order_by(ForecastReference.created_at.desc())).first()

def prior(day):
    d=datetime.fromisoformat(day).date()
    from datetime import timedelta
    return (d-timedelta(days=1)).isoformat()

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
        # Initial worker delivers compact event batches per simulated day; finer intra-day scheduling follows when the event dataset is provided.
        payload=[{'kind':e.kind,'source_id':e.source_id,'event_time_utc':e.event_time_utc,'game_placement_id':e.game_placement_id,'country':e.country,'amount':e.amount,'commission':e.commission} for e in events]
        emit(db,run.id,'source.events_batch',{'date':day,'count':len(payload),'events':payload},day)
    run.simulated_day+=1
    metrics={'date':day,'amount':amount.amount,'commission':amount.commission,'payout':amount.payout,'margin':amount.margin,'source_kind':amount.source_kind,'finalized':True}
    emit(db,run.id,'revenue.daily_finalized',metrics,day)
    ref=forecast_for(db,run.dataset_id,day)
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
    future=db.scalars(select(ForecastValue).where(ForecastValue.dataset_id==run.dataset_id,ForecastValue.issue_day==day).order_by(ForecastValue.target_day)).all()
    if len(future)==7:
        emit(db,run.id,'forecast.updated',{'issue_day':day,'horizon_days':7,'daily':[{'date':v.target_day,'point':v.point,'lower':v.lower,'upper':v.upper} for v in future],'source':future[0].source},day)
    else:
        emit(db,run.id,'forecast.unavailable',{'issue_day':day,'reason':'tabpfn_outlook_not_supplied'},day)
    if run.simulated_day==31 and run.status=='running':run.status='completed'
    emit(db,run.id,'simulation.state_changed',serialize_run(run),day)
    db.commit()

def execute_command(db,cmd):
    run=db.get(Run,cmd.run_id)
    if not run: raise ValueError('Run missing')
    payload=json.loads(cmd.payload)
    name=cmd.command
    if name=='start':
        if not check_31_days(db,run.dataset_id): raise ValueError('October dataset must have exactly 31 daily records before playback')
        if run.status not in ('created','ready','paused'):raise ValueError('Cannot start from '+run.status)
        run.status='running';run.paused_reason=None
    elif name=='pause':
        if run.status not in ('running','paused'): raise ValueError('Run is not running')
        run.status='paused';run.paused_reason='operator_pause'
    elif name=='resume':
        if run.status!='paused':raise ValueError('Run is not paused')
        run.status='running';run.paused_reason=None
    elif name=='set_speed':
        speed=float(payload.get('seconds_per_day',4))
        if not (0.5<=speed<=60):raise ValueError('Speed out of range')
        run.speed_seconds=speed
    elif name=='stop':run.status='stopped';run.paused_reason='operator_stop'
    elif name=='step':
        if run.status not in ('paused','ready'): raise ValueError('Step requires paused or ready')
        run.status='running';run_step(db,run)
        if run.status=='running':run.status='paused';run.paused_reason='manual_step'
    else:raise ValueError('Unknown command')
    emit(db,run.id,'simulation.state_changed',serialize_run(run))
    cmd.status='completed';cmd.completed_at=datetime.now(timezone.utc)
    db.commit()
