import os, time
import env  # noqa: F401 — loads the repo-root .env before models reads DATABASE_URL
from sqlalchemy import select
from models import initialize, SessionLocal, Command, Run, utcnow
from core import execute_command, run_step, advance_run_live
from agent_runtime import AgentBus, start_runtime
from agent_runtime.bus import iso

WORKER_HEARTBEAT_S=2.0

def write_worker_heartbeat(bus,started_at,stopped=False):
    # Track A services (Deviation Detector, ...) run inside this loop: the Agents tab shows their
    # state from this heartbeat. Best effort: a full disk must not stop the simulation.
    try:bus.write_status('_worker',{'pid':os.getpid(),'started_at':started_at,'heartbeat_at':iso(),'stopped':stopped})
    except OSError:pass

def main():
    initialize()
    print('CHEETAH v1.5 worker started — one worker only in SQLite development mode',flush=True)
    with SessionLocal() as db:
        # Worker restart: don't let the downtime between process death and restart become a
        # simulated-time catch-up burst for any live run that was still 'running'.
        for run in db.scalars(select(Run).where(Run.status=='running',Run.ingestion_mode=='live_events')).all():
            run.last_tick_wall_utc=None
        db.commit()
    bus=AgentBus().ensure();started_at=iso();last_hb=0.0
    scheduler=start_runtime(bus=bus)   # agent threads (every 30 min / on events / on click); None if disabled
    last_tick={}
    try:
        while True:
            with SessionLocal() as db:
                cmds=db.scalars(select(Command).where(Command.status=='queued').order_by(Command.created_at).limit(10)).all()
                for cmd in cmds:
                    try:execute_command(db,cmd)
                    except Exception as exc:
                        db.rollback();bad=db.get(Command,cmd.command_id);bad.status='failed';bad.error=str(exc);bad.completed_at=utcnow();db.commit()
                runs=db.scalars(select(Run).where(Run.status=='running')).all()
                for run in runs:
                    if run.ingestion_mode=='live_events':
                        advance_run_live(db,run,utcnow())
                    else:
                        now=time.monotonic()
                        if now-last_tick.get(run.id,0)>=run.speed_seconds:
                            run_step(db,run);last_tick[run.id]=now
            if time.monotonic()-last_hb>=WORKER_HEARTBEAT_S:
                write_worker_heartbeat(bus,started_at);last_hb=time.monotonic()
            time.sleep(.1)
    finally:
        if scheduler:scheduler.stop()
        write_worker_heartbeat(bus,started_at,stopped=True)

if __name__=='__main__':main()
