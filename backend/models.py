from datetime import datetime, timezone
from sqlalchemy import create_engine, Column, String, Integer, Float, Boolean, DateTime, Text, ForeignKey, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, sessionmaker
import os

class Base(DeclarativeBase): pass

def utcnow(): return datetime.now(timezone.utc)

class Run(Base):
    __tablename__ = 'runs'
    id = Column(String, primary_key=True)
    status = Column(String, nullable=False, default='created')
    mode = Column(String, nullable=False, default='manual')
    speed_seconds = Column(Float, default=4.0)
    simulated_day = Column(Integer, default=0)
    dataset_id = Column(String, nullable=True)
    scenario_id = Column(String, nullable=True)
    seed = Column(Integer, default=42)
    graph_version = Column(String, default='unavailable')
    parent_run_id = Column(String, nullable=True)
    replay_type = Column(String, nullable=True)
    paused_reason = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    updated_at = Column(DateTime(timezone=True), default=utcnow)

class DailyAmount(Base):
    __tablename__ = 'daily_amounts'
    id = Column(Integer, primary_key=True, autoincrement=True)
    dataset_id = Column(String, nullable=False)
    day = Column(String, nullable=False)
    amount = Column(Float, nullable=False)
    commission = Column(Float, nullable=True)
    payout = Column(Float, nullable=True)
    margin = Column(Float, nullable=True)
    source_kind = Column(String, nullable=False, default='historical')
    source_id = Column(String, nullable=True)
    __table_args__ = (UniqueConstraint('dataset_id', 'day'),)

class SourceEvent(Base):
    __tablename__ = 'source_events'
    id = Column(Integer, primary_key=True, autoincrement=True)
    dataset_id = Column(String, nullable=False)
    kind = Column(String, nullable=False)
    source_id = Column(String, nullable=False)
    event_time_utc = Column(String, nullable=False)
    event_day = Column(String, nullable=False)
    game_placement_id = Column(String, nullable=False)
    country = Column(String, nullable=True)
    amount = Column(Float, nullable=True)
    commission = Column(Float, nullable=True)
    payload = Column(Text, nullable=False, default='{}')
    __table_args__ = (UniqueConstraint('dataset_id','kind','source_id'),)

class ForecastReference(Base):
    __tablename__ = 'forecast_references'
    id = Column(String, primary_key=True)
    dataset_id = Column(String, nullable=False)
    reference_day = Column(String, nullable=False)
    issue_day = Column(String, nullable=False)
    point = Column(Float, nullable=False)
    lower = Column(Float, nullable=False)
    upper = Column(Float, nullable=False)
    model_version = Column(String, nullable=False)
    source = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint('dataset_id','reference_day','issue_day','model_version'),)

class ForecastValue(Base):
    __tablename__ = 'forecast_values'
    id = Column(String, primary_key=True)
    dataset_id = Column(String, nullable=False)
    issue_day = Column(String, nullable=False)
    target_day = Column(String, nullable=False)
    point = Column(Float, nullable=False)
    lower = Column(Float, nullable=False)
    upper = Column(Float, nullable=False)
    model_version = Column(String, nullable=False)
    source = Column(String, nullable=False, default='provided_external_model')
    __table_args__ = (UniqueConstraint('dataset_id','issue_day','target_day','model_version'),)

class Event(Base):
    __tablename__ = 'event_outbox'
    event_id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String, ForeignKey('runs.id'), nullable=False)
    event_type = Column(String, nullable=False)
    business_date = Column(String, nullable=True)
    payload = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow)

class Command(Base):
    __tablename__ = 'commands'
    command_id = Column(String, primary_key=True)
    run_id = Column(String, ForeignKey('runs.id'), nullable=False)
    command = Column(String, nullable=False)
    payload = Column(Text, nullable=False, default='{}')
    status = Column(String, nullable=False, default='queued')
    error = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow)
    completed_at = Column(DateTime(timezone=True), nullable=True)

class Incident(Base):
    __tablename__ = 'incidents'
    id = Column(String, primary_key=True)
    run_id = Column(String, ForeignKey('runs.id'), nullable=False)
    metric = Column(String, default='amount_total')
    scope = Column(String, default='aggregate')
    game_placement_id = Column(String, nullable=True)
    detection_date = Column(String, nullable=False)
    direction = Column(String, nullable=False)
    actual_amount = Column(Float, nullable=False)
    lower = Column(Float, nullable=False)
    upper = Column(Float, nullable=False)
    forecast_reference_id = Column(String, nullable=False)
    lifecycle_status = Column(String, default='identified')
    evidence_outcome = Column(String, default='not_evaluated')
    __table_args__ = (UniqueConstraint('run_id','metric','detection_date','direction'),)

class Action(Base):
    __tablename__ = 'actions'
    id = Column(String, primary_key=True)
    run_id = Column(String, ForeignKey('runs.id'), nullable=False)
    incident_id = Column(String, nullable=False)
    type = Column(String, nullable=False)
    parameters = Column(Text, nullable=False, default='{}')
    policy_status = Column(String, nullable=False, default='unreviewed')
    approval_status = Column(String, nullable=False, default='pending')
    execution_status = Column(String, nullable=False, default='not_executed')

class AgentTask(Base):
    __tablename__ = 'agent_tasks'
    id = Column(String, primary_key=True)
    run_id = Column(String, ForeignKey('runs.id'), nullable=False)
    incident_id = Column(String, nullable=False)
    agent = Column(String, nullable=False)
    status = Column(String, nullable=False)
    evidence_outcome = Column(String, nullable=True)
    input_refs = Column(Text, nullable=False, default='[]')
    output_refs = Column(Text, nullable=False, default='[]')
    fallback = Column(String, nullable=True)
    started_at = Column(DateTime(timezone=True), default=utcnow)
    ended_at = Column(DateTime(timezone=True), nullable=True)

DB_URL = os.getenv('DATABASE_URL','sqlite:///./cheetah_dev.db')
engine = create_engine(DB_URL, future=True, connect_args={'check_same_thread':False} if DB_URL.startswith('sqlite') else {})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
def initialize(): Base.metadata.create_all(engine)
